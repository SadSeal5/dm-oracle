"""Command line interface for dm-oracle."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.tree import Tree

from . import ci as ci_mod
from . import context as context_mod
from . import embed as embed_mod
from . import eval as eval_mod
from . import multi
from . import query, store
from .config import RepoPaths
from .indexer import print_report, run_index

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Codebase oracle for BYOND/DreamMaker (SS13) repositories.",
)
console = Console()


def _paths(ctx: typer.Context) -> RepoPaths:
    return ctx.obj["paths"]


def _conn(ctx: typer.Context):
    paths = _paths(ctx)
    if not paths.db_path.exists():
        console.print(f"[red]no index at {paths.db_path}[/] — run [bold]oracle index[/] first")
        raise typer.Exit(2)
    try:
        conn = store.connect(paths.db_path)
    except sqlite3.OperationalError as exc:
        console.print(f"[red]cannot open {paths.db_path}: {exc}[/]")
        raise typer.Exit(2) from exc
    found = store.schema_version(conn)
    if found and found != store.SCHEMA_VERSION:
        console.print(
            f"[red]index uses schema v{found}, this build needs "
            f"v{store.SCHEMA_VERSION}[/] — rebuild it with "
            f"[bold]oracle -r {paths.root} index --force[/]"
        )
        conn.close()
        raise typer.Exit(2)
    return conn


def _other_conn(root: Path) -> tuple[RepoPaths, sqlite3.Connection]:
    """Open another repo's index, or explain how to build it."""
    paths = RepoPaths.discover(root)
    if not paths.db_path.exists():
        console.print(
            f"[red]no index at {paths.db_path}[/] — run "
            f"[bold]oracle -r {root} index[/] first"
        )
        raise typer.Exit(2)
    try:
        conn = store.connect(paths.db_path)
    except sqlite3.OperationalError as exc:
        console.print(f"[red]cannot open {paths.db_path}: {exc}[/]")
        raise typer.Exit(2) from exc
    found = store.schema_version(conn)
    if found and found != store.SCHEMA_VERSION:
        console.print(
            f"[yellow]{paths.db_path} uses schema v{found}, current is "
            f"v{store.SCHEMA_VERSION}[/] — re-index it for accurate results"
        )
    return paths, conn


def _record_json(record: multi.SymbolRecord) -> dict:
    return {
        "path": record.path,
        "kind": record.kind,
        "signature": record.signature,
        "file": record.file,
        "line": record.start_line,
    }


def _write_bundle(out: Path, rendered: str) -> None:
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered, encoding="utf-8")
    except OSError as exc:
        console.print(f"[red]cannot write {out}: {exc}[/]")
        raise typer.Exit(2)


def _semantic(fn, *args, **kwargs):
    """Run an embedding-backed call, turning a missing extra into a clean exit."""
    try:
        return fn(*args, **kwargs)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc


@app.callback()
def main(
    ctx: typer.Context,
    repo: Path = typer.Option(
        Path("."),
        "--repo",
        "-r",
        help="Repository root to operate on.",
        exists=True,
        file_okay=False,
        resolve_path=True,
    ),
    db: Optional[Path] = typer.Option(None, "--db", help="Override the index database path."),
) -> None:
    try:
        ctx.obj = {"paths": RepoPaths.discover(repo, str(db) if db else None)}
    except OSError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc


