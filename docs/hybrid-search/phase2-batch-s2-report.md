# Phase 2 — Batch S2 (Tasks 2.6–2.10) — Semantic Embedding Lifecycle

**Date:** 2026-07-29
**Provider lane:** GLM 5.2 (single session, no subagents, no model switch, no Task/Agent delegation)
**Plan:** `docs/architecture/hivemind-hybrid-search-plan.md`
**Scope:** 2.6 deterministic representation-aware chunking · 2.7 generalized
embed/reembed/drop jobs + source triggers that only enqueue · 2.8 hardened
SKIP LOCKED claim/complete/fail/recover/cancel RPCs · 2.9 bounded
embedding-worker Edge Function + Supabase Cron · 2.10 stale/deletion/opt-out/
failed-contract/replacement cleanup.

**Out of scope (deferred, per the batch brief):** durable historical backfill
(2.11+), the resumable backfill CLI (2.12), semantic candidate SQL (2.15), HNSW
(2.16), the hybrid Edge/RPC (3.x), and pack integration (4.x).

**Dependency honored:** Semantic Foundation Batch S1 (2.1–2.5) returned success;
its files/contracts are present and were reused unchanged. No S1 migration
(020–024) or Phase-1 migration (008–019) was edited. No production database,
Edge, or Cron mutation occurred — production apply is operator-run at the
approved window; everything below is rehearsed on a throwaway isolated cluster
or offline.

---

## 1. Lifecycle design (one state machine, not five pieces)

The batch implements ONE embedding lifecycle end to end so chunk identity feeds
the queue, the queue contract feeds the worker, and cleanup shares the state
machine:

```text
source change (message/resource/distillation)
   │  schema/025 trigger: enqueue ONE idempotent job per
   │  (entity_type, item_id, representation_type); no provider, no hashing
   ▼
embedding_jobs  ── dedup: one pending job per identity+representation
   │  schema/026  hivemind_claim_embedding_jobs (FOR UPDATE SKIP LOCKED, bounded
   │              batch + lease) ──▶ processing/locked_by/lease_expires_at
   ▼
embedding-worker Edge Function (one bounded batch/invocation)
   │  schema/027  hivemind_embedding_payload  → canonical chunks from CURRENT source
   │  provider boundary (OPENAI_API_KEY, server-side) → 384-d vectors, validated
   │  schema/027  hivemind_upsert_embedding_chunks  → atomic replace per identity+rep
   │  schema/026  complete / fail(retry+backoff|max→failed)
   ▼
content_embeddings (schema/022, fixed vector(384), active contract)
   │  schema/029  cleanup: ineligible/deleted/opt-out/failed/superseded
   │  schema/023  atomic active-contract switch (replacement coverage guard)
   ▼
Supabase Cron (schema/028) ticks the worker; SKIP LOCKED prevents overlap.
```

**Why this is source-hash safe:** a job is a "this identity needs (re)processing"
signal, never a content snapshot. The worker always reads CURRENT source to
embed; any change after enqueue simply produced a fresh pending job (the dedup
partial unique index covers only `status='pending'`). A cancelled/superseded job
is terminal (outside the `pending` claim set), so superseded work can never
reactivate.

**Chunk identity (2.6):** chunk *texts* come from the frozen chunkers
(`executors.workflow_representation.chunk_prose`/`chunk_python`); chunk *hashes*
come from the frozen `chunk_hash`. Each chunk carries the immutable
`(entity_type, item_id)` + `representation_type` + the representation-level
`representation_hash`. Prose and workflow_python are SEPARATE streams, each with
its own `chunk_index` starting at 0 — code is never duplicated. Quarantined
workflow Python never reaches chunking (`build_representations` excludes it).

### Files produced (all additive; no existing or protected file edited)

