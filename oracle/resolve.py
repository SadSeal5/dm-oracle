"""Call-target resolution for DreamMaker.

DM has no namespaces and relies on type-path inheritance, so resolution is a
best-effort ranking:

1. exact path (``/obj/item/proc/attack``)
2. inherited proc on the caller's type chain (implicit ``src``)
3. receiver type inference for ``X.foo()`` calls
4. global proc (``/proc/foo``)
5. preprocessor macro or engine builtin
6. unique proc name in the repository / file
7. nearest common ancestor of all candidates (virtual dispatch base)
8. unresolved
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass

# DreamMaker engine builtins that look like proc calls.
BUILTINS = {
    "abs", "alert", "animate", "animate_movement", "arccos", "arcsin", "arctan",
    "assoc", "browse", "browse_rsc", "ckey", "ckeyEx", "clamp", "cmptext",
    "cmptextEx", "copytext", "cos", "crash", "dd_hasprefix", "dd_hassuffix",
    "dd_locate", "dd_range", "dd_replacetext", "dd_suffix", "del", "fcopy",
    "fdel", "fexists", "file", "file2text", "filter", "findtext", "findtextEx",
    "flick", "flist", "fs", "ftpsend", "ftpconnect", "generator", "get_dir",
    "get_dist", "get_step", "get_step_away", "get_step_rand", "get_step_to",
    "get_step_towards", "html_decode", "html_encode", "icon", "image", "input",
    "isinf", "islist", "isnull", "isnum", "istext", "istype", "jointext",
    "json_decode", "json_encode", "length", "length_char", "link", "list",
    "list2params", "locate", "log", "lowertext", "matrix", "max", "md5", "min",
    "miss", "num2hex", "num2text", "orange", "oview", "oviewers", "params2list",
    "particles", "pick", "prob", "rand", "rand_seed", "range", "ref", "regex",
    "replacetext", "replacetextEx", "rgb", "rgb2num", "round", "run", "sha1",
    "shell", "shutdown", "sin", "sleep", "sortList", "sorttext", "sound",
    "spawn", "splittext", "sqrt", "step", "step_away", "step_rand", "step_to",
    "step_towards", "tan", "text", "text2num", "text2path", "time2text",
    "trim", "trunc", "turn", "typesof", "uppertext", "url_decode", "url_encode",
    "view", "viewers", "walk", "walk_away", "walk_rand", "walk_to",
    "walk_towards", "winset", "winget", "winshow", "world", "wrap",
}

# Builtin vars with a known static type, used for receiver inference.
BUILTIN_VAR_TYPES = {
    "client": "/client",
    "loc": "/turf",
    "contents": "/list",
    "vars": "/list",
    "type": "/type",
    "parent_type": "/type",
}

_PARENT_CALL = {"..", "."}
_VAR_DECL_RE = re.compile(r"^\s*var\s*/([A-Za-z0-9_/]+)")
# ``var/static/list/things``: the modifier is not part of the type path.
_VAR_MODIFIERS = {"static", "const", "tmp", "global", "final", "atomic"}


@dataclass
class ProcInfo:
    symbol_id: int
    path: str
    name: str
    parent: str | None
    file_id: int


class Resolver:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.procs_by_name: dict[str, list[ProcInfo]] = defaultdict(list)
        self.procs_by_path: dict[str, ProcInfo] = {}
        self.types: set[str] = set()
        self.parents: dict[str, str] = {}
        self.file_procs: dict[int, dict[str, list[ProcInfo]]] = defaultdict(lambda: defaultdict(list))
        self.var_types: dict[str, dict[str, str]] = defaultdict(dict)
        self.params: dict[str, dict[str, str]] = defaultdict(dict)
        self.locals: dict[str, dict[str, str]] = defaultdict(dict)
        self.defines: set[str] = set()
        self._load()

    def _load(self) -> None:
        rows = self.conn.execute(
            "SELECT id, path, name, parent, file_id FROM symbols WHERE kind IN ('proc', 'verb')"
        ).fetchall()
        for row in rows:
            info = ProcInfo(
                symbol_id=int(row["id"]),
                path=row["path"],
                name=row["name"],
                parent=row["parent"],
                file_id=int(row["file_id"]),
            )
            self.procs_by_name[info.name].append(info)
            self.procs_by_path[info.path] = info
            self.file_procs[info.file_id][info.name].append(info)

        for row in self.conn.execute("SELECT path FROM symbols WHERE kind = 'type'"):
            self.types.add(row["path"])

        for row in self.conn.execute("SELECT child, parent FROM inherits ORDER BY explicit, child, parent"):
            self.parents[row["child"]] = row["parent"]

        for row in self.conn.execute(
            "SELECT path, name, signature FROM symbols WHERE kind = 'var'"
        ):
            declared = _declared_type(row["signature"], row["name"])
            if not declared:
                continue
            parent = row["path"].split("/var/", 1)[0] or None
            if parent:
                self.var_types[parent][row["name"]] = declared

        self.defines = {
            row["name"] for row in self.conn.execute("SELECT name FROM defines")
        }

        for row in self.conn.execute("SELECT proc_path, name, type FROM params"):
            if row["type"]:
                self.params[row["proc_path"]][row["name"]] = row["type"]

        for row in self.conn.execute("SELECT proc_path, name, type FROM locals"):
            if row["type"]:
                self.locals[row["proc_path"]][row["name"]] = row["type"]

    def ancestors(self, type_path: str | None) -> list[str]:
        """Return the type and its ancestors, nearest first."""
        chain: list[str] = []
        seen: set[str] = set()
        current = type_path
        while current and current not in seen:
            seen.add(current)
            chain.append(current)
            nxt = self.parents.get(current)
            if nxt is None:
                if current.count("/") <= 1:
                    break
                nxt = current.rstrip("/").rsplit("/", 1)[0] or "/"
                if nxt == "/":
                    break
            current = nxt
        return chain

    def _var_type(self, type_path: str | None, name: str) -> str | None:
        for ancestor in self.ancestors(type_path):
            declared = self.var_types.get(ancestor, {}).get(name)
            if declared:
                return declared
        return BUILTIN_VAR_TYPES.get(name)

    def _infer_receiver(self, receiver: str, caller_path: str | None) -> str | None:
        parts = [part for part in receiver.split(".") if part]
        if not parts:
            return None
        caller = self.procs_by_path.get(caller_path or "")
        owner = caller.parent if caller else None

        head = parts[0]
        if head == "src":
            current = owner
        elif head == "usr":
            current = "/mob"
        elif head == "world":
            current = None
        else:
            current = self.params.get(caller_path or "", {}).get(head)
            if not current:
                current = self.locals.get(caller_path or "", {}).get(head)
            if not current:
                current = self._var_type(owner, head)

        for part in parts[1:]:
            if not current:
                break
            current = self._var_type(current, part)
        return current

    def resolve(self, callee_raw: str, caller_path: str | None, file_id: int | None) -> tuple[str | None, str]:
        raw = callee_raw.strip()
        if not raw:
            return None, "unresolved"

        if raw in _PARENT_CALL:
            if caller_path and caller_path in self.procs_by_path:
                info = self.procs_by_path[caller_path]
                chain = self.ancestors(info.parent)
                if len(chain) > 1:
                    candidate = f"{chain[1]}/proc/{info.name}"
                    if candidate in self.procs_by_path:
                        return candidate, "inherited"
            return None, "unresolved"

        # Explicit absolute path.
        if raw.startswith("/"):
            if raw in self.procs_by_path:
                return raw, "exact"
            if "/proc/" in raw:
                return raw, "declared"
            if raw in self.types:
                return raw, "type"
            name = raw.rstrip("/").rsplit("/", 1)[-1]
            owner = raw[: -(len(name) + 1)] or "/"
            candidate = f"{owner}/proc/{name}"
            if candidate in self.procs_by_path:
                return candidate, "exact"
            return None, "unresolved"

        # Member call: receiver.name(...)
        if "." in raw:
            receiver, _, name = raw.rpartition(".")
            receiver = receiver.strip()
            name = name.strip()
            if receiver.startswith("/"):
                for ancestor in self.ancestors(receiver):
                    candidate = f"{ancestor}/proc/{name}"
                    if candidate in self.procs_by_path:
                        return candidate, "exact" if ancestor == receiver else "inherited"
            else:
                inferred = self._infer_receiver(receiver, caller_path)
                if inferred:
                    for ancestor in self.ancestors(inferred):
                        candidate = f"{ancestor}/proc/{name}"
                        if candidate in self.procs_by_path:
                            return candidate, "receiver"
            return self._by_name(name, caller_path, file_id, allow_unique=False)

        # Bare name: implicit src, then global, then macro/builtin, then heuristics.
        if caller_path:
            caller = self.procs_by_path.get(caller_path)
            owner = caller.parent if caller else None
            for ancestor in self.ancestors(owner):
                candidate = f"{ancestor}/proc/{raw}"
                if candidate in self.procs_by_path:
                    return candidate, "exact" if ancestor == owner else "inherited"

        global_path = f"/proc/{raw}"
        if global_path in self.procs_by_path:
            return global_path, "global"

        if raw in self.defines:
            return None, "macro"
        if raw in BUILTINS:
            return None, "builtin"

        return self._by_name(raw, caller_path, file_id, allow_unique=True)

    def _by_name(
        self,
        name: str,
        caller_path: str | None,
        file_id: int | None,
        allow_unique: bool,
    ) -> tuple[str | None, str]:
        candidates = self.procs_by_name.get(name, [])
        if not candidates:
            return None, "unresolved"

        if file_id is not None:
            local = self.file_procs.get(file_id, {}).get(name, [])
            if len(local) == 1:
                return local[0].path, "file-local"

        if allow_unique and len(candidates) == 1:
            return candidates[0].path, "unique"

        if caller_path:
            caller = self.procs_by_path.get(caller_path)
            if caller and caller.parent:
                for ancestor in self.ancestors(caller.parent):
                    for candidate in candidates:
                        if candidate.parent == ancestor:
                            return candidate.path, "inherited"

        base = self._common_base(name, candidates)
        if base:
            return base, "virtual"

        return None, "ambiguous"

    def _common_base(self, name: str, candidates: list[ProcInfo]) -> str | None:
        chains = [set(self.ancestors(c.parent)) for c in candidates if c.parent]
        if not chains:
            return None
        common = set.intersection(*chains)
        if not common:
            return None
        base = max(common, key=lambda path: (path.count("/"), path))
        candidate = f"{base}/proc/{name}"
        return candidate if candidate in self.procs_by_path else None


def _declared_type(signature: str | None, name: str) -> str | None:
    """Extract ``/mob/living`` from ``var/mob/living/target = null``."""
    if not signature:
        return None
    match = _VAR_DECL_RE.match(signature)
    if not match:
        return None
    parts = [part for part in match.group(1).split("/") if part]
    while parts and parts[0] in _VAR_MODIFIERS:
        parts.pop(0)
    if len(parts) <= 1:
        # Either no type at all (`var/name`) or only modifiers.
        return None
    if parts[-1] == name:
        parts.pop()
    if not parts:
        return None
    return "/" + "/".join(parts)


def resolve_all(conn: sqlite3.Connection) -> dict[str, int]:
    """Resolve every stored call edge. Returns counts by resolution kind."""
    resolver = Resolver(conn)
    rows = conn.execute(
        "SELECT id, callee_raw, caller_symbol_id, file_id FROM calls"
    ).fetchall()

    caller_paths: dict[int, str | None] = {}
    updates: list[tuple[str | None, str, int]] = []
    counts: dict[str, int] = defaultdict(int)

    for row in rows:
        caller_id = row["caller_symbol_id"]
        caller_path = None
        if caller_id is not None:
            if caller_id not in caller_paths:
                symbol = conn.execute("SELECT path FROM symbols WHERE id = ?", (caller_id,)).fetchone()
                caller_paths[caller_id] = symbol["path"] if symbol else None
            caller_path = caller_paths[caller_id]

        path, resolution = resolver.resolve(row["callee_raw"], caller_path, int(row["file_id"]))
        counts[resolution] += 1
        updates.append((path, resolution, int(row["id"])))

    conn.executemany("UPDATE calls SET callee_path = ?, resolution = ? WHERE id = ?", updates)
    conn.commit()
    return dict(counts)
