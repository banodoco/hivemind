# Phase 0 — Task 0.8 Workflow Representation Decision Record

**Date (frozen):** 2026-07-28
**Task:** 0.8 — Inventory workflow representation cohorts and freeze the
authoritative Python precedence, delimiters, recovery/quarantine/no-duplication
rules, pilot embedding contracts, prose/code chunk candidates, numeric quality
gates, and rollback criteria.
**Plan:** `docs/architecture/hivemind-hybrid-search-plan.md` (Astrid planning copy).
**Prereqs:** 0.1 access, 0.2 schema/eligibility, 0.3 inventory, 0.4 baseline,
0.5 evaluation harness, 0.6 golden set, 0.7 capacity (all complete; untouched).
**Endpoint ref:** `ujlwuvkrxlvoswwkerdf` · **Evidence date:** 2026-07-28

This is a **freeze / decision record**, not Phase 1 implementation. It classifies
the live workflow representation cohorts with dated counts, fixes the
representation, security, embedding, chunking, evaluation, and rollback contracts,
and hands them to Phase 1/2 as a pinned spec. It creates no migration, index,
Edge function, provider call, or corpus/backfill change, and does not start task
1.1.

## Files produced

| Path | Role |
|---|---|
| `docs/hybrid-search/phase0-workflow-representation-contract.md` | **This decision record** (human). |
| `docs/hybrid-search/phase0-workflow-representation-contract.json` | **Machine-readable contract** (version-pinned). |
| `executors/workflow_representation.py` | **Frozen reference module** (precedence, delimiter grammar, no-duplication, normalization/hashing, secret scanner, chunking). Pure stdlib, offline, dependency-free. |
| `scripts/validate_workflow_contract.py` | **Deterministic validator** (contract JSON consistency + reference-module behaviour on fixtures). |
| `tests/test_workflow_representation.py` | **48 offline tests** pinning every frozen behaviour. |

## Commands

```bash
python3 scripts/validate_workflow_contract.py                         # contract validation (46 checks)
python3 -m unittest tests.test_workflow_representation -v              # this task's tests (48)
python3 -m unittest discover tests/                                    # whole repo
```

The reference module is the frozen spec Phase 1/2 must match; the deployed
SQL/Edge production path (tasks 1.2 / 2.5 / 2.6) is still to be implemented
against it. Reconciliation with VibeComfy used **bounded, read-only discovery**
only — no stored workflow Python was executed and the live corpus was not
modified.

---

## 1. Workflow representation cohorts (frozen classification)

Every eligible `kind=workflow` row resolves to **exactly one authoritative
source cohort**, plus a cross-cutting public-search state:

| Cohort | Definition |
|---|---|
| `payload_python` | Non-empty `payload.python_source` is authoritative. |
| `body_python` | No payload Python, but a recognized body-delimited block exists. |
| `recoverable` | Neither; Python deterministically regenerated via the VibeComfy exporter from `workflow_json`/`compiled_api`/`corpus_path`/provenance. |
| `unavailable` | Neither; no trustworthy Python can be recovered (explicit reason recorded). |

Cross-cutting:

- **`both`** — a duplication *flag*, not a competing cohort. It marks a
  `payload_python` row whose bytes also form the body block. These are
  `payload_python` for precedence and flagged for the no-duplication rule.
- **`quarantined`** — public-search state (`safe` | `quarantined`), independent
  of source cohort. It says whether the authoritative bytes may enter public
  lexical/embedding/snippet/retrieval, not where the bytes came from.

### Counts (2026-07-28, exact over all 2,757 `kind=workflow` rows)

| Cohort / flag | Count |
|---|---:|
| total workflows | **2,757** |
| `payload_python` (incl. `both`) | **222** |
| `body_python` (body-only) | **0** |
| `both` (duplicate body + payload) | **222** |
| `neither` = `recoverable` ∪ `unavailable` | **2,535** |
| `recoverable` | deferred (subset of 2,535) |
| `unavailable` | deferred (subset of 2,535) |
| `quarantined` (under the safe, trap-free policy) | deferred; expected ≈ 0 |

