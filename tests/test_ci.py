from __future__ import annotations

import sqlite3

from helpers import add_call, add_symbol
from oracle import ci, query, store

PATCH = """\
diff --git a/code/foo.dm b/code/foo.dm
index 1111111..2222222 100644
--- a/code/foo.dm
+++ b/code/foo.dm
@@ -10,2 +10,3 @@
-old line
+new line
+extra line
@@ -40,0 +41,1 @@
+inserted line
diff --git a/code/notes.md b/code/notes.md
--- a/code/notes.md
+++ b/code/notes.md
@@ -1 +1 @@
-a
+b
diff --git a/code/gone.dm b/code/gone.dm
deleted file mode 100644
--- a/code/gone.dm
+++ /dev/null
@@ -1,3 +0,0 @@
-/obj/thing
-	foo()
-	bar()
"""


def _file(conn: sqlite3.Connection, path: str) -> int:
    return store.insert_file(conn, path, 100, "sha", 0.0)


def test_parse_unified_diff_keeps_dm_files_and_hunks() -> None:
    diffs = ci.parse_unified_diff(PATCH)
    assert [item.path for item in diffs] == ["code/foo.dm", "code/gone.dm"]

    foo = diffs[0]
    assert foo.added is False
    assert foo.deleted is False
    assert [(hunk.new_start, hunk.new_count) for hunk in foo.hunks] == [(10, 3), (41, 1)]
    assert foo.hunks[0].new_range == (10, 12)
    assert foo.hunks[1].new_range == (41, 41)

    gone = diffs[1]
    assert gone.deleted is True
    assert gone.path == "code/gone.dm"


def test_changed_symbols_intersect_hunks(conn: sqlite3.Connection) -> None:
    file_id = _file(conn, "code/foo.dm")
    add_symbol(conn, file_id, "proc", "/obj/thing/proc/hit", start_line=5, end_line=12)
    add_symbol(conn, file_id, "proc", "/obj/thing/proc/miss", start_line=20, end_line=30)
    add_symbol(conn, file_id, "type", "/obj/thing", start_line=38, end_line=45)
    conn.commit()

    changed = ci.changed_symbols(conn, ci.parse_unified_diff(PATCH))
    assert {hit.path for hit in changed} == {"/obj/thing/proc/hit", "/obj/thing"}


def test_changed_symbols_anchor_deletions_at_the_new_side(conn: sqlite3.Connection) -> None:
    file_id = _file(conn, "code/foo.dm")
    add_symbol(conn, file_id, "proc", "/obj/thing/proc/hit", start_line=5, end_line=12)
    conn.commit()

    patch = """\
diff --git a/code/foo.dm b/code/foo.dm
--- a/code/foo.dm
+++ b/code/foo.dm
@@ -9,2 +8,0 @@
-gone one
-gone two
"""
    changed = ci.changed_symbols(conn, ci.parse_unified_diff(patch))
    assert [hit.path for hit in changed] == ["/obj/thing/proc/hit"]


def test_expand_walks_transitive_callers(conn: sqlite3.Connection) -> None:
    file_id = _file(conn, "code/foo.dm")
    leaf = add_symbol(conn, file_id, "proc", "/proc/leaf")
    mid = add_symbol(conn, file_id, "proc", "/proc/mid")
    top = add_symbol(conn, file_id, "proc", "/proc/top")
    add_symbol(conn, file_id, "proc", "/proc/unrelated")
    add_call(conn, file_id, mid, "leaf", line=2)
    add_call(conn, file_id, top, "mid", line=3)
    conn.execute("UPDATE calls SET callee_path = '/proc/leaf' WHERE callee_raw = 'leaf'")
    conn.execute("UPDATE calls SET callee_path = '/proc/mid' WHERE callee_raw = 'mid'")
    conn.commit()

    seed = query.get_symbol(conn, "/proc/leaf")[0]
    affected = ci.expand(conn, [seed], max_depth=3)

    assert set(affected) == {"/proc/mid", "/proc/top"}
    assert affected["/proc/mid"].depth == 1
    assert affected["/proc/top"].depth == 2
    assert affected["/proc/top"].roots == {"/proc/leaf"}