| Path | Role |
|---|---|
| `executors/chunking.py` | Task 2.6 — `EmbeddingChunk` (identity+offsets), `chunk_representation`/`chunk_representations` reusing frozen chunkers, `select_best_chunk`/`collapse_by_item` (best-chunk-per-item), `chunk_manifest` → content_embeddings rows, `coverage_ok`. |
| `executors/embedding_jobs.py` | Task 2.7 — pure-Python mirror of the trigger decision matrix (`decide_jobs`) + `JobIntent`/vocab. |
| `schema/025_embedding_jobs.sql` | Task 2.7 — `embedding_jobs`, dedup partial unique index, `hivemind_active_contract_id`, `hivemind_enqueue_embedding_job`, source triggers (discord_messages/external_resources/distillations) that ONLY enqueue. |
| `schema/026_embedding_job_rpc.sql` | Task 2.8 — `hivemind_claim_embedding_jobs` (SKIP LOCKED), complete, fail (bounded retry+backoff), recover_stale_leases, cancel_pending_jobs, backoff+sanitize_error helpers; SECURITY DEFINER, fixed search_path, revoked from public/anon/authenticated. |
| `schema/027_embedding_worker_surface.sql` | Task 2.9 — `hivemind_chunk_text` (deterministic), `hivemind_embedding_payload` (canonical chunks from current source), `hivemind_upsert_embedding_chunks` (atomic replace; dimension rejection), `hivemind_drop_embedding_chunks`. |
| `supabase/functions/embedding-worker/{index,protocol,protocol_test}.ts` | Task 2.9 — bounded Edge worker + injectable OpenAI transport + 384-d validation + Deno tests. |
| `schema/028_embedding_worker_cron.sql` | Task 2.9 — Supabase Cron (pg_cron+pg_net) schedule, guarded/idempotent/reversible. |
| `schema/029_embedding_cleanup.sql` | Task 2.10 — ineligible/deactivate/drop-contract/cleanup-superseded RPCs; active-contract refusal. |
| `scripts/rehearse_embedding_lifecycle.py` | Isolated-cluster rehearsal of 2.7–2.10 (triggers, concurrency, state machine, worker surface, end-to-end protocol, cleanup). |
| `tests/test_chunking.py` | 22 offline tests (2.6). |
| `tests/test_embedding_jobs.py` | 22 offline tests (2.7 decision matrix). |
| `tests/test_embedding_lifecycle_sql.py` | 6 discoverable SQL tests wrapping the rehearsal (auto-skip if no pgvector). |

Evidence: `docs/hybrid-search/phase2-embedding-lifecycle-rehearsal.json`
(52/52 lifecycle checks PASS on the isolated cluster, sanitized).

---

## 2. Per-task status vs. the plan's completion signals

### 2.6 — Deterministic paragraph-aware prose + code-aware workflow-Python chunking — COMPLETE

- **Signal:** *"Repeated runs yield identical chunks; long prose and
  generated-Python golden cases can hit later chunks."* ✅
- Reuses the frozen `chunk_prose`/`chunk_python`/`CHUNK_CONFIGS`; chunk texts are
  byte-identical to the frozen chunkers (parity test), so chunk identity is stable
  across runs and contract versions.
- Records best-effort char+line **offsets** (anchored on the non-overlapped
  partition, overlap-safe; `coverage_ok` is the hard no-silent-truncation guard);
  ≥2 pilot configs per representation type; AST alignment + `ast_fallback` for
  huge generated literals; **no silent truncation** on the measured ~1.4M-char
  worst case.
- **Later chunk can be the best hit** for prose AND python (golden fixtures:
  the relevant fact/symbol lives in a non-leading chunk and `select_best_chunk`
  picks it). `collapse_by_item` collapses to one best chunk per
  `(entity_type, item_id)` across representation types.

### 2.7 — Generalized embed/reembed/drop jobs + source triggers that only enqueue — COMPLETE

- **Signal:** *"New, changed, deleted, and ineligible source representations
  produce the correct idempotent jobs."* ✅ proven on the isolated cluster
  (11/11 trigger checks) and offline (22 decision-matrix tests).