**Matching method.** Cohorts are computed deterministically from
`(payload.python_source non-empty?) × (recognized body block present?)` over all
2,757 rows fetched via PostgREST (counts exact). `both` is `payload_python` rows
whose normalized payload bytes equal a body block (or appear verbatim in body).
The `recoverable`/`unavailable` split is **not observable** from the database —
it requires running the VibeComfy exporter against local artifacts — and is
deferred to the operator remediation job (task 2.12). The `quarantined` count is
deferred because the only task-0.3 scanner reason (`high_entropy_base64`, fired
on all 222) is rejected below as a false-positive trap; the real count must be
measured under the frozen safe policy.

**Evidence timestamps.** Inventory completed `2026-07-28T18:50:48Z`; golden-set
evidence captured `2026-07-28T19:15Z`; legacy baseline captured
`2026-07-28T19:01:24Z`.

**Uncertainty.** The `recoverable`/`unavailable` split of the 2,535 `neither`
rows is unknown. The 0.2 schema map's bounded sample (first 200 workflows) found
199/200 carry `corpus_path`, suggesting `recoverable` is likely the majority but
this is **unconfirmed**. The real `quarantined` count is unknown; expected near
zero because the 0.3 reason was a false positive, but real secrets cannot be
ruled out without running the safe-policy scan. Two data-quality anomalies
(`source='s'`, `source='comfyui'`) are out of scope for cohort math.

**How historical rows move between states (frozen).**
`recoverable → payload_python` when remediation materializes
`payload.python_source` (idempotent, high-water monotonic); `body_python →
payload_python` when the body block is promoted into payload; `unavailable →`
another cohort when an authoritative source artifact later appears (new
ingestion / operator repair) with `external_resources.id` unchanged; `both` →
deduplicated when remediation re-renders body with the Python block present
exactly once; `safe ↔ quarantined` only via re-scan on content change, and
quarantine never auto-clears without operator review and secret rotation.

### Headline consequence

In the **current** corpus, all 222 Python-bearing workflows are the `both`
duplicated case: the same Python lives in `payload.python_source` **and** under a
body delimiter. There are zero `payload`-only and zero `body`-only rows today.
The no-duplication rule's present-day effect is therefore: for these 222, strip
the body block and treat `payload.python_source` as authoritative. The precedence
still handles all four states for future ingestion.

---

## 2. Canonical Python precedence (frozen exactly)

Strict precedence for the authoritative `workflow_python` bytes (plan AD-4):

1. **Non-empty `payload.python_source`** is authoritative.
2. **Else** an exact recognized body-delimited Python block.
3. **Else** `unavailable` (or `recoverable` once the exporter runs).

This authoritative source is what feeds the `workflow_python` embedding
representation, the full-code lexical document, and `get_item representation=python`.
A second copy is never recovered from prose.

---

## 3. Delimiter grammar (frozen)

Recognized body delimiters, **exact VibeComfy literals** (read-only discovery;
not imported at runtime):

| Delimiter | VibeComfy source |
|---|---|
| `Python ready-template source:` | `scripts/upload_ready_templates_to_hivemind.py:242` |
| `Python scratchpad source:` | `scripts/upload_external_workflows_to_hivemind.py:744` |

**Grammar.** A block starts at a line whose `rstrip()` equals a recognized
literal; the Python text runs until the next recognized delimiter, the
`Workflow semantics (rule-based):` marker, or end of body. The delimiter line is
not part of the returned Python. **No markdown fence** is emitted by VibeComfy.
A near-miss (`python scratchpad source:`, `Python source:`) is ignored, so an
unrecognized block never silently masquerades as Python. VibeComfy appends Python
at the **end** of the body, so the block is the trailing region; preceding prose
survives the strip. Emitter headers (`# vibecomfy: generated` / `… scratchpad` /
`… external workflow archive`) are provenance signals, not delimiters.
Delimiter-grammar version: `delimiters/v1`.

---

## 4. No-duplication + canonical prose (frozen)

- When `payload.python_source` bytes also form the body block, **remove that
  block from the canonical prose** before hashing/embedding/indexing. Never embed
  or index both copies.
