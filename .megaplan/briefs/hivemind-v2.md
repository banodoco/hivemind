# Hivemind v2 — unified corpus protocol, contribute edge function, and installable Astrid pack

## Outcome

Turn this repo from a single-table query skill into the full hivemind v2 artifact: a Supabase
edge function (`contribute`) that is the sole write path into the corpus, an installable Astrid
pack (6 self-contained executors) at the repo root, and updated docs (Claude skill, README,
DESIGN.md) — all speaking one protocol against the schema already written in
`schema/001_unified_corpus.sql`.

## Scope

IN:
1. Amend `schema/001_unified_corpus.sql` (NOT yet applied to any database — edit in place, no
   second migration file): add `revoked_at timestamptz` to `contributors`.
2. New Supabase edge function: `supabase/functions/contribute/` (Deno/TypeScript).
3. New Astrid external pack at the repo root: `pack.yaml` + `executors/` (6 executors) +
   `AGENTS.md` + pack `skill/SKILL.md`.
4. Test fixtures + unit tests for the pack (Python, stdlib `unittest`, mocked HTTP — no live
   network in tests) and for the edge function's validation logic (pure TS functions).
5. Docs: update `hivemind/SKILL.md` (the existing Claude skill) and `README.md` for v2; write
   `DESIGN.md` capturing the architecture; tiny `scripts/issue_contributor_key.py` helper that
   generates an `hm_` key + prints the INSERT statement with its sha256 hash.

OUT (anti-scope — do not build):
- No deployment (no `supabase` CLI invocations against the live project; the user deploys).
- No embeddings / pgvector, no curation UI, no MCP server, no orchestrators, no TaskPlans.
- No changes to the Discord mirror pipeline or the live `message_feed`.
- No changes to the Astrid repo itself (`~/Documents/reigh-workspace/Astrid` is READ-ONLY
  pattern reference).
- Do not touch `assets/`, `install.sh`'s existing skill-install behavior, or `LICENSE`.

## Context

- This repo is currently doc-only: `README.md`, `hivemind/SKILL.md` (Claude skill teaching raw
  PostgREST queries), `install.sh`, `schema/001_unified_corpus.sql` (new, unapplied).
- Live read endpoint (public, works today):
  `https://ujlwuvkrxlvoswwkerdf.supabase.co/rest/v1/message_feed`
  anon key: `sb_publishable_O38oPBafrBoFrpi_rlWJvA_UJrulFsx`
  NOTE: `unified_feed`, `external_resources`, `distillations` do NOT exist in the live DB yet
  (schema unapplied). All tests must mock HTTP; executors must accept env overrides so they can
  be pointed at any deployment.
- Architecture (already decided; DESIGN.md should capture this): raw layer (`message_feed` +
  `external_resources`) and distilled layer (`distillations` with pending→approved lifecycle,
  mandatory cites) unified by the `unified_feed` view. Reads = open PostgREST. Writes = the
  `contribute` edge function only. Flywheel loop: search distillations-first → on miss research
  raw layer → answer the human → submit a cited distillation back.

## Locked decisions

### Edge function API contract (this exact contract; both sides build against it)

`POST {SUPABASE_URL}/functions/v1/contribute` — deployed with `--no-verify-jwt`; auth is our own
header. Function uses the service-role key from its runtime env (`SUPABASE_SERVICE_ROLE_KEY`,
auto-provided by Supabase) — the service-role key never appears in client code or this repo.

Headers: `Content-Type: application/json`, `X-Contributor-Key: hm_<64 hex>`.

Auth: sha256(key) looked up in `contributors.api_key_hash` where `revoked_at is null`.

Body: `{ "action": "add_resource" | "submit_distillation", "data": { ... } }`

- `add_resource` data: `kind` (required), `source` (required), `title` (required), `body`
  (required, non-empty), optional `external_id`, `author`, `url`, `metadata` (object),
  `payload` (object). Inserts into `external_resources`.
- `submit_distillation` data: `question` (required), `answer` (required), `confidence`
  (required, one of `high|medium|low`), optional `conditions`, `supersedes_id`, and `cites`
  (required, array of `{item_kind: "message"|"resource"|"distillation", item_id: int}`, length
  ≥ 1). Inserts into `distillations` with `status` FORCED to `'pending'` (ignore any client
  value), `author_id` from the resolved contributor; then inserts `distillation_cites` rows.
  If `supersedes_id` is given, verify that row exists (else 400); do NOT mutate the old row —
  curators flip statuses at approval time.

