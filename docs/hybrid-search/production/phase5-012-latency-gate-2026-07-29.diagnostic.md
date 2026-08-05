# Phase 5 — 012 warm-latency gate: FAILED (timeout), 2026-07-29

Status: **GATE FAILED — task 1.11 NOT complete.** Read-only validation pass only.
Production path left on 012; nothing mutated, rolled back, or re-provisioned.
Model: GLM 5.2 via Claude Code (no other model dispatched).

## What was run
Exactly one clean probe, single persistent `psql` session, direct redirection, no
outer `timeout`, no pipe, no `tee`:

    python3 scripts/probe_golden_latency.py --prime 1 --timed 1 \
      > docs/hybrid-search/production/phase5-012-latency-gate-2026-07-29.json \
      2> docs/hybrid-search/production/phase5-012-latency-gate-2026-07-29.stderr

## Outcome
- Process exit: **failed, code 1**.
- Cause (from .stderr): `subprocess.TimeoutExpired` at `probe_golden_latency.py:83`
  — the single persistent `psql` session did **not** finish all 112 cases within its
  internal `subprocess.run(timeout=1800)` budget. NOT an auth failure, NOT a
  pooler/circuit-breaker error, NOT a query/plan error. The session ran healthy for
  the full 1800s, then the Python timeout killed `psql`.
- stdout (.json): **0 bytes — empty — not valid JSON.** The probe writes its JSON
  only after `subprocess.run` returns; on `TimeoutExpired` it raises before parsing,
  so **0 of 112 cases were measured**.
- stderr: 1841 bytes; secret-pattern scan = 0 matches (no credentials/tokens).
- Measured metrics (p50/p95/p99/max/cases_over_750ms/slowest): **none produced**.
- n_measured = 0; missing = all 112 (no Execution Time lines captured; partial stdout
  lost with the exception). `psql_rc`: not present (no report written; failure was a
  Python `TimeoutExpired`, not a psql return code).
- Gate boolean (`n_measured=112` ∧ no-missing ∧ `warm_p95<=750ms`): **FALSE**.

## Why the timeout itself proves the gate fails
Per-case the session ran 1 prime `count(*)` + 1 `explain (analyze)` against
`public.hivemind_lexical_candidates(...)` with the case filters → 224 candidate
executions total. `statement_timeout='120000ms'` caps each at 120s. To finish inside
1800s the mean execution must be ≤ ~8s; the gate needs warm p95 ≤ 750ms. The session
could not finish in 1800s, so either (a) mean latency ≫ 8s ⇒ p95 ≫ 750ms, or
(b) a subset of cases hit the 120s cap ⇒ those are missing ⇒ `n_measured<112`.
Either branch fails the gate. The timeout is therefore itself sufficient evidence.

## Workload composition (aggregates only — no query strings)
- n_cases = 112.
- **86/112 cases (77%) are query-only with NO narrowing filters** (kinds/sources/
  item_ids/channels/authors/since all unset) — the broadest, most expensive scans.
  Likely the bulk of the elapsed time.
- Filter presence: kinds 14, item_ids 8, channels 4, authors 4, since 4 (all
  cardinality 1). sources 0.
- Notable categories: workflow_code 32, exact_name 25, snowflake 18; plus
  no_hit 8, long_resource_chunk 4, timeout_prone 3 — markers of expensive patterns.

## Recommended next-operator actions (GLM optimization pass — not done here)
1. **Instrument the probe to stream partial results.** It is all-or-nothing: on
   timeout it yields zero measurements. Write per-case timing as each case completes
   (incremental JSONL or per-statement parsing), so a timeout still returns data and
   tells us *which* cases blow the budget and *how far* the session got in 1800s.
2. **Distinguish "slow" from "statement_timeout-killed".** Capture per-case
   `EXPLAIN (ANALYZE)` Execution Time AND detect statements that hit the 120s cap
   (they produce no Execution Time line → currently indistinguishable from "not
   reached"). The 86 query-only cases are the prime suspects.
3. **Characterize, then optimize** the candidate function on the broad (unfiltered)
   path — index/MV usage, filter push-down for the 0/100-kinds/sources defaults,
   and any sequential scans on the 012 MV that the p95≤750ms target cannot tolerate.
4. Re-run the gate with the instrumented probe (still single persistent session).
   Do NOT relax the gate (`n_measured=112` ∧ no-missing ∧ warm `p95<=750ms`); do NOT
   claim 1.11 complete until it passes.

## Production state (unchanged, read-only)
012 path intact (phase2 apply / phase3 refresh+verify / phase4 security+mv+smoke
evidence all present and untouched in this directory). This pass added only:
`phase5-012-latency-gate-2026-07-29.json` (empty), `.stderr` (traceback), and this note.
