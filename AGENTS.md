# Hivemind — Agent Guide

## What this pack provides

Hivemind is a shared knowledge corpus with a read path (public PostgREST over
the raw corpus tables — `message_feed`, `external_resources`,
`distillations`) and a locked write path (a Supabase edge function called
`contribute`).  The seven executors in this pack let you search, fetch, refresh
media URLs, and contribute to the corpus from any environment — CLI, Astrid, or
a Claude Code skill.

`hivemind.search` queries the three raw tables in parallel with per-token
ILIKE predicates and ranks the merge client-side.  It deliberately never
queries the `unified_feed` UNION view for text search — that view's
derived-table scan blows the anon role's 3s statement budget (HTTP 500 /
SQLSTATE 57014) on any multi-word or per-token OR, and a single multi-word
phrase ILIKE returns zero rows (the literal substring never occurs).  Use
`unified_feed` kind-scoped only for single-row fetches via `hivemind.get_item`.

## When to use each executor

| Executor               | Use when … |
|------------------------|------------|
| `hivemind.search`      | You need to find distillations, resources, or messages by topic or keyword. Per-token matching over raw tables — works for multi-word queries (the old unified_feed phrase search returned zero rows). Filters: `--kinds`, `--sources`, `--since`, `--channel`/`--author` (messages only), `--thread` (thread id); page with `--limit`/`--offset` (response has `total`/`has_more`). |
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

## Installing and using this pack

The repo root doubles as the importable `hivemind` package.  Two equivalent
ways to run the search executor:

```bash
# From anywhere, after the editable install (this repo's pyproject maps the
# repo root to the 'hivemind' package):
pip install -e /Users/peteromalley/Documents/banodoco-workspace/hivemind
python3 -m hivemind.executors.search.run --query "which lora" --out /tmp/search.json

# Or straight from the repo root, no install needed (stdlib-only):
python3 executors/search/run.py --query "which lora" --out /tmp/search.json
```

Basic usage (the full surface table and filter reference live in
`skill/SKILL.md` — that is the canonical playbook):

```bash
# Bare query — per-token match across messages, resources, distillations.
python3 -m hivemind.executors.search.run --query "wan animate workflow" --limit 20

# Topic-dense channels: scope to the channel family for the model in question
# (minimax_h3_*, wan_*, ltx_*, comfyui, *_resources).  A bare multi-token
# query floods the ranked pool with generic rows for common tokens ("lora"),
# so scope to the channel when the topic is known.
python3 -m hivemind.executors.search.run --query "minimax lora" --channel minimax_h3_chatter

# By author, kind, time, or page:
python3 -m hivemind.executors.search.run --query "settings" --author Kijai
python3 -m hivemind.executors.search.run --query "distillation" --kinds distillation
python3 -m hivemind.executors.search.run --query "lora" --since 2026-08-01 --offset 20
```

The executor writes JSON to `--out` (or stdout): `results[]`, `total`,
`has_more`, `next_offset`.  A human summary prints to stderr.  This search is
the canonical research surface for the corpus — use it in place of any
`unified_feed` text-search client (which times out with 57014 and/or returns
zero rows for multi-word queries).

## Key constraints

- Every executor is **stdlib-only** (no pip install required).
- Tests use `python3 -m unittest discover tests/` and mock all HTTP.
- `hivemind.search` never queries `unified_feed` for text search and never
  projects `payload` on `external_resources` (full Comfy JSON). The tests
  grep for both (see `tests/test_search.py`).
- `hivemind.refresh_media` sends Discord snowflakes as strings; never coerce
  message ids to JSON numbers.
- The contribute executor requires `HIVEMIND_CONTRIBUTOR_KEY` in the
  environment (a `hm_<64 hex>` key issued by the corpus owner).
- YouTube ingest requires `yt-dlp` on PATH at runtime (tests mock it).
