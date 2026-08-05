# Phase 1 — Task 1.4 Identifier Normalization (Unicode / case / punctuation / aliases)

**Date (frozen):** 2026-07-28
**Task:** 1.4 — Implement and test Unicode, case, punctuation-preserving,
punctuation-separated, and alias normalization for model/node/code identifiers.
**Plan:** `docs/architecture/hivemind-hybrid-search-plan.md` (Astrid planning copy).
**Prereqs:** 0.6 frozen 112-case golden set, 1.1 lexical contract (complete, untouched).
**Concurrency:** Tasks 1.2 (`schema/003`) and 1.3 (`schema/004` / live Discord index)
ran in parallel and are preserved untouched; this task adds only its own files.

This deliverable is a **frozen, deterministic, cross-language normalization contract**
plus the **explicit, bounded alias representation** the exact-identifier candidate arm
(tasks 1.5–1.7) will consume. It creates no trigram index (1.5), no full-message
identifier structure (1.6), and no candidate SQL/RPC (1.7). SQL/Python parity is
**proven byte-for-byte on the frozen fixture corpus** on an isolated PostgreSQL 14
cluster; function immutability and index suitability are proven, not asserted.

## Files produced

| Path | Role |
|---|---|
| `executors/identifier_normalization.py` | **Frozen reference module** — forms, casefold policy, alias registry (provenance/version/collision/priority/safe-update, no-NL-rewrite). Pure stdlib, offline. Re-exports the frozen 1.1 helpers (single source of truth). |
| `schema/005_identifier_normalization.sql` | **IMMUTABLE SQL mirror** — `hivemind_normalize_identifier`, `hivemind_normalize_identifier_preserve`, `hivemind_identifier_alias_forms`, deterministic ICU collation, the `identifier_aliases` reference table, registration + collision-detection functions. |
| `eval/retrieval/fixtures/identifier-normalization-v1.json` | **Versioned machine-readable fixture corpus** — 86 fixtures (77 accept + 9 distinct), grounded in the real 112 golden identifiers + expanded forms. |
| `scripts/validate_identifier_normalization.py` | **Deterministic validator** — offline contract checks (always) + opt-in isolated-cluster SQL/Python parity + IMMUTABLE/index/locale-independence proof. |
| `tests/test_identifier_normalization.py` | **Offline tests** — 42 tests pinning every frozen rule, non-equivalence, alias behavior, and corpus self-consistency. |
| `docs/hybrid-search/phase1-identifier-normalization.md` | **This report** (human). |
| `docs/hybrid-search/phase1-identifier-normalization.json` | **Machine-readable summary** (version-pinned, `post_hoc_locked: true`). |

## Commands

```bash
python3 scripts/validate_identifier_normalization.py                  # offline contract checks
HIVEMIND_EVAL_CLUSTER=1 python3 scripts/validate_identifier_normalization.py   # + isolated-cluster parity
python3 -m unittest tests.test_identifier_normalization -v            # this task's tests
python3 -m unittest discover tests/                                   # whole repo
```

---

## 1. The frozen forms (one cross-language contract)

Every identifier normalizes to **two** deterministic keys; both are implemented
identically in Python (`executors/identifier_normalization.py`) and PostgreSQL
(`schema/005`). All on the frozen `'simple'`-aligned policy from task 1.1.

| Form | Python | SQL (IMMUTABLE) | Rule |
|---|---|---|---|
| **compact** (punctuation-separated/removed) | `normalize_identifier` | `hivemind_normalize_identifier` | NFC + lower + strip + drop the frozen separator set |
| **preserve** (punctuation-preserving) | `normalize_identifier_preserve` | `hivemind_normalize_identifier_preserve` | NFC + lower + strip + collapse whitespace runs to single spaces (**keeps punctuation**) |
| ordered both | `identifier_forms` | `hivemind_identifier_alias_forms` | `(compact, preserve)` de-duplicated, compact first |

