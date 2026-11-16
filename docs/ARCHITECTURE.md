# Architecture

This document records the parts of DreamMaker that made the design
non-obvious, and the algorithms that follow from them.

## 1. Grammar shape

`tree-sitter-dm` (SpacemanDMM) is permissive and path-oriented. The important
node types:

| node | meaning |
| --- | --- |
| `type_definition` | first segment of a type path (`/obj/...`) |
| `subtype_definition` | a nested segment (`/obj/item/...`) |
| `proc_definition` | `proc/foo(...)` with a body |
| `proc_override` | `foo(...)` inside a type body — no `proc/` prefix |
| `proc_declaration` | `proc/foo(...)` without a body |
| `var_definition` | `var/...` declaration |
| `var_assignment` | `name = value` (type-level default or var name) |
| `var_type` | declared type of a typed var (`mob/living/target`) |
| `preproc_def` / `preproc_function_def` | `#define` |
| `call_expression` | `foo(...)`, `x.foo(...)`, `..()` |
| `new_expression` | `new /obj/item(...)` |
| `field_expression` | `usr.client.foo` |
| `parent_proc` | `..` |

### Prefix forms

`mob/Destroy()` parses as `type_definition(mob)` containing
`proc_override(Destroy)`, with no `subtype_definition`. It declares a proc on
`/mob`, not a new type. Detection: a member child starts on the same source
line as the type path node.

`/obj/item` parses as `type_definition(obj)` → `subtype_definition(item)`.
Both are real type declarations.

### Bodyless types

A type with no members is flattened by the grammar: `/obj/empty` becomes
`type_definition` with two *sibling* identifiers (`obj`, `empty`) and no
`subtype_definition`. The tree-sitter `name` field points at the last
identifier, so reading only that field would index the type as `/empty`. The
parser joins the sibling identifiers instead.

The outermost `type_definition` of a nested path only carries the root segment.
It is skipped during indexing: emitting it would add a bogus `/obj` row for
every declaration in the repository.

### Typed variables

`var/mob/living/target` has no direct `var_assignment` child; the name lives
inside `var_type` → `var_subtype` → `var_assignment`. The parser searches
descendants for the first `var_assignment` and takes its identifier.

### Parameters

`mob/user` becomes `proc_parameter` → `inline_var_definition` → `type`. The
last path segment is the parameter name; the rest is its declared type. These
are stored in `params` and are the main source of receiver types.

### Local variables

Inside a proc body, `var/mob/living/target = user` is a `var_definition` whose
`var_type` node holds the declared path; the initializer is cut off at the
`var_assignment`, so the type is `/mob/living`. A `for(var/mob/M in range(3))`
loop produces an `inline_var_definition` instead, and the same
last-segment-is-the-name rule applies.

Locals are stored per proc path, so they are only visible inside the proc that
declares them — unlike type-level vars, which are inherited. Lookup order is
parameter, then local, then type-level var.

## 2. Canonical paths

Everything is normalized to an absolute DM path:

```
/obj/item                        type
/obj/item/proc/attack_self       proc
/obj/item/var/held               var
/proc/global_helper              global proc
```

A proc declared as `proc/foo()` inside `/obj/item` becomes
`/obj/item/proc/foo`. A top-level `proc/foo()` becomes `/proc/foo`.

## 3. Type dedupe

`/obj/item/foo` re-declares `/obj/item` every time. A 1.25M-line codebase
produces ~212k duplicate type rows for ~41k distinct types. After indexing, a
window function keeps the largest declaration per path:

```sql
ROW_NUMBER() OVER (PARTITION BY path ORDER BY (end_byte - start_byte) DESC, id)
```

and deletes the rest (including their FTS rows). Inheritance declarations are
deduped per file with `MIN(id) GROUP BY file_id, child, parent`: keeping one
edge per file is what lets `delete_file` remove exactly the edges that file
owned without erasing a declaration made elsewhere.

A type's body is captured from its direct `var_definition` / `var_assignment`
children. There is no `block` node for types, and including nested procs would
duplicate the whole subtree under every ancestor.

## 4. Resolution

Inputs: `callee_raw`, `caller_path`, `file_id`.

