# Phase-1 Lexical Correction Batch (tasks 1.7–1.11) — GLM 5.2

> Durable plan + results file. This is the compaction-resume point: read this, the
> task list, and recent commits to continue. **Model: GLM 5.2 only.** The
> `arnold_pipelines.megaplan` harness is NOT used (it dispatches to non-GLM models);
> megaplan rigor (brief→plan→critique→gate→verify) is applied manually as GLM 5.2.
> Conceptual tier: `partnered-5/thorough/high`.

## Goal

Finish + prove Phase-1 lexical tasks 1.7–1.11 by fixing the 7 coordinator-found
defects, with live production proof, and sign `PROCEED` only when every gate is green.

## Confirmed state (do NOT re-derive)

- **Live access**: psql path only (`supabase db dump --schema public --dry-run` → short-lived
  CLI-login libpq env, held in child-process env; output via `verify_access.redact`).
  Pooler, port 5432. **HTTP/service-role path does NOT work** (key unset, no Edge Function).
  Reuse `scripts/live_lexical_search.py::{derive_pg_env, psql, elevate}`.
- **Preflight GREEN**: `fn_candidates`, `fn_rpc`, `tbl_lexical_documents`, `idx_python_chunk_trgm`
  all live; 15,265 `lexical_documents` (all `workflow_python`); 0 long txns; rehearsal 18/18.
- **Tests**: 1,060 passing, 5 skipped. `python3` interpreter, `unittest.TestCase`. Skips are
  `tests/test_message_identifier.py::ClusterIntegration` (gated `HIVEMIND_EVAL_CLUSTER=1`).
- **Rehearsal harness**: `scripts/lexical_pg.py` (throwaway PG14 cluster), `rehearse_lexical_candidate.py`
  (forward 001→009), `rehearse_lexical_apply_rollback.py` (reverse rollback). Prod apply = operator psql.
- **Latency (PROVEN BAD)**: WanVideoSampler 13.6s, FLUX.1 5.4s, CogVideoX 5.6s, Mochi 2.8s,
  KSampler 27.9s. Target warm p95 ≤ 750 ms. Candidate fn: `Function Scan`, **2,752,717 shared
  buffer hits** for 100 rows — near-full-scans `discord_messages` (~1.25M rows) per query.
  Probe tool: `scripts/probe_lexical_latency.py`.

## Requirements A–G (acceptance criteria + touchpoints)

### A — Repair refresh verifier (`scripts/live_lexical_refresh.py`)
- Coverage SQL (≈L311-325): add `safe_available_python_rows` = state where `public_state='safe'
  AND available=true AND cohort IN ('payload_python','body_python','recoverable')`; also expose
  `unavailable_safe_rows`, `quarantined_rows` for explicit 3-way partition.
- `ok` (≈L339-345) must require: state_rows==workflows_total; zero unrefreshed; zero quarantined
  doc rows; zero duplicate chunk hashes within item; `python_distinct_items` ==
  `safe_available_python_rows`; per-item stored `chunk_count` == actual doc count; cohort/public_state
  partition sums. (Fixes 221-vs-2,756: denominator was `by_public_state.safe`.)
- Tests (new file, e.g. `tests/test_lexical_refresh_coverage.py`): unit that asserts `ok=true`
  when unavailable-safe rows exist (the bug), `ok=false` when counts drift, hash-skip no-churn,
  quarantine credential-refresh, batch fallback. Pattern: PG-gated `@skipUnless` + in-memory pure-logic tests.
- **Live (serial, owner)**: idempotent rerun in bounded batches; capture source-table checksums
  before/after (prove unchanged); prove all 2,757 rows hash-skip (no derived churn).

### B — Repair evidence capture (`scripts/live_lexical_search.py::capture_evidence`, ≈L275)
- Read RPC count from `response->>'count'`, results from `response->'results'` (RPC envelope
  `{results,count,meta}`). Record both `rpc_smoke_count` and `rpc_smoke_results_len`.
- Regression test pinning the `{results,count,meta}` envelope parse.
- Evidence stays sanitized (no message bodies / workflow source / credentials / snippets).

### C — Additive security migration `schema/010_lexical_security_hardening.sql`
- REVOKE execute on `hivemind_lexical_candidates` from public/anon/authenticated.
- REVOKE all on `lexical_documents` + `lexical_resource_python_state` from public/anon/authenticated.
- PRESERVE: service_role RPC grant (009:268), owner/maintenance access, Phase-2 (020-029).
- DDL only (GRANT/REVOKE; autocommit-safe, no CIC).
- Tests: isolated-cluster + live security tests proving anon/authenticated cannot call either fn
  or read/write either table; service_role can call RPC; rejected/deleted/quarantined never ranks.
