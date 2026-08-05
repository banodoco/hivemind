# Phase 0 — Task 0.2 Schema & Eligibility Map

**Date:** 2026-07-28
**Task:** 0.2 — Inventory source tables, views, eligibility rules, opt-outs,
deletion behavior, RLS, grants, and ingestion paths.
**Plan:** `docs/architecture/hivemind-hybrid-search-plan.md` (Astrid planning copy)
**Prereq:** Task 0.1 — access audit (`docs/hybrid-search/phase0-access-audit.md`).
**Reproduce:** `python3 scripts/inventory_schema.py` (catalog + public eligibility),
`python3 scripts/inventory_schema.py --json out.json`. Tests:
`python3 -m unittest tests.test_inventory_schema`.

This map records **safe schema/object names, RLS policies, decoded grants,
constraint/check definitions, function security flags, row-count estimates, and
redacted role names only.** No API key, database password, token, connection
string, key hash, or row body is recorded anywhere in this file. Every figure is
from a live read-only probe on 2026-07-28 against the verified Hivemind project
(`ujlwuvkrxlvoswwkerdf`), via the task-0.1 session-mode path and the public
publishable-key path.

The headline finding for later phases: **the live corpus is healthy and matches
the repo contract where it matters, but the current search/read path does not
enforce two eligibility rules the plan assumes** — soft-deleted messages are
searchable, and an author-consent flag exists but is applied nowhere. These are
called out under [Drift](#repo-vs-live-drift) and
[Unresolved gaps](#unresolved-gaps-and-decisions-for-later-tasks).

---

## Scope and method

Inventory was produced two ways and reconciled:

1. **Repo-defined schema + ingestion code** — `schema/001_unified_corpus.sql`,
   `schema/002_vibecomfy_ratings.sql`, `supabase/functions/{contribute,
   contribute-resource,submit-vibecomfy-rating}/`, `executors/`,
   `scripts/backfill_workflow_semantics.py`, `executors/workflow_semantics.py`,
   `DESIGN.md`, `README.md`, `skill/SKILL.md`.
2. **Live database metadata** — `scripts/inventory_schema.py` runs a strictly
   read-only catalog pass (`pg_catalog` + `pg_policies` + bounded `SELECT … GROUP
   BY`; pinned read-only by `tests/test_inventory_schema.py`) plus a public
   publishable-key eligibility pass (`Prefer: count=exact`). The CLI login
   credential is held only inside a child-process env for one `psql` call and is
   never printed or persisted (same boundary as task 0.1).

The login role has full `pg_catalog` visibility but **no SELECT on user tables**,
so structural facts come from the catalog and eligibility counts come from the
public anon path (which is exactly the public-eligible picture after RLS).

---

## Entity map at a glance

| Plan entity | Live object(s) | Kind | ~Rows | Public-read gate |
|---|---|---|---:|---|
| message | `discord_messages` (table) → `message_feed` (view) → `unified_feed` (view branch) | table / view | 1,251,991 | RLS `using (true)` |
| resource (article / workflow / transcript / …) | `external_resources` (table) → `unified_feed` branch | table | 2,759 | RLS `using (true)` |
| workflow (concrete resource kind) | `external_resources` where `kind='workflow'`, `source∈{vibecomfy,vibecomfy-external}` | table rows | 2,757 | RLS `using (true)` |
| distillation | `distillations` (table) → `unified_feed` branch | table | 11 | RLS `using (status <> 'rejected')` |
| distillation citation | `distillation_cites` (table) | table | 33 | RLS `using (true)` |
| contributor (identity) | `contributors` (table) | table | 3 | RLS enabled, **zero policies → deny-all** |
| vibecomfy rating | `vibecomfy_ratings` (table) | table | 4 | RLS `using (true)` |
| author / channel / reaction (Discord envelope) | `members`, `discord_channels`, `discord_reactions`, `discord_reaction_log` | tables | 7,672 / 1,241 / 271,265 / 51,271 | RLS `using (true)` (reaction_log: RLS off) |

Counts are live `n_live_tup` / `count=exact` on 2026-07-28. Discord snowflake
`message_id`/`channel_id`/`guild_id`/`author_id` are stored as `bigint`.

`public` holds **537 relations**: the Hivemind corpus above plus a much larger
Banodoco/Astrid application (admins, payments, votes, agent-node catalog, media,
competitions, etc.). Only the corpus rows above are in scope for search; the
search RPC must be explicitly scoped to them and never scan the shared schema.

---

## 1. Messages

### Underlying table: `discord_messages` (the indexed surface)

The plan says "search the underlying Discord messages table through its existing
matching GIN expression." That table is **`discord_messages`** (not
`message_feed`, which is a view).

Columns (21): `message_id bigint PK`, `channel_id bigint`, `author_id bigint`,
`content text` (nullable), `created_at timestamptz`, `attachments jsonb default '[]'`,
`embeds jsonb default '[]'`, `reaction_count int default 0`, `reactors jsonb default '[]'`,
`reference_id bigint`, `edited_at timestamptz`, `is_pinned bool default false`,
`thread_id bigint`, `message_type text`, `flags int`, **`is_deleted bool default false`**,
`indexed_at timestamptz default now()`, `synced_at timestamptz default now()`,
`edit_history jsonb default '[]'`, **`deleted_at timestamptz`**, `guild_id bigint`.

Existing indexes on `discord_messages`:

- **GIN expression FTS**: `USING gin (to_tsvector('english'::regconfig, content))`
  — the plan-critical index. **Configuration is `english`.** (Index name is
  operator-controlled and masked by the probe's ≥32-char safety filter; it is
  not a secret. The expression is the actionable fact.)
- btree: `message_id` (PK), `author_id`, `channel_id`, `created_at`, `guild_id`,
  `thread_id`, `synced_at`, `reference_id`, `is_deleted`, and a partial
  `(channel_id, reaction_count DESC) WHERE message_id = thread_id AND is_deleted = false`.

Trigger: `BEFORE INSERT OR UPDATE … EXECUTE FUNCTION enforce_writable_server_guild()`
— an ingestion guard (server-guild allowlist on writes), not a search-eligibility
control.

### Presentation view: `message_feed` (a VIEW, RLS off)

`message_feed` is a **view**, not a table: `discord_messages m` left joins
`members a` (author display name) and `discord_channels c` (channel name), plus a
correlated subquery over `discord_reactions` (`removed_at IS NULL`) for the
reactions list. It projects exactly the columns `unified_feed` consumes:
`message_id, content, created_at, author_name, channel_name, channel_id, guild_id, reactions`.

**`message_feed` has no `WHERE` clause** — it does **not** filter `is_deleted`.
`recent_messages` and `message_stats` (two sibling views) **do** filter
`is_deleted = false`; the search surface does not. See [Deletion](#5-deletion-and-update-paths).

### Hydration into `unified_feed`

`unified_feed` message branch (live, matches repo 001 exactly):

```text
kind='message', source='banodoco-discord', item_id=message_id::text,
title=null, body=content, author=author_name, context=channel_name,
url='https://discord.com/channels/'||guild_id||'/'||channel_id||'/'||message_id,
metadata=jsonb_build_object('channel_id', channel_id, 'reactions', reactions),
created_at
```

`item_id` is cast to **text** here, so Discord snowflakes survive the public JSON
boundary as strings (confirmed by the inventory's 25-row sample: all `item_id`
are strings). The stored `message_id` is `bigint`; the plan's "strings at every
JSON/shared-index boundary" invariant holds at the API but **not** inside the DB
or in `get_item`/cite `item_id` (`bigint`) — see [Unresolved gaps](#unresolved-gaps-and-decisions-for-later-tasks).

### Channel/author/reaction envelope (filtering surface)

- `discord_channels` (1,241): `channel_id, channel_name, guild_id, channel_type,
  parent_id, category_id, nsfw, speaker_mode`, plus effective-config booleans
  (`logging_enabled, archiving_enabled, summarising_enabled, reactions_enabled,
  sharing_enabled`). Channel-name and channel-id filters (plan AD-1) join here.
- `members` (7,672): `member_id, username, global_name, server_nick, bot, system,
  allow_content_sharing, include_in_updates, …`. Author filters join here. **Author
  display name in the feed = `COALESCE(global_name, username)`** (not server_nick).
- `discord_reactions` (271,265) with `removed_at`; aggregated into `message_feed.reactions`.
  The plan treats reactions as non-authoritative for ranking; they are mostly null
  in the corpus per `DESIGN.md` and are not a ranking input.

---

## 2. External resources (articles, transcripts, workflows)

### Table: `external_resources` (RLS `using (true)`)

Columns (11) — matches repo 001 exactly: `id bigint identity PK`, `kind text`,
`source text`, `external_id text`, `title text`, `body text`, `author text`,
`url text`, `metadata jsonb default '{}'`, `payload jsonb`, `created_at timestamptz`.
Constraints: PK `(id)`; **unique `(source, external_id)`**.

Indexes: `external_resources_body_trgm` (GIN `body gin_trgm_ops`),
`external_resources_title_trgm` (GIN `title`), `external_resources_kind_idx` (btree `kind`),
`external_resources_metadata_idx` (GIN `metadata`), PK, plus one further index
(name masked). **No FTS/`tsvector` index exists on resources** — lexical resource
search today is `ILIKE` over `unified_feed` (unindexed for this arm). Phase 1
(task 1.2) adds the resource `tsvector`.

Live distribution (count=exact, 2026-07-28): **2,759 rows** — `kind=workflow`
2,757, `kind=transcript` 1, and 1 further row. Sources are overwhelmingly
`vibecomfy-external` (498 of a 500-row sample), plus two anomalies: one row
`source='comfyui'` and one row `source='s'` (data-quality drift; flagged).

### Workflows — the representation contract (plan AD-4, AD-5, task 0.8)

Workflow resources are `external_resources` rows with `kind='workflow'`. Their
searchable content lives in three places, and the plan's precedence
(`payload.python_source` → body delimiter → recoverable → unavailable) maps onto:

- `body text` — full searchable prose rendition. The repo backfill
  (`executors/workflow_semantics.py`) **appends** a compact
  `"Workflow semantics (rule-based): …"` block to `body` (it no-ops if already
  present), so `body` is prose + semantics, and may also carry a legacy delimited
  Python block (`"Python ready-template source:"` / `"Python scratchpad source:"`).
- `metadata jsonb.workflow_semantics` (+ `metadata.workflow_semantics_version=1`)
  — structured, rule-derived facts: `media_type, task_type, model_families,
  adapter_directions, node_types, node_class_multiset, custom_nodes, models,
  searchable_aliases, evidence{derived_from,confidence}, promotion_gates
  {has_workflow_json,has_compiled_api,has_python_source,parseable_workflow}`.
  This is the projection the plan embeds into the workflow-prose canonical text.
- `payload jsonb` — `python_source`, `workflow_json`, `compiled_api`, `corpus_path`
  (native artifacts). `payload.python_source` is AD-4's authoritative Python.

**Bounded live sample (first 200 workflows, 2026-07-28):**

| payload key present | count / 200 |
|---|---:|
| `corpus_path` | 199 |
| `python_source` (non-empty) | 19 |
| `workflow_json` | 19 |
| `compiled_api` | 19 |

This confirms the plan's representation-cohort concern: only a minority of
workflows have materialized `python_source`/`workflow_json`/`compiled_api` in the
DB, while nearly all carry a `corpus_path` (local VibeComfy artifact) — i.e. the
bulk are in the **`recoverable`** cohort (regenerable via the VibeComfy exporter)
or **`body_python`**, not **`payload_python`**. Exact cohort counts (and the
body-vs-payload duplication rate) are task 0.3 / 0.8; this map establishes the
structural columns and the bounded signal only.

Non-workflow resources are ordinary prose (`body`), one transcript today.

---

## 3. Distillations

### Table: `distillations` (RLS `using (status <> 'rejected')`)

Columns (11) — matches repo 001: `id bigint identity PK`, `question text`,
`conditions text`, `answer text`, `confidence text check ∈ {high,medium,low}`,
`status text default 'pending' check ∈ {pending,approved,rejected,superseded}`,
`author_id bigint → contributors(id)`, `approved_by bigint → contributors(id)`,
`supersedes_id bigint → distillations(id)`, `created_at`, `updated_at`.

Constraints (live, match repo): status check, confidence check, FK
`author_id→contributors`, FK `approved_by→contributors`, FK
`supersedes_id→distillations(id)` (self-reference; **not** `ON DELETE CASCADE`).

Indexes: `distillations_question_trgm` (GIN `question`), `distillations_answer_trgm`
(GIN `answer`), `distillations_status_idx` (btree `status`), PK. **No FTS/`tsvector`
index** — Phase 1 (task 1.2) adds the weighted `tsvector(question, conditions, answer)`.

### Lifecycle / eligibility (the approval+pending rules)

- **Agents can only create `pending`** — the `contribute` edge function forces
  `status='pending'` and ignores any client-supplied status.
- **Public read excludes `rejected`** — enforced two ways: the RLS policy
  `using (status <> 'rejected')`, and the `unified_feed` branch
  `WHERE status IN ('pending','approved')`. (Net effect identical for the feed;
  superseded rows remain publicly visible in the feed because the view keeps
  `pending`/`approved`, while supersession is a manual `status` update — see
  `DESIGN.md` "supersede, don't edit".)
- **Promotion to `approved`/`rejected`/`superseded` is curator-only** via direct
  SQL (no curation UI; `DESIGN.md` deferred work). `approved_by` records the curator.

Live distribution (count=exact, 2026-07-28): **11 distillations, all `pending`**
(approved 0, rejected 0, superseded 0). Confidence: low 2, medium 6, high 3.
Consequence for the plan: the "approved-distillation boost" weighting (AD-6) has
**no approved rows to boost yet**; the pilot/eval must not assume approved
coverage exists.

### Duplicate-check RPC: `check_duplicate_distillation`

`check_duplicate_distillation(question_text text, supersedes_id bigint default null)`
→ `table(existing_id bigint, similarity real)`. **`language sql`, `stable`,
`prosecdef=false` (NOT SECURITY DEFINER), no `search_path` config.** Returns the
best `pending`/`approved` match with `similarity(question, question_text) > 0.6`,
excluding `supersedes_id`. Called by the `contribute` edge function via
`/rest/v1/rpc/…`. This is the only existing corpus function; it is a caller-context
function, **not** the hardened-SECURITY-DEFINER pattern the search RPC will use.

---

## 4. Distillation citations

### Table: `distillation_cites` (RLS `using (true)`)

Columns (3): `distillation_id bigint → distillations(id) ON DELETE CASCADE`,
`item_kind text check ∈ {message,resource,distillation}`, `item_id bigint`,
PK `(distillation_id, item_kind, item_id)`.

Polymorphic provenance edge — **no hard FK on `(item_kind, item_id)`** by design
(a cite may point at `message_feed`/`discord_messages`, `external_resources`, or
another `distillation`). `item_id` is `bigint` here (message_id / external_resources.id
/ distillation.id). Live: 33 cite rows across 11 distillations.

The `contribute` function enforces **≥1 cite per distillation** (validated in
`protocol.ts`) and dedupes cites on `(item_kind, item_id)`. `get_item` resolves
cites both directions: a distillation's `cites`, and the `cited_by` distillations
for a given message/resource (via `distillation_cites` then `unified_feed`).

---

## 5. Deletion and update paths

### Message deletion (soft delete) — **live gap**

Discord messages are **soft-deleted**: `discord_messages.is_deleted` (default
`false`) and `deleted_at timestamptz`. There are **6,987 `is_deleted=true`
messages** vs 1,245,006 `is_deleted=false` (total ≈ 1,251,991) on 2026-07-28.

**The search surface does not filter them.** `message_feed` (→ `unified_feed` →
`hivemind.search`) has no `is_deleted` predicate, so those 6,987 deleted messages
are currently retrievable. Sibling views `recent_messages` and `message_stats`
*do* filter `is_deleted = false` — so the omission is inconsistent, not
intentional. **Phase 1's lexical message arm must add `is_deleted = false`** (the
plan's SQL test "Deleted, opted-out, or otherwise ineligible rows cannot rank"
will fail against the current read path until it does). Because the service role
bypasses RLS, the hardened search RPC must encode this predicate itself; it cannot
inherit it.

### Message update (edit) path

`discord_messages.edited_at`, `edit_history jsonb`, and `synced_at`/`indexed_at`
show edits and re-syncs are tracked. The Discord ingestion job (external; writes
via `enforce_writable_server_guild()`) upserts on `message_id`, so an edit changes
the same row — search backfill change-detection keys on content hash, not row
identity, which is correct here.

### Distillation deletion / supersession

Distillations are **never edited in place** (`DESIGN.md` "supersede, don't edit"):
a new row with `supersedes_id` replaces an old one, and the old row's `status` is
manually set to `superseded` (no trigger automates it — deferred work). There is
no hard delete path in the contract; `distillation_cites` cascades if a
distillation row is deleted. The semantic-index lifecycle (plan task 2.10) must
treat a `pending→rejected` or `→superseded` transition as "drop this item's
vectors" because the feed stops surfacing it.

### Resource deletion

`external_resources` has no soft-delete column and no delete path in the contract;
resources are immutable rows upserted on `(source, external_id)`. Re-ingestion
replaces via the unique constraint (the edge functions return 409 on duplicate).
A resource removal would be a direct-SQL/operator action; the index lifecycle must
treat a vanished `external_resources.id` as a drop.

---

## 6. Author opt-outs — **live gap**

There is **no opt-out column on `discord_messages`** and **no opt-out predicate in
any read view**. Two related signals exist but are unwired to search:

1. `members.allow_content_sharing` (boolean, default true) — **4 of 7,672 members
   have `allow_content_sharing=false`** on 2026-07-28. `message_feed`/`unified_feed`
   do not reference it, so those authors' messages are still in the public search
   surface.
2. `discord_channels.sharing_enabled` / `members.include_in_updates` — channel/member
   consent flags, also not referenced by the read path.

`README.md` and `skill/SKILL.md` state that **opted-out authors are excluded only
in the Hugging Face dataset export**, not in the live endpoint. So today "opt-out"
is an **export-time** concept, not a live-read concept. The plan repeatedly says
"preserve all current public-corpus eligibility and opt-out rules" — but there is
effectively **no live opt-out rule to preserve**. Whether `allow_content_sharing=false`
(or any future author opt-out) must also exclude from live search is a **policy
decision** the search project must make explicit (see [Unresolved gaps](#unresolved-gaps-and-decisions-for-later-tasks)),
because the hardened RPC will be the place an opt-out predicate would finally be
enforced.

---

## 7. RLS, policies, and grants

### Row-level security

RLS is **enabled** on every corpus table; `force` is off everywhere (so table
owners would normally bypass — but the service role is what matters).

| Table | RLS | Policy | `using` |
|---|:-:|---|---|
| `discord_messages` | ✓ | "Allow public read access to messages" | `true` |
| `discord_channels` | ✓ | "Allow public read access to channels" | `true` |
| `discord_reactions` | ✓ | "Allow public read access to reactions" | `true` |
| `members` | ✓ | "Allow public read access to members" | `true` |
| `external_resources` | ✓ | "public read" | `true` |
| `distillation_cites` | ✓ | "public read" | `true` |
| `distillations` | ✓ | "public read" | `status <> 'rejected'` |
| `contributors` | ✓ | **(none)** | deny-all (key hashes protected) |
| `vibecomfy_ratings` | ✓ | "vibecomfy_ratings_public_read" | `true` |
| `discord_reaction_log` | ✗ | — | operator-only (RLS off) |

All SELECT policies are granted to **`roles = ['public']`** (which `anon` and
`authenticated` inherit). Views (`message_feed`, `unified_feed`, `recent_messages`,
`message_stats`) have RLS off; `unified_feed` is `security_invoker = true`, so the
underlying-table policies re-apply through it (this is how rejected distillations
stay excluded from the feed).

### Grants (decoded `relacl`, 2026-07-28)

Every corpus table (including `contributors` and the views) carries the **full
privilege set** `{SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER}`
granted to **`anon`, `authenticated`, `service_role`, and `postgres`**
(`grantable=false` throughout). Implications:

- **The grant model is permissive; RLS is the actual row gate.** `contributors`
  is fully granted to `anon` yet invisible because RLS has zero policies.
- The repo (`001`) only documents `grant select on unified_feed to anon,
  authenticated`. Live, the underlying tables are independently grantable to
  `anon` too (the broader app's migrations did this), so the public PostgREST
  surface exposes `discord_messages`, `external_resources`, `distillations`, etc.
  **directly**, not only via `unified_feed`. The Hivemind pack chooses to use
  `unified_feed`; the hardened search RPC must not assume clients can't reach the
  base tables.
- **`service_role` bypasses RLS** (BYPASSRLS). The search RPC will run as service
  role, so its eligibility predicates are the only thing preventing "return every
  row." This is the core reason the plan mandates a SECURITY DEFINER RPC with
  encoded eligibility (see [Security](#8-security-definer-functions-and-trust-boundary)).

---

## 8. SECURITY DEFINER functions and trust boundary

`check_duplicate_distillation` is **not** SECURITY DEFINER. But `public` already
contains **47 SECURITY DEFINER functions** (the broader app), so the hardened-RPC
pattern is an established precedent in this project, e.g.:

- `get_message_for_refresh(p_message_id text)` — SECURITY DEFINER; the DB helper
  behind the deployed `refresh-media-urls` edge function (refreshes expiring
  Discord CDN attachment URLs back into `discord_messages.attachments`).
- `get_entry_feedback(p_entry_id uuid)` — SECURITY DEFINER, `search_path=public`.
- `mark_approval_dirty_for_media(...)`, `set_primary_media(...)` — SECURITY DEFINER
  with explicit `search_path` (one set to `public`, one to `""`).

Lessons for the Phase-1/Phase-3 search RPC (plan tasks 1.9 / 3.3 / 3.7): the
project already ships `SECURITY DEFINER` + explicit `search_path` + service-role
execution, so the plan's requirements (fully-qualify relations, fixed trusted
`search_path`, `revoke` from `PUBLIC`/`anon`/`authenticated`, grant only to the
service-role path, encode eligibility inside the function, bound time/candidates/
results in SQL) are consistent with existing conventions — not a new pattern.

No triggers exist on `external_resources`, `distillations`, `distillation_cites`,
or `vibecomfy_ratings`. The only corpus-adjacent trigger is the
`discord_messages` write guard. So the plan's incremental embedding triggers
(task 2.7) will be the first triggers on those tables.

---

## 9. Ingestion paths

| Surface | Mechanism | Auth / role | Writes to |
|---|---|---|---|
| Contribute a resource or cited distillation | `supabase/functions/contribute` (Deno) | `X-Contributor-Key: hm_<64hex>` → `contributors.api_key_hash` (sha256 of full key), `revoked_at IS NULL`; service role | `external_resources`, `distillations` (forced `pending`), `distillation_cites` (≥1, deduped) |
| Public resource ingest (workflows) | `supabase/functions/contribute-resource` (Deno) | publishable key; `ALLOWED_SOURCES` allowlist (default `vibecomfy`,`vibecomfy-external`); annotates `metadata._submitted_from_{ip,user_agent},_submitted_at`; service role | `external_resources` |
| VibeComfy rating | `supabase/functions/submit-vibecomfy-rating` (Deno) | contributor-key hash; service role | `vibecomfy_ratings` |
| Workflow-semantics enrichment (operator) | `scripts/backfill_workflow_semantics.py` | anon read; `--apply` needs service role | `external_resources.metadata.workflow_semantics`, `.body` |
| Discord message sync (ingestion) | **external** job (not in this repo); `discord_messages` trigger `enforce_writable_server_guild()` guards writes | operator/service | `discord_messages`, `discord_channels`, `discord_reactions`, `members` (upsert on ids; `synced_at`/`indexed_at`) |
| Discord media URL refresh | `refresh-media-urls` edge function (**deployed; source NOT in this repo**) + `get_message_for_refresh()` SECURITY DEFINER | publishable key | `discord_messages.attachments` |
| Client ingestion helpers | `executors/ingest_{article,workflow,youtube}` | contributor key → `contribute` | (via contribute) |

Repo references for these conventions (plan "Existing systems"): `schema/001_unified_corpus.sql`
(DDL), `supabase/functions/contribute/` (service-role use, request validation,
secret handling), `scripts/backfill_workflow_semantics.py` (paged dry-run/apply,
service-role updates, sampling, reporting), `tests/`.

`pg_cron` and `pg_net` extensions are installed — relevant because the plan's
incremental embedder (task 2.9) is a bounded Edge Function **scheduled by Supabase
Cron**; the scheduling substrate already exists.

---

## Repo vs. live drift

| # | Area | Repo says | Live is | Impact |
|---|---|---|---|---|
| D1 | `message_feed` | treated as a pre-existing RLS-enabled table feeding `unified_feed` | a **view** (RLS off) over `discord_messages` | Lexical search must target `discord_messages`, not `message_feed`. Plan AD-3 already says "underlying table" — that table is `discord_messages`. |
| D2 | Discord cluster | not modeled in repo schema | full cluster: `discord_messages`, `discord_channels`, `discord_reactions`, `discord_reaction_log`, `members` | Channel/author filters and hydration join `discord_channels`/`members`; these are the real filter tables. |
| D3 | `public` scope | implies a Hivemind-only schema | **537 relations**; shared with the larger Banodoco/Astrid app | Search RPC must be explicitly scoped to corpus tables; never scan `public` broadly. |
| D4 | Grants | `grant select on unified_feed to anon, authenticated` only | **all privileges** on all corpus tables to `anon`/`authenticated`/`service_role` | RLS (not grants) is the gate; base tables are directly anon-readable. RPC must self-enforce eligibility. |
| D5 | Deletion | not modeled | `discord_messages.is_deleted`/`deleted_at` exist; **`message_feed` does not filter them** → 6,987 deleted msgs searchable | Phase 1 must add `is_deleted=false`. (Sibling views already do.) |
| D6 | Author opt-out | "opted-out authors excluded" (HF export only) | `members.allow_content_sharing` (4=false) exists; **no read-path predicate** | No live opt-out rule to "preserve"; policy decision needed. |
| D7 | `refresh-media-urls` | implied deployed | deployed, but **source not in this repo** (DB helper `get_message_for_refresh()` exists) | Refresh writes to `discord_messages.attachments`; confirm repo ownership if search must reason about attachment URLs. |
| D8 | Bots/system authors | not addressed | 19 `bot`, 2 `system` members; `message_feed` does not exclude them | Decide bot-message handling in search (no current filter). |
| D9 | Extensions | `pg_trgm` | `pg_trgm` ✓, plus `pg_cron`,`pg_net`,`pgcrypto`,`pgsodium`,`supabase_vault`,`uuid-ossp`; **`vector` ABSENT** | `vector` enablement remains task 2.2 (unchanged). pg_cron available for the embedder schedule. |
| D10 | `unified_feed`, `distillations`, `distillation_cites`, `external_resources`, `contributors`, `vibecomfy_ratings` shape | repo 001/002 | **matches live exactly** (columns, checks, FKs, the view text) | No contract drift on the corpus shape — the plan's `unified_feed` hydration contract is accurate. |

---

## Self-review against plan invariants

- **AD-1 (client contract; kinds/sources/item_ids/mode).** `unified_feed` carries
  `kind/source/item_id`; resource concrete kinds (workflow/transcript/…) appear as
  `kind` in the feed. `kind=workflow` maps to `external_resources.kind='workflow'`.
  `item_ids` are text at the API. **Covered.**
- **AD-2 (one shared embedding index; no per-table vector cols).** No vector
  columns exist anywhere; `vector` extension absent (task 2.2). The shared
  `content_embeddings` table is greenfield. **Covered.**
- **AD-3 (search underlying indexed tables; hydrate after).** Underlying message
  table = `discord_messages`; its GIN index is `to_tsvector('english', content)`.
  Resources/distillations have **no** `tsvector` index yet (task 1.2). Hydration
  via `unified_feed` is confirmed accurate. **Covered, with the english-config
  fact recorded.**
- **AD-4 (canonical reps explicit/dedup/versioned).** `payload.python_source` is
  authoritative; body delimiters and `metadata.workflow_semantics` confirmed;
  bounded sample shows `payload_python` is the minority cohort and `corpus_path`
  near-universal — exactly the remediation shape task 0.8 must freeze. **Covered
  (exact cohort counts deferred to 0.3/0.8).**
- **AD-6 (conservative source/status weights).** Approved-distillation boost has
  **0 approved rows** to act on today; eval must not assume approved coverage.
  **Flagged.**
- **Security: "deleted/opted-out/ineligible rows cannot rank"; service-role bypass
  must not return every row.** Today the read path **does** surface deleted rows
  and enforces **no** opt-out; service_role bypasses RLS. The hardened RPC must
  encode: `discord_messages.is_deleted=false`, distillation `status∈{pending,approved}`,
  resource (no status gate), and an explicit opt-out/bot decision. **Covered as a
  required Phase-1/3 input, with current-state gaps named.**
- **Snowflakes as strings at JSON boundaries.** Holds at the API
  (`unified_feed.item_id::text`); **does not** hold for `distillation_cites.item_id`,
  `get_item --id`, or the stored `bigint` ids. **Flagged.**
- **"Existing Hivemind Supabase project remains the only database."** Confirmed —
  single project, additive-only plan. No new DB touched in this task. **Met.**

---

## Unresolved gaps and decisions for later tasks

These are **named, not solved** (per the task brief). None blocks the 0.2
completion signal; each is an input to a later task.

1. **Soft-delete enforcement (D5).** `message_feed` omits `is_deleted`; 6,987
   deleted messages are searchable. Phase 1 (task 1.7/1.8/1.9) must filter
   `is_deleted=false` on `discord_messages`. *Decision for 1.1:* search
   `discord_messages` directly, not the `message_feed` view.
2. **Author opt-out semantics (D6).** `allow_content_sharing` (and dataset-export
   opt-outs) are not live-read-enforced. *Policy decision (0.8 / Phase 1):* does
   opt-out for the HF dataset also mean exclusion from live search? If yes, the
   RPC needs a `members`-join eligibility predicate; if no, document the
   divergence. Today: **no live opt-out rule exists to preserve.**
3. **Bot/system messages (D8).** 19 bots, 2 system members; no filter. *Decision
   (Phase 1):* include/exclude bot-authored messages from search.
4. **FTS config (english vs simple) — task 1.1.** The live index is `english`.
   The plan warns a `simple` query will not use an `english` index. 1.1 must
   choose one canonical config and either reuse this index (`english`) or build a
   new `simple` one. The map records the current expression; the choice is 1.1's.
5. **Workflow representation cohort counts — tasks 0.3 / 0.8.** Bounded sample
   only here (19/200 `payload_python`, 199/200 `corpus_path`). Exact counts,
   body-vs-payload duplication rate, and per-cohort sizes are 0.3's measurement
   and 0.8's freeze.
6. **Snowflake string boundary.** `distillation_cites.item_id` and `get_item --id`
   are `bigint`. Plan wants strings everywhere; a snowflake cite id or message id
   near/above 2^53 would round as a JSON number. *Decision (2.4 / Phase 4):*
   stringify cite/message ids through the cite and `get_item` contracts.
7. **`refresh-media-urls` source ownership (D7).** Deployed function source is
   not in this repo; it mutates `discord_messages.attachments` via
   `get_message_for_refresh()`. Confirm whether Hivemind owns/maintains it before
   search depends on attachment-URL freshness (the `refresh_media` executor calls
   it).
8. **Message total count probe.** Unfiltered `count=exact` on 1.25M returned no
   total (PostgREST count cap/timeout); use `reltuples=1,248,240` or the
   `is_deleted` sum `1,251,991` as the headline. Cosmetic, not a blocker.
9. **Data-quality anomalies.** One `external_resources` row with `source='s'` and
   one with `source='comfyui'` outside the expected namespaces; one non-workflow/
   non-transcript resource kind. Minor; flag for ingestion validation.

---

## Reproducibility and tests

- **Tool:** `scripts/inventory_schema.py` — re-runs the read-only catalog pass
  (via the task-0.1 session-mode credential) and the public eligibility pass
  (publishable key, `count=exact`). Reuses `verify_access.redact` and
  `verify_access.parse_dryrun_pg_env`; the credential never leaves a child env.
  `--no-db` skips the catalog for offline runs; `--json PATH` emits structured
  output; default prints the redacted human summary.
- **Safety:** `tests/test_inventory_schema.py` pins (a) the inventory SQL is
  strictly read-only (every statement is a `SELECT`; no DML/DDL/GRANT/`SET
  search_path`), (b) all subprocess output is redacted (never leaks a credential),
  (c) PROBE-line parsing tolerates psql errors, (d) the derived analysis
  (opt-out/delete-column discovery, missing-table detection, SECURITY DEFINER
  detection, FTS-config extraction), and (e) the public count helper parses
  PostgREST `Content-Range` and degrades safely on failure.
- **Result:** `python3 -m unittest tests.test_inventory_schema tests.test_verify_access`
  → 34 tests pass. Live run exit 0.
- **Boundary:** no database object was created, altered, or dropped; no function
  deployed; no secret set, rotated, printed, or committed; no production data
  modified. The dirty Hivemind working tree (pre-existing edits to `.astridignore`,
  `executors/_common.py`, `pack.yaml`, `skill/SKILL.md`, `supabase/.temp/cli-latest`,
  `tests/test_common.py`, and the untracked VibeComfy-rating/schema/`submit-vibecomfy-rating`
  artifacts) was preserved untouched.

---

## Completion signal (0.2)

> A reviewed schema/eligibility map covers messages, resources, distillations, and citations.

**Met.** This document covers, with live 2026-07-28 evidence: messages
(`discord_messages` + `message_feed` + the `english` GIN FTS index + deletion/edit
state + channel/author envelope), external resources including workflows (table,
`payload`/`metadata` representation contract, bounded cohort signal, indexes),
distillations (lifecycle, RLS, duplicate RPC, indexes), and citations (polymorphic,
cascade). It also covers eligibility/approval/pending rules, author opt-outs,
deletion/update paths, public hydration, RLS/policies/grants, SECURITY DEFINER
functions and the trust boundary, ingestion paths, repo-vs-live drift, a
plan-invariant self-review, and named unresolved gaps. Reproducibility tooling and
tests follow repo conventions.

## Dependency-safe next tasks

Task 0.2 has no blockers. Per the plan's dependency graph, the tasks this map
unblocks (do **not** start here — this task stops at 0.2):

- **0.3** — Measure row counts, text/token length distributions, long-resource
  distribution, workflow prose/Python sizes and representation coverage, and index
  sizes. (Depends on 0.1; uses the same read-only access; this map's structural
  inventory and bounded workflow signal feed its measurement plan.)
- **0.4** — Capture current `ILIKE` relevance/latency/timeout/zero-result/doubled-limit
  baseline. (**Depends on 0.2** — now unblocked.)
- **0.5** — Build the golden-set schema/adapters/metrics/comparison-report generator
  by porting Pumpernickel's evaluation structure. (**Depends on 0.2** — now unblocked.)
- **0.8** — Inventory workflow representation cohorts and freeze the Python
  precedence/delimiters/recovery/quarantine/no-duplication/embedding/chunking
  contracts. (Depends on 0.2–0.7; this map's D5/D6/D8 gaps and workflow-cohort
  signal are explicit inputs.)

Do **not** begin 0.4, 0.5, or any Phase 1 implementation from this task.