- **Canonical prose** = title + body-with-python-blocks-removed + projected
  `workflow_semantics`.
- **Canonical Python** = authoritative bytes only (one copy).
- Hashing: `representation_hash` over the full canonical representation;
  `chunk_hash` per chunk (independent columns).
- **Gate:** count of `(workflow, representation bytes)` embedded or indexed more
  than once == 0.

---

## 5. Identity, version, hash, refresh triggers (frozen)

- **Embedding identity:** `(contract_id, entity_type=resource,
  item_id=external_resources.id::text, representation_type ∈ {prose,
  workflow_python}, chunk_index)`.
- **Immutable identity:** `entity_type + item_id`. `representation_type` and
  `chunk_index` are representation identity, not row identity.
- **Version fields:** `canonicalization_version`, `chunking_version`,
  `secret_scan_version` (all `1`), mirrored in `embedding_contracts`.
- **Hash:** SHA-256 of UTF-8 frozen-normalized text. Normalization (frozen):
  Unicode NFC; CRLF/CR → LF; per-line trailing whitespace stripped; stable single
  trailing-newline convention. Identical inputs always yield identical hashes.
- **Refresh triggers:** any change to payload Python, body-block bytes,
  title/description/tags/`workflow_semantics`, a version bump, or a
  `safe↔quarantined` transition → re-canonicalize + re-hash; **hash-skip** if
  unchanged (idempotent).
- **Structured projection:** `workflow_semantics` (from
  `executors/workflow_semantics.py`, v1) projected into prose — `media_type,
  task_type, model_families, adapter_directions, node_types, node_class_multiset,
  custom_nodes, models, searchable_aliases`. Arbitrary `payload`/`workflow_json`/
  `compiled_api` are never embedded wholesale.

---

## 6. Safe retrieval / snippet behavior (frozen)

- `matched_snippet`: ≤240-char window around the match, secret-redacted via the
  same scanner (defense in depth); never the matched secret value.
- `get_item representation=python`: whitelisted full `payload.python_source` (or
  recovered authoritative) + version/hash; rejects if not `kind=workflow`;
  **fails closed if quarantined**; never returns arbitrary `payload`,
  `corpus_path`, `workflow_json`, or `compiled_api`.
- Quarantined Python is excluded from lexical, embeddings, snippets, and
  `get_item`.

---

## 7. Secret scanning / quarantine (frozen — the task-0.3 trap fix)

This is the heart of the freeze. The task-0.3 inventory flagged all 222
Python-bearing workflows with the reason `high_entropy_base64` because
VibeComfy-generated source legitimately embeds large base64/JSON literals. **The
frozen policy removes `high_entropy_base64` as a reason code.** A bare
high-entropy run with no credential context is benign. A match requires
**structure**:

| Reason code | Trigger |
|---|---|
| `private_key_block` | PEM/SSH/PGP headers (`-----BEGIN ... PRIVATE KEY-----`) |
| `provider_token_prefix` | `sk-`/`sk-proj-`, `gh[pousr]_`, `xox[abprs]-`, `AKIA`/`ASIA`, `AIza`, `sk_live_`/`rk_live_`, `sb_secret_` (public `sb_publishable_` excluded), `hm_<64hex>`, structured `eyJ…eyJ…sig` JWT |
| `credential_assignment` | credential-keyword LHS (`api_key`/`secret`/`token`/`client_secret`/…) assigned a quoted high-entropy string ≥16 chars |
| `credential_url` | `scheme://user:password@host` with a non-placeholder password ≥6 chars |
| `aws_env_var` | `AWS_(ACCESS_KEY_ID|SECRET_ACCESS_KEY)=` with a plausible value |

**Fail-closed rule.** Any hit quarantines the representation from public
lexical/embedding/snippet/full-code retrieval. New ingestion fails closed;
historical hits enter the documented remove/rotate incident path (not silently
copied into a new index).

**Logging rule.** Never log, echo, serialize, hash-to-client, or include the
matched value in metadata/reports/snippets/reason objects. Record only
`{reason_code, bounded position, scanner_version}`.

