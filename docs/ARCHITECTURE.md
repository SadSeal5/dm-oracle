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

`/obj/item/foo` re-declares `/obj` and `/obj/item` every time. A 1.25M-line
codebase produces ~356k type rows for ~41k distinct types. After indexing, a
window function keeps the largest declaration per path:

```sql
ROW_NUMBER() OVER (PARTITION BY path ORDER BY (end_byte - start_byte) DESC, id)
```

and deletes the rest (including their FTS rows). Inheritance edges are
deduped with `MIN(id) GROUP BY child, parent`.

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
`<repo>/.oracle/oracle.db`. Schema version is stored in `meta`; a mismatch is
handled by `index --force`.

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
you want to see divergence, not a semantic diff.

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

`ci.py` turns a unified diff into an impact report in four steps.

1. **Parse.** `git diff --unified=0` (or a patch file / stdin) is split into
   per-file hunks; only `.dm` files survive. A hunk's new-side range is
   `[new_start, new_start + new_count - 1]`; when `new_count` is 0 (pure
   deletion) the range collapses to the insertion point.
2. **Map to symbols.** For each changed file, symbols whose line span
   intersects a hunk are selected. Deletions anchor at the new-side position,
   which still falls inside the enclosing proc, so a removed line attributes to
   the proc that contained it.
3. **Expand.** Seeds are changed procs, plus — for a changed type — its member
   procs, because a var default on `/obj/item` is read by everything defined on
   that type. A BFS over `calls.callee_path` walks callers outward, one SQL
   query per level and per chunk of 400 paths. Roots propagate outward with the
   frontier (`roots[caller] |= roots[callee]`), so each affected symbol can say
   which change it is downstream of. Symbols under the test prefix are skipped:
   tests are the verification step, not the blast radius.
4. **Suggest tests.** First, unit tests that call an affected proc (an exact
   edge into `/datum/unit_test/...`). Then, as a fallback, unit tests whose body
   mentions a changed file's stem, searched per word because FTS splits on
   underscores. Suggestions are reported at the datum level
   (`/datum/unit_test/mob_damage`, not its `Run` proc).

`--fail-on-affected` turns the report into a gate: exit 1 if the affected set
is non-empty.

Known blind spot: a *new* override adds no call edges, so it cannot appear in
the affected set even though behaviour changed. The changed symbol itself is
still reported.

## 11. Testing strategy

* Parser tests use a small synthetic `.dm` file that exercises prefix forms,
  typed vars, params, locals, defines and call attribution.
* Resolver tests build a tiny in-memory graph and assert every resolution
  kind, including `ambiguous` and `unresolved`.
* Query tests cover ordering, FTS snippets, callers/callees and transitive
  impact.
* Embedding tests stub `Embedder.encode` with a deterministic 4-d vector, then
  assert cosine ranking, RRF ordering and both branches of the confidence gate.
* MCP tests list the registered tools and call a few through
  `server.call_tool`.
* Multi-repo tests build two in-memory indexes and assert the diff buckets,
  signature-only changes, owner-type detection and type-scoped call sites.
* CI tests feed a synthetic patch through the parser, assert hunk-to-symbol
  mapping (including deletion anchoring), transitive expansion with roots, and
  both test-suggestion paths.
* The eval harness is the end-to-end check: 17 questions against a real fork,
  in lexical and semantic mode.

## 12. Known gaps

* `call(target, "name")` and `call(src, proc_ref)` are dynamic and unresolved.
* Locals without an explicit type (`var/x = f()`) are not inferred.
* `#if` branches are all indexed.
* The embedding model is general English; `ex_act` and other BYOND jargon are
  outside its vocabulary, so `"explosion damage"` still misses.
* Cross-fork diff is path-based: renames appear as one missing plus one extra.
* `port` copies nothing; it reports. Applying the patch is still manual.
* `ci` maps hunks by line number, so the index must describe the new side of
  the diff; a stale index silently attributes changes to the wrong proc.