def test_expand_pulls_in_members_of_a_changed_type(conn: sqlite3.Connection) -> None:
    file_id = _file(conn, "code/foo.dm")
    add_symbol(conn, file_id, "type", "/obj/item")
    member = add_symbol(conn, file_id, "proc", "/obj/item/proc/use", parent="/obj/item")
    caller = add_symbol(conn, file_id, "proc", "/proc/other")
    add_call(conn, file_id, caller, "use", line=4)
    conn.execute("UPDATE calls SET callee_path = '/obj/item/proc/use' WHERE callee_raw = 'use'")
    conn.commit()

    seed = query.get_symbol(conn, "/obj/item")[0]
    affected = ci.expand(conn, [seed], max_depth=2)

    assert "/proc/other" in affected
    assert affected["/proc/other"].roots == {"/obj/item"}
    assert member is not None


def test_suggest_tests_reports_the_test_datum(conn: sqlite3.Connection) -> None:
    file_id = _file(conn, "code/foo.dm")
    add_symbol(conn, file_id, "proc", "/proc/leaf")
    test_proc = add_symbol(
        conn, file_id, "proc", "/datum/unit_test/leaf_check/proc/Run",
        parent="/datum/unit_test/leaf_check",
    )
    add_call(conn, file_id, test_proc, "leaf", line=7)
    conn.execute("UPDATE calls SET callee_path = '/proc/leaf' WHERE callee_raw = 'leaf'")
    conn.commit()

    suggestions = ci.suggest_tests(conn, {"/proc/leaf"}, ["code/foo.dm"])
    assert [item.path for item in suggestions] == ["/datum/unit_test/leaf_check"]
    assert suggestions[0].reason == "calls an affected proc"


def test_suggest_tests_falls_back_to_file_mentions(conn: sqlite3.Connection) -> None:
    file_id = _file(conn, "code/foo_behaviour.dm")
    test_proc = add_symbol(
        conn, file_id, "proc", "/datum/unit_test/foo_behaviour/proc/Run",
        parent="/datum/unit_test/foo_behaviour",
    )
    store.insert_fts(
        conn, test_proc, "/datum/unit_test/foo_behaviour/proc/Run", "Run",
        "Run()", None, "checks foo behaviour in the game loop",
    )
    conn.commit()

    suggestions = ci.suggest_tests(conn, set(), ["code/foo_behaviour.dm"])
    assert [item.path for item in suggestions] == ["/datum/unit_test/foo_behaviour"]
    assert suggestions[0].reason.startswith("mentions")

DELETED = """\
diff --git a/code/gone.dm b/code/gone.dm
deleted file mode 100644
--- a/code/gone.dm
+++ /dev/null
@@ -1,4 +0,0 @@
-/obj/item/proc/gone_proc(mob/user)
-	var/mob/living/target = user
-	target.attack_self()
-	return
"""


def test_parse_unified_diff_keeps_the_removed_text() -> None:
    diffs = ci.parse_unified_diff(DELETED)
    gone = diffs[0]
    assert gone.deleted is True
    # The index has no rows for a deleted file, so this is the only record of
    # what it used to declare.
    assert [line for _number, line in gone.removed_lines] == [
        "/obj/item/proc/gone_proc(mob/user)",
        "\tvar/mob/living/target = user",
        "\ttarget.attack_self()",
        "\treturn",
    ]
    assert [number for number, _line in gone.removed_lines] == [1, 2, 3, 4]


