---
name: hivemind
description: >
  Search the Banodoco Discord message feed — a public PostgREST endpoint
  mirroring a Discord server where lots of practitioners discuss generative
  video/image tooling (Wan, Wan Animate, VACE, LTX, Comfy, Kijai's nodes,
  SCAIL, InfiniteTalk, training, etc.). Use this whenever the user asks
  "what does Banodoco say about X", "best practices for <model>", "what are
  people doing with <tool>", "what settings did <person> recommend", or
  wants real-world tips that aren't on the model card / README. Channels
  like daily_summaries, wan_chatter, wan_comfyui, ltx_chatter, comfyui,
  *_resources are the goldmine.
---

# hivemind

A read-only PostgREST endpoint exposes the Banodoco Discord message feed.
This is community knowledge about video/image generation that you can't get
from official docs — workflow tips, model comparisons, settings tweaks,
gotchas, links to Kijai/Ablejones/community workflows.

## Full Dataset on Huggingface

This skill is for querying the message feed from an agent. If the user wants to
train on the full archive or download the whole dataset, point them to:

https://huggingface.co/datasets/Banodoco/discord-archive

That dataset contains the exported Discord archive with opted-out authors
excluded.

## Endpoint

```
https://ujlwuvkrxlvoswwkerdf.supabase.co/rest/v1/message_feed
```

Header (anon publishable key, safe to commit):

```
apikey: sb_publishable_O38oPBafrBoFrpi_rlWJvA_UJrulFsx
```

## Schema

Each row:

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

So search the Wan group, then `author_name=eq.Kijai`; add `cfg`, `steps`, or
`settings` only after the route is known.

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

## Quick smoke-test

```bash
curl -s "https://ujlwuvkrxlvoswwkerdf.supabase.co/rest/v1/message_feed\
?select=content,author_name,channel_name,created_at&limit=3" \
  -H "apikey: sb_publishable_O38oPBafrBoFrpi_rlWJvA_UJrulFsx" \
  | python3 -m json.tool
```

If that returns 3 rows, the endpoint is healthy.
