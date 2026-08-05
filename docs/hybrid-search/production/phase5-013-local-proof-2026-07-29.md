# Schema 013 local proof — 2026-07-29

Status: locally accepted; production unchanged; remains on 012.

## Test-first history

The corrected benchmark was run against the unchanged first draft before the
adaptive SQL was written. It failed honestly:

- dense: 012 `45.056 ms`, 013 `63.929 ms`, ratio `1.419` (gate `<= 1.25`);
- sparse: 012 `35.540 ms`, 013 `36.637 ms`, ratio `1.031` (gate `< 1.0`);
- the exact sparse inner plan did not use
  `lexical_documents_python_chunk_trgm_idx`.

The first adaptive attempt was also rejected. Its materialized sparse CTE was
executed for dense searches, causing a `42.674×` dense regression. This led to
the final branch-isolated design; no threshold was relaxed.

## Accepted design

`schema/013_lexical_latency_phase3.sql` remains additive: its only executable
DDL change is one `CREATE OR REPLACE FUNCTION` for
`public.hivemind_lexical_candidates`. It creates no table, materialized view, or
index and changes no grant or revoke.

The function samples at most eight matching rows from the existing
`lexical_workflow_python_search` materialized view and selects one of two
semantically identical anchor strategies:

- dense/common needles use a bounded `LEFT JOIN LATERAL`, preserving the
  schema/012 early-stop behavior;
- sparse/selective needles enter a separate PL/pgSQL branch, materialize the
  direct matching-chunk set, use the existing trigram GIN, select the newest
  matching anchor per item, and retain the result in a function-local JSONB map.

The sparse global scan is not planned or executed on the dense path.

## Final isolated-cluster evidence

The final repeated run passed all gates:

- combined focused suites: `45/45` tests passed;
- functional candidate battery: `26/26`;
- full canonical-row byte parity: all `12/12` query/filter cases;
- cross-chunk negative behavior: passed;
- newest matching anchor: passed;
- quarantine and 1..8000 fragment bounds: passed;
- anon/authenticated/public restrictions and service-role RPC: passed;
- schema/012 proacl preserved: passed;
- rollback stream equality: passed;
- applying schema/013 twice: passed;
- Python compilation: passed.

Final interleaved function-only medians:

| Shape | schema/012 | schema/013 | Ratio | Gate |
|---|---:|---:|---:|---|
| Dense, every chunk matches | 44.852 ms | 45.503 ms | 1.015 | `<= 1.25`, pass |
| Sparse, one oldest match/item | 35.865 ms | 29.167 ms | 0.813 | `< 1.0`, pass |

The sparse proof used `100,000` non-candidate planner-scale decoy rows so
PostgreSQL could choose naturally between a sequential scan and the existing
trigram index. The exact marker-delimited inner statement was explained without
`enable_seqscan=off` or any forced-index substitute. Its plan used
`lexical_documents_python_chunk_trgm_idx`.

These are local relative timings, not the production `750 ms` latency gate.

## Reproduce

```bash
python3 -m unittest \
  tests.test_lexical_latency_phase3 \
  tests.test_rehearse_lexical_013 -v

python3 scripts/rehearse_lexical_013.py

python3 -m py_compile \
  scripts/rehearse_lexical_013.py \
  tests/test_rehearse_lexical_013.py \
  tests/test_lexical_latency_phase3.py
```

Machine-readable evidence:
`docs/hybrid-search/production/phase5-013-local-proof-rehearsal-2026-07-29.json`.

## Production posture

Do not infer production readiness from this local proof alone. A production
apply still requires the plan's explicit preflight, canary, latency, parity,
security, and rollback gates.

Safe rollback after any future controlled apply is to reapply the schema/012
candidate function. No schema/013 object needs to be dropped.

production unchanged; remains on 012.