@app.command()
def index(
    ctx: typer.Context,
    force: bool = typer.Option(False, "--force", help="Rebuild the index from scratch."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Disable the progress bar."),
) -> None:
    """Parse every .dm file and (re)build the index."""
    paths = _paths(ctx)
    console.print(f"[bold]indexing[/] {paths.root}")
    try:
        report = run_index(paths, force=force, quiet=quiet)
    except store.SchemaMismatch as exc:
        console.print(
            f"[red]{exc}[/] — the index format changed; rebuild it with "
            f"[bold]oracle -r {paths.root} index --force[/]"
        )
        raise typer.Exit(2)
    except (OSError, sqlite3.OperationalError) as exc:
        console.print(f"[red]cannot use {paths.db_path}: {exc}[/]")
        raise typer.Exit(2) from exc
    print_report(report, paths.db_path)


@app.command()
def stats(ctx: typer.Context) -> None:
    """Show index statistics."""
    conn = _conn(ctx)
    data = query.stats(conn)
    table = Table(title="oracle index", show_header=False)
    table.add_column("metric", style="cyan")
    table.add_column("value", justify="right")
    table.add_row("files", f"{data['files']:,}")
    table.add_row("lines of DM", f"{data['loc']:,}")
    table.add_row("symbols", f"{data['symbols']:,}")
    table.add_row("call edges", f"{data['calls']:,}")
    table.add_row("defines", f"{data['defines']:,}")
    table.add_row("inheritance edges", f"{data['inherits']:,}")
    try:
        embedded = conn.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()["n"]
    except sqlite3.OperationalError:
        embedded = 0
    table.add_row("embeddings", f"{embedded:,}")
    console.print(table)

    kinds = Table(title="symbols by kind")
    kinds.add_column("kind")
    kinds.add_column("count", justify="right")
    for kind, count in data["by_kind"].items():
        kinds.add_row(kind, f"{count:,}")
    console.print(kinds)

    if data["resolutions"]:
        res = Table(title="call resolution")
        res.add_column("resolution")
        res.add_column("count", justify="right")
        for kind, count in data["resolutions"].items():
            res.add_row(kind, f"{count:,}")
        console.print(res)

    if data["top_types"]:
        top = Table(title="largest types (by proc count)")
        top.add_column("type")
        top.add_column("procs", justify="right")
        for path, count in data["top_types"]:
            top.add_row(path, str(count))
        console.print(top)


@app.command()
def embed(
    ctx: typer.Context,
    model: str = typer.Option(embed_mod.DEFAULT_MODEL, "--model", help="model2vec model name."),
    batch_size: int = typer.Option(256, "--batch-size", min=1, max=4096),
    max_chars: int = typer.Option(embed_mod.DEFAULT_MAX_CHARS, "--max-chars", min=0),
    limit: Optional[int] = typer.Option(None, "--limit", min=0, help="Embed only the first N symbols."),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
) -> None:
    """Build local embeddings for semantic search."""
    conn = _conn(ctx)
    store.init_schema(conn)
    count = _semantic(
        embed_mod.build_embeddings,
        conn,
        model_name=model,
        batch_size=batch_size,
        max_chars=max_chars,
        limit=limit,
        quiet=quiet,
    )
    if count:
        embed_mod.set_embedding_model(conn, model)
    else:
        console.print("[yellow]no symbols to embed[/]")


@app.command()
def mcp(
    ctx: typer.Context,
    transport: str = typer.Option("stdio", "--transport", help="stdio, sse, or streamable-http."),
    show_config: bool = typer.Option(False, "--print-config", help="Print an MCP client config snippet."),
) -> None:
    """Serve the oracle over the Model Context Protocol."""
    paths = _paths(ctx)
    if show_config:
        from .mcp_server import print_config as render_config

        console.print(render_config(paths))
        return
    # Fail fast with a readable message: a stdio server that starts and then
    # errors on every tool call is much harder to debug.
    _conn(ctx).close()
    from .mcp_server import create_server

    server = create_server(paths)
    server.run(transport=transport)


@app.command()
def diff(
    ctx: typer.Context,
    other: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        resolve_path=True,
        help="Other indexed repository to compare against.",
    ),
    kind: Optional[list[str]] = typer.Option(
        None, "--kind", "-k", help="Symbol kinds to compare (default: proc, type)."
    ),
    only: str = typer.Option(
        "missing",
        "--only",
        help="missing (in the other fork only) | extra | changed | renamed | all.",
    ),
    limit: int = typer.Option(40, "--limit", "-n", min=0),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable results."),
) -> None:
    """Compare this fork's index with another fork's index."""
    paths = _paths(ctx)
    conn = _conn(ctx)
    other_paths, other_conn = _other_conn(other)
    if only not in {"missing", "extra", "changed", "renamed", "all"}:
        console.print("[red]--only must be one of: missing, extra, changed, renamed, all[/]")
        raise typer.Exit(2)
    kinds = tuple(kind) if kind else multi.DEFAULT_KINDS
    report = multi.diff(conn, other_conn, paths.root.name, other_paths.root.name, kinds=kinds)

    if as_json:
        import json

        payload = {
            "left": report.left_name,
            "right": report.right_name,
            "kinds": list(kinds),
            "counts": report.counts,
            "only_left": [_record_json(record) for record in report.only_left[:limit]],
            "only_right": [_record_json(record) for record in report.only_right[:limit]],
            "changed": [
                {"left": _record_json(left), "right": _record_json(right)}
                for left, right in report.changed[:limit]
            ],
            "renamed": [
                {"left": _record_json(pair.left), "right": _record_json(pair.right)}
                for pair in report.renamed[:limit]
            ],
        }
        console.print_json(json.dumps(payload))
        return

    summary = Table(title=f"{report.left_name}  vs  {report.right_name}", header_style="bold")
    summary.add_column("kind")
    summary.add_column("here", justify="right")
    summary.add_column("other", justify="right")
    summary.add_column("shared", justify="right")
    summary.add_column("only here", justify="right")
    summary.add_column("only other", justify="right")
    summary.add_column("changed", justify="right")
    summary.add_column("renamed", justify="right")
    for kind_name, row in report.counts.items():
        summary.add_row(
            kind_name,
            f"{row['left']:,}",
            f"{row['right']:,}",
            f"{row['shared']:,}",
            f"{row['only_left']:,}",
            f"{row['only_right']:,}",
            f"{row['changed']:,}",
            f"{row['renamed']:,}",
        )
    console.print(summary)

    def render(records: list[multi.SymbolRecord], title: str, style: str) -> None:
        table = Table(title=title, header_style="bold")
        table.add_column("symbol", overflow="fold")
        table.add_column("kind", style="dim")
        table.add_column("where", style="dim", overflow="fold")
        for record in records[:limit]:
            table.add_row(
                f"[{style}]{record.path}[/]",
                record.kind,
                f"{record.file}:{record.start_line}",
            )
        console.print(table)
        if len(records) > limit:
            console.print(f"[dim]… {len(records) - limit:,} more (use --limit)[/]")

    if only in ("missing", "all"):
        render(
            report.only_right,
            f"only in {report.right_name} — port candidates ({len(report.only_right):,})",
            "cyan",
        )
    if only in ("extra", "all"):
        render(
            report.only_left,
            f"only in {report.left_name} ({len(report.only_left):,})",
            "magenta",
        )
    if only in ("changed", "all"):
        table = Table(title=f"changed in both ({len(report.changed):,})", header_style="bold")
        table.add_column("symbol", overflow="fold")
        table.add_column("what changed")
        table.add_column("where", style="dim", overflow="fold")
        for left, right in report.changed[:limit]:
            what = []
            if left.signature != right.signature:
                what.append("signature")
            if left.body_hash != right.body_hash:
                what.append("body")
            table.add_row(f"[yellow]{left.path}[/]", ", ".join(what), f"{left.file}:{left.start_line}")
        console.print(table)
        if len(report.changed) > limit:
            console.print(f"[dim]… {len(report.changed) - limit:,} more (use --limit)[/]")

    if only in ("renamed", "all"):
        table = Table(
            title=f"renamed / moved — same body, different path ({len(report.renamed):,})",
            header_style="bold",
        )
        table.add_column("here", overflow="fold")
        table.add_column("other", overflow="fold")
        table.add_column("kind", style="dim")
        for pair in report.renamed[:limit]:
            table.add_row(
                f"[magenta]{pair.left.path}[/]",
                f"[cyan]{pair.right.path}[/]",
                pair.left.kind,
            )
        console.print(table)
        if len(report.renamed) > limit:
            console.print(f"[dim]… {len(report.renamed) - limit:,} more (use --limit)[/]")


