from __future__ import annotations

import json
from pathlib import Path

import pytest

from oracle.eval import load_questions


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "questions.json"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_a_valid_file(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        json.dumps([{"query": "explosion", "expect": ["/proc/explosion"]}]),
    )
    questions = load_questions(path)
    assert questions[0].query == "explosion"
    assert questions[0].mode == "text"


def test_invalid_json_is_a_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        load_questions(_write(tmp_path, "{not json"))


def test_non_list_payload_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="JSON array"):
        load_questions(_write(tmp_path, '{"query": "x"}'))


def test_missing_keys_are_reported_with_the_index(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="question 1"):
        load_questions(_write(tmp_path, '[{"query": "x"}]'))


def test_expect_must_be_a_list(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be a list"):
        load_questions(_write(tmp_path, '[{"query": "x", "expect": "y"}]'))


def test_unknown_mode_is_rejected(tmp_path: Path) -> None:
    # A typo would silently run as a text query and skew the recall number.
    with pytest.raises(ValueError, match="'mode' must be"):
        load_questions(
            _write(tmp_path, '[{"query": "x", "expect": [], "mode": "symbo"}]')
        )


def test_symbol_mode_is_kept(tmp_path: Path) -> None:
    questions = load_questions(
        _write(tmp_path, '[{"query": "explosion", "expect": [], "mode": "symbol"}]')
    )
    assert questions[0].mode == "symbol"


def test_missing_file_raises_oserror(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        load_questions(tmp_path / "nope.json")
