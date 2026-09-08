# Hivemind knowledge backend — simplified implementation plan

Status: updated by the host under the user's scope/review changes. Planning only; implementation is not authorized. Source baseline: abe41fdf72df3bbcfe45087eae64ccf50a1bb809. Previous plan/reviews are historical under archive/ and findings/; they do not certify this update or any executable code.

## Outcome
Agents and humans create resources and propose revisions through the same contribution API. An editor accepts exact content. Guides, workflows, articles and focused answers share one resource lifecycle; distillation becomes provenance rather than a separate entity. Evidence records what was reported or observed about an exact revision or message snapshot. Existing search continues to work with this model.

Keep the implementation small: a pending revision is the proposal, not a second content store. Draft locally, submit full proposed content, compute the diff, and publish atomically. No media subsystem, graph framework, search redesign, compatibility layer, editorial scheduler or intermediate model-review gates.

## Data and approval

- `resources`: stable ID, current published revision pointer, creator/time, optional origin_source/origin_external_id with non-null uniqueness, and a simple promoted flag for canonical guides.
- `resource_revisions`: resource ID, base revision, kind/title/Markdown body/payload/metadata/provenance, authenticated submitter/time/rationale, pending/accepted/rejected/withdrawn state and decision attribution. Submitted content/base are immutable. Only pending → one terminal decision is allowed. Historical accepted revisions remain retrievable; the resource pointer selects the current one.
- `evidence`: immutable claim text, exact subject(s), conditions, reported result, reported/observed basis, submitter/time, sources and optional superseded evidence ID. Claim lives in this record: no separate claim table. Reported/observed describes the submission, not an automatic verification guarantee. Editorial acceptance never certifies a run occurred.
- `message_snapshots`: capture the current observed Discord row through the trusted evidence write path, retaining content/source metadata and capture time. Preserve new differing observations, not a fabricated complete history. Existing ingestion/current-message mirror stays unchanged.
- A small derived reference index: source revision or evidence ID, one of the supported target types, target ID and optional version ID. Validate concrete targets and owner/version matches. The index is derived from submitted content/structured evidence; it is not independently editable and has no arbitrary relationship vocabulary or graph-query service.

Original author/source URL/capture/generator details live in revision provenance; authenticated submitter is separate. IDs and version IDs are decimal strings at JSON boundaries. Preserve original workflow payload semantics; a text diff and a bounded structural JSON diff describe exactly the candidate being approved.

Reuse existing contributor-key auth; add `contributors.is_editor` with operator-only SQL grant/revoke. Ordinary contributors cannot approve or promote. Actor comes from authentication, not a public actor field; trusted DB functions are not directly callable by anonymous clients. Approval locks the resource, checks pending state and base == current head, records the decision and moves the pointer in one transaction. Two competing proposals cannot both publish from the same base. A stale proposal must be reconciled and submitted again; no automatic merge. Retry-safe submission/decision uses the existing equivalent mechanism or a small caller-scoped token, not a new service.

Publication makes current content searchable. Promotion is only a canonical-guide marker, not another approval state. An editor can set it for an accepted guide; changing kind away from guide clears it. Editorial instructions explain when to extend a guide, create a distinct one, split or consolidate it, and preserve competing methods/conditions. Do not implement these judgments as a policy engine.

## Specific references only

Support this closed set:

- `[resource:42]` and `[resource:42@7]` for identity/current head or exact revision.
- `[message:123]` and `[message:123@456]` for current source or captured snapshot.
- `[revision:7]` for an exact proposed or historical revision.
- `[evidence:8]` for an evidence report.

Labelled form may use the same tokens as Markdown destinations, e.g. `[training guide](hivemind:resource:42@7)`. Both normalize to `{type,id,version_id?}`. Implement these exact forms with small parsing helpers, code-span/fence/escape exclusions and focused tests. Do not build a comprehensive Markdown processor, extension registry or generic URI/graph framework. Ordinary HTTP links remain ordinary links. Sections, quote offsets, time ranges and media IDs are deferred.

References promise connection, not endorsement. A link to an unreviewed resource does not block publication. Unpinned resolution selects the accepted head when present, otherwise returns identity + unreviewed status without choosing an arbitrary proposal. Explicit revision links show exact content/state. Evidence subjects pin resource revisions or message snapshots; later changes never silently advance them. Reference validation checks existence and ownership, not the truth of linked content. Bounded outgoing links/backlinks are enough; no recursive expansion.

## Evidence without media infrastructure

An agent can submit “workflow 42 revision 7 completed on a 4090 with these settings,” with conditions, result and source snapshots. A guide cites that evidence; a later workflow revision leaves the claim scoped to revision 7. Corrections/reproductions are new attributed records.

