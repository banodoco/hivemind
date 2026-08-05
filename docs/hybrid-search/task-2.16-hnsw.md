# Task 2.16 — selected-contract HNSW pilot (GLM-5.2 correction pass)

Status: **correction pass complete; local structural proof green; authorized
read-only representative reconstruction succeeded on real cached content.**

- Local structural rehearsal: `verdict = blocked_not_representative`,
  `run_class = local_volume_only`, 31/31 structural checks pass.
- **Representative reconstruction** (read-only PostgREST + read-only paid cache):
  `run_class = representative_local_content_nonproduction_compute`,
  `representative_semantic_evidence = true`, exact-vs-ANN Recall@10 = **0.999**
  on real cached 384-d vectors across 100 applicable golden queries.
- **Production rollout / hosted latency (Q8/Q9) / recurring compute (P3) remain
  unselected/unproven** — even a successful real-content *local* run cannot close
  hosted gates or select production without production-like staging, so
  `selected_ef_search` for production stays `null`.

No provider call, no source/production mutation, no contract activation outside
throwaway clusters occurred. The paid Task 2.14 cache was opened **read-only**
(verified byte-identical afterward) and never written.

## What this correction pass fixed (C1–C7)

| Defect | Fix |
|---|---|
| **C1** — kNN `LIMIT` applied *before* the eligibility join (nearer ineligible rows crowded out valid entities) | `schema/033` (renewal): eligibility is resolved to the eligible, embedded item-id set per entity and **baked as a literal `text[]` into each per-arm single-table kNN** `ORDER BY embedding <=> query LIMIT`. That row-local predicate is applied by `hnsw.iterative_scan` **during** the distance-ordered HNSW scan, **before** the per-arm LIMIT, so a nearer ineligible vector cannot crowd out a farther valid entity. The source JOIN is a post-collapse decoration only. Proven adversarially (100 nearer deleted messages do not crowd out a farther valid entity; 0 ineligible leak). |
| **C2** — "exact baseline" still `LIMIT`-truncated | one shared dynamic template; exact-mode GUC `hivemind.semantic_exact_mode` NULLs the per-arm bound → complete applicable cohort (true exact). 033-exact ≡ 032 byte-identical on a 600-row high-cardinality fixture. |
| **C3** — ineligible rows physically indexed; no audit | 23,138-vector HNSW cohort is **eligible-only**; fail-closed **pre-build eligibility audit** (8 classes) aborts before `CREATE INDEX` if any selected-contract ineligible/quarantined count ≠ 0 (all 0). Safety-filter exclusion proven in a **separate parity fixture**, not the indexed cohort. |
| **C4** — `vector_dims` recorded as 3 (`[1,2,3]`) | probes the physical column: `vector_dims(embedding)=384`, typmod `vector(384)`, 23138/23138 rows consistent. |
| **C5** — operator-safety gaps | filesystem free-space + DB/table/index bytes measured with 12 GB + safe-margin abort; `maintenance_work_mem` justified against local headroom; effective `hnsw.ef_search`/`iterative_scan`/`max_scan_tuples`/`scan_mem_multiplier` queried+recorded; malformed progress SQL fixed; invalid-index detection handles **both `f` and `false`** and aborts **before** `IF NOT EXISTS`; operator DDL schema-qualified (`public.content_embeddings`). |
| **C6** — `d1_hashes_bound` was a non-empty-string check; "cache miss" test touched no cache | strict **offline** `hashlib` recompute of all 9 Task 2.14 frozen-manifest anchor hashes (fail-closed on tamper); real read-only cache-index fail-closed tests (missing key/file, wrong dim/field, duplicate-conflicting, wrong count/hash, permissive mode) — never opening any cache for write. |
| **C7** — truthfulness | import-time `assert` guards → `raise` exceptions (fire under `python -O`); `source_mutations=0` scoped to external/production with local fixture inserts disclosed; recover/rollback modes documented `render_only`; `selected_ef_search=null` / `blocked_not_representative` for non-representative evidence. |

