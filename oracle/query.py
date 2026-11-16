"""Read-side queries over the oracle index."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import store


@dataclass
class SymbolHit:
    id: int
    kind: str
    path: str
    name: str
    parent: str | None
    signature: str | None
    file: str
    start_line: int
    end_line: int
    doc: str | None = None
    flags: str = ""

    @property
    def location(self) -> str:
        return f"{self.file}:{self.start_line}"


@dataclass
class CallEdge:
    caller_path: str | None
    caller_file: str | None
    caller_line: int | None
    callee_raw: str
    callee_path: str | None
    resolution: str
    line: int


@dataclass
class ImpactNode:
    symbol: SymbolHit
    depth: int
    via: str | None = None
    children: list["ImpactNode"] = field(default_factory=list)


def _row_to_hit(row: sqlite3.Row) -> SymbolHit:
    return SymbolHit(
        id=int(row["id"]),
        kind=row["kind"],
        path=row["path"],
        name=row["name"],
        parent=row["parent"],
        signature=row["signature"],
        file=row["file"],
        start_line=int(row["start_line"]),
        end_line=int(row["end_line"]),
        doc=row["doc"],
        flags=row["flags"],
    )


_SYMBOL_SELECT = """
SELECT s.id, s.kind, s.path, s.name, s.parent, s.signature, s.start_line, s.end_line,
       s.doc, s.flags, f.path AS file
