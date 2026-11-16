"""MCP server exposing the oracle to agents.

Run it over stdio:

    python -m oracle -r <repo> mcp

The tools return JSON for structured data and Markdown for context bundles, so
an agent can both reason over the graph and paste a ready-made context window.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Iterator

from mcp.server.mcpserver import MCPServer

from . import __version__
from . import context as context_mod
from . import embed, query, store
from .config import RepoPaths


def _hit(hit: query.SymbolHit) -> dict:
    return {
        "path": hit.path,
        "kind": hit.kind,
        "file": hit.file,
        "line": hit.start_line,
        "signature": hit.signature,
        "doc": hit.doc,
    }


def _edge(edge: query.CallEdge) -> dict:
    return {
        "caller": edge.caller_path,
        "callee": edge.callee_path or edge.callee_raw,
        "resolution": edge.resolution,
        "line": edge.line,
    }


def _impact(node: query.ImpactNode) -> dict:
    return {
        "path": node.symbol.path,
        "file": node.symbol.file,
        "line": node.symbol.start_line,
        "depth": node.depth,
        "callers": [_impact(child) for child in node.children],
    }


def create_server(paths: RepoPaths) -> MCPServer:
    root = paths.root

    @contextmanager
    def db() -> Iterator:
        if not paths.db_path.exists():
            raise RuntimeError(
                f"no index at {paths.db_path}; run `oracle -r {root} index` first"
            )
        conn = store.connect(paths.db_path)
        found = store.schema_version(conn)
        if found and found != store.SCHEMA_VERSION:
            conn.close()
            raise RuntimeError(
                f"index uses schema v{found}, this build needs v{store.SCHEMA_VERSION}; "
                f"rebuild it with `oracle -r {root} index --force`"
            )
        try:
            yield conn
        finally:
            conn.close()

    server = MCPServer(
        name="dm-oracle",
        version=__version__,
        instructions=(
            "DreamMaker (BYOND/SS13) codebase oracle. Use oracle_find or oracle_search "
            "to locate symbols, oracle_show for source, oracle_callers/callees for the "
            "call graph, oracle_impact for change impact, and oracle_context to pack a "
            "token-budgeted bundle. All paths are canonical DM paths such as "
            "/obj/item/proc/attack_self."
        ),
    )

    @server.tool(description="Index statistics: files, symbols, call edges, resolution breakdown.")
    def oracle_stats() -> str:
        with db() as conn:
            return json.dumps(query.stats(conn), indent=2)

    @server.tool(description="Find symbols by path substring. Exact name matches rank first.")
    def oracle_find(pattern: str, kind: str | None = None, limit: int = 20) -> str:
        if kind and kind not in {"proc", "type", "var", "verb"}:
            return json.dumps(
                {"error": f"unknown kind: {kind}", "valid": ["proc", "type", "var", "verb"]}
            )
        with db() as conn:
            hits = query.find(conn, pattern, kind=kind, limit=limit)
            return json.dumps([_hit(hit) for hit in hits], indent=2)

    @server.tool(description="Full-text search over paths, signatures, docs and bodies.")
    def oracle_search(query_text: str, limit: int = 10, semantic: bool = False) -> str:
        with db() as conn:
            if semantic and embed.has_embeddings(conn):
                hits = embed.hybrid_semantic_search(conn, query_text, limit=limit)
                return json.dumps([_hit(hit) for hit in hits], indent=2)
            results = query.search_text(conn, query_text, limit=limit)
            return json.dumps(
                [{"path": hit.path, "kind": hit.kind, "file": hit.file,
                  "line": hit.start_line, "snippet": snippet}
                 for hit, snippet in results],
                indent=2,
            )

    @server.tool(description="Show a symbol's metadata and source span.")
    def oracle_show(symbol_path: str, include_source: bool = True) -> str:
        with db() as conn:
            hits = query.get_symbol(conn, symbol_path)
            if not hits:
                return json.dumps({"error": f"not found: {symbol_path}"})
            payload = []
            for hit in hits:
                item = _hit(hit)
                if include_source:
                    item["source"] = query.source_span(root, hit)
                payload.append(item)
            return json.dumps(payload, indent=2)

    @server.tool(description="List procs and vars declared directly on a type path.")
    def oracle_members(type_path: str, limit: int = 300) -> str:
        with db() as conn:
            hits = query.members(conn, type_path, limit=limit)
            return json.dumps([_hit(hit) for hit in hits], indent=2)

    @server.tool(description="Who calls this proc? Returns resolved call edges.")
    def oracle_callers(symbol_path: str, limit: int = 50) -> str:
        with db() as conn:
            edges = query.callers(conn, symbol_path, limit=limit)
            return json.dumps([_edge(edge) for edge in edges], indent=2)

    @server.tool(description="What does this proc call? Returns resolved call edges.")
    def oracle_callees(symbol_path: str, limit: int = 100) -> str:
        with db() as conn:
            hits = query.get_symbol(conn, symbol_path)
            if not hits:
                return json.dumps({"error": f"not found: {symbol_path}"})
            edges = query.callees(conn, hits[0].id, limit=limit)
            return json.dumps([_edge(edge) for edge in edges], indent=2)

    @server.tool(description="Transitive reverse dependencies: what breaks if this symbol changes?")
    def oracle_impact(symbol_path: str, depth: int = 3, limit: int = 200) -> str:
        with db() as conn:
            node = query.impact(conn, symbol_path, max_depth=depth, limit=limit)
            if node is None:
                return json.dumps({"error": f"not found: {symbol_path}"})
            return json.dumps(_impact(node), indent=2)

    @server.tool(
        description=(
            "Pack a token-budgeted Markdown context bundle: seeds from search, "
            "expanded along call edges, with file:line anchors and source."
        )
    )
    def oracle_context(
        query_text: str,
        budget: int = 12_000,
        seeds: int = 6,
        semantic: bool = False,
    ) -> str:
        with db() as conn:
            bundle = context_mod.build_query_context(
                conn,
                root,
                query_text,
                budget=budget,
                seed_limit=seeds,
                semantic=semantic and embed.has_embeddings(conn),
            )
            return bundle.render()

    return server


def print_config(paths: RepoPaths) -> str:
    """Return an MCP client config snippet for this repository."""
    return json.dumps(
        {
            "mcpServers": {
                "dm-oracle": {
                    "command": "python",
                    "args": ["-m", "oracle", "-r", str(paths.root), "mcp"],
                }
            }
        },
        indent=2,
    )
