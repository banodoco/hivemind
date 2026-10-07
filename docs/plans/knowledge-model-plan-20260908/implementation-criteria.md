# Hivemind implementation acceptance contract

The delivery candidate has been implemented and locally validated. Final holistic certification is still pending; the final packet is the authoritative disposition for H1–H7. Review packets use the H IDs below. Production deployment, database cutover, and live corpus writes remain out of scope.

| ID | Required outcome | Tasks | Required evidence/scenarios | Dependencies |
| --- | --- | --- | --- | --- |
| H1 | Resource identity, immutable proposed revisions, exact diffs and authorized atomic approval work through DB + contribution API/CLI | T2,T3 | `run_knowledge_model.py --scenario revisions`, protocol tests, foundation review packet | PASS — foundation round 2; final recheck pending |
| H2 | Specific resource/message/revision/evidence refs resolve with exact ownership and version semantics | T4 | `--scenario references`, resolver evidence, active Deno protocol tests, foundation review packet | PASS — foundation round 2; final recheck pending |
| H3 | Evidence and on-demand observed message snapshots preserve the exact inputs claimed | T5,T6 | `--scenario evidence`, v1/v2 and snapshot-pin fixture, foundation review packet | PASS — foundation round 2; final recheck pending |
| H4 | All existing affected search paths work with current accepted resources and retired distillation type | T7 | Integrated `all` scenario, `search_freshness`, current-head focused tests, revision-aware jobs/lexical/semantic source diff | Implemented and locally proven; final disposition pending |
| H5 | Direct conversion preserves useful data/provenance/lifecycle and removes old active surfaces without shims | T8 | `conversion` scenario, converter rehearsal/apply boundary, three ingestor tests, caller-inventory diff | Implemented and locally proven; final disposition pending |
| H6 | Complete agent-facing backend and documentation describe the implemented flow consistently | T9,T10 | descriptor/pack/docs/skill diff, Deno suite, full Python suite `1498 OK (17 skipped)`, integrated scenarios | Implemented and locally proven; final disposition pending |
| H7 | Integrated design advances the North Star with the agreed minimal scope and coherent boundaries | T1–T10 | whole delivery diff, North Star/plan, review packets, no-media/no-shim/no-redesign inspection | Implemented; final holistic disposition pending |

Commands and fixture limits are in tasklist.md. Executable receipts must record candidate SHA/snapshot, command/input/environment, actual result and artifact path/digest. Fixtures prove system handling of a reported run, not that a real GPU experiment occurred. Source inspection supplements tests; a passing mocked transport test alone cannot satisfy transactional or vector-SQL behavior.

C1 completion scope is H1–H3: do not require H4–H7 work to be finished at that stage. The final stage covers H1–H7. Final strategic suggestions block only when they demonstrate a conflict with these outcomes/non-goals or a concrete implementation defect; otherwise they are optional. Corrections invalidate only affected criteria and their dependency closure.