**Validation gates (pinned by tests + the validator):** false-positive rate on a
generated-code fixture == 0 (the trap fix); true-positive == 100% with the
correct reason code for each planted secret (PEM, `sk-`, `ghp_`, `xox`, `AKIA`,
`AIza`, `hm_`, JWT, `client_secret =`, `:pass@`, AWS env); matched-value leakage
== 0. The reference module (`scan_secrets`) and the validator prove these on a
representative VibeComfy fixture and the planted-secret set.

---

## 8. Recovery rules (frozen — VibeComfy exporters, no LLM / no execution)

- **Reuse VibeComfy's exporters** — `emit_ready_template_python` /
  `emit_scratchpad_python` / `_fallback_python_archive_source` via
  `port_convert_workflow` / `load_workflow_any`. No second generator in Hivemind;
  no LLM inference; **no execution of stored Python**.
- **No overwrite of authoritative source:** never overwrite a non-empty
  `payload.python_source`. Recovery fills empty payload from a body delimiter, or
  regenerates from `workflow_json`/`compiled_api`/`corpus_path`/provenance when
  both payload and body lack Python.
- **`corpus_path`** resolved only in an authorized operator job with an explicit
  VibeComfy root; Edge/search requests never read local paths.
- **Explicit unavailable reasons:** `no_source_artifact`,
  `corpus_path_unresolvable`, `conversion_error`, `exporter_unavailable`,
  `secret_quarantine_blocks_public`.
- **Durable / idempotent / monotonic:** persist run id, cursor
  (`external_resources.id` asc = high-water boundary), per-cohort and
  per-public-state counters, retryable per-row failures, and a final
  reconciliation report. Idempotent (hash-skip); never restore an older
  representation over a newer authoritative source; refresh lexical state only
  after the source-row patch commits.

The reference module exposes `resolve_authoritative_python(row, recoverer=…)` so
the recoverable/unavailable behavior is unit-testable **offline** (the recoverer
is injected; in production it wraps VibeComfy). This keeps the freeze testable
without importing VibeComfy or executing code.

---

## 9. Pilot embedding contracts (frozen — without preselecting task 2.14's winner)

- **Candidates (frozen for the pilot only):**
  - `text-embedding-3-small` @ **384-d** (`dimensions=384`) — capacity-preferred
    (0.7: full corpus **4.59 GB PASS** under the 12 GB gate).
  - `text-embedding-3-small` @ **1536-d** (native) — quality fallback (0.7: full
    corpus **16.4 GB FAIL**; viable only at pilot/cohort scope).
  - Quality alternative noted (not pilot-default): `text-embedding-3-large`.
- **Representation types:** `prose`, `workflow_python` (part of embedding
  identity).
- **Contract identity:** `contract_id = provider + model + dimension +
  canonicalization_version + chunking_version`; one active contract per
  dimension; dimension migration via a separate fixed-dim table + HNSW index +
  atomic active-contract switch only after replacement coverage passes.
- **Vector validation:** finite, no NaN/Inf, exact dimension D, L2-normalized at
  write and query; wrong-dimension → fail closed (skip + count + retry); cosine
  via `<=>`.
- **Query normalization:** same text normalization as corpus canonicalization;
  embed once per request; timeout ~400 ms; best-effort per-instance cache;
  failure → lexical degradation + `semantic_degraded`.
- **Server-side secret boundary:** provider key only in Hivemind's Edge secret
  store (`OPENAI_API_KEY`, task 2.1 setup action) for query embeddings; the
  operator-run backfill reads the approved provider credential from an authorized
  local/managed environment; never in pack/repo/client; deterministic fake
  embedder for unit/contract tests.
- **Deferred to 2.14:** production dimension and final chunk configuration.

---

## 10. Chunk candidates (frozen — at least two per representation type)

Token estimate: chars/4 (frozen planner estimate; real tokenizer in task 2.6).

| Config | Mode | Target | Overlap | Method |
|---|---|---|---|---|
| `prose_512` | prose | 512 tok | 50 tok | paragraph-aware |
| `prose_1024` | prose | 1024 tok | 100 tok | paragraph-aware |
| `python_512` | python | 512 tok | 50 tok | AST top-level alignment |
| `python_2048` | python | 2048 tok | 100 tok | AST top-level alignment |

