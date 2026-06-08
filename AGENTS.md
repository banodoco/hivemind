# Hivemind — Agent Guide

## What this pack provides

Hivemind is a shared knowledge corpus with a read path (PostgREST against
`unified_feed`) and a locked write path (a Supabase edge function called
`contribute`).  The seven executors in this pack let you search, fetch, refresh
media URLs, and contribute to the corpus from any environment — CLI, Astrid, or
a Claude Code skill.

## When to use each executor

| Executor               | Use when … |
|------------------------|------------|
| `hivemind.search`      | You need to find distillations, resources, or messages by topic or keyword. |
| `hivemind.get_item`    | You have a kind + id and want the full untruncated row with citation context. |
| `hivemind.refresh_media` | You have a Discord message id and need fresh attachment URLs for expired Discord CDN media. |
| `hivemind.contribute`  | You want to submit a new resource or a cited distillation to the corpus. |
| `hivemind.ingest_article` | You have a URL and want to extract its text and submit it as a resource. |
| `hivemind.ingest_workflow` | You have a ComfyUI workflow JSON and want to index its models and structure. |
| `hivemind.ingest_youtube`  | You have a YouTube URL and want to extract captions and submit a transcript resource. |

## Flywheel loop

1. **Search** distillations first (`hivemind.search`).
2. **On hit** — relay the answer to the human.
3. **On miss** — research the raw layer (messages, resources), answer the
   human, then **submit a cited distillation** via `hivemind.contribute`.
4. The next person who asks gets your distillation immediately.

## Key constraints

- Every executor is **stdlib-only** (no pip install required).
- Tests use `python3 -m unittest discover tests/` and mock all HTTP.
- `hivemind.refresh_media` sends Discord snowflakes as strings; never coerce
  message ids to JSON numbers.
- The contribute executor requires `HIVEMIND_CONTRIBUTOR_KEY` in the
  environment (a `hm_<64 hex>` key issued by the corpus owner).
- YouTube ingest requires `yt-dlp` on PATH at runtime (tests mock it).
