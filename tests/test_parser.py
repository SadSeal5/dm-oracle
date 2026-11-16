from __future__ import annotations

from pathlib import Path

from oracle.parser import parse_file

SNIPPET = '''/// A test item.
/obj/item/thing
\tvar/name = "thing"
\tvar/mob/living/target
\tvar/obj/item/held

/obj/item/thing/proc/pickup(mob/user, obj/item/tool)
\tvar/mob/living/target = user
\tfor(var/mob/M in range(3))
\t\tM.attack_self()
\tuser.put_in_hands(tool)
\ttarget.attack_self()
\t..()

/obj/item/thing/attack_self(mob/user)
\treturn

mob/Destroy()
\treturn

#define SHOULD_CALL_PARENT(x) if(!(x)) stack_trace("no parent")
#include "defines.dm"
'''


def _parse(tmp_path: Path):
    path = tmp_path / "test.dm"
    path.write_text(SNIPPET, encoding="utf-8")
    return parse_file(path, tmp_path)


def test_extracts_types_and_members(tmp_path: Path) -> None:
    parsed = _parse(tmp_path)
    assert parsed.error is None

    paths = {symbol.path for symbol in parsed.symbols}
    assert "/obj/item/thing" in paths
    assert "/obj/item/thing/var/name" in paths
    assert "/obj/item/thing/var/target" in paths
    assert "/obj/item/thing/var/held" in paths
    assert "/obj/item/thing/proc/pickup" in paths
    assert "/obj/item/thing/proc/attack_self" in paths


def test_prefix_form_declares_proc_not_type(tmp_path: Path) -> None:
    parsed = _parse(tmp_path)
    destroy = [s for s in parsed.symbols if s.path == "/mob/proc/Destroy"]
    assert len(destroy) == 1
    assert destroy[0].kind == "proc"
    # The prefix form must not create a duplicate /mob type declaration.
    mob_types = [s for s in parsed.symbols if s.path == "/mob" and s.kind == "type"]
    assert mob_types == []


def test_typed_vars_keep_their_declared_type(tmp_path: Path) -> None:
    parsed = _parse(tmp_path)
    target = next(s for s in parsed.symbols if s.path == "/obj/item/thing/var/target")
    assert target.signature == "var/mob/living/target"


def test_proc_parameters_are_typed(tmp_path: Path) -> None:
    parsed = _parse(tmp_path)
    params = {(name, declared) for _proc, name, declared, _pos in parsed.params}
    assert ("user", "/mob") in params
    assert ("tool", "/obj/item") in params


def test_local_vars_are_typed(tmp_path: Path) -> None:
    parsed = _parse(tmp_path)
    locals_ = {(name, declared) for _proc, name, declared, _line in parsed.locals}
    assert ("target", "/mob/living") in locals_
    assert ("M", "/mob") in locals_


def test_calls_are_attributed_to_the_enclosing_proc(tmp_path: Path) -> None:
    parsed = _parse(tmp_path)
    by_raw = {call.callee_raw: call for call in parsed.calls}
    assert by_raw["user.put_in_hands"].caller_path == "/obj/item/thing/proc/pickup"
    assert by_raw["target.attack_self"].caller_path == "/obj/item/thing/proc/pickup"
    assert by_raw[".."].caller_path == "/obj/item/thing/proc/pickup"


def test_chained_calls_drop_the_inner_arguments(tmp_path: Path) -> None:
    path = tmp_path / "chain.dm"
    path.write_text(
        "/obj/thing/proc/go(mob/user)\n"
        "\tuser.get_item().attack_self(user)\n",
        encoding="utf-8",
    )
    parsed = parse_file(path, tmp_path)
    raws = {call.callee_raw for call in parsed.calls}
    # The inner call's arguments must not end up inside the outer target.
    assert "user.get_item().attack_self" not in raws
    assert "user.get_item.attack_self" in raws
    assert "user.get_item" in raws


