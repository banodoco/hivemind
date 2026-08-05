# Phase 1 — Task 1.2 Lexical Resource/Distillation Documents Report

**Date:** 2026-07-28
**Task:** 1.2 — Add weighted lexical documents/GIN indexes for resource prose/code
and distillations, including workflow-Python precedence, secret scanning/quarantine,
deduplication, and bounded code documents.
**Plan:** `docs/architecture/hivemind-hybrid-search-plan.md` (Astrid planning copy).
**Frozen inputs:** task 0.8 (`phase0-workflow-representation-contract.{md,json}`,
`executors/workflow_representation.py`) and task 1.1
(`phase1-lexical-contract.{md,json}`, `executors/lexical_contract.py`) — both
implemented against exactly, never reinterpreted.

This task is **implementation**, not a new freeze. It creates the lexical storage,
the IMMUTABLE SQL helpers that mirror the frozen Python reference, the chunk-aware
workflow-Python refresh path, and the SQL/EXPLAIN evidence. It is additive, owns
no concurrent task's files, and applies **no live migration** (production DB
mutation is not owned by this concurrent task).

## Files produced

| Path | Role |
|---|---|
| `schema/003_lexical_resource_documents.sql` | **Migration** (additive, idempotent): IMMUTABLE SQL helpers mirroring `workflow_representation.py`; generated weighted `tsvector` columns on `distillations` + `external_resources`; `lexical_documents` table (durable chunk identity, offsets/hashes/version/quarantine, constraints); `lexical_resource_python_state` table; GIN + btree indexes; the `hivemind_workflow_python_state` accessor. |
| `executors/lexical_documents.py` | **Bridge helper** (pure stdlib): reuses the frozen reference to resolve precedence, dedup, scan/quarantine, AST-aware chunking with no-silent-truncation, and refresh/upsert/drop with hash-skip behind an injected store. Never executes stored Python; never serializes matched secrets. |
| `scripts/lexical_pg.py` | **Throwaway local PG cluster harness** + reproducible runner: lifecycle, deterministic fixtures (all task-1.2 families), assertions, `EXPLAIN (ANALYZE, BUFFERS)` capture. No Docker, no network, tears down. |
| `tests/test_lexical_documents.py` | **27 offline unit tests** pinning precedence, no-duplication, quarantine/secret-non-leakage, chunking/coverage, refresh/upsert/drop/hash-skip, distillation eligibility. |
| `tests/test_lexical_sql.py` | **12 SQL tests** on an isolated cluster: migration idempotence, generated columns, constraints, eligibility/quarantine, IMMUTABLE-helper↔Python parity, EXPLAIN index usage. |
| `docs/hybrid-search/phase1-lexical-explain-evidence.json` | **Saved `EXPLAIN (ANALYZE, BUFFERS)` evidence** + assertion results (17/17). |

## Commands

```bash
python3 -m unittest tests.test_lexical_documents -v        # offline unit (27)
python3 -m unittest tests.test_lexical_sql -v              # SQL on throwaway cluster (12)
python3 scripts/lexical_pg.py                               # reproducible runner + evidence
python3 -m unittest discover tests/                         # whole repo
```

## Schema decisions

1. **`'simple'::regconfig` everywhere, frozen expressions realized in SQL.**
   - `distillations.lexical_tsv` is a STORED generated column realizing
     `DISTILLATION_SPEC.expression()` (question A / conditions B / answer C).
   - `external_resources.prose_tsv` is a STORED generated column realizing
     `RESOURCE_PROSE_SPEC.expression()` (title A / tags+semantics B / body C).
   - `lexical_documents.tsv` is a STORED generated column realizing
     `WORKFLOW_PYTHON_SPEC.expression()` (`setweight(to_tsvector('simple', chunk_text),'C')`)
     — so each code chunk's vector is derived in SQL from `chunk_text`, never drifts,
     and the production chunker can change without touching the expression.
   - De-risked up front on an isolated PG14 cluster: a STORED generated `tsvector`
     backed by an IMMUTABLE plpgsql helper compiles and is served by its GIN index.

2. **IMMUTABLE SQL helpers mirror `workflow_representation.py`.** `hivemind_workflow_prose`
   (mirrors `strip_python_blocks`), `hivemind_workflow_semantics_text` (mirrors
   `project_semantics`, frozen field order, value-only object descent), and
   `hivemind_resource_tags`. Parity is pinned by token-set tests against the Python
   reference. All declared IMMUTABLE (no table access) so they may anchor generated
   columns and expression indexes.

3. **Chunk-aware storage keyed by the frozen identity.** `lexical_documents`
   PK = `(entity_type, item_id, representation_type, chunk_index)` — the same shape
   as the 0.8 embedding identity (so a lexical chunk and a future embedding chunk
   share an addressable, collapsible identity). Holds `workflow_python` code chunks
   + the rare over-long-prose fallback; normal prose/distillation prose live in
   generated columns; messages stay on the `discord_messages` index (task 1.3).
   Includes offsets (best-effort char anchors), `representation_hash`/`chunk_hash`,
   the four frozen version columns, `quarantine_state`, and `method`.

4. **No-duplication is structural.** A CHECK constraint guarantees a
   `workflow_python` document is always `safe` (`representation_type <> 'workflow_python'
   OR quarantine_state = 'safe'`): quarantined Python has **zero** rows, by construction.
   The prose arm strips delimited Python blocks (`hivemind_workflow_prose`), so identical
   code in `payload` and `body` (the 222-row "both" cohort) is indexed exactly once —
   as `workflow_python`. (Note: a class name like `WanVideoSampler` may still appear in
   *prose* via the projected `workflow_semantics` `node_types`; that is intended and
   correct. The dedup discriminator is a code-only symbol such as `num_frames`/`lora_weight`,
   which is present in `workflow_python` and absent from prose.)