- **Overlap/offsets:** record `chunk_index`, content `chunk_hash`, method, stable
  source offsets; deterministic → repeated runs identical (pinned by tests).
- **Parser fallback:** on `ast.parse` failure or a single literal node exceeding
  the budget (generated archives with huge literals), fall back to a bounded
  line/token window with overlap; **never require execution**.
- **Best-chunk collapse:** one best chunk per `(entity_type, item_id)` across
  representation types before RRF.
- **No silent truncation (frozen):** never silently truncate searchable or
  embeddable Python; split into bounded chunks. Measured worst case
  `payload.python_source` max = **1,415,262 chars** (~354K tokens) → ~76 chunks
  at 512 tokens. The reference module's `coverage_ok` guard is pinned by a test
  on this size.
- **Bounded lexical code-document policy (frozen):** full canonical Python
  participates in lexical retrieval (imports, symbols, node classes, model
  filenames, kwargs, fragments) via a bounded code-document arm; over-limit
  documents split per `(resource_id, representation_type, chunk_index)` and
  collapse to the resource identity before global ranking; never silent-truncate.

**Measured workflow sizes (frozen inputs from 0.3):**

| Field | n | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|
| prose body (chars) | 2,757 | 1,613 | 13,898 | 345,703 | 1,417,640 |
| payload python (chars) | 222 | 95,216 | 448,574 | 719,694 | 1,415,262 |
| body-extracted python (chars) | 222 | 96,027 | 450,192 | 720,821 | 1,416,828 |

---

## 11. Numeric gates (frozen — no post-hoc threshold changes)

All gates are pinned in the contract JSON with `post_hoc_locked: true`; the
validator refuses a contract without that flag. Thresholds are fixed **before**
Phase 1/2/3 runs against the frozen 112-case golden set (`golden/v1`).

**Quality (Q1–Q16):** Hybrid Recall@10 ≥ ILIKE + 0.15; Hybrid MRR ≥ ILIKE + 0.10;
Exact-identifier Recall@10 ≥ 0.95 and ≥ best-lexical − 0.02; overall Hybrid
Recall@10 ≥ max(lexical, semantic) − 0.01; duplicate-item rate after collapse ==
0; every response ≤ requested global limit; forced embedding failure → lexical in
100% of valid cases and every response `semantic_degraded`; warm p95 ≤ 1.0 s /
p50 ≤ 500 ms; e2e p95 incl. Edge cold start ≤ 2.0 s; lexical/degraded p95 ≤
750 ms; non-validation search error < 0.5% (load); unplanned semantic degradation
< 2% (provider healthy); **workflow-code exact-match Recall@10 ≥ 0.95**
(`workflow_code`, n=32); every judged single-workflow query returns only its
`item_id` (n=8); duplicate indexing of identical workflow Python from body and
payload == 0.

**Security (S1–S4):** quarantined Python never ranks/snippets/embeds/`get_item`
(security regression fixture); matched secret values in logs/metadata == 0;
scanner false-positive on generated-code fixture == 0; scanner true-positive ==
100% per planted secret with the correct reason code.

**Coverage (C1–C6):** active-contract coverage == 100% for eligible distillations
+ resources and ≥ 95% for the enabled message cohort; every recoverable workflow
has materialized+versioned+hashed Python; every unavailable row has an explicit
non-secret reason; stale/mismatched representation hashes < 0.1%; any ineligible
indexed item = release blocker.

**Capacity/cost (P1–P3, from 0.7):** pilot embedding API spend ≤ $25 (PASS both
dims); projected vector-table + HNSW storage ≤ 12 GB (384-d **PASS** 4.59 GB,
1536-d **FAIL** 16.4 GB); projected search-infra recurring ≤ $50/month
(384-d-full **CONDITIONAL**, latency-gated to 2.16/3.10).

