# Hivemind v2 — Design

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                      Read Path                           │
│  Public PostgREST → unified_feed view → anon key         │
│  (RLS-enforced read-only: message_feed, external_        │
│   resources, distillations, distillation_cites)          │
├──────────────────────────────────────────────────────────┤
│                      Write Path                          │
│  Edge function (contribute) → service role               │
│  Auth: X-Contributor-Key + SHA-256 hash → contributors   │
│  Only "pending" distillations; resources direct insert   │
└──────────────────────────────────────────────────────────┘
```

Hivemind v2 is a **unified corpus protocol** — a single searchable surface (`unified_feed`)
combining raw Discord messages, externally ingested resources (articles, transcripts,
workflows), and curated question/answer distillations, all surfaced through a public
PostgREST API. Writes are locked behind a single Supabase edge function that
authenticates contributor keys and enforces the data model.

## Schema

The corpus lives in `schema/001_unified_corpus.sql` and adds these tables alongside the
existing `message_feed`:

| Table | Purpose |
|---|---|
| `external_resources` | Articles, transcripts, ComfyUI workflows, blog posts, repos. `kind`-polymorphic, one table for all low-volume resource types. `body` holds the full searchable text; `url` is provenance only. Unique on `(source, external_id)`. |
| `contributors` | Identities for agents/humans who submit distillations. `api_key_hash` (SHA-256 of `hm_<64 hex>` key) checked by the edge function. `revoked_at` enables key rotation without deletion. **Never publicly readable** — RLS enabled with zero policies. |
| `distillations` | Question-shaped knowledge with a review lifecycle: `pending` → `approved` / `rejected` / `superseded`. Agents can only create `pending` rows. Supersede, don't edit-in-place: `supersedes_id` points at the replaced distillation. |
| `distillation_cites` | Provenance edges. Every distillation must cite ≥1 item (message, resource, or another distillation). Polymorphic reference — no hard FK on `(item_kind, item_id)`. |
| `unified_feed` | The one searchable view. Combines messages, external_resources, and non-rejected distillations into a common shape: `(kind, source, item_id, title, body, author, context, url, metadata, created_at)`. |

### RPC: `check_duplicate_distillation`

PostgREST cannot use `pg_trgm.similarity()` as a REST filter, so the edge function
calls this RPC function at `/rest/v1/rpc/check_duplicate_distillation`. It returns
the best pending/approved match above a strict `> 0.6` similarity threshold, excluding
the caller's own `supersedes_id`.

## Read path

All reads use the public PostgREST endpoint with the anon (publishable) key.
RLS policies grant `select` on `external_resources`, `distillations` (excluding
rejected), `distillation_cites`, and the `unified_feed` view. The `contributors`
table is invisible to anon/authenticated roles — only the service role can read it.

Six executors wrap the read path:
- **search** — ilike queries on `unified_feed`, distillations-first merging
- **get_item** — single-row fetch with citation context (distillation_cites join)
- **ingest_article** — URL → HTML extraction → resource submission
- **ingest_workflow** — ComfyUI JSON → model extraction → resource submission
- **ingest_youtube** — YouTube URL → yt-dlp caption extraction → transcript resource

## Write path

All writes flow through a single Supabase edge function at `POST /functions/v1/contribute`.

### Authentication

1. Client sends `X-Contributor-Key: hm_<64 hex>` header.
2. Edge function computes SHA-256 of the full key string (including `hm_` prefix).
3. Looks up `contributors` where `api_key_hash = eq.<hash> AND revoked_at IS NULL`.
4. On match → authorized; on miss → 401.

### Two actions

**`add_resource`** — inserts into `external_resources`. Returns 409 on unique
violation (`source + external_id` already exists).

**`submit_distillation`** — four-step transaction:
1. Check duplicate via RPC (`check_duplicate_distillation`). If `similarity > 0.6`
   and the existing ID isn't the caller's `supersedes_id`, return 409 with the
   existing ID.
2. If `supersedes_id` is provided, verify the target exists → 400 if not.
3. Insert distillation row with `status = 'pending'` (forced by the edge function,
   ignoring any client-supplied value).
4. Insert `distillation_cites` rows (≥1 required, validated by protocol.ts).

### Response codes

| Status | Body | Meaning |
|---|---|---|
| 201 | `{"id": N, "status": "ok"}` | Created |
| 400 | `{"error":"validation","detail":"..."}` | Invalid request |
| 401 | `{"error":"unauthorized"}` | Bad or revoked key |
| 409 | `{"error":"duplicate","existing_id":N,...}` | Similar question exists |
| 405 | `{"error":"method_not_allowed",...}` | Not POST |
| 500 | `{"error":"internal","detail":"internal server error"}` | Unexpected error |

## Lifecycle

```
                    ┌──────────┐
                    │ pending   │  ← agents create (edge function enforces)
                    └────┬─────┘
                         │ curator review
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
    ┌──────────┐  ┌───────────┐  ┌────────────┐
    │ approved │  │ rejected  │  │ superseded  │  ← set when a new distillation
    └──────────┘  └───────────┘  │              │    points supersedes_id here
                                 └────────────┘