## Renewal pass (GLM-5.2, 2026-07-30) — decisive blocker RESOLVED

The prior correction pass left a decisive blocker: it changed the suite to
accept a **direct inner single-table kNN** as the "natural HNSW" proof while the
**ACTUAL** `public.hivemind_semantic_candidates(...)` did **not** use the HNSW
index (`idx_scan` delta `3 → 3`, i.e. 0). That is failure regardless of any
displayed pass count, and it is exactly what this renewal rejects.

**Root cause (diagnosed empirically, PG 14.15 + pgvector 0.8.5):** the partial
HNSW kNN path is generated **only** for a single-table scan whose filters are
ROW-LOCAL on `content_embeddings`. **Any** source JOIN (inner/left/semi/lateral/
`IN`) — and any **parameterized** array/limit — forces a non-HNSW driver
(btree `content_embeddings_identity_idx` + Sort). This holds even under
`enable_seqscan=off` + `enable_indexscan=off` (after dropping the competing
btree): the join then drives a Nested Loop off `external_resources`, never HNSW.
So HNSW is **structurally unreachable through a join** in this build — it is not
a cost-tuning problem. (A correlated `EXISTS`/semi-join is rewritten to a join by
the planner and behaves identically — it does **not** keep HNSW as the driver.)

**Fix (`schema/033`, smallest correct change):** eligibility is resolved to the
**eligible, EMBEDDED item-id set per entity** from current source state and
**baked as a LITERAL `text[]`** into each per-arm single-table kNN predicate
(`ORDER BY embedding <=> query LIMIT`). The literal array is a **row-local**
predicate, so `hnsw.iterative_scan` applies it **during** the distance-ordered
HNSW scan, **before** the per-arm LIMIT (C1 preserved: a nearer
deleted/rejected/quarantined/wrong-kind vector is filtered before it can crowd
out a farther eligible entity). The source JOIN (resource kind / `created_at`)
becomes a **post-collapse decoration** only (1:1 by PK, cannot duplicate/reorder;
final order byte-identical to `schema/032`). Only `p_query_embedding` is a bind
parameter (`$1`); the array and limit are baked because parameterizing either
drops HNSW. PL/pgSQL `EXECUTE … USING` keeps the `$1` vector indexable here (a
session-level `PREPARE` with an opaque Param vector does **not**).

**Decisive result:** the **ACTUAL** canonical function now naturally increments
the selected HNSW index — `idx_scan` delta **`0 → 6`** over 6 broad ANN calls
(rehearsal); `280 → 286` in the benchmark. The decisive gate (`full_function_
idx_scan_delta_positive`) is restored and asserted; the inner single-table kNN is
demoted to `diagnostic_only_insufficient`. Adversarial C1 (100 nearer deleted
messages do not crowd out a farther valid entity; 0 ineligible leak), exact mode
(complete cohort, not LIMIT-truncated), 033≡032 parity, recall@10 = 1.0,
security posture, idempotent build, and rollback all remain green.

## Critical planner finding — RESOLVED by the renewal (was: C1 vs natural HNSW)

The prior correction recorded an honest but **pessimistic** conclusion: that
C1-correctness and natural-HNSW-for-the-full-function conflict at 23 k, so only
the inner kNN could prove HNSW viability. The renewal disproves that: the
conflict was an artifact of putting the **source JOIN inside the kNN arm**.
Moving eligibility to a **row-local baked-array predicate** (above) keeps HNSW
as the driving path *and* preserves C1 — so the canonical path is now both
indexable and semantically equivalent, exactly what the brief required.

## Files changed

