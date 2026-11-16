from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from helpers import add_symbol
from oracle import store

pytest.importorskip("mcp")

from oracle.config import RepoPaths  # noqa: E402
from oracle.mcp_server import create_server  # noqa: E402


def _server(tmp_path: Path):
    db_path = tmp_path / "oracle.db"
    conn = store.connect(db_path)
    store.init_schema(conn)
    file_id = store.insert_file(conn, "code/test.dm", 10, "abc", 0.0)
    add_symbol(conn, file_id, "proc", "/obj/item/proc/attack_self", start_line=5, end_line=8)
    add_symbol(conn, file_id, "type", "/obj/item", start_line=1, end_line=3)
    conn.commit()
    conn.close()
    return create_server(RepoPaths(root=tmp_path, db_path=db_path))


def test_mcp_exposes_the_oracle_tools(tmp_path: Path) -> None:
    server = _server(tmp_path)
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}
    assert {
        "oracle_stats",
        "oracle_find",
        "oracle_search",
        "oracle_show",
        "oracle_members",
        "oracle_callers",
        "oracle_callees",
        "oracle_impact",
        "oracle_context",
    } <= names


def test_find_tool_returns_json(tmp_path: Path) -> None:
    server = _server(tmp_path)
    result = asyncio.run(server.call_tool("oracle_find", {"pattern": "attack_self"}))
    payload = json.loads(result.content[0].text)
    assert payload[0]["path"] == "/obj/item/proc/attack_self"
    assert payload[0]["line"] == 5


def test_show_tool_reports_missing_symbols(tmp_path: Path) -> None:
    server = _server(tmp_path)
    result = asyncio.run(server.call_tool("oracle_show", {"symbol_path": "/nope"}))
    payload = json.loads(result.content[0].text)
    assert "not found" in payload["error"]


def test_find_tool_rejects_an_unknown_kind(tmp_path: Path) -> None:
    server = _server(tmp_path)
    result = asyncio.run(
        server.call_tool("oracle_find", {"pattern": "attack", "kind": "procs"})
    )
    payload = json.loads(result.content[0].text)
    assert "unknown kind" in payload["error"]
    assert "proc" in payload["valid"]