def test_removed_symbols_reports_the_callers_of_a_deleted_file(
    conn: sqlite3.Connection,
) -> None:
    file_id = _file(conn, "code/caller.dm")
    add_symbol(conn, file_id, "type", "/obj/item")
    add_symbol(conn, file_id, "type", "/obj/item/weapon")
    add_symbol(conn, file_id, "proc", "/obj/item/proc/caller", parent="/obj/item")
    add_symbol(conn, file_id, "proc", "/obj/item/weapon/proc/other", parent="/obj/item/weapon")
    add_symbol(conn, file_id, "proc", "/proc/unrelated")
    store.insert_inherits(conn, file_id, [("/obj/item/weapon", "/obj/item", 0)])
    for caller in ("/obj/item/proc/caller", "/obj/item/weapon/proc/other", "/proc/unrelated"):
        caller_id = query.get_symbol(conn, caller)[0].id
        add_call(conn, file_id, caller_id, "gone_proc", line=3)
    conn.commit()

    removed = ci.removed_symbols(conn, ci.parse_unified_diff(DELETED))

    assert [symbol.path for symbol in removed] == ["/obj/item/proc/gone_proc"]
    # The bare name is matched literally and restricted to the owner family, so
    # an unrelated `gone_proc()` elsewhere is not blamed.
    assert {edge.caller_path for edge in removed[0].callers} == {
        "/obj/item/proc/caller",
        "/obj/item/weapon/proc/other",
    }


def test_removed_symbols_ignores_paths_still_defined_elsewhere(
    conn: sqlite3.Connection,
) -> None:
    file_id = _file(conn, "code/still_here.dm")
    add_symbol(conn, file_id, "type", "/obj/item")
    add_symbol(conn, file_id, "proc", "/obj/item/proc/gone_proc", parent="/obj/item")
    conn.commit()

    # The deleted file declared the same paths, but the index still has them.
    assert ci.removed_symbols(conn, ci.parse_unified_diff(DELETED)) == []


def test_removed_impact_marks_callers_as_first_order_breakage(
    conn: sqlite3.Connection,
) -> None:
    file_id = _file(conn, "code/caller.dm")
    add_symbol(conn, file_id, "type", "/obj/item")
    caller = add_symbol(conn, file_id, "proc", "/obj/item/proc/caller", parent="/obj/item")
    add_call(conn, file_id, caller, "gone_proc", line=3)
    conn.commit()

    removed = ci.removed_symbols(conn, ci.parse_unified_diff(DELETED))
    affected = ci.removed_impact(conn, removed)

    assert set(affected) == {"/obj/item/proc/caller"}
    assert affected["/obj/item/proc/caller"].depth == 1
    assert affected["/obj/item/proc/caller"].roots == {"/obj/item/proc/gone_proc"}


def test_suggest_tests_finds_tests_that_called_a_removed_proc(
    conn: sqlite3.Connection,
) -> None:
    file_id = _file(conn, "code/tests.dm")
    add_symbol(conn, file_id, "type", "/obj/item")
    test_proc = add_symbol(
        conn, file_id, "proc", "/datum/unit_test/gone_check/proc/Run",
        parent="/datum/unit_test/gone_check",
    )
    add_call(conn, file_id, test_proc, "gone_proc", line=7)
    conn.commit()

    suggestions = ci.suggest_tests(
        conn,
        set(),
        [],
        removed_paths={"/obj/item/proc/gone_proc"},
    )

    assert [item.path for item in suggestions] == ["/datum/unit_test/gone_check"]
    assert "removes" in suggestions[0].reason

GLOBAL_DELETION = """\
diff --git a/code/gone_global.dm b/code/gone_global.dm
deleted file mode 100644
--- a/code/gone_global.dm
+++ /dev/null
@@ -1,2 +0,0 @@
-/proc/gone_global()
-	return
"""


def test_removed_global_proc_matches_callers_without_a_type_family(
    conn: sqlite3.Connection,
) -> None:
    file_id = _file(conn, "code/caller.dm")
    caller = add_symbol(conn, file_id, "proc", "/proc/other")
    add_call(conn, file_id, caller, "gone_global", line=2)
    conn.commit()

    removed = ci.removed_symbols(conn, ci.parse_unified_diff(GLOBAL_DELETION))

    assert [symbol.path for symbol in removed] == ["/proc/gone_global"]
    # `/proc/...` has no owner type, so the family restriction must not apply.
    assert [edge.caller_path for edge in removed[0].callers] == ["/proc/other"]
