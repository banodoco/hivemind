# Phase 1 — Task 1.1 Lexical Configuration Decision Record

**Date (frozen):** 2026-07-28
**Task:** 1.1 — Choose the canonical PostgreSQL text-search configuration and exact
indexed expressions for all entity/representation types.
**Plan:** `docs/architecture/hivemind-hybrid-search-plan.md` (Astrid planning copy).
**Prereqs:** 0.1 access, 0.2 schema/eligibility, 0.3 inventory, 0.4 baseline, 0.5
eval harness, 0.6 golden set, 0.7 capacity, 0.8 workflow representation contract
(all complete; untouched).
**Endpoint ref:** `ujlwuvkrxlvoswwkerdf` · **Evidence date:** 2026-07-28

This is a **freeze / decision record**, not Phase 1 implementation. It chooses the
canonical text-search configuration, freezes the exact weighted `to_tsvector`
expressions, query constructors, normalizers, eligibility predicates, ranking
inputs, the bounded workflow-code lexical-document policy, and the golden
acceptance fixtures. It creates **no migration, no index, no RPC, no corpus
change**, and does not start tasks 1.2–1.10. Every tokenization claim below was
**observed on a real, isolated PostgreSQL 14 instance** (§ "Evidence"); it is not
recalled from memory.

## Files produced

| Path | Role |
|---|---|
| `docs/hybrid-search/phase1-lexical-contract.md` | **This decision record** (human). |
| `docs/hybrid-search/phase1-lexical-contract.json` | **Machine-readable contract** (version-pinned, `post_hoc_locked: true`). |
| `executors/lexical_contract.py` | **Frozen reference module** — config, weighted specs, query arms, normalizers, eligibility predicates, collapse rule, bounded code-doc policy. Pure stdlib, offline. |
| `docs/hybrid-search/phase1-fts-probes.sql` | **SQL probe script** (read-only) for 1.2/1.3 to re-confirm exact tokenization on the live DB. |
| `scripts/validate_lexical_contract.py` | **Deterministic validator** (contract consistency + reference behaviour on fixtures incl. observed evidence). |
| `tests/test_lexical_contract.py` | **Offline tests** pinning every frozen decision. |

## Commands

```bash
python3 scripts/validate_lexical_contract.py                # contract validation
python3 -m unittest tests.test_lexical_contract -v          # this task's tests
python3 -m unittest discover tests/                         # whole repo
```

---

## 1. The canonical configuration decision (frozen): `'simple'` everywhere

**Decision: every entity and representation uses `'simple'::regconfig`, and every
query constructor passes the same explicit `'simple'::regconfig`.**

| Surface | Frozen config |
|---|---|
| Message prose (`discord_messages.content`) | `'simple'` |
| Resource prose (title / tags+semantics / body) | `'simple'` |
| Resource `workflow_python` (per code chunk) | `'simple'` |
| Distillation prose (question / conditions / answer) | `'simple'` |
| Query: `websearch_to_tsquery` (default) | `'simple'` |
| Query: `phraseto_tsquery` (exact-name / quoted phrase arm) | `'simple'` |
| Query: exact-identifier side arm | `'simple'` FTS + `normalize_identifier` equality + trigram |

Uniform config is the whole point: a `websearch_to_tsquery('simple', q)` can then
never mismatch an indexed expression, across messages, resources, and
distillations. There is **no representation-aware config split** — `simple` for
prose *and* code. The only representation-aware separation is the **exact-identifier
arm** (§5), which is a parallel candidate *arm*, not a different FTS config.

### 1.1 Resolving `simple` vs the live `english` index (the completion signal)

The live Discord index (task 0.3) is:

```sql
idx_discord_messages_content_fts ON public.discord_messages
  USING gin (to_tsvector('english'::regconfig, content))   -- 85 MB
```

Pumpernickel uses `'simple'`. The plan warns that a `simple` query will not use an
`english` index. **Observed on an isolated PostgreSQL 14 instance (2026-07-28):**

| Probe (all on `WanVideoSampler`) | Result |
|---|---|
| `to_tsvector('english', …) @@ websearch_to_tsquery('simple', …)` | **FALSE** |
| `to_tsvector('english', …) @@ websearch_to_tsquery('english', …)` | TRUE |
| `to_tsvector('simple', …) @@ websearch_to_tsquery('simple', …)` | TRUE |

