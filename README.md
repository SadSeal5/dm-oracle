# dm-oracle

A codebase oracle for **BYOND / DreamMaker** repositories — the language SS13
forks are written in. It parses every `.dm` file, builds a symbol index and a
call graph, resolves virtual dispatch across type paths, ranks results with
local embeddings, and packs token-budgeted context bundles for LLMs. It also
speaks MCP, so an agent can query it directly.

Built for codebases where `grep` gives up: 7,500 files, 1.25M lines of DM,
65k procs and 350k call sites.
---

## Why this exists

DreamMaker has no namespaces, no imports, and no static types on calls.
A bare `attack_self()` inside `/obj/item/weapon/sword` means "look up
`attack_self` on my type, then my ancestors, then globally". A proc can be
overridden in a dozen files. `..()` calls the parent implementation. The
preprocessor is everywhere.

That makes ordinary code intelligence useless:

* no reliable "go to definition"
* no call graph
* no way to answer "what breaks if I change this?"
* no way to give an LLM the *right* 20 files instead of the whole repo

`dm-oracle` answers those questions from the shipped source, with a
tree-sitter grammar (SpacemanDMM's) instead of regexes.

---

## Quickstart

```bash
pip install -e .

# If the `oracle` script is not on PATH, use `python -m oracle` instead.
oracle --help

# Point REPO at any folder containing .dm files.
REPO=/path/to/ss13-fork

# Index a fork (≈72s for 1.25M lines)
oracle -r "$REPO" index

oracle -r "$REPO" stats
oracle -r "$REPO" find attack_self
oracle -r "$REPO" show /obj/item/proc/attack_self
oracle -r "$REPO" callers /obj/item/proc/attack_self
oracle -r "$REPO" impact /obj/item/proc/attack_self --depth 3
oracle -r "$REPO" context "supermatter delaminate" --budget 6000

# Optional: local embeddings for natural-language queries (≈8s, CPU only)
pip install -e ".[embeddings]"
oracle -r "$REPO" embed
oracle -r "$REPO" search "how does being hit work" --semantic
```

On PowerShell, set `$REPO = "C:\path\to\ss13-fork"` instead.

The index lives in `<repo>/.oracle/oracle.db` (SQLite + FTS5). Nothing leaves
the machine.

---

## What it indexes

For a 1.25M-line SS13 fork:

| metric | value |
| --- | --- |
| files | 7,586 |
| lines of DM | 1,254,686 |
| symbols | 358,750 |
| types | 40,724 |
| procs | 65,490 |
| vars | 252,532 |
| call edges | 351,509 |
| `#define`s | 20,106 |
| inheritance declarations | 57,841 |
| embeddings | 106,233 |
| index time | ~98 s |
| embed time | ~8 s |

Call resolution after the full pipeline:

| resolution | count | meaning |
| --- | --- | --- |
| `macro` | 82,908 | `#define` expansion, not a proc |
| `builtin` | 79,948 | engine builtin (`istype`, `min`, …) |
| `receiver` | 38,313 | inferred from `X.foo()` |
| `global` | 35,400 | `/proc/name` |
| `inherited` | 33,695 | found on an ancestor type |
| `exact` | 23,621 | exact path or own type |
| `unresolved` | 20,523 | likely external or dynamic |
| `ambiguous` | 17,576 | multiple candidates, no safe answer |
| `virtual` | 10,400 | nearest common ancestor of all overrides |
| `type` | 5,146 | `new /obj/...` instantiation |
| `unique` | 3,201 | single candidate |
| `file-local` | 760 | single candidate in the same file |
| `declared` | 18 | declared but not defined |

`macro` and `builtin` are deliberately *not* treated as failures — they are
most of the noise in a naive call graph.

---

## Commands

### `index`

```bash
oracle -r <repo> index [--force] [--quiet]
```

Incremental: unchanged files are skipped by SHA-1. `--force` rebuilds from
scratch.

### `stats`

Inventory plus the largest types by proc count, symbol kinds and resolution
breakdown.

### `find` / `search`

```bash
oracle -r <repo> find attack_self          # path substring, exact names first
oracle -r <repo> search "explosion damage" # FTS5 over path/name/doc/body
oracle -r <repo> search "how does being hit work" --semantic
```

`--semantic` blends FTS with vector neighbours (see `embed`). It is
confidence-gated: when the top lexical hit is a proc whose name or path
segments cover the whole query, the lexical answer is trusted unchanged. Only
queries the ranker is unsure about get a second opinion.

### `embed`

```bash
oracle -r <repo> embed [--model minishlab/potion-base-8M] [--limit N] [--quiet]
```

Builds local embeddings for every proc and type from their path, doc comment,
signature and body. Uses [model2vec](https://github.com/MinishLab/model2vec)
static embeddings: 256 dimensions, CPU only, no torch, ~8 s for 106k symbols.
Vectors are stored in the same SQLite file and searched with a brute-force
cosine scan (~10 ms per query).

Re-running with a different `--model` replaces the vectors; the model name is
recorded in `meta` so queries use the right one.

### `mcp`

```bash
oracle -r <repo> mcp                 # stdio transport
oracle -r <repo> mcp --print-config  # JSON snippet for an MCP client
```

Exposes nine tools over the Model Context Protocol: `oracle_stats`,
`oracle_find`, `oracle_search`, `oracle_show`, `oracle_members`,
`oracle_callers`, `oracle_callees`, `oracle_impact`, `oracle_context`.
Structured tools return JSON; `oracle_context` returns a Markdown bundle.

```json
{
  "mcpServers": {
    "dm-oracle": {
      "command": "python",
      "args": ["-m", "oracle", "-r", "/path/to/ss13-fork", "mcp"]
    }
  }
}
```

### `show` / `members` / `outline`

```bash
oracle -r <repo> show /obj/item/proc/attack_self
oracle -r <repo> members /obj/item
oracle -r <repo> outline code/game/objects/items.dm
```

`show` prints the source span from the file, with line numbers.

### `callers` / `callees` / `impact`

```bash
oracle -r <repo> callers /obj/item/proc/attack_self
oracle -r <repo> callees /obj/item/proc/attack_self
oracle -r <repo> impact  /obj/item/proc/attack_self --depth 3
```

`impact` is a transitive reverse-dependency tree: every proc that would need
review if the target changes.

### `diff` / `port`

Compare this fork with another fork — the porting workflow.

```bash
oracle -r <repo> diff /path/to/other-fork                 # port candidates
oracle -r <repo> diff /path/to/other-fork --only changed  # diverged definitions
oracle -r <repo> diff /path/to/other-fork --kind proc --limit 100
oracle -r <repo> port /obj/item/proc/some_proc --from /path/to/other-fork
```

`diff` compares canonical paths and answers four questions at once: symbols
only here, only there, defined on both sides with a different signature or
body, and **renamed** — a path that only exists on one side paired with a path
that only exists on the other when the body is byte-identical. A rename keeps
the body and changes the name, so the signature is useless as a key; the body
hash is the only thing that survives. Pairs are only claimed when the match is
unique on both sides and the body spans at least three lines, which keeps the
trivial `return` procs from pairing with each other. `--only missing` (the
default) is the port candidate list; `--only renamed` is the move list.

`port` shows the upstream source, whether the target already has the symbol,
whether the signature or body diverged, whether the owner type exists at all,
and **call sites on the same type family** in the target — the places that
would start dispatching to the ported proc. Bare name matches are filtered out;
otherwise every `turn_on()` in the tree would match, which is noise.

Both commands need the other fork indexed first
(`oracle -r /path/to/other-fork index`).

Real numbers, one 1.25M-line fork against another: 64,037 shared procs, 4,463
changed, 3,079 only upstream, 1,070 only here, 16 renamed, in about 2 s. The
renames are real refactors — a light overlay component becoming a datum, a
keybinding datum renamed, a tongue organ moved under `/fish/`.

### `ci`

Change impact for CI: given a diff, list the symbols that changed, everything
downstream of them, and the unit tests that exercise them.

```bash
oracle -r <repo> ci                       # working tree vs HEAD
oracle -r <repo> ci --base origin/main    # branch vs upstream
oracle -r <repo> ci --staged              # pre-commit hook
oracle -r <repo> ci --diff change.patch   # pre-generated patch, or '-' for stdin
oracle -r <repo> ci --base-rev origin/main  # name procs removed from surviving files
oracle -r <repo> ci --fail-on-affected --json
```

It parses the unified diff, maps every hunk to the symbols whose line span it
intersects, expands those along the reverse call graph, and suggests tests:
first unit tests that call an affected proc, then unit tests whose body
mentions a changed file.

A **deleted file** has no new side to anchor to, so the diff's removed lines are
parsed back into symbols and every surviving call site that used their name is
reported as first-order breakage. After a re-index those call edges are already
`unresolved` with a null target, so the raw name is matched literally — and for
a member proc restricted to callers on the owner type or one of its
descendants, otherwise every `turn_on()` in the tree would be blamed.

A proc removed from a file that **still exists** is the same problem in
miniature: `--unified=0` carries no context, so the hunk cannot say what the old
file declared. `--base-rev <git-ref>` asks git for the whole old file
(`git show <ref>:<path>`) and diffs its symbols against the index, which names
the removed proc and its call sites. Without it the deletion is still
attributed to the enclosing symbol, but the proc itself cannot be reported.

```
1 changed .dm file(s), 0 changed symbol(s), 1 removed symbol(s), 2 affected downstream
removed:  /proc/explosion (2 call sites)
affected: /datum/component/explodable/proc/detonate — because of /proc/explosion
```

`--fail-on-affected` exits 1 when anything downstream changed, which is what a
CI gate wants. `--depth 0` reports only the changed and removed symbols, with
no downstream walk.

The index must describe the new side of the diff (`oracle index` before `ci`),
which is the normal order in a pipeline.

In GitHub Actions:

```yaml
- uses: actions/checkout@v4
  with: { fetch-depth: 0 }
- run: pip install -e .
- run: oracle -r . index --quiet
- run: |
    BASE="origin/${{ github.base_ref }}"
    oracle -r . ci --base "$BASE" --base-rev "$BASE" --fail-on-affected
```

`--base` decides what to diff; `--base-rev` decides where the old file text
comes from. They are usually the same ref.

### `context` / `symbol-context`

The LLM-facing part. Produces a Markdown bundle with an index, file:line
anchors and fenced source, capped by an approximate token budget.

```bash
oracle -r <repo> context "supermatter delaminate" --budget 12000 -o ctx.md
oracle -r <repo> context "how does being hit work" --semantic --budget 8000
oracle -r <repo> symbol-context /obj/item/proc/attack_self --budget 8000
```

A real bundle for `"supermatter delaminate"` (budget 6,000) contains 22
symbols and ~5,600 tokens: the base `delaminate` proc, every delamination
subtype, their callees, and the callers that trigger them. That is the whole
answer, with file:line anchors, instead of 7,500 files.

Seeds come from a hybrid retriever (path search + underscore-joined search +
FTS, optionally fused with vector neighbours), then the bundle expands along
call edges (callees and callers), dedupes and stops at the budget.

### `eval`

A gold set of questions with expected symbol paths, scored with recall@k, MRR
and top-1.

```bash
oracle -r <repo> eval --file evals/ss13.json --k 5
oracle -r <repo> eval --semantic
oracle -r <repo> eval --compare   # lexical vs semantic, side by side
oracle -r <repo> eval --json
```

Current result on that fork:

```
lexical   recall@5 94%   MRR 0.941   top-1 94%   (16/17)
semantic  recall@5 94%   MRR 0.941   top-1 94%   (16/17)
```

The confidence gate is what keeps semantic from degrading lexical: pure vector
ranking alone scored `MRR 0.668 · top-1 53%` on this set, because DM symbol
names (`attack_self`, `delaminate`) are already excellent lexical signals.

The one remaining miss is `"explosion damage"` → `/atom/proc/ex_act`. Both
rankers prefer `/datum/corpse_damage/cause_of_death/explosion`; the vector
model puts `ex_act` outside its top 20. Natural-language phrasings do improve —
`"how does the engine handle being hit by a projectile"` returns
`/obj/projectile/proc/on_hit` first.

---

## How resolution works

Given `callee_raw`, the caller's proc path and the caller's file:

1. **Exact path** — `/obj/item/proc/attack` exists.
2. **Parent call** — `..()` resolves to the same proc name on the parent type.
3. **Implicit `src`** — a bare `foo()` walks the caller's type chain.
4. **Receiver inference** — `X.foo()` uses the declared type of `X`, where `X`
   is a proc parameter (`mob/user`), a type-level var (`var/obj/item/held`), a
   local (`var/mob/living/target = user`, `for(var/mob/M in ...)`), or a
   builtin (`usr`, `src`, `client`, `loc`).
5. **Global** — `/proc/foo`.
6. **Macro / builtin** — recorded, but not linked as a proc edge.
7. **Unique / file-local** — a single candidate.
8. **Virtual base** — if all candidates are overrides, link to the deepest
   common ancestor that defines the proc.
9. **Ambiguous / unresolved** — left honest instead of guessed.

The resolver never invents an edge it cannot justify; `ambiguous` is a
first-class outcome.

---

## Architecture

```
oracle/
  config.py      repo discovery, .dm walk, skip rules
  parser.py      tree-sitter-dm → symbols, calls, params, locals, defines, inherits
  store.py       SQLite schema (files, symbols, calls, inherits, defines, params,
                 locals, embeddings, FTS5)
  indexer.py     incremental orchestration, type dedupe, progress
  resolve.py     call-target resolution + receiver/type inference
  query.py       find / search / hybrid search / callers / impact
  embed.py       model2vec embeddings, cosine scan, weighted RRF fusion
  multi.py       cross-fork diff and port reports
  ci.py          diff → changed symbols → impact → test suggestions
  context.py     token-budgeted context bundles
  eval.py        retrieval gold set + metrics
  mcp_server.py  MCP tools over the same query layer
  cli.py         Typer + Rich CLI
```

Data model:

* **files** — path, LOC, SHA-1, mtime.
* **symbols** — `type`, `proc`, `var`, `verb`; canonical path, owner, signature,
  line/byte span, flags, doc comment, body hash. A type's body is its direct
  `var` declarations, so type-level defaults are searchable and comparable.
* **calls** — caller symbol, raw callee, resolved path, resolution kind, line.
* **inherits** — child → parent, explicit (`parent_type`) or implicit (path
  prefix), tagged with the declaring file so incremental re-indexing can drop
  exactly the edges a file owned.
* **defines** — `#define` name/value.
* **params** — typed proc parameters, used for receiver inference.
* **locals** — typed local vars and `for(var/type/name in ...)` bindings,
  scoped to the enclosing proc path.
* **embeddings** — one float32 vector per proc/type, keyed by symbol id, with
  the model name and dimension.
* **symbols_fts** — FTS5 over path, name, signature, doc and body.

`type_definition` / `subtype_definition` nodes declare one path segment each.
The outermost node only carries the root segment (`obj`), so it is skipped;
the remaining segments still produce implied type declarations, and the
indexer keeps the largest declaration per path.

Schema v5 adds `file_id` to `inherits`, `params` and `locals` so a re-index
replaces exactly the rows a file used to own instead of appending duplicates.
An index built by an older version is refused with a `--force` hint rather than
silently mis-queried.

---

## Limitations

* **Dynamic dispatch is approximated.** `call(target, "foo")()` and
  `call(src, proc_name)` are not resolved.
* **Local variable types are best-effort.** Explicit `var/type/name` and
  `for(var/type/name in ...)` are captured; a bare `var/x = some_call()` stays
  untyped, so `x.foo()` falls back to the other strategies.
* **Preprocessor conditionals** are walked, but inactive branches are still
  indexed (DM has no reliable way to know which `#if` is live without the
  full define set of the build).
* **Semantic search is conservative.** Vectors only rerank when the lexical
  ranker is unsure, and the fusion weights lexical 2:1. That protects precise
  symbol lookups but means a bad lexical top hit is rarely overturned.
* **The embedding model is general-purpose English.** It knows "projectile"
  and "airlock", not BYOND jargon like `ex_act` or `delaminate`; DM-specific
  vocabulary would need fine-tuning.
* **Cross-fork comparison is path-based.** A rename is now reported when the
  body is byte-identical and the match is unique, but a rename *with* edits
  still shows up as one missing plus one extra.
* **`ci` needs the index to match the new side of the diff.** It maps hunks to
  symbols by line number, so re-index before running it in a pipeline.
* **`ci` can only reconstruct a fully deleted file from the diff alone.** The
  old side of a partial deletion is not available with `--unified=0`, so pass
  `--base-rev <ref>` to read the whole old file from git; without it a proc
  removed from a surviving file is only attributed when the hunk still lands
  inside it.
* **Deleted symbols are not expanded transitively.** Their call sites are
  reported at depth 1; the graph no longer contains the removed node, so there
  is nothing to walk from it.
* **New overrides are not "affected".** Adding `attack_self` to a new type does
  not change any existing call edge, so the impact list stays empty even though
  behaviour changed. The changed symbol is still reported.
* **Test suggestions are heuristic.** They come from call edges into unit test
  datums plus file-name mentions, not from coverage data.
* **Type bodies cover var declarations only.** Nested procs and subtypes are
  separate symbols, so a type's `body_hash` changes when its defaults change,
  not when a member proc changes.
* **Old indexes must be rebuilt.** The schema is versioned; `index --force` is
  required after an upgrade, and every command says so instead of guessing.
* **Maps, icons and TGUI** are out of scope. This is a code oracle, not an
  asset index.

---

## Roadmap

1. ~~**Vector layer**~~ — done: local model2vec embeddings, confidence-gated
   hybrid retrieval, gold-set comparison.
2. ~~**Local variable typing**~~ — done: `var/type/name` and `for(var/type/X in ...)`
   feed receiver inference.
3. ~~**MCP server**~~ — done: nine tools over stdio.
4. ~~**Multi-repo mode**~~ — done: `diff` and `port` compare two forks'
   indexes, with type-scoped call-site suggestions. Each fork keeps its own
   database because DM paths collide across forks.
5. ~~**Change impact in CI**~~ — done: `oracle ci` maps a diff to changed
   symbols, expands the reverse call graph and suggests unit tests.
6. ~~**Rename detection**~~ — done: `diff` pairs same-body symbols across
   paths, and `ci` recovers the symbols of a deleted file and reports the call
   sites that lost their target. `ci --base-rev` extends that to procs removed
   from files that still exist.

---

## Development

```bash
python -m pip install -e ".[dev]"   # or: pip install pytest
python -m pip install -e ".[all]"   # dev + embeddings + mcp
python -m pytest -q
```

92 tests cover the parser (typed vars, prefix forms, params, locals, call
attribution, explicit `parent_type`, bodyless type paths), the resolver
(inheritance, receiver inference, macros, ambiguity), the query layer (search,
callers, impact, LIKE escaping, empty patterns), the embedding layer (fusion
weights, the confidence gate, cosine lookup), the MCP server (tool registration
and JSON output), cross-fork diff/port (including rename pairing and the
trivial-body guard), CI change impact (including the callers of a deleted
file), incremental indexing (schema guard, stale rows, deleted files), context
budgets, eval-file validation and the CLI's markup-safe output.
