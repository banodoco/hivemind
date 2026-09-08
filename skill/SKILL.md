---
name: hivemind
description: Search Banodoco messages and accepted current resource heads, fetch exact revisions, and contribute immutable resource revisions or evidence.
---

# Hivemind skill

Search the raw index-backed surfaces. Use `hivemind.search` for a topic and
`hivemind.get_item` for the full row. Do not text-search `unified_feed`: its
derived UNION scan exceeds the public statement budget. Search results include
only live messages and the accepted `resources.current_revision_id` head.

```bash
python3 executors/search/run.py --query "wan animate workflow"
python3 executors/search/run.py --query "lora" --kinds workflow --channel wan_comfyui
python3 executors/get_item/run.py --kind resource --id 42
python3 executors/get_item/run.py --kind resource --id 42 --revision-id 107
```

Search resource kinds are `resource`, `workflow`, `article`, `transcript`,
`guide`, and other concrete revision kinds. `distillation` is not an active
search kind. A stable resource ID identifies the subject; a revision ID pins
the exact candidate.

## Contribution

The only active write endpoint is the locked `contribute` edge function. Use
the CLI with a caller-scoped `--idempotency-token`:

```bash
python3 executors/contribute/run.py --type submit-resource \
  --kind article --source web --external-id https://example.test/a \
  --title "A" --body-file body.txt --idempotency-token article-a --dry-run
python3 executors/contribute/run.py --type propose-revision \
  --resource-id 42 --base-revision-id 107 --kind article --title "A" \
  --body-file body-v2.txt --idempotency-token article-a-v2 --dry-run
python3 executors/contribute/run.py --type decide-revision --revision-id 108 \
  --decision accepted --idempotency-token review-108 --dry-run
```

The candidate is complete and immutable. Acceptance is transactional and
requires the exact accepted base. Pending candidates never replace the head;
competing stale approvals fail. Use `--type mark-canonical` only for an
accepted guide. Use `capture-message` when observed evidence needs a stable
message snapshot, and `evidence` for immutable `reported` or `observed` claims
with exact subjects and sources.

## Editorial rules

- Extend when the method is the same and the new candidate corrects or improves it.
- Create a new resource for a materially different method, artifact, or question.
- Split mixed independent methods so each resource has a coherent head.
- Consolidate only when every source and exact revision/evidence pin remains recoverable.
- State conditions, competing methods, and uncertainty explicitly.
- Canonical-guide marking is editorial navigation, never proof of truth.

Article, ComfyUI workflow, and YouTube ingestors emit `submit_resource` while
preserving extraction, payloads, source provenance, and dry-run behavior.
`refresh_media` remains unchanged. Media retention is deferred.
