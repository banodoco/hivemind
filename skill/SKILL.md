# Hivemind Skill

## Overview

Hivemind is a shared, searchable knowledge corpus.  You read it through a
public PostgREST API (`unified_feed`) and write to it through a single
locked edge function (`contribute`).  This skill teaches Claude Code how
to use both paths.

## Read path (public PostgREST)

Base URL: `https://ujlwuvkrxlvoswwkerdf.supabase.co/rest/v1`
Anon key: `sb_publishable_O38oPBafrBoFrpi_rlWJvA_UJrulFsx`

The `unified_feed` view combines messages, external resources, and
distillations into one searchable table with these item kinds:

- `message` — raw Discord messages from the message_feed.
- `resource` — articles, transcripts, workflows submitted via contribute.
- `distillation` — curated Q&A pairs with cited sources.

Distillations have a lifecycle: `pending` → `approved` (curator action).
Search should prefer approved distillations, then pending, then raw items.

### Search query pattern

```
GET /unified_feed?select=*&or=(title.ilike.*QUERY*,body.ilike.*QUERY*)&limit=20
```

Always query distillations first, then the rest; merge distillations-first.

### Get single item

```
GET /unified_feed?item_kind=eq.KIND&item_id=eq.ID
```

For distillations, also fetch `distillation_cites`:
```
GET /distillation_cites?distillation_id=eq.ID
```

For messages/resources, fetch distillations that cite them:
```
GET /distillation_cites?item_kind=eq.KIND&item_id=eq.ID
```

## Write path (edge function)

**Endpoint:** `POST {SUPABASE_URL}/functions/v1/contribute`
**Auth header:** `X-Contributor-Key: hm_<64 hex>`
**Content-Type:** `application/json`

### Add resource

```json
{
  "action": "add_resource",
  "data": {
    "kind": "article",
    "source": "web",
    "title": "Title here",
    "body": "Body text here …",
    "url": "https://…",
    "author": "…"
  }
}
```

### Submit distillation

```json
{
  "action": "submit_distillation",
  "data": {
    "question": "What is …?",
    "answer": "It is …",
    "confidence": "high",
    "cites": [
      {"item_kind": "message", "item_id": 88123},
      {"item_kind": "resource", "item_id": 17}
    ]
  }
}
```

Required: `question`, `answer`, `confidence` (high|medium|low),
`cites` (≥ 1, each with `item_kind` and `item_id`).

Optional: `supersedes_id` (must reference an existing distillation),
`conditions`.

Status is always forced to `pending` by the edge function — ignore any
client-supplied value.

### Responses

- `201 {"id": N, "status": "ok"}` — success.
- `400 {"error":"validation","detail":"…"}` — bad request.
- `401 {"error":"unauthorized"}` — bad or revoked key.
- `409 {"error":"duplicate","existing_id":N,"detail":"similar question exists — extend or supersede it"}`.

## Flywheel loop (the full procedure)

1. **Search distillations first** on the user's question.
2. **Hit** → relay the answer with its cites.
3. **Miss** → research the raw layer (`message_feed`, `external_resources`),
   keeping item ids.  Answer the human.
4. **Give back** — submit a cited distillation via the write path.

### Worth-it criteria

Before submitting a distillation, check:
- The question is generalizable (not a one-off personal request).
- You did real research effort (surfaced sources, compared answers).
- You have at least one cite.

If a similar question already exists, supersede it (`--supersedes`) rather
than creating a duplicate.

## Contribute curl example

```bash
curl -s -X POST "$SUPABASE_URL/functions/v1/contribute" \
  -H "Content-Type: application/json" \
  -H "X-Contributor-Key: hm_$(cat ~/.hivemind/key)" \
  -d '{
    "action": "submit_distillation",
    "data": {
      "question": "How do I …?",
      "answer": "You …",
      "confidence": "high",
      "cites": [{"item_kind": "resource", "item_id": 42}]
    }
  }'
```
