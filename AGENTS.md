# Hivemind — Agent Guide

## What this pack provides

Hivemind is a shared knowledge corpus with a read path (public PostgREST over
`message_feed`, stable `resources`, and accepted current
`resource_revisions`) and a locked write path (the `contribute` edge function).
The seven executors search, fetch exact revisions, refresh media URLs, ingest,
and contribute from CLI, Astrid, or a skill.

`hivemind.search` queries message_feed and the resources/current-revision raw
relationship in parallel with per-token ILIKE predicates and ranks the merge client-side. It deliberately never
queries the `unified_feed` UNION view for text search — that view's
derived-table scan blows the anon role's 3s statement budget (HTTP 500 /
SQLSTATE 57014) on any multi-word or per-token OR, and a single multi-word
phrase ILIKE returns zero rows (the literal substring never occurs).  Use
`resources` + `resource_revisions` for exact fetches via `hivemind.get_item`.

## When to use each executor

| Executor               | Use when … |
|------------------------|------------|
| `hivemind.search`      | Find accepted current resources or messages by topic/keyword. Per-token raw-table matching; pending and historical revisions never rank. |
| `hivemind.get_item`    | Fetch a full message, current resource, or exact pinned revision with reference context. |
| `hivemind.refresh_media` | You have a Discord message id and need fresh attachment URLs for expired Discord CDN media. |
| `hivemind.contribute`  | You want to submit/propose/review a resource revision or record exact evidence. |
| `hivemind.ingest_article` | You have a URL and want to extract its text and submit it as a resource. |
| `hivemind.ingest_workflow` | You have a ComfyUI workflow JSON and want to index its models and structure. |
| `hivemind.ingest_youtube`  | You have a YouTube URL and want to extract captions and submit a transcript resource. |

## Flywheel loop

1. **Search** accepted current resources and messages (`hivemind.search`).
2. **On hit** — relay the answer and exact resource/revision references.
3. **On miss** — research the raw message layer, answer the human, then submit
   a full cited resource revision or immutable evidence record.
4. Preserve conditions, competing methods, and source custody for the next reader.

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
# Bare query — per-token match across messages and accepted current resources.
python3 -m hivemind.executors.search.run --query "wan animate workflow" --limit 20

# Topic-dense channels: scope to the channel family for the model in question
# (minimax_h3_*, wan_*, ltx_*, comfyui, *_resources).  A bare multi-token
# query floods the ranked pool with generic rows for common tokens ("lora"),
# so scope to the channel when the topic is known.
python3 -m hivemind.executors.search.run --query "minimax lora" --channel minimax_h3_chatter

# By author, kind, time, or page:
python3 -m hivemind.executors.search.run --query "settings" --author Kijai
python3 -m hivemind.executors.search.run --query "lora" --kinds workflow --since 2026-08-01 --offset 20
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
  projects revision `payload` (full Comfy JSON). The active focused proof is
  `tests/integration/run_knowledge_model.py --scenario search_freshness`.
- `hivemind.refresh_media` sends Discord snowflakes as strings; never coerce
  message ids to JSON numbers.
- The contribute executor requires `HIVEMIND_CONTRIBUTOR_KEY` in the
  environment (a `hm_<64 hex>` key issued by the corpus owner).
- YouTube ingest requires `yt-dlp` on PATH at runtime (tests mock it).
- `scripts/convert_legacy_knowledge.py` is the only direct conversion boundary;
  use `--rehearse` for rollback proof and never run it against a live corpus.
