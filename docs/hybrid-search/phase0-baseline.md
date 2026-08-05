# Phase 0 — Task 0.4 Legacy `ILIKE` Search Baseline

**Captured (UTC):** 2026-07-28T19:01:24Z
**Tool:** `baseline-search/0.4.0` · **Manifest:** `legacy-ilike/2026-07-28`
**Python:** 3.14.3 · **Endpoint ref:** `ujlwuvkrxlvoswwkerdf`
**Behavior replicated:** Two PostgREST GET passes against unified_feed (kind=eq.distillation, then kind=neq.distillation / in.(...)), each capped independently at the requested limit; results merged distillations-first; no global limit; no relevance ranking.

This is an **observed** baseline only. It records what the deployed two-pass `ILIKE` search returns and how long it takes. It does **not** assign relevance grades — that human judgment is task 0.6 and is fenced off at the end. No secret, full body, or anon key is recorded here.

## Headline findings

- **Latency:** end-to-end p50 1236 ms / p95 30578 ms (max 30593 ms) across 11 queries; per-pass p50 478 ms / p95 30039 ms across 21 GETs. The p95 is dominated by genuine 30 s timeouts (see below); **healthy** successful passes alone run p50 333 ms / p95 1956 ms (n=16).
- **Failures on real queries:** 3 pass(es) on ordinary queries (not the artificial probe) failed — genuine 30 s timeouts and a server 500. Under the deployed executor each is an unhandled exception. See "Failures on real queries" below.
- **No global limit:** 3 of 11 queries returned more rows than the requested limit — each of the two passes is capped independently at `limit`, so the merged list is bounded by `2 × limit`, not `limit`. See the doubled-limit row below.
- **Unranked:** results come back in `unified_feed`'s default order (see per-query `created_at_order`); there is no lexical or relevance ranking.
- **Contiguous-substring only:** `ILIKE` matches the single phrase `*<query>*` on title/body, so multi-term queries (e.g. "controlnet settings") miss rows that contain both terms non-contiguously.
- **Duplicates:** 0 duplicate `(kind, item_id)` items observed across merged lists (expected ~0; the two passes query disjoint `kind` sets).
- **Snowflakes:** all item_ids preserved as strings.
- **Outcomes:** {'success': 7, 'http_error': 1, 'timeout': 3} (5 zero-result queries).
- **Timeout handling:** the deployed executor catches only `HTTPError`; a network/`URLError`/timeout propagates as an unhandled traceback. This tool classifies it instead (see the `timeout_prone` row).

## Per-query results

| id | category | query | passes | outcome | fetched | >limit? | dup | e2e | pA / pB (ms) |
|---|---|---|---|---|---:|:--:|---:|---:|---|
| hit_common | hit | `upscale` | D/O | success | 21 | yes | 0 | 261 ms | 119 / 141 |
| hit_multiword | hit | `controlnet settings` | D/O | http_error | 0 |  | 0 | 3615 ms | 478 / 3137 |
| exact_dotted | exact_identifier | `FLUX.1` | D/O | success | 20 |  | 0 | 3028 ms | 2538 / 491 |
| exact_versioned | exact_identifier | `Wan 2.2` | D/O | success | 20 |  | 0 | 1038 ms | 128 / 910 |
| exact_identifier_word | exact_identifier | `WanVideoSampler` | D/O | timeout | 0 |  | 0 | 30563 ms | 520 / 30043 |
| workflow_kinds | workflow | `lora` | O | success | 20 |  | 0 | 189 ms | — / 188 |
| rare_nohit | rare_no_hit | `zzqxnotarealterm-99999` | D/O | success | 0 |  | 0 | 1886 ms | 123 / 1762 |
| filter_source | filter | `model` | D/O | timeout | 0 |  | 0 | 30593 ms | 554 / 30039 |
| filter_since | filter | `model` | D/O | success | 12 | yes | 0 | 1236 ms | 115 / 1120 |
| doubled_limit | doubled_limit | `model` | D/O | success | 5 | yes | 0 | 237 ms | 117 / 120 |
| timeout_prone | timeout_prone | `upscale` | D/O | timeout | 0 |  | 0 | 55 ms | 29 / 26 |

Categories covered: doubled_limit, exact_identifier, filter, hit, rare_no_hit, timeout_prone, workflow.

## Doubled-limit detail

Query `model` with `limit=3` fetched 5 rows (distillation=2, others=3). Each pass is capped independently at 3; the executor applies no global trim, so a query that hits both distillations and other kinds can return up to `2 × limit`. Task 1.8 / 3.3 enforces one global limit.

## Failures on real queries

These are failures on **ordinary** queries (default 30 s client timeout) — not the artificial `timeout_prone` probe. They are the strongest evidence that `ILIKE` over the un-indexed `unified_feed` view is unstable for some queries, which is the core problem the indexed lexical work in Phase 1 addresses.

| query | pass | outcome | latency | detail |
|---|---|---|---:|---|
| `controlnet settings` | others | `http_error` | 3137 ms | HTTP 500 Internal Server Error |
| `WanVideoSampler` | others | `timeout` | 30043 ms | timeout: TimeoutError |
| `model` | others | `timeout` | 30039 ms | timeout: TimeoutError |

Under the deployed `executors/search/run.py`, only `urllib.error.HTTPError` is caught (→ exit code 2). A `URLError`/`socket.timeout` (the 30 s timeouts above) is **not** caught and surfaces as an unhandled traceback. The 500 is caught but yields zero results. This baseline tool classifies every outcome instead.

## Timeout classification (artificial probe)

`timeout_prone` used a client-side 0.01 s timeout on an ordinary query (`upscale`). Outcome: `timeout` (timeout: URLError) at 29 ms. This only exercises the classification path; it does not load the database. It is reported separately from the genuine timeouts above.

## Reproduce

```bash
python3 scripts/baseline_search.py --dry-run                       # review the request plan
python3 scripts/baseline_search.py \
  --out-json docs/hybrid-search/phase0-baseline-results-2026-07-28.json \
  --out-md  docs/hybrid-search/phase0-baseline.md
python3 -m unittest tests.test_baseline_search                     # offline tests
```

## Relevance judgments — deliberately deferred (task 0.6)

No relevance grades are fabricated here. The rows above are **observed**. Task 0.6 will judge the stable `(kind, item_id)` identities returned by these queries (and ~100 more) and feed them to the evaluation harness from task 0.5. Until then this file contains measurements only.