Existing message attachment metadata and ordinary external output/log URLs can be retained as source information. They are not immutable captured bytes and may expire or change. Capture no blobs and promise no archival guarantee. No media table/IDs, uploads, storage adapter, hash-based asset store, download service, retention policy or orphan-cleanup machinery in this implementation. Existing `refresh_media` behavior remains as-is. Standalone durable media is a later addition, not a prerequisite or hidden task in this plan.

## Preserve and adapt existing search

Adapt the infrastructure to resource revisions and removal of distillations; do not change ranking, retrieval algorithms, embedding models/configuration, indexing strategy, or message search behavior. Do not disable/defer functioning lexical or semantic paths merely to reduce this scope. Remove old distillation branches and update their callers/tests to answer resources.

Keep resource item IDs stable. Reuse current representation/chunk hashes, manifests, jobs, leases/retries and stale-finalization checks. Carry an explicit resource revision/expected revision where needed to distinguish the indexed head; avoid a parallel versioning system. Search current accepted resources, not historical/pending revisions. An old job/result must not overwrite or rank as a newer head. An acceptance enqueues existing index work; canonical promotion does not change content or regenerate embeddings. Historical retrieval remains separate from default discovery.

Preserve per-token raw-table search and exclusion of full workflow payloads. Use the smallest published-resource projection needed by existing queries, not the known slow multi-source UNION text-search path. Evidence is retrievable by ID/context and explicit kind; it does not become an endorsed default-search result. Explicit unreviewed filters retain state labels.

Current effective integration points: lexical definitions from 003/006/012/013, embeddings/jobs from 022/025/027/029, semantic/manifest definitions from 032–034. Update effective definitions through new migrations. Keep the current machinery; this list is a dependency map, not authorization for a search project or new evaluation programme.

## Direct conversion

One inventory, one affected-data export, one disposable rehearsal, one direct cutover. No shims, dual writes, alias APIs, staged deprecation or repeated migration gates.

Convert external_resources into resource identities/revisions; convert distillations into answer resources, preserving question, answer, conditions, provenance and real lifecycle. Preserve existing resource IDs where practical; use a temporary map for converted distillation citations. Preserve legacy confidence as inert metadata, not active truth scoring. Do not manufacture evidence records from ordinary citations or fabricate missing historical message snapshots. Existing unreviewed content does not become approved by accident; define legacy visibility mapping during the one inventory.

Remove the old distillation table/cites, fuzzy duplicate-distillation checker and special CLI/read branches. Retire the parallel contribute-resource endpoint as part of the same cutover after checking actual callers. Keep all three ingestors' extraction, payloads and dry-run behavior; update their envelopes. A repeat import with stable source identity proposes a revision rather than overwriting content or bypassing approval.

At a separately authorized deployment, briefly pause affected writers, export knowledge tables/schema (not the full Discord corpus), run the tested transactional conversion/dependency replacement, deploy matching code, smoke-check and reopen. Before reopening, restore the old data/code pair if necessary. After new writes, preserve them and repair forward rather than overwrite them with an old export. No production action is authorized by this planning update.

## Repository map

- New migration after current 037, e.g. `schema/038_knowledge_resources.sql`; do not rewrite historical migrations.
- `supabase/functions/contribute/{protocol.ts,index.ts,protocol_test.ts}` and the retired `contribute-resource` endpoint.
- `executors/{_common.py,entity_identity.py}`, contribution/search/get-item executors and all three ingestion executors.
- Existing `postgres_backfill.py`, `embedding_jobs.py`, `embedding_backfill.py`, `canonical_representations.py`, `lexical_documents.py`, `lexical_contract.py`, and affected runtime/rehearsal callers. Update only for the new data contract; historical benchmark documents stay historical.
- All seven executor descriptors, `pack.yaml`, `README.md`, `DESIGN.md`, `skill/SKILL.md`, `AGENTS.md`, and affected tests. Refresh-media behavior remains unchanged.
- Reuse `scripts/rehearse_lexical_security.py` disposable PostgreSQL pattern. Existing local PG16 lacks pgvector; verify a matched PG14/17 binary/extension installation. Deno was absent during exploration. Resolve these ordinary test prerequisites during implementation without a tooling redesign.

## Coordination and oracle requests

The authoritative role/model/review declaration is [run.yaml](./run.yaml). This revisit adopts the current role defaults and a single intermediate checkpoint, superseding the earlier host-as-oracle/final-only policy. Product scope and implementation authorization are unchanged.

Luna coordinates ready work, prescribed tests, source integration and counters. It also supplies normal workers and independent normal reviewers. Sol handles XHARD assignments and oracle judgment; Astra handles the final holistic review. Agents fulfilling review roles are independent of implementers even when the model is the same. The current planning session has not been switched or an execution coordinator launched by creating this file.

The coordinator advances on required passing tests and uncontested stage PASS, or routes clear in-scope defects back to workers. It cannot dismiss blockers, change the architecture/acceptance contract or enlarge authority/budgets. Send those decisions to the configured Sol oracle using the five-field request and four-field reply in Megado. Reuse already settled rulings; do not commission a new initial oracle call merely to repeat this plan.

