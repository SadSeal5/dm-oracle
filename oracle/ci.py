"""Change impact for CI.

Given a unified diff, find the symbols whose source lines changed, expand them
along the reverse call graph, and suggest unit tests that exercise them.

The index is assumed to describe the *new* side of the diff (that is what
`oracle index` produces when it runs before this command). Deleted lines are
therefore anchored at the hunk's new-side position, which maps to the symbol
that still encloses that point.

A deleted file has no new side at all, so there is nothing to anchor to.
For those the diff's removed lines are parsed back into symbols, and the call
sites that referenced them are matched by raw name — after a re-index the
resolver has already turned those edges into `unresolved` with a NULL target.

A proc removed from a file that still exists is the same problem in miniature:
`--unified=0` carries no context, so the old file cannot be rebuilt from the
hunk. `--base-rev` closes that gap by asking git for the whole old file.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import query
from .parser import parse_source

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_DIFF_GIT = re.compile(r"^diff --git a/(.+?) b/(.+)$")


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int

    @property
    def new_range(self) -> tuple[int, int]:
        if self.new_count <= 0:
            return self.new_start, self.new_start
        return self.new_start, self.new_start + self.new_count - 1


@dataclass
class FileDiff:
    path: str
    old_path: str | None = None
    hunks: list[Hunk] = field(default_factory=list)
    added: bool = False
    deleted: bool = False
    # (old line number, text) for a deleted file. The index has no rows for it,
    # so this is the only description of what the file used to declare.
    removed_lines: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class RemovedSymbol:
    path: str
    kind: str
    file: str
    line: int
    callers: list[query.CallEdge] = field(default_factory=list)


@dataclass
class Affected:
    symbol: query.SymbolHit | None
    depth: int
    roots: set[str] = field(default_factory=set)


@dataclass
class TestSuggestion:
    path: str
    reason: str


def _strip_prefix(path: str) -> str:
    path = path.strip().strip('"')
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def parse_unified_diff(text: str) -> list[FileDiff]:
    """Parse ``git diff`` output into per-file hunks, keeping only ``.dm``.

    For a deleted file the removed lines are kept: they are the old side of the
    diff, and the only way to know which symbols the file used to declare.
    """
    files: list[FileDiff] = []
    current: FileDiff | None = None
    in_hunk = False
    old_line = 0

    for line in text.splitlines():
        match = _DIFF_GIT.match(line)
        if match:
            current = FileDiff(path=match.group(2), old_path=match.group(1))
            files.append(current)
            in_hunk = False
            continue

        # `--- ` and `+++ ` are headers only before the first hunk; inside a
        # hunk they are ordinary removed/added source lines.
        if not in_hunk and line.startswith("--- "):
            if current is None:
                current = FileDiff(path="")
                files.append(current)
            old = line[4:].strip()
            if old == "/dev/null":
                current.added = True
            else:
                current.old_path = _strip_prefix(old)
            continue

        if not in_hunk and line.startswith("+++ "):
            if current is None:
                current = FileDiff(path="")
                files.append(current)
            new = line[4:].strip()
            if new == "/dev/null":
                current.deleted = True
                if current.old_path:
                    current.path = current.old_path
            else:
                current.path = _strip_prefix(new)
            continue

        match = _HUNK.match(line)
        if match and current is not None:
            in_hunk = True
            old_line = int(match.group(1))
            current.hunks.append(
                Hunk(
                    old_start=int(match.group(1)),
                    old_count=int(match.group(2)) if match.group(2) is not None else 1,
                    new_start=int(match.group(3)),
                    new_count=int(match.group(4)) if match.group(4) is not None else 1,
                )
            )
            continue

        if not in_hunk or current is None:
            continue
        if line.startswith("\\"):
            continue  # "\ No newline at end of file"
        if line.startswith("-"):
            if current.deleted:
                current.removed_lines.append((old_line, line[1:]))
            old_line += 1
        elif line.startswith("+"):
            continue
        else:
            old_line += 1  # context line

    return [
        item
        for item in files
        if item.path.endswith(".dm") and (item.hunks or item.deleted)
    ]


def _old_text(lines: list[tuple[int, str]]) -> str:
    """Rebuild the old file from removed lines, padding so line numbers match.

    `git diff --unified=0` emits no context lines, so the reconstruction is
    only complete for a whole-file deletion. For anything else use
    :func:`read_old_sources`, which reads the real old file from git.
    """
    if not lines:
        return ""
    first = lines[0][0]
    out: list[str] = [""] * max(0, first - 1)
    expected = first
    for line_number, text in lines:
        while expected < line_number:
            out.append("")
            expected += 1
        out.append(text)
        expected = line_number + 1
    return "\n".join(out)


def git_diff(root: Path, base: str | None = "HEAD", staged: bool = False) -> str:
    """Run ``git diff`` in *root* and return unified output."""
    command = [
        "git",
        "-C",
        str(root),
        "diff",
        "--unified=0",
        "--no-color",
        "--no-ext-diff",
        "--find-renames",
    ]
    if staged:
        command.append("--cached")
    if base:
        command.append(base)
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git diff failed")
    return result.stdout


def git_has_rev(root: Path, rev: str) -> bool:
    """Whether *rev* names a commit in *root*."""
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def git_show(root: Path, rev: str, path: str) -> str | None:
    """Return *path* as it was at *rev*, or ``None`` when git cannot produce it.

    A file that did not exist at *rev* (an addition) and a path that is not
    tracked both return ``None``; the caller falls back to the diff text.
    """
    result = subprocess.run(
        ["git", "-C", str(root), "show", f"{rev}:{path}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return None
    return result.stdout


def read_old_sources(root: Path, diffs: list[FileDiff], rev: str) -> dict[str, str]:
    """Old-side text for every diffed file that still exists at *rev*.

    Keyed by the *new* path, because that is what the rest of the report uses;
    the lookup itself uses ``old_path`` so a file renamed in the same diff is
    read under its old name. Added files are skipped: they have no old side.
    """
    sources: dict[str, str] = {}
    for file_diff in diffs:
        if file_diff.added:
            continue
        # Every hunk is a pure insertion, so nothing can have been removed.
        if file_diff.hunks and all(hunk.old_count == 0 for hunk in file_diff.hunks):
            continue
        path = file_diff.old_path or file_diff.path
        if not path:
            continue
        text = git_show(root, rev, path)
        if text is not None:
            sources[file_diff.path] = text
    return sources


def changed_symbols(
    conn: sqlite3.Connection,
    diffs: list[FileDiff],
    include_vars: bool = False,
) -> list[query.SymbolHit]:
    """Symbols whose line span intersects a changed hunk."""
    kinds = ("proc", "type", "var", "verb") if include_vars else ("proc", "type")
    placeholders = ",".join("?" for _ in kinds)
    hits: dict[str, query.SymbolHit] = {}

    for file_diff in diffs:
        if file_diff.deleted or not file_diff.hunks:
            continue
        rows = conn.execute(
            f"""
            SELECT s.id, s.kind, s.path, s.name, s.parent, s.signature,
                   s.start_line, s.end_line, s.doc, s.flags, f.path AS file
            FROM symbols s JOIN files f ON f.id = s.file_id
            WHERE f.path = ? AND s.kind IN ({placeholders})
            ORDER BY s.start_line
            """,
            [file_diff.path, *kinds],
        ).fetchall()
        for row in rows:
            start, end = int(row["start_line"]), int(row["end_line"])
            if any(
                not (end < hunk.new_range[0] or start > hunk.new_range[1])
                for hunk in file_diff.hunks
            ):
                hits.setdefault(row["path"], query._row_to_hit(row))
    return list(hits.values())


_REMOVED_KINDS = ("proc", "type", "verb")


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _owner_of(path: str) -> str:
    """``/obj/item/proc/foo`` → ``/obj/item``; ``/proc/foo`` → ``/``."""
    for marker in ("/proc/", "/var/", "/verb/"):
        if marker in path:
            return path.split(marker, 1)[0] or "/"
    return path


def _callers_by_name(
    conn: sqlite3.Connection,
    symbol: RemovedSymbol,
    family: set[str] | None = None,
    limit: int = 20,
) -> list[query.CallEdge]:
    """Call sites that could have reached a symbol that no longer exists.

    Once the file is gone and the index is rebuilt, the resolver has forgotten
    the target: those edges are `unresolved` with a NULL `callee_path`. The raw
    name is all that is left, so it is matched literally — and for a member
    proc restricted to callers on the owner type or one of its descendants,
    otherwise every `turn_on()` in the tree would be reported.
    """
    name = symbol.path.rsplit("/", 1)[-1]
    rows = conn.execute(
        """
        SELECT cs.path AS caller_path, cs.parent AS caller_parent,
               cf.path AS caller_file, cs.start_line AS caller_line,
               c.callee_raw, c.callee_path, c.resolution, c.line
        FROM calls c
        LEFT JOIN symbols cs ON cs.id = c.caller_symbol_id
        LEFT JOIN files cf ON cf.id = cs.file_id
        WHERE c.callee_raw = ? OR c.callee_raw LIKE ? ESCAPE '\\' OR c.callee_raw = ?
        ORDER BY c.id
        LIMIT 2000
        """,
        (name, f"%.{_escape_like(name)}", symbol.path),
    ).fetchall()

    # A type is instantiated from anywhere, so its family says nothing about
    # who could call it; only member procs get the restriction.
    owner = _owner_of(symbol.path) if symbol.kind != "type" else "/"
    if family is None:
        family = query.type_family(conn, owner) if owner != "/" else set()

    edges: list[query.CallEdge] = []
    for row in rows:
        if family and not (row["caller_parent"] in family or row["caller_path"] == owner):
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


def removed_symbols(
    conn: sqlite3.Connection,
    diffs: list[FileDiff],
    limit: int = 200,
    old_sources: dict[str, str] | None = None,
) -> list[RemovedSymbol]:
    """Symbols a changed file no longer declares, with their surviving call sites.

    The index describes the *new* side of the diff, so a deleted file's symbols
    are simply absent and `changed_symbols` has nothing to anchor its hunks to.
    The diff still carries the old text, so parse that and keep the paths the
    index no longer defines.

    *old_sources* maps a diffed file to its content at a base revision. That is
    what makes a proc removed from a *surviving* file visible: `--unified=0`
    hunks carry no context, so the old file cannot be rebuilt from them. When a
    file is missing from the mapping, a whole-file deletion still falls back to
    its removed lines.
    """
    removed: list[RemovedSymbol] = []
    seen: set[str] = set()
    sources = old_sources or {}

    for file_diff in diffs:
        text = sources.get(file_diff.path)
        if text is None:
            if not file_diff.deleted or not file_diff.removed_lines:
                continue
            text = _old_text(file_diff.removed_lines)
        if not text.strip():
            continue
        old_path = file_diff.old_path or file_diff.path
        parsed = parse_source(text.encode("utf-8"), old_path)
        for symbol in parsed.symbols:
            if symbol.kind not in _REMOVED_KINDS or symbol.path in seen:
                continue
            seen.add(symbol.path)
            # A type or proc can be declared in several files; only report the
            # ones the index really lost.
            if conn.execute(
                "SELECT 1 FROM symbols WHERE path = ? LIMIT 1", (symbol.path,)
            ).fetchone():
                continue
            removed.append(
                RemovedSymbol(
                    path=symbol.path,
                    kind=symbol.kind,
                    file=old_path,
                    line=symbol.start_line,
                )
            )
            if len(removed) >= limit:
                break
        if len(removed) >= limit:
            break

    families: dict[str, set[str]] = {}
    for symbol in removed:
        owner = _owner_of(symbol.path) if symbol.kind != "type" else "/"
        if owner != "/" and owner not in families:
            families[owner] = query.type_family(conn, owner)
        symbol.callers = _callers_by_name(
            conn, symbol, family=families.get(owner, set())
        )
    return removed


def removed_impact(
    conn: sqlite3.Connection,
    removed: list[RemovedSymbol],
    test_prefix: str | None = None,
) -> dict[str, Affected]:
    """Direct call sites of removed symbols, as depth-1 affected entries.

    Transitive expansion is deliberately skipped: the removed symbol is no
    longer in the graph, so every caller is a first-order breakage and the
    blast radius beyond it is the same one `expand` already reports for the
    changed symbols.
    """
    affected: dict[str, Affected] = {}
    for symbol in removed:
        for edge in symbol.callers:
            caller = edge.caller_path
            if not caller:
                continue
            if test_prefix and caller.startswith(test_prefix):
                continue
            entry = affected.get(caller)
            if entry is None:
                entry = Affected(symbol=None, depth=1, roots=set())
                affected[caller] = entry
            entry.roots.add(symbol.path)
    _fill_symbols(conn, affected)
    return affected


def _member_procs(conn: sqlite3.Connection, type_path: str, limit: int = 200) -> list[str]:
    rows = conn.execute(
        "SELECT path FROM symbols WHERE parent = ? AND kind = 'proc' ORDER BY start_line LIMIT ?",
        (type_path, limit),
    ).fetchall()
    return [row["path"] for row in rows]


def expand(
    conn: sqlite3.Connection,
    seeds: list[query.SymbolHit],
    max_depth: int = 3,
    limit: int = 400,
    test_prefix: str | None = None,
) -> dict[str, Affected]:
    """Transitive reverse dependencies of the changed symbols.

    A changed type pulls in its member procs first: a var default on
    ``/obj/item`` is read by every proc defined on that type, and those procs'
    callers are the ones that actually break.

    Symbols under *test_prefix* are skipped: unit tests are the verification
    step, reported separately, not part of the blast radius.
    """
    roots: dict[str, set[str]] = {}
    for seed in seeds:
        roots.setdefault(seed.path, set()).add(seed.path)
        if seed.kind == "type":
            for member in _member_procs(conn, seed.path):
                roots.setdefault(member, set()).add(seed.path)

    seen = set(roots)
    affected: dict[str, Affected] = {}
    frontier = set(roots)

    for depth in range(1, max_depth + 1):
        if not frontier or len(affected) >= limit:
            break
        next_frontier: set[str] = set()
        items = list(frontier)
        for start in range(0, len(items), 400):
            chunk = items[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"""
                SELECT DISTINCT cs.path AS caller, c.callee_path AS callee
                FROM calls c JOIN symbols cs ON cs.id = c.caller_symbol_id
                WHERE c.callee_path IN ({placeholders})
                """,
                chunk,
            ).fetchall()
            for row in rows:
                caller, callee = row["caller"], row["callee"]
                if not caller:
                    continue
                # Roots propagate outward, so the report can say which change
                # a caller is downstream of.
                roots.setdefault(caller, set()).update(roots.get(callee, set()))
                if caller in seen:
                    continue
                seen.add(caller)
                if test_prefix and caller.startswith(test_prefix):
                    continue
                next_frontier.add(caller)
                affected[caller] = Affected(symbol=None, depth=depth, roots=roots[caller])
                if len(affected) >= limit:
                    break
        frontier = next_frontier

    if affected:
        _fill_symbols(conn, affected)
    return affected


def _fill_symbols(conn: sqlite3.Connection, affected: dict[str, Affected]) -> None:
    paths = list(affected)
    for start in range(0, len(paths), 400):
        chunk = paths[start : start + 400]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            query._SYMBOL_SELECT + f" WHERE s.path IN ({placeholders})",
            chunk,
        ).fetchall()
        for row in rows:
            hit = query._row_to_hit(row)
            entry = affected.get(hit.path)
            if entry is not None:
                entry.symbol = hit

    for path, entry in list(affected.items()):
        if entry.symbol is None:
            del affected[path]


def suggest_tests(
    conn: sqlite3.Connection,
    affected_paths: set[str],
    changed_files: list[str],
    test_prefix: str = "/datum/unit_test",
    limit: int = 10,
    removed_paths: set[str] | None = None,
) -> list[TestSuggestion]:
    """Unit tests that call an affected proc, or mention a changed file.

    Suggestions are reported at the test datum level (``/datum/unit_test/foo``)
    rather than the proc that happens to make the call, because that is the
    unit the test runner knows about.

    *removed_paths* are symbols the diff deleted. Their call edges are already
    unresolved, so they are matched by raw name instead of by resolved path.
    """
    suggestions: dict[str, TestSuggestion] = {}

    def add(path: str, reason: str) -> None:
        test_path = path.split("/proc/", 1)[0] if "/proc/" in path else path
        suggestions.setdefault(test_path, TestSuggestion(path=test_path, reason=reason))

    paths = list(affected_paths)
    escaped_prefix = _escape_like(test_prefix)
    for start in range(0, len(paths), 400):
        chunk = paths[start : start + 400]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT DISTINCT cs.path AS path
            FROM calls c JOIN symbols cs ON cs.id = c.caller_symbol_id
            WHERE c.callee_path IN ({placeholders}) AND cs.path LIKE ? ESCAPE '\\'
            ORDER BY cs.path
            LIMIT ?
            """,
            [*chunk, f"{escaped_prefix}%", limit * 4],
        ).fetchall()
        for row in rows:
            add(row["path"], "calls an affected proc")

    for removed in sorted(removed_paths or ()):
        name = removed.rsplit("/", 1)[-1]
        rows = conn.execute(
            """
            SELECT DISTINCT cs.path AS path
            FROM calls c JOIN symbols cs ON cs.id = c.caller_symbol_id
            WHERE (c.callee_raw = ? OR c.callee_raw LIKE ? ESCAPE '\\' OR c.callee_raw = ?)
              AND cs.path LIKE ? ESCAPE '\\'
            ORDER BY cs.path
            LIMIT ?
            """,
            (name, f"%.{_escape_like(name)}", removed, f"{escaped_prefix}%", limit * 4),
        ).fetchall()
        for row in rows:
            add(row["path"], f"calls {removed}, which this diff removes")

    for file_path in changed_files:
        stem = Path(file_path).stem
        if len(stem) < 4:
            continue
        # FTS splits on underscores, so search the words rather than the stem.
        for hit, _snippet in query.search_text(conn, stem.replace("_", " "), limit=30):
            if not hit.path.startswith(test_prefix):
                continue
            add(hit.path, f"mentions {stem}")

    return list(suggestions.values())[:limit]
