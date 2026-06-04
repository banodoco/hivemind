# banodoco/hivemind

A drop-in skill that lets a coding agent (Claude Code, Codex, etc.) search the
**Banodoco Discord message feed** — a public, read-only PostgREST mirror of
the Banodoco server. Banodoco is where a lot of the practitioners working on
generative video & image tooling (Wan, Wan Animate, VACE, LTX, ComfyUI,
Kijai's nodes, SCAIL, InfiniteTalk, training, etc.) actually talk to each
other. This skill teaches your agent how to query it.

> The endpoint exposes the same data the Discord shows — public messages,
> attributed by display name. Be respectful when surfacing tips and credit
> authors.

---

## v2: Unified Corpus Protocol

Hivemind v2 upgrades the raw message feed into a **unified knowledge corpus**
with a read path (public PostgREST against `unified_feed`) and a locked write
path (Supabase edge function). The corpus combines:

- **Messages** — the original Discord message feed (v1)
- **Resources** — articles, transcripts, ComfyUI workflows, blog posts
- **Distillations** — curated Q&A pairs with cited sources, submitted by agents
  and reviewed by humans

Everything is searchable through one `unified_feed` view. Distillations make
the corpus self-improving: every researched answer becomes a permanent,
findable entry for the next person.

---

## Install targets

### 1. Claude Code skill (the classic)

```bash
git clone https://github.com/banodoco/hivemind /tmp/hivemind-skill-tmp \
  && mkdir -p ~/.claude/skills \
  && cp -r /tmp/hivemind-skill-tmp/hivemind ~/.claude/skills/ \
  && rm -rf /tmp/hivemind-skill-tmp \
  && echo "Installed. Restart Claude Code, then try: /hivemind"
```

Or run `bash install.sh` from a clone of this repo.

### 2. Astrid pack (executor-based)

```bash
astrid pack install /path/to/hivemind
```

The pack exposes six executors: `hivemind.search`, `hivemind.get_item`,
`hivemind.contribute`, `hivemind.ingest_article`, `hivemind.ingest_workflow`,
`hivemind.ingest_youtube`. See `AGENTS.md` for the agent guide.

### 3. Codex / any agent (instruction-file copy)

Copy `hivemind/SKILL.md` into your `AGENTS.md` (or equivalent instruction
file) — the content is self-contained with endpoint, schema, query patterns,
and the full contribute API.

---

## What you get

### Read path

- The endpoint URL + the public anon key (safe to commit — it's the
  publishable key, RLS makes it read-only).
- The `unified_feed` view: messages + resources + distillations in one table.
- `ilike` search across title and body, distillations-first merging.
- Get single items by kind + id with full citation context.
- A taxonomy of which Discord channels are high-signal (`daily_summaries`,
  `wan_chatter`, `wan_comfyui`, `ltx_chatter`, `comfyui`, `*_resources`)
  vs. background noise.
- A short list of power-users to weight (Kijai, Ablejones, djbfilmz,
  42hub, BNDC the summary bot).
- PostgREST query patterns the agent can use directly: `ilike` substring
  search, `in.(…)` channel filtering, repeated-key AND, `or=(…)` for
  spelling variants, author + time filtering.
- Gotchas — most importantly that `fts` (full-text search) **times out**
  on this table, so use `ilike` only.

### Write path (contribute API)

- `POST /functions/v1/contribute` — the single write entrypoint.
- Auth via `X-Contributor-Key: hm_<64 hex>` header (SHA-256 checked against
  the `contributors` table, revoked keys rejected).
- Two actions:
  - `add_resource` — submit articles, transcripts, workflows.
  - `submit_distillation` — submit Q&A with ≥1 cited source. Duplicate
    detection via `pg_trgm` similarity (>0.6 threshold), `supersedes_id`
    for replacing outdated answers.
- Status forced to `pending` by the edge function; curators promote to
  `approved`.
- Responses: 201 (created), 400 (validation), 401 (unauthorized), 409
  (duplicate), 500 (internal error).

### Flywheel loop

1. **Search** distillations first on the user's question.
2. **Hit** → relay the answer with its cites.
3. **Miss** → research raw messages/resources, answer the human, then
   **submit a cited distillation**.
4. The next person who asks gets it immediately.

### Worth-it criteria for distillations

Before submitting, check:
- The question is generalizable (not a one-off personal request).
- You did real research effort (surfaced sources, compared answers).
- You have at least one cite.
- If a similar question exists, supersede it rather than duplicating.

### Contribute curl example

```bash
curl -s -X POST "$SUPABASE_URL/functions/v1/contribute" \
  -H "Content-Type: application/json" \
  -H "X-Contributor-Key: hm_$(cat ~/.hivemind/key)" \
  -d '{
    "action": "submit_distillation",
    "data": {
      "question": "What is the best upscale model for anime-style Wan output?",
      "answer": "4x-UltraSharp with 50% blend on original, then a second GFPGAN pass.",
      "confidence": "high",
      "conditions": "for anime-style video, 1080p target",
      "cites": [
        {"item_kind": "message", "item_id": 88123},
        {"item_kind": "resource", "item_id": 17}
      ]
    }
  }'
```

---

## Executor inventory

| Executor | CLI | Description |
|---|---|---|
| `search` | `python3 executors/search/run.py --query "..."` | ilike search on unified_feed, distillations-first |
| `get_item` | `python3 executors/get_item/run.py --kind distillation --id 42` | Full untruncated row with citation context |
| `contribute` | `python3 executors/contribute/run.py --type resource ...` | Submit resources or distillations via edge function |
| `ingest_article` | `python3 executors/ingest_article/run.py --url https://...` | Extract HTML text → submit as resource |
| `ingest_workflow` | `python3 executors/ingest_workflow/run.py --path workflow.json` | Parse ComfyUI JSON → extract models → submit |
| `ingest_youtube` | `python3 executors/ingest_youtube/run.py --url https://...` | yt-dlp captions → submit transcript resource |

All executors are stdlib-only Python. See `AGENTS.md` for usage guidance and
`DESIGN.md` for architecture details.

---

## Full Dataset on Huggingface

Hivemind is for querying the live public message feed from an agent. If you want
to train on the full archive or grab the whole dataset directly, use the
Hugging Face dataset:

https://huggingface.co/datasets/Banodoco/discord-archive

That dataset contains the exported Discord archive with opted-out authors
excluded.

---

## Example prompts that will trigger the skill

- "What does Banodoco say about Wan Animate best practices?"
- "Search Banodoco for SCAIL vs Wan Animate"
- "What settings has Kijai recommended for the lightx2v LoRA?"
- "Find me workflows for long-video context windows in Wan"
- "What did people say about LTX 2.3 last week?"

---

## Endpoint

```
GET https://ujlwuvkrxlvoswwkerdf.supabase.co/rest/v1/message_feed
Header: apikey: sb_publishable_O38oPBafrBoFrpi_rlWJvA_UJrulFsx
```

The `unified_feed` view is at the same base:

```
GET https://ujlwuvkrxlvoswwkerdf.supabase.co/rest/v1/unified_feed
```

Smoke-test:

```bash
curl -s "https://ujlwuvkrxlvoswwkerdf.supabase.co/rest/v1/unified_feed?select=kind,title,body&limit=3" \
  -H "apikey: sb_publishable_O38oPBafrBoFrpi_rlWJvA_UJrulFsx" | python3 -m json.tool
```

---

## Further reading

- **[hivemind/SKILL.md](hivemind/SKILL.md)** — full raw query playbook: channel
  map, power users, search snippets, trend questions, caveats.
- **[AGENTS.md](AGENTS.md)** — agent guide: when to use each executor, flywheel
  loop, key constraints.
- **[DESIGN.md](DESIGN.md)** — architecture, schema design, lifecycle, deferred
  work, design decisions.
- **[skill/SKILL.md](skill/SKILL.md)** — Astrid skill documentation: read/write
  paths, contribute API, curl examples.

---

## License

MIT. The data the endpoint serves is public Discord content authored by
Banodoco members — credit them when you surface their tips.
