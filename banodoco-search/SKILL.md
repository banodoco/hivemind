---
name: banodoco-search
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

# banodoco-search

A read-only PostgREST endpoint exposes the Banodoco Discord message feed.
This is community knowledge about video/image generation that you can't get
from official docs — workflow tips, model comparisons, settings tweaks,
gotchas, links to Kijai/Ablejones/community workflows.

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

## Channels worth knowing

Highest signal:
- `daily_summaries` — bot-curated daily roll-ups by **BNDC**. **Start here** for any topic — these are pre-distilled summaries with attributions and Discord links.
- `wan_chatter`, `wan_comfyui`, `wan_gens`, `wan_resources` — Wan / Wan Animate
- `ltx_chatter`, `ltx_resources`, `ltx_gens`, `ltx_training` — LTX 2.x
- `comfyui` — generic ComfyUI tips
- `resources` — pinned community resources
- `vibecoding` — tooling/dev chatter
- `training_control_loras` — LoRA training know-how

Lower signal: `chatter`, `nsfw` (yes there's tech advice in there but be selective), `hunyuanvideo`, `qwen-image`, `chroma`, `flux`, `z-image`, `magi`, `ace-step`, `kandinsky-5`, `seedance`, `hunyuanvideo`, `top_gens`, `art_sharing`.

## Power users to watch

These names come up repeatedly with authoritative answers — weight their messages higher:
- **Kijai** — author of the WanVideoWrapper / most ComfyUI nodes for Wan/LTX. Treat as canonical.
- **Ablejones** — context windows, color matching, native Comfy integrations, PRs to ComfyUI core.
- **djbfilmz** — heavy Wan Animate user, mocap / reskinning experiments.
- **42hub** — curates the [wanx-troopers.github.io](https://wanx-troopers.github.io/) knowledge base.
- **BNDC** — the daily-summary bot.

## Query patterns (PostgREST)

Always URL-encode spaces (`%20`). Use `Accept: application/json` implicitly.

### 1. Basic substring search

```bash
curl -s "https://ujlwuvkrxlvoswwkerdf.supabase.co/rest/v1/message_feed\
?select=content,author_name,channel_name,created_at\
&content=ilike.*wan%20animate*\
&order=created_at.desc&limit=30" \
  -H "apikey: sb_publishable_O38oPBafrBoFrpi_rlWJvA_UJrulFsx"
```

`ilike.*foo*` is case-insensitive substring. Wrap in `*…*` — the leading/trailing
asterisks are SQL `%` wildcards.

### 2. Restrict to high-signal channels

```bash
&channel_name=in.(daily_summaries,wan_comfyui,wan_chatter,wan_gens,resources,wan_resources)
```

### 3. Compose AND across multiple content terms

Repeat the `content=ilike.*X*` filter — PostgREST ANDs duplicate keys:

```
&content=ilike.*wan%20animate*&content=ilike.*workflow*
```

### 4. OR across spelling variants (compound terms!)

`Wan Animate` is spelled with and without the space (`WanAnimate`). Use `or=`:

```
&or=(content.ilike.*wan%20animate*,content.ilike.*wananimate*)
```

Note the **dot syntax inside `or=`** (`content.ilike.*…*`), not `=` — different from
top-level filters. Don't forget to URL-encode the parens if your shell needs it.

### 5. Filter by author

```
&author_name=eq.Kijai
```

For a topic-by-author query (e.g. "everything Kijai said about animate"):

```
&author_name=eq.Kijai&content=ilike.*animate*
```

### 6. Time bounds

```
&created_at=gte.2026-04-01&created_at=lte.2026-05-01
```

Default to `&order=created_at.desc&limit=30` so the newest messages bubble up first.

### 7. Headers for total count (rare)

```
-H "Prefer: count=exact"
```

then `Content-Range` header has `start-end/total`.

## What does NOT work

- **`fts` / full-text search** (`content=fts.foo`) — **times out** on this table.
  Stick with `ilike`.
- `reactions` is mostly `null` — you can't rank by popularity.
- No pagination cursor — use `offset=N` with `limit` if you need to page back.
- Messages are imported in batches; very recent (last few minutes) messages may
  not be there yet.

## Recipe: "best practices for X"

1. Search `daily_summaries` first — it has pre-distilled lessons and Discord
   permalinks back to the source thread:
   ```
   ?channel_name=eq.daily_summaries&or=(content.ilike.*X*,content.ilike.*Xalt*)
   &order=created_at.desc&limit=30
   ```
2. Then sweep author=Kijai (and Ablejones / djbfilmz when relevant) for
   first-party guidance:
   ```
   ?author_name=eq.Kijai&content=ilike.*X*&order=created_at.desc&limit=30
   ```
3. Finally widen to topic channels for specific settings/troubleshooting:
   ```
   ?channel_name=in.(wan_comfyui,wan_chatter,comfyui,wan_resources)
   &content=ilike.*X*&content=ilike.*setting*&order=created_at.desc&limit=30
   ```
4. **Always cross-check with [wanx-troopers.github.io](https://wanx-troopers.github.io/)**
   — that's 42hub's curated wiki and tends to be the most up-to-date community
   resource (especially `/wan-animates.html`, `/sigmas.html`,
   `/loras/part-01.html`, `/control.html`).

## General tips

- **Surface workflow links wherever possible.** When summarising a tip,
  practical material trumps abstract advice. Look hard for URLs in the
  matched messages — Hugging Face, Civitai, ComfyWorkflows, Discord
  attachments, YouTube, GitHub PRs (especially Comfy-Org/ComfyUI PRs and
  Kijai's wrappers). If a user asks "how do I do X", a one-line "use the
  Sapiens2 pose model" is half an answer; a "use Sapiens2 — here's the
  workflow JSON: <link>" is a complete one. Always include the link if
  you can find one. If you can't find one, say so plainly — never invent
  a URL.
- **Quote attributions.** Banodoco messages are public-but-attributed; if you
  surface a tip to the user, name the author and link via the Discord URL when
  one is in the message. Daily summaries always include a permalink.
- **Filter out the noise.** `len(content) > 40` skips one-word reactions. Most
  of the value is in messages that read like a paragraph.
- **Recency matters.** This space moves fast — a tip from 6 months ago may be
  obsolete (different model version, different node names). Lean on the most
  recent 2–3 months unless searching for a historical decision.
- **Spelling drift.** Models get casual names: "wan animate", "wananimate",
  "WAN Animate", "WAN-Animate". Include both major variants in `or=`.
- **Context windows / long video** is a recurring theme — if a question is
  about "endless" or "long" video, search for `context%20window` and `looping`.
- **Don't read the whole feed.** It's huge (tens of thousands of messages);
  always use channel + content filters, never raw `limit=1000` browsing.

## Quick smoke-test

```bash
curl -s "https://ujlwuvkrxlvoswwkerdf.supabase.co/rest/v1/message_feed\
?select=content,author_name,channel_name,created_at&limit=3" \
  -H "apikey: sb_publishable_O38oPBafrBoFrpi_rlWJvA_UJrulFsx" \
  | python3 -m json.tool
```

If that returns 3 rows, the endpoint is healthy.