**Golden set (frozen scope):** 112 cases (104 judged + 8 no-hit); all 23 required
families present (`workflow_code` 32, `exact_name` 25, `snowflake` 18,
`best_is_distillation` 17, `best_is_message` 17, `selective_filter` 11,
`paraphrase` 10, `multi_term` 10, `best_is_resource` 8, `spelling_variant` 8,
`workflow_python_evidence` 8, `single_workflow` 8, `no_hit` 8, `settings` 7,
`code_fragment` 6, `cross_source` 6, `workflow_only` 6, `named_author` 4,
`channel_scoped` 4, `time_scoped` 4, `long_resource_chunk` 4, `timeout_prone` 3,
`pending_status` 2). Re-judging only via a versioned v2 sibling.

---

## 12. Rollback / stop criteria (frozen — additive schema, source rows untouched)

**Invariants (all stages):** schema additive only; source rows never modified or
deleted on rollback; failed/stale jobs remain inspectable and retryable; durable
cursors preserved; never roll back by deleting source content.

- **Lexical (Phase 1):** roll back via client `mode=legacy` (two-pass ILIKE). The
  RPC/tsvector columns/indexes are additive; drop or rebuild indexes in a
  maintenance window. **Stop** if exact-identifier Recall@10 < 0.95, workflow-code
  exact-match < 0.95, or non-validation error ≥ 0.5%.
- **Embedding pilot (Phase 2):** keep vectors; disable semantic retrieval (no
  deletion) on quality regression or a capacity/cost gate; resume from the
  durable cursor + hashes. **Stop** at any capacity gate ($25 / 12 GB / $50-mo),
  coverage below gate, or stale-hash ≥ 0.1%.
- **Workflow remediation:** separately resumable from its own cursor; never
  restore older over newer authoritative source; high-water monotonic; on rollback
  leave payload untouched and mark the run superseded. **Stop** if any non-empty
  payload is overwritten or any unavailable/quarantined row lacks a reason.
- **HNSW (Phase 5.6):** drop or rebuild the index in an approved maintenance
  window; retain embedding rows for diagnosis; never delete source. **Stop** on
  capacity pressure or destabilizing ingestion/search.
- **Edge (Phase 3):** per-request degradation to lexical; secrets stay
  server-side; roll back to lexical/legacy via flag. **Stop** immediately on any
  credential leak, or on error/latency gate failure.
- **Canary (Phase 6.1):** retain lexical + legacy overrides; one-config rollback.
  **Stop** on any release-blocking metric.
- **Production backfill (Phase 5):** cohort-gated with pause/resume; source data
  never modified. **Stop** at any cohort gate or storage/cost gate.

---

## 13. Self-review against the plan's acceptance criteria

Each plan acceptance criterion touching workflow representation is covered by a
frozen decision above:

- *`kinds=["workflow"]` / bounded string `item_ids` constrain to one workflow.*
  Identity + retrieval contract (§5, §6); single-workflow gate Q15.
- *Workflow lexical retrieval searches exact canonical Python (imports, symbols,
  node classes, model filenames, kwargs, fragments).* Precedence (§2) + bounded
  code-document policy (§10); workflow-code gate Q14.
- *Workflow semantic uses separately labeled `prose` and `workflow_python`,
  collapsed to one workflow result.* Representation types (§5, §9) + best-chunk
  collapse (§10).
- *Identical Python in `payload` and `body` indexed/embedded exactly once.* §4 +
  gate Q16/S-free; pinned by the no-duplication test.
- *Python-source changes refresh lexical state and enqueue re-embedding without
  changing workflow identity.* §5 refresh triggers + immutable identity.
- *Every recoverable historical workflow has materialized, versioned, hashed
  Python; every unavailable row has an explicit reason and stays prose-searchable.*
  §8 + coverage gates C3/C4.
- *Python hits return a bounded matched snippet; `get_item representation=python`
  returns whitelisted full code without arbitrary payload.* §6.
- *Workflow Python matching the scanner is quarantined before lexical/embedding/
  snippet/full-code retrieval; logs/metadata never contain the secret.* §7 +
  security gates S1–S4.
- *Messages/distillations not redundantly chunked.* §10 (only pathological
  overlong messages may use the resource chunker, observably).