- Rehearse apply/rollback/idempotence; **live apply serial after preflight**; live security proof.

### D — Production latency (critical path, additive index/SQL only)
- Read `schema/008` fully; break candidate fn into arms; `EXPLAIN (ANALYZE, BUFFERS)` each arm
  individually (plpgsql fn is a black box to EXPLAIN).
- Identify arm(s) full-scanning `discord_messages` or applying `hivemind_normalize_identifier`
  per-row without a matching expression index; check whether `idx_discord_messages_identifier_trgm`
  matches the normalized-content expression actually queried.
- Fix with ADDITIVE index/SQL. **Do NOT** relax latency/recall gates or drop required arms.
  Preserve exact-name, phrase, FTS, normalized-identifier, bounded workflow-code, filters, identity
  collapse, deterministic order, post-limit hydration.
- Rehearse every change (apply/rollback/idempotence); **live apply serial**; prove warm p95 ≤ 750 ms
  on golden workload; record cold/warm methodology, per-query timeout, failures, outliers.

### E — Reusable secret-safe live eval driver (psql-backed adapter)
- New adapter (NOT `RemoteSearchAdapter`, which needs HTTP). Calls production RPC via psql path.
- Frozen 112-case golden set `eval/retrieval/golden/golden-v1.json` UNCHANGED.
- Exercise RPC + all filters: workflow-only, single-workflow, snowflake IDs, author/channel/date,
  no-hit. Bounded concurrency + durable progress/results file (resume on interrupt).
- Gate evidence: exact-name Recall@10≥0.95; workflow-code exact-match Recall@10≥0.95; single-workflow
  exact scope; no duplicate identities; every response ≤ global limit; quarantined workflow-python
  never ranks; no-hit/security/latency/capacity gates; deterministic repeated order.
- A failing gate must be fixed + rerun. Never sign PROCEED over a live failure.

### F — Audit 222 authoritative Python representations
- Validate without logging contents/secrets. Sanitized counts: syntactically valid, parser-fallback,
  invalid source, secret-quarantined, coverage-failed.
- If failures are extraction/chunking/ingestion bugs → repair code + regression-test.
- Do NOT overwrite authoritative non-empty `payload.python_source`, infer code with an LLM, or
  publish a credential. Invalid/unsafe source stays quarantined/unavailable w/ recorded remediation.

### G — Evidence + final gate
- Replace stale `docs/hybrid-search/phase1-lexical-eval.md` (L13-18, 74-75, 80, 85-92, 108-109:
  schema-003-absent + live-confirm-as-handoff — both false). Report real production verdict.
- Run: focused tests; 003→010 rehearsal; full unittest suite; live preflight; live security checks;
  live evaluation; idempotent refresh rerun.
- Return requirement-by-requirement table for 1.7–1.11 (paths, commands, counts, metrics, live
  object/grant state, remaining blockers). Confirm GLM 5.2 only.

## Hard constraints

- **No commit/push/secret-exposure/provider-switch.**
- **Production mutations are SERIALIZED**: serial, owner-only, in the main thread (me). Never parallel.
  Never two live writes concurrently. Read-only live probes (EXPLAIN, preflight) are OK to delegate.
- **Preserve unrelated ratings work**: do not touch `schema/002_vibecomfy_ratings.sql` or ratings code.
- **Do not touch Phase-2 migrations** (020-029) or their executors.
- **GLM 5.2 only**: subagents via Agent tool (inherit parent model), NOT `subagent-launcher` external models.
- Keep main thread context lean: delegate byproduct-heavy reads/implementation to subagents; do
  live/serial/verification in main thread; write conclusions here, compact at seams.

## Execution order

1. **D-investigate** (subagent, read-only): schema/008 deep-read + per-arm live EXPLAIN + diagnosis + candidate fix.
2. **Code fixes (parallel subagents, disjoint files)**: A, B, C, E-adapter, F-tool — each returns diffs + own-test results.
3. **D-fix** (main thread): implement additive index/SQL; rehearse; **serial live apply**; prove p95.
4. **C live apply** (main thread, serial): rehearse 010; apply; live security proof.
5. **A live rerun** (main thread, serial): bounded batches; checksum before/after; hash-skip proof.
6. **E live run** (main thread): frozen 112-case eval; gate evidence; fix + rerun any failure.
7. **F audit** (subagent/main): 222 reps; sanitized counts; remediation paths.
8. **G final gate** (main thread): full suite; docs; requirement-by-requirement table.