and `EXPLAIN` of a `simple` query over a table holding both an `english` and a
`simple` GIN expression index: the planner uses the **`simple` index only** — it
cannot use the `english` expression index, because PostgreSQL expression-index
matching requires an **identical expression** (`to_tsvector('simple',content)` ≠
`to_tsvector('english',content)`). This is conclusive, not heuristic.

**Stemming / stopword behavior (same instance):**

| Input | `simple` | `english` |
|---|---|---|
| `WanVideoSampler` | `wanvideosampler` | `wanvideosampl` *(stemmer strips `-er`)* |
| `… running configs …` | `running`, `configs` | `run`, `config` |
| `the message is not a control net` | keeps `the/is/not/a` | drops them (stopword list) |
| `动漫 视频 anime video` | `动漫 视频 anime video` | `动漫 视频 anim video` |

The dictionaries confirm it: the built-in `simple` dictionary has
`dictinitoption = NULL` (lowercase only — no stemmer, no stopword list), while
`english_stem` runs `language='english', stopwords='english'`.

**Resolution.** `english`'s only advantage is morphological stemming for paraphrase
queries (`running`→`run`). That gap is closed by the **embedding layer (Phase 2)**;
the lexical arm is the **exact/keyword** arm, and exact-identifier recall is a
blocking gate (`exact_identifier Recall@10 ≥ 0.95`, `workflow_code ≥ 0.95`). For a
corpus whose high-value targets are model names (`FLUX.1`, `Wan 2.2`), node
classes (`WanVideoSampler`, `IPAdapterFaceIDKolors`), filenames (`.safetensors`,
`lightx2v_I2V_14B.safetensors`), and Python symbols, stemming **corrupts**
identifiers (`wanvideosampler`→`wanvideosampl`) and stopword removal drops real
tokens. `simple` is also strictly better for the global/multilingual Discord
community text. Therefore **adopt `simple`** and build new `simple` indexes.

**Consequence for the live index.** The existing `english` index cannot serve the
canonical `simple` query, so task **1.3 builds a NEW `simple` index** on
`discord_messages`. Whether the superseded `english` index is dropped (85 MB
reclamation) is an **additive 1.3 storage decision** — it is never required by the
canonical path and is gated inside the 12 GB capacity envelope (0.7: corpus tables
≈1.28 GB; 85 MB is negligible). This record neither drops nor creates it.

### 1.2 Bounded workflow-code documents (the other half of the signal)

Workflow Python is up to **1,415,262 chars** (~354K tokens, ~76 chunks at 512
tokens; 0.3). A single `to_tsvector` over 1.4M chars would (a) make `ts_rank` a
length-dominated magnitude and (b) lose position/phrase precision. **Decision:
workflow Python is ALWAYS stored as bounded per-chunk lexical documents** keyed by
`(resource_id, representation_type, chunk_index)`, using the frozen 0.8 code
chunker (AST-aware, pilot target 512 tok / 50 overlap, parser-fallback,
`coverage_ok` no-silent-truncation guard). Chunks collapse to one resource
identity before global ranking (§7). Full policy in §6.

---

## 2. `regconfig` per entity and representation (frozen)

| Entity | Representation | Config | Document shape | Weighted? |
|---|---|---|---|---|
| message | `prose` | `simple` | bare `content` | no (single field) |
| resource | `prose` | `simple` | weighted (title/tags+semantics/body) | A/B/C |
| resource | `workflow_python` | `simple` | one doc per code chunk | C (uniform) |
| distillation | `prose` | `simple` | weighted (question/conditions/answer) | A/B/C |

---

## 3. Exact weighted `to_tsvector` expressions (frozen)

Weights use PostgreSQL defaults (`ts_rank`: A=1.0, B=0.4, C=0.2, D=0.1). Frozen
map: **A** = title/question (high); **B** = tags + projected `workflow_semantics`
(incl. `searchable_aliases`) / distillation conditions (medium); **C** = prose
body / answer / Python chunk (normal); **D** reserved/unused.

### Message — bare field (no weighting)

```sql
-- task 1.3: canonical message index (NEW; supersedes reliance on english idx)
to_tsvector('simple'::regconfig, coalesce(content, ''))
```

### Distillation — weighted, single document

