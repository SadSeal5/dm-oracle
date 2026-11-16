"""DreamMaker (.dm) parsing built on the SpacemanDMM tree-sitter grammar.

The grammar is intentionally permissive: SS13 codebases contain a lot of
preprocessor trickery and legacy syntax. The parser therefore extracts what
it can and never raises on a single bad file.

Grammar notes (tree-sitter-dm 0.26):

* ``type_definition`` / ``subtype_definition`` declare one path segment each.
  ``/obj/item`` is ``type_definition(obj)`` containing ``subtype_definition(item)``.
* ``mob/Destroy()`` is a *prefix form*: ``type_definition(mob)`` whose member
  child starts on the same line as the type path. It declares a proc on
  ``/mob``, not a new type.
* ``var_definition`` wraps a ``var_assignment`` holding the identifier.
* ``proc_override`` is a proc without the ``proc/`` prefix.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_dm
from tree_sitter import Language, Node, Parser

_LANGUAGE: Language | None = None

TYPE_NODES = {"type_definition", "subtype_definition"}
PROC_NODES = {"proc_definition", "proc_declaration", "proc_override", "proc_statement"}
VAR_NODES = {"var_definition"}
ASSIGN_NODES = {"var_assignment"}
VERB_NODES = {"verb_definition", "verb_declaration"}
INCLUDE_NODES = {"preproc_include"}
CALL_NODES = {"call_expression"}
NEW_NODES = {"new_expression"}

MEMBER_NODES = PROC_NODES | VAR_NODES | VERB_NODES | ASSIGN_NODES

# `#define NAME value` and `#define NAME(params) value`. The name is a C-style
# identifier; the parameter list cannot contain parentheses.
_DEFINE_LINE = re.compile(r"^\s*#define\s+([A-Za-z_]\w*)(\(([^()]*)\))?\s*(.*)$")

PROC_SEGMENT = "proc"
VAR_SEGMENT = "var"
VERB_SEGMENT = "verb"


def language() -> Language:
    global _LANGUAGE
    if _LANGUAGE is None:
        try:
            _LANGUAGE = Language(tree_sitter_dm.language())
        except TypeError:  # older tree-sitter API
            _LANGUAGE = Language(tree_sitter_dm.language(), "dm")
    return _LANGUAGE


def make_parser() -> Parser:
    try:
        return Parser(language())
    except TypeError:  # older tree-sitter API
        parser = Parser()
        parser.set_language(language())
        return parser


@dataclass
class ParsedSymbol:
    kind: str
    path: str
    name: str
    parent: str | None
    signature: str | None
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    flags: str = ""
    doc: str | None = None
    body_hash: str | None = None
    body: str = ""
    node: Node | None = field(default=None, repr=False)


@dataclass
class ParsedCall:
    callee_raw: str
    kind: str
    line: int
    col: int
    caller_path: str | None


@dataclass
class ParsedFile:
    path: str
    source: bytes
    symbols: list[ParsedSymbol] = field(default_factory=list)
    calls: list[ParsedCall] = field(default_factory=list)
    defines: list[tuple[str, str | None, int]] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)
    params: list[tuple[str, str, str | None, int]] = field(default_factory=list)
    locals: list[tuple[str, str, str | None, int]] = field(default_factory=list)
    inherits: list[tuple[str, str, int]] = field(default_factory=list)
    error: str | None = None

    @property
    def loc(self) -> int:
        return self.source.count(b"\n") + 1


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _named_children(node: Node) -> list[Node]:
    return [child for child in node.children if child.is_named]


def _first_child_of_type(node: Node, types: set[str]) -> Node | None:
    for child in _named_children(node):
        if child.type in types:
            return child
    return None


def _path_node(node: Node) -> Node | None:
    for field_name in ("path", "name", "type", "target"):
        child = node.child_by_field_name(field_name)
        if child is not None:
            return child
    for child in _named_children(node):
        if child.type in {"identifier", "type_path", "path", "field_expression"}:
            return child
    return None


def _path_text(node: Node, source: bytes) -> str | None:
    path_node = _path_node(node)
    if path_node is None:
        return None
    text = _text(path_node, source).strip()
    return text or None


def _type_path_text(node: Node, source: bytes) -> str | None:
    """Raw path of a type node.

    A type with a body is a chain of nested ``subtype_definition`` nodes, so
    each level only owns one segment. A type *without* a body is flattened by
    the grammar into sibling identifiers (``/obj/empty`` -> ``obj``, ``empty``)
    and the ``name`` field points at the last one; joining them keeps the
    prefix instead of indexing the type as ``/empty``.
    """
    if node.type in {"type_definition", "subtype_definition"}:
        identifiers = [
            child
            for child in _named_children(node)
            if child.type == "identifier"
        ]
        nested = _first_child_of_type(node, {"subtype_definition"})
        if nested is None and len(identifiers) > 1:
            # A bodyless path is flattened into sibling identifiers:
            # ``/datum/element/foo`` becomes ``identifier[element],
            # identifier[foo]``. The ``name`` field only sees ``foo``, which
            # would index the type as ``/datum/foo``.
            return "/".join(_text(child, source).strip() for child in identifiers)
    return _path_text(node, source)


def _body_node(node: Node) -> Node | None:
    for field_name in ("body", "block", "definition"):
        child = node.child_by_field_name(field_name)
        if child is not None:
            return child
    for child in _named_children(node):
        if child.type == "block":
            return child
    return None


def _join_path(base: str | None, raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return base or ""
    if raw.startswith("/"):
        return "/" + raw.lstrip("/")
    if base:
        return f"{base.rstrip('/')}/{raw.lstrip('/')}"
    return "/" + raw.lstrip("/")


def _split_owner(path: str, segment: str) -> str | None:
    marker = f"/{segment}/"
    idx = path.find(marker)
    if idx == -1:
        return None
    return path[:idx] or "/"


def _doc_comment(
    source: bytes,
    start_line: int,
    lines: list[str] | None = None,
) -> str | None:
    """The comment block directly above *start_line*, if any.

    Handles `///` runs and `/* ... */` blocks, single- or multi-line. A blank
    line ends the block: a comment separated from the definition is not its
    doc. A trailing `/* ... */` on a code line is not a doc comment either.
    """
    # Decoding the whole file per symbol is quadratic on large files; callers
    # that walk a tree pass the lines they already decoded.
    if lines is None:
        lines = source.decode("utf-8", "replace").splitlines()
    idx = start_line - 2  # 0-based line above the definition
    collected: list[str] = []
    in_block = False
    while idx >= 0:
        stripped = lines[idx].strip()
        if in_block:
            if stripped.startswith("/*"):
                collected.append(stripped[2:].strip())
                break
            collected.append(stripped.lstrip("*").strip())
            idx -= 1
            continue
        if not stripped:
            # The comment must be adjacent; a blank line ends the block.
            break
        if stripped.endswith("*/"):
            body = stripped[:-2].strip()
            if "/*" in body:
                before, _sep, after = body.partition("/*")
                if before.strip():
                    # `code /* note */` is a trailing comment, not a doc.
                    break
                collected.append(after.strip())
                break
            if body:
                collected.append(body.lstrip("*").strip())
            in_block = True
            idx -= 1
            continue
        if stripped.startswith("//"):
            collected.append(stripped.lstrip("/").strip())
            idx -= 1
            continue
        break
    if not collected:
        return None
    return "\n".join(reversed(collected)).strip() or None


def _flags(node: Node, source: bytes) -> str:
    text = _text(node, source)
    head = text.split("(", 1)[0]
    flags: list[str] = []
    if re.search(r"\boverride\b", head):
        flags.append("override")
    if re.search(r"\bstatic\b", head):
        flags.append("static")
    if re.search(r"\babstract\b", head):
        flags.append("abstract")
    return ",".join(flags)


def _signature(node: Node, source: bytes, body: Node | None, path: str) -> str:
    end = body.start_byte if body is not None else node.end_byte
    head = source[node.start_byte:end].decode("utf-8", "replace")
    idx = head.find("(")
    if idx == -1:
        return path
    params = head[idx:].strip()
    # Collapse multi-line parameter lists.
    params = re.sub(r"\s+", " ", params)
    return f"{path}{params}"


def _is_prefix_form(node: Node, source: bytes) -> bool:
    """True for ``mob/Destroy()`` style nodes that declare a member on a type."""
    path_node = _path_node(node)
    if path_node is None:
        return False
    for child in _named_children(node):
        if child.type in MEMBER_NODES and child.start_point[0] == path_node.start_point[0]:
            return True
    return False


def _find_descendant(node: Node, types: set[str]) -> Node | None:
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in types:
            return current
        stack.extend(reversed(current.children))
    return None


def _var_name(node: Node, source: bytes) -> str | None:
    assign = _find_descendant(node, ASSIGN_NODES)
    if assign is not None:
        ident = _first_child_of_type(assign, {"identifier"})
        if ident is not None:
            return _text(ident, source).strip()
    path_node = _path_node(node)
    if path_node is not None:
        text = _text(path_node, source).strip()
        return text.rsplit("/", 1)[-1]
    return None


def _declaration_start(node: Node, source: bytes) -> int:
    """First byte of the type path a node belongs to.

    The grammar splits ``/obj/a/b`` into a chain of nodes, each owning one
    segment, and leaves the leading ``/`` as a sibling token. A symbol span has
    to cover the whole declaration: slicing from the node's own start yields
    ``b`` instead of ``/obj/a/b``, which turns an inserted subtype into a
    member of its parent.
    """
    start = node.start_byte
    if start > 0 and source[start - 1 : start] == b"/":
        return start - 1
    return start


def _walk(
    node: Node,
    source: bytes,
    current_type: str | None,
    out: ParsedFile,
    proc_paths: dict[int, str],
    lines: list[str],
    depth: int = 0,
    in_proc: bool = False,
    decl_start: int | None = None,
) -> None:
    if depth > 32:
        return

    for child in _named_children(node):
        ntype = child.type

        if ntype in TYPE_NODES:
            # A type cannot be declared inside a proc body; if the grammar ever
            # produces one there, indexing it under the proc's owner would
            # invent a path.
            if in_proc:
                continue
            raw = _type_path_text(child, source)
            if not raw:
                continue
            path = _join_path(current_type, raw)
            prefix = _is_prefix_form(child, source)
            body = _body_node(child)
            # Type bodies are not wrapped in a `block` node: their members are
            # direct children of the type node. Capture the var declarations so
            # type-level defaults are searchable and comparable across forks.
            body_text = _text(body, source) if body else _type_body_text(child, source)

            # Every node in a path chain starts at its own segment; a
            # declaration's span starts at the first one (and at the `/`
            # before it for an absolute path).
            start = (
                decl_start
                if decl_start is not None
                and decl_start < child.start_byte
                and node.start_point[0] == child.start_point[0]
                else _declaration_start(child, source)
            )

            # `/obj/item/thing` is a chain of nested nodes. The outermost one
            # only holds the root segment (`obj`); emitting it would add a
            # bogus `/obj` symbol for every declaration in the repository.
            # Intermediate levels are real implicit types in DM, so they stay.
            is_root_segment = child.type == "type_definition" and (
                _first_child_of_type(child, {"subtype_definition"}) is not None
            )

            if not prefix and not is_root_segment:
                symbol = ParsedSymbol(
                    kind="type",
                    path=path,
                    name=path.rstrip("/").rsplit("/", 1)[-1] or path,
                    parent=current_type,
                    signature=None,
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                    start_byte=start,
                    end_byte=child.end_byte,
                    doc=_doc_comment(source, child.start_point[0] + 1, lines),
                    body=body_text,
                    node=child,
                )
                symbol.body_hash = hashlib.sha1(
                    symbol.body.encode("utf-8", "replace")
                ).hexdigest()
                out.symbols.append(symbol)

                explicit = _explicit_parent(child, source)
                if explicit:
                    out.inherits.append((path, explicit, 1))
                else:
                    parent_path = path.rstrip("/").rsplit("/", 1)[0]
                    if parent_path and parent_path != path:
                        out.inherits.append((path, parent_path, 0))

            _walk(
                child,
                source,
                path,
                out,
                proc_paths,
                lines,
                depth + 1,
                in_proc,
                start,
            )
            continue

        if ntype in PROC_NODES:
            if in_proc:
                # DM has no nested procs; a proc node inside a body is a
                # grammar artefact, not a declaration.
                continue
            raw = _path_text(child, source)
            if not raw:
                continue
            name = raw.rsplit("/", 1)[-1]
            name = name.split("(", 1)[0].strip()
            if not name:
                continue
            if current_type:
                path = _join_path(current_type, f"{PROC_SEGMENT}/{name}")
            else:
                path = f"/{PROC_SEGMENT}/{name}"
            body = _body_node(child)
            symbol = ParsedSymbol(
                kind="proc",
                path=path,
                name=name,
                parent=_split_owner(path, PROC_SEGMENT) or current_type,
                signature=_signature(child, source, body, path),
                start_line=child.start_point[0] + 1,
                end_line=child.end_point[0] + 1,
                start_byte=child.start_byte,
                end_byte=child.end_byte,
                flags=_flags(child, source),
                doc=_doc_comment(source, child.start_point[0] + 1, lines),
                body=_text(body, source) if body else "",
                node=child,
            )
            symbol.body_hash = hashlib.sha1(
                symbol.body.encode("utf-8", "replace")
            ).hexdigest()
            out.symbols.append(symbol)
            proc_paths[child.start_byte] = path
            out.params.extend(_extract_params(child, source, path))
            if body is not None:
                out.locals.extend(_extract_locals(body, source, path))
            _walk(child, source, current_type, out, proc_paths, lines, depth + 1, True)
            continue

        if ntype in VAR_NODES:
            # A `var/...` inside a proc body is a local. `_extract_locals`
            # records it against its proc; indexing it as a type member would
            # put every local variable on the owner type.
            if in_proc:
                continue
            name = _var_name(child, source)
            if not name:
                continue
            path = _join_path(current_type, f"{VAR_SEGMENT}/{name}")
            out.symbols.append(
                ParsedSymbol(
                    kind="var",
                    path=path,
                    name=name,
                    parent=_split_owner(path, VAR_SEGMENT) or current_type,
                    signature=_text(child, source).strip().split("\n", 1)[0][:240],
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                    start_byte=child.start_byte,
                    end_byte=child.end_byte,
                    flags=_flags(child, source),
                    doc=_doc_comment(source, child.start_point[0] + 1, lines),
                )
            )
            continue

        if ntype in ASSIGN_NODES and current_type:
            if in_proc:
                continue
            ident = _first_child_of_type(child, {"identifier"})
            if ident is None:
                continue
            name = _text(ident, source).strip()
            path = _join_path(current_type, f"{VAR_SEGMENT}/{name}")
            out.symbols.append(
                ParsedSymbol(
                    kind="var",
                    path=path,
                    name=name,
                    parent=current_type,
                    signature=_text(child, source).strip().split("\n", 1)[0][:240],
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                    start_byte=child.start_byte,
                    end_byte=child.end_byte,
                    flags="assignment",
                )
            )
            continue

        if ntype in VERB_NODES:
            if in_proc:
                continue
            raw = _path_text(child, source)
            if not raw:
                continue
            name = raw.rsplit("/", 1)[-1].split("(", 1)[0].strip()
            path = _join_path(current_type, f"{VERB_SEGMENT}/{name}")
            body = _body_node(child)
            out.symbols.append(
                ParsedSymbol(
                    kind="verb",
                    path=path,
                    name=name,
                    parent=_split_owner(path, VERB_SEGMENT) or current_type,
                    signature=_signature(child, source, body, path),
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                    start_byte=child.start_byte,
                    end_byte=child.end_byte,
                    doc=_doc_comment(source, child.start_point[0] + 1, lines),
                    body=_text(body, source) if body else "",
                )
            )
            continue

        if ntype in INCLUDE_NODES:
            match = re.search(r'[<"]([^>"]+)[>"]', _text(child, source))
            if match:
                out.includes.append(match.group(1))
            continue

        if ntype == "preproc_call":
            # A macro invocation with a body: `SUBSYSTEM_DEF(x)` followed by
            # its assignments, `ADMIN_VERB(...)` followed by its body. The
            # grammar puts that body in a `block` child. Without descending
            # into it, every `#define` and declaration after a multi-line
            # macro call is lost — `code/__DEFINES/food.dm` lost 261 of its
            # 285 lines to one `DEFINE_BITFIELD(...)` call.
            block = _first_child_of_type(child, {"block"})
            if block is not None:
                # The body behaves like a proc body: bare vars and assignments
                # there belong to the macro expansion, not to the enclosing
                # type, so they must not become `/var/name` symbols.
                _walk(block, source, current_type, out, proc_paths, lines, depth + 1, True)
            continue

        if ntype == "ERROR":
            # A mis-parsed region still contains real nodes: `#define` lines
            # around a construct the grammar cannot handle end up as
            # `preproc_def` children of an ERROR node. Descending with
            # `in_proc=True` recovers the defines without indexing the
            # fragments as types, procs or vars.
            _walk(child, source, current_type, out, proc_paths, lines, depth + 1, True)
            continue

        if ntype in {"block", "preproc_if", "preproc_ifdef", "preproc_else", "preproc_elif"}:
            _walk(child, source, current_type, out, proc_paths, lines, depth + 1, in_proc)


def _blank_block_comments(text: str) -> list[str]:
    """Return *text* as lines with block comments replaced by nothing.

    Line numbering is preserved so a recovered define keeps its real line.
    Quotes are tracked so a `/*` inside a string literal does not start a
    comment; line comments need no handling, because a directive cannot start
    after `//`.
    """
    out: list[str] = []
    in_block = False
    for line in text.splitlines():
        pieces: list[str] = []
        index = 0
        quote: str | None = None
        while index < len(line):
            char = line[index]
            if in_block:
                end = line.find("*/", index)
                if end == -1:
                    index = len(line)
                else:
                    in_block = False
                    index = end + 2
                continue
            if quote is not None:
                pieces.append(char)
                if char == "\\" and index + 1 < len(line):
                    pieces.append(line[index + 1])
                    index += 2
                    continue
                if char == quote:
                    quote = None
                index += 1
                continue
            if char in {'"', "'"}:
                quote = char
                pieces.append(char)
                index += 1
                continue
            if line.startswith("/*", index):
                in_block = True
                index += 2
                continue
            pieces.append(char)
            index += 1
        out.append("".join(pieces))
    return out


def _scan_defines(text: str) -> list[tuple[str, str | None, int]]:
    """Collect `#define` directives from the text.

    The grammar is not a reliable source for these: it nests consecutive
    defines inside each other, drops defines that sit inside a mis-parsed
    expression, and turns some multi-line defines into ERROR nodes. On a 7.5k
    file fork that lost 445 of 6,857 defines, including `IS_EDIBLE`,
    `MC_AVERAGE` and `QUEUE_SMOOTH`. `#define` is line-oriented, so a text scan
    is exact; block comments are blanked first so a commented-out define is not
    indexed, and a trailing `\\` joins the continuation lines.
    """
    lines = _blank_block_comments(text)
    defines: list[tuple[str, str | None, int]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        match = _DEFINE_LINE.match(line)
        if match is None:
            index += 1
            continue
        name = match.group(1)
        params = match.group(3)
        value = match.group(4).rstrip()
        line_number = index + 1
        # A `\` continuation belongs to the same directive, even when the
        # following line starts with `#define`.
        while value.endswith("\\") and index + 1 < len(lines):
            index += 1
            value = value[:-1].rstrip() + " " + lines[index].strip()
        value = value.strip() or None
        if params is not None:
            value = f"({params}) {value or ''}".strip()
        defines.append((name, value, line_number))
        index += 1
    return defines


def _type_body_text(node: Node, source: bytes) -> str:
    """Type-level members only: var declarations and ``parent_type``.

    Nested procs and subtypes have their own symbols, so including their text
    here would duplicate the whole file under every ancestor type.
    """
    parts: list[str] = []
    for child in _named_children(node):
        if child.type in {"var_definition", "var_assignment"}:
            parts.append(_text(child, source).strip())
    return "\n".join(part for part in parts if part)


def _explicit_parent(node: Node, source: bytes) -> str | None:
    """Read ``parent_type = /obj/base`` from a type's direct members."""
    for child in _named_children(node):
        if child.type != "var_assignment":
            continue
        ident = _first_child_of_type(child, {"identifier"})
        if ident is None or _text(ident, source).strip() != "parent_type":
            continue
        literal = _first_child_of_type(child, {"type_literal"})
        if literal is not None:
            return _text(literal, source).strip()
    return None