Dedup (distillations only): trigram `similarity(question, $q)` against existing rows with
`status in ('pending','approved')`; if best similarity > 0.6 and that row's id ≠
`supersedes_id` → reject 409.

Responses: `201 {"id": N, "status": "ok"}` · `400 {"error":"validation","detail":"..."}` ·
`401 {"error":"unauthorized"}` · `409 {"error":"duplicate","existing_id":N,"detail":"similar
question exists — extend or supersede it"}`.

Implementation: keep validation + dedup-decision as pure TS functions in a module separate from
the Deno entrypoint so they unit-test without a DB.

### Astrid pack (external pack at REPO ROOT)

Astrid (read-only reference: `~/Documents/reigh-workspace/Astrid`) installs external packs via
`astrid packs install <path|git URL>`; the source directory name must equal the pack id — this
repo is named `hivemind`, pack id is `hivemind`, so `pack.yaml` sits at the REPO ROOT with
`origin: external` and `content: {executors: executors}`. Installed packs import as
`hivemind.executors.<name>.run`, so `executors/` and each executor dir need `__init__.py`
(and a root `__init__.py` if required for the namespace — check the external-pack fixture at
`Astrid/tests/fixtures/external_pack/` and `Astrid/astrid/packs/install.py` for the exact
contract; `Astrid/astrid/packs/reigh/executors/reigh_data/` is the style reference for
executor.yaml + run.py shape).

Hard constraint: every `run.py` is **stdlib-only** (argparse, json, urllib, hashlib, pathlib —
plus shelling out to `yt-dlp` where noted) and imports NOTHING from the `astrid` package. The
pack must work standalone (`python3 -m hivemind.executors.search.run --query ...` from the
repo parent, or direct `python3 executors/search/run.py`) so the same code serves non-Astrid
users. Default endpoint + anon key (above) baked in as constants; env overrides
`HIVEMIND_API_URL`, `HIVEMIND_ANON_KEY`, `HIVEMIND_CONTRIBUTOR_KEY` resolved from OS env.

The 6 executors (each: `executors/<name>/executor.yaml` + `run.py`; JSON to stdout, or to
`--out FILE` when given):

1. `hivemind.search` — `--query` (required), `--kinds`, `--sources`, `--since`, `--limit`
   (default 20). Two PostgREST GETs against `unified_feed`: distillation-kind hits first, then
   the rest; merge distillations-first. `ilike` on title+body via `or=()`. Truncate each `body`
   to 700 chars (`"truncated": true` flag on the row). When ZERO distillation rows match,
   append to the JSON output: `"nudge": "No distillation exists for this query. If you research
   this, submit one via hivemind.contribute."`
2. `hivemind.get_item` — `--kind` + `--id`. Full row, untruncated. For distillations also fetch
   its `distillation_cites`; for messages/resources also fetch distillations citing it.
3. `hivemind.contribute` — `--type resource|distillation`, field flags mirroring the API
   contract (`--question`, `--answer`, `--confidence`, `--cites message:88123,resource:17`,
   `--supersedes`, `--kind`, `--title`, `--body-file`, etc.) or `--from-file payload.json`;
   `--dry-run` prints the request body without sending. POSTs to the edge function with
   `X-Contributor-Key`; clear error if the env key is missing. Map 401/409/400 responses to
   readable messages (409 must surface `existing_id` and the extend-or-supersede advice).
4. `hivemind.ingest_article` — `--url` (+ optional `--title`, `--kind`, default `article`).
   Fetch with urllib, extract readable text with a stdlib `html.parser`-based extractor (strip
   script/style/nav, collapse whitespace; title from `<title>`/og:title fallback). Builds the
   envelope, then submits via the same code path as contribute. `--dry-run` supported.
5. `hivemind.ingest_workflow` — `--path` or `--url` to a ComfyUI workflow JSON. Parse it;
   extract `metadata.models` (checkpoint/lora/vae names found in node widget values),
   `metadata.custom_nodes` (non-core `class_type`s), `metadata.node_count`; generate the
   searchable `body` description (name, node-type summary, models list); full JSON into
   `payload`. Submit as `kind=workflow`. `--dry-run` supported.
