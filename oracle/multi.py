"""Cross-repo comparison for multi-fork workspaces.

DreamMaker paths are repo-global identifiers: ``/obj/item/proc/attack_self``
exists in every fork, and a dozen forks may each define their own version. That
makes a single merged index a bad idea — every symbol would become ambiguous
and every query would need a repo prefix.

So each fork keeps its own database (``<repo>/.oracle/oracle.db``) and this
module compares two of them. That is what porting between forks actually needs:
which procs exist only upstream, which ones diverged, and where a ported proc
would be called from in the target tree.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import query

DEFAULT_KINDS = ("proc", "type")


@dataclass(frozen=True)
class SymbolRecord:
    path: str
    kind: str
    signature: str | None
    body_hash: str | None
    file: str
    start_line: int
    end_line: int
    doc: str | None = None


@dataclass
class DiffReport:
    left_name: str
    right_name: str
    only_left: list[SymbolRecord] = field(default_factory=list)
    only_right: list[SymbolRecord] = field(default_factory=list)
    changed: list[tuple[SymbolRecord, SymbolRecord]] = field(default_factory=list)
    counts: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def shared(self) -> int:
        return sum(row["shared"] for row in self.counts.values())


@dataclass
class PortReport:
    symbol: str
    source_name: str
    target_name: str
    status: str
    source: SymbolRecord | None = None
    target: SymbolRecord | None = None
    source_code: str = ""
    target_code: str = ""
    signature_changed: bool = False
    body_changed: bool = False
    owner: str | None = None
    owner_missing: bool = False
    target_callers: list[query.CallEdge] = field(default_factory=list)
    source_callees: list[query.CallEdge] = field(default_factory=list)
    source_members: int = 0
    target_members: int = 0


def load_records(conn: sqlite3.Connection, kinds: tuple[str, ...] = DEFAULT_KINDS) -> dict[str, SymbolRecord]:
    """Map canonical path → first declaration, for the requested kinds."""
    placeholders = ",".join("?" for _ in kinds)
    rows = conn.execute(
        f"""
        SELECT s.path, s.kind, s.signature, s.body_hash, s.doc,
               s.start_line, s.end_line, f.path AS file
        FROM symbols s JOIN files f ON f.id = s.file_id
        WHERE s.kind IN ({placeholders})
        ORDER BY s.path, s.id
        """,
        list(kinds),
    )
    records: dict[str, SymbolRecord] = {}
    for row in rows:
        records.setdefault(
            row["path"],
            SymbolRecord(
                path=row["path"],
                kind=row["kind"],
                signature=row["signature"],
                body_hash=row["body_hash"],
                file=row["file"],
                start_line=int(row["start_line"]),
                end_line=int(row["end_line"]),
                doc=row["doc"],
            ),
        )
    return records


def diff(
    left_conn: sqlite3.Connection,
    right_conn: sqlite3.Connection,
    left_name: str,
    right_name: str,
    kinds: tuple[str, ...] = DEFAULT_KINDS,
) -> DiffReport:
    """Compare two indexes by canonical path.

    ``only_right`` is the porting list: symbols that exist in the other fork but
    not here. ``changed`` means both sides define the path, but the body hash or
    the signature differs.
    """
    left = load_records(left_conn, kinds)
    right = load_records(right_conn, kinds)
    report = DiffReport(left_name=left_name, right_name=right_name)

    counts = {
        kind: {"left": 0, "right": 0, "shared": 0, "only_left": 0, "only_right": 0, "changed": 0}
        for kind in kinds
    }
    for record in left.values():
        counts.setdefault(record.kind, {"left": 0, "right": 0, "shared": 0, "only_left": 0, "only_right": 0, "changed": 0})
        counts[record.kind]["left"] += 1
    for record in right.values():
        counts.setdefault(record.kind, {"left": 0, "right": 0, "shared": 0, "only_left": 0, "only_right": 0, "changed": 0})
        counts[record.kind]["right"] += 1

    for path, record in left.items():
        other = right.get(path)
        if other is None:
            report.only_left.append(record)
            counts[record.kind]["only_left"] += 1
            continue
        counts[record.kind]["shared"] += 1
        if record.body_hash != other.body_hash or record.signature != other.signature:
            report.changed.append((record, other))
            counts[record.kind]["changed"] += 1

    for path, record in right.items():
        if path not in left:
            report.only_right.append(record)
            counts[record.kind]["only_right"] += 1

    report.only_left.sort(key=lambda record: record.path)
    report.only_right.sort(key=lambda record: record.path)
    report.changed.sort(key=lambda pair: pair[0].path)
    report.counts = counts
    return report


def _record_for(conn: sqlite3.Connection, symbol: str) -> tuple[SymbolRecord | None, query.SymbolHit | None]:
    hits = query.get_symbol(conn, symbol)
    if not hits:
        return None, None
    hit = hits[0]
    row = conn.execute(
        """
        SELECT s.path, s.kind, s.signature, s.body_hash, s.doc,
               s.start_line, s.end_line, f.path AS file
        FROM symbols s JOIN files f ON f.id = s.file_id
        WHERE s.path = ? ORDER BY s.id LIMIT 1
        """,
        (hit.path,),
    ).fetchone()
    if row is None:
        return None, hit
    return (
        SymbolRecord(
            path=row["path"],
            kind=row["kind"],
            signature=row["signature"],
            body_hash=row["body_hash"],
            file=row["file"],
            start_line=int(row["start_line"]),
            end_line=int(row["end_line"]),
            doc=row["doc"],
        ),
        hit,
    )


def _owner(path: str) -> str:
    """``/obj/item/proc/foo`` → ``/obj/item``; a type path is its own owner."""
    for marker in ("/proc/", "/var/", "/verb/"):
        if marker in path:
            return path.split(marker, 1)[0]
    return path


def _type_family(conn: sqlite3.Connection, root: str, limit: int = 5000) -> set[str]:
    """The root type plus every type that inherits from it."""
    children: dict[str, list[str]] = {}
    for row in conn.execute("SELECT child, parent FROM inherits"):
        children.setdefault(row["parent"], []).append(row["child"])
    family = {root}
    stack = [root]
    while stack and len(family) < limit:
        node = stack.pop()
        for child in children.get(node, ()):
            if child not in family:
                family.add(child)
                stack.append(child)
    return family


def _call_sites(
    conn: sqlite3.Connection,
    path: str,
    name: str,
    owner: str,
    limit: int = 50,
) -> list[query.CallEdge]:
    """Call sites that could reach this symbol once it is ported.

    A bare name match is not enough — every `turn_on()` in the tree would
    qualify. So matches are restricted to callers whose owner type is the
    symbol's owner or a descendant of it, which is the set that would actually
    dispatch to the ported proc.
    """
    family = _type_family(conn, owner)
    rows = conn.execute(
        """
        SELECT cs.path AS caller_path, cs.parent AS caller_parent,
               cf.path AS caller_file, cs.start_line AS caller_line,
               c.callee_raw, c.callee_path, c.resolution, c.line
        FROM calls c
        LEFT JOIN symbols cs ON cs.id = c.caller_symbol_id
        LEFT JOIN files cf ON cf.id = cs.file_id
        WHERE c.callee_path = ? OR c.callee_raw = ? OR c.callee_raw LIKE ?
        LIMIT 4000
        """,
        (path, name, f"%.{name}"),
    ).fetchall()

    edges: list[query.CallEdge] = []
    for row in rows:
        if row["callee_path"] == path:
            keep = True
        else:
            keep = row["caller_parent"] in family or row["caller_path"] == owner
        if not keep:
            continue
        edges.append(
            query.CallEdge(
                caller_path=row["caller_path"],
                caller_file=row["caller_file"],
                caller_line=row["caller_line"],
                callee_raw=row["callee_raw"],
                callee_path=row["callee_path"],
                resolution=row["resolution"],
                line=int(row["line"]),
            )
        )
        if len(edges) >= limit:
            break
    return edges


def _member_count(conn: sqlite3.Connection, type_path: str) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM symbols WHERE parent = ?", (type_path,)).fetchone()
    return int(row["n"]) if row else 0


def port(
    target_conn: sqlite3.Connection,
    target_root: Path,
    source_conn: sqlite3.Connection,
    source_root: Path,
    symbol: str,
    source_name: str,
    target_name: str,
) -> PortReport:
    """Describe what porting ``symbol`` from one fork into another involves."""
    report = PortReport(symbol=symbol, source_name=source_name, target_name=target_name, status="unknown")

    source_record, source_hit = _record_for(source_conn, symbol)
    target_record, target_hit = _record_for(target_conn, symbol)

    report.source = source_record
    report.target = target_record
    if source_record is None:
        report.status = "missing-upstream"
        return report

    if source_hit is not None:
        report.source_code = query.source_span(source_root, source_hit)
    if target_hit is not None:
        report.target_code = query.source_span(target_root, target_hit)

    if target_record is None:
        report.status = "missing"
    else:
        report.signature_changed = source_record.signature != target_record.signature
        report.body_changed = source_record.body_hash != target_record.body_hash
        report.status = "changed" if (report.signature_changed or report.body_changed) else "identical"

    name = source_record.path.rsplit("/", 1)[-1]
    owner = _owner(source_record.path)
    report.owner = owner
    report.owner_missing = (
        owner != source_record.path
        and target_conn.execute("SELECT 1 FROM symbols WHERE path = ? LIMIT 1", (owner,)).fetchone()
        is None
    )
    report.target_callers = _call_sites(target_conn, source_record.path, name, owner, limit=50)

    if source_hit is not None:
        report.source_callees = query.callees(source_conn, source_hit.id, limit=100)

    if source_record.kind == "type":
        report.source_members = _member_count(source_conn, source_record.path)
        report.target_members = _member_count(target_conn, target_record.path) if target_record else 0

    return report
