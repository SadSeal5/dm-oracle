"""Command line interface for dm-oracle."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.tree import Tree

from . import context as context_mod
from . import embed as embed_mod
from . import eval as eval_mod
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
    return store.connect(paths.db_path)


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
    ctx.obj = {"paths": RepoPaths.discover(repo, str(db) if db else None)}


@app.command()
def index(
    ctx: typer.Context,
    force: bool = typer.Option(False, "--force", help="Rebuild the index from scratch."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Disable the progress bar."),
) -> None:
    """Parse every .dm file and (re)build the index."""
    paths = _paths(ctx)
    console.print(f"[bold]indexing[/] {paths.root}")
    report = run_index(paths, force=force, quiet=quiet)
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
    limit: Optional[int] = typer.Option(None, "--limit", help="Embed only the first N symbols."),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
) -> None:
    """Build local embeddings for semantic search."""
    conn = _conn(ctx)
    store.init_schema(conn)
    count = embed_mod.build_embeddings(
        conn,
        model_name=model,
        batch_size=batch_size,
        max_chars=max_chars,
        limit=limit,
        quiet=quiet,
    )
    embed_mod.set_embedding_model(conn, model)
    if not count:
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
    from .mcp_server import create_server

    server = create_server(paths)
    server.run(transport=transport)


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
    questions = eval_mod.load_questions(file)

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
        vector = eval_mod.run_eval(conn, questions, k=k, semantic=True)
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
            table.add_row(lex.question.query, lex_rank, sem_rank, top)
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

    report = eval_mod.run_eval(conn, questions, k=k, semantic=semantic)

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
        table.add_row(result.question.query, result.question.mode, rank, top)
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
    limit: int = typer.Option(30, "--limit", "-n"),
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
    limit: int = typer.Option(20, "--limit", "-n"),
    semantic: bool = typer.Option(False, "--semantic", "-s", help="Blend lexical and vector search."),
) -> None:
    """Full-text search across paths, signatures, docs and bodies."""
    conn = _conn(ctx)
    if semantic:
        hits = embed_mod.hybrid_semantic_search(conn, text, limit=limit)
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
            console.print(f"  [dim]{snippet}[/]")


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
                + (f"\n\n{hit.doc}" if hit.doc else ""),
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
        table.add_row(hit.kind, hit.name, hit.signature or "", hit.location)
    console.print(table)


@app.command()
def outline(
    ctx: typer.Context,
    file: str = typer.Argument(..., help="File path relative to the repo root."),
) -> None:
    """Show every symbol declared in a file, in source order."""
    conn = _conn(ctx)
    hits = query.outline(conn, file)
    if not hits:
        console.print("[yellow]no symbols (check the path)[/]")
        raise typer.Exit(1)
    for hit in hits:
        console.print(f"{hit.start_line:>6}  [cyan]{hit.kind:<5}[/] {hit.path}")


@app.command()
def callers(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Callee symbol path."),
    limit: int = typer.Option(100, "--limit", "-n"),
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
    limit: int = typer.Option(100, "--limit", "-n"),
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
        table.add_row(edge.callee_path or edge.callee_raw, str(edge.line), edge.resolution)
    console.print(table)


@app.command()
def impact(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Symbol to analyse."),
    depth: int = typer.Option(3, "--depth", "-d", min=1, max=8),
    limit: int = typer.Option(300, "--limit", "-n"),
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
    budget: int = typer.Option(24_000, "--budget", "-b", help="Approximate token budget."),
    seeds: int = typer.Option(6, "--seeds", help="Number of search hits to start from."),
    no_expand: bool = typer.Option(False, "--no-expand", help="Do not follow call edges."),
    semantic: bool = typer.Option(False, "--semantic", "-s", help="Use vector search for seeds."),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Write the bundle to a file."),
) -> None:
    """Pack a token-budgeted context bundle for an LLM."""
    paths = _paths(ctx)
    conn = _conn(ctx)
    bundle = context_mod.build_query_context(
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
        out.write_text(rendered, encoding="utf-8")
        console.print(
            f"[green]wrote[/] {out} · {len(bundle.blocks)} symbols · ~{bundle.tokens:,} tokens"
        )
    else:
        console.print(rendered)


@app.command("symbol-context")
def symbol_context(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Symbol path to build context around."),
    budget: int = typer.Option(24_000, "--budget", "-b"),
    out: Optional[Path] = typer.Option(None, "--out", "-o"),
) -> None:
    """Pack the symbol, its fields, callees and callers into one bundle."""
    paths = _paths(ctx)
    conn = _conn(ctx)
    bundle = context_mod.build_symbol_context(conn, paths.root, path, budget=budget)
    rendered = bundle.render()
    if out:
        out.write_text(rendered, encoding="utf-8")
        console.print(f"[green]wrote[/] {out} · ~{bundle.tokens:,} tokens")
    else:
        console.print(rendered)


if __name__ == "__main__":
    app()
