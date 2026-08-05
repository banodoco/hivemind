# Task 2.14 — Embedding Dimension & Chunk-Configuration Decision

- Mode: `real-provider`
- Exact-cosine local evaluation (numpy). **Not** HNSW/SQL/Edge latency.
- Golden set: `golden/2026-07-28/v1` (112 cases: 104 judged, 8 no-hit, 141 judgments).
- Capacity facts frozen from task 0.7: 384-d full corpus 4.59 GB (PASS); 1536-d 16.4 GB (FAIL the 12 GB gate).
- Generated (eval3): `2026-07-29T19:16:00+00:00`

## Candidate metrics

| Candidate | Dim | recall@10 | MRR | nDCG@10 | wf_code R@10 | long-res R@10 | later-chunk hit | standalone $ | eligible |
|---|---|---|---|---|---|---|---|---|---|
| 384-small | 384 | 0.4038 | 0.3159 | 0.3271 | 0.3594 | 0.2500 | n/a | 0.1905 | yes |
| 384-large | 384 | 0.4038 | 0.2982 | 0.3146 | 0.3125 | 0.2500 | n/a | 0.1850 | yes |
| 1536-small | 1536 | 0.4423 | 0.3415 | 0.3542 | 0.3594 | 0.2500 | n/a | 0.1905 | no (projected_full_corpus_storage_above_gate) |
| 1536-large | 1536 | 0.4135 | 0.3189 | 0.3310 | 0.3125 | 0.2500 | n/a | 0.1850 | no (projected_full_corpus_storage_above_gate) |

> `long_res R@10` = `long_resource_chunk_recall_at_10` (the frozen selection signal). `later-chunk hit` = `later_chunk_hit_rate`, which is **unavailable** for eval3 (`raw_eval3_ranking_provenance_not_persisted`) and never fabricated.

## Decision

- **Selected production dimension:** `384`
- **Prose chunk contract:** target `512` / overlap `50` tokens
- **Workflow-Python chunk contract:** target `512` / overlap `50` tokens
- **Selected evaluation contract id:** `12e19cdb566b8744` (`chunk_configv1prose#512/50workflow_python#512/50`)
- **Production chunking-version handoff:** canonicalization v1, chunking v2, chunk-config v1
- **Production activated:** `False` (task 2.17 is a propagation/verification/acceptance-test handoff only; actual activation is a later, separate rollout decision)
- **Selection rationale:** recall@10, mrr, ndcg@10, workflow_code_recall@10, long_resource_chunk_recall@10, lower standalone candidate cost
- **Eligible ranking:** ['384-small', '384-large']

### Non-winner state (machine-readable in JSON `candidates[].selection_state`)

- `384-small`: selected=`True`, active=`False`, evaluation_only=`False`, production_activated=`False` — selected winner
- `384-large`: selected=`False`, active=`False`, evaluation_only=`True`, production_activated=`False` — lower priority on frozen tiebreak (mrr 0.298224 < 0.315858; ndcg@10 0.314581 < 0.32707; lower workflow_code_recall@10) [384-large]
- `1536-small`: selected=`False`, active=`False`, evaluation_only=`True`, production_activated=`False` — capacity-disqualified (projected_full_corpus_storage_above_gate)
- `1536-large`: selected=`False`, active=`False`, evaluation_only=`True`, production_activated=`False` — capacity-disqualified (projected_full_corpus_storage_above_gate)

## Provider accounting (eval3 exact incremental bake-off)

| Candidate | requests | inputs | tokens | cost $ | cache hits | cache misses |
|---|---|---|---|---|---|---|
| 384-small | 103 | 22951 | 7911142 | 0.1582 | 0 | 22951 |
| 384-large | 85 | 4513 | 6402213 | 0.1280 | 6836 | 4513 |
| 1536-small | 99 | 22951 | 7911142 | 0.1582 | 0 | 22951 |
| 1536-large | 84 | 4513 | 6402213 | 0.1280 | 6836 | 4513 |

- **eval3 exact total:** 28626710 input tokens = **$0.572534** (shared dimension-cache reuse recorded per candidate).

### Standalone candidate accounting (order-independent tiebreak cost)