def _extract_params(node: Node, source: bytes, proc_path: str) -> list[tuple[str, str, str | None, int]]:
    """Extract typed proc parameters: (proc_path, name, declared_type, position)."""
    rows: list[tuple[str, str, str | None, int]] = []
    position = 0
    for child in _named_children(node):
        if child.type != "proc_parameter":
            continue
        position += 1
        inline = _first_child_of_type(child, {"inline_var_definition"})
        target = inline if inline is not None else child
        type_node = _first_child_of_type(target, {"type"})
        name: str | None = None
        declared: str | None = None
        if type_node is not None:
            text = _text(type_node, source).strip()
            head, sep, tail = text.rpartition("/")
            if sep and tail:
                declared = "/" + head.lstrip("/")
                name = tail
            else:
                name = text
        else:
            ident = _first_child_of_type(target, {"identifier"})
            if ident is not None:
                name = _text(ident, source).strip()
        if name:
            rows.append((proc_path, name, declared, position))
    return rows


def _type_from_var_type(node: Node, source: bytes, name: str) -> str | None:
    """Extract ``/mob/living`` from a local ``var/mob/living/target`` node.

    A modifier wraps the type: ``var/static/mob/living/target`` is
    ``var_definition -> var_modifier -> var_type``, so the type has to be
    searched for rather than read from the direct child.
    """
    var_type = _find_descendant(node, {"var_type"})
    if var_type is None:
        return None
    assign = _find_descendant(var_type, ASSIGN_NODES)
    end = assign.start_byte if assign is not None else var_type.end_byte
    text = source[var_type.start_byte:end].decode("utf-8", "replace").strip().rstrip("/")
    if not text or text == name:
        return None
    if text.endswith("/" + name):
        text = text[: -(len(name) + 1)]
    if not text:
        return None
    return "/" + text.lstrip("/")


