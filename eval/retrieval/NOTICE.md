# Notice — retrieval evaluation harness

## License

This harness ships under the Hivemind repository's MIT License
(`Copyright (c) 2026 Banodoco`). It is Hivemind-owned code: it does **not**
import Pumpernickel at runtime and copies none of Pumpernickel's database
configuration, secrets, or live data.

## Attribution

The reusable evaluation *structure* was ported — with permission — from the
**Pumpernickel** repository (`github.com/peteromallet/Pumpernickel`,
`eval/retrieval/`), an internal same-organisation codebase. Per the Hivemind
hybrid-search plan (AD-8: "Port code, do not create a runtime dependency"), we
ported the narrow algorithms and test patterns and rewrote everything that is
Pumpernickel-specific.

### Ported (algorithm shape, rewritten in Hivemind-owned code)

| Pumpernickel source | Hivemind target | What was carried over |
|---|---|---|
| `eval/retrieval/metrics.py` | `metrics.py` | `recall_at_k`, `reciprocal_rank`, set precision/recall, group/category aggregation, macro-average shape. |
| `eval/retrieval/schema.py` | `schema.py` | "ranked keys + expected keys" identity model, golden-case shape. |
| `eval/retrieval/adapters.py` (`IlikeBaselineRetriever`) | `adapters.py` (`LegacyIlikeAdapter`) | The idea of a pure-Python re-implementation of the production substring search as a baseline. |
| `eval/retrieval/runner.py` | `runner.py` | Per-case → aggregate pipeline and JSON report shape. |
| `eval/retrieval/loader.py` | `loader.py` | load/validate corpus + golden set. |

### Added for Hivemind (not present in Pumpernickel)

* **Graded relevance + nDCG@k** (`dcg_at_k`, `ndcg_at_k`) — Pumpernickel's set is
  binary; Hivemind judgments are graded.
* **Latency percentiles** p50/p95/p99 and mean.
* **Failure accounting**: per-case `ok | timeout | error` outcomes, timeout rate,
  error rate, failure rate, and the rule that failures count as 0 recall while
  remaining separately inspectable.
* **Zero-result rate** and **no-hit satisfaction rate** as first-class metrics
  (Pumpernickel required a non-empty expected set and so could not express
  expected no-result queries).
* **Categories as a covering** (a case may belong to several; e.g.
  `exact_name`, `workflow_code`, `single_workflow`, `no_hit`).
* **Snowflake-safe string identity** with `workflow`↔`resource` aliasing and
  explicit ID-safety / ambiguity rejection.
* **The one-command comparison CLI** (`compare.py`) that runs many systems and
  emits combined JSON + Markdown in a single invocation (Pumpernickel ran one
  adapter per invocation and merged offline).
* The `RemoteSearchAdapter` extension point and the deterministic
  `OracleAdapter` / `ReverseAdapter` / `ErrorAdapter` / `TimeoutAdapter`
  fixtures.

### Intentionally not ported / rewritten

* Pumpernickel's `mediator.*` / dyad / topic / private-visibility / UUID-source
  identity model — Hivemind uses the `(kind, item_id)` citation vocabulary.
* Pumpernickel's `DbBackedRetriever` (async, `DIRECT_DATABASE_URL`,
  `app.services.retrieval`) — replaced by the generic, opt-in
  `RemoteSearchAdapter` that points at whichever Hivemind endpoint is live.
* Pumpernickel's `MiniLMEmbedder` / `OpenAIEmbedder` corpus embeddings — out of
  scope for task 0.5 (semantic backends arrive in Phase 2; this task must not
  call embedding providers).
* Pumpernickel's `corpus.yaml` / `golden_set.yaml` data — not copied; Hivemind
  ships its own small seed fixtures and task 0.6 curates the real 100-query set.

## Extension points for later plan tasks

| Task | How it uses this harness |
|---|---|
| **0.6** (curate 100 queries) | Author `golden.json`/`.yaml` with graded `JudgedItem`s, `categories`, `filters`, and `expect_no_hit`; the loader + schema validate it. |
| **1.11** (lexical eval) | Point `RemoteSearchAdapter` at the lexical RPC, or `register_adapter("lexical", ...)`; add `lexical` to `--systems`. |
| **2.14** (384/1536 + chunk compare) | Register semantic adapters; `by_category["workflow_code"]` / `long_resource_chunk` surface code-vs-dimension recall. |
| **3.10** (hybrid/weighted gate) | Compare `legacy` vs `lexical` vs `semantic` vs `hybrid` vs `weighted`; the MD report's overall + category tables feed the gate verdict. |
| **5.9** (post-backfill re-eval) | Re-run the frozen golden set; `compare_systems` with a fixed `now` gives a reproducible before/after diff. |
