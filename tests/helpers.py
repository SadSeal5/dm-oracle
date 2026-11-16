from __future__ import annotations

import sqlite3

from oracle import store


def add_symbol(
    conn: sqlite3.Connection,
    file_id: int,
    kind: str,
    path: str,
    name: str | None = None,
    parent: str | None = None,
    signature: str | None = None,
    start_line: int = 1,
    end_line: int = 1,
    body_hash: str | None = None,
) -> int:
    rows = [
        store.SymbolRow(
            file_id=file_id,
            kind=kind,
            path=path,
            name=name or path.rsplit("/", 1)[-1],
            parent=parent,
            signature=signature,
            start_line=start_line,
            end_line=end_line,
            start_byte=0,
            end_byte=0,
            body_hash=body_hash,
        )
    ]
    return store.insert_symbols(conn, rows)[0]


def add_call(
    conn: sqlite3.Connection,
    file_id: int,
    caller_id: int | None,
    raw: str,
    line: int = 1,
) -> None:
    store.insert_calls(
        conn,
        [
            store.CallRow(
                caller_symbol_id=caller_id,
                file_id=file_id,
                callee_raw=raw,
                callee_path=None,
                resolution="unresolved",
                kind="call",
                line=line,
                col=1,
            )
        ],
    )