5. **Eligibility is explicit.** `lexical_resource_python_state` records each resource's
   cohort + `safe`/`quarantined` state and the non-secret reason codes; the STABLE
   `hivemind_workflow_python_state(id)` accessor realizes the frozen workflow-Python
   eligibility predicate. Distillation eligibility is encoded in the candidate query
   (`status IN ('pending','approved')`); service-role RLS bypass never implies "return all".

6. **No collision with task-2 embeddings.** `lexical_documents` carries no vector and is
   keyed without `contract_id`; the task-2 `content_embeddings` table is a distinct,
   contract-keyed, fixed-dimension table. Identity columns are shared by design
   (collapsibility), tables are not.

## Representative outputs (evidence: `phase1-lexical-explain-evidence.json`, 17/17)

EXPLAIN (forced `enable_seqscan=off`, the same method as the task-1.1 probe script)
shows each representative `simple` query uses its GIN index:

| Arm | Index used | Forced plan head |
|---|---|---|
| resource prose | `external_resources_prose_tsv_idx` | Bitmap Index Scan on `external_resources_prose_tsv_idx` |
| distillation | `distillations_lexical_tsv_idx` | Bitmap Index Scan on `distillations_lexical_tsv_idx` |
| workflow python | `lexical_documents_tsv_idx` | Bitmap Index Scan on `lexical_documents_tsv_idx` |

(The fixture tables are small, so the *natural* plan may still Seq Scan; at production
scale — thousands of resources / chunks — the planner uses the GIN indexes. The forced
plans definitively prove index reachability, matching the task-1.1 evidence method.)

Behavioral results on the deterministic fixture set (payload-only, body-only,
both, changed, huge, parser-fallback, unavailable, quarantined, prose-only article,
pending/approved/rejected distillation):

- **Safe workflow Python is searchable:** ≥5 workflows match a code symbol (`wanvideosampler`).
- **Quarantined Python is excluded:** the secret resource has 0 `workflow_python`
  documents; `hivemind_workflow_python_state` returns `quarantined`; the matched
  secret value appears nowhere in the state table.
- **Duplicate body/payload indexed once:** the "both" resource has exactly its payload
  chunk set; the code-only symbol `num_frames` is in `workflow_python` and **absent**
  from `prose_tsv`.
- **Distillation eligibility:** the rejected distillation is excluded; pending/approved
  included; the A-weight (question) hit outranks a C-weight (answer) hit under `ts_rank(...,32)`.
- **Constraints** reject: a quarantined `workflow_python` row, negative `chunk_index`,
  and an invalid `entity_type`.
- **Migration idempotent:** re-applying `003` is a no-op.

## Boundary / concurrency

- Additive only. No source rows read or mutated. **No live/production migration applied**
  (production DB mutation is not owned by this concurrent task).
- Files owned and not touched by this task: task 1.3's `schema/004_discord_message_fts.sql`
  and the live Discord index; task 1.4's `schema/005_identifier_normalization.sql` and
  identifier-normalization helpers. This task does **not** create a message FTS index, the
  trigram/identifier arm, candidate/RPC SQL, semantic embeddings, or backfill live data.
- Pre-existing dirty files and all completed Phase-0 / 1.1 artifacts were left untouched.

## Gaps (explicitly deferred, none block the 1.2 signal)

| Item | Owner task |
|---|---|
| Apply `003` to production (live migration / index build) | operator / 1.3 window (production DB mutation) |
| Candidate SQL combining fts/phrase/ident/code arms + collapse | 1.7 |
| Filters (kind/item_id/source/date/author/channel) + hydration | 1.8 |
| Hardened `SECURITY DEFINER` RPC, fixed `search_path`, grants | 1.9 |
| `normalize_identifier` exact-identifier arm + trigram on titles/questions | 1.4 / 1.5 |
| Live workflow-representation remediation (`recoverable`/`unavailable` split, real quarantined count) | 2.12 |
| Production chunk configuration (final target/overlap) | 2.14 |
| Embedding storage (`content_embeddings`) sharing this identity | 2.3 |

## Completion signal (1.2)

> Migrations and fixtures prove safe workflow Python is searchable, quarantined Python
> is excluded, duplicate body/payload code is indexed once, and representative queries
> use the indexes.

**Met.** `schema/003` adds the weighted generated `tsvector` columns + GIN indexes for
resource prose and distillations, the IMMUTABLE SQL helpers mirroring the frozen Python,
and the chunk-aware `lexical_documents` storage with durable identity, offsets/hashes/
versions/quarantine, constraints, and indexes. The frozen workflow-Python precedence,
no-duplication, secret-scanner/quarantine, AST chunking with no-silent-truncation, and
refresh/upsert/drop are implemented in `executors/lexical_documents.py` (reusing the frozen
reference; never executing stored Python; never serializing matched secrets). Deterministic
fixtures cover payload-only, body-only, duplicated-both, changed, huge, parser-fallback,
unavailable, quarantined-credential, prose-only, and pending/approved/rejected distillation.
Saved `EXPLAIN (ANALYZE, BUFFERS)` evidence on an isolated throwaway PostgreSQL 14 cluster
shows representative `simple` FTS queries use the new GIN indexes. Pinned by 27 offline
unit tests + 12 SQL tests + a reproducible local runner, all green.
