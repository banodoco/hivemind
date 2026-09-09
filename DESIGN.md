# Hivemind knowledge-model design

## Contract

The corpus has three source families: current Discord messages, stable
resources, and immutable resource revisions. `resources.current_revision_id`
is the only head pointer. A revision is full candidate content, not a patch;
its state is `pending`, `accepted`, `rejected`, or `withdrawn`. Terminal
revision content and resource identity are immutable. IDs are bigint in SQL and
decimal strings in JSON.

`knowledge_references` is a derived typed link index. It supports only
revision/evidence sources and resource/message/revision/evidence targets, with
an optional exact target version. `message_snapshots` preserve the observed
input when evidence needs a stable message copy. Evidence is immutable and
explicitly `reported` or `observed`; editorial acceptance never upgrades a
reported claim into a verified fact.

## Search and freshness

The active lexical and semantic paths preserve the existing ranking,
configuration, dimensions, chunking, and message behavior. They read
`message_feed` plus `resources` joined to `resource_revisions` at the current
accepted head. Search never scans `unified_feed` for text. Lexical documents,
embeddings, manifests, and jobs carry `source_revision_id` for resources;
candidate SQL requires that pin to equal the current head. Pending and
historical revisions therefore cannot rank. A same-content new revision keeps
the stable resource ID while changing the revision identity. A late job is
cancelled when its pinned revision is no longer the head.

`schema/039_knowledge_model_delivery.sql` is an additive active-path
replacement after the foundation migration. Earlier migrations are historical;
the delivery migration drops legacy source triggers and replaces active
candidate, hydration, and queue definitions.

## Editorial workflow

Use `submit_resource` for a new method/artifact. Use `propose_revision` to
extend an existing method from its accepted head. Split mixed methods into
separate resources. Consolidate only when all source custody and exact pins
remain recoverable. Put conditions, competing methods, and uncertainty in
candidate text and evidence—not in an uninspectable score. Editors decide one
terminal state and may mark only an accepted guide canonical. Canonical is a
guide/navigation marker, not a truth claim.

## Conversion boundary

`scripts/convert_legacy_knowledge.py` performs one export → transaction →
commit boundary. Legacy external resources retain useful IDs when available;
legacy question/answer rows become ordinary `knowledge` resources with
`legacy_confidence` inert metadata. The explicit lifecycle mapping is:

| legacy visibility | revision state |
|---|---|
| approved | accepted |
| pending | pending |
| rejected | rejected |
| superseded | withdrawn |

Legacy cites become exact `knowledge_references`, retaining message IDs and
mapped resource/revision pins. Missing legacy targets are not fabricated into
evidence. `--rehearse` rolls the transaction back; `--apply` is guarded to a
local/test database. There are no compatibility shims, dual writes, fuzzy
duplicate RPCs, or parallel resource endpoint.

## Explicit deferrals

Media URL refresh behavior is unchanged. Media retention, uploads, archival,
GPU/provider fixtures, and a curation UI are outside this delivery.