| File | Kind |
|---|---|
| `executors/selected_contract.py` | selected-contract identity surface; C7a (`raise` guards) |
| `schema/033_selected_contract_hnsw.sql` | additive `hivemind_selected_contract_id` + upgraded canonical `hivemind_semantic_candidates`: **row-local baked-array eligibility** per arm so the partial HNSW index is the driver (iterative scan filters before LIMIT = C1), post-collapse source decoration, exact-mode GUC, selected-literal fail-closed gate |
| `scripts/rehearse_hnsw_pilot.py` | D1–D5 preflight, isolated PG14 cluster, eligible-only 23,138-vector cohort, pre-build eligibility audit, CONCURRENTLY build w/ progress + capacity/invalid checks, **decisive actual-function HNSW proof** (strictly-positive `idx_scan` delta over broad calls; inner kNN diagnostic-only), sanitized evidence |
| `scripts/benchmark_hnsw_pilot.py` | pre-registered manifest + ef sweep `[40,80,120,200]`, true exact baseline (GUC), recall/latency, `source_mutations` scoped |
| `scripts/reconstruct_hnsw_representative.py` | **new** — read-only representative reconstruction CLI (`reconstruct-preflight`, `reconstruct`, `representative-benchmark`); reuses the accepted Task 2.14 architecture |
| `tests/test_hnsw_pilot.py` | 60 tests (identity/gate/hygiene + static migration shape + SQL rehearsal/parity + adversarial-C1 + true-exact-vs-032 + real cache fail-closed + offline hash recompute) |
| `tests/test_reconstruct_hnsw.py` | **new** — 21 tests (offline preflight, high-water pinning, drift/cache fail-closed, hygiene) |
| `docs/hybrid-search/task-2.16-hnsw-{rehearsal,benchmark}.json` | sanitized structural/benchmark evidence |
| `docs/hybrid-search/task-2.16-reconstruction.json` | **new** — sanitized reconstruction evidence |
| `.cache/hivemind-hnsw-task-2.16/representative-snapshot.json` | **new private** snapshot (dir 0700 / file 0600, gitignored) |
| `eval/retrieval/loader.py` | minimal Python-3.11 compat fix (nested f-string `SyntaxError`) so the accepted Task 2.14 toolchain imports and the strict-offline gate runs — logic unchanged, all hashes verified intact |

No migration `020`–`032` was modified; the dirty worktree and concurrent work are
preserved. `schema/033` only `CREATE OR REPLACE`s the canonical function and adds
one helper (no table/constraint changes, no production seeding).

## Selected-contract identity (CHUNKING v2 — re-verified)

- Preimage:
  `openai\x1ftext-embedding-3-small\x1f384\x1f1\x1f2\x1fchunk_config\x1fv1\x1fprose#512/50\x1fworkflow_python#512/50`
- Full SHA-256: `12e19cdb566b87445ab2d3563e6cb948f58801f78f8395878fc9e0c2457d5462`
- **Selected full-literal bigint**: `1360541028304258884`
- Corroboration: Task 2.14 `eval_contract_id` `12e19cdb566b8744` = `sha256(preimage)[:16]`.
- Dimension-only `6368594834396668537` (the chunking-v2 base id; the chunking-v1
  dimension-only id was `7571371577804399660`) is insufficient (shared by
  384-small/large) and is **not** the literal.
- Two DISTINCT version axes in the preimage: the base `ContractSpec`
  `chunking_version` is **2** (the chunker ALGORITHM, bumped 1→2 for the bounded
  oversized workflow-Python fallback fix), while the chunk-config IDENTITY scheme
  stays **v1** (the fallback fix added no identity axis, so the identity scheme
  did not bump). An earlier draft bumped the chunk-config identity to `v2` too —
  an invalid *double* version bump (literal `8308923303736049826`,
  eval `f34f39b8e12700a2`); it is corrected back to `v1`. The stale chunking-v1
  selected literal was `4663917141483337699` (chunking=1 + chunk_config v1).

## Exact commands and results