`normalize_query` (FTS text: NFC + whitespace collapse, **no lower** — the config
lowercases) and `identifier_aliases` are **re-exported unchanged** from the frozen
1.1 module; `identifier_forms` is asserted contract-equal to `identifier_aliases`
for every string fixture.

### Frozen separator set (dropped by compact, kept by preserve)

```
whitespace ( \s )  .  -  _  /  \  ,  :  ;  (  )  {  }  [  ]  @  "  '  `
```

So `Wan 2.2`, `Wan2.2`, `wan_2.2`, `WAN 2.2`, `wan2.2` all collapse to compact
`wan22`; `key=value` keeps `=` (operator, not a separator) → compact
`key=value`; `C:\Users\flux` → compact `cusersflux`, preserve `c:\users\flux`.

## 2. Casefold policy (frozen): NFC + `str.lower()`, NOT `str.casefold()`

`str.casefold()` introduces multi-character expansions (`ß`→`ss`, ligatures
`ﬁ`→`fi`) and combining-mark insertions (`İ`→`i` + U+0307) that PostgreSQL has no
single built-in to reproduce, which would break byte-for-byte parity. NFC +
`str.lower()` agrees with PostgreSQL `lower(value COLLATE <icu-und>)` across the
BMP Latin / Latin-Extended / Greek / Cyrillic / CJK ranges that constitute the
corpus. The `≠ casefold` consequences are the documented distinct forms (§4).

## 3. The locale trap, and how SQL avoids it

PostgreSQL has **no built-in Unicode NFC**, and stock `lower(text)` is
**locale/collation-dependent**. Observed on an isolated PostgreSQL 14 cluster
whose default collation is `C`:

```text
cluster lc_collate = 'C'
stock lower('ÜBER')              -> 'Über'        -- Ü NOT lowered (the locale trap)
hivemind_normalize_identifier('ÜBER CAFÉ 动漫') -> 'übercafé动漫'   -- locale-independent
```

**Resolution (frozen, in `schema/005`):** the functions lowercase through a
**deterministic ICU collation** created by the migration:

```sql
create collation if not exists public.hivemind_unicode (
  provider = icu, locale = 'und', deterministic = true
);
```

`lower(value collate public.hivemind_unicode)` is locale-independent (verified on
a `C`-locale cluster) and matches Python `str.lower()`. Because the collation is
deterministic, the functions are `IMMUTABLE` and eligible for **expression
indexes** and **STORED generated columns** (proven in § Evidence). Supabase
PostgreSQL is built `--with-icu`, so the collation is available in production.

The one residual SQL/Python difference is **NFC composition** (§5): Python applies
NFC; SQL cannot, so the ingest layer must store identifier text in NFC (the Python
reference enforces it). This is the single documented non-equivalence, and the
frozen fixture corpus is authored in NFC so the two agree byte-for-byte.

## 4. Characters/forms that intentionally remain distinct

These are deliberately **not** folded (no `casefold`, no NFKC, no confusable
folding):

| Form | Behavior | Why |
|---|---|---|
| `ß` | stays `ß` (not `ss`); `groß` ≠ `gross` | no multi-char expansion |
| ligatures `ﬁ ﬂ ﬀ` | stay composed; `ﬁle` ≠ `file` | NFC, not NFKC |
| fullwidth `ＡＢＣ` | stays fullwidth; `ＡＢＣ` ≠ `ABC` | NFC, not NFKC |
| superscript `²` | stays distinct; `²` ≠ `2` | NFC, not NFKC |
| `İ` | → `i` + U+0307 (not plain `i`) | matches ICU `und`; both engines agree |
| homoglyphs `a / а / α` | Latin/Cyrillic/Greek `a` = **3 distinct** compact keys | confusable folding is a separate, riskier transform; deliberately absent |

## 5. Known non-equivalences and collisions (named, not blockers)

1. **NFC composition (Python↔SQL).** Python NFC-folds NFD input; SQL has no NFC.
   NFD inputs (combining diacritics) are the only Python/SQL divergence. Mitigation:
   the indexing pipeline stores identifier text in NFC (enforced by the Python
   reference at index time); the frozen corpus is NFC-authored. Accept-corpus
   parity is byte-for-byte (0 mismatches / 86).
2. **`identifier_forms(None)` vs frozen `identifier_aliases(None)`.** Our
   `identifier_forms(None)` → `()` (no forms); the frozen 1.1
   `identifier_aliases(None)` → `('none',)` because it stringifies `None` first.
   `None` is the NULL boundary, not a string identifier; the parity guarantee is
   stated for string inputs only. SQL returns `''` for NULL (NULL-safe via
   `coalesce`), matching the Python reference.
3. **Alias collisions are reported, not silently merged.** When two distinct
   canonical identities share a compact key (e.g. `ControlNet` and `Control Mesh`
   both → `controlnet`), both remain valid candidates ordered by deterministic
   priority, and `hivemind_identifier_alias_collisions()` /
   `AliasRegistry.collisions()` surface them for operator disambiguation. No
   identity is ever dropped to "resolve" an alias.

## 6. Alias representation (explicit, bounded; for the candidate arm)

`identifier_aliases` is an internal, versioned reference (RLS-on, no anon policy,
like `contributors`) that expands a query written one way to a canonical identity
known another way. Columns `canonical_key` / `alias_compact` / `alias_preserve`
are **STORED GENERATED** from the IMMUTABLE functions (the proof they are
index-suitable).

- **Provenance + version.** Each row records `provenance` (frozen vocabulary:
  `curated` > `workflow_semantics.searchable_aliases` > `.node_class` >
  `.models` > `derived_canonical`), `provenance_detail` (non-secret), and
  `alias_version`. A version bump re-registers the live set; stale rows become
  `live=false` tombstones (append-only; never deleted in place).
- **Deterministic priority.** `hivemind_alias_provenance_priority` /
  `provenance_priority()` map provenance → integer; ties break by provenance
  weight then stable identity order (`AliasRegistry.resolve_alias_candidates`).
- **Safe update.** `hivemind_register_identifier_alias` is idempotent on
  `(canonical_kind, canonical_id, alias_compact, provenance)` and fails closed on
  an alias/canonical that normalizes to empty.
- **No silent natural-language rewrite (the core safety property).** Aliases only
  **add candidate identity edges** for the exact-identifier arm.
  `AliasRegistry.expand_query_identifiers` returns a set of canonical
  **identity strings** (`kind:id`), never a rewritten query; arbitrary natural
  language is never synthesized into an alias. Aliases are never substituted into
  FTS query text or the prose tsvector, and never relabel one item's identity as
  another's. (The bounded B-arm projection of `searchable_aliases` into resource
  prose is task 1.2's separate, frozen projection; this is the parallel
  exact-identifier index.)

## 7. Evidence — isolated PostgreSQL 14 cluster

A throwaway cluster (`initdb --auth=trust --no-locale -E UTF8`, temp data dir,
port 5494, no network) loaded `schema/005` and ran the frozen corpus. Torn down
after; no production or shared database touched.

```text
[cluster] SQL/Python parity + IMMUTABLE/index proof (isolated PG14)
  cluster lc_collate = 'C'
  ok: stock lower() leaves Ü untouched under C locale (the locale trap)
  ok: ICU-collation function lowercases non-ASCII regardless of locale
  ok: SQL/Python byte-for-byte parity on all 86 fixtures (mismatches=0)
  ok: all four normalization functions are IMMUTABLE (provolatile=i):
      hivemind_alias_provenance_priority|i / hivemind_identifier_alias_forms|i /
      hivemind_normalize_identifier|i / hivemind_normalize_identifier_preserve|i
  ok: STORED generated columns (compact + preserve) populate correctly
  ok: expression index hivemind_normalize_identifier(title) is USED by the planner
      (Index Scan using iproof_idx on a 40,000-row table)
  ok: alias registration populates generated compact columns
  ok: collision detection reports 1 shared alias compact key
  ok: stored function definition pins the ICU collation