```sql
setweight(to_tsvector('simple'::regconfig, coalesce(question,  '')), 'A')
 || setweight(to_tsvector('simple'::regconfig, coalesce(conditions, '')), 'B')
 || setweight(to_tsvector('simple'::regconfig, coalesce(answer,     '')), 'C')
```

### Resource prose — weighted, single document (over-long → chunked fallback)

```sql
setweight(to_tsvector('simple'::regconfig, coalesce(title, '')), 'A')
 || setweight(to_tsvector('simple'::regconfig,
       coalesce(hivemind_resource_tags(metadata)
             || ' ' || hivemind_workflow_semantics_text(metadata), '')), 'B')
 || setweight(to_tsvector('simple'::regconfig,
       coalesce(hivemind_workflow_prose(body, kind), '')), 'C')
```

### Resource `workflow_python` — one document PER code chunk (uniform weight)

```sql
-- per-chunk column `chunk_text` of the maintained document table (§6)
setweight(to_tsvector('simple'::regconfig, coalesce(chunk_text, '')), 'C')
```

**Null handling.** Every field is `coalesce(<expr>, '')` so a null column
contributes nothing rather than nulling the whole `||`-concatenated vector. An
entirely null row yields an empty tsvector that matches nothing.

**IMMUTABLE helper functions** (`hivemind_resource_tags`, `hivemind_workflow_prose`,
`hivemind_workflow_semantics_text`) are task 1.2's job. They must be declared
`IMMUTABLE` (fixed-`regconfig` `to_tsvector`/`setweight` are immutable; the helpers
read only row columns) and must **mirror `executors/workflow_representation.py`
exactly** (`strip_python_blocks` for prose, the projected `workflow_semantics`
fields for B, payload/body precedence for python — see §6).

---

## 4. Query constructors (frozen, parallel-arm model)

