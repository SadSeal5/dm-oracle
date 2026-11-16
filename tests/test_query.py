from __future__ import annotations

import sqlite3
from pathlib import Path

from helpers import add_call, add_symbol
from oracle import query, store


def _build(conn: sqlite3.Connection) -> dict[str, int]:
    file_id = store.insert_file(conn, "code/test.dm", 100, "abc", 0.0)
    ids: dict[str, int] = {}
    ids["/obj/item"] = add_symbol(conn, file_id, "type", "/obj/item", start_line=1, end_line=10)
    ids["/obj/item/proc/attack_self"] = add_symbol(
        conn, file_id, "proc", "/obj/item/proc/attack_self", parent="/obj/item", start_line=5, end_line=8
    )
    ids["/obj/item/proc/use"] = add_symbol(
        conn, file_id, "proc", "/obj/item/proc/use", parent="/obj/item", start_line=12, end_line=18
    )
    ids["/obj/item/proc/use_more"] = add_symbol(
        conn, file_id, "proc", "/obj/item/proc/use_more", parent="/obj/item", start_line=20, end_line=22
    )
    store.insert_fts(
        conn,
        ids["/obj/item/proc/attack_self"],
        "/obj/item/proc/attack_self",
        "attack_self",
        "attack_self(mob/user)",
        "Activate the item in hand.",
        "attack_self(user) return",
    )
    add_call(conn, file_id, ids["/obj/item/proc/use"], "attack_self", line=14)
    add_call(conn, file_id, ids["/obj/item/proc/use_more"], "use", line=21)
    conn.execute(
        "UPDATE calls SET callee_path = '/obj/item/proc/attack_self', resolution = 'inherited' "
        "WHERE callee_raw = 'attack_self'"
    )
    conn.execute(
        "UPDATE calls SET callee_path = '/obj/item/proc/use', resolution = 'inherited' "
        "WHERE callee_raw = 'use'"
    )
    conn.commit()
    return ids


def test_find_orders_by_shortest_path(conn: sqlite3.Connection) -> None:
    _build(conn)
    hits = query.find(conn, "use")
    assert hits[0].path == "/obj/item/proc/use"


def test_search_text_finds_body_mentions(conn: sqlite3.Connection) -> None:
    _build(conn)
    results = query.search_text(conn, "activate item")
    assert any(hit.path == "/obj/item/proc/attack_self" for hit, _snippet in results)


def test_callers_and_callees(conn: sqlite3.Connection) -> None:
    ids = _build(conn)
    callers = query.callers(conn, "/obj/item/proc/attack_self")
    assert [edge.caller_path for edge in callers] == ["/obj/item/proc/use"]

    callees = query.callees(conn, ids["/obj/item/proc/use"])
    assert [edge.callee_path for edge in callees] == ["/obj/item/proc/attack_self"]


def test_impact_walks_transitive_callers(conn: sqlite3.Connection) -> None:
    _build(conn)
    root = query.impact(conn, "/obj/item/proc/attack_self", max_depth=3)
    assert root is not None
    assert root.symbol.path == "/obj/item/proc/attack_self"
    assert any(child.symbol.path == "/obj/item/proc/use" for child in root.children)
    use = next(child for child in root.children if child.symbol.path == "/obj/item/proc/use")
    assert any(grandchild.symbol.path == "/obj/item/proc/use_more" for grandchild in use.children)


def test_find_rejects_an_empty_pattern(conn: sqlite3.Connection) -> None:
    _build(conn)
    # `LIKE '%%'` would return every symbol in the index.
    assert query.find(conn, "") == []
    assert query.find(conn, "   ") == []


def test_find_treats_like_wildcards_literally(conn: sqlite3.Connection) -> None:
    _build(conn)
    file_id = conn.execute("SELECT id FROM files LIMIT 1").fetchone()[0]
    add_symbol(conn, file_id, "proc", "/obj/item/proc/useZmore")
    conn.commit()

    # `_` would match any character, so "use_" must not match "useZmore".
    assert [hit.path for hit in query.find(conn, "use_")] == [
        "/obj/item/proc/use_more"
    ]
    # `%` must not match everything.
    assert query.find(conn, "%") == []


def test_get_symbol_treats_wildcards_literally(conn: sqlite3.Connection) -> None:
    _build(conn)
    assert query.get_symbol(conn, "%") == []
    assert query.get_symbol(conn, "item/proc/use")[0].path == "/obj/item/proc/use"


def test_negative_limits_do_not_dump_the_index(conn: sqlite3.Connection) -> None:
    ids = _build(conn)
    # SQLite treats LIMIT -1 as "unlimited", which would return everything.
    assert query.find(conn, "use", limit=-1) == []
    assert query.search_text(conn, "activate", limit=-1) == []
    assert query.members(conn, "/obj/item", limit=-1) == []
    assert query.callers(conn, "/obj/item/proc/attack_self", limit=-1) == []
    assert query.callees(conn, ids["/obj/item/proc/use"], limit=-1) == []
    assert query.hybrid_search(conn, "attack", limit=-1) == []


def test_fts_query_treats_lowercase_boolean_words_as_text() -> None:
    # FTS5 operators are uppercase-only; "and" here is prose, not a boolean.
    assert query._fts_query("attack and defend") == '"attack" OR "defend"'
    assert query._fts_query("attack AND defend") == "attack AND defend"


def test_source_span_reuses_the_line_cache(tmp_path: Path) -> None:
    hit = query.SymbolHit(
        id=1, kind="proc", path="/obj/item/proc/go", name="go", parent="/obj/item",
        signature="go()", file="code/thing.dm", start_line=2, end_line=3,
    )
    # The file does not exist: the span can only come from the cache.
    cache = {"code/thing.dm": ["/obj/item", "go()", "\treturn"]}

    assert query.source_span(tmp_path, hit, cache) == "go()\n\treturn"


def test_source_span_populates_the_cache(tmp_path: Path) -> None:
    (tmp_path / "code").mkdir()
    (tmp_path / "code" / "thing.dm").write_text("/obj/item\ngo()\n\treturn\n", encoding="utf-8")
    hit = query.SymbolHit(
        id=1, kind="proc", path="/obj/item/proc/go", name="go", parent="/obj/item",
        signature="go()", file="code/thing.dm", start_line=2, end_line=3,
    )
    cache: dict[str, list[str]] = {}

    assert query.source_span(tmp_path, hit, cache) == "go()\n\treturn"
    assert cache["code/thing.dm"] == ["/obj/item", "go()", "\treturn"]


def test_type_family_is_not_truncated_at_five_thousand(conn: sqlite3.Connection) -> None:
    file_id = store.insert_file(conn, "code/family.dm", 100, "sha", 0.0)
    # `/datum` and `/obj` really have ~19k descendants on a large fork; the old
    # 5,000 cap silently dropped the rest, so `port` and `ci` missed call sites.
    rows = [(f"/obj/child_{index}", "/obj", 0) for index in range(6000)]
    store.insert_inherits(conn, file_id, rows)
    conn.commit()

    family = query.type_family(conn, "/obj")

    assert len(family) == 6001
    assert "/obj/child_5999" in family
