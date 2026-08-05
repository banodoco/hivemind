# Phase 1 — Task 1.3 Discord Message FTS Index Build

**Date:** 2026-07-28
**Task:** 1.3 — Build the canonical Discord message FTS index safely on staging and
production-sized data.
**Plan:** `docs/architecture/hivemind-hybrid-search-plan.md` (Astrid planning copy).
**Prereqs:** 0.1 access, 0.3 inventory (~1.25M rows; 85 MB `english` GIN index),
1.1 frozen lexical contract (`'simple'`, message bare source `content`).
**Tests:** `python3 -m unittest tests.test_discord_message_fts`; full suite green.

This task built the canonical `simple` Discord-message FTS index, rehearsed it on
production-sized data in an isolated cluster, preflighted the live Hivemind project
through the task-0.1 safe session-mode access path, performed the live online build
authorized by the user, and captured `EXPLAIN` evidence that representative queries
use it. It created **no RPC, no `message_feed` change, and no source-row mutation**.

## Completion signal

> Online index build completes within storage/lock limits and query plans use it.

**Met.** The live `CREATE INDEX CONCURRENTLY` build completed in **42.1 s**, left a
**valid** 67 MB index, never mutated a source row, and saved `EXPLAIN (ANALYZE,
BUFFERS)` plans in which all three representative `simple` queries
(`websearch_to_tsquery`, `phraseto_tsquery`, dotted name) run a **Bitmap Index Scan
on `idx_discord_messages_content_fts_simple`** (never the `english` index, never a
seq scan) with `is_deleted = false` encoded and Discord snowflakes cast to text.

## Files produced (task-1.3 owned)

| Path | Role |
|---|---|
| `schema/004_discord_message_fts.sql` | **Canonical additive/idempotent migration** — guard DO block + `CREATE INDEX CONCURRENTLY IF NOT EXISTS` + verification SELECT + rollback recipe. Elevation-free (runnable as the owner). |
| `scripts/discord_message_fts.py` | **Pure importable module** — frozen index identity (cross-checked vs `executors.lexical_contract`), build/rollback SQL, preflight query set + verdict logic, evidence queries, EXPLAIN parser, rehearsal schema/seed. |
| `scripts/rehearse_discord_fts.py` | **Isolated-cluster rehearsal harness** — throwaway PG 14 cluster, ~1.25M production-shaped rows, timed online build, sizes, plans, locks/headroom, cancellation/rollback, teardown. |
| `scripts/live_discord_fts.py` | **Live driver** — task-0.1 session-mode preflight + online build (monitored) + evidence + rollback; all output redacted; `--apply` gated on a green preflight. |
| `tests/test_discord_message_fts.py` | **43 tests** — frozen identity, SQL/dry-run shape, schema-file consistency, preflight verdict logic, EXPLAIN parsing, rehearsal+live evidence, redaction, plus PG-gated idempotence & cancellation integration tests. |
| `docs/hybrid-search/phase1-message-fts-rehearsal.json` | Rehearsal evidence (machine-readable). |
| `docs/hybrid-search/phase1-message-fts-live.json` | Live preflight + build + evidence (machine-readable). |
| `docs/hybrid-search/phase1-message-fts-build.md` | **This report.** |

No file owned by task 1.2 (`schema/003_lexical_resource_documents.sql`) or 1.4
(`schema/005_identifier_normalization.sql`) was touched; the dirty working tree and
all concurrent work were preserved.

## Frozen index identity

| Attribute | Value | Source |
|---|---|---|
| Schema / table | `public.discord_messages` (the underlying table, **not** the `message_feed` view) | plan AD-3; 0.2 D1 |
| Column | `content` (`text`, nullable) | 1.1 `MESSAGE_BARE_SOURCE`; 0.2 |
| Config | `'simple'::regconfig` | 1.1 (frozen) |
| Expression | `to_tsvector('simple'::regconfig, coalesce(content, ''))` | 1.1 §3 |
| Index name | `idx_discord_messages_content_fts_simple` | frozen here |
| Superseded (retained) | `idx_discord_messages_content_fts` = `gin(to_tsvector('english', content))`, 85 MB | 0.3; kept until the 1.11 gate |

The `coalesce(content, '')` is part of the frozen §3 expression: `content` is
nullable (attachments-only messages), and a null argument would yield a null
tsvector. **The candidate SQL (task 1.7) must use this identical expression** —
PostgreSQL matches an expression index only against a structurally identical query
expression (1.1 §1.1), so any drift (e.g. omitting `coalesce`) would silently fall
back to a seq scan.

## Rehearsal — production-sized, isolated cluster (throwaway PG 14.15)

`initdb --auth=trust`, temp data dir, **Unix socket only (no network)**, torn down
after capture. 1,250,000 production-shaped rows (content shaped to the 0.3
distribution: p50≈46 / mean≈77 / p99≈660 chars; ~2% null; ~0.56% soft-deleted to
mirror the live 6,987/1,251,991 ratio; sparse golden needles from the 1.1
exact_name/workflow_code families; realistic lexeme diversity via a ~100-word
dictionary + bounded pseudo-unique tokens + version strings).

