from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from oracle.cli import app
from oracle.config import RepoPaths
from oracle.indexer import run_index

# DM source is full of bracketed type paths; Rich would read `[/obj/item]` as a
# closing markup tag and crash while printing a context bundle.
SOURCE = (
    "/obj/item/thing\n"
    "\tvar/name = \"[/obj/item]\"\n"
    "\n"
    "/// Explodes, see [/obj/item] for details.\n"
    "/obj/item/thing/proc/explode(mob/user)\n"
    "\tvar/mob/living/target = user\n"
    "\treturn target\n"
)

runner = CliRunner()


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "code").mkdir()
    (tmp_path / "code" / "thing.dm").write_text(SOURCE, encoding="utf-8")
    run_index(RepoPaths.discover(tmp_path), quiet=True)
    return tmp_path


def test_context_prints_bracketed_source(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = runner.invoke(app, ["-r", str(repo), "context", "explode"])

    assert result.exit_code == 0, result.output
    assert "/obj/item/thing/proc/explode" in result.output


def test_symbol_context_prints_bracketed_source(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = runner.invoke(
        app, ["-r", str(repo), "symbol-context", "/obj/item/thing/proc/explode"]
    )

    assert result.exit_code == 0, result.output


def test_context_zero_budget_does_not_emit_source(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = runner.invoke(
        app, ["-r", str(repo), "context", "explode", "--budget", "0"]
    )

    assert result.exit_code == 0, result.output
    assert "```dm" not in result.output


def test_missing_symbol_exits_nonzero(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = runner.invoke(app, ["-r", str(repo), "show", "/nope"])

    assert result.exit_code == 1
    assert "not found" in result.output


def test_unwritable_output_path_is_reported(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    result = runner.invoke(
        app, ["-r", str(repo), "context", "explode", "-o", str(blocker / "bundle.md")]
    )

    assert result.exit_code == 2
    assert "cannot write" in result.output


def test_search_snippet_with_brackets_is_escaped(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = runner.invoke(app, ["-r", str(repo), "search", "name"])

    assert result.exit_code == 0, result.output
    # The snippet comes from source; Rich must not swallow `[/obj/item]`.
    assert "[/obj/item]" in result.output


def test_show_doc_with_brackets_is_escaped(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = runner.invoke(
        app, ["-r", str(repo), "show", "/obj/item/thing/proc/explode"]
    )

    assert result.exit_code == 0, result.output
    assert "[/obj/item]" in result.output


def test_db_path_that_is_a_directory_is_reported(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = runner.invoke(app, ["-r", str(repo), "--db", str(tmp_path), "stats"])

    assert result.exit_code == 2
    assert "directory" in result.output