```

Key design decision: **supersede, don't edit**. When knowledge changes, create a new
distillation row with `supersedes_id` pointing at the old one. The old row's status
should be manually updated to `superseded`. This preserves provenance and lets the
unified feed show the latest answer while the archive retains history.

## Flywheel loop

The core value proposition — each human question that requires research becomes a
permanent, searchable distillation:

1. **Search** distillations first on the user's question.
2. **Hit** → relay the answer with its cites.
3. **Miss** → research the raw layer (messages, resources), keeping item IDs.
   Answer the human.
4. **Give back** — submit a cited distillation via the write path. The next person
   who asks gets it immediately.

## Deferred work

Items intentionally left for future iterations:

- **Curation UI** — no dashboard for reviewing pending distillations. Curators must
  use direct SQL or a future admin tool.
- **Contributor onboarding** — `contributors` rows are issued manually. No self-service
  registration.
- **Automated ingestion** — no cron/trigger for periodic YouTube channel scraping or
  RSS feed monitoring. Ingest executors are on-demand only.
- **Superseded status propagation** — when a new distillation supersedes an old one,
  the old row's status must be manually updated to `superseded`. No trigger automates
  this.
- **Full-text search** — `ilike` with pg_trgm indexes works well for the current
  corpus size but will need FTS (`tsvector`/`tsquery`) at scale.
- **Reaction/ranking signals** — the `reactions` column on messages is mostly null
  and not used for ranking. Distillation quality scoring is deferred.
- **Channel inventory view** — no `message_feed_channels` materialized view. Channel
  discovery requires a `select distinct` maintenance query.

## Design decisions

| Decision | Rationale |
|---|---|
| Polymorphic `external_resources.kind` over separate tables | Low volume per kind; avoids DDL for each new resource type |
| `body` always holds full searchable text | `url` is provenance only — never a substitute for searchable content |
| SHA-256 of full key string (including `hm_` prefix) | Matching contract between `issue_contributor_key.py` and edge function |
| `pg_trgm` similarity > 0.6 threshold | Strict enough to avoid false duplicate flags, loose enough to catch rephrasings |
| `contributors` invisible to anon | Key hashes are sensitive; zero RLS policies = deny-all for public roles |
| `unified_feed` as a view, not a materialized table | Always fresh; pg_trgm indexes on underlying tables provide sufficient performance |
| `security_invoker = true` on unified_feed | Each underlying table's RLS policies apply, so rejected distillations are excluded |
| Stdlib-only Python executors | Zero pip install; works in any Python 3.9+ environment |
| Deno edge function with pure protocol.ts | Validation logic is testable without Supabase; 103 protocol tests run in <1s |
