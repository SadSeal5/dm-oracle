from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from helpers import add_call, add_symbol
from oracle import multi, store


def _repo(conn: sqlite3.Connection, path: str) -> int:
    return store.insert_file(conn, path, 100, "sha", 0.0)


def _left(conn: sqlite3.Connection) -> dict[str, int]:
    file_id = _repo(conn, "code/left.dm")
    ids = {
        "/obj/item": add_symbol(conn, file_id, "type", "/obj/item"),
        "/obj/item/proc/shared": add_symbol(
            conn, file_id, "proc", "/obj/item/proc/shared", parent="/obj/item",
            signature="shared()", body_hash="aaa",
        ),
        "/obj/item/proc/diverged": add_symbol(
            conn, file_id, "proc", "/obj/item/proc/diverged", parent="/obj/item",
            signature="diverged()", body_hash="bbb",
        ),
        "/obj/item/proc/left_only": add_symbol(
            conn, file_id, "proc", "/obj/item/proc/left_only", parent="/obj/item",
            signature="left_only()", body_hash="ccc",
        ),
    }
    conn.commit()
    return ids


def _right(conn: sqlite3.Connection) -> dict[str, int]:
    file_id = _repo(conn, "code/right.dm")
    ids = {
        "/obj/item": add_symbol(conn, file_id, "type", "/obj/item"),
        "/obj/item/proc/shared": add_symbol(
            conn, file_id, "proc", "/obj/item/proc/shared", parent="/obj/item",
            signature="shared()", body_hash="aaa",
        ),
        "/obj/item/proc/diverged": add_symbol(
            conn, file_id, "proc", "/obj/item/proc/diverged", parent="/obj/item",
            signature="diverged(mob/user)", body_hash="zzz",
        ),
        "/obj/item/proc/right_only": add_symbol(
            conn, file_id, "proc", "/obj/item/proc/right_only", parent="/obj/item",
            signature="right_only()", body_hash="ddd",
        ),
    }
    conn.commit()
    return ids


@pytest.fixture()
def pair(conn: sqlite3.Connection) -> tuple[sqlite3.Connection, sqlite3.Connection]:
    other = sqlite3.connect(":memory:")
    other.row_factory = sqlite3.Row
    other.execute("PRAGMA foreign_keys = ON")
    store.init_schema(other)
    _left(conn)
    _right(other)
    yield conn, other
    other.close()


def test_diff_splits_shared_only_and_changed(pair) -> None:
    left, right = pair
    report = multi.diff(left, right, "left", "right")

    assert [record.path for record in report.only_right] == ["/obj/item/proc/right_only"]
    assert [record.path for record in report.only_left] == ["/obj/item/proc/left_only"]
    assert [left.path for left, _right in report.changed] == ["/obj/item/proc/diverged"]

    proc = report.counts["proc"]
    assert proc["left"] == 3
    assert proc["right"] == 3
    assert proc["shared"] == 2
    assert proc["only_left"] == 1
    assert proc["only_right"] == 1
    assert proc["changed"] == 1
    assert report.shared == 3  # 2 procs + 1 type


def test_diff_ignores_kinds_outside_the_filter(pair) -> None:
    left, right = pair
    report = multi.diff(left, right, "left", "right", kinds=("type",))
    assert report.only_left == []
    assert report.only_right == []
    assert report.changed == []
    assert report.counts["type"]["shared"] == 1


def test_diff_detects_signature_only_changes(conn: sqlite3.Connection) -> None:
    other = sqlite3.connect(":memory:")
    other.row_factory = sqlite3.Row
    store.init_schema(other)
    left_file = _repo(conn, "code/a.dm")
    right_file = _repo(other, "code/a.dm")
    add_symbol(conn, left_file, "proc", "/proc/thing", signature="thing(a)", body_hash="same")
    add_symbol(other, right_file, "proc", "/proc/thing", signature="thing(a, b)", body_hash="same")

    report = multi.diff(conn, other, "left", "right")
    assert len(report.changed) == 1
    left_record, right_record = report.changed[0]
    assert left_record.signature == "thing(a)"
    assert right_record.signature == "thing(a, b)"
    other.close()


def test_port_reports_missing_symbol_and_owner(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    target_file = _repo(conn, "code/target.dm")
    add_symbol(conn, target_file, "type", "/obj/item")

    source = sqlite3.connect(":memory:")
    source.row_factory = sqlite3.Row
    store.init_schema(source)
    source_file = _repo(source, "code/source.dm")
    add_symbol(source, source_file, "type", "/obj/item")
    add_symbol(
        source, source_file, "proc", "/obj/item/proc/special_act",
        parent="/obj/item", signature="special_act()", body_hash="eee",
    )

    report = multi.port(conn, tmp_path, source, tmp_path, "/obj/item/proc/special_act",
                        source_name="upstream", target_name="here")
    assert report.status == "missing"
    assert report.owner == "/obj/item"
    assert report.owner_missing is False
    assert report.source is not None
    assert report.source.signature == "special_act()"
    source.close()


def test_port_flags_missing_owner_type(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    source = sqlite3.connect(":memory:")
    source.row_factory = sqlite3.Row
    store.init_schema(source)
    source_file = _repo(source, "code/source.dm")
    add_symbol(source, source_file, "proc", "/obj/machinery/gun/proc/fire",
               parent="/obj/machinery/gun", signature="fire()", body_hash="fff")

    report = multi.port(conn, tmp_path, source, tmp_path, "/obj/machinery/gun/proc/fire",
                        source_name="upstream", target_name="here")
    assert report.status == "missing"
    assert report.owner == "/obj/machinery/gun"
    assert report.owner_missing is True
    source.close()


def test_port_call_sites_are_scoped_to_the_type_family(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    target_file = _repo(conn, "code/target.dm")
    add_symbol(conn, target_file, "type", "/obj/item")
    add_symbol(conn, target_file, "type", "/obj/item/weapon", parent="/obj/item")
    add_symbol(conn, target_file, "type", "/obj/machinery")
    item_proc = add_symbol(
        conn, target_file, "proc", "/obj/item/proc/use_item", parent="/obj/item",
        signature="use_item()", body_hash="1",
    )
    weapon_proc = add_symbol(
        conn, target_file, "proc", "/obj/item/weapon/proc/swing", parent="/obj/item/weapon",
        signature="swing()", body_hash="2",
    )
    machine_proc = add_symbol(
        conn, target_file, "proc", "/obj/machinery/proc/process", parent="/obj/machinery",
        signature="process()", body_hash="3",
    )
    store.insert_inherits(conn, [("/obj/item/weapon", "/obj/item", 0)])
    add_call(conn, target_file, item_proc, "special_act", line=10)
    add_call(conn, target_file, weapon_proc, "item.special_act", line=20)
    add_call(conn, target_file, machine_proc, "special_act", line=30)
    conn.commit()

    source = sqlite3.connect(":memory:")
    source.row_factory = sqlite3.Row
    store.init_schema(source)
    source_file = _repo(source, "code/source.dm")
    add_symbol(source, source_file, "proc", "/obj/item/proc/special_act",
               parent="/obj/item", signature="special_act()", body_hash="4")

    report = multi.port(conn, tmp_path, source, tmp_path, "/obj/item/proc/special_act",
                        source_name="upstream", target_name="here")
    callers = {edge.caller_path for edge in report.target_callers}
    assert callers == {"/obj/item/proc/use_item", "/obj/item/weapon/proc/swing"}
    source.close()

