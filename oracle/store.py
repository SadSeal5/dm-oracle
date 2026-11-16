"""SQLite storage for the oracle index.

The schema is deliberately boring: one row per file, symbol, call edge and
define. Full-text search uses FTS5 so the index stays useful without any
external service.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

SCHEMA_VERSION = 4

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id       INTEGER PRIMARY KEY,
    path     TEXT NOT NULL UNIQUE,
    loc      INTEGER NOT NULL DEFAULT 0,
    sha1     TEXT NOT NULL,
    mtime    REAL NOT NULL DEFAULT 0,
    indexed_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS symbols (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    path        TEXT NOT NULL,
    name        TEXT NOT NULL,
    parent      TEXT,
    signature   TEXT,
    start_line  INTEGER NOT NULL,
    end_line    INTEGER NOT NULL,
    start_byte  INTEGER NOT NULL,
    end_byte    INTEGER NOT NULL,
    flags       TEXT NOT NULL DEFAULT '',
    doc         TEXT,
    body_hash   TEXT
);

CREATE INDEX IF NOT EXISTS idx_symbols_path   ON symbols(path);
CREATE INDEX IF NOT EXISTS idx_symbols_parent ON symbols(parent);
CREATE INDEX IF NOT EXISTS idx_symbols_name   ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_kind   ON symbols(kind);
CREATE INDEX IF NOT EXISTS idx_symbols_file   ON symbols(file_id);

CREATE TABLE IF NOT EXISTS calls (
    id               INTEGER PRIMARY KEY,
    caller_symbol_id INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
    file_id          INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    callee_raw       TEXT NOT NULL,
    callee_path      TEXT,
    resolution       TEXT NOT NULL DEFAULT 'unresolved',
    kind             TEXT NOT NULL DEFAULT 'call',
    line             INTEGER NOT NULL,
    col              INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_calls_caller   ON calls(caller_symbol_id);
CREATE INDEX IF NOT EXISTS idx_calls_callee   ON calls(callee_path);
CREATE INDEX IF NOT EXISTS idx_calls_raw      ON calls(callee_raw);
CREATE INDEX IF NOT EXISTS idx_calls_file     ON calls(file_id);

CREATE TABLE IF NOT EXISTS inherits (
    id        INTEGER PRIMARY KEY,
    child     TEXT NOT NULL,
    parent    TEXT NOT NULL,
    explicit  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_inherits_child  ON inherits(child);
CREATE INDEX IF NOT EXISTS idx_inherits_parent ON inherits(parent);

CREATE TABLE IF NOT EXISTS defines (
    id      INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    name    TEXT NOT NULL,
    value   TEXT,
    line    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_defines_name ON defines(name);

CREATE TABLE IF NOT EXISTS params (
    id        INTEGER PRIMARY KEY,
    proc_path TEXT NOT NULL,
    name      TEXT NOT NULL,
    type      TEXT,
    position  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_params_proc ON params(proc_path);

CREATE TABLE IF NOT EXISTS locals (
    id        INTEGER PRIMARY KEY,
    proc_path TEXT NOT NULL,
    name      TEXT NOT NULL,
    type      TEXT,
    line      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_locals_proc ON locals(proc_path);

CREATE TABLE IF NOT EXISTS embeddings (
    symbol_id INTEGER PRIMARY KEY REFERENCES symbols(id) ON DELETE CASCADE,
    model     TEXT NOT NULL,
    dim       INTEGER NOT NULL,
    vector    BLOB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_embeddings_model ON embeddings(model);

CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
    path,
    name,
    signature,
    doc,
    body,
    tokenize = 'unicode61'
);
"""


@dataclass
class SymbolRow:
    file_id: int
    kind: str
    path: str
    name: str
    parent: str | None
    signature: str | None
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    flags: str = ""
    doc: str | None = None
    body_hash: str | None = None


