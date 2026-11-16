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


def test_defines_and_includes(tmp_path: Path) -> None:
    parsed = _parse(tmp_path)
    assert any(name == "SHOULD_CALL_PARENT" for name, _value, _line in parsed.defines)
    assert "defines.dm" in parsed.includes
