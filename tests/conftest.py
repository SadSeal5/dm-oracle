from __future__ import annotations

import sqlite3

import pytest

from oracle import store


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    store.init_schema(connection)
    yield connection
    connection.close()