## Status (updated as work completes)

| Req | Status | Evidence |
|-----|--------|----------|
| A | code+tests DONE; live rerun pending (serial) | `live_lexical_refresh.py` evaluate_coverage_ok (7 checks); 17 tests pass (incl 2 cluster) |
| B | code+tests DONE; live capture pending (serial) | `live_lexical_search.py` parse_rpc_envelope; 8 tests pass |
| C | migration+tests+rehearsal DONE; live apply+proof pending (serial) | `schema/011`; 23 tests + rehearsal 16/16 (anon/auth blocked, service_role RPC ok) |
| D | **schema/010 APPLIED** (live); warm p95 pending golden gate | root cause: per-chunk NL on workflow_python arms. 010 = materialized safe_wf + distinct-item fragment subquery. Applied rc=0, post-check ok. Subset warm: p50=399ms; tail (VACE/KSampler/CLIPTextEncode/VAEDecode) ~1-1.6s. Rehearsal: 21/21, canonical_streams_match=true |
| E | pending | psql adapter to build; depends on D gate |
| F | DONE | 222 reps: 221 safe+valid, 1 secret_quarantined (id 2625), 0 invalid/fallback/coverage-failed, 0 drift. No code bugs |
| G | pending | final gate + docs + report |

| C | DONE (applied + proven live) | schema/011 applied rc=0; ACL `{postgres,service_role}=X` (anon/auth revoked). Live proof all_pass: anon/auth DENIED on candidate fn + both tables (read); service_role RPC ok (10|10); quarantined id 2625 has 0 workflow_python docs. |

### CRITICAL operational learning (applies to E + all live probing)
The Supabase pooler TRIPS AN AUTH-FAILURE CIRCUIT BREAKER (`ECIRCUITBREAKER`) and the
short-lived CLI credential ROTATES. Per-query psql connections (new auth each) fail after
~65 calls and get blocked. **All live multi-query work MUST use ONE persistent psql session**
(one auth for the whole run, re-deriving the credential only at session start). This is why
the eval driver (E) and latency probes use a single `psql -f` over a generated script, not
per-query subprocess calls.

### D latency detail (post-010, live, authoritative EXPLAIN Execution Time)
- Representative subset warm: p50=399ms, p95=1616ms. Slow tail = high-frequency ComfyUI
  node-class tokens (VACE 1616, KSampler 1157, CLIPTextEncode 1118, VAEDecode 1028).
- Root residual cost: trigram bitmap heap scan over lexical_documents chunks + message arms
  over 1.25M discord_messages for high-frequency tokens.
- Next: definitive golden-workload p95 (scripts/probe_golden_latency.py). If >750ms, targeted
  additive optimization (per-arm rank-ordered LIMIT caps, or per-item workflow_python search
  index) preserving recall; re-measure. Do NOT relax gates or drop arms.

### Serial live work remaining (owner-only, main thread, one at a time)
1. D: golden p95 → optimize if needed → re-measure. (010 already applied.)
2. C: preflight + apply schema/011; live security proof.
3. A: bounded idempotent refresh rerun; source-checksum before/after unchanged; 2757 hash-skip.
4. E: build psql adapter; frozen 112-case run; gate evidence; fix+rerun any failure.
5. B: live evidence capture (capture_evidence fix) → phase1-lexical-live.json.
6. G: full suite; docs; requirement-by-requirement table.

## Phase-2 schema/012 correction (2026-07-29, GLM 5.2) — root-review gaps

The isolated rehearsal for `schema/012_lexical_latency_phase2.sql` reported 26/26,
but root review found five gaps that made that evidence insufficient. All five are
corrected; nothing here mutates production (the parent coordinator authorizes a
later serial apply). Files: `schema/012_lexical_latency_phase2.sql`,
`scripts/rehearse_lexical_012.py`, `scripts/live_lexical_refresh.py`,
`tests/test_lexical_refresh_mv.py`.

### Gaps found + corrections