```
exact path?           → exact
".." / "."?           → same proc on parent type
"/..."?               → type / declared / exact
"receiver.name"?      → infer receiver type, walk ancestors
bare name?            → caller type chain (implicit src)
                      → /proc/name
                      → #define  (macro)
                      → builtin  (builtin)
                      → file-local / unique
                      → common ancestor of candidates (virtual)
                      → ambiguous
```

### Receiver inference

`_infer_receiver` resolves a dot-chain left to right:

1. `src` → caller's owner type.
2. `usr` → `/mob`.
3. parameter of the caller proc → declared type from `params`.
4. local variable in the caller proc → declared type from `locals`.
5. type-level var on the caller's ancestor chain → declared type from `var_type`.
6. builtin var (`client`, `loc`, `contents`, …).

Then `name` is looked up on that type and its ancestors.

### Virtual dispatch

When a bare name has multiple candidates and none is on the caller's chain,
the resolver intersects the ancestor sets of every candidate's owner and
picks the deepest common type that defines the proc. That is the static base
of the virtual call, which is what an impact analysis wants.

Example: `attack_hand` is defined on `/atom`, `/turf`, `/obj/item`,
`/mob/living` …; a call from an unrelated type resolves to
`/atom/proc/attack_hand` with resolution `virtual`.

## 5. Retrieval

`hybrid_search` merges four candidate sources:

1. `find(query)` — path substring, exact names first.
2. `find(tokens_joined_by_underscore)` — `"bullet act"` → `bullet_act`.
3. `find(token)` per token (length ≥ 3).
4. FTS5 `"token1" OR "token2" …` over path/name/signature/doc/body.

Scoring (lower is better):

```
-300  name == query                       (single token)
-300  name == joined                      (single token only)
-25·m² tokens present as path segments
-30   name is one of the query tokens     (when not an exact match)
-80   kind == proc
-20   kind == type
-20…  FTS rank bonus, only when no path token matched
+0.05·len(path)
```

Path tokens are split on `/` and `_`, so `damages` does not match `damage`.
The FTS bonus is reserved for candidates that path search could not justify —
that is what lets body-text matches compete with literal path matches.

### Vector layer

`embed.py` builds one vector per proc and type from path, doc comment,
signature and the first 1,200 characters of body, using model2vec
`potion-base-8M` (256 dimensions, static embeddings, CPU only). Vectors are
L2-normalized float32 blobs in `embeddings`; a query does a full matrix scan
(`matrix @ q`) and takes the top-k. At 106k symbols that is ~110 MB and ~10 ms
per query, which beats maintaining an ANN index.

`hybrid_semantic_search` is deliberately conservative:

1. Run lexical `hybrid_search`.
2. If the top hit is a proc whose name equals the query, or whose path
   segments cover every query token, return the lexical list unchanged.
3. Otherwise fetch semantic neighbours and fuse with weighted reciprocal rank
   fusion: `1.0/(60+rank)` lexical, `0.5/(60+rank)` semantic.

Rationale: on the 17-question gold set, pure semantic ranking scored
`MRR 0.668 · top-1 53%`, while lexical scored `0.941 · 94%`. DreamMaker names
(`attack_self`, `delaminate`) are already strong lexical signals, so vectors
are only worth consulting when the lexical ranker is unsure. The gate keeps
the semantic run at parity with lexical (`0.941 · 94%`) while fixing
natural-language queries such as *"how does the engine handle being hit by a
projectile"* → `/obj/projectile/proc/on_hit`.

## 6. Context bundles

`build_query_context`:

1. Seed from `find` (symbol-like query), `hybrid_search` (natural language), or
   `hybrid_semantic_search` with `--semantic`.
2. Add each seed's source span.
3. Expand one hop: callees first, then callers (capped).
4. Dedupe by `path@file:line`.
5. Stop when the estimated token budget is exhausted; count drops.

`estimate_tokens` uses ~3.6 characters per token, which is conservative for
dense code.

`build_symbol_context` adds the owner type's vars, then callees and callers.

## 7. Storage

SQLite, WAL mode, foreign keys on. One database per repo at
`<repo>/.oracle/oracle.db`. The schema version is stored in `meta`; a mismatch
is refused with a `--force` hint instead of being silently accepted. The
version check runs *before* `init_schema`, because `CREATE INDEX` on a column
an older database lacks would otherwise fail with an opaque `OperationalError`.

