from __future__ import annotations

import sqlite3

from helpers import add_call, add_symbol
from oracle import store
from oracle.resolve import resolve_all


def _build(conn: sqlite3.Connection) -> dict[str, int]:
    file_id = store.insert_file(conn, "code/test.dm", 100, "abc", 0.0)
    ids: dict[str, int] = {}

    for path in ("/obj", "/obj/item", "/obj/item/weapon", "/mob", "/datum"):
        ids[path] = add_symbol(conn, file_id, "type", path)

    procs = [
        ("/obj/item/proc/attack_self", "/obj/item"),
        ("/obj/item/proc/swing", "/obj/item"),
        ("/obj/item/proc/foo", "/obj/item"),
        ("/obj/item/weapon/proc/swing", "/obj/item/weapon"),
        ("/obj/item/weapon/proc/foo", "/obj/item/weapon"),
        ("/mob/proc/zzz", "/mob"),
        ("/datum/proc/zzz", "/datum"),
        ("/proc/global_helper", None),
    ]
    for path, parent in procs:
        ids[path] = add_symbol(conn, file_id, "proc", path, parent=parent)

    store.insert_inherits(
        conn,
        [
            ("/obj/item", "/obj", 0),
            ("/obj/item/weapon", "/obj/item", 0),
        ],
    )
    store.insert_params(
        conn,
        [("/obj/item/weapon/proc/swing", "M", "/obj/item", 1)],
    )
    store.insert_defines(conn, [(file_id, "SHOULD_CALL_PARENT", "1", 1)])

    caller = ids["/obj/item/weapon/proc/swing"]
    for raw in (
        "attack_self",
        "global_helper",
        "M.attack_self",
        "SHOULD_CALL_PARENT",
        "istype",
        "foo",
        "..",
        "zzz",
        "nonexistent_proc",
    ):
        add_call(conn, file_id, caller, raw)

    return ids


def _resolved(conn: sqlite3.Connection) -> dict[str, tuple[str | None, str]]:
    rows = conn.execute("SELECT callee_raw, callee_path, resolution FROM calls").fetchall()
    return {row["callee_raw"]: (row["callee_path"], row["resolution"]) for row in rows}


def test_resolution_strategies(conn: sqlite3.Connection) -> None:
    _build(conn)
    resolve_all(conn)
    resolved = _resolved(conn)

    assert resolved["attack_self"] == ("/obj/item/proc/attack_self", "inherited")
    assert resolved["global_helper"] == ("/proc/global_helper", "global")
    assert resolved["M.attack_self"] == ("/obj/item/proc/attack_self", "receiver")
    assert resolved["SHOULD_CALL_PARENT"] == (None, "macro")
    assert resolved["istype"] == (None, "builtin")
    assert resolved["foo"] == ("/obj/item/weapon/proc/foo", "exact")
    assert resolved[".."] == ("/obj/item/proc/swing", "inherited")
    assert resolved["zzz"] == (None, "ambiguous")
    assert resolved["nonexistent_proc"] == (None, "unresolved")


def test_resolution_counts(conn: sqlite3.Connection) -> None:
    _build(conn)
    counts = resolve_all(conn)
    assert counts["inherited"] >= 2
    assert counts["macro"] == 1
    assert counts["builtin"] == 1
    assert counts["ambiguous"] == 1
    assert counts["unresolved"] == 1
