# Phase 0 — Task 0.6 Curated & Judged Golden Set

**Date:** 2026-07-28
**Task:** 0.6 — Curate and judge at least 100 representative Hivemind queries
(exact identifiers, paraphrases, filters, long resources, workflow-only /
single-workflow / code-only evidence, no-hit cases).
**Plan:** `docs/architecture/hivemind-hybrid-search-plan.md` (Golden set section).
**Prereqs:** 0.2 schema/eligibility map, 0.3 inventory, 0.4 legacy baseline, 0.5
evaluation harness, 0.7 capacity model (complete; untouched).

This deliverable is a **graded, live-validated golden set** of real Hivemind
queries plus deterministic validation/audit tooling. It does **not** implement
lexical/semantic/hybrid retrieval and does not start task 0.8 or Phase 1. Every
judged identity was validated against the live public corpus at capture time;
Discord snowflakes stay strings; no full bodies, `payload.python_source`,
credentials, or anon keys are persisted.

## Files produced

| Path | Role |
|---|---|
| `eval/retrieval/golden/golden-v1.json` | **Production golden set** — 112 judged cases (version `golden/2026-07-28/v1`). |
| `eval/retrieval/golden/corpus-v1.json` | **Bounded evidence snapshot** in `unified_feed`/`CorpusItem` shape: the 52 judged items + 6 real distractors. Identity anchor + harness demo (version `corpus-snapshot/2026-07-28/v1`). |
| `eval/retrieval/golden/evidence-v1.json` | **Live evidence** captured 2026-07-28T19:15Z: all distillations+cites, all 2,757 workflows (id/title/source/python-cohort flag/semantics), symbol→workflow and exact-name→workflow maps, message/channel samples. The drift anchor. |
| `eval/retrieval/golden/curation-suggestions-v1.json` | Intermediate curation aid (per candidate query → matched real identities). Derivable from `evidence-v1.json`. |
| `eval/retrieval/golden/validation-live-2026-07-28.json` | Opt-in live identity re-validation output (52/52 present). |
| `eval/retrieval/golden/comparison_phase0-golden-v1.{json,md}` | One-command harness comparison (legacy vs oracle) over the snapshot — proves reportability. |
| `scripts/golden_probe.py` | Read-only live discovery/evidence-capture tool. |
| `scripts/validate_golden.py` | Deterministic offline validator + opt-in live identity check. |
| `tests/test_golden_set.py` | 27 deterministic offline tests (contract, validator, probe helpers, seed-untouched, harness reportability). |

The task-0.5 seed fixture (`eval/retrieval/fixtures/golden.json`, 10 cases) is
**preserved untouched** as a contract smoke test; the production set is a clearly
versioned sibling, not an edit of the seed.

## Commands

```bash
# Offline validation (counts, categories, grades, filters, snowflakes, snapshot integrity)
python3 scripts/validate_golden.py

# Opt-in read-only live identity re-validation (every judged id against unified_feed NOW)
HIVEMIND_EVAL_NETWORK=1 python3 scripts/validate_golden.py --live

# Re-run live discovery (read-only; regenerates evidence-v1.json)
python3 scripts/golden_probe.py --dry-run      # plan only
python3 scripts/golden_probe.py                # live capture

# One-command harness report over the production set (reportability)
python3 -m eval.retrieval.compare \
  --systems legacy,oracle \
  --corpus eval/retrieval/golden/corpus-v1.json \
  --golden eval/retrieval/golden/golden-v1.json \
  --out-dir eval/retrieval/golden --name phase0-golden-v1

# Tests
python3 -m unittest tests.test_golden_set -v          # this task
python3 -m unittest discover tests/                   # whole repo (551 tests)
```

## Counts

- **112 cases**: 104 judged + 8 no-hit.
- **141 graded judgments**: 104 primary (grade 3), 35 strong (grade 2), 2 marginal (grade 1).
  The rubric is exercised across all three positive bands (no grade-0 distractors
  are encoded in `expected`; the relevant set for Recall/MRR is `grade ≥ 1`).
