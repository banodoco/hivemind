# Simplified execution tasklist — NOT AUTHORIZED TO EXECUTE

Role assignments and budgets are defined once in [run.yaml](./run.yaml). Luna coordinates/implements/does normal review; Sol supplies XHARD work and oracle judgment; Astra performs final holistic review. This replaces the prior host-as-oracle/final-only arrangement under the user's request to revisit the plan. Product scope and 5–8 engineering-day estimate remain unchanged.

## Coordination

Follow plan.md “Coordination and oracle requests.” Routine dispatch and clear corrections stay with the coordinator; ambiguous direction, disputes and exceptions go to the oracle. The two declared review stages have at most five rounds combined; oracle calls have a separate cap of three. Record counters before dispatch and retain them through fixes/restarts. No new product orchestration service.

## 1. Resources and approval

North Star: stable identity, immutable exact-content approval; avoid duplicate proposal stores. Criteria P1–P3.

- T1: One inventory of current schema/callers and affected data. Verify a matched local PostgreSQL+pgvector runtime and Deno, reuse disposable rehearsal scaffolding. Identify the actual cutover dependency set and legacy visibility mapping; no new search tooling or repeated census.
- T2 (T1): Add resources/revisions, origin uniqueness and provenance, editor bit, immutable content, pending→terminal decisions and trusted transactional approval with base/head ownership checks. Preserve stable IDs. Test contributor denial, retry, rollback, stale base and competing approvals with real DB transactions.
- T3 (T2): Update contribution protocol/CLI for initial submission, revision proposal, decision and canonical-guide marker. Full candidate content is the proposal; return readable text/JSON diffs and status. Reuse retry/idempotency mechanism or add minimal caller-scoped token. No automatic merge or editor service.

Proof: pending content does not replace the accepted head; the approved diff matches immutable content; two competitors cannot both publish from one base. No separate gate here; the integrated foundation is reviewed at C1.

## 2. Specific references and evidence

North Star: honest provenance and exact evidence; avoid graph/media systems. Criteria P1–P3. Depends on T2 data identities; fixtures and parser work can proceed alongside T3.

- T4: Implement only resource/message/revision/evidence targets and exact revision/snapshot pins, with the two documented authoring forms and a canonical string-ID shape. Small parser helpers, target/owner validation and a derived link index; no generic graph, extension registry or relation ontology. Test escaped/code literals, unknown targets, mismatched pins and connection-only links to unreviewed resources.
- T5 (T2,T4): Immutable evidence records containing claim, conditions/result/basis, exact subjects, sources and superseding link. Capture current observed messages through trusted DB code when used as evidence; distinguish authenticated reporter from original author. No rewrite of external ingestion, no fabricated history.
- T6 (T5): Seed guide → evidence → workflow revision + message snapshot. Publish a later workflow revision and change the current message mirror; old evidence still resolves to original inputs. Report-only/contradictory evidence remains visibly attributed. External artifact URLs remain ordinary unretained source metadata. No media IDs/tables, uploads/storage/archival work.

Proof: exact evidence scope survives revisions and source edits; approval does not turn reported claims into verified facts.

**C1 — foundation checkpoint:** T2–T6 complete, including T3 contribution surface, with focused DB/protocol/reference/evidence checks passing. Assemble the fixed review packet for H1–H3/T2–T6 from implementation-criteria.md, frozen source/diff and test receipts. Invoke `foundation_completion` from run.yaml (Luna medium, up to two rounds). It checks the whole foundation before dependent search/conversion implementation. On PASS proceed; clear defects receive fixes/tests then affected verification. Unresolved judgment goes to the oracle, not a coordinator waiver.

## 3. Existing search, conversion and handoff

North Star: one working backend using existing infrastructure, simple direct cutover. Criteria P1–P3. Independent inventories, search fixtures and docs drafts may start earlier. Search/conversion implementation consuming the new contract proceeds after C1 passes; final integration consumes all completed tasks.

- T7 (C1 PASS): Adapt search/get and existing lexical/semantic/queue/hash/manifest paths to current accepted resources, removing distillation branches. Preserve rankings, models, configurations, message behavior and raw-table search performance. Keep resource IDs; add explicit revision freshness only where needed. Old jobs/index rows cannot surface or overwrite a newer head; pending content does not rank. Test same-content revision changes and late jobs with deterministic vectors. Do not disable existing search paths, rewrite search, or build a new benchmark programme.
- T8 (C1 PASS for conversion implementation; independent source inventory/fixtures can precede it): One conversion script/migration and rehearsal for resources/distillations/cites, preserving data/provenance/lifecycle. Remove old confidence semantics/fuzzy duplicate RPC and parallel contribution endpoint after actual caller inventory. Update all three ingestors' envelopes while keeping extraction and dry runs. No compatibility shims. Document one export, direct transaction/code cutover and reopen-writes rollback boundary.
- T9 (interfaces from T3–T7): Update descriptors/pack/active docs/skills and examples to create, propose, inspect diff, adjudicate and cite evidence. Editorial rules cover extend/new/split/consolidate, conditions/conflicting methods and canonical marker. Existing refresh_media functionality stays unchanged; media retention is deferred.
- T10 (T1–T9 complete): Run the integrated small-fixture Python/Deno/real-DB proof, including conversion and all affected existing search paths. Once passing, assemble the full artifact-based H1–H7 packet (North Star, goal/plan, integrated diff, actual test/conversion/search evidence). This task's completion triggers the `final` stage in run.yaml: Astra medium, up to three total rounds, completion plus strategic coherence of the whole deliverable. Later rounds verify corrections/affected integration; no separate final oracle or XHARD review. Clear corrections stay with Luna, disputed judgments go to the separately budgeted Sol oracle. Stop early on PASS; preserve stage/overall counters and report unresolved blockers at a cap.

T10's task-complete status means its integration tests are done, not that the run is complete. Delivery completion also requires both declared review stages to pass with no unresolved required criteria.

## Validation commands and limits

Existing commands:
- `python3 -m unittest discover tests/`
- `deno test supabase/functions/contribute/protocol_test.ts`

Focused planned tests: `python3 -m unittest discover -s tests -p 'test_revisions*.py'` and equivalent references/evidence modules.

Planned real DB harness (create by adapting existing rehearsal utilities, not currently available):
`python3 tests/integration/run_knowledge_model.py --database-url-env HIVEMIND_TEST_DATABASE_URL --scenario all`
Support focused `revisions`, `evidence`, `search_freshness`, `conversion` scenarios. Use a disposable local DB; refuse production/non-disposable targets. A mock HTTP suite does not prove concurrent approval or vector SQL freshness.

Fixtures: <=100 resources, <=1000 references, small text/JSON data and deterministic vectors, no blob/media fixtures, GPU, paid embeddings or live writes. Focused checks target <=2 minutes, DB scenarios <=5 minutes, final affected suite <=15 minutes. Reuse installed local runtimes; reassess if setup exceeds 2 GB rather than silently installing a large stack.

## Execution boundary

This tasklist is the future implementation scope, not authorization to execute. No code, tests, installation, migration or deployment in the planning update. Later implementation should recheck source drift and inventory once. Production deployment/cutover remains separately authorized. No huge-run classification, cumulative reviews or packet-review gates.
