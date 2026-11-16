from __future__ import annotations

from pathlib import Path

from oracle import context, store
from oracle.config import RepoPaths
from oracle.indexer import run_index

SOURCE = (
    "/obj/item/thing\n"
    "\tvar/name = \"thing\"\n"
    "\n"
    "/obj/item/thing/proc/explode(mob/user)\n"
    "\tvar/mob/living/target = user\n"
    "\ttarget.attack_self()\n"
    "\treturn\n"
    "\n"
    "/obj/item/thing/proc/explode_later()\n"
    "\treturn\n"
)


def _indexed(tmp_path: Path) -> tuple[RepoPaths, Path]:
    (tmp_path / "code").mkdir()
    (tmp_path / "code" / "thing.dm").write_text(SOURCE, encoding="utf-8")
    paths = RepoPaths.discover(tmp_path)
    run_index(paths, quiet=True)
    return paths, tmp_path


def _query_bundle(tmp_path: Path, budget: int) -> context.ContextBundle:
    paths, root = _indexed(tmp_path)
    conn = store.connect(paths.db_path)
    try:
        return context.build_query_context(conn, root, "explode", budget=budget)
    finally:
        conn.close()


def test_zero_budget_emits_no_blocks(tmp_path: Path) -> None:
    bundle = _query_bundle(tmp_path, 0)

    assert bundle.blocks == []
    assert bundle.tokens == 0
    assert bundle.dropped > 0
    # Rendering must still produce a valid header, not a stray code block.
    rendered = bundle.render()
    assert "0 symbols" in rendered
    assert "```" not in rendered


def test_tiny_budget_keeps_only_the_first_block(tmp_path: Path) -> None:
    bundle = _query_bundle(tmp_path, 1)

    assert len(bundle.blocks) == 1
    assert bundle.dropped > 0


def test_symbol_context_for_a_missing_path_is_empty(tmp_path: Path) -> None:
    paths, root = _indexed(tmp_path)
    conn = store.connect(paths.db_path)
    try:
        bundle = context.build_symbol_context(conn, root, "/nope/nope")
    finally:
        conn.close()

    assert bundle.blocks == []


def test_zero_seeds_emits_no_blocks(tmp_path: Path) -> None:
    paths, root = _indexed(tmp_path)
    conn = store.connect(paths.db_path)
    try:
        bundle = context.build_query_context(conn, root, "explode", seed_limit=0)
    finally:
        conn.close()

    assert bundle.blocks == []


def test_symbol_context_for_a_type_includes_its_own_fields(tmp_path: Path) -> None:
    paths, root = _indexed(tmp_path)
    conn = store.connect(paths.db_path)
    try:
        bundle = context.build_symbol_context(conn, root, "/obj/item/thing")
    finally:
        conn.close()

    assert "/obj/item/thing/var/name" in [block.symbol.path for block in bundle.blocks]
