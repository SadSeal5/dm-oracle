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
