# Phase 1 / Tasks 1.7–1.9 — Lexical candidate SQL, filters + hydration, hardened RPC

**Status:** COMPLETE (isolated-cluster rehearsal 18/18 green; offline + SQL tests
green; full offline suite 1051 pass). The live production apply is **blocked on
schema/003 (task 1.2) not yet being live** — see "Live deployment state" below.
**Date:** 2026-07-29. **Provider:** GLM 5.2 (single session).

## Completion signals (plan)

- **1.7** — *"Implement the lexical candidate SQL combining FTS, phrase,
  exact-identifier, and bounded workflow-code fragment arms with deterministic
  ranks. One ranked identity stream covers all entity/representation types
  without duplicate items."* → `public.hivemind_lexical_candidates` (schema/008).
- **1.8** — *"Add kind, item-ID, source, date, author, and channel filters plus
  post-limit hydration into the public unified_feed shape."* → in the candidate
  WHERE + the RPC hydration.
- **1.9** — *"Wrap lexical retrieval in a hardened SECURITY DEFINER RPC with
  fixed search_path, qualified relations, eligibility predicates, grants, and
  limits."* → `public.hivemind_lexical_search` (schema/009).

## Design

One canonical candidate SQL — there is **no second copy** in tests, RPC, or
drivers. The hardened RPC (009), the cluster rehearsal/EXPLAIN (1.10), the
offline eval adapter, and the live driver all consume this one function.

**`hivemind_lexical_candidates(p_query, p_candidate_limit, p_kinds, p_sources,
p_item_ids, p_since, p_channels, p_authors, p_author_optout, p_bots_excluded)`**
(schema/008) emits a `UNION ALL` of arms, then collapses to exactly one row per
`(entity_type, item_id)` and applies a deterministic global order + the candidate
limit:

| Arm | Index | Source |
|---|---|---|
| message FTS | `idx_discord_messages_content_fts_simple` (004) | `to_tsvector('simple',content) @@ websearch_to_tsquery('simple',q)` |
| message exact-identifier (containment) | `idx_discord_messages_identifier_trgm` (007) | task-1.6 corrected contract: `normalize(content) LIKE '%'||normalize(q)||'%'`, whole-body equality 1.0 / containment 0.9 |
| resource prose FTS + phrase | `external_resources_prose_tsv_idx` (003) | `prose_tsv @@ tsq` |
| resource title exact-name | `idx_external_resources_title_trgm_norm` (006) | normalized title containment |
| workflow_python FTS | `lexical_documents_tsv_idx` (003) | safe code chunks `tsv @@ tsq` |
| workflow_python fragment (bounded) | `lexical_documents_python_chunk_trgm_idx` (008, new) | normalized code-chunk containment (subsequence `simple` FTS misses) |
| distillation FTS | `distillations_lexical_tsv_idx` (003) | `lexical_tsv @@ tsq` |
| distillation question exact-name | `idx_distillations_question_trgm_norm` (006) | normalized question containment |

**Rank bands** (one comparable score per identity so the global `ORDER BY` is
well-defined — a *definitional* hit outranks a passing prose mention):
title/question ident `0.95` > workflow-python code `0.93` > message ident
containment `0.90` (task-1.6) > FTS `ts_rank(tsv,tsq,32)` (capped below 0.90).
So for an exact node-name query the workflow that *defines* the node outranks the
messages that merely mention it. Phrase arm fires only for single-token names or
whole `"quoted"` phrases (mirrors `lexical_contract.query_arms`).

**Filters** apply INSIDE each arm's WHERE (pre-ranking) so filtered-out rows are
never scored: kinds (workflow↔resource alias), sources, `since`, **channels**
(join `discord_channels.channel_name`), **authors** (join
`members` on `COALESCE(global_name,username)`), and **item_ids** (a bounded
text allow-list — never interpolated SQL). item_ids require exactly one kind
(AD-1); ambiguous cross-kind ids are rejected by the RPC, not guessed.

**Eligibility is encoded in SQL** because the service role bypasses RLS:
`is_deleted=false` (messages), opt-out (`allow_content_sharing`, default-off
flag, preserves current behavior), bot policy (default-off), distillation
`status IN ('pending','approved')`, and workflow-python `quarantine_state='safe'`
+ `hivemind_workflow_python_state(r.id)='safe'` (quarantined code is structurally
absent from `lexical_documents`, schema/003).

**`hivemind_lexical_search(...)`** (schema/009) is `SECURITY DEFINER`,
`set search_path = public, pg_temp`, fully-qualified relations. It validates +
bounds inputs (query ≤2000 chars, limit ≤100, arrays ≤64, item_ids ≤256, mode
must be `'lexical'`), rejects ambiguous item_ids, applies the global limit
**after** the candidate stream and **before** hydration, hydrates only the
surviving identities into the exact `unified_feed` shape (carrying
`matched_representation` + the ≤240-char secret-redacted `matched_snippet`), sets
a 2000 ms statement backstop, and `REVOKE`s execute from `public/anon/authenticated`
granting only `service_role`. Snowflake `item_id`s are text throughout; the
response is the public search shape (`results[]`, `count`, `meta`).