- **52 distinct judged identities**, all re-confirmed live (52/52 present at
  `ujlwuvkrxlvoswwkerdf` on 2026-07-28). Distribution: 24 workflows, 11
  distillations, 17 messages.

### Category coverage (a case may carry several; every required family is present)

| Category | n | | Category | n |
|---|---:|---|---|---:|
| exact_name | 25 | | best_is_distillation | 17 |
| workflow_code | 32 | | best_is_message | 17 |
| multi_term | 10 | | snowflake | 18 |
| paraphrase | 10 | | selective_filter | 11 |
| settings | 7 | | best_is_resource | 8 |
| single_workflow | 8 | | spelling_variant | 8 |
| workflow_python_evidence | 8 | | no_hit | 8 |
| cross_source | 6 | | workflow_only | 6 |
| code_fragment | 6 | | long_resource_chunk | 4 |
| channel_scoped | 4 | | time_scoped | 4 |
| named_author | 4 | | timeout_prone | 3 |
| pending_status | 2 | | | |

All 23 required families (`exact_name, workflow_code, multi_term, paraphrase,
settings, named_author, channel_scoped, time_scoped, cross_source,
best_is_distillation, best_is_message, best_is_resource, long_resource_chunk,
workflow_only, single_workflow, spelling_variant, no_hit,
workflow_python_evidence, pending_status, timeout_prone, selective_filter,
code_fragment, snowflake`) are represented; the validator fails if any is absent.

## Grading rubric (documented in `golden-v1.json` `meta.rubric`)

| Grade | Label | Meaning |
|:-:|---|---|
| 3 | **primary** | Single best / canonical / direct exact match. |
| 2 | **strong** | Clearly on-topic & correct, but not the single best (co-equal or supporting). |
| 1 | **marginal** | Topically related / tangential; useful context, weak match. |
| 0 | not_relevant | Explicit distractor (not encoded in `expected`; relevant set = `grade ≥ 1`). |

**Judgment principle:** grades reflect *what an item actually contains relative to
the query intent*, **not** where the legacy `ILIKE` system happens to rank it. We
never grade a system's current rank as truth.

### Grading process

1. **Discover real identities (read-only).** `scripts/golden_probe.py` queried the
   live public corpus with the publishable anon key: every distillation + its
   cites, all 2,757 workflow resources (id, title, source, `has_python_source`
   cohort flag, model filenames, node-class names, aliases), bounded message
   samples per common term, and the channel list. Every line of human-facing
   output is routed through `verify_access.redact`; no `payload.python_source`,
   no full bodies, no anon key persisted.
2. **Validate exact names and Python symbols.** A targeted read-only pass ran
   bounded `body.ilike` over `external_resources` for each candidate name/symbol,
   returning id+title only (never the matching code). For python-cohort workflows
   whose ids fall beyond a top-N result, an **id-scoped** `body.ilike` confirmed
   presence — this is exactly the single-workflow search contract (`kinds=workflow,
   item_ids=[id]`).
3. **Curate queries and assign grades.** Each query was authored against the
   validated identities with a primary (3) plus supporting strong/marginal (2/1)
   items and a reviewer note citing the live evidence (alias, node class, model
   filename, distillation question/cites, or message snippet).
4. **Fail-safe id handling.** No Discord snowflake is hand-typed: the curation
   builder selects message ids programmatically from the evidence by
   `(author, context[, term])` and `assert`s the row exists; workflow/distillation
   ids come from evidence. (An initial hand-typed draft produced two snowflake
   transcription errors — `…376863` vs `…370863` — which the fail-safe selection
   eliminated; this is precisely the rounding/transcription failure the plan warns
   about.)

## Live validation evidence

`HIVEMIND_EVAL_NETWORK=1 python3 scripts/validate_golden.py --live` re-checks each
distinct judged identity with one bounded read-only `unified_feed?item_id=eq.…`
existence GET:

```
LIVE: 52/52 identities present at ujlwuvkrxlvoswwkerdf
OK: no problems.
```