FROM symbols s JOIN files f ON f.id = s.file_id
"""


def find(conn: sqlite3.Connection, pattern: str, kind: str | None = None, limit: int = 50) -> list[SymbolHit]:
    """Substring search over symbol paths, case-insensitive.

    Exact symbol-name matches rank first, then the shortest paths.
    """
    like = f"%{pattern}%"
    sql = _SYMBOL_SELECT + " WHERE s.path LIKE ? COLLATE NOCASE"
    params: list[object] = [like]
    if kind:
        sql += " AND s.kind = ?"
        params.append(kind)
    sql += " ORDER BY CASE WHEN s.name = ? COLLATE NOCASE THEN 0 ELSE 1 END, LENGTH(s.path), s.path LIMIT ?"
    params.extend([pattern, limit])
    return [_row_to_hit(row) for row in conn.execute(sql, params)]


_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does",
    "for", "from", "how", "i", "in", "is", "it", "of", "on", "or", "that",
    "the", "this", "to", "we", "what", "when", "where", "which", "why",
    "with", "work", "works", "working", "you",
}


def _content_tokens(text: str) -> list[str]:
    raw = re.findall(r"\w+", text)
    filtered = [token for token in raw if token.lower() not in _STOPWORDS]
    return filtered or raw


def _fts_query(text: str) -> str:
    """Turn a natural-language query into a forgiving FTS5 expression.

    Explicit FTS syntax is passed through untouched; plain text becomes an OR
    of quoted content tokens so multi-word queries still return something
    useful.
    """
    if re.search(r'\b(AND|OR|NOT|NEAR)\b|["*:()]', text):
        return text
    tokens = _content_tokens(text)
    if not tokens:
        return text
    return " OR ".join(f'"{token}"' for token in tokens)


def search_text(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[tuple[SymbolHit, str]]:
    """FTS5 search over path/name/signature/doc/body. Returns hit + snippet."""
    expression = _fts_query(query)
    fetch = max(limit * 4, 40)
    sql = """
        SELECT s.id, s.kind, s.path, s.name, s.parent, s.signature, s.start_line, s.end_line,
               s.doc, s.flags, f.path AS file,
               snippet(symbols_fts, 4, '<<', '>>', ' … ', 12) AS snip
        FROM symbols_fts
        JOIN symbols s ON s.id = symbols_fts.rowid
        JOIN files f ON f.id = s.file_id
        WHERE symbols_fts MATCH ?
        ORDER BY bm25(symbols_fts, 4.0, 10.0, 3.0, 5.0, 1.0)
        LIMIT ?
    """
    try:
        rows = conn.execute(sql, (expression, fetch)).fetchall()
    except sqlite3.OperationalError:
        safe = '"' + query.replace('"', '""') + '"'
        rows = conn.execute(sql, (safe, fetch)).fetchall()

    tokens = {token.lower() for token in re.findall(r"\w+", query)}
    results = [(_row_to_hit(row), row["snip"]) for row in rows]

    # Exact name matches are almost always what the user meant.
    results.sort(
        key=lambda item: (
            0 if item[0].name.lower() in tokens else 1,
            0 if item[0].kind in ("proc", "type") else 1,
        )
    )
    return results[:limit]


def hybrid_search(conn: sqlite3.Connection, text: str, limit: int = 20) -> list[SymbolHit]:
    """Combine path search, underscore-joined search and full-text search.

    Natural-language queries ("bullet act") and symbol names ("bullet_act")
    both land on the same symbol without the caller having to guess.
    """
    stripped = text.strip()
    tokens = _content_tokens(stripped)
    joined = "_".join(tokens)

    candidates: list[SymbolHit] = []
    seen: set[str] = set()
    fts_positions: dict[str, int] = {}
    pool = max(limit * 6, 60)

    def push(hits: list[SymbolHit]) -> None:
        for hit in hits:
            if hit.path not in seen:
                seen.add(hit.path)
                candidates.append(hit)

    if "/" in stripped or " " not in stripped:
        push(find(conn, stripped, limit=pool))
    if len(tokens) > 1:
        push(find(conn, joined, limit=pool))
    for token in tokens:
        if len(token) >= 3:
            push(find(conn, token, limit=pool))
    for position, (hit, _snippet) in enumerate(search_text(conn, stripped, limit=pool)):
        fts_positions.setdefault(hit.path, position)
        push([hit])

    query_lower = stripped.lower()
    joined_lower = joined.lower()
    token_set = {token.lower() for token in tokens}
    multi = len(tokens) > 1

    def path_tokens(path: str) -> set[str]:
        return {part for part in re.split(r"[/_]", path.lower()) if part}

    def score(hit: SymbolHit) -> float:
        name = hit.name.lower()
        path = hit.path.lower()
        value = 0.0
        exact = False
        if name == query_lower:
            value -= 300
            exact = True
        elif not multi and name == joined_lower:
            value -= 300
            exact = True
        segments = path_tokens(path)
        matched = sum(1 for token in token_set if token in segments)
        value -= matched * matched * 25
        if not exact and name in token_set:
            value -= 30
        if hit.kind == "proc":
            value -= 80
        elif hit.kind == "type":
            value -= 0
        else:
            value += 20
        if not exact and matched == 0:
            fts_position = fts_positions.get(hit.path)
            if fts_position is not None:
                value -= max(0, 40 - fts_position * 2)
        value += len(hit.path) * 0.05
        return value

    candidates.sort(key=score)
    return candidates[:limit]


def get_symbol(conn: sqlite3.Connection, path: str) -> list[SymbolHit]:
    rows = conn.execute(
        _SYMBOL_SELECT + " WHERE s.path = ? ORDER BY s.start_line",
        (path,),
    ).fetchall()
    if rows:
        return [_row_to_hit(row) for row in rows]
    # Allow a proc name without the /proc/ segment.
    rows = conn.execute(
        _SYMBOL_SELECT + " WHERE s.path LIKE ? ORDER BY LENGTH(s.path) LIMIT 10",
        (f"%/{path}",),
    ).fetchall()
    return [_row_to_hit(row) for row in rows]


def symbol_by_id(conn: sqlite3.Connection, symbol_id: int) -> SymbolHit | None:
    row = conn.execute(_SYMBOL_SELECT + " WHERE s.id = ?", (symbol_id,)).fetchone()
    return _row_to_hit(row) if row else None


def source_span(root: Path, hit: SymbolHit) -> str:
    path = root / hit.file
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    start = max(0, hit.start_line - 1)
    end = min(len(lines), hit.end_line)
    return "\n".join(lines[start:end])


def members(conn: sqlite3.Connection, type_path: str, limit: int = 500) -> list[SymbolHit]:
    rows = conn.execute(
        _SYMBOL_SELECT + " WHERE s.parent = ? ORDER BY s.kind, s.start_line LIMIT ?",
        (type_path, limit),
    ).fetchall()
    return [_row_to_hit(row) for row in rows]


def outline(conn: sqlite3.Connection, file: str) -> list[SymbolHit]:
    rows = conn.execute(
        _SYMBOL_SELECT + " WHERE f.path = ? ORDER BY s.start_line",
        (file,),
    ).fetchall()
    return [_row_to_hit(row) for row in rows]


def callers(conn: sqlite3.Connection, callee_path: str, limit: int = 200) -> list[CallEdge]:
    rows = conn.execute(
        """
        SELECT cs.path AS caller_path, cf.path AS caller_file, cs.start_line AS caller_line,
               c.callee_raw, c.callee_path, c.resolution, c.line
        FROM calls c
        LEFT JOIN symbols cs ON cs.id = c.caller_symbol_id
        LEFT JOIN files cf ON cf.id = cs.file_id
        WHERE c.callee_path = ?
        ORDER BY cf.path, c.line
        LIMIT ?
        """,
        (callee_path, limit),
    ).fetchall()
    return [
        CallEdge(
            caller_path=row["caller_path"],
            caller_file=row["caller_file"],
            caller_line=row["caller_line"],
            callee_raw=row["callee_raw"],
            callee_path=row["callee_path"],
            resolution=row["resolution"],
            line=int(row["line"]),
        )
        for row in rows
    ]


def callees(conn: sqlite3.Connection, symbol_id: int, limit: int = 500) -> list[CallEdge]:
    rows = conn.execute(
        """
        SELECT cs.path AS caller_path, cf.path AS caller_file, cs.start_line AS caller_line,
               c.callee_raw, c.callee_path, c.resolution, c.line
        FROM calls c
        LEFT JOIN symbols cs ON cs.id = c.caller_symbol_id
        LEFT JOIN files cf ON cf.id = cs.file_id
        WHERE c.caller_symbol_id = ?
        ORDER BY c.line
        LIMIT ?
        """,
        (symbol_id, limit),
    ).fetchall()
    return [
        CallEdge(
            caller_path=row["caller_path"],
            caller_file=row["caller_file"],
            caller_line=row["caller_line"],
            callee_raw=row["callee_raw"],
            callee_path=row["callee_path"],
            resolution=row["resolution"],
            line=int(row["line"]),
        )
        for row in rows
    ]


def impact(conn: sqlite3.Connection, callee_path: str, max_depth: int = 3, limit: int = 400) -> ImpactNode | None:
    """Transitive reverse dependency tree (who breaks if this changes?)."""
    root_hits = get_symbol(conn, callee_path)
    if not root_hits:
        return None
    root = root_hits[0]
    seen: set[str] = {root.path}
    node = ImpactNode(symbol=root, depth=0)

    frontier: list[ImpactNode] = [node]
    total = 0
    for depth in range(1, max_depth + 1):
        next_frontier: list[ImpactNode] = []
        for parent in frontier:
            for edge in callers(conn, parent.symbol.path, limit=limit):
                if not edge.caller_path or edge.caller_path in seen:
                    continue
                seen.add(edge.caller_path)
                hit = get_symbol(conn, edge.caller_path)
                if not hit:
                    continue
                child = ImpactNode(symbol=hit[0], depth=depth, via=edge.caller_path)
                parent.children.append(child)
                next_frontier.append(child)
                total += 1
                if total >= limit:
                    break
            if total >= limit:
                break
        frontier = next_frontier
        if not frontier:
            break
    return node


def stats(conn: sqlite3.Connection) -> dict[str, object]:
    base = store.stats(conn)
    by_kind = {
        row["kind"]: int(row["n"])
        for row in conn.execute("SELECT kind, COUNT(*) AS n FROM symbols GROUP BY kind ORDER BY n DESC")
    }
    resolutions = {
        row["resolution"]: int(row["n"])
        for row in conn.execute("SELECT resolution, COUNT(*) AS n FROM calls GROUP BY resolution ORDER BY n DESC")
    }
    top_types = [
        (row["path"], int(row["n"]))
        for row in conn.execute(
            "SELECT parent AS path, COUNT(*) AS n FROM symbols WHERE kind = 'proc' AND parent IS NOT NULL "
            "GROUP BY parent ORDER BY n DESC LIMIT 15"
        )
    ]
    return {**base, "by_kind": by_kind, "resolutions": resolutions, "top_types": top_types}
