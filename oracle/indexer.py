"""Index orchestration: parse files, write rows, resolve the call graph."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from . import store
from .config import RepoPaths, iter_dm_files
from .parser import ParsedFile, parse_file
from .resolve import resolve_all

console = Console()

MAX_FTS_BODY = 40_000


@dataclass
class IndexReport:
    files_seen: int = 0
    files_indexed: int = 0
    files_skipped: int = 0
    files_removed: int = 0
    files_failed: int = 0
    symbols: int = 0
    calls: int = 0
    resolutions: dict[str, int] = field(default_factory=dict)
    elapsed: float = 0.0
    errors: list[tuple[str, str]] = field(default_factory=list)


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _existing_files(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {row["path"]: row for row in conn.execute("SELECT id, path, sha1, mtime FROM files")}


def _write_parsed(conn: sqlite3.Connection, parsed: ParsedFile, file_id: int) -> tuple[int, int]:
    symbol_rows: list[store.SymbolRow] = []
    for sym in parsed.symbols:
        symbol_rows.append(
            store.SymbolRow(
                file_id=file_id,
                kind=sym.kind,
                path=sym.path,
                name=sym.name,
                parent=sym.parent,
                signature=sym.signature,
                start_line=sym.start_line,
                end_line=sym.end_line,
                start_byte=sym.start_byte,
                end_byte=sym.end_byte,
                flags=sym.flags,
                doc=sym.doc,
                body_hash=sym.body_hash,
            )
        )

    ids = store.insert_symbols(conn, symbol_rows)

    for symbol_id, sym in zip(ids, parsed.symbols):
        body = sym.body[:MAX_FTS_BODY]
        store.insert_fts(conn, symbol_id, sym.path, sym.name, sym.signature, sym.doc, body)

    path_to_id: dict[str, int] = {}
    for symbol_id, sym in zip(ids, parsed.symbols):
        if sym.kind in ("proc", "verb"):
            path_to_id[sym.path] = symbol_id

    call_rows: list[store.CallRow] = []
    for call in parsed.calls:
        call_rows.append(
            store.CallRow(
                caller_symbol_id=path_to_id.get(call.caller_path or ""),
                file_id=file_id,
                callee_raw=call.callee_raw,
                callee_path=None,
                resolution="unresolved",
                kind=call.kind,
                line=call.line,
                col=call.col,
            )
        )
    store.insert_calls(conn, call_rows)
    store.insert_inherits(conn, parsed.inherits)
    store.insert_defines(conn, [(file_id, name, value, line) for name, value, line in parsed.defines])
    store.insert_params(conn, parsed.params)
    store.insert_locals(conn, parsed.locals)
    return len(ids), len(call_rows)


def _dedupe(conn: sqlite3.Connection) -> None:
    """Collapse repeated type declarations and inheritance edges.

    Every ``/obj/item/foo`` declaration re-declares its intermediate path
    segments (``/obj``, ``/obj/item``), so the raw index contains tens of
    thousands of duplicate type rows. Keep the largest declaration per path.
    """
    conn.execute("DROP TABLE IF EXISTS temp.keep_types")
    conn.execute(
        "CREATE TEMP TABLE keep_types AS "
        "SELECT id FROM ("
        "  SELECT id, ROW_NUMBER() OVER ("
        "    PARTITION BY path ORDER BY (end_byte - start_byte) DESC, id"
        "  ) AS rn FROM symbols WHERE kind = 'type'"
        ") WHERE rn = 1"
    )
    conn.execute(
        "DELETE FROM symbols_fts WHERE rowid IN ("
        "  SELECT id FROM symbols WHERE kind = 'type' AND id NOT IN (SELECT id FROM keep_types)"
        ")"
    )
    conn.execute(
        "DELETE FROM symbols WHERE kind = 'type' AND id NOT IN (SELECT id FROM keep_types)"
    )
    conn.execute("DROP TABLE temp.keep_types")
    conn.execute(
        "DELETE FROM inherits WHERE id NOT IN ("
        "  SELECT MIN(id) FROM inherits GROUP BY child, parent"
        ")"
    )


def run_index(paths: RepoPaths, force: bool = False, quiet: bool = False) -> IndexReport:
    started = time.perf_counter()
    report = IndexReport()
    conn = store.connect(paths.db_path)
    store.init_schema(conn)

    if force:
        store.reset(conn)
        store.init_schema(conn)

    existing = _existing_files(conn)
    seen_paths: set[str] = set()
    files = list(iter_dm_files(paths.root))
    report.files_seen = len(files)

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]indexing[/]"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        disable=quiet,
    )

    with progress, store.transaction(conn):
        task = progress.add_task("files", total=len(files))
        for path in files:
            rel = path.relative_to(paths.root).as_posix()
            seen_paths.add(rel)
            try:
                stat = path.stat()
                data = path.read_bytes()
                digest = _sha1(data)
            except OSError as exc:
                report.files_failed += 1
                report.errors.append((rel, f"read error: {exc}"))
                progress.advance(task)
                continue

            prev = existing.get(rel)
            if prev is not None and not force and prev["sha1"] == digest:
                report.files_skipped += 1
                progress.advance(task)
                continue

            if prev is not None:
                store.delete_file(conn, int(prev["id"]))

            parsed = parse_file(path, paths.root)
            if parsed.error:
                report.files_failed += 1
                report.errors.append((rel, parsed.error))

            file_id = store.insert_file(conn, rel, parsed.loc, digest, stat.st_mtime)
            symbols, calls = _write_parsed(conn, parsed, file_id)
            report.symbols += symbols
            report.calls += calls
            report.files_indexed += 1
            progress.advance(task)

        # Drop files that disappeared from disk.
        for rel, row in existing.items():
            if rel not in seen_paths:
                store.delete_file(conn, int(row["id"]))
                report.files_removed += 1

    _dedupe(conn)
    report.resolutions = resolve_all(conn)
    store.set_meta(conn, "indexed_at", str(int(time.time())))
    store.set_meta(conn, "root", str(paths.root))
    conn.commit()
    conn.close()

    report.elapsed = time.perf_counter() - started
    return report


def print_report(report: IndexReport, db_path: Path) -> None:
    console.print()
    console.print(f"[bold green]index complete[/] in {report.elapsed:.1f}s -> [dim]{db_path}[/]")
    console.print(
        f"  files: {report.files_indexed} indexed, {report.files_skipped} unchanged, "
        f"{report.files_removed} removed, {report.files_failed} failed"
    )
    console.print(f"  symbols: {report.symbols:,}   calls: {report.calls:,}")
    if report.resolutions:
        parts = ", ".join(f"{k}={v:,}" for k, v in sorted(report.resolutions.items(), key=lambda kv: -kv[1]))
        console.print(f"  resolved: {parts}")
    if report.errors:
        console.print(f"  [yellow]{len(report.errors)} file(s) had parse errors[/]")
        for rel, err in report.errors[:5]:
            console.print(f"    [dim]{rel}: {err}[/]")
