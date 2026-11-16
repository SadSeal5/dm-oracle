from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
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

CALLER_SOURCE = (
    "/obj/item\n"
    "\tvar/name = \"thing\"\n"
    "\n"
    "/obj/item/proc/caller()\n"
    "\tgone_proc()\n"
    "\treturn\n"
)

GONE_SOURCE = (
    "/obj/item/proc/gone_proc(mob/user)\n"
    "\tvar/mob/living/target = user\n"
    "\ttarget.attack_self()\n"
    "\treturn\n"
)

DELETION_PATCH = """\
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


def _deleted_file_repo(tmp_path: Path) -> Path:
    """Index a repo, delete one file, re-index, and return the deletion patch."""
    (tmp_path / "code").mkdir()
    (tmp_path / "code" / "keep.dm").write_text(CALLER_SOURCE, encoding="utf-8")
    gone = tmp_path / "code" / "gone.dm"
    gone.write_text(GONE_SOURCE, encoding="utf-8")
    paths = RepoPaths.discover(tmp_path)
    run_index(paths, quiet=True)

    # The index always describes the new side of the diff.
    gone.unlink()
    run_index(paths, quiet=True)

    patch = tmp_path / "delete.patch"
    patch.write_text(DELETION_PATCH, encoding="utf-8")
    return patch


def test_ci_reports_the_callers_of_a_deleted_file(tmp_path: Path) -> None:
    patch = _deleted_file_repo(tmp_path)
    result = runner.invoke(app, ["-r", str(tmp_path), "ci", "--diff", str(patch)])

    assert result.exit_code == 0, result.output
    assert "/obj/item/proc/gone_proc" in result.output
    # The caller is the point: its target no longer exists.
    assert "/obj/item/proc/caller" in result.output


def test_ci_depth_zero_does_not_expand_deleted_file_callers(tmp_path: Path) -> None:
    patch = _deleted_file_repo(tmp_path)
    result = runner.invoke(
        app, ["-r", str(tmp_path), "ci", "--diff", str(patch), "--depth", "0"]
    )

    assert result.exit_code == 0, result.output
    # The removed symbol is still reported, but --depth 0 means no expansion.
    assert "/obj/item/proc/gone_proc" in result.output
    assert "/obj/item/proc/caller" not in result.output


def test_diff_reports_renamed_symbols(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    for root, name in ((left, "old_name"), (right, "new_name")):
        (root / "code").mkdir(parents=True)
        (root / "code" / "thing.dm").write_text(
            f"/obj/item/proc/{name}(mob/user)\n"
            "\tvar/mob/living/target = user\n"
            "\treturn target\n",
            encoding="utf-8",
        )
        run_index(RepoPaths.discover(root), quiet=True)

    result = runner.invoke(
        app, ["-r", str(left), "diff", str(right), "--only", "renamed"]
    )

    assert result.exit_code == 0, result.output
    assert "old_name" in result.output
    assert "new_name" in result.output


def test_diff_rejects_an_unknown_kind(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    for root in (left, right):
        (root / "code").mkdir(parents=True)
        (root / "code" / "thing.dm").write_text("/obj/item\n", encoding="utf-8")
        run_index(RepoPaths.discover(root), quiet=True)

    result = runner.invoke(
        app, ["-r", str(left), "diff", str(right), "--kind", "procs"]
    )

    # A typo would otherwise silently compare nothing.
    assert result.exit_code == 2
    assert "proc, type, var, verb" in result.output


def test_find_rejects_an_unknown_kind(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = runner.invoke(app, ["-r", str(repo), "find", "thing", "--kind", "procs"])

    assert result.exit_code == 2
    assert "proc, type, var, verb" in result.output


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

SURVIVING_SOURCE = (
    "/obj/item\n"
    "\tvar/name = \"thing\"\n"
    "\n"
    "/obj/item/proc/gone_proc()\n"
    "\treturn\n"
    "\n"
    "/obj/item/proc/caller()\n"
    "\tgone_proc()\n"
    "\treturn\n"
)

SURVIVING_AFTER = (
    "/obj/item\n"
    "\tvar/name = \"thing\"\n"
    "\n"
    "/obj/item/proc/caller()\n"
    "\treturn\n"
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
    )


@needs_git
def test_ci_base_rev_finds_a_proc_removed_from_a_surviving_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "code").mkdir()
    thing = tmp_path / "code" / "thing.dm"
    thing.write_text(SURVIVING_SOURCE, encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    _git(
        tmp_path,
        "-c",
        "user.name=oracle",
        "-c",
        "user.email=oracle@example.invalid",
        "commit",
        "-q",
        "-m",
        "initial",
    )

    # The index always describes the new side of the diff, so gone_proc is
    # already absent from it.
    thing.write_text(SURVIVING_AFTER, encoding="utf-8")
    run_index(RepoPaths.discover(tmp_path), quiet=True)

    result = runner.invoke(app, ["-r", str(tmp_path), "ci", "--base-rev", "HEAD"])

    assert result.exit_code == 0, result.output
    assert "/obj/item/proc/gone_proc" in result.output
    # The caller is the point: its target no longer exists.
    assert "/obj/item/proc/caller" in result.output


def test_ci_json_stays_parseable_when_the_index_is_stale(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    # Change the file without re-indexing: the index is now stale.
    (repo / "code" / "thing.dm").write_text(SOURCE + "\n// edited\n", encoding="utf-8")
    patch = tmp_path / "change.patch"
    patch.write_text(
        "diff --git a/code/thing.dm b/code/thing.dm\n"
        "--- a/code/thing.dm\n"
        "+++ b/code/thing.dm\n"
        "@@ -1,1 +1,1 @@\n"
        "-/obj/item/thing\n"
        "+/obj/item/thing\n",
        encoding="utf-8",
    )
    result = runner.invoke(
        app, ["-r", str(repo), "ci", "--diff", str(patch), "--json"]
    )

    assert result.exit_code == 0, result.output
    # The warning must not be mixed into the JSON stream.
    payload = json.loads(result.stdout)
    assert payload["stale_files"] == ["code/thing.dm"]
    assert "index is stale" in result.stderr


def test_ci_json_is_emitted_for_an_empty_diff(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    patch = tmp_path / "notes.patch"
    patch.write_text(
        "diff --git a/notes.md b/notes.md\n"
        "--- a/notes.md\n"
        "+++ b/notes.md\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n",
        encoding="utf-8",
    )
    result = runner.invoke(
        app, ["-r", str(repo), "ci", "--diff", str(patch), "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["files"] == []
    assert payload["stale_files"] == []


def test_eval_json_stays_parseable_without_embeddings(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    evals = tmp_path / "questions.json"
    evals.write_text(
        '[{"query": "explode", "expect": ["/obj/item/thing/proc/explode"], "mode": "symbol"}]',
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        ["-r", str(repo), "eval", "--file", str(evals), "--json", "--semantic"],
    )

    assert result.exit_code == 0, result.output
    # The "no embeddings" warning must not be mixed into the JSON stream.
    payload = json.loads(result.stdout)
    assert payload["total"] == 1
    assert "no embeddings" in result.stderr


@needs_git
def test_ci_base_rev_is_optional(tmp_path: Path) -> None:
    (tmp_path / "code").mkdir()
    thing = tmp_path / "code" / "thing.dm"
    thing.write_text(SURVIVING_SOURCE, encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    _git(
        tmp_path,
        "-c",
        "user.name=oracle",
        "-c",
        "user.email=oracle@example.invalid",
        "commit",
        "-q",
        "-m",
        "initial",
    )
    thing.write_text(SURVIVING_AFTER, encoding="utf-8")
    run_index(RepoPaths.discover(tmp_path), quiet=True)

    # Without --base-rev the hunk still attributes the deletion to the caller,
    # but the removed proc itself cannot be named.
    result = runner.invoke(app, ["-r", str(tmp_path), "ci"])

    assert result.exit_code == 0, result.output
    assert "/obj/item/proc/gone_proc" not in result.output


@needs_git
def test_ci_unknown_base_rev_is_reported(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    patch = tmp_path / "change.patch"
    patch.write_text(DELETION_PATCH, encoding="utf-8")
    result = runner.invoke(
        app, ["-r", str(repo), "ci", "--diff", str(patch), "--base-rev", "nope"]
    )

    assert result.exit_code == 2
    assert "unknown git revision" in result.output