| Measurement | Value |
|---|---|
| Seed (1.25M rows) | 6.67 s — 1,250,000 rows, 6,944 deleted (0.56%), 25,000 null (2%) |
| **Online `CREATE INDEX CONCURRENTLY` (simple)** | **6.39 s**, `indisvalid=t` |
| `english` index build (for comparison) | 5.56 s |
| simple index size | 27.2 MB (synthetic vocab → **conservative lower bound**; see Notes) |
| `english` index size | 27.2 MB |
| `discord_messages` total | 228 MB |
| **Baseline plan (no simple index)** | **Seq Scan**, does **not** use the `english` index (expression mismatch) — 37.99 ms |
| **Evidence plans** | all 3 representative `simple` queries → Bitmap Index Scan on the simple index; none use `english`; none seq scan |
| **Cancellation/rollback** | cancelled CIC → `indisvalid=f` (invalid remnant); `DROP INDEX CONCURRENTLY` → removed (0 remain); rebuild → valid (t), 5.87 s |

Rehearsal verdict: **all_pass = True** (online build valid; evidence uses simple
index; evidence does not use english; baseline cannot use english; cancellation
leaves an invalid index; rollback removes it; rebuild is valid).

## Live preflight (task-0.1 session-mode access, read-only)

Derived the short-lived CLI-login libpq env from `supabase db dump --dry-run`
(credential held only in child-process memory, never printed). **Verdict: GREEN.**

| Gate | Result |
|---|---|
| table/column identity | `content` is `text`, nullable ✓ |
| existing FTS indexes | `idx_discord_messages_content_fts` (`english`) present; target absent ✓ |
| invalid index remnants | none ✓ |
| in-progress index builds | none ✓ |
| blocking long transactions | none ✓ |
| relation locks on `discord_messages` | none ✓ |
| storage headroom | db≈2.26 GB, disk 8 GiB, **free≈6.33 GB**, need≈0.33 GB (idx est≈109 MB ×3) ✓ |
| settings | `lock_timeout=0`, `statement_timeout=120s`, `maintenance_work_mem=128MB` (the build sets its own bounds) |
| connection mode | **session / port 5432** (Supavisor session mode — the 0.1-validated path; transaction pooler 6543 would block) ✓ |
| est. rows | 1,250,376 |

## Live build (authorized; `CREATE INDEX CONCURRENTLY`, monitored)

Preflight green → applied `SET ROLE postgres; SET lock_timeout='30s'; SET
statement_timeout='1800s'; CREATE INDEX CONCURRENTLY IF NOT EXISTS …` **outside any
transaction**, progress-monitored via `pg_stat_progress_create_index`, no
source-row mutation. (See *Findings* for the `SET ROLE` rationale.)

| Measurement | Value |
|---|---|
| Status | **ok**, rc=0 |
| Elapsed | **42.08 s** (7 progress samples) |
| `indisvalid` | **t** |
| Index size | **67 MB** (70,033,408 bytes) |
| Notice during build | `DETAIL: Words longer than 2047 characters are ignored.` (see Notes) |

### Live EXPLAIN evidence (representative `simple` queries)

All three plans (`websearch_to_tsquery('simple','WanVideoSampler')`,
`phraseto_tsquery('simple','controlnet settings')`,
`websearch_to_tsquery('simple','FLUX.1')`) run a **Bitmap Index Scan on
`idx_discord_messages_content_fts_simple`**, never the `english` index. Example:

```
Limit (actual rows=20)
  -> Gather Merge
     -> Sort  Sort Key: (ts_rank(to_tsvector('simple', coalesce(content,'')), '''wanvideosampler'''::tsquery, 32)) DESC, created_at DESC, (message_id)::text
        -> Parallel Bitmap Heap Scan on discord_messages m
              Filter: (NOT is_deleted)                       -- is_deleted=false encoded
              -> Bitmap Index Scan on idx_discord_messages_content_fts_simple (rows=116)
Execution Time: 10.722 ms
```

Representative hit counts (with `is_deleted = false`): `WanVideoSampler`=116,
`controlnet settings`=67, `FLUX.1`=112 — snowflakes returned as text
(`message_id::text AS item_id`).

## Exact live object state (post-build)

| Index | Schema.Table | Expression | valid | owner | size |
|---|---|---|---|---|---|
| `idx_discord_messages_content_fts` *(retained)* | `public.discord_messages` | `gin(to_tsvector('english'::regconfig, content))` | t | postgres | **85 MB** |
| `idx_discord_messages_content_fts_simple` *(new, this task)* | `public.discord_messages` | `gin(to_tsvector('simple'::regconfig, coalesce(content, ''::text)))` | t | postgres | **67 MB** |

`discord_messages`: total 1221 MB (table 662 MB + all indexes 555 MB). The two FTS
indexes together add 152 MB — comfortably inside the 12 GB capacity envelope (0.7).

## Rollback readiness

