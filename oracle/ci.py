"""Change impact for CI.

Given a unified diff, find the symbols whose source lines changed, expand them
along the reverse call graph, and suggest unit tests that exercise them.

The index is assumed to describe the *new* side of the diff (that is what
`oracle index` produces when it runs before this command). Deleted lines are
therefore anchored at the hunk's new-side position, which maps to the symbol
that still encloses that point.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import query

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
    """Parse ``git diff`` output into per-file hunks, keeping only ``.dm``."""
    files: list[FileDiff] = []
    current: FileDiff | None = None

    for line in text.splitlines():
        match = _DIFF_GIT.match(line)
        if match:
            current = FileDiff(path=match.group(2), old_path=match.group(1))
            files.append(current)
            continue

        if line.startswith("--- "):
            if current is None:
                current = FileDiff(path="")
                files.append(current)
            old = line[4:].strip()
            if old == "/dev/null":
                current.added = True
            else:
                current.old_path = _strip_prefix(old)
            continue

        if line.startswith("+++ "):
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
            current.hunks.append(
                Hunk(
                    old_start=int(match.group(1)),
                    old_count=int(match.group(2)) if match.group(2) is not None else 1,
                    new_start=int(match.group(3)),
                    new_count=int(match.group(4)) if match.group(4) is not None else 1,
                )
            )

    return [
        item
        for item in files
        if item.path.endswith(".dm") and (item.hunks or item.deleted)
    ]


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
) -> list[TestSuggestion]:
    """Unit tests that call an affected proc, or mention a changed file.

    Suggestions are reported at the test datum level (``/datum/unit_test/foo``)
    rather than the proc that happens to make the call, because that is the
    unit the test runner knows about.
    """
    suggestions: dict[str, TestSuggestion] = {}

    def add(path: str, reason: str) -> None:
        test_path = path.split("/proc/", 1)[0] if "/proc/" in path else path
        suggestions.setdefault(test_path, TestSuggestion(path=test_path, reason=reason))

    paths = list(affected_paths)
    escaped_prefix = (
        test_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
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