A query does **not** pick one constructor. The lexical candidate SQL (task 1.7)
runs the relevant arms **in parallel** and merges their de-duplicated, collapsed
candidates (plan 1.7: "combining FTS, phrase, exact-identifier, and bounded
workflow-code fragment arms"). All on `'simple'`.

| Arm | Constructor | Fires when | Purpose |
|---|---|---|---|
| `fts` | `websearch_to_tsquery('simple', normalize_query(q))` | always (non-empty q) | AND-of-terms; honors user-typed `"phrase"`, `-exclusion`, `OR`; forgiving |
| `phrase` | `phraseto_tsquery('simple', …)` | whole quoted phrase OR a single bare name (no spaces/operators) | tight adjacency for exact names |
| `ident` | `normalize_identifier(q)` equality + `gin_trgm` similarity on the side index | always (non-empty q) | bridges punctuation/spelling variants FTS misses |

`websearch_to_tsquery` is the default because it supports multi-term AND, quoted
phrases, exclusions, and `OR` without callers building tsquery syntax, and does not
error on stray punctuation (unlike `to_tsquery`). Observed semantics: bare words
are AND'd (`'wanvideo' & 'sampler'`, both required); `"control net"` → adjacency
`'control' <-> 'net'`; `settings -deluxe` → `& !'deluxe'`.

`plainto_tsquery` is **not used** (no phrase/exclusion support, stricter); recorded
only for completeness.

### The exact-identifier need (observed)

`to_tsvector('simple','Wan 2.2')` → `'2.2':2 'wan':1`, but
`websearch_to_tsquery('simple','Wan2.2')` → `'wan2.2'`, and the two **do not match**
(`@@` → false). A user typing `Wan2.2` against a body that says `Wan 2.2` would get
no FTS hit — exactly the golden `spelling_variant` pair. The `ident` arm resolves
it by normalizing both to `wan22` (§5). This is why the exact-identifier arm is a
blocking requirement, not polish.

---

## 5. Normalization + alias handling (frozen)

`normalize_query(q)` — Unicode **NFC** + collapse whitespace to single spaces +
strip. Applied to the text handed to every FTS constructor. The frozen config does
the lowercasing/tokenization; this never lowercases. Mirrors Pumpernickel's
`normalize_query_for_embedding` (ported).

`normalize_identifier(v)` — NFC, lowercase, strip, then **remove** every
separator/punctuation char (`\s . - _ / \ , : ; ( ) { } [ ] @ " ' \``), keeping
alphanumerics. Deterministic; applied identically to indexed value and query term.

| Input | `normalize_identifier` |
|---|---|
| `Wan 2.2` | `wan22` |
| `Wan2.2` | `wan22` |
| `wan_2.2` | `wan22` |
| `FLUX.1` | `flux1` |
| `LTX-Video` | `ltxvideo` |
| `model.safetensors` | `modelsafetensors` |

Stored as a **normalized column** (`public.hivemind_normalize_identifier(text)`,
IMMUTABLE) on resource titles and distillation questions (task 1.4/1.5), matched
by exact equality **plus** a `gin_trgm_ops` similarity path (`<%`) for typo
tolerance. `identifier_aliases(name)` also yields the whitespace-collapsed lower
form so both compact and spaced variants index.

**Alias handling.** Workflow `searchable_aliases` (e.g. `flux.1`, `wanvideo`,
`control net`) are projected into the **B-weight arm** of resource prose
(via `hivemind_workflow_semantics_text`), so a workflow known by an alias is FTS-
discoverable. The `ident` arm additionally normalizes aliases so `ControlNet`,
`control net`, and `controlnet` resolve together. Aliases are never embedded into
the semantic canonical text wholesale except via the 0.8 frozen projection.

---

## 6. Bounded workflow-code lexical-document policy (frozen)

Keyed by **`(entity_type, item_id, representation_type, chunk_index)`** — the same
identity shape as the 0.8 embedding table, so a lexical chunk and an embedding
chunk share an addressable identity.

**Document table (frozen shape for task 1.2):**

```sql
-- maintained by workflow-representation remediation/refresh (0.8 §8, tasks 1.2/2.12)
CREATE TABLE public.lexical_documents (
  entity_type          text not null,        -- message | resource | distillation
  item_id              text not null,        -- external_resources.id::text | message_id::text | distillation.id::text
  representation_type  text not null default 'prose',  -- prose | workflow_python
  chunk_index          integer not null default 0,
  tsv                  tsvector not null,    -- the frozen weighted expression (§3)
  matched_anchor       text,                 -- ≤240-char secret-redacted snippet anchor
  representation_hash  text,                 -- freshness (mirrors 0.8)
  chunk_hash           text,
  lexicalization_version integer not null default 1,
  PRIMARY KEY (entity_type, item_id, representation_type, chunk_index)
);
-- task 1.2 indexes: GIN(tsv); btree(entity_type,item_id) for collapse;
-- btree(representation_hash) for staleness; task 1.5 trigram on matched_anchor/ident.
```

Note: messages stay on the `discord_messages` expression index (no row in this
table); distillations and resource-prose use GENERATED columns on their source
tables when single-doc, and over-long prose + all workflow_python use this table.

**Policy (frozen):**

- **Chunk sizing / overlap / offsets.** Workflow Python: AST-aware chunker, pilot
  target 512 tok / 50 overlap (0.8 `python_512`); record `chunk_index`, content
  `chunk_hash`, method, stable source offsets. Prose over-long fallback: 512 tok /
  50 overlap. Production chunk config chosen in 2.14.
- **No silent truncation.** Every workflow-Python representation is fully covered
  by its chunk set (`workflow_representation.coverage_ok`, 0.8). Over-limit
  documents are **split, never dropped**; no Python is head/tail-truncated to fit
  one vector. Worst case: 1,415,262 chars → ~76 chunks.
- **Canonical Python precedence / dedup / quarantine** (0.8, mandatory here):
  authoritative bytes = non-empty `payload.python_source` → recognized body
  delimiter → recoverable → unavailable. When payload bytes also form the body
  block, that block is **stripped from prose** before indexing (no-duplication; the
  same bytes index exactly once, as `workflow_python`). A `secret-scanner` hit
  (0.8 safe policy) sets public state `quarantined` → **no `workflow_python`
  lexical document at all** (prose/semantics remain searchable if independently
  safe). The 222 VibeComfy `both`-cohort rows are the present-day case: strip body
  block, treat `payload.python_source` as authoritative.
- **Identifier / code-fragment normalization.** Code is indexed as **inert text**
  via `simple` — symbols like `wanvideosampler`, `modelspec`, `lora_weight`,
  `num_frames`, `class`, `def` are preserved lexemes (observed). The `ident` arm
  adds the normalized form for fuzzy/exact bridging. Code is never executed.
- **Collapse to one workflow identity.** Within one `representation_type`, keep the
  best-`ts_rank` chunk per item; across representations, keep the best
  representation and carry its `(representation_type, chunk_index, matched_snippet)`
  onto the identity. The item then ranks exactly once (§7).
- **Refresh / version / hash.** Lexical state is refreshed **only after the
  source-row patch commits** (0.8 §8). `lexicalization_version`,
  `representation_hash`, `chunk_hash` track freshness; a Python/body/title/
  semantics change, a version bump, or a `safe↔quarantined` transition re-derives
  the affected documents (hash-skip if unchanged).

---

## 7. Ranking inputs + chunk collapse (frozen)

- **Lexical rank:** `ts_rank(tsv, tsq, 32)` — frozen normalization flag `32`
  (Pumpernickel's value). Verified: flag 32 dampens long-document magnitude vs
  flag 0, so a symbol hit in a ~1.4M-char workflow-Python chunk does not dominate
  shorter matches. Default weight multipliers A=1.0/B=0.4/C=0.2/D=0.1.
- **Chunk collapse** (before global ranking): best `ts_rank` chunk per
  `(entity_type, item_id, representation_type)` → best representation per
  `(entity_type, item_id)` → **exactly one** ranked row per item, carrying
  `matched_representation` + `matched_snippet`.
- **Deterministic tie-break:** `lexical_rank DESC NULLS LAST, created_at DESC NULLS
  LAST, entity_type ASC, item_id ASC`. `item_id` is text (snowflake-safe).
- **Hybrid fusion** (Phase 3, frozen K): RRF with `K=60`,
  `score = source_weight × [1/(K+lexical_rank) + 1/(K+semantic_rank)]`; source/status
  weights conservative and cannot overpower both arms. The lexical rank feeds the
  `lexical_rank` term; the candidate multiplier and weights are 1.7/3.3/3.4 scope,
  not re-decided here.

---

## 8. Eligibility — integrating 0.2 (frozen)

The lexical candidate query runs as the **service role**, which **bypasses RLS**
(0.2 §7). Every eligibility rule is therefore encoded **inside the SQL**, never
inherited. Frozen predicates:

| Entity | Predicate | Source (0.2) |
|---|---|---|
| message | `discord_messages.is_deleted = false` | D5: `message_feed` omits this; 6,987 deleted msgs currently searchable. **Search `discord_messages` directly, not `message_feed`/`unified_feed`.** |
| distillation | `distillations.status IN ('pending','approved')` | RLS `status <> 'rejected'` + feed branch (net identical). |
| resource (prose) | (no status/soft-delete column) | 0.2 §5: all rows eligible. |
| workflow_python | `kind='workflow' AND hivemind_workflow_python_state(id)='safe'` | 0.8 quarantine gate; quarantined python never indexes. |

**Author opt-out decision (D6).** No live opt-out rule exists to preserve: 0.2
found `members.allow_content_sharing` (4/7,672 = false) is enforced **nowhere** in
the read path, and README/SKILL.md state opt-out is an **HF-dataset-export-only**
concept. **Decision: bind it to an implementation-safe predicate behind an
off-by-default flag `hivemind.author_optout_enabled` (default `false`).** At launch
search behavior is unchanged (no opt-out), preserving current public eligibility
exactly; it can be enabled later without a schema change. This is the explicit
binding the plan asks for, not an open question.

**Bot/system author decision (D8).** 19 bot + 2 system members; the live path
does not exclude them and they carry real workflow discussion. **Decision: include
by default** (preserve current behavior). The same flag family can exclude them
later if policy changes.

**Rejected / ineligible rules encoded:** rejected distillations, soft-deleted
messages, and quarantined workflow Python cannot rank, snippet, or hydrate
(security regression fixture required, plan SQL tests / 1.9 / 1.10).

---

## 9. Snowflake-string boundary (frozen — plan invariant)

Every identity and filter crosses SQL and JSON boundaries as **TEXT**. Discord
snowflakes are `bigint` in the DB but cast to `::text` at the lexical candidate
boundary: `message_id::text`, `channel_id::text`, `author_id::text`, `guild_id::text`
(already done in `unified_feed.item_id`, 0.2). `item_ids` from the Edge function
are validated JSON strings, bound via an **allow-listed identity predicate**, never
interpolated SQL (plan 3.1/3.7). The remaining `bigint`-at-boundary gaps
(`distillation_cites.item_id`, `get_item --id`) are deferred to 2.4 / Phase 4
(0.2 gap #6).

---

## 10. Golden acceptance fixtures — frozen for tasks 1.10 / 1.11

Frozen set: `eval/retrieval/golden/golden-v1.json` — **112 cases** (104 judged + 8
no-hit), version `golden/2026-07-28/v1`, `post_hoc_locked`. Re-judging only via a
versioned v2 sibling. All 23 required families present:

| Family | n | | Family | n |
|---|---:|---|---|---:|
| workflow_code | 32 | | best_is_distillation | 17 |
| exact_name | 25 | | best_is_message | 17 |
| snowflake | 18 | | selective_filter | 11 |
| multi_term | 10 | | paraphrase | 10 |
| best_is_resource | 8 | | single_workflow | 8 |
| spelling_variant | 8 | | workflow_python_evidence | 8 |
| no_hit | 8 | | cross_source | 6 |
| workflow_only | 6 | | code_fragment | 6 |
| settings | 7 | | long_resource_chunk | 4 |
| channel_scoped | 4 | | time_scoped | 4 |
| named_author | 4 | | timeout_prone | 3 |
| pending_status | 2 | | | |

**Blocking gates 1.10/1.11 carry forward (frozen, no post-hoc changes):**

- `exact_identifier Recall@10 ≥ 0.95` and within 0.02 of the best lexical config
  (covers `exact_name` 25 + `spelling_variant` 8: `FLUX.1`, `Wan 2.2`/`Wan2.2`,
  `LTX-Video`, `.gguf`/`.safetensors`, `lightx2v_I2V_14B`).
- `workflow_code exact-match Recall@10 ≥ 0.95` (`workflow_code` 32 + `code_fragment`
  6: node classes `WanVideoSampler`/`IPAdapterFaceIDKolors`/`Flux2Scheduler`,
  model filenames `ltx-2-19b-ic-lora-detailer`, `class `/`def ` fragments).
- **single_workflow:** every judged `single_workflow` query (n=8) returns only its
  `item_id` (incl. adversarial scoped no-hits where a symbol exists globally but
  not in the scoped workflow).
- **workflow_only:** `workflow_only` (n=6) results come from `kind=workflow`.
- **no duplicate Python indexing:** identical workflow Python from body and payload
  indexes exactly once (the 222 `both`-cohort rows).
- **duplicate-item rate after collapse == 0.**
- **no-hit:** `no_hit` (n=8) returns zero (incl. injection-shaped `DROP TABLE
  unified_feed`, emoji, future-date `since`).
- **timeout_prone** (n=3: `WanVideoSampler`, `model`, `controlnet settings`) — the
  legacy 30 s timeouts / HTTP 500 must not recur under indexed lexical search.

---

## 11. Pumpernickel attribution (port, not runtime dependency)

Ported with attribution (no runtime dependency): `websearch_to_tsquery('simple')`;
`ts_rank(..., 32)`; RRF `K=60`; `normalize_query` (NFC + whitespace collapse);
the parallel-arm / merge candidate model. **Rewritten for Hivemind:** all
`mediator.*` SQL; `v_searchable_content`/`v_searchable_messages` (Hivemind uses
`discord_messages`, `external_resources`, `distillations`); UUID source identity →
text `item_id`; Pumpernickel's dyad/topic/partner/OOB visibility rules → Hivemind
eligibility predicates (§8). Hivemind owns the resulting SQL, RPC, tests, and pack.

---

## 12. Deferred items (explicitly assigned to later tasks)

| Item | Task |
|---|---|
| Create weighted `tsvector` columns + `lexical_documents` table + GIN indexes; IMMUTABLE SQL helpers mirroring `workflow_representation.py` | 1.2 |
| Build canonical `simple` index on `discord_messages`; decide `english` index retention/drop (85 MB) | 1.3 |
| Implement + test `normalize_identifier` exact-identifier arm (titles, questions) | 1.4 |
| Bounded trigram indexes on high-value short fields (titles, questions) | 1.5 |
| Full-message exact-identifier path (message trigram vs normalized side index; 0.7/12 GB gate) | 1.6 |
| Lexical candidate SQL combining fts/phrase/ident/code-fragment arms + collapse | 1.7 |
| kind/item_id/source/date/author/channel filters + post-limit hydration | 1.8 |
| Hardened `SECURITY DEFINER` RPC, fixed `search_path`, eligibility, grants, limits | 1.9 |
| SQL plan / unit / integration / timeout / workflow / security / snowflake / order tests; gate verdict | 1.10 / 1.11 |
| Production embedding dimension + final chunk configuration | 2.14 |
| `recoverable`/`unavailable` split + real quarantined count | 2.12 |

None blocks the 1.1 completion signal; each is a named input to a later task.

---

## 13. Self-review against the completion signal and plan criteria

**Completion signal:** *"Decision explicitly resolves `simple` versus existing
`english` behavior and bounded workflow-code documents."*

- **simple vs english resolved** (§1.1): `simple` chosen for all; observed
  config-mismatch `@@`→FALSE, EXPLAIN showing the `english` index is unreachable by
  a `simple` query, and the stemming/stopword/multilingual differences. The
  superseded live index's disposition is fixed (additive 1.3).
- **bounded workflow-code resolved** (§1.2/§6): per-`(resource_id, representation_type,
  chunk_index)` documents, frozen chunker, no-silent-truncation, precedence/dedup/
  quarantine, collapse, refresh/version/hash.

**Plan lexical/query/security/workflow/golden/capacity sections covered:**

- regconfig per entity/representation (§2); exact weighted expressions + null
  handling + IMMUTABLE helpers (§3); query constructors + exact-identifier arm
  (§4); normalization + aliases (§5); bounded code-doc policy (§6); ranking +
  collapse (§7); eligibility incl. opt-out/bot decisions + service-role RLS (§8);
  snowflakes as strings (§9); golden fixtures + gates for 1.10/1.11 (§10);
  Pumpernickel attribution (§11); deferred items (§12).

---

## 14. Reproducibility, tests, and boundary

- **Evidence method.** An isolated, throwaway PostgreSQL 14.15 cluster
  (`initdb --auth=trust`, temp data dir, port 5433, no network) reproduced the
  tokenization/EXPLAIN facts cited above and in the JSON `simple_vs_english_evidence`.
  It was torn down after capture; no production or shared database was touched.
- **Reference module.** `executors/lexical_contract.py` is pure stdlib, offline,
  dependency-free; it is the frozen spec 1.2–1.10 implement against.
- **Validator + tests.** `scripts/validate_lexical_contract.py` checks the JSON
  contract consistency and the reference behaviour on fixtures (including the
  observed simple-vs-english facts and the `Wan 2.2`/`Wan2.2` collapse).
  `tests/test_lexical_contract.py` pins every frozen decision; both green.
- **Boundary.** No migration, index, RPC, Edge function, provider call, or corpus
  change was made; no production mutation; the dirty Hivemind working tree and all
  0.1–0.8 / in-progress artifacts were preserved untouched. This task only adds the
  six files listed above.

## Completion signal (1.1)

> Decision explicitly resolves `simple` versus existing `english` behavior and
> bounded workflow-code documents.

**Met.** This record (2026-07-28) freezes `'simple'::regconfig` for every entity and
representation with observed simple-vs-english evidence (config-mismatch `@@`→FALSE,
EXPLAIN index-unreachability, stemming/stopword/multilingual differences) and fixes
the superseded live `english` index's additive disposition; freezes the exact
weighted `to_tsvector` expressions, query-constructor parallel arms, identifier/query
normalizers, eligibility predicates (incl. the author-opt-out and bot policy
decisions), ranking inputs, chunk collapse, snowflake boundary, the bounded
workflow-code lexical-document policy keyed by `(resource_id, representation_type,
chunk_index)`, and the golden acceptance fixtures/gates for 1.10/1.11. It is
machine-readable and pinned by offline tests + a validator, both green.

## Dependency-safe next tasks

Task 1.1 has no blockers. Per the plan's critical path it unblocks (do **not** start
here — this task stops at 1.1):

- **1.2** — Add weighted lexical documents/GIN indexes for resource prose/code and
  distillations, implementing the frozen §3 expressions and §6 document table
  against `executors/lexical_contract.py` + `executors/workflow_representation.py`.
- **1.3** — Build the canonical `simple` Discord index; decide the `english` index
  retention/drop.
- **1.4** — Implement + test the `normalize_identifier` exact-identifier arm.

Do **not** begin 1.2–1.10 implementation from this task.