One line, online, outside a transaction (idempotent — rehearsed in the cancellation
path and proven via the live `--rollback` path; `IF EXISTS` makes re-runs safe):

```sql
DROP INDEX CONCURRENTLY IF EXISTS public.idx_discord_messages_content_fts_simple;
```

```
python3 scripts/live_discord_fts.py --rollback     # does SET ROLE postgres; DROP INDEX CONCURRENTLY IF EXISTS ...
```

The `english` index is retained throughout, so rolling back the simple index
returns the read path to the **exact pre-1.3 state** with zero source-data effect.

## Security / redaction

Every human-facing line from the live driver and rehearsal harness is routed
through the task-0.1 `redact()` boundary (reused verbatim from
`scripts/verify_access.py`): connection strings, JWTs, Supabase keys, contributor
keys, `PGPASSWORD=…`, and 32+ char opaque tokens are masked. The CLI-login
credential lives only in child-process env for the duration of the psql calls.
No credential or connection string is printed, logged, or committed.

## Findings and notes

1. **`SET ROLE postgres` elevation is required for DDL + evidence.** The CLI login
   role (`cli_login_postgres`) is a **member of `postgres`** (the table owner;
   `pg_has_role(…,'MEMBER')=t`) but the `CREATE INDEX` ownership check is **not**
   satisfied by inherited privileges (`must be owner of table discord_messages`),
   and the role has no direct `SELECT` on the externally-owned table. The live
   driver therefore elevates with `SET ROLE postgres` for build/drop/evidence
   (the canonical migration `schema/004` stays elevation-free so it is runnable as
   the owner, e.g. from the SQL editor). This mirrors how the 1.7 candidate RPC
   will run as the service role (bypassing RLS, with `is_deleted` encoded in SQL),
   and was **proven locally** (member-role `SET ROLE postgres; CREATE INDEX
   CONCURRENTLY` → valid; `DROP INDEX CONCURRENTLY` → clean). Not a blocker.
2. **GIN "Words longer than 2047 characters are ignored."** A `DETAIL` notice
   during the live build: the FTS tokenizer skips individual tokens longer than
   2047 chars (a handful of extreme messages with very long unbroken strings).
   Normal PostgreSQL behavior; does not affect normal tokens. Flagged for the
   exact-identifier arm (1.4–1.6): very long tokens are not FTS-discoverable and
   belong on the trigram/side-index path.
3. **Rehearsal index size is a conservative lower bound.** Synthetic content
   under-represents real Discord lexeme diversity, so the rehearsal simple index
   (27 MB) is smaller than the live one (67 MB). The **live measurement is
   authoritative**; the rehearsal's value is proving mechanics and scale (1.25M
   rows, online build, locks, cancellation/rollback, index usage), not the byte
   size. The 0.7 capacity estimate (~109 MB) over-estimated; actual 67 MB is even
   more comfortable.
4. **Connection mode.** The CLI-derived env is `pooler.supabase.com:5432` =
   **Supavisor session mode** (pinned backend), which is the path the 0.1 audit
   validated and is suitable for a multi-statement `CREATE INDEX CONCURRENTLY`.
   The hard blocker would be the **transaction pooler (6543)**; the preflight
   blocks it.

## Reproduce

```bash
python3 scripts/live_discord_fts.py --preflight        # read-only preflight
python3 scripts/live_discord_fts.py --dry-run          # SQL it would run + preflight
python3 scripts/live_discord_fts.py --evidence         # capture plans (index must exist)
python3 scripts/live_discord_fts.py --apply            # build (only if preflight green)
python3 scripts/live_discord_fts.py --rollback         # drop
python3 scripts/rehearse_discord_fts.py --rows 1250000 # isolated rehearsal
python3 -m unittest tests.test_discord_message_fts -v  # this task's tests
python3 -m unittest discover tests/                    # whole repo (766 green)
```

## Next tasks (dependency-safe — do not start here)

Task 1.3 unblocks the lexical-candidate SQL that consumes this index:

- **1.7** — Lexical candidate SQL. **Must use the identical indexed expression**
  `to_tsvector('simple'::regconfig, coalesce(content,''))`, encode `is_deleted =
  false`, and cast snowflakes to text; combine fts/phrase/ident/code-fragment arms
  with the frozen collapse + tie-break.
- **1.5 / 1.6** — Trigram indexes and the full-message exact-identifier path
  (very-long tokens flagged in finding 2 belong here).
- **1.2 / 1.4** — Resource/distillation lexical docs and identifier normalization
  (concurrent owners of `schema/003` / `schema/005`).
- **1.11** — Gate: decides whether the superseded `english` index (85 MB) is
  dropped now that the canonical `simple` index serves the query path.

## Boundary

The only live mutation was the additive, online creation of
`idx_discord_messages_content_fts_simple` (and its read-only verification). No
source row was changed, no RPC or `message_feed`/`unified_feed` change was made,
no `english` index was dropped, no resource/distillation/function was touched, no
pgvector was enabled, and no provider was called. The dirty Hivemind working tree
and all concurrent task work were preserved.