Each snapshot id is also anchored in `evidence-v1.json` (0 unanchored). Drift
review: `evidence-v1.json` carries a capture timestamp, masked anon-key prefix,
endpoint ref, per-workflow `title_hash` (SHA-256 short), and bounded redacted
title/model/node snippets — sufficient to detect future change without storing
private content.

## Judgment-quality notes (per plan families)

- **Exact identifiers & punctuation.** `FLUX.1` (dotted), `Wan 2.2` / `Wan2.2`
  (spaced vs no-space), `LTX-Video` (hyphenated), `.gguf` / `.safetensors`
  (filename ext), `lightx2v_I2V_14B` (filename fragment). Aliases like `flux.1`,
  `wanvideo`, `sd_xl`, `control net` cover the punctuation-separated forms.
- **Workflow code (AD-4 / 1.4 / 1.7).** Node classes (`BerniniConditioning`,
  `BlockifyMask`, `MochiDecodeSpatialTiling`, `IPAdapterFaceIDKolors`,
  `Flux2Scheduler`, `LoadWanVideoT5TextEncoder`, …), model filenames
  (`ltx-2-19b-ic-lora-detailer`, `wan2.2_animate_14B`), and code fragments
  (`class `, `def `) — validated by body ilike.
- **Python-only evidence (2.6/2.14).** NL questions whose answer is only in
  workflow Python (`ColorMatch` node wiring, `ipadapter_weight_types` source) —
  graded on python-cohort rows (2537/2540/2542/2750/2757/2758), confirmed by
  id-scoped body ilike.
- **Representation precedence (AD-4, no double indexing).** `CFGGuider` and
  `DownloadAndLoadFlorence2Model` judge a **both-cohort** row (2538:
  `payload.python_source` authoritative and the same bytes in `body`) — the case
  the plan flags for precedence / zero-duplicate indexing.
- **Workflow-only & single-workflow (AD-1).** `kinds=[workflow]` and
  `kinds=[workflow], item_ids=[id]` cases; adversarial scoped no-hits where a
  symbol exists globally but not in the scoped workflow (`BerniniConditioning` in
  `[5]`, `ColorMatch` in `[83]`).
- **Long-resource later-chunk (2.6).** `taeltx`, `wan2.2_animate_14B_bf16`,
  `wan2.1_vace_14B` — matches deep in py-cohort bodies (inventory:
  `payload.python_source` p50≈95K, p99≈720K, max≈1.4M chars).
- **Pending/approved behavior (AD-6).** All 11 distillations are `pending`
  (approved = 0 at capture). `best_is_distillation` + `pending_status` cases make
  this explicit: the approved-distillation boost has zero approved rows to act on
  today, so eval must not assume approved coverage (matches the 0.2 map finding).
- **Timeout-prone legacy (0.4).** `WanVideoSampler` (legacy 30 s timeout), `model`
  (legacy 30 s timeout), `controlnet settings` (legacy HTTP 500) — tagged
  `timeout_prone` so the evaluation lane can confirm indexed search fixes them.
- **Selective filters.** author (`QuintForms`, `buggz`, `spacepxl`, `BNDC`),
  channel (`wan_chatter`, `ltx_chatter`, `krea-2-image`), source, date (`since`),
  and kind filters — all reference real values from the live message envelope.
- **No-hit / adversarial.** nonsense terms, a future-date `since`, an
  injection-shaped string (`DROP TABLE unified_feed`), and emoji nonsense.

## What the harness comparison shows (reportability, not a relevance claim)

`comparison_phase0-golden-v1.md` runs `legacy` and `oracle` over the **bounded
snapshot** (`corpus-v1.json`). On the snapshot:

```
Recall@10  legacy 0.6058   oracle 1.0000
MRR        legacy 0.4847   oracle 1.0000
nDCG@10    legacy 0.4954   oracle 1.0000
```