- *Long resources match through a non-leading chunk.* §10 chunking + coverage;
  `long_resource_chunk` (n=4) in the golden set.
- *Snowflakes survive JSON round-trips.* Identity is `id::text` (§5); full
  cite/`get_item` string boundary deferred to 2.4/Phase 4 (§14).
- *Semantic failure returns lexical with `semantic_degraded: true`.* §9 + gate Q7.
- *No credential exposed to the pack.* §9 secret boundary.
- *Backfill resumable/idempotent/hash-aware/bounded; remediation separately
  resumable and before resource embedding.* §8 + rollback §12.
- *Rollback to lexical/legacy rehearsed.* §12.

---

## 14. Deferred items (explicitly assigned to later tasks)

| Item | Task |
|---|---|
| FTS config `simple` vs existing `english` (live index is `english`) | 1.1 |
| Production embedding dimension + final chunk configuration | 2.14 |
| HNSW tuning; resolve the 384-d `$50/mo` latency tier | 2.16 / 3.10 |
| `recoverable` vs `unavailable` exact split + real `quarantined` count | 2.12 |
| Soft-delete `is_deleted=false` filter (message cohort, from 0.2 D5) | 1.7 / 1.8 / 1.9 |
| Author opt-out + bot/system message policy (from 0.2 D6/D8) | Phase 1 |
| Snowflake string boundary in `distillation_cites` + `get_item --id` (0.2) | 2.4 / Phase 4 |
| Full-project DB size measurement before index build (0.7) | 5.2 |

None of these block the 0.8 completion signal; each is a named input to a later
task.

---

## Reproducibility, safety, and boundary

- **Offline only.** No network, database, provider call, VibeComfy import, or
  stored-Python execution. No migration, index, Edge function, secret, or corpus
  change. The frozen reference module is pure stdlib.
- **Read-only VibeComfy reconciliation.** The two body delimiters and exporter
  entrypoints were confirmed by bounded, read-only discovery of the VibeComfy
  repository; nothing was executed or modified, and VibeComfy/Pumpernickel remain
  read-only references.
- **Dirty tree preserved.** The pre-existing dirty Hivemind working tree and all
  completed 0.1–0.7 artifacts were left untouched. This task only **adds** new
  files: this record, the contract JSON, the reference module, the validator, and
  the test file.

## Completion signal (0.8)

> A dated decision record classifies the workflow cohorts and fixes the
> representation, security, embedding, chunking, evaluation, and rollback
> contracts.

**Met.** This record (2026-07-28) classifies the four source cohorts with exact
counts, matching method, evidence timestamps, uncertainty, and state transitions;
freezes Python precedence, the delimiter grammar, the no-duplication rule,
identity/version/hash/refresh, safe retrieval, the secret-scanner trap fix,
VibeComfy recovery, pilot embedding candidates, ≥2 prose/code chunk
configurations with the no-silent-truncation guard, the numeric
quality/security/coverage/capacity gates (no post-hoc changes), and the per-stage
rollback/stop criteria. It self-reviews every relevant acceptance criterion and
lists deferred items with their target tasks. The contract is machine-readable
and pinned by 48 offline tests + a 46-check validator, both green.

## Dependency-safe next tasks

Task 0.8 has no blockers and is the last Phase-0 task. Per the plan's critical
path it unblocks (do **not** start here — this task stops at 0.8):

- **1.1** — Choose the canonical FTS configuration and exact indexed expressions
  for all entity/representation types (the delimiter/precedence/dedup contract
  here is its input).
- **1.2** — Add weighted lexical documents/GIN indexes for resource prose/code,
  including workflow-Python precedence, secret scanning/quarantine, dedup, and
  bounded code documents (implement against this frozen contract).
- **2.5 / 2.6** — Canonical representations/hashes and representation-aware
  chunking (implement against the frozen reference module).
- **2.12** — Resumable historical backfill with the workflow-representation
  remediation/reconciliation first stage (resolves the recoverable/unavailable
  split and the real quarantined count).

Do **not** begin Phase 1 implementation from this task.