def test_macro_invocations_are_recorded_by_name(tmp_path: Path) -> None:
    path = tmp_path / "macro.dm"
    path.write_text(
        "#define RUSTG_CALL(x, y) x\n"
        "/obj/thing/proc/go()\n"
        '\tRUSTG_CALL(RUST_G, "unix_timestamp")\n',
        encoding="utf-8",
    )
    parsed = parse_file(path, tmp_path)
    raws = {call.callee_raw for call in parsed.calls}
    assert "RUSTG_CALL" in raws
    assert 'RUSTG_CALL(RUST_G, "unix_timestamp")' not in raws


def test_a_call_on_a_call_result_is_recorded_once(tmp_path: Path) -> None:
    path = tmp_path / "nested.dm"
    path.write_text(
        "/obj/thing/proc/go()\n"
        '\tcall(src, "do_thing")(1)\n',
        encoding="utf-8",
    )
    parsed = parse_file(path, tmp_path)
    raws = [call.callee_raw for call in parsed.calls]
    # The outer and inner call nodes start at the same position.
    assert raws.count("call") == 1


def test_defines_and_includes(tmp_path: Path) -> None:
    parsed = _parse(tmp_path)
    assert any(name == "SHOULD_CALL_PARENT" for name, _value, _line in parsed.defines)
    assert "defines.dm" in parsed.includes

def test_explicit_parent_type_replaces_the_path_edge(tmp_path: Path) -> None:
    path = tmp_path / "child.dm"
    path.write_text(
        "/obj/child\n"
        "\tparent_type = /obj/base\n"
        "\tvar/x = 1\n",
        encoding="utf-8",
    )
    parsed = parse_file(path, tmp_path)
    assert parsed.error is None
    # The implicit `/obj` edge must not survive next to the explicit parent.
    assert parsed.inherits == [("/obj/child", "/obj/base", 1)]


def test_type_body_holds_var_declarations_only(tmp_path: Path) -> None:
    path = tmp_path / "body.dm"
    path.write_text(
        "/obj/thing\n"
        '\tvar/name = "thing"\n'
        "\tvar/mob/living/target\n"
        "\n"
        "/obj/thing/proc/go()\n"
        "\treturn\n",
        encoding="utf-8",
    )
    parsed = parse_file(path, tmp_path)
    thing = next(s for s in parsed.symbols if s.path == "/obj/thing" and s.kind == "type")
    assert "var/name" in thing.body
    assert "var/mob/living/target" in thing.body
    # Nested procs are separate symbols; duplicating them here would make every
    # type body contain the whole subtree.
    assert "proc/go" not in thing.body
    assert thing.body_hash


def test_type_without_members_has_an_empty_body(tmp_path: Path) -> None:
    path = tmp_path / "empty.dm"
    path.write_text("/obj/empty\n", encoding="utf-8")
    parsed = parse_file(path, tmp_path)
    empty = next(s for s in parsed.symbols if s.path == "/obj/empty" and s.kind == "type")
    assert empty.body == ""


def test_bodyless_type_keeps_its_full_path(tmp_path: Path) -> None:
    path = tmp_path / "empty.dm"
    path.write_text("/obj/empty\n", encoding="utf-8")
    parsed = parse_file(path, tmp_path)
    paths = {s.path for s in parsed.symbols if s.kind == "type"}
    # The grammar flattens a bodyless type into sibling identifiers; taking
    # only the last one would index this as `/empty`.
    assert paths == {"/obj/empty"}


def test_nested_paths_do_not_emit_the_root_segment(tmp_path: Path) -> None:
    path = tmp_path / "nested.dm"
    path.write_text("/obj/a/b\n\tvar/x = 1\n", encoding="utf-8")
    parsed = parse_file(path, tmp_path)
    paths = {s.path for s in parsed.symbols if s.kind == "type"}
    assert paths == {"/obj/a", "/obj/a/b"}
    # `/obj` is the root segment of the chain, not a declaration in this file.
    assert "/obj" not in paths


