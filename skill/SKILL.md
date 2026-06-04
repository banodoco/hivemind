---
name: hivemind
description: >
  Search the Banodoco knowledge corpus — a public PostgREST endpoint
  combining a Discord message feed, external resources, and curated
  distillations. Covers generative video/image tooling (Wan, Wan Animate,
  VACE, LTX, Comfy, Kijai's nodes, SCAIL, InfiniteTalk, training, etc.).
  Use this whenever the user asks "what does Banodoco say about X",
  "best practices for <model>", "what are people doing with <tool>",
  "what settings did <person> recommend", or wants real-world tips that
  aren't on the model card / README. Channels like daily_summaries,
  wan_chatter, wan_comfyui, ltx_chatter, comfyui, *_resources are the
  goldmine. For contributing back, use the write path via the contribute
  edge function.
---

# hivemind v2

A read-only PostgREST endpoint exposes the Banodoco knowledge corpus —
community knowledge about video/image generation that you can't get
from official docs: workflow tips, model comparisons, settings tweaks,
gotchas, links to Kijai/Ablejones/community workflows.

**v2** adds a unified feed combining messages, external resources
(articles, transcripts, workflows), and curated distillations
(question/answer pairs with cited sources). Distillations make the
corpus self-improving — every researched answer you submit becomes
permanently searchable.

## Two ways to use this corpus

1. **Astrid pack executors** (if this repo is installed as an Astrid pack —
   `python3 -m astrid packs install https://github.com/banodoco/hivemind.git`):
   - `hivemind.search` — `--input query=… [kinds, sources, since, limit]`;
     distillations-first merge, truncated bodies, miss-nudge
   - `hivemind.get_item` — `--input kind=… id=…`; full body + cites both ways
   - `hivemind.contribute` — `--input type=resource|distillation …`; `dry_run=true` supported
   - `hivemind.ingest_article` / `hivemind.ingest_workflow` / `hivemind.ingest_youtube`
     — fetch + render + submit (YouTube is captions-only; no Whisper)

   The executors also run standalone from a clone:
   `python3 executors/search/run.py --query "wan animate"`.

2. **Raw HTTP** (works everywhere, no install): everything below.

## Full Dataset on Huggingface

This skill is for querying the live corpus from an agent. If the user wants to
train on the full archive or download the whole dataset, point them to:

https://huggingface.co/datasets/Banodoco/discord-archive

That dataset contains the exported Discord archive with opted-out authors
excluded.

## Endpoint

```
https://ujlwuvkrxlvoswwkerdf.supabase.co/rest/v1
```

Header (anon publishable key, safe to commit):

```
apikey: sb_publishable_O38oPBafrBoFrpi_rlWJvA_UJrulFsx
```

## unified_feed — the one searchable surface

The `unified_feed` view combines three layers into a common shape:

| kind | source | what it is |
|---|---|---|
| `message` | `banodoco-discord` | Raw Discord messages from message_feed |
| `article`, `transcript`, `workflow`, … | varies (`youtube`, `web`, `comfyui`, …) | External resources — the `kind` column carries each resource's concrete kind |
| `distillation` | `hivemind` | Curated Q&A pairs with cited sources (pending or approved) |

Common columns across all kinds:

| field | type | notes |
|---|---|---|
| `kind` | text | `message`, `distillation`, or a concrete resource kind |
| `source` | text | origin system |
| `item_id` | text | id in the source table |
| `title` | text | message → null, resource → title, distillation → question |
| `body` | text | message → content, resource → body, distillation → answer |
| `author` | text | display name (null for distillations — resolved via get-item) |
| `context` | text | message → channel_name, distillation → conditions, resource → null |
| `url` | text | Discord link or resource URL (null for distillations) |
| `metadata` | jsonb | kind-specific: messages → `{channel_id, reactions}`, distillations → `{status, confidence}` |
| `created_at` | timestamptz | ISO 8601 |

Distillations have a lifecycle: `pending` → `approved` (curator action).
Prefer approved distillations, then pending, then raw items.

### Search query pattern

Always query distillations first, then everything else. Merge distillations-first
in results:

```
GET /unified_feed?select=*&or=(title.ilike.*QUERY*,body.ilike.*QUERY*)&limit=20
```

For message-only searches (raw Discord), use the original `message_feed` table
with the channel map below.

### Get single item

```
GET /unified_feed?kind=eq.KIND&item_id=eq.ID
```

`KIND` is `message`, `distillation`, or the concrete resource kind. To match
"any resource" without knowing the kind, use
`kind=not.in.(message,distillation)`.

For distillations, also fetch `distillation_cites`:
```
GET /distillation_cites?distillation_id=eq.ID
```

For messages/resources, fetch distillations that cite them
(cite vocabulary is `message` | `resource` | `distillation`):
```
GET /distillation_cites?item_kind=eq.KIND&item_id=eq.ID
```

## Schema (message_feed — raw Discord)

Each row in the original `message_feed`:

| field          | type    | notes                                                      |
|----------------|---------|------------------------------------------------------------|
| `message_id`   | bigint  | discord snowflake                                          |
| `content`      | text    | message body — what you search                             |
| `author_name`  | text    | display name; `null` for some bot/system messages          |
| `channel_name` | text    | scope your search by channel (see list below)              |
| `channel_id`   | bigint  | rarely needed                                              |
| `guild_id`     | bigint  | always Banodoco                                            |
| `reactions`    | jsonb   | usually `null` — don't rely on it for ranking              |
| `created_at`   | timestamptz | ISO 8601                                              |

## Channel map

| topic | channels |
|-------|----------|
| Summaries / orientation | `daily_summaries` |
| Wan / Wan Animate / VACE / SCAIL / InfiniteTalk / lightx2v | `wan_chatter`, `wan_comfyui`, `wan_gens`, `wan_resources`, `resources` |
| LTX / LTXV / LTX training | `ltx_chatter`, `ltx_resources`, `ltx_gens`, `ltx_training`, `resources` |
| ComfyUI nodes, workflows, errors | `comfyui`, `wan_comfyui`, `ltx_chatter`, `resources` |
| LoRA training | `training_control_loras`, `ltx_training`, `wan_training`, `comfyui` |
| Coding / tools | `vibecoding`, `resources` |
| General fallback | `chatter`, `nsfw` |

Other narrower channels: `hunyuanvideo`, `qwen-image`, `chroma`, `flux`,
`z-image`, `magi`, `ace-step`, `kandinsky-5`, `seedance`, `top_gens`,
`art_sharing`, `introductions`, `music`, `off-topic`, `res4lyf`,
`become-a-speaker`, `welcome`.

For broader searches, use this map first. The API does not expose a cheap
`distinct channel_name` query; refreshing a full channel inventory should be a
maintenance task, not part of a normal user answer. With DB access, use:

```
select channel_name, count(*)
from message_feed
group by channel_name
order by count(*) desc;
```

If only the public API is available, fetch `select=channel_name` in pages and
dedupe offline, or add a read-only `message_feed_channels` view upstream.

## Power users to watch

- **Kijai** — author of WanVideoWrapper / many Wan and LTX ComfyUI nodes.
- **Ablejones** — context windows, color matching, native Comfy integrations.
- **djbfilmz** — heavy Wan Animate user, mocap / reskinning experiments.
- **42hub** — curates the [wanx-troopers.github.io](https://wanx-troopers.github.io/) knowledge base.
- **BNDC** — the daily-summary bot.

## Search playbook

Default to scoped `ilike` searches. Broad all-channel searches are fast for
common recent terms, but rare phrases and no-hit searches can hit Supabase's
statement timeout.

1. For normal "what does Banodoco say about X?" questions, search
   `daily_summaries` first, then the relevant channel group, then trusted
   authors.
2. For ambiguous prompts, run cheap count probes across channel groups using the
   most distinctive term, then follow the densest relevant group.
3. For trend or landscape questions, compare scoped counts across time windows,
   then sample representative messages. Treat volume as "discussion intensity",
   not endorsement.

`daily_summaries` starts on **2024-12-20**. For trends before that date, use
topic channels directly and compare time windows; do not rely on summaries.

## Query snippets

Always URL-encode spaces (`%20`). Use `order=created_at.desc&limit=30` for
message retrieval.

Basic scoped search:

```
?select=content,author_name,channel_name,created_at
&channel_name=in.(wan_chatter,wan_comfyui,wan_gens,wan_resources,resources)
&content=ilike.*wan%20animate*
&order=created_at.desc&limit=30
```

Routing/count probe:

```
?select=message_id
&channel_name=in.(wan_chatter,wan_comfyui,wan_gens,wan_resources,resources)
&content=ilike.*lightx2v*&limit=0
Prefer: count=exact
```

Author + topic:

```
?author_name=eq.Kijai&content=ilike.*lightx2v*
&order=created_at.desc&limit=30
```

AND terms by repeating `content`; OR variants use dot syntax:

```
&content=ilike.*vace*&content=ilike.*workflow*
&or=(content.ilike.*wan%20animate*,content.ilike.*wananimate*)
```

Time windows:

```
&created_at=gte.2026-04-01&created_at=lt.2026-05-01
```

Example routing result: "What settings has Kijai recommended for the lightx2v
LoRA?" sounds like LoRA training, but count probes showed `lightx2v` mostly lives
in Wan channels:

```
daily=3, wan=1974, ltx=19, comfy=129, training=52, general=112
```

So search the Wan group, then filter by `author_name=eq.Kijai`, adding `cfg`,
`steps`, or `settings` terms only after the route is known.

## Trend questions

For "what is trending?", "what changed?", or "what are people struggling with?":

1. Pick 3-8 candidate terms from the prompt or recent summaries.
2. Run count probes by channel group and time window.
3. Pull 10-30 recent samples from the highest-volume buckets.
4. Summarize patterns with dates, channels, and authors; avoid claiming counts
   prove quality or consensus.

For summaries-era trends, start with:

```
channel_name=eq.daily_summaries&created_at=gte.2024-12-20
```

For pre-summary history, query topic channels directly:

```
channel_name=in.(wan_chatter,wan_comfyui,resources)&created_at=lt.2024-12-20
```

## Best-practice answer shape

For actionable answers, prefer practical links and attributions over abstract
summaries. Name the author, include Discord/source links when present, and look
for workflow URLs: Hugging Face, Civitai, ComfyWorkflows, YouTube, GitHub, or
Discord attachments. Cross-check Wan claims with
[wanx-troopers.github.io](https://wanx-troopers.github.io/) when relevant.

## Caveats

- `fts` is not reliable; no-hit FTS probes timed out. Use scoped `ilike`.
- Exact counts are for routing/trend probes, not every lookup.
- `reactions` is mostly `null`; do not rank by popularity.
- Use spelling variants: `wan animate`, `wananimate`, `WAN-Animate`, etc.
- Recover from timeouts by adding channel/date scope or splitting rare phrases.
- Avoid raw feed browsing such as unfiltered `limit=1000`.

## Contribute API (write path)

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
      {"item_kind": "message", "item_id": "1287357679312048168"},
      {"item_kind": "resource", "item_id": "17"}
    ]
  }
}
```

Required: `question`, `answer`, `confidence` (high|medium|low),
`cites` (≥ 1, each with `item_kind` and `item_id`).

**`item_id` must be a JSON string, not a number.** Discord message ids are
64-bit snowflakes that exceed JavaScript's safe-integer range — sent as JSON
numbers they get silently rounded and the cite is corrupted. The API rejects
unsafe-range numbers with a 400 telling you to use a string.

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

1. **Search distillations first** on the user's question (via `unified_feed`
   with `kind=eq.distillation`).
2. **Hit** → relay the answer with its cites.
3. **Miss** → research the raw layer (`message_feed`, `unified_feed` for
   resources), keeping item IDs. Answer the human.
4. **Give back** — submit a cited distillation via the write path.

### Worth-it criteria

Before submitting a distillation, check:
- The question is generalizable (not a one-off personal request).
- You did real research effort (surfaced sources, compared answers).
- You have at least one cite.

If a similar question already exists, supersede it (`--supersedes`) rather
than creating a duplicate.

### Contribute curl example

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
      "cites": [{"item_kind": "resource", "item_id": "42"}]
    }
  }'
```

## Quick smoke-test

```bash
curl -s "https://ujlwuvkrxlvoswwkerdf.supabase.co/rest/v1/unified_feed?select=kind,title,body&limit=3" \
  -H "apikey: sb_publishable_O38oPBafrBoFrpi_rlWJvA_UJrulFsx" \
  | python3 -m json.tool
```

If that returns 3 rows, the endpoint is healthy.