1. **Cross-chunk false-positive.** The draft stored `string_agg(normalize(chunk),
   ' ')` and re-applied `hivemind_normalize_identifier()` to the whole
   concatenation in BOTH the GIN index and the LIKE. Because normalize() strips
   all whitespace, the inter-chunk space was erased, so a needle present only
   across two chunk boundaries could match (schema/010 never does — it tests each
   chunk independently).
   **Fix:** the MV adds schema/010's `char_length(chunk_text) between 1 and 8000`
   predicate, and the index + LIKE operate on `search_norm` DIRECTLY (no
   re-normalization). normalize() strips every whitespace char, so a space never
   appears inside a normalized chunk (clean separator) and `v_qn` never contains
   one — a needle can never span two chunks. `search_norm LIKE '%v_qn%'` iff
   `v_qn` is a substring of some `normalize(chunk_i)`: byte-equivalent to 010.
   **Proof:** adversarial fixture item 9100 (chunk0 "...ksamp", chunk1 "ler...");
   needle "ksampler" matches in NEITHER 010 nor 012; a direct check shows
   `corrected_direct=false` while the buggy `normalize(concatenation)=true`
   (the vulnerability was real).

2. **matched_anchor parity.** 010 picks the NEWEST MATCHING chunk's anchor
   (`distinct on (item_id) ... order by item_id, created_at desc`); the draft
   picked the FIRST chunk's anchor regardless of which chunk matched.
   **Fix:** the fragment arm finds matching items via the tiny MV, then a bounded
   scalar subquery re-selects the matched_anchor of the newest matching
   `lexical_documents` chunk (same WHERE + `order by created_at desc limit 1`),
   served by the `lexical_documents_workflow_python_idx` partial index. `created_at`
   stays the resource's created_at (`sw.created_at`), as in 010.
   **Proof:** fixture item 9200 (3 chunks, distinct created_at, only chunk 2
   matches) → 010 and 012 both select `ANCHOR_TWO`; the full candidate row stream
   (incl matched_snippet/rank/source/order) is byte-equivalent.
   **Caveat (pre-existing, NOT introduced):** when multiple matching chunks share
   `created_at` (production refresh writes an item's chunks in one txn → same
   `now()`), 010's anchor selection is itself non-deterministic on the tie; 012
   matches 010's ordering exactly, so it is no worse. Recall (the item set) is
   unaffected; only the snippet anchor could differ on such ties. The rehearsal
   uses distinct `created_at` so the parity proof is deterministic.

3. **MV security posture.** The MV is now `revoke all ... from public, anon,
   authenticated` (same posture as the lexical tables in 011). The rehearsal now
   bootstraps through 011 (so the post-011 `proacl` is the grants baseline) and
   proves: anon/authenticated/public cannot SELECT the MV (`has_table_privilege`
   false + a live `SET ROLE` SELECT errors); 011's candidate-function ACL
   survives 012's `CREATE OR REPLACE` (`proacl` `{postgres=X/postgres}` unchanged
   pre/post); the service-role RPC still returns workflow results; a quarantined
   workflow contributes zero candidates.

4. **Refresh lifecycle + read-path trust distinction.**
   `scripts/live_lexical_refresh.py` refreshes the MV after the
   lexical_documents/state rebuild: `refresh_search_mv` tries
   `REFRESH ... CONCURRENTLY` (unique index `lexical_workflow_python_search_item_uidx`
   present) with a deterministic plain `REFRESH` fallback (also the
   first-population path). `rollback()` refreshes the MV too. The coverage
   verifier gained an MV dimension (`evaluate_mv_coverage_ok` + `_mv_coverage`):
   it DETECTS a stale MV (distinct-item count vs `lexical_documents`) and fails
   the combined `ok`.

   **An absent MV is NOT unconditionally a warning.** It is correct only under
   the *pre-012* read path; it is a hard failure under schema/012 (see the final
   trust/lifecycle correction below). The verdict is decided by which read path
   is live, detected by reading the installed `hivemind_lexical_candidates` body:

   | installed candidate fn body | MV state | coverage verdict | refresh verdict |
   |-----------------------------|----------|------------------|-----------------|
   | pre-012 (no MV reference)   | absent   | compatible pass + warning | compatible no-op success |
   | schema/012 (references MV)  | absent   | **HARD FAIL** | **HARD FAIL (not success)** |
   | either                      | present + stale/unreadable | HARD FAIL | HARD FAIL |
   | either                      | present + fresh | pass | success |

   New tests `tests/test_lexical_refresh_mv.py` (26: pure + shape + two
   isolated-cluster classes) cover the read-path detector, all four coverage +
   refresh absent/present cases, plus initial population, refresh-after-change,
   stale detection, idempotent rerun, and CONCURRENTLY support.

