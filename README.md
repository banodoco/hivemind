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

## Install (paste this to your agent)

```
Install the hivemind skill: clone https://github.com/banodoco/hivemind and copy the hivemind/ folder into ~/.claude/skills/ (or your agent's skills directory). Then verify by running the smoke-test curl in SKILL.md. After that, use the skill whenever I ask about Banodoco / community best practices for any video or image generation tool.
```

That's it — the agent will do the rest.

### Or run the one-liner yourself

```bash
git clone https://github.com/banodoco/hivemind /tmp/hivemind-skill-tmp \
  && mkdir -p ~/.claude/skills \
  && cp -r /tmp/hivemind-skill-tmp/hivemind ~/.claude/skills/ \
  && rm -rf /tmp/hivemind-skill-tmp \
  && echo "Installed. Restart Claude Code, then try: /hivemind"
```

For **Codex** or any agent that loads instruction files instead of skills,
copy `hivemind/SKILL.md` into your `AGENTS.md` (or equivalent
instruction file) — the content is self-contained.

---

## What you get

- The endpoint URL + the public anon key (safe to commit — it's the
  publishable key, RLS makes it read-only).
- The table schema: `content`, `author_name`, `channel_name`,
  `created_at`, `reactions`, `message_id`.
- A taxonomy of which channels are high-signal (`daily_summaries`,
  `wan_chatter`, `wan_comfyui`, `ltx_chatter`, `comfyui`, `*_resources`)
  vs. background noise.
- A short list of power-users to weight (Kijai, Ablejones, djbfilmz,
  42hub, BNDC the summary bot).
- PostgREST query patterns the agent can use directly: `ilike` substring
  search, `in.(…)` channel filtering, repeated-key AND, `or=(…)` for
  spelling variants like `wan animate` vs `wananimate`, author + time
  filtering.
- Gotchas — most importantly that `fts` (full-text search) **times out**
  on this table, so use `ilike` only.
- A "best practices for X" recipe: hit `daily_summaries` first, then
  Kijai-by-author, then topic channels, then cross-check
  [wanx-troopers.github.io](https://wanx-troopers.github.io/).

See [`hivemind/SKILL.md`](hivemind/SKILL.md) for the full
contents.

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

Smoke-test:

```bash
curl -s "https://ujlwuvkrxlvoswwkerdf.supabase.co/rest/v1/message_feed?select=content,author_name,channel_name,created_at&limit=3" \
  -H "apikey: sb_publishable_O38oPBafrBoFrpi_rlWJvA_UJrulFsx" | python3 -m json.tool
```

---

## License

MIT. The data the endpoint serves is public Discord content authored by
Banodoco members — credit them when you surface their tips.