def test_bodyless_deep_type_keeps_every_segment(tmp_path: Path) -> None:
    path = tmp_path / "deep.dm"
    path.write_text("/datum/component/foo/bar\n", encoding="utf-8")
    parsed = parse_file(path, tmp_path)
    paths = {s.path for s in parsed.symbols if s.kind == "type"}
    # The grammar flattens the tail into sibling identifiers; reading only the
    # `name` field would index this as `/datum/bar`.
    assert "/datum/component/foo/bar" in paths
    assert "/datum/bar" not in paths


def test_type_span_starts_at_the_declaration(tmp_path: Path) -> None:
    source = "/obj/a/b\n\tvar/x = 1\n"
    path = tmp_path / "span.dm"
    path.write_bytes(source.encode())
    parsed = parse_file(path, tmp_path)
    for symbol in parsed.symbols:
        if symbol.kind != "type":
            continue
        # The grammar gives each segment its own node and leaves the leading
        # `/` outside it; a span that starts at `b` would make an inserted
        # subtype look like a member of its parent.
        text = source.encode()[symbol.start_byte : symbol.end_byte].decode()
        assert text.startswith("/obj/a"), (symbol.path, text[:20])


def test_relative_subtype_span_starts_at_its_own_segment(tmp_path: Path) -> None:
    source = "/obj/a\n\tvar/x = 1\n\tb\n\t\tvar/y = 2\n"
    path = tmp_path / "relative.dm"
    path.write_bytes(source.encode())
    parsed = parse_file(path, tmp_path)
    subtype = next(s for s in parsed.symbols if s.path == "/obj/a/b")
    text = source.encode()[subtype.start_byte : subtype.end_byte].decode()
    assert text.startswith("b\n"), text[:20]


def test_modified_locals_keep_their_declared_type(tmp_path: Path) -> None:
    path = tmp_path / "static_locals.dm"
    path.write_text(
        "/obj/thing/proc/go()\n"
        "\tvar/static/list/cache = list()\n"
        "\tvar/static/mob/living/target\n"
        "\tvar/tmp/obj/item/held\n"
        "\tvar/const/limit = 3\n",
        encoding="utf-8",
    )
    parsed = parse_file(path, tmp_path)
    locals_ = {name: declared for _proc, name, declared, _line in parsed.locals}
    # The modifier wraps the type node, so a direct-child lookup misses it.
    assert locals_["cache"] == "/list"
    assert locals_["target"] == "/mob/living"
    assert locals_["held"] == "/obj/item"
    assert locals_["limit"] is None


def test_local_vars_are_not_indexed_as_type_members(tmp_path: Path) -> None:
    path = tmp_path / "locals.dm"
    path.write_text(
        "/obj/thing\n"
        '\tvar/name = "thing"\n'
        "\n"
        "/obj/thing/proc/go()\n"
        "\tvar/local_only = 1\n"
        "\tfor(var/mob/M in range(3))\n"
        "\t\tM.attack_self()\n",
        encoding="utf-8",
    )
    parsed = parse_file(path, tmp_path)
    paths = {s.path for s in parsed.symbols if s.kind == "var"}
    # A proc-local belongs to the proc, not to the owner type: indexing it as
    # `/obj/thing/var/local_only` would put every local on the type.
    assert "/obj/thing/var/local_only" not in paths
    assert "/obj/thing/var/M" not in paths
    assert paths == {"/obj/thing/var/name"}
    locals_ = {(name, declared) for _proc, name, declared, _line in parsed.locals}
    assert ("local_only", None) in locals_
    assert ("M", "/mob") in locals_


def test_doc_comments_are_attached_to_the_definition(tmp_path: Path) -> None:
    parsed = _parse(tmp_path)
    thing = next(s for s in parsed.symbols if s.path == "/obj/item/thing" and s.kind == "type")
    assert thing.doc == "A test item."


def _doc(tmp_path: Path, source: str) -> str | None:
    path = tmp_path / "doc.dm"
    path.write_text(source, encoding="utf-8")
    parsed = parse_file(path, tmp_path)
    thing = next(s for s in parsed.symbols if s.path == "/obj/thing")
    return thing.doc