| Candidate | corpus texts | corpus tok | golden tok | total tok | cost $ |
|---|---|---|---|---|---|
| 384-small | 22850 | 9524482 | 430 | 9524912 | 0.1905 |
| 384-large | 11248 | 9248264 | 430 | 9248694 | 0.1850 |
| 1536-small | 22850 | 9524482 | 430 | 9524912 | 0.1905 |
| 1536-large | 11248 | 9248264 | 430 | 9248694 | 0.1850 |

### Historical duplicate-attempt disclosure

- **Exact earlier spend:** `unavailable` — the first implementation did not persist per-attempt provider usage; two interrupted/destructive resets make exact earlier spend unrecoverable
- **Destructive cache resets disclosed:** 2; duplicate provider work was performed in the interrupted attempts.
- **Evaluation attempts:** 3; per-full-bakeoff projection `$0.75091`.
- **Conservative reconstructed estimate:** **$2.25273** — CONSERVATIVE RECONSTRUCTED ESTIMATE, not a guaranteed upper bound and not exact: it scales the frozen preflight projection by the 3 attempts. The true spend is NOT guaranteed to be lower — responses can be billed before cache persistence/retry, and the frozen preflight OMITTED golden-query tokens, so actual spend could exceed this figure. No bounded in-flight/retry + golden-token margin has been added or justified.
- Estimate within `$25.0` cap: `True`. the ESTIMATE is under the cap; this is NOT proof that true spend was under the cap, because the estimate omits in-flight/retry billing and golden-query tokens.

## Preflight (frozen, pre-call)

- Projected spend (all 4 candidates): `$0.75091` of `$25.0` cap — within cap: `True`
- Price assumption: $0.02/1M input tokens (text-embedding-3-small, any dimension)

## Immutable offline evidence replay

- Provider calls attempted: `0`; network calls: `0`.
- Winner reproduced from frozen policy: `384-small`; checks passed: `44`.
- Replay bundle: `.cache/hivemind-semantic-eval/replay-bundle.json` (file sha256 `09dbce078cd89677…`).
- Evidence pair id: `task-2.14-evidence-05caa124d4b47d58` (shared by the JSON + Markdown; bound by the manifest).
- Frozen manifest: `docs/hybrid-search/task-2.14-frozen-manifest.json`.

## Honest limitations

- Ranking is **local exact cosine** over the eval3 cohort — not HNSW/SQL/Edge.
- The 1536-d dimension is evaluated for quality but **capacity-disqualified** (16.4 GB > 12 GB gate).
- The eval3 cohort was an **observed bounded read-only eval3 cohort whose raw contents were NOT transactionally frozen/persisted and cannot be raw-replayed** (the raw texts / identity-to-chunk mapping were not persisted before the provider calls).
- Task 2.17 is a **propagation / verification / acceptance-test handoff**; it does NOT activate production. Actual activation is a later, separate rollout decision, and there are known production gaps to close first (see `task_2_17_handoff.known_production_gaps_to_close`).
- **`later_chunk_hit_rate` is unavailable** for eval3 (`raw_eval3_ranking_provenance_not_persisted`); eval3 workflow_code Recall proves entity recall but CANNOT prove the workflow_python representation won. `long_resource_chunk_recall_at_10` is the frozen selection signal and is unaffected.
- **Exact earlier-attempt spend is unavailable**; the conservative reconstructed estimate is an estimate, NOT a guaranteed upper bound and NOT proof that true spend was below the $25 cap (in-flight/retry billing and golden-query tokens were omitted from the frozen preflight).
- **Runtime failed-attempt billing is unavailable and NOT counted.** The aggregate actual-usage guard records usage ONLY for a response the provider returned; a failed attempt (transport error / HTTP 429 / 5xx, retries bounded to `4` total attempts) reports no usage and is not counted. The recorded aggregate token/cost total is the sum of successful-response usage only and is **NOT proof against billed-but-unreported failed attempts** (see `accounting.runtime_spend_gates.aggregate_actual_usage_guard`).
- The strict replay is a **cryptographic evidence/cache replay**, not a recomputation of exact-cosine retrieval from raw text.