Schema v5 adds `file_id` to `inherits`, `params` and `locals`. Before that, a
re-index appended a second copy of every parameter and local and never replaced
a changed inheritance edge; the file id makes each row attributable, so
`delete_file` and the re-index path can remove exactly what the old version
wrote.

FTS5 is external-content-free: rows are inserted explicitly with the symbol
id as `rowid`, so deleting a symbol requires deleting its FTS row first (the
indexer does this in `delete_file` and `_dedupe`).

`embeddings` holds one row per symbol (`symbol_id` primary key), so switching
models replaces vectors instead of accumulating them. The active model name is
stored in `meta.embedding_model`; queries read it rather than assuming the
default. The in-process matrix cache is keyed by database path, file mtime and
model name, so a re-index or re-embed invalidates it.

## 8. MCP server

`mcp_server.py` wraps the same query layer in `MCPServer` (mcp 2.x; `FastMCP`
was renamed). Each tool opens a short-lived SQLite connection, so the server is
safe to run alongside an indexing process. Tools return JSON strings for
structured data and a rendered Markdown bundle for `oracle_context`.

## 9. Multi-fork comparison

A merged database across forks was considered and rejected. DM paths are
repo-global identifiers: `/obj/item/proc/attack_self` exists in every fork, so
a merged index would store N rows for the same path and every query would need
a repo qualifier to stay meaningful. Instead each fork keeps its own index and
`multi.py` compares two of them.

`diff` loads `path → (kind, signature, body_hash)` for procs and types from
both indexes and classifies each path as shared, only-left, only-right or
changed. `body_hash` is the SHA-1 of the proc body computed by the parser, so
whitespace-level edits count as changes; that is intentional for porting, where
you want to see divergence, not a semantic diff. For a type, the body is its
direct `var` declarations (including `parent_type`), so a changed default shows
up as a changed type instead of an always-empty body.

### Rename detection

A renamed symbol keeps its body and changes its name, so the signature is
useless as a key — only `body_hash` survives. `_detect_renames` therefore keys
the leftover `only_left`/`only_right` records by `(kind, body_hash)` and pairs
them when the match is unique on both sides and the body spans at least three
lines. The span guard matters: without it, every `return` proc in the tree
shares a hash and the pairing would be arbitrary. A pair is moved out of the
port-candidate lists, so a refactor shows up once instead of twice.

The same body in two places is not a rename, and the uniqueness requirement is
what rejects it: two candidates on either side means the body is not
distinctive enough to claim one.

`port` answers a narrower question: what happens if I copy this symbol over?

1. Look the symbol up in the source fork; if it is absent, stop.
2. Look it up in the target fork and compare signature and body hash.
3. Check whether the owner type exists in the target at all — if not, the type
   has to be ported first.
4. Find call sites in the target that would dispatch to the ported proc.

Step 4 is the interesting one. Matching `callee_raw` by name alone is useless:
every `turn_on()` in the tree matches. So the search is restricted to callers
whose owner type is the symbol's owner or one of its descendants, using the
`inherits` table to expand the family. That is the set of call sites that would
actually resolve to the ported proc.

## 10. Change impact

`ci.py` turns a unified diff into an impact report in five steps.

1. **Parse.** `git diff --unified=0` (or a patch file / stdin) is split into
   per-file hunks; only `.dm` files survive. A hunk's new-side range is
   `[new_start, new_start + new_count - 1]`; when `new_count` is 0 (pure
   deletion) the range collapses to the insertion point. `--- `/`+++ ` are only
   treated as headers before the first hunk, so a removed source line that
   happens to start with `-- ` is not mistaken for one.
2. **Map to symbols.** For each changed file, symbols whose line span
   intersects a hunk are selected. Deletions anchor at the new-side position,
   which still falls inside the enclosing proc, so a removed line attributes to
   the proc that contained it.
3. **Recover deleted files.** A deleted file has no new side and no index rows,
   so the removed lines are rebuilt into text (padded so line numbers match the
   old file) and parsed with `parse_source`. Every path the index no longer
   defines is a removed symbol. Their call edges were already re-resolved to
   `unresolved` with a null target, so callers are matched by raw name instead
   — literally, and for a member proc restricted to the owner's type family, or
   every `turn_on()` in the tree would be blamed. Those call sites enter the
   report at depth 1: they are broken now, not downstream.