`oracle = 1.0` everywhere proves the judgments are internally consistent and the
set is reportable through the task-0.5 one-command harness. **These snapshot
numbers are not the production baseline** — the snapshot is a bounded subset with
synthesized workflow bodies (public semantics only, no Python) and cannot model
1.4M-char chunking (that is Phase 2.6). The authoritative legacy baseline remains
`phase0-baseline.md` (task 0.4); real lexical/semantic/hybrid numbers arrive in
1.11 / 2.14 / 3.10 against this frozen golden set.

## Gaps and caveats (named, not blockers)

1. **Approved distillations absent.** 0/11 approved at capture → the
   approved-boost weighting (AD-6) cannot be evaluated against real approved rows
   yet. `pending_status` cases record this; revisit when curation promotes rows.
2. **Long-resource chunking is not modeled in the snapshot.** `corpus-v1.json`
   stores bounded bodies; `long_resource_chunk` cases mark *intent* and are
   grounded by the inventory's measured py-cohort body distribution. True
   late-chunk semantics are validated in Phase 2.6.
3. **Message ilike is timeout-prone.** Discovery used common terms with bounded
   limits and 25 s timeouts (0 failures). Rare-term message queries may still be
   slow on the legacy path; indexed FTS (1.3) addresses this.
4. **`recoverable` vs `unavailable` workflow cohorts not split here.** That is
   task 0.8's freeze; this set uses the observable `payload_python` / `body_python`
   / both cohorts only.
5. **Curation is one reviewer.** Grades are documented and reproducible, but a
   second-reviewer pass and adjudication are recommended before the set gates a
   default switch (Phase 6).

## Reproducibility and safety

- Read-only only: every network call is a `GET` with the publishable key (what an
  installed client does). No writes, no schema/function/secret/corpus mutation,
  no deployment. No `vector`/RPC touched.
- `verify_access.redact` masks API keys, tokens, connection strings, and
  publishable keys in all human-facing output; the anon key is never persisted
  (only a masked prefix).
- Workflow Python is validated by **existence** (ilike → id only); suspect Python
  is never turned into a snippet (quarantined by omission). Snapshot workflow
  bodies are synthesized from public derived semantics (title, model filenames,
  node-class names, aliases) — never `payload.python_source`.
- The dirty Hivemind working tree (pre-existing edits to `.astridignore`,
  `executors/_common.py`, `pack.yaml`, `skill/SKILL.md`, `supabase/.temp/*`,
  `tests/test_common.py`, and the untracked 0.4/0.7/VibeComfy artifacts) and the
  completed 0.7 capacity model were preserved untouched. No pre-existing file was
  modified; this task only adds new files under `eval/retrieval/golden/`,
  `scripts/golden_probe.py`, `scripts/validate_golden.py`,
  `tests/test_golden_set.py`, and this doc.

## Completion signal (0.6)

> Golden set has stable item IDs, relevance grades, query categories, and
> reviewer notes.

**Met.** 112 cases with stable `G001..G112` ids, graded judgments (rubric
exercised across primary/strong/marginal), categories (all 23 required families),
reviewer notes per case, explicit filters, and no-hit expectations. Identities
validated live (52/52 present); snowflakes are strings; deterministic
validation/audit tooling and 27 tests pass; the set is reportable through the
task-0.5 one-command harness (oracle = perfect ceiling). The seed fixture is
preserved as a contract smoke test.

## Dependency-safe next task

Task 0.6 has no blockers. Per the plan's dependency graph and critical path, it
unblocks:

- **0.8** — Inventory workflow representation cohorts and freeze the authoritative
  Python precedence, delimiters, recovery/quarantine/no-duplication rules, pilot
  embedding contracts, prose/code chunk candidates, numeric quality gates, and
  rollback criteria. (Depends on 0.2–0.7; this set's representation-cohort and
  pending-status findings are explicit inputs — see G099/G100 and the pending gap.)
- **1.4 / 1.7 / 1.11** — exact-identifier and lexical evaluation will run this
  frozen golden set against the indexed lexical system.

Do **not** begin 0.8 or Phase 1 implementation from this task; it stops at 0.6.
