---
name: hivemind
description: >
  Search the Banodoco knowledge corpus — a public PostgREST endpoint
  combining a Discord message feed, external resources, and curated
  distillations. Covers generative video/image tooling (Wan, Wan Animate,
  VACE, LTX, Comfy, Kijai's nodes, SCAIL, InfiniteTalk, training, etc.).
  Use this whenever the user asks "what does Banodoco say about X",
  "best practices for a model", "what are people doing with a tool",
  "what settings did a named person recommend", or wants real-world tips that
  aren't on the model card / README. Channels like daily_summaries,
  wan_chatter, wan_comfyui, ltx_chatter, comfyui, *_resources are the
  goldmine. For contributing back, use the write path via the contribute
  edge function.
---

# hivemind

A read-only public endpoint exposing the Banodoco knowledge corpus —
community knowledge about video/image generation you can't get from official
docs: workflow tips, model comparisons, settings tweaks, gotchas, and links
to Kijai/Ablejones/community workflows. It layers three things into one
searchable feed: **messages** (raw Discord), **resources** (articles,
transcripts, workflows), and **distillations** (curated Q&A with cited
sources). Distillations make the corpus self-improving — every researched
answer you submit becomes permanently searchable.

## Quick start

```
Endpoint: https://ujlwuvkrxlvoswwkerdf.supabase.co/rest/v1
Key (anon publishable, safe to commit):
  apikey: sb_publishable_O38oPBafrBoFrpi_rlWJvA_UJrulFsx
```

Smoke test (raw search surface — per-token OR on message content; do NOT
text-search `unified_feed`, the UNION scan times out with 57014):

```bash
curl -s "https://ujlwuvkrxlvoswwkerdf.supabase.co/rest/v1/message_feed?select=message_id,content,author_name,channel_name,created_at&limit=5&order=created_at.desc&or=(content.ilike.*wan*,content.ilike.*animate*)" \
  -H "apikey: sb_publishable_O38oPBafrBoFrpi_rlWJvA_UJrulFsx"
```

### Surfaces at a glance — pick by what you want

| You want to… | Use | Why |
|---|---|---|
| Search curated Q&A (distillations) | `distillations` + `or=(question.ilike.*T*,answer.ilike.*T*,conditions.ilike.*T*)` | flywheel, highest signal; status `pending,approved` |
| Search resources (workflows, articles) | `external_resources?kind=eq.<resource-kind>&or=(title.ilike.*T*)` | raw table, kind btree + trigram GIN |
| **Search message content** (free text) | `message_feed` + `or=(content.ilike.*T1*,content.ilike.*T2*)` | per-token OR is index-friendly; `unified_feed` ilike **times out** (57014) |
| Just search everything at once | `python3 executors/search/run.py --query "wan animate workflow"` | the pack executor: 3 scopes in parallel, per-token predicates, client-ranked merge |
| Search one Discord channel | `python3 executors/search/run.py --query "lora" --channel wan_chatter` | channel-scoped message search |
| Search one person's messages | `python3 executors/search/run.py --query "lora" --author Kijai` | author-scoped message search |
| Search inside a thread | `python3 executors/search/run.py --query "context" --thread <snowflake>` | index-backed `message_filters` thread surface |
| Page through results | `--limit 10 --offset 10` (response has `total`/`has_more`) | deterministic ranked pool; stable pages |
| Filter messages by a field (pinned, thread, reply, attachment, channel) | `message_filters` | index-backed, ~0.1–0.25s |
| Fetch a full row by id | `get_item` (executor) or `unified_feed?item_id=eq.<id>` | complete body + metadata + cites |
| Refresh an expiring media URL | `refresh-media-urls` edge function | fresh Discord CDN URL |

More on each below. The golden rule: **match the surface to the question**
(gotchas in the next section).

## The surfaces

### unified_feed — the results feed (distillations + resources; NOT text search)

`unified_feed` is a UNION of three layers with one common shape. Use it
**kind-scoped** for single-row fetches (`get_item`) of distillations and
resources; do NOT use it for text search of any kind (a per-token OR over the
UNION scan times out with 57014). For text search use the raw tables
(`message_feed` / `external_resources` / `distillations`) or the pack's
`hivemind.search` executor, which does exactly that.

| kind | source | what it is |
|---|---|---|
| `message` | `banodoco-discord` | Raw Discord messages (use `message_feed`/`message_filters` to query) |
| `article`, `transcript`, `workflow`, … | varies | External resources — `kind` carries the concrete kind |
| `distillation` | `hivemind` | Curated Q&A pairs with cited sources (pending or approved) |

Common columns: `kind, source, item_id, title, body, author, context, url,
metadata, created_at`. Distillations have a lifecycle `pending → approved`;
prefer approved, then pending, then raw items.

### Message metadata (the Discord envelope)

`unified_feed` message rows carry the full Discord envelope in `metadata`:

| key | type | notes |
|---|---|---|
| `channel_id` | number | legacy numeric (pre-dates the stringification rule) |
| `guild_id` | string | Discord ids are **strings** (snowflakes exceed 2⁵³) |
| `reactions` | json | **active** reactions only (removed filtered) — sparse (~7%) |
| `author_id` | string | author's `members.member_id` |
| `avatar_url` | text | author's avatar, when known (~75% of authors) |
| `reference_id` | string | the message this one **replies to** (Discord reply/cite) |
| `thread_id` | string | thread/forum id the message belongs to |
| `message_type` | text | e.g. `DEFAULT`, `reply` |
| `edited_at` | timestamptz | last edit |
| `is_pinned` | bool | pinned/curated |
| `reaction_count` | int | **historical total** incl. removed — the reliable popularity signal |
| `embeds` | jsonb | Discord link previews (title/description/source URL) |
| `attachments` | jsonb | **the resource signal** — non-empty = contains a file/media |
| `channel_type` | text | `text`, `forum`, `news`, `thread`, … (now ~100% populated) |

Scope + semantics:
- These envelope keys appear **only on `kind=message` rows**. Resource and
  distillation rows carry their own `metadata` (tags/status) — they do NOT
  have these keys.
- Deleted messages are filtered out of **every** surface (view, `message_feed`,
  search, and direct `discord_messages` reads via RLS) — they never appear.

### The two resource signals (attachments + citations)

**Attachments → skip the noise, find the resources.** A message that actually
contains/shows something (image, video, workflow `.json`, PDF) has a
**non-empty** `attachments` array; prose-only messages have `[]`. ~188k
messages carry attachments. Each element:

```json
{ "filename": "image.png", "url": "https://cdn.discordapp.com/attachments/..." }
```

`filename` is **always** present — infer the type from its extension
(`image/*`, `video/*`, `.json`, `.pdf`). `content_type` (MIME) is present on
only ~3–5% of attachments, so treat it as a bonus, not a guarantee. Use
`refresh-media-urls` for a fresh CDN URL before using one.

**`reference_id` → build chains.** It is the snowflake of the message this one
replies to. Walk it both directions to reconstruct threads of thoughts:
read a reply's `reference_id`, `get_item` the parent, repeat. A message with a
`reference_id` is a response/correction/elaboration; without one it's a root.

### message_feed — raw message content search (the fast text surface)

Each row: `message_id, content, author_name, channel_name, channel_id,
guild_id, reactions, created_at` (deleted already filtered). This is the
surface for **message content `ilike`** — it's index-backed and fast, where
`unified_feed` ilike is not. Scope by channel for speed.

### message_filters — structured filtering (schema/036)

A simple, index-backed view over `discord_messages` with the hot filter
columns exposed top-level (not buried in jsonb metadata), so PostgREST
filters push to indexes:

```
GET /rest/v1/message_filters?is_pinned=eq.true&limit=20            # pinned canon
GET /rest/v1/message_filters?channel_name=eq.minimax_h3_resources  # by channel (note: channel_name is NOT unique — use channel_id for exactly one)
GET /rest/v1/message_filters?thread_id=eq.<tid>                    # whole thread
GET /rest/v1/message_filters?reference_id=eq.<id>                  # replies TO a message (chains)
GET /rest/v1/message_filters?attachments=cs.[{"content_type":"video/mp4"}]  # has typed attachment (GIN)
GET /rest/v1/message_filters?attachments=cs.[{"filename":"x.png"}]          # exact file
```

Columns: `message_id, channel_id, guild_id, author_id, thread_id,
reference_id, is_pinned, reaction_count, attachments, embeds, content,
created_at, channel_name`. All filters index-backed (is_pinned ~0.1s,
reference_id ~0.1s, attachments containment ~0.1–0.6s). Attachments uses
PostgREST's `cs` operator (jsonb `@>`). Note: `attachments=cs.[{"filename":"wan"}]`
substring does NOT work — it's exact match; and filtering by `content_type`
only finds attachments the archive typed (~5% of videos, ~3% of images).

### get_item — full rows

```
python3 executors/get_item/run.py --kind message|resource|distillation --id <id>
# or: unified_feed?item_id=eq.<id>
```

Returns the full row + metadata + (for distillations) cites / cited-by.

## How to answer a question (the workflow)

1. **Distillations first** — `distillations?status=in.(pending,approved)&or=(question.ilike.*T1*,answer.ilike.*T1*,question.ilike.*T2*,answer.ilike.*T2*)` (per-token, not one phrase — a phrase never occurs verbatim and a multi-token OR over `unified_feed` times out). Hit → relay the answer + its cites.
2. **Then the pinned canon** — `message_filters?is_pinned=eq.true` (~72 pins, mostly in `wan_resources`/`ltx_resources`; the community's highest-signal artifacts).
3. **Then channel-scoped message search** — `message_feed?channel_name=in.(…)&content=ilike.*term*`, starting from the channel map.
4. **Verify + deepen** — `get_item` promising ids; walk reply chains (`message_filters?reference_id=eq.<id>`); pull a whole thread (`thread_id=eq.<tid>`).
5. **Assemble a cited answer** — name authors, include Discord/permalink + source links, prefer concrete settings over abstractions.

**Paging a big result set** — the search response carries `has_more`,
`page`/`pages`, and `next_offset`. When `has_more` is true, fetch the next
page by re-running the same query with `--offset <next_offset>` — the next
offset is given, so do not compute it yourself. `total` is the ranked pool
(~100–300 rows), not the full corpus match count; to dig further than the
pool, narrow with `--channel`/`--author`/`--since`/`--thread` instead of
deep offsets.

**Ordering** — default `--sort relevance`: score (distinctive tokens in
title weigh more than body; approved distillations and parseable workflows
float), then recency as tiebreak. Use `--sort recent` for newest-first.

**Let channel flavor guide you** — `wan_chatter` = chat/experience,
`wan_comfyui` = technical/errors, `*_gens`/`*_resources` = showcases/files,
`training_*` = training, `updates` = announcements/essays, `daily_summaries` =
orientation (starts 2024-12-20; before that use topic channels).

## Gotchas (the traps)

- **`unified_feed` text search times out** (HTTP 500 `code 57014`) for
  message-content `ilike` AND for multi-word / per-token ORs over the UNION
  (the derived-view scan exceeds the anon role's 3s statement budget).
  `57014` = statement timeout — **NOT bad data**. The pack search executor
  never touches `unified_feed`: it queries `message_feed`,
  `external_resources`, and `distillations` directly with per-token
  predicates and ranks client-side. Use that executor, or the raw tables, or
  `message_filters`.
- **Do NOT filter on `unified_feed.metadata` jsonb paths** — same timeout. Read
  the fields from result rows instead, or filter the source columns.
- **Rank with `reaction_count`, not `reactions`** — `reactions` is active-only
  (~7% of messages); `reaction_count` is the historical total (~16%) and the
  reliable popularity signal.
- **Snowflakes are strings** at the JSON boundary (`author_id`,
  `reference_id`, `thread_id`, `guild_id`). `item_id` and cite `item_id` too —
  never send them as JSON numbers.
- **`content_type` on attachments is sparse (~3–5%)** — infer type from the
  `filename` extension; `cs` filters by `content_type` only find typed ones.
- **`channel_name` is not unique** — multiple channels can share a name; use
  `channel_id` for exactly one.

## Reference

### Channel map

| topic | channels |
|---|---|
| Summaries / orientation | `daily_summaries` |
| Wan / Wan Animate / VACE / SCAIL / InfiniteTalk / lightx2v | `wan_chatter`, `wan_comfyui`, `wan_gens`, `wan_resources`, `resources` |
| LTX / LTXV / LTX training | `ltx_chatter`, `ltx_resources`, `ltx_gens`, `ltx_training`, `resources` |
| ComfyUI nodes, workflows, errors | `comfyui`, `wan_comfyui`, `ltx_chatter`, `resources` |
| LoRA training | `training_control_loras`, `ltx_training`, `wan_training`, `comfyui` |
| Coding / tools | `vibecoding`, `resources` |
| General fallback | `chatter`, `nsfw` |

Narrower: `hunyuanvideo`, `qwen-image`, `chroma`, `flux`, `z-image`, `magi`,
`ace-step`, `kandinsky-5`, `seedance`, `top_gens`, `art_sharing`,
`introductions`, `music`, `off-topic`, `res4lyf`, `become-a-speaker`, `welcome`.
(No cheap `distinct channel_name` API — a full inventory is a maintenance task.)

### Power users to watch

- **Kijai** — WanVideoWrapper / many Wan + LTX ComfyUI nodes (~115k messages, the corpus's dominant expert).
- **Ablejones** — context windows, color matching, native Comfy integrations; many pinned workflow revisions.
- **djbfilmz** — heavy Wan Animate user, mocap / reskinning experiments.
- **42hub** — curates [wanx-troopers.github.io](https://wanx-troopers.github.io/).
- **BNDC** — the daily-summary bot.

A handful of authors produce most durable knowledge — `author_id` +
`reaction_count` surfaces them.

### Query snippets

Always URL-encode spaces (`%20`); use `order=created_at.desc&limit=30`.

```
# scoped message search
?select=content,author_name,channel_name,created_at
&channel_name=in.(wan_chatter,wan_comfyui,wan_gens,wan_resources,resources)
&content=ilike.*wan%20animate*
&order=created_at.desc&limit=30

# routing/count probe
?select=message_id&channel_name=in.(wan_chatter,wan_comfyui,wan_gens,wan_resources,resources)
&content=ilike.*lightx2v*&limit=0  + Prefer: count=exact

# author + topic
?author_name=eq.Kijai&content=ilike.*lightx2v*&order=created_at.desc&limit=30

# AND terms by repeating content; OR via dot syntax
&content=ilike.*vace*&content=ilike.*workflow*
&or=(content.ilike.*wan%20animate*,content.ilike.*wananimate*)

# time window
&created_at=gte.2026-04-01&created_at=lt.2026-05-01
```

Routing example: "What settings has Kijai recommended for the lightx2v LoRA?"
sounds like LoRA training, but count probes showed `lightx2v` mostly lives in
Wan channels (`daily=3, wan=1974, ltx=19, comfy=129, training=52,
general=112`) — search the Wan group, then `author_name=eq.Kijai`, adding
`cfg`/`steps`/`settings` only after the route is known.

### Trend questions

For "what's trending / what changed / what are people struggling with?":
pick 3–8 candidate terms, run count probes by channel group + time window,
pull 10–30 recent samples from the densest buckets, summarize with
dates/channels/authors — and treat volume as "discussion intensity," not
endorsement. Summaries-era trends: `channel_name=eq.daily_summaries&created_at=gte.2024-12-20`. Pre-summary: topic channels with `created_at=lt.2024-12-20`.

### Best-practice answer shape

Prefer practical links + attributions over abstract summaries. Name the author,
include Discord/permalink + source links, look for workflow URLs (HF, Civitai,
ComfyWorkflows, YouTube, GitHub, Discord attachments). Cross-check Wan claims
against [wanx-troopers.github.io](https://wanx-troopers.github.io/).

## Refresh Discord media URLs

Discord CDN attachment URLs expire. Given a message id/permalink with no usable
media URL, refresh through the public edge function:

```bash
python3 executors/refresh_media/run.py --message-id 1512127379039060118
# or raw:
curl -s -X POST 'https://ujlwuvkrxlvoswwkerdf.supabase.co/functions/v1/refresh-media-urls' \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"message_id": "1512127379039060118"}'
```

`message_id` must be a JSON **string** (snowflakes exceed JS safe integers).
Response: `{success, message_id, attachments: [{filename, url, …}], urls_updated}`.

The function enforces throttling + a 24h freshness cache (2026-08-05):
- **24h cached delivery** — URL refreshed <24h ago AND the Discord `ex` expiry
  (a HEX unix-seconds param) is still >1h out → returns the SAME URL with
  `cached: true`, no Discord call.
- **Per-IP 10 real refreshes/min, global 60/min** → HTTP 429 + `Retry-After`.
  Fail-closed: throttle gate error → 503.
- Each real refresh = 1 Discord call (bot global 50/s; per-channel ~5/5s).
  Supabase edge-fn monthly quota: 500K free / 2M pro (billed per invocation
  even when `urls_updated: 0`). A full-corpus refresh (~188k messages) is
  ~37% free / 9% pro.
- Bulk/backfill: sequential, spread across channels, rely on the cache to skip
  fresh URLs, and budget the monthly quota first.

## Contribute API (write path)

**Endpoint:** `POST {SUPABASE_URL}/functions/v1/contribute`
**Auth header:** `X-Contributor-Key: hm_<64 hex>` · **Content-Type:** `application/json`

Add a resource:

```json
{ "action": "add_resource",
  "data": { "kind": "article", "source": "web", "title": "…", "body": "…", "url": "https://…", "author": "…" } }
```

Submit a distillation:

```json
{ "action": "submit_distillation",
  "data": { "question": "…", "answer": "…", "confidence": "high",
            "cites": [ {"item_kind": "message", "item_id": "1287357679312048168"},
                       {"item_kind": "resource", "item_id": "17"} ] } }
```

Required: `question`, `answer`, `confidence` (high|medium|low), `cites` (≥1).
**`item_id` must be a JSON string, not a number** (the API rejects unsafe-range
numbers with a 400). Optional: `supersedes_id`, `conditions`. Status is always
forced to `pending` by the edge function.

Responses: `201 {"id":N,"status":"ok"}` · `400 validation` · `401 unauthorized`
· `409 duplicate` (extend or supersede instead).

### Flywheel loop (the full procedure)

1. **Search distillations first** on the user's question.
2. **Hit** → relay the answer with its cites.
3. **Miss** → research the raw layer, keeping item IDs; answer the human.
4. **Give back** — submit a cited distillation (generalizable question, real
   research effort, ≥1 cite; supersede a similar existing one rather than
   duplicate).

## Astrid pack (optional)

If installed as an Astrid pack (`python3 -m astrid packs install
https://github.com/banodoco/hivemind.git`), use the executors:
`hivemind.search`, `hivemind.get_item`, `hivemind.refresh_media`,
`hivemind.contribute`, `hivemind.ingest_article|workflow|youtube` (YouTube is
captions-only). They also run standalone: `python3 executors/search/run.py
--query "wan animate"`.

Hivemind is Astrid's default shared knowledge pack — search it before
re-researching community practice; keep raw runs/conclusions locally; promote
only evidence-backed, generalizable learnings; treat contribution as public
publication (dry-run first, remove private content, get explicit user
confirmation). Do not copy this skill into agent-specific directories.

## Full dataset

For training or the whole archive, point users to
https://huggingface.co/datasets/Banodoco/discord-archive (opted-out authors
excluded).

## Caveats

- Use spelling variants (`wan animate`, `wananimate`, `WAN-Animate`).
- Recover from timeouts by adding channel/date scope or splitting rare phrases;
  avoid raw feed browsing (unfiltered `limit=1000`).
- `fts` is not reliable — use scoped `ilike`.
- Exact counts are for routing/trend probes, not every lookup.