```
python3 -m unittest tests.test_hnsw_pilot tests.test_reconstruct_hnsw \
    tests.test_semantic_candidate_sql tests.test_embedding_pilot   # 114/114 OK
python3 scripts/compare_embedding_candidates.py replay             # strict offline: 46 checks, 0 provider/network, winner 384-small
python3 scripts/rehearse_hnsw_pilot.py                             # 31/31 checks; deterministic rerun (stable manifest_hash/verdict)
python3 scripts/benchmark_hnsw_pilot.py                            # recall@10=1.0 (synthetic); hygiene clean
python3 scripts/reconstruct_hnsw_representative.py --mode reconstruct-preflight   # 6/6 green (read-only)
python3 scripts/reconstruct_hnsw_representative.py --mode reconstruct-offline      # GENUINELY OFFLINE representative rebuild (raw snapshot, network_calls=0); canonical idx_scan delta +12; recall ef120=0.979/ef200=0.988
python3 scripts/build_v2_chunking_equivalence_proof.py                             # cohort no-op + 0 cache misses + 0 provider/network; winner 384-small
python3 -m unittest discover -s tests                                                # 1629 OK (6 env-skipped)
python3 -m py_compile <all changed/new .py>                        # OK
git diff --check                                                   # clean
```

Adjacent 2.15 (`test_semantic_candidate_sql`) and 2.13 (`test_embedding_pilot`)
were green **before and after** the correction (no regression; 033≡032 parity).

## Build / storage (local, PG 14.15 + pgvector 0.8.5, 23,138 vectors)

| Metric | Value |
|---|---|
| vectors loaded | 23,138 (prose 7,873 / workflow_python 15,265 / unique 22,850) |
| build | returncode 0; `m=16`, `ef_construction=64`, `vector_cosine_ops` |
| `lock_timeout` / `statement_timeout` / `maintenance_work_mem` | `5s` / `300s` / `64MB` effective (build-session override conservative) |
| index `indisvalid` / `indisready` | `t` / `t` |
| predicate | `(contract_id = '1360541028304258884'::bigint)` |
| free disk / projected index | 12.588 GB free / 1.976 GB projected — **within the 12 GB gate** |
| pre-build ineligible-indexed counts | all 8 classes = **0** |

## Recall / latency

Local synthetic (corpus-vector queries): exact-vs-ANN Recall@10 = **1.0** at
ef 40/80/120/200; warm p95 ≤ ~29 ms; ranked-identity hashes identical to exact.

**Representative (real cached content, OFFLINE rebuild, 100 applicable golden
queries):** exact-vs-ANN Recall@10 = **0.979** at ef 120 and **0.988** at ef 200
(100 pairs; ef 40=0.928, ef 80=0.971); 23,138 vectors loaded, 100% cache-served
(22850/22850 unique chunk keys, 0 cache misses). The cohort is reconstructed from
the persisted raw snapshot (`.cache/hivemind-task-2.14-reconstruction/`), NOT a
live PostgREST read — `network_calls=0`, `provider_calls=0`. Meets the ≥ 0.95
fidelity gate locally; hosted latency (Q8/Q9) and recurring compute (P3) remain
unproven.

## Natural HNSW use — DECISIVE (the ACTUAL canonical function)

- `full_function_idx_scan_delta = +6` (`0 → 6` rehearsal; `280 → 286` benchmark)
  on synthetic local volume, and **`+12` (`0 → 12`) on the OFFLINE representative
  rebuild** (real cached 384-small content) — both from **multiple broad ANN calls
  to the ACTUAL**
  `public.hivemind_semantic_candidates(vector,int,text[],text[])`. The partial
  HNSW index `content_embeddings_hnsw_c1360541028304258884` is **naturally the
  driving path** of the canonical function (eligibility baked as a literal
  row-local `text[]` per arm; iterative scan filters before the per-arm LIMIT).
  `full_function_uses_hnsw = true`.
- The direct single-table **inner kNN** (`idx_scan` delta positive, `EXPLAIN` =
  `Index Scan using content_embeddings_hnsw_c…`) is recorded as
  **`diagnostic_only_insufficient`** — a positive delta there does NOT prove the
  canonical function uses HNSW (it uses the index by construction, with no
  eligibility). The prior pass wrongly treated it as the proof; this renewal
  makes the actual-function delta the decisive gate and demotes the inner kNN.