Keep request/reply together under a stable decision ID in status notes. Request: decision; why current instructions do not resolve it; new evidence/prior ruling; recommendation/alternative; blocked work/repeat count. Oracle replies: disposition, decision/reason, concrete next action, return condition. Pause affected dependents only. If the same decision returns twice without evidence/action, diagnose the coordination failure rather than repeat the opinion request. The oracle has three calls total, including diagnosis calls; exhaustion does not give the coordinator oracle authority.

These are agent-run instructions, not a new Hivemind coordinator/oracle service. Hivemind's product editor capability is distinct from this development run's oracle role.

## Delivery, estimate and review

Three implementation work packages remain: (1) resources/approval, (2) references/evidence/snapshots, (3) existing search adaptation/conversion/docs. Estimate remains **5–8 focused engineering days**, with all existing search paths preserved. The main uncertainty is the effective SQL/index dependency closure. No media infrastructure, shims or search redesign are added.

One intermediate checkpoint is sufficient because the foundational approval/identity/evidence contract is a meaningful boundary before adapting search and conversion. A second search checkpoint would substantially overlap the final candidate and evidence, so omit it.

| Stage | Ready when | Scope | Reviewer | Maximum rounds |
| --- | --- | --- | --- | --- |
| foundation_completion | C1: T2–T6 implemented and focused DB/protocol/reference/evidence checks pass | Exact approval/immutability, identity ownership, reference resolution, evidence pins and message snapshots work together | Luna, medium | 2 |
| final | T10: all T1–T9 work integrated and final affected suites/scenario pass | Complete outcome plus strategic coherence/simplicity, preserved search, conversion and docs | Astra, medium | 3 |

C1 gates dependent search/conversion implementation; independent parser fixtures, inventories and documentation drafts can proceed. No gate per individual task and no separate strategic reviewer after Astra. Stage 1 catches a wrong revision/approval contract before it propagates into indexes and conversion. Stage 2 evaluates the whole deliverable, not merely its last task.

There are **two stages and at most five review invocations overall**, with early stopping at each PASS. Corrections, model escalation, process restart and renamed checkpoints do not reset counts. An XHARD review, if justified, uses the configured Sol reviewer within the same stage's remaining budget, not a new stage. The final explicit Astra assignment remains authoritative. Oracle calls are a separate maximum of three: therefore at most eight review/oracle invocations combined, not a required count. Factual exploration and tests cannot issue hidden review verdicts or replace an unpassed gate.

After findings, the coordinator dispatches clear corrections, runs affected tests and uses the next available round. Disputes go to Sol oracle if calls remain. Keep counters durable before invocation, recover missing results or conservatively count the call, and preserve unaffected evidence. At a cap, pause affected progression and report the remaining decision; continue authorized independent/corrective work without declaring completion or silently adding reviews. User authorization is required to increase fixed budgets; oracle advice cannot grant it.

Focused implementation tests continue throughout. Required proof: approval/diff/auth/immutability and competing acceptance; pinned evidence/snapshot survival; narrow reference parsing and ownership; published-only lexical/semantic discovery and stale jobs; conversion fidelity. Reuse a small seeded scenario, real local DB transactions and deterministic vectors. Run the broad affected Python/Deno/DB suite once at final integration; repeat only affected checks after fixes.

This update changes planning/coordination documents only. No implementation, runtime install, product tests, migration or deployment has occurred.

## Artifact-based review packets

Use the portable [review-contract.md](./review-contract.md) together with run.yaml and [implementation-criteria.md](./implementation-criteria.md). The coordinator does not invent a review brief's goals. P1–P5 planning PASS entries are not implementation evidence.

C1 Luna packet: full North Star, run goal, H1–H3, T2–T6 requirements, frozen foundation diff/source, and actual approval/reference/evidence test receipts. Goal: identify missing or incorrect implementation and required proof gaps, not demand unfinished search/conversion or redesign the product.

Final Astra packet: full North Star/goal/plan, H1–H7, complete integrated diff from base, relevant integration paths and full seeded backend/search/conversion evidence. Goal: verify the complete outcome, then assess strategy/coherence/simplicity against the agreed scope. It must catch interactions and unnecessary machinery; it must not turn optional alternatives into requirements. Later rounds include accepted findings, correction delta and affected criterion evidence, preserving other approvals.

Packets contain an artifact path/digest manifest and coverage rows mapping criteria→expected behavior→command/result/evidence. Missing evidence stays MISSING. Reviewer result is PASS/REWORK/UNKNOWN with classified, cited findings and criterion dispositions; inaccessible/stale artifacts cannot yield PASS. This is template-based agent assembly, not a new product feature or automated builder already implemented. No additional reviewer or gate is introduced.