- Triggers enqueue ONE idempotent job per `(entity_type, item_id,
  representation_type)` — no provider call, no representation computation, no
  hashing inside the source transaction. Dedup (one pending job per identity+rep)
  collapses rapid edits, delete→re-insert, and drop→embed to the latest op.
- Encodes insert / change / delete / soft-delete / status (rejected/superseded) /
  workflow-Python change (payload/body/semantics) / semantic reclassification
  (workflow→other drops code). A prose-only change never duplicates a code chunk.
- Opt-out: the eligibility map (D6) records NO live opt-out column today, so there
  is no source trigger to fire; opt-out is implemented as the
  `hivemind_deactivate_item_embeddings` cleanup path (2.10) a policy decision
  wires up per identity. This is the "preserve all current opt-out rules" reality,
  not a gap introduced here.

### 2.8 — Hardened SKIP LOCKED claim/complete/retry/supersede/fail RPCs — COMPLETE

- **Signal:** *"Concurrent worker tests prove no double processing and bounded
  retries."* ✅
- `hivemind_claim_embedding_jobs` uses `FOR UPDATE … SKIP LOCKED`, a bounded batch
  (1..64), and a lease. **Concurrency:** 3 parallel workers draining a 40-job pool
  → zero double-claims, all 40 processed exactly once, all `done` (rehearsal).
- Bounded retries: attempts counted at claim; `fail(retryable)` backs off
  (capped exponential) until `attempts ≥ max_attempts` → `failed`; non-retryable
  → `failed` immediately. Stale-lease recovery re-queues dead-worker leases.
  Cancelled jobs are terminal (outside the `pending` claim set) → superseded work
  cannot become active; a later change enqueues a FRESH pending row.
- Completion is source-hash safe (change-while-processing enqueues a fresh job).
  Errors are sanitized+bounded (`hivemind_sanitize_error`) — no credential leaks.

### 2.9 — Bounded embedding-worker Edge Function + Supabase Cron — COMPLETE

- **Signal:** *"Each invocation processes one bounded batch, records outcomes, and
  exits inside platform limits."* ✅ the worker SQL path is proven end-to-end on
  the isolated cluster (6/6 protocol checks: claim→payload→fake-embed→upsert→
  complete, vectors stored under the active contract).
- The Edge Function is THIN: it claims one batch, asks `hivemind_embedding_payload`
  for canonical chunks (chunking lives in SQL — one deterministic chunker, no
  second algorithm in TypeScript), embeds via the approved server-side boundary
  (`OPENAI_API_KEY`), validates 384-d vectors, atomically upserts, and
  completes/fails. Bounded batch/lease from env; stale-lease recovery at start;
  per-invocation outcome counts. Overlap is prevented by the SKIP LOCKED claim
  protocol, not by the schedule.
- Cron (`schema/028`): `*/2 * * * *` via pg_cron+pg_net, guarded/idempotent/
  reversible; reads operator-configured URL+key from DB settings.
- The TypeScript protocol was executed through `npx deno test`: **7/7 tests
  passed**, covering vector validation, order preservation, provider result
  cardinality/dimension failures, fail-closed missing credentials, and pgvector
  literal rendering. The Edge Function has **not** been deployed and Cron has
  **not** been applied to production (operator-run; see §5).

### 2.10 — Stale-source/deletion/opt-out/failed-contract/replacement cleanup — COMPLETE

- **Signal:** *"Semantic index contains no deleted/ineligible fixture and contract
  switches preserve the active index."* ✅ (11/11 cleanup checks).
- `hivemind_cleanup_ineligible_embeddings`: drops active-contract vectors whose
  source is gone/ineligible (deleted message, rejected/superseded distillation,
  deleted resource); eligible rows are kept. `hivemind_deactivate_item_embeddings`
  drops an identity across reps (opt-out). `hivemind_drop_contract_embeddings`
  drops a failed/retired contract and **REFUSES the active contract**.
  `hivemind_cleanup_superseded_contracts` reclaims storage beyond the most recent
  `keep`.