def test_single_line_block_doc_comment_drops_the_delimiter(tmp_path: Path) -> None:
    # `/* ... */` used to keep the trailing `*/` in the doc text.
    assert _doc(tmp_path, "/* A doc comment. */\n/obj/thing\n") == "A doc comment."


def test_block_doc_comment_does_not_collect_the_line_above_it(tmp_path: Path) -> None:
    # The scan used to continue past the `/*` line and attach `// unrelated`.
    assert (
        _doc(tmp_path, "// unrelated\n/* A doc comment. */\n/obj/thing\n")
        == "A doc comment."
    )


def test_a_blank_line_ends_the_doc_comment(tmp_path: Path) -> None:
    # A comment separated from the definition is not its doc.
    assert _doc(tmp_path, "/// A doc comment.\n\n/obj/thing\n") is None


def test_a_trailing_block_comment_is_not_a_doc_comment(tmp_path: Path) -> None:
    assert _doc(tmp_path, "var/x = 1 /* note */\n/obj/thing\n") is None


def test_defines_after_a_multiline_macro_call_are_collected(tmp_path: Path) -> None:
    path = tmp_path / "macro_body.dm"
    path.write_text(
        "#define FIRST 1\n"
        "DEFINE_BITFIELD(flags, list(\n"
        "\tONE = \"one\",\n"
        "\tTWO = \"two\",\n"
        "))\n"
        "#define AFTER (1<<0)\n"
        "#define WITH_ARG(x) (x + 1)\n",
        encoding="utf-8",
    )
    parsed = parse_file(path, tmp_path)
    names = {name for name, _value, _line in parsed.defines}
    # The grammar turns the multi-line macro call into a `preproc_call` whose
    # body block holds the rest of the file; a walk that stops at the call
    # loses every define after it.
    assert names == {"FIRST", "AFTER", "WITH_ARG"}


def test_consecutive_defines_are_all_collected(tmp_path: Path) -> None:
    path = tmp_path / "consecutive.dm"
    path.write_text(
        "#define A \"a\"\n"
        "#define B \"b\"\n"
        "#define C \"c\"\n",
        encoding="utf-8",
    )
    parsed = parse_file(path, tmp_path)
    # The grammar nests each `#define` inside the previous one.
    assert [name for name, _value, _line in parsed.defines] == ["A", "B", "C"]


def test_a_commented_out_define_is_not_indexed(tmp_path: Path) -> None:
    path = tmp_path / "comments.dm"
    path.write_text(
        "#define REAL 1\n"
        "/*\n"
        "#define FAKE 2\n"
        "*/\n"
        "// #define ALSO_FAKE 3\n",
        encoding="utf-8",
    )
    parsed = parse_file(path, tmp_path)
    assert [name for name, _value, _line in parsed.defines] == ["REAL"]


def test_a_block_comment_marker_inside_a_string_is_not_a_comment(tmp_path: Path) -> None:
    path = tmp_path / "string_marker.dm"
    path.write_text(
        'var/x = "/* not a comment"\n'
        "#define REAL 1\n"
        'var/y = "*/"\n'
        "#define AFTER 2\n",
        encoding="utf-8",
    )
    parsed = parse_file(path, tmp_path)
    # A `/*` inside a string used to blank every following line until a `*/`.
    assert [name for name, _value, _line in parsed.defines] == ["REAL", "AFTER"]


def test_a_multiline_define_joins_its_continuations(tmp_path: Path) -> None:
    path = tmp_path / "continuation.dm"
    path.write_text(
        "#define FLAGS list( \\\n"
        "\tONE, \\\n"
        "\tTWO \\\n"
        ")\n"
        "#define AFTER 1\n",
        encoding="utf-8",
    )
    parsed = parse_file(path, tmp_path)
    values = {name: value for name, value, _line in parsed.defines}
    assert set(values) == {"FLAGS", "AFTER"}
    assert "ONE" in values["FLAGS"] and "TWO" in values["FLAGS"]
    assert values["AFTER"] == "1"