@app.command()
def port(
    ctx: typer.Context,
    symbol: str = typer.Argument(..., help="Symbol path or name to port."),
    source: Path = typer.Option(
        ...,
        "--from",
        exists=True,
        file_okay=False,
        resolve_path=True,
        help="Indexed repository to port from.",
    ),
    show_source: bool = typer.Option(True, "--source/--no-source", help="Print the source."),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable results."),
) -> None:
    """Inspect what porting a symbol from another fork involves."""
    paths = _paths(ctx)
    conn = _conn(ctx)
    source_paths, source_conn = _other_conn(source)
    report = multi.port(
        conn,
        paths.root,
        source_conn,
        source_paths.root,
        symbol,
        source_name=source_paths.root.name,
        target_name=paths.root.name,
    )

    if as_json:
        import json

        payload = {
            "symbol": report.symbol,
            "status": report.status,
            "source": _record_json(report.source) if report.source else None,
            "target": _record_json(report.target) if report.target else None,
            "signature_changed": report.signature_changed,
            "body_changed": report.body_changed,
            "owner": report.owner,
            "owner_missing": report.owner_missing,
            "source_members": report.source_members,
            "target_members": report.target_members,
            "target_callers": [
                {"caller": edge.caller_path, "file": edge.caller_file,
                 "line": edge.caller_line, "resolution": edge.resolution}
                for edge in report.target_callers
            ],
            "source_callees": [
                {"callee": edge.callee_path or edge.callee_raw, "resolution": edge.resolution}
                for edge in report.source_callees
            ],
            "source_code": report.source_code,
        }
        console.print_json(json.dumps(payload))
        return

    if report.source is None:
        console.print(f"[red]{symbol}[/] not found in [bold]{report.source_name}[/]")
        raise typer.Exit(1)

    status_style = {
        "identical": "green",
        "changed": "yellow",
        "missing": "cyan",
    }.get(report.status, "red")
    console.print(
        f"[bold]{report.source.path}[/] "
        f"[{status_style}]{report.status}[/] "
        f"[dim]({report.source_name} → {report.target_name})[/]"
    )
    console.print(f"  [dim]source[/] {report.source.file}:{report.source.start_line}")
    if report.target is not None:
        console.print(f"  [dim]target[/] {report.target.file}:{report.target.start_line}")
        if report.signature_changed:
            console.print(f"  [yellow]signature[/] {escape(report.source.signature or '')}")
            console.print(f"            {escape(report.target.signature or '')}")
    if report.source.kind == "type":
        console.print(
            f"  [dim]members[/] {report.source_members} upstream, "
            f"{report.target_members} here"
        )
    if report.owner_missing:
        console.print(
            f"  [yellow]owner type[/] {report.owner} does not exist in "
            f"{report.target_name} — port the type first"
        )

    if report.target_callers:
        table = Table(
            title=f"call sites on the same type family in {report.target_name}",
            header_style="bold",
        )
        table.add_column("caller", overflow="fold")
        table.add_column("where", style="dim", overflow="fold")
        table.add_column("resolution", style="dim")
        for edge in report.target_callers[:20]:
            table.add_row(
                edge.caller_path or "—",
                f"{edge.caller_file}:{edge.caller_line}",
                edge.resolution,
            )
        console.print(table)
    else:
        console.print("[dim]no call sites on the same type family in the target fork[/]")

    if show_source and report.source_code:
        console.print(
            Panel(
                Syntax(
                    report.source_code,
                    "text",
                    line_numbers=True,
                    start_line=report.source.start_line,
                ),
                title=f"{report.source_name}:{report.source.file}",
                border_style="dim",
            )
        )