5. **Strengthened rehearsal/evidence.** `scripts/rehearse_lexical_012.py` now
   compares FULL canonical candidate rows (entity/item/representation/
   matched_snippet/lexical_rank/lexical_source/created_at, ORDER, global limit) via
   jsonb for 12 representative + adversarial queries (incl the cross-boundary and
   query-anchor fixtures), plus apply/rollback-to-010/idempotence/ACL-preservation/
   MV-denial/quarantine-exclusion/refresh-lifecycle/no-message-filter-loss.

### Rehearsal verdict (isolated PG17 cluster, throwaway)

`docs/hybrid-search/phase1-lexical-012-rehearsal.json` — `all_pass: true`:
applied_ok; functional 26/26; **full-row parity 012==010 for all 12 queries**;
cross_ok (absent in 010+012, corrected_direct=false, buggy_renormalize=true);
anchor_ok (`ANCHOR_TWO` in both, full-row equal); security_ok (MV denied to
anon/auth/public; 011 ACL preserved; RPC ok; quarantine zero candidates);
rollback_ok; idempotent_ok; grants_preserved (`{postgres=X/postgres}`).

### Final trust/lifecycle correction (2026-07-29, GLM 5.2) — absent-MV is not always a warning

Root review of the five-gap correction found one remaining trust/lifecycle bug:
`evaluate_mv_coverage_ok({"mv_present": False})` always passed and
`refresh_search_mv` always treated an absent MV as success. That is correct only
when the installed candidate function is still schema/010 (pre-012). It is
dangerously wrong when schema/012 is active but its MV was dropped/missing: the
search function then depends on the missing MV and the verifier would falsely
report success. Nothing here mutates production (isolated cluster + unit tests
only); files touched: `scripts/live_lexical_refresh.py`,
`tests/test_lexical_refresh_mv.py`.

**Fix — a precise read-path distinction.**

1. New read-only, sanitized, unit-testable detector:
   - `candidate_fn_uses_search_mv(prosrc)` (pure): True iff a candidate-function
     body references `lexical_workflow_python_search`. The relation name appears
     in the body iff schema/012 is the active read path (schema/008 and schema/010
     bodies never reference it — 0 vs 13 occurrences in the migration sources), so
     a plain substring test is exact.
   - `_candidate_fn_prosrc(cred)` reads the installed `public.hivemind_lexical_candidates`
     body from `pg_proc` (pinned by `pronargs=10`), and
     `search_mv_read_path_active(cred)` returns whether the schema/012 read path
     is live.

2. Coverage rules (`evaluate_mv_coverage_ok`, fed `cov["mv_read_path_active"]` by
   `verify_coverage`): pre-012 + absent ⇒ compatible pass + warning; schema/012 +
   absent ⇒ **hard failure**; present + stale/unreadable ⇒ hard failure; present +
   fresh ⇒ pass. (`mv_read_path_active` defaults false when omitted, preserving
   the historical pre-012 reading.)

3. Refresh rules (`refresh_search_mv`): pre-012 + absent ⇒ compatible no-op
   success; schema/012 + absent ⇒ **hard failure, not success**; present ⇒
   CONCURRENTLY-with-plain-fallback as before.

4. `verify_coverage` only emits the benign "MV not validated" warning under the
   pre-012 read path; under schema/012 + absent it lets the hard-failure reason
   stand (no contradictory benign warning).

5. Tests: pure coverage of all four coverage + four refresh cases plus the
   detector; isolated-cluster (`_ClusterCred` driving the REAL helpers against a
   throwaway cluster) proving (a) 010/011 only ⇒ absent MV compatible; (b) 012
   applied + MV dropped ⇒ coverage AND refresh fail closed; (c) reapply 012 ⇒
   fresh passes. 26 tests, all green.

**Latent typo also fixed:** the unique index that backs `REFRESH ...
CONCURRENTLY` is `lexical_workflow_python_search_item_uidx` (schema/012); the
prior code/doc spelled it `...item_uidz` (the PG-gated assertion could only fail
where binaries were present). Corrected in `live_lexical_refresh.py` and the
test.

### NOT done in this run (operator-gated, serial, main thread)

- Apply `schema/012` to production (the parent coordinator authorizes a later
  serial apply). NOT applied here.
- Run `scripts/live_lexical_refresh.py --apply` (refreshes lexical_documents +
  the MV) and `--verify` (now includes the MV dimension).
- Run the frozen live gate (all 112 golden cases, warm p95 ≤ 750 ms). NOT run
  here — the latency claim is proven separately on production; the isolated
  rehearsal proves correctness/parity/security only.
- The created_at-tie anchor non-determinism is pre-existing in 010 and out of
  scope for this correction (recall is unaffected).