def _extract_locals(body: Node, source: bytes, proc_path: str) -> list[tuple[str, str, str | None, int]]:
    """Collect typed local variables and ``for(var/type/name in ...)`` bindings."""
    rows: list[tuple[str, str, str | None, int]] = []
    stack: list[Node] = [body]
    while stack:
        node = stack.pop()
        if node.type == "var_definition":
            name = _var_name(node, source)
            if name:
                rows.append(
                    (
                        proc_path,
                        name,
                        _type_from_var_type(node, source, name),
                        node.start_point[0] + 1,
                    )
                )
        elif node.type == "inline_var_definition":
            type_node = _first_child_of_type(node, {"type"})
            if type_node is not None:
                text = _text(type_node, source).strip()
                head, sep, tail = text.rpartition("/")
                if sep and tail:
                    rows.append((proc_path, tail, "/" + head.lstrip("/"), node.start_point[0] + 1))
        stack.extend(node.children)
    return rows


def _strip_call_args(text: str) -> str:
    """Drop ``(...)`` groups from a call target.

    A chained call is nested: ``user.get_item().attack_self()`` has the inner
    call inside the outer target, so the raw text would be
    ``user.get_item().attack_self``. Removing the arguments keeps the path
    segments and turns ``MACRO(arg)`` into the macro name it really is.
    """
    out: list[str] = []
    depth = 0
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(char)
    return "".join(out).strip()