## EXPLAIN (ANALYZE, BUFFERS) evidence — every arm index-served

Isolated PG 14 cluster, production-shaped seed (60k messages w/ sparse identifier
prose + soft-deletes + channels/authors; 3k filler resources; 4k filler
distillations; 3k filler workflow-python chunks; planted WanVideoSampler/CogVideoX
in message prose AND workflow Python). Evidence:
`docs/hybrid-search/phase1-lexical-candidate-rehearsal.json`.

Each arm's intended GIN is **servable** (forced `enable_seqscan=off` plan uses it;
probes isolate the GIN predicate). Real plans for the low-volume
resource/distillation tables legitimately seq-scan a few-thousand-row cohort
(correct + sub-ms; the GIN wins at production scale by selectivity). **No arm
seq-scans the 60k message table; no legacy 30 s timeout class.** The task-1.6
containment arm is GIN-served on messages (`Bitmap Index Scan on
idx_discord_messages_identifier_trgm`).

## Containment proof (gate 9) + latency gate

`hivemind_lexical_search('WanVideoSampler')` returns workflow **20** at rank 1
(via the workflow-python arm) ahead of the messages that mention it;
`'CogVideoX'` returns workflow **64**. Spaced-form bridge proven ("FLUX 1"
body → `flux1`). Warm RPC latency p95 over the containment probes (lexical p95
≤ 750 ms gate):

| Probe | warm p95 |
|---|---:|
| WanVideoSampler | 60.8 ms |
| CogVideoX | 63.9 ms |
| ipadapter | 58.5 ms |
| controlnet (genuinely common word) | 129.4 ms |

All ≪ the 750 ms lexical-only gate and the legacy 30 s timeout.

## Files (this batch owns these only)

- `schema/008_lexical_candidate_sql.sql` — the canonical candidate function + the
  bounded workflow-code fragment trigram index (`CREATE INDEX CONCURRENTLY`).
- `schema/009_lexical_search_rpc.sql` — the hardened SECURITY DEFINER RPC.
- `scripts/rehearse_lexical_candidate.py` — isolated-cluster rehearsal (bootstrap
  001+003–009, seed, RPC, eligibility, EXPLAIN, verdict).
- `scripts/live_lexical_search.py` — preflight / gated apply / rollback / evidence.
- `tests/test_lexical_candidate_sql.py` — 14 cluster SQL+RPC tests.
- `tests/test_lexical_adapter.py` — 18 offline adapter model tests.
- `eval/retrieval/adapters.py` — `LexicalAdapter` + `lexical_*` helpers (registered).
- `docs/hybrid-search/phase1-lexical-candidate-rehearsal.json`,
  `phase1-lexical-preflight-live.json`, `phase1-lexical-eval.md`.

## Live deployment state (BLOCKER — handoff to merge gate)

`scripts/live_lexical_search.py --preflight` (read-only, 2026-07-29):

| Object | Live |
|---|---|
| `hivemind_normalize_identifier` (005) | ✓ |
| `idx_discord_messages_content_fts_simple` (004) | ✓ |
| `idx_discord_messages_identifier_trgm` (007) | ✓ |
| `idx_external_resources_title_trgm_norm` / `idx_distillations_question_trgm_norm` (006) | ✓ |
| **`lexical_documents` (003)** | **✗** |
| **`external_resources.prose_tsv` (003)** | **✗** |
| **`distillations.lexical_tsv` (003)** | **✗** |
| **`hivemind_workflow_python_state` (003)** | **✗** |

**schema/003 (task 1.2) is not yet live on production**, so the candidate SQL/RPC
cannot be applied live yet (the candidate function references `lexical_documents`,
`prose_tsv`, `lexical_tsv`, `hivemind_workflow_python_state`). The `--apply` step
correctly refuses (preflight not green). The 008/009 code is proven correct on the
isolated-cluster rehearsal, which applies the **full 003→009 chain** and passes
18/18 gates. **Apply order for the merge gate: schema/003 → 008 → 009** (then
Phase 3). Rollback (`--rollback`) drops the RPC, the candidate function, and the
fragment index; no source row is touched.

## Rollback (one command, once applied)

```bash
python3 scripts/live_lexical_search.py --rollback
# equivalent to: drop function ... hivemind_lexical_search(...);
#                drop function ... hivemind_lexical_candidates(...);
#                drop index if exists public.lexical_documents_python_chunk_trgm_idx;
```