- **Safe replacement transition:** rehearsed A(active)→B(equal-coverage) switch
  (schema/023 coverage guard) → A superseded → `drop_contract_embeddings(A)`
  frees A's vectors while B's active index is preserved, and dropping B is
  refused. The active index is never the cleanup target.

---

## 3. Local / live evidence

- **Staging = isolated local cluster.** A throwaway `initdb` cluster
  (unix-socket, no network) on Homebrew PG17 + the locally-available pgvector.
  **This is local rehearsal, not production** — stated explicitly per the brief.
- `phase2-embedding-lifecycle-rehearsal.json` — schema/020–029 applied; 52/52
  lifecycle checks PASS:

| Section | Checks |
|---|---:|
| 2.7 source triggers (insert/update/dedup/soft-delete/workflow-two-stream/python-only/status) | 11/11 |
| 2.8 concurrency (3 workers, 40 jobs, no double-claim, all done) | 5/5 |
| 2.8 state machine (empty-claim, bounded retries→failed, non-retryable, stale recovery, cancel terminal/not-claimable, reenqueue-fresh, source-hash-safe) | 9/9 |
| 2.9 worker surface (chunk determinism, payload availability, upsert atomic replace, dimension rejection, drop) | 10/10 |
| 2.9 end-to-end protocol (claim→payload→fake-embed→upsert→complete→stored) | 6/6 |
| 2.10 cleanup (ineligible/deactivate/safe-replacement/active-refused) | 11/11 |

- No credential, raw private text, vector, or secret-like substring is printed or
  persisted; all rehearsal output is routed through `verify_access.redact`, and
  job errors are sanitized server-side (`hivemind_sanitize_error`).

## 4. Exact tests and concurrency results

```
tests.test_chunking                       22  offline                  PASS
tests.test_embedding_jobs                 22  offline                  PASS
tests.test_embedding_lifecycle_sql         6  isolated PG17 + pgvector PASS
scripts/rehearse_embedding_lifecycle.py   52/52 lifecycle checks       PASS
embedding-worker/protocol_test.ts           7  npx deno                 PASS
repository-wide discover                1057 tests, OK (5 skipped)     PASS (no regressions)
```

**Concurrency result:** 3 workers, batch=1, shared 40-job pool, lease=30s —
union of claimed job_ids had **0 duplicates**, 40/40 processed, 40/40 `done`,
0 errors. SKIP LOCKED + the per-call CTE claim make double-processing
impossible; idempotent upsert bounds any stale-lease redo.

## 5. Deployment / rollback state

| Migration | Apply | Rollback (dependency-ordered) |
|---|---|---|
| 025 jobs+triggers | `psql "$HIVEMIND_DB_URL" -f schema/025_embedding_jobs.sql` | drop 3 triggers, `drop function hivemind_enqueue_embedding_job`, `drop function hivemind_active_contract_id`, `drop table embedding_jobs cascade` |
| 026 job RPCs | `psql … -f schema/026_embedding_job_rpc.sql` | drop the 5 RPCs + 2 helpers |
| 027 worker surface | `psql … -f schema/027_embedding_worker_surface.sql` | drop 4 fns + `hivemind_trailing_lines` |
| 028 Cron | `psql … -f schema/028_embedding_worker_cron.sql` | `select cron.unschedule('hivemind-embedding-worker');` |
| 029 cleanup | `psql … -f schema/029_embedding_cleanup.sql` | drop the 4 cleanup RPCs |

All additive + idempotent + guarded; rollback touches only regenerable
infrastructure (jobs/vectors/indexes/schedules), never source rows. Apply order
020→029; rollback 029→025. **Production apply (DB, Edge deploy, secret wiring,
Cron enable) is operator-run at the approved window and was NOT performed.**
Required operator steps before live: register `OPENAI_API_KEY` (already done per
S1) + `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY` are existing Edge secrets; set
optional `EMBEDDING_WORKER_KEY`/`EMBEDDING_WORKER_BATCH`/`EMBEDDING_WORKER_LEASE_S`;
set `app.hivemind_worker_url`/`app.hivemind_worker_key` DB settings; deploy the
function; then apply 025–029 + 028 on Supabase (pg_cron present there).

