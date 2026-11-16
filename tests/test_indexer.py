from __future__ import annotations

from pathlib import Path

import pytest

from oracle import store
from oracle.config import RepoPaths
from oracle.indexer import run_index


def _repo(tmp_path: Path) -> RepoPaths:
    return RepoPaths.discover(tmp_path)


def _write(root: Path, name: str, text: str) -> None:
    (root / name).write_text(text, encoding="utf-8")


def _rows(paths: RepoPaths, sql: str) -> list[tuple]:
    conn = store.connect(paths.db_path)
    try:
        return [tuple(row) for row in conn.execute(sql)]
    finally:
        conn.close()


def test_reindex_replaces_params_and_locals(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.dm",
        "/obj/child/proc/act(mob/user)\n"
        "\tvar/mob/living/target = user\n"
        "\treturn target\n",
    )
    paths = _repo(tmp_path)
    run_index(paths, quiet=True)
    assert _rows(paths, "SELECT proc_path, name, type FROM params") == [
        ("/obj/child/proc/act", "user", "/mob")
    ]
    assert _rows(paths, "SELECT proc_path, name, type FROM locals") == [
        ("/obj/child/proc/act", "target", "/mob/living")
    ]

    # Drop the local and retype the parameter.
    _write(tmp_path, "a.dm", "/obj/child/proc/act(obj/item/tool)\n\treturn tool\n")
    run_index(paths, quiet=True)

    assert _rows(paths, "SELECT proc_path, name, type FROM params") == [
        ("/obj/child/proc/act", "tool", "/obj/item")
    ]
    assert _rows(paths, "SELECT proc_path, name, type FROM locals") == []


def test_deleting_a_file_removes_derived_rows(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.dm",
        "/obj/child\n"
        "\tparent_type = /obj/base\n"
        "/obj/child/proc/act(mob/user)\n"
        "\tvar/mob/living/target = user\n"
        "\treturn target\n",
    )
    paths = _repo(tmp_path)
    run_index(paths, quiet=True)
    assert _rows(paths, "SELECT child, parent FROM inherits") == [("/obj/child", "/obj/base")]

    (tmp_path / "a.dm").unlink()
    run_index(paths, quiet=True)

    assert _rows(paths, "SELECT child, parent FROM inherits") == []
    assert _rows(paths, "SELECT proc_path FROM params") == []
    assert _rows(paths, "SELECT proc_path FROM locals") == []


def test_changing_parent_type_replaces_the_edge(tmp_path: Path) -> None:
    _write(tmp_path, "a.dm", "/obj/child\n\tparent_type = /obj/base\n\tvar/x = 1\n")
    paths = _repo(tmp_path)
    run_index(paths, quiet=True)
    assert _rows(paths, "SELECT child, parent, explicit FROM inherits") == [
        ("/obj/child", "/obj/base", 1)
    ]

    _write(tmp_path, "a.dm", "/obj/child\n\tparent_type = /obj/other\n\tvar/x = 1\n")
    run_index(paths, quiet=True)
    assert _rows(paths, "SELECT child, parent, explicit FROM inherits") == [
        ("/obj/child", "/obj/other", 1)
    ]


def test_old_schema_requires_force(tmp_path: Path) -> None:
    _write(tmp_path, "a.dm", "/obj/thing\n\tvar/x = 1\n")
    paths = _repo(tmp_path)
    run_index(paths, quiet=True)

    conn = store.connect(paths.db_path)
    store.set_meta(conn, "schema_version", "2")
    conn.commit()
    conn.close()

    with pytest.raises(store.SchemaMismatch):
        run_index(paths, quiet=True)

    run_index(paths, quiet=True, force=True)
    conn = store.connect(paths.db_path)
    assert store.schema_version(conn) == store.SCHEMA_VERSION
    conn.close()


def test_incremental_skip_does_not_duplicate_rows(tmp_path: Path) -> None:
    _write(tmp_path, "a.dm", "/obj/child/proc/act(mob/user)\n\treturn user\n")
    paths = _repo(tmp_path)
    run_index(paths, quiet=True)
    run_index(paths, quiet=True)
    run_index(paths, quiet=True)
    assert _rows(paths, "SELECT proc_path, name FROM params") == [
        ("/obj/child/proc/act", "user")
    ]


def test_insert_file_upsert_returns_the_same_id(tmp_path: Path) -> None:
    paths = _repo(tmp_path)
    conn = store.connect(paths.db_path)
    try:
        store.init_schema(conn)
        first = store.insert_file(conn, "a.dm", 10, "aaa", 0.0)
        second = store.insert_file(conn, "a.dm", 12, "bbb", 1.0)
        conn.commit()
        assert first == second
        assert _rows(paths, "SELECT loc, sha1 FROM files") == [(12, "bbb")]
    finally:
        conn.close()


def test_init_schema_does_not_touch_an_old_database(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "old.db")
    try:
        store.init_schema(conn)
        store.set_meta(conn, "schema_version", "1")
        conn.commit()
        # Must not raise on the missing `file_id` columns of the old layout.
        store.init_schema(conn)
        assert store.schema_version(conn) == 1
    finally:
        conn.close()