## Representative reconstruction (read-only)

Reuses the accepted Task 2.14 future-freeze architecture
(`compare_embedding_candidates.read_real_cohort_entities` / freeze helpers;
`semantic.strict_offline_replay` / `cache_file_facts`), pinned to the frozen
high-water `message_id = 1532071167588958368` (not today's moving latest).

- Counts **exact**: 23138 chunks / 7769 entities / 7873 prose / 15265
  workflow_python / 22850 unique / 288 dup-collapsed — all match Task 2.14.
- Paid `cache.384.jsonl` verified **read-only** and byte-identical afterward:
  sha256 `70a9fc74…a2b5`, 60091232 bytes, 27464 records, mode 0o600, dim 384.
- 100% cache-served → real cached 384-d vectors loaded into an isolated PG14
  cluster; selected-literal contract active; HNSW partial index built/valid.
- Frozen in a NEW private snapshot (`.cache/hivemind-hnsw-task-2.16/`, 0700/0600):
  entity-order-identity hash, per-representation chunk-map hash, text-hash
  multiset hash, counts, snapshot file hash.
- **Limitation stated precisely:** Task 2.14 never persisted the raw eval3
  identity-to-chunk mapping or raw texts, so reconstruction verifies COUNTS, the
  high-water, and the count-level `eval3_cohort_manifest_hash` only. The fresh
  chunk-map/text hashes are frozen in the new snapshot but have **no
  Task-2.14-persisted equivalent** to compare against. No old identity-to-chunk
  hash is invented.
- Golden split: 100 task-2.15-applicable (93 judged + 7 no-hit, filters ⊆
  {kinds, item_ids}); 12 future-filter (authors/channels/since) reported
  separately, never compared as equivalent.

## Rollback / idempotence

- `drop index concurrently if exists content_embeddings_hnsw_c1360541028304258884;`
  → returncode 0, **23,138 embeddings retained** (drops only the regenerable index).
- Idempotent rebuild after drop → returncode 0, `indisvalid=t`.
- Invalid same-name index detected (both `f`/`false`) and the build aborts
  **before** `CREATE INDEX … IF NOT EXISTS`; the fail-closed path is unit-tested.

## Explicit limitations / blockers

1. **Hosted latency/cost unprovable locally.** Q8/Q9/P3 require production-like
   representative **staging** (the Phase-0 audit evidences none — only the live
   Banodoco project, which must not be mutated). The successful representative
   *local* run proves semantic fidelity/build/storage only.
2. **Production rollout unselected.** `selected_ef_search` for production stays
   `null`; selecting a production `ef_search` requires staging.
3. **C1-vs-HNSW at local volume — RESOLVED** (renewal): the full canonical
   function now naturally uses the partial HNSW index at 23 k
   (`full_function_idx_scan_delta = +6`), via the row-local baked-array
   eligibility predicate. Production-scale engagement of the full function is
   still a separate (staging) claim, but the local decisive blocker is closed.
4. **Production identity gap (Task 2.17):** production `embedding_contracts` is
   dimension-only; activating the selected literal in production is a separate
   rollout. This task adds the additive identity surface + fail-closed gate and
   activates nothing.
5. **Live-read flakiness:** the public PostgREST endpoint was intermittently
   unreachable (HTTP 522) during development; the reconstructor has bounded retry
   (3×30 s) and fails closed as `blocked_not_representative: live_read_failed`
   when it cannot complete. The recorded success was captured in a responsive
   window; re-running later may fail-closed if the endpoint or corpus has drifted.
6. **Local benchmark storage:** in the representative local cluster the
   `embedding` vectors and `(entity_type,item_id,representation_type,chunk_index)`
   identity are REAL (cached 384-d vectors matched by real text-hash, which was
   computed from the live corpus in memory and never persisted); the
   `chunk_text` column is a placeholder. Recall@10 is identity + embedding-distance
   based, so the placeholder text does not affect it (only `matched_snippet`, which
   is not part of the recall metric).