@dataclass
class CallRow:
    caller_symbol_id: int | None
    file_id: int
    callee_raw: str
    callee_path: str | None
    resolution: str
    kind: str
    line: int
    col: int


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def reset(conn: sqlite3.Connection) -> None:
    """Drop every table so a rebuild starts from a clean slate."""
    tables = [
        "symbols_fts",
        "embeddings",
        "calls",
        "inherits",
        "defines",
        "params",
        "locals",
        "symbols",
        "files",
        "meta",
    ]
    for table in tables:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()
    init_schema(conn)


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def insert_file(conn: sqlite3.Connection, path: str, loc: int, sha1: str, mtime: float) -> int:
    cur = conn.execute(
        "INSERT INTO files(path, loc, sha1, mtime, indexed_at) VALUES(?, ?, ?, ?, strftime('%s','now')) "
        "ON CONFLICT(path) DO UPDATE SET loc=excluded.loc, sha1=excluded.sha1, "
        "mtime=excluded.mtime, indexed_at=excluded.indexed_at",
        (path, loc, sha1, mtime),
    )
    if cur.lastrowid:
        return int(cur.lastrowid)
    row = conn.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()
    return int(row["id"])


def insert_symbols(conn: sqlite3.Connection, rows: Sequence[SymbolRow]) -> list[int]:
    ids: list[int] = []
    for row in rows:
        cur = conn.execute(
            "INSERT INTO symbols(file_id, kind, path, name, parent, signature, start_line, "
            "end_line, start_byte, end_byte, flags, doc, body_hash) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.file_id,
                row.kind,
                row.path,
                row.name,
                row.parent,
                row.signature,
                row.start_line,
                row.end_line,
                row.start_byte,
                row.end_byte,
                row.flags,
                row.doc,
                row.body_hash,
            ),
        )
        ids.append(int(cur.lastrowid))
    return ids


def insert_fts(conn: sqlite3.Connection, symbol_id: int, path: str, name: str,
               signature: str | None, doc: str | None, body: str) -> None:
    conn.execute(
        "INSERT INTO symbols_fts(rowid, path, name, signature, doc, body) VALUES(?, ?, ?, ?, ?, ?)",
        (symbol_id, path, name, signature or "", doc or "", body),
    )


def insert_calls(conn: sqlite3.Connection, rows: Iterable[CallRow]) -> None:
    conn.executemany(
        "INSERT INTO calls(caller_symbol_id, file_id, callee_raw, callee_path, resolution, kind, line, col) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                r.caller_symbol_id,
                r.file_id,
                r.callee_raw,
                r.callee_path,
                r.resolution,
                r.kind,
                r.line,
                r.col,
            )
            for r in rows
        ],
    )


def insert_inherits(conn: sqlite3.Connection, rows: Iterable[tuple[str, str, int]]) -> None:
    conn.executemany(
        "INSERT INTO inherits(child, parent, explicit) VALUES(?, ?, ?)",
        list(rows),
    )


def insert_defines(conn: sqlite3.Connection, rows: Iterable[tuple[int, str, str | None, int]]) -> None:
    conn.executemany(
        "INSERT INTO defines(file_id, name, value, line) VALUES(?, ?, ?, ?)",
        list(rows),
    )


def insert_params(conn: sqlite3.Connection, rows: Iterable[tuple[str, str, str | None, int]]) -> None:
    conn.executemany(
        "INSERT INTO params(proc_path, name, type, position) VALUES(?, ?, ?, ?)",
        list(rows),
    )


def insert_locals(conn: sqlite3.Connection, rows: Iterable[tuple[str, str, str | None, int]]) -> None:
    conn.executemany(
        "INSERT INTO locals(proc_path, name, type, line) VALUES(?, ?, ?, ?)",
        list(rows),
    )


def delete_file(conn: sqlite3.Connection, file_id: int) -> None:
    """Remove a file and everything derived from it."""
    conn.execute(
        "DELETE FROM symbols_fts WHERE rowid IN (SELECT id FROM symbols WHERE file_id = ?)",
        (file_id,),
    )
    conn.execute("DELETE FROM files WHERE id = ?", (file_id,))


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    for table in ("files", "symbols", "calls", "defines", "inherits", "params", "locals"):
        out[table] = int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
    out["loc"] = int(conn.execute("SELECT COALESCE(SUM(loc), 0) AS n FROM files").fetchone()["n"])
    return out