def _call_target(node: Node, source: bytes) -> str | None:
    children = _named_children(node)
    if not children:
        return None
    first = children[0]
    if first.type == "argument_list":
        return None
    return _strip_call_args(_text(first, source)) or None


def collect_calls(root: Node, source: bytes, proc_paths: dict[int, str], out: ParsedFile) -> None:
    """Single pass over the tree; each call is attributed to its enclosing proc."""
    stack: list[Node] = [root]
    seen: set[tuple[str, int, int, str | None]] = set()

    def add(target: str, kind: str, node: Node) -> None:
        line = node.start_point[0] + 1
        col = node.start_point[1] + 1
        caller_path = _enclosing_proc(node, proc_paths)
        key = (target, line, col, caller_path)
        # A call on a call result (`call(src, "x")(1)`) nests two call nodes at
        # the same position; the normalised target is identical.
        if key in seen:
            return
        seen.add(key)
        out.calls.append(
            ParsedCall(
                callee_raw=target,
                kind=kind,
                line=line,
                col=col,
                caller_path=caller_path,
            )
        )

    while stack:
        node = stack.pop()
        if node.type in CALL_NODES:
            target = _call_target(node, source)
            if target:
                add(target, "call", node)
        elif node.type in NEW_NODES:
            literal = _first_child_of_type(node, {"type_literal"})
            if literal is not None:
                add(_text(literal, source).strip(), "new", node)
        stack.extend(node.children)