@app.command()
def ci(
    ctx: typer.Context,
    base: str = typer.Option(
        "HEAD", "--base", help="Git ref to diff against (working tree vs HEAD by default)."
    ),
    base_rev: Optional[str] = typer.Option(
        None,
        "--base-rev",
        help="Read the old side of each changed file from this git ref. Catches "
        "procs removed from a file that still exists.",
    ),
    diff_file: Optional[Path] = typer.Option(
        None, "--diff", help="Read a unified diff from a file, or '-' for stdin."
    ),
    staged: bool = typer.Option(False, "--staged", help="Diff the index instead of the working tree."),
    depth: int = typer.Option(3, "--depth", min=0, max=10, help="Reverse-dependency depth."),
    limit: int = typer.Option(300, "--limit", min=0, help="Maximum affected symbols to report."),
    include_vars: bool = typer.Option(False, "--include-vars", help="Also report changed vars."),
    test_prefix: str = typer.Option(
        "/datum/unit_test", "--test-prefix", help="Type path prefix used by unit tests."
    ),
    fail_on_affected: bool = typer.Option(
        False, "--fail-on-affected", help="Exit with status 1 when anything is affected."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable results."),
) -> None:
    """List what a diff affects and which unit tests to run."""
    import sys

    paths = _paths(ctx)
    conn = _conn(ctx)

    if diff_file is not None:
        if str(diff_file) == "-":
            text = sys.stdin.read()
        else:
            try:
                text = diff_file.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                console.print(f"[red]cannot read {diff_file}: {exc}[/]")
                raise typer.Exit(2)
    else:
        try:
            text = ci_mod.git_diff(paths.root, base=base or None, staged=staged)
        except RuntimeError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(2)

    diffs = ci_mod.parse_unified_diff(text)
    if not diffs:
        console.print("[yellow]no .dm changes in this diff[/]")
        return

    sources = None
    if base_rev:
        if not ci_mod.git_has_rev(paths.root, base_rev):
            console.print(f"[red]unknown git revision: {base_rev}[/]")
            raise typer.Exit(2)
        sources = ci_mod.read_old_sources(paths.root, diffs, base_rev)

    changed = ci_mod.changed_symbols(conn, diffs, include_vars=include_vars)
    removed = ci_mod.removed_symbols(conn, diffs, old_sources=sources)
    affected = (
        ci_mod.expand(conn, changed, max_depth=depth, limit=limit, test_prefix=test_prefix)
        if depth
        else {}
    )
    # A call site of a deleted symbol is broken now, not downstream: it belongs
    # at depth 1 no matter how deep the walk goes. `--depth 0` still means "no
    # expansion", so the removed table stays the only report of them.
    if depth:
        for path, entry in ci_mod.removed_impact(
            conn, removed, test_prefix=test_prefix
        ).items():
            existing = affected.get(path)
            if existing is None:
                affected[path] = entry
            else:
                existing.roots |= entry.roots
                existing.depth = min(existing.depth, entry.depth)
    tests = ci_mod.suggest_tests(
        conn,
        set(affected),
        [item.path for item in diffs],
        test_prefix=test_prefix,
        removed_paths={symbol.path for symbol in removed},
    )

    ordered = sorted(affected.values(), key=lambda item: (item.depth, item.symbol.path))

    if as_json:
        import json

        payload = {
            "base_rev": base_rev,
            "files": [{"path": item.path, "added": item.added, "deleted": item.deleted} for item in diffs],
            "changed": [
                {"path": hit.path, "kind": hit.kind, "file": hit.file, "line": hit.start_line}
                for hit in changed
            ],
            "removed": [
                {
                    "path": symbol.path,
                    "kind": symbol.kind,
                    "file": symbol.file,
                    "line": symbol.line,
                    "callers": [
                        {"caller": edge.caller_path, "file": edge.caller_file,
                         "line": edge.caller_line, "raw": edge.callee_raw}
                        for edge in symbol.callers
                    ],
                }
                for symbol in removed
            ],
            "affected": [
                {
                    "path": item.symbol.path,
                    "kind": item.symbol.kind,
                    "file": item.symbol.file,
                    "line": item.symbol.start_line,
                    "depth": item.depth,
                    "roots": sorted(item.roots),
                }
                for item in ordered
            ],
            "tests": [{"path": item.path, "reason": item.reason} for item in tests],
        }
        console.print_json(json.dumps(payload))
        if fail_on_affected and affected:
            raise typer.Exit(1)
        return

    console.print(
        f"[bold]{len(diffs)}[/] changed .dm file(s), "
        f"[bold]{len(changed)}[/] changed symbol(s), "
        f"[bold]{len(removed)}[/] removed symbol(s), "
        f"[bold]{len(affected)}[/] affected downstream"
    )

    if removed:
        table = Table(title="removed symbols", header_style="bold")
        table.add_column("symbol", overflow="fold")
        table.add_column("kind", style="dim")
        table.add_column("was at", style="dim", overflow="fold")
        table.add_column("call sites", justify="right")
        for symbol in removed[:15]:
            table.add_row(
                f"[red]{escape(symbol.path)}[/]",
                symbol.kind,
                escape(f"{symbol.file}:{symbol.line}"),
                str(len(symbol.callers)),
            )
        console.print(table)
        if len(removed) > 15:
            console.print(f"[dim]… {len(removed) - 15:,} more removed symbols[/]")

    if changed:
        table = Table(title="changed symbols", header_style="bold")
        table.add_column("symbol", overflow="fold")
        table.add_column("kind", style="dim")
        table.add_column("where", style="dim", overflow="fold")
        ordered_changed = sorted(changed, key=lambda item: (item.file, item.start_line))
        for hit in ordered_changed[:15]:
            table.add_row(f"[cyan]{hit.path}[/]", hit.kind, f"{hit.file}:{hit.start_line}")
        console.print(table)
        if len(ordered_changed) > 15:
            console.print(f"[dim]… {len(ordered_changed) - 15:,} more changed symbols[/]")
    elif not removed:
        console.print("[dim]no indexed symbols intersect the changed lines[/]")

    if ordered:
        table = Table(title="affected downstream", header_style="bold")
        table.add_column("symbol", overflow="fold")
        table.add_column("depth", justify="right")
        table.add_column("because of", style="dim", overflow="fold")
        table.add_column("where", style="dim", overflow="fold")
        for item in ordered[:limit]:
            roots = ", ".join(sorted(item.roots)) or "—"
            table.add_row(
                f"[yellow]{item.symbol.path}[/]",
                str(item.depth),
                roots,
                f"{item.symbol.file}:{item.symbol.start_line}",
            )
        console.print(table)
        if len(ordered) > limit:
            console.print(f"[dim]… {len(ordered) - limit:,} more (use --limit)[/]")

    if tests:
        table = Table(title="suggested tests", header_style="bold")
        table.add_column("test", overflow="fold")
        table.add_column("why", style="dim")
        for suggestion in tests:
            table.add_row(f"[green]{suggestion.path}[/]", suggestion.reason)
        console.print(table)
    else:
        console.print("[dim]no unit tests reference the affected symbols[/]")

    if fail_on_affected and affected:
        raise typer.Exit(1)


@app.command("eval")
def eval_command(
    ctx: typer.Context,
    file: Path = typer.Option(
        Path("evals/ss13.json"),
        "--file",
        "-f",
        help="JSON file with retrieval questions.",
        exists=True,
        dir_okay=False,
    ),
    k: int = typer.Option(5, "--k", min=1, max=50),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable results."),
    semantic: bool = typer.Option(False, "--semantic", "-s", help="Use vector search."),
    compare: bool = typer.Option(False, "--compare", help="Compare lexical and semantic side by side."),
) -> None:
    """Measure retrieval quality against a gold set of questions."""
    conn = _conn(ctx)
    try:
        questions = eval_mod.load_questions(file)
    except (OSError, ValueError) as exc:
        console.print(f"[red]invalid eval file {file}: {exc}[/]")
        raise typer.Exit(2)

    if (semantic or compare) and not embed_mod.has_embeddings(conn):
        console.print(
            "[yellow]no embeddings in this index[/] — semantic results will be empty. "
            "Run [bold]oracle embed[/] first."
        )

    def payload(report: eval_mod.EvalReport) -> dict:
        return {
            "k": k,
            "total": report.total,
            "hits": report.hits,
            "recall_at_k": report.recall_at_k,
            "mrr": report.mrr,
            "top1": report.top1,
            "results": [
                {
                    "query": result.question.query,
                    "mode": result.question.mode,
                    "rank": result.rank,
                    "expect": result.question.expect,
                    "top": result.ranked,
                }
                for result in report.results
            ],
        }

    if compare:
        import json

        lexical = eval_mod.run_eval(conn, questions, k=k, semantic=False)
        vector = _semantic(eval_mod.run_eval, conn, questions, k=k, semantic=True)
        if as_json:
            console.print_json(json.dumps({"lexical": payload(lexical), "semantic": payload(vector)}))
            return

        table = Table(title=f"retrieval eval · lexical vs semantic · k={k}")
        table.add_column("query", overflow="fold")
        table.add_column("lexical", justify="right")
        table.add_column("semantic", justify="right")
        table.add_column("semantic top hit", overflow="fold", style="dim")
        for lex, sem in zip(lexical.results, vector.results):
            lex_rank = str(lex.rank) if lex.rank else "[red]miss[/]"
            sem_rank = str(sem.rank) if sem.rank else "[red]miss[/]"
            top = sem.ranked[0] if sem.ranked else "—"
            table.add_row(escape(lex.question.query), lex_rank, sem_rank, escape(top))
        console.print(table)
        console.print(
            f"[bold]lexical[/]  recall@{k} {lexical.recall_at_k:.0%} · "
            f"MRR {lexical.mrr:.3f} · top-1 {lexical.top1:.0%}"
        )
        console.print(
            f"[bold]semantic[/] recall@{k} {vector.recall_at_k:.0%} · "
            f"MRR {vector.mrr:.3f} · top-1 {vector.top1:.0%}"
        )
        return

    report = (
        _semantic(eval_mod.run_eval, conn, questions, k=k, semantic=True)
        if semantic
        else eval_mod.run_eval(conn, questions, k=k, semantic=False)
    )

    if as_json:
        import json

        console.print_json(json.dumps(payload(report)))
        return

    table = Table(title=f"retrieval eval · k={k}", show_header=True, header_style="bold")
    table.add_column("query", overflow="fold")
    table.add_column("mode", style="dim")
    table.add_column("rank", justify="right")
    table.add_column("top hit", overflow="fold", style="dim")
    for result in report.results:
        rank = str(result.rank) if result.rank else "[red]miss[/]"
        top = result.ranked[0] if result.ranked else "—"
        table.add_row(escape(result.question.query), result.question.mode, rank, escape(top))
    console.print(table)

    console.print(
        f"[bold]recall@{k}[/] {report.recall_at_k:.0%}   "
        f"[bold]MRR[/] {report.mrr:.3f}   "
        f"[bold]top-1[/] {report.top1:.0%}   "
        f"({report.hits}/{report.total})"
    )


@app.command()
def find(
    ctx: typer.Context,
    pattern: str = typer.Argument(..., help="Substring to look for in symbol paths."),
    kind: Optional[str] = typer.Option(None, "--kind", "-k", help="type, proc, var, verb"),
    limit: int = typer.Option(30, "--limit", "-n", min=0),
) -> None:
    """Find symbols by path substring."""
    conn = _conn(ctx)
    hits = query.find(conn, pattern, kind=kind, limit=limit)
    if not hits:
        console.print("[yellow]no matches[/]")
        raise typer.Exit(1)
    table = Table(show_header=True, header_style="bold")
    table.add_column("kind", width=6)
    table.add_column("path", overflow="fold")
    table.add_column("location", style="dim")
    for hit in hits:
        table.add_row(hit.kind, hit.path, hit.location)
    console.print(table)


@app.command()
def search(
    ctx: typer.Context,
    text: str = typer.Argument(..., help="Full-text query (FTS5 syntax supported)."),
    limit: int = typer.Option(20, "--limit", "-n", min=0),
    semantic: bool = typer.Option(False, "--semantic", "-s", help="Blend lexical and vector search."),
) -> None:
    """Full-text search across paths, signatures, docs and bodies."""
    conn = _conn(ctx)
    if semantic:
        hits = _semantic(embed_mod.hybrid_semantic_search, conn, text, limit=limit)
        if not hits:
            console.print("[yellow]no matches (did you run `oracle embed`?)[/]")
            raise typer.Exit(1)
        for hit in hits:
            console.print(f"[bold cyan]{hit.path}[/] [dim]{hit.location}[/]")
        return

    results = query.search_text(conn, text, limit=limit)
    if not results:
        console.print("[yellow]no matches[/]")
        raise typer.Exit(1)
    for hit, snippet in results:
        console.print(f"[bold cyan]{hit.path}[/] [dim]{hit.location}[/]")
        if snippet:
            console.print(f"  [dim]{escape(snippet)}[/]")


@app.command()
def show(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Exact symbol path."),
    no_source: bool = typer.Option(False, "--no-source", help="Only print metadata."),
) -> None:
    """Show a symbol and its source."""
    paths = _paths(ctx)
    conn = _conn(ctx)
    hits = query.get_symbol(conn, path)
    if not hits:
        console.print(f"[red]not found:[/] {path}")
        raise typer.Exit(1)
    for hit in hits:
        console.print(
            Panel(
                f"[bold]{hit.path}[/]\n[dim]{hit.kind} · {hit.location} · {hit.flags or 'no flags'}[/]"
                + (f"\n\n{escape(hit.doc)}" if hit.doc else ""),
                title="symbol",
            )
        )
        if not no_source:
            source = query.source_span(paths.root, hit)
            console.print(Syntax(source, "dm", line_numbers=True, start_line=hit.start_line))


@app.command()
def members(
    ctx: typer.Context,
    type_path: str = typer.Argument(..., help="Type path, e.g. /obj/item"),
) -> None:
    """List procs and vars declared directly on a type."""
    conn = _conn(ctx)
    hits = query.members(conn, type_path)
    if not hits:
        console.print("[yellow]no members (is the type path exact?)[/]")
        raise typer.Exit(1)
    table = Table(show_header=True, header_style="bold")
    table.add_column("kind", width=6)
    table.add_column("name")
    table.add_column("signature", overflow="fold")
    table.add_column("location", style="dim")
    for hit in hits:
        table.add_row(hit.kind, hit.name, escape(hit.signature or ""), hit.location)
    console.print(table)


@app.command()
def outline(
    ctx: typer.Context,
    file: str = typer.Argument(..., help="File path relative to the repo root."),
) -> None:
    """Show every symbol declared in a file, in source order."""
    conn = _conn(ctx)
    # The index stores POSIX-style relative paths; accept Windows separators.
    hits = query.outline(conn, file.replace("\\", "/"))
    if not hits:
        console.print("[yellow]no symbols (check the path)[/]")
        raise typer.Exit(1)
    for hit in hits:
        console.print(f"{hit.start_line:>6}  [cyan]{hit.kind:<5}[/] {hit.path}")


@app.command()
def callers(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Callee symbol path."),
    limit: int = typer.Option(100, "--limit", "-n", min=0),
) -> None:
    """Who calls this proc?"""
    conn = _conn(ctx)
    edges = query.callers(conn, path, limit=limit)
    if not edges:
        console.print("[yellow]no resolved callers[/]")
        raise typer.Exit(1)
    table = Table(show_header=True, header_style="bold")
    table.add_column("caller", overflow="fold")
    table.add_column("at", style="dim")
    table.add_column("resolution", style="dim")
    for edge in edges:
        table.add_row(edge.caller_path or "<top level>", f"{edge.caller_file}:{edge.line}", edge.resolution)
    console.print(table)


@app.command()
def callees(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Caller symbol path."),
    limit: int = typer.Option(100, "--limit", "-n", min=0),
) -> None:
    """What does this proc call?"""
    conn = _conn(ctx)
    hits = query.get_symbol(conn, path)
    if not hits:
        console.print(f"[red]not found:[/] {path}")
        raise typer.Exit(1)
    edges = query.callees(conn, hits[0].id, limit=limit)
    if not edges:
        console.print("[yellow]no calls recorded[/]")
        raise typer.Exit(1)
    table = Table(show_header=True, header_style="bold")
    table.add_column("callee", overflow="fold")
    table.add_column("line", justify="right")
    table.add_column("resolution", style="dim")
    for edge in edges:
        table.add_row(escape(edge.callee_path or edge.callee_raw), str(edge.line), edge.resolution)
    console.print(table)


@app.command()
def impact(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Symbol to analyse."),
    depth: int = typer.Option(3, "--depth", "-d", min=1, max=8),
    limit: int = typer.Option(300, "--limit", "-n", min=0),
) -> None:
    """Transitive reverse dependencies: what breaks if this changes?"""
    conn = _conn(ctx)
    root = query.impact(conn, path, max_depth=depth, limit=limit)
    if root is None:
        console.print(f"[red]not found:[/] {path}")
        raise typer.Exit(1)

    def render(node: query.ImpactNode, tree: Tree) -> None:
        for child in node.children:
            label = f"[cyan]{child.symbol.path}[/] [dim]{child.symbol.file}:{child.symbol.start_line}[/]"
            branch = tree.add(label)
            render(child, branch)

    tree = Tree(f"[bold]{root.symbol.path}[/] [dim]{root.symbol.file}:{root.symbol.start_line}[/]")
    render(root, tree)
    console.print(tree)


@app.command()
def context(
    ctx: typer.Context,
    text: str = typer.Argument(..., help="Natural-language or symbol query."),
    budget: int = typer.Option(24_000, "--budget", "-b", min=0, help="Approximate token budget."),
    seeds: int = typer.Option(6, "--seeds", min=0, help="Number of search hits to start from."),
    no_expand: bool = typer.Option(False, "--no-expand", help="Do not follow call edges."),
    semantic: bool = typer.Option(False, "--semantic", "-s", help="Use vector search for seeds."),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Write the bundle to a file."),
) -> None:
    """Pack a token-budgeted context bundle for an LLM."""
    paths = _paths(ctx)
    conn = _conn(ctx)
    bundle = _semantic(
        context_mod.build_query_context,
        conn,
        paths.root,
        text,
        budget=budget,
        seed_limit=seeds,
        expand=not no_expand,
        semantic=semantic,
    )
    rendered = bundle.render()
    if out:
        _write_bundle(out, rendered)
        console.print(
            f"[green]wrote[/] {out} · {len(bundle.blocks)} symbols · ~{bundle.tokens:,} tokens"
        )
    else:
        # The bundle is Markdown and DM source is full of `[/obj/item]` paths,
        # which Rich would try to parse as markup.
        console.print(rendered, markup=False)


@app.command("symbol-context")
def symbol_context(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Symbol path to build context around."),
    budget: int = typer.Option(24_000, "--budget", "-b", min=0),
    out: Optional[Path] = typer.Option(None, "--out", "-o"),
) -> None:
    """Pack the symbol, its fields, callees and callers into one bundle."""
    paths = _paths(ctx)
    conn = _conn(ctx)
    bundle = context_mod.build_symbol_context(conn, paths.root, path, budget=budget)
    rendered = bundle.render()
    if out:
        _write_bundle(out, rendered)
        console.print(f"[green]wrote[/] {out} · ~{bundle.tokens:,} tokens")
    else:
        console.print(rendered, markup=False)


if __name__ == "__main__":
    app()
