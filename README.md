# Hivemind

Hivemind is a shared knowledge corpus with a public read path and a locked
write path. Messages remain the source-custodied Discord layer. All authored
knowledge is a stable `resources` identity with immutable full-content
`resource_revisions`; only an accepted revision is the searchable current head.

## Read

Use the stdlib-only executors:

```bash
python3 executors/search/run.py --query "wan animate workflow"
python3 executors/search/run.py --query "lora" --kinds workflow --channel wan_comfyui
python3 executors/get_item/run.py --kind resource --id 42
python3 executors/get_item/run.py --kind resource --id 42 --revision-id 107
```

`search` queries `message_feed` and `resources` with its accepted current
revision relationship. Pending and historical revisions never rank. Resource
IDs are stable; revision IDs are returned when an exact head is included.
`unified_feed` is not a text-search surface. `refresh_media` is unchanged and
media retention remains explicitly deferred.

## Write

Every write goes through `POST /functions/v1/contribute` with
`X-Contributor-Key`. The active actions are:

- `submit_resource`: initial full candidate content, returning pending resource/revision IDs and a diff.
- `propose_revision`: full candidate content plus the accepted `resource_id`/`base_revision_id`.
- `decide_revision`: editor-only `accepted`, `rejected`, or `withdrawn` decision.
- `mark_canonical`: editor-only canonical guide marker.
- `capture_message_snapshot`: trusted observation of a current message.
- `submit_evidence`: immutable reported/observed evidence pinned to exact subjects.

Use `python3 executors/contribute/run.py --type submit-resource --dry-run ...`
or the corresponding `propose-revision`, `decide-revision`, `mark-canonical`,
`capture-message`, and `evidence` types. IDs are decimal strings at JSON
boundaries and every knowledge write has a caller-scoped idempotency token.

The three ingestors keep their extraction and dry-run behavior but emit
`submit_resource` envelopes: article, ComfyUI workflow, and YouTube transcript.
The native payload remains in the revision; URLs remain ordinary provenance.

## Editorial guidance

Extend an existing resource when the method is the same and the candidate is a
clear correction or improvement. Create a new resource when the method,
artifact, or question is materially different. Split when one candidate mixes
independently useful methods; consolidate only when a reviewer can preserve the
source and evidence pins. Put conditions and competing methods in body,
metadata, or evidence explicitly—do not silently flatten disagreements.

An accepted `guide` may be marked canonical by an editor. Canonical marking is
an editorial navigation signal, not proof that reported evidence is true.
Evidence records retain basis (`reported` or `observed`), reporter identity,
exact revision/message-snapshot subjects, sources, and supersession.

## Conversion and proof

`scripts/convert_legacy_knowledge.py` is the one direct conversion boundary.
It supports export-only (default), disposable `--rehearse` rollback, and
explicit local/test `--apply`; it performs no dual write and fabricates no
evidence. Visibility maps to accepted/pending/rejected/withdrawn and old
confidence is inert metadata. T7 freshness, T8 conversion, foundation, and
integration proof run with:

```bash
python3 tests/integration/run_knowledge_model.py --scenario all
python3 -m unittest discover tests/
deno test supabase/functions/contribute/knowledge_protocol_test.ts
```

The real-DB runner refuses non-disposable URLs and uses small fixtures.