def _enclosing_proc(node: Node, proc_paths: dict[int, str]) -> str | None:
    current = node.parent
    while current is not None:
        path = proc_paths.get(current.start_byte)
        if path:
            return path
        current = current.parent
    return None


def parse_source(source: bytes, rel: str = "<memory>") -> ParsedFile:
    """Parse DM source that has no file on disk.

    `ci` uses this on the old side of a deletion: the diff carries the text, but
    the file is gone, so there is nothing to read.
    """
    parsed = ParsedFile(path=rel, source=source)
    try:
        tree = make_parser().parse(source)
        proc_paths: dict[int, str] = {}
        text = source.decode("utf-8", "replace")
        lines = text.splitlines()
        _walk(tree.root_node, source, None, parsed, proc_paths, lines)
        # `#define` is line-oriented and the grammar drops some of them, so the
        # text scan is the authoritative source.
        parsed.defines = _scan_defines(text)
        collect_calls(tree.root_node, source, proc_paths, parsed)
    except Exception as exc:  # pragma: no cover - defensive
        parsed.error = f"{type(exc).__name__}: {exc}"
    return parsed


def parse_file(path: Path, root: Path) -> ParsedFile:
    source = path.read_bytes()
    rel = path.relative_to(root).as_posix()
    return parse_source(source, rel)