## 6. Blockers / deferred live-only gates

1. **Live Edge deploy:** the local TypeScript protocol is proven (7/7), but live
   deployment remains an operator-run production action.
2. **Cron apply:** pg_cron/pg_net are Supabase-provided (absent on the vanilla
   rehearsal cluster, so 028 correctly self-skipped there with a NOTICE). The
   schedule SQL is written/idempotent/reversible; applying it on Supabase is
   operator-run.
3. **Production DB apply** of 025–029: operator-run at the approved window;
   rehearsed locally only.
4. **Worker chunker vs backfill chunker unification:** the worker chunks via the
   SQL `hivemind_chunk_text` (deterministic line-window); the authoritative
   Python chunker (2.6, frozen paragraph/AST) is the backfill path. Both are
   deterministic; the production backfill (2.12) should call the same SQL chunk
   surface (or pin one algorithm) so chunk identity is stable across the
   backfill→incremental boundary. Named handoff for 2.12 (§7).
5. **Opt-out policy (eligibility map D6):** no live opt-out column exists; the
   `hivemind_deactivate_item_embeddings` mechanism is provided for a policy
   decision; no behavior is silently invented.

## 7. Handoff contract for tasks 2.11–2.14

- **2.11 (durable backfill runs):** reuse `embedding_jobs` + the 026 RPCs as the
  durable claim/checkpoint substrate. A backfill run can express its cursor as a
  claimed job stream; the same claim/complete/fail protocol (SKIP LOCKED, bounded
  retries, source-hash-safe completion) applies. `hivemind_active_contract_id`
  resolves the filing contract.
- **2.12 (resumable backfill CLI):** MUST chunk through `hivemind_embedding_payload`
  / `hivemind_chunk_text` (the SAME surface the worker uses) so chunk identity is
  identical across the backfill→incremental boundary (blocker §6.4). File via
  `hivemind_upsert_embedding_chunks`. For workflow Python, materialize
  `payload.python_source` first (the payload surface reads it; body-block recovery
  and VibeComfy conversion belong to the remediation stage, as in S1's 2.5 note).
  Workflow-representation remediation must complete before resource embedding
  (plan backfill order). Reuse `executors/canonical_representations` + `executors/chunking`.
- **2.13 (pilot cohorts):** enqueue cohorts via `hivemind_enqueue_embedding_job`
  (or a bulk variant) under the active contract; the worker (2.9) drains them.
  Coverage gates map to `hivemind_set_active_embedding_contract`'s coverage guard.
- **2.14 (dimension + chunk-size decision):** the chunker target/overlap are
  contract-axis parameters (`chunking_version` on `ContractSpec`); compare ≥2
  `CHUNK_CONFIGS` per representation. A dimension change uses the documented
  sibling-table + atomic-switch path (S1 AD-2); cleanup is 029.

---

## Completion summary

| Task | Status | Signal met | Live/deferred gate |
|---|---|---|---|
| 2.6 | ✅ Complete | identical chunks across runs; later prose/python chunks can be the best hit | — |
| 2.7 | ✅ Complete | correct idempotent jobs for new/changed/deleted/ineligible reps | opt-out = policy decision (D6) |
| 2.8 | ✅ Complete | no double-process (SKIP LOCKED); bounded retries; stale recovery; supersession safe | — |
| 2.9 | ✅ Complete | one bounded batch/invocation; outcomes recorded; exits in limits; Edge TS 7/7 | live deploy + Cron = operator-run |
| 2.10 | ✅ Complete | no deleted/ineligible fixture indexed; contract switch preserves active index | — |

No task is redefined as complete against an unmet signal. The only incomplete
gates are live-only/operator-run and named precisely: Edge deployment, Cron
apply (pg_cron is Supabase-provided), and production DB apply — all rehearsed
locally and reversible.