```

The expression index `hivemind_normalize_identifier(title)` is **used by the
planner** (`Index Scan`), and STORED generated columns using both functions
succeed — the operational proof of IMMUTABLE / index suitability.

## 8. Test evidence

```text
python3 -m unittest tests.test_identifier_normalization      -> 42 tests OK
python3 scripts/validate_identifier_normalization.py         -> OK (offline)
HIVEMIND_EVAL_CLUSTER=1 python3 scripts/validate_identifier_normalization.py -> OK (cluster)
python3 -m unittest discover tests/                          -> 723 tests OK (642 baseline + this task + concurrent 1.2/1.3)
python3 scripts/validate_lexical_contract.py                 -> OK (1.1 still validates)
python3 scripts/validate_workflow_contract.py                -> PASS (0.8 still validates)
```

Coverage (42 offline tests): frozen metadata/versions, re-export identity,
compact form (dotted/versioned/hyphen/filename/symbol/keyword-arg/backslash/
code-punctuation/null), preserve form, forms-parity with 1.1, distinct forms
(ß/ligature/fullwidth/confusables/İ), malformed + length bounds, alias
registration/idempotency/validation, collision reporting + deterministic
resolution, no-NL-rewrite, provenance priority, and corpus self-consistency.

## 9. Boundary and preserved work

- **No production mutation.** Every database claim is on an isolated throwaway
  cluster, torn down after capture. No source-row, index, RPC, Edge function, or
  corpus change.
- **Scope respected.** No trigram index (1.5), no full-message identifier
  structure (1.6), no candidate SQL/RPC (1.7). `schema/005` defines only the
  IMMUTABLE primitives + the alias reference; both are clearly marked as inputs to
  1.5/1.7.
- **No collision with concurrent work.** `schema/005`'s collation
  (`hivemind_unicode`) and functions are unique; `schema/003` / `schema/004`
  (tasks 1.2/1.3) reference none of them. The dirty tree and all concurrent
  artifacts are preserved untouched.

## Completion signal (1.4)

> Golden fixtures cover dotted, versioned, hyphenated, filename, Python symbol,
> keyword-argument, and alias forms.

**Met.** The frozen 86-fixture corpus (`identifier-normalization-v1.json`,
`post_hoc_locked`) covers every required family — dotted (`FLUX.1`), versioned
(`Wan 2.2`/`Wan2.2`), hyphenated (`LTX-Video`, `ltx-2-19b-ic-lora-detailer`),
filename (`.safetensors`, `lightx2v_I2V_14B.safetensors`), Python symbol
(`WanVideoSampler`, `IPAdapterFaceIDKolors`), keyword-argument (`force_clip_output=False`),
and alias (`control net`/`controlnet`) — grounded in the real 112 golden
identifiers, plus imports/class/def, paths, CJK, diacritics, confusables,
adversarial SQL-like strings, and malformed/length bounds. SQL and Python agree
byte-for-byte on all 86; the IMMUTABLE functions are index-suitable and
locale-independent, proven on an isolated PG14 cluster.

## Dependency-safe next tasks

Task 1.4 has no blockers. Per the plan's critical path it unblocks (do **not**
start here — this task stops at 1.4):

- **1.5** — Add bounded trigram indexes for high-value short fields (resource
  titles, distillation questions) over `hivemind_normalize_identifier` /
  `alias_compact`. The IMMUTABLE primitives + generated columns are ready.
- **1.6** — Select the full-message exact-identifier path (message trigram index
  vs normalized identifier side index). The compact/preserve keys + alias table
  are the side-index payload.
- **1.7** — Lexical candidate SQL combining FTS / phrase / exact-identifier /
  bounded workflow-code fragment arms; consumes `identifier_aliases` via
  `resolve_alias_candidates`-style deterministic identity expansion.
