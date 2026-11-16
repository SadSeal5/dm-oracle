"""Context packing: turn a query into a token-budgeted bundle for an LLM."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import sqlite3

from . import embed, query


def estimate_tokens(text: str) -> int:
    """Cheap token estimate. Code is denser than prose; ~3.6 chars/token."""
    return max(1, int(len(text) / 3.6))


@dataclass
class ContextBlock:
    title: str
    symbol: query.SymbolHit
    source: str
    reason: str

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.source)


@dataclass
class ContextBundle:
    query: str
    blocks: list[ContextBlock] = field(default_factory=list)
    dropped: int = 0
    budget: int = 0

    @property
    def tokens(self) -> int:
        return sum(block.tokens for block in self.blocks)

    def render(self) -> str:
        lines: list[str] = []
        lines.append(f"# Context bundle: {self.query}")
        lines.append("")
        lines.append(
            f"_{len(self.blocks)} symbols, ~{self.tokens:,} tokens "
            f"(budget {self.budget:,}), {self.dropped} dropped_"
        )
        lines.append("")
        lines.append("## Index")
        for i, block in enumerate(self.blocks, 1):
            lines.append(f"{i}. `{block.symbol.path}` — {block.symbol.location} ({block.reason})")
        lines.append("")

        for i, block in enumerate(self.blocks, 1):
            lines.append(f"## {i}. {block.symbol.path}")
            lines.append("")
            lines.append(f"`{block.symbol.file}:{block.symbol.start_line}` · {block.reason}")
            if block.symbol.signature:
                lines.append("")
                lines.append(f"```dm\n{block.symbol.signature}\n```")
            lines.append("")
            lines.append("```dm")
            lines.append(block.source.rstrip())
            lines.append("```")
            lines.append("")
        return "\n".join(lines)


def _add(
    bundle: ContextBundle,
    root: Path,
    hit: query.SymbolHit,
    reason: str,
    seen: set[str],
    max_tokens: int,
) -> bool:
    key = f"{hit.path}@{hit.file}:{hit.start_line}"
    if key in seen:
        return False
    if max_tokens <= 0:
        bundle.dropped += 1
        return False
    source = query.source_span(root, hit)
    if not source:
        return False
    cost = estimate_tokens(source)
    if bundle.tokens + cost > max_tokens and bundle.blocks:
        bundle.dropped += 1
        return False
    seen.add(key)
    bundle.blocks.append(ContextBlock(title=hit.path, symbol=hit, source=source, reason=reason))
    return True


def build_query_context(
    conn: sqlite3.Connection,
    root: Path,
    text: str,
    budget: int = 24_000,
    seed_limit: int = 6,
    expand: bool = True,
    semantic: bool = False,
) -> ContextBundle:
    bundle = ContextBundle(query=text, budget=budget)
    seen: set[str] = set()
    seed_limit = max(0, int(seed_limit))

    stripped = text.strip()
    if semantic:
        ranked = embed.hybrid_semantic_search(conn, stripped, limit=max(seed_limit * 4, 12))
    elif "/" in stripped or " " not in stripped:
        # Looks like a symbol name or path: path search ranks the canonical
        # definition first, which is usually what the caller wants.
        ranked = query.find(conn, stripped, limit=max(seed_limit * 4, 12))
    else:
        ranked = query.hybrid_search(conn, stripped, limit=max(seed_limit * 4, 12))

    seeds = [(hit, "") for hit in ranked[:seed_limit]]
    # Vars make poor anchors: make sure a few procs from the ranked list are
    # represented even when the top hits are fields. `--seeds 0` means no seeds.
    extra_procs = (
        [hit for hit in ranked[seed_limit:] if hit.kind in ("proc", "verb")][:3]
        if seed_limit
        else []
    )
    seeds.extend((hit, "") for hit in extra_procs)

    if not seeds and seed_limit:
        seeds = query.search_text(conn, stripped, limit=seed_limit)
    if not seeds and seed_limit:
        seeds = [(hit, "") for hit in query.find(conn, stripped, limit=seed_limit)]

    for hit, _snippet in seeds:
        _add(bundle, root, hit, "search hit", seen, budget)

    if expand:
        for block in list(bundle.blocks):
            hit = block.symbol
            if hit.kind not in ("proc", "verb"):
                continue
            for edge in query.callees(conn, hit.id, limit=20):
                if not edge.callee_path:
                    continue
                targets = query.get_symbol(conn, edge.callee_path)
                if targets:
                    _add(bundle, root, targets[0], f"callee of {hit.path}", seen, budget)
            for edge in query.callers(conn, hit.path, limit=10):
                if not edge.caller_path:
                    continue
                targets = query.get_symbol(conn, edge.caller_path)
                if targets:
                    _add(bundle, root, targets[0], f"caller of {hit.path}", seen, budget)

    return bundle


def build_symbol_context(
    conn: sqlite3.Connection,
    root: Path,
    symbol_path: str,
    budget: int = 24_000,
    depth: int = 1,
) -> ContextBundle:
    bundle = ContextBundle(query=symbol_path, budget=budget)
    seen: set[str] = set()
    hits = query.get_symbol(conn, symbol_path)
    if not hits:
        return bundle

    root_hit = hits[0]
    _add(bundle, root, root_hit, "target", seen, budget)

    # For a type, its fields are its own members; for a proc, they live on the
    # owner type.
    owner = root_hit.path if root_hit.kind == "type" else root_hit.parent
    if owner:
        for member in query.members(conn, owner, limit=40):
            if member.kind == "var":
                _add(bundle, root, member, f"field of {owner}", seen, budget)

    if depth >= 1:
        for edge in query.callees(conn, root_hit.id, limit=30):
            if edge.callee_path:
                targets = query.get_symbol(conn, edge.callee_path)
                if targets:
                    _add(bundle, root, targets[0], f"callee of {root_hit.path}", seen, budget)
        for edge in query.callers(conn, root_hit.path, limit=15):
            if edge.caller_path:
                targets = query.get_symbol(conn, edge.caller_path)
                if targets:
                    _add(bundle, root, targets[0], f"caller of {root_hit.path}", seen, budget)

    return bundle