6. `hivemind.ingest_youtube` — `--url`. Captions ONLY: shell out to `yt-dlp --skip-download
   --write-subs --write-auto-subs --sub-format vtt` into a temp dir; parse VTT → clean text
   (dedupe rolling auto-caption repeats); metadata from `yt-dlp -j` (title, channel, duration,
   video id). Submit as `kind=transcript`, `source=youtube`, `external_id=<video id>`. If no
   captions exist, exit non-zero with: captions unavailable — transcribe the audio yourself
   (e.g. Astrid's `editorial.transcribe`) and submit via `hivemind.contribute`. NO Whisper, no
   audio download, no cross-pack calls. `--dry-run` supported.

Shared HTTP/envelope helpers live in ONE module (e.g. `executors/_common.py`) imported
relatively by the executors — no duplication of the request code six times.

### Docs

- `hivemind/SKILL.md` (Claude skill): keep all current channel/query-playbook content that
  still applies; repoint primary search at `unified_feed`; document kinds + the distillation
  tiering (approved > pending); add the flywheel loop as numbered procedure — (1) search
  distillations-first, (2a) hit → relay, (2b) miss → research raw layer KEEPING item ids,
  (3) answer the human first, (4) give back: submit a cited distillation; include worth-it
  criteria (generalizable question, real research effort, ≥1 cite; supersede rather than
  duplicate) and the contribute curl example.
- `README.md`: v2 overview, the three install targets (Claude skill via install.sh, Astrid pack
  via `astrid packs install <git URL>`, raw PostgREST), schema pointer, contribute API summary.
- `DESIGN.md`: the architecture summary from Context above — layers diagram, lifecycle, what's
  deferred (embeddings on distillations, curation UI, Whisper fallback, MCP) with triggers.

## Open questions (planner resolves; do not halt)

- Exact `__init__.py` / namespace arrangement the Astrid external-pack loader needs — resolve
  by reading the fixture + install.py named above; if genuinely ambiguous, prefer the fixture's
  literal layout.
- VTT cleaning details and the ComfyUI model-name extraction heuristics — use judgment; tests
  pin the chosen behavior via fixtures.

## Constraints

- Tests: Python stdlib `unittest`, runnable as `python3 -m unittest discover tests/`; mock all
  HTTP (no live network). Edge function logic: pure-TS validation module; if `deno` is on PATH,
  add `deno test` + `deno check`; if not, the TS must still be cleanly structured and the test
  file written so `deno test` passes once available (note which in the final report).
- Fixtures: a small sample ComfyUI workflow JSON, a sample VTT file, a sample article HTML —
  committed under `tests/fixtures/`.
- No new Python dependencies; no Node toolchain; yt-dlp invoked as an external binary only
  (skip live yt-dlp in tests — fixture VTT instead).
- Keep the repo installable as a Claude skill exactly as today (install.sh path untouched).

## Done criteria

1. `python3 -m unittest discover tests/` passes (envelope builders, VTT parser, workflow
   extractor, article extractor, contribute request building incl. dry-run, search merge +
   truncation + nudge logic — all against fixtures/mocked HTTP).
2. Edge function: pure validation/dedup-decision functions covered by a deno test file;
   `deno check` clean if deno available.
3. `pack.yaml` + executor manifests are structurally consistent with Astrid's external-pack
   fixture (and `python3 -m astrid packs validate <this repo>` passes if runnable against the
   read-only Astrid checkout — do not modify that repo to make this work).
4. Every executor responds to `--help`; ingest executors + contribute produce correct envelopes
   under `--dry-run` against the fixtures.
5. Docs updated as specified; `DESIGN.md` exists; key-issuance helper script works
   (`python3 scripts/issue_contributor_key.py` prints key + INSERT).

## Touchpoints

`schema/001_unified_corpus.sql` · `supabase/functions/contribute/` (new) · `pack.yaml` (new) ·
`executors/` (new) · `tests/` (new) · `hivemind/SKILL.md` · `README.md` · `DESIGN.md` (new) ·
`AGENTS.md` (new, pack-level) · `skill/SKILL.md` (new, pack-level) · `scripts/` (new)
