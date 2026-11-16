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


@dataclass(frozen=True)
class Rename:
    """The same body under a new path — a rename, not a port candidate."""

    left: SymbolRecord
    right: SymbolRecord


@dataclass
class DiffReport:
    left_name: str
    right_name: str
    only_left: list[SymbolRecord] = field(default_factory=list)
    only_right: list[SymbolRecord] = field(default_factory=list)
    changed: list[tuple[SymbolRecord, SymbolRecord]] = field(default_factory=list)
    renamed: list[Rename] = field(default_factory=list)
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


def _empty_counts() -> dict[str, int]:
    return {
        "left": 0,
        "right": 0,
        "shared": 0,
        "only_left": 0,
        "only_right": 0,
        "changed": 0,
        "renamed": 0,
    }


# A rename keeps the body and changes the name, so the signature cannot be the
# key. A body of at least three lines keeps trivial `return` procs from pairing
# with each other; a unique match on both sides does the rest.
_RENAME_MIN_SPAN = 2


def _rename_key(record: SymbolRecord) -> tuple[str, str] | None:
    if not record.body_hash:
        return None
    if record.end_line - record.start_line < _RENAME_MIN_SPAN:
        return None
    return record.kind, record.body_hash


def _detect_renames(report: DiffReport, counts: dict[str, dict[str, int]]) -> None:
    """Move same-body pairs out of ``only_left``/``only_right`` into ``renamed``."""
    left: dict[tuple[str, str], list[SymbolRecord]] = {}
    for record in report.only_left:
        key = _rename_key(record)
        if key is not None:
            left.setdefault(key, []).append(record)
    right: dict[tuple[str, str], list[SymbolRecord]] = {}
    for record in report.only_right:
        key = _rename_key(record)
        if key is not None:
            right.setdefault(key, []).append(record)

    paired_left: set[str] = set()
    paired_right: set[str] = set()
    for key, left_records in left.items():
        right_records = right.get(key)
        # Two candidates on either side means the body is not distinctive
        # enough to claim a rename.
        if not right_records or len(left_records) != 1 or len(right_records) != 1:
            continue
        pair = Rename(left=left_records[0], right=right_records[0])
        report.renamed.append(pair)
        paired_left.add(pair.left.path)
        paired_right.add(pair.right.path)
        row = counts.setdefault(pair.left.kind, _empty_counts())
        row["renamed"] += 1
        row["only_left"] -= 1
        row["only_right"] -= 1

    if not report.renamed:
        return
    report.only_left = [record for record in report.only_left if record.path not in paired_left]
    report.only_right = [record for record in report.only_right if record.path not in paired_right]
    report.renamed.sort(key=lambda pair: pair.left.path)


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
    the signature differs. ``renamed`` pairs a path that only exists on one side
    with a path that only exists on the other when the body is identical, so a
    moved proc is not reported as a port candidate twice.
    """
    left = load_records(left_conn, kinds)
    right = load_records(right_conn, kinds)
    report = DiffReport(left_name=left_name, right_name=right_name)

    counts = {kind: _empty_counts() for kind in kinds}
    for record in left.values():
        counts.setdefault(record.kind, _empty_counts())["left"] += 1
    for record in right.values():
        counts.setdefault(record.kind, _empty_counts())["right"] += 1

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
    _detect_renames(report, counts)
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
            # A global proc has no owner type; `/` is the global root.
            return path.split(marker, 1)[0] or "/"
    return path


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
    family = query.type_family(conn, owner)
    escaped_name = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = conn.execute(
        """
        SELECT cs.path AS caller_path, cs.parent AS caller_parent,
               cf.path AS caller_file, cs.start_line AS caller_line,
               c.callee_raw, c.callee_path, c.resolution, c.line
        FROM calls c
        LEFT JOIN symbols cs ON cs.id = c.caller_symbol_id
        LEFT JOIN files cf ON cf.id = cs.file_id
        WHERE c.callee_path = ? OR c.callee_raw = ? OR c.callee_raw LIKE ? ESCAPE '\\'
        ORDER BY c.id
        LIMIT 4000
        """,
        (path, name, f"%.{escaped_name}"),
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
        owner not in ("/", source_record.path)
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
