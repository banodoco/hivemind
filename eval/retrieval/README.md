# Hivemind retrieval evaluation harness

A Hivemind-owned, stdlib-first harness for comparing search systems (legacy
ILIKE, lexical, semantic, hybrid, weighted) against a **graded** golden set. It
ports the reusable structure of Pumpernickel's `eval/retrieval/` and extends it
with nDCG, latency percentiles, and explicit failure / zero-result / no-hit
accounting. See `NOTICE.md` for attribution and the extension points for later
plan tasks.

## The one command

```bash
python3 -m eval.retrieval.compare \
    --systems legacy,stub,oracle \
    --corpus eval/retrieval/fixtures/corpus.json \
    --golden eval/retrieval/fixtures/golden.json \
    --out-dir eval/retrieval/reports \
    --name phase0-seed
```

This runs every named system over the same golden set + corpus and writes, under
`--out-dir`:

* `<system>_report.json` — per-system report (Pumpernickel-compatible layout),
* `comparison_<name>.json` — combined machine-readable report,
* `comparison_<name>.md` — human-readable comparison (overall relevance,
  operational metrics, latency p50/p95/p99, per-category Recall@10/nDCG@10/MRR,
  and per-query diagnostics).

The command also echoes the headline tables to stdout. Defaults point at the
shipped seed fixtures, so it runs with no arguments.

`--list-adapters` prints every registered adapter.

## What it measures

Per system, over **judged** cases (cases with at least one relevant item;
failures count as 0):

* **Recall@1/5/10**, **MRR**, **nDCG@10** (graded), **MAP**.
* **Latency** p50 / p95 / p99 / mean (successful calls only).
* **Zero-result rate**, **failure rate**, **timeout rate**, **error rate**,
  **no-hit satisfied rate**.
* Breakdowns by **category** (`exact_name`, `workflow_code`, `paraphrase`,
  `single_workflow`, `no_hit`, …) and by expected **entity kind**.
* **Per-query diagnostics**: top-1 id, Recall@10, and outcome per system.

No-hit queries (expected to return nothing) feed `no-hit satisfied`, **not**
Recall — so an honest empty result is not penalised as a recall miss.

## Golden-set schema (summary)

A case (JSON or YAML):

```json
{
  "id": "GC03",
  "query": "block_swap",
  "expected": [{"kind": "workflow", "item_id": "2580", "grade": 3}],
  "categories": ["single_workflow", "workflow_code"],
  "filters": {"kinds": ["workflow"], "item_ids": ["2580"]},
  "limit": 20,
  "notes": "reviewer note"
}
```

* `kind` ∈ `message | resource | distillation` (+ `workflow` alias of
  `resource`). `item_id` is **always a string** (Discord snowflake-safe).
* `grade` ≥ 1 = relevant (higher = better); `0` = explicitly not relevant.
* `categories`: a case may carry several; the report breaks down by each.
* `filters`: `kinds`, `sources`, `item_ids` (requires exactly one `kinds`
  value — ambiguous cross-kind bare ids are rejected), `since`, `mode`,
  `channels`, `authors`.
* `expect_no_hit: true` for expected no-result queries.

Malformed or unsafe ids (non-string, blank, over-length, whitespace, control
chars) and ambiguous `item_ids` are rejected at load time.

## Adapters

| Name | Kind | Notes |
|---|---|---|
| `legacy` | real | Offline model of the current two-pass `ILIKE` search (distillations first, each pass capped at `limit` → up to `2×limit`; ignores `item_ids`/`channels`/`authors`, faithfully). |
| `stub` | fixture | Always empty (no-result floor). |
| `oracle` | fixture | Returns judged relevant items in grade order (perfect ceiling). |
| `reverse` | fixture | Anti-legacy ordering over the same matches (order/tie tests). |
| `error` | fixture | Always raises (proves error accounting). |
| `timeout` | fixture | Always times out (proves timeout accounting). |
| `remote` | extension | Generic opt-in adapter for a live search URL (tasks 1.11/3.10/5.9). |

Add a real backend with `register_adapter(name, factory)` (see `adapters.py`).

## Tests

```bash
python3 -m unittest discover tests/        # whole repo
python3 -m unittest tests.test_retrieval_eval -v   # this harness only
```

The suite is deterministic and offline: metric math, ties/order, malformed
schemas, snowflake round-trips, adapter errors/timeouts, report stability
(latency stripped), the one-command CLI, and opt-in remote/ network guards.