4. **Expand.** Seeds are changed procs, plus — for a changed type — its member
   procs, because a var default on `/obj/item` is read by everything defined on
   that type. A BFS over `calls.callee_path` walks callers outward, one SQL
   query per level and per chunk of 400 paths. Roots propagate outward with the
   frontier (`roots[caller] |= roots[callee]`), so each affected symbol can say
   which change it is downstream of. Symbols under the test prefix are skipped:
   tests are the verification step, not the blast radius.
5. **Suggest tests.** First, unit tests that call an affected proc (an exact
   edge into `/datum/unit_test/...`), plus tests whose raw call name matches a
   removed symbol. Then, as a fallback, unit tests whose body mentions a
   changed file's stem, searched per word because FTS splits on underscores.
   Suggestions are reported at the datum level (`/datum/unit_test/mob_damage`,
   not its `Run` proc).

`--fail-on-affected` turns the report into a gate: exit 1 if the affected set
is non-empty.

Known blind spots: a *new* override adds no call edges, so it cannot appear in
the affected set even though behaviour changed (the changed symbol itself is
still reported); and a proc removed from a *surviving* file is only attributed
when the hunk still lands inside it, because `--unified=0` carries no context
lines to reconstruct the old side.

## 11. Testing strategy

* Parser tests use a small synthetic `.dm` file that exercises prefix forms,
  typed vars, params, locals, defines and call attribution, plus the flattened
  bodyless-type shape and explicit `parent_type` precedence.
* Resolver tests build a tiny in-memory graph and assert every resolution
  kind, including `ambiguous` and `unresolved`.
* Query tests cover ordering, FTS snippets, callers/callees, transitive
  impact, LIKE-wildcard escaping, empty patterns and negative limits.
* Indexer tests re-index a temporary repo and assert that parameters, locals
  and inheritance edges are replaced rather than duplicated, that deleting a
  file removes its derived rows, and that an old schema is refused until
  `--force`.
* Context tests assert the zero-budget and tiny-budget paths, and that a
  missing symbol produces an empty bundle.
* Eval tests feed malformed JSON and assert a readable `ValueError`.
* CLI tests drive Typer's runner over an indexed temp repo: markup-heavy
  source must not crash `context`, `search` or `show`, a zero budget must not
  emit source, and an unwritable `-o` path exits 2.
* Embedding tests stub `Embedder.encode` with a deterministic 4-d vector, then
  assert cosine ranking, RRF ordering and both branches of the confidence gate.
* MCP tests list the registered tools and call a few through
  `server.call_tool`.
* Multi-repo tests build two in-memory indexes and assert the diff buckets,
  signature-only changes, rename pairing (and its trivial-body and
  ambiguous-body guards), owner-type detection and type-scoped call sites.
* CI tests feed a synthetic patch through the parser, assert hunk-to-symbol
  mapping (including deletion anchoring), transitive expansion with roots, both
  test-suggestion paths, and the deleted-file path end to end: index, remove the
  file, re-index, then check that the surviving caller is reported as broken.
* The eval harness is the end-to-end check: 17 questions against a real fork,
  in lexical and semantic mode.

## 12. Known gaps

* `call(target, "name")` and `call(src, proc_ref)` are dynamic and unresolved.
* Locals without an explicit type (`var/x = f()`) are not inferred.
* `#if` branches are all indexed.
* The embedding model is general English; `ex_act` and other BYOND jargon are
  outside its vocabulary, so `"explosion damage"` still misses.
* Cross-fork diff is path-based: a rename is only detected when the body is
  byte-identical and the pairing is unique; a rename *with* edits still appears
  as one missing plus one extra.
* `port` copies nothing; it reports. Applying the patch is still manual.
* `ci` maps hunks by line number, so the index must describe the new side of
  the diff; a stale index silently attributes changes to the wrong proc.
* `ci` reconstructs only a fully deleted file. With `--unified=0` there is no
  context to rebuild the old side of a partial deletion, so a proc removed from
  a surviving file is only attributed when the hunk still lands inside it.
* Call sites of a deleted symbol are reported at depth 1 and not expanded
  further: the removed node is no longer in the graph to walk from.
* Type bodies contain var declarations only; a member proc's edit changes the
  proc's hash, not the owner type's.
* An index from an older schema is rejected until `index --force` rebuilds it.
