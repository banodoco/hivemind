# Phase 0 — Task 0.1 Access Audit

**Date:** 2026-07-28
**Task:** 0.1 — Confirm existing Hivemind Supabase project/deployment ownership and verify current CLI, database, Edge deployment, secret-store, and session-mode access.
**Plan:** `docs/architecture/hivemind-hybrid-search-plan.md` (Astrid planning copy)
**Reproduce:** `python3 scripts/verify_access.py` (add `--db-probe` for the authenticated DB round-trip). Tests: `python3 -m unittest tests.test_verify_access`.

This audit records **capability status, command names, safe project identifiers,
timestamps, and redacted failure classes only.** No API key, database password,
token, or connection string is recorded anywhere in this file.

## Project identity (confirmed)

| Field | Value |
|---|---|
| Project name | Banodoco (the Hivemind project) |
| Project reference (`ref`) | `ujlwuvkrxlvoswwkerdf` (20 chars) |
| Region | Central EU (Frankfurt) / `eu-central-1` |
| Created (UTC) | 2025-03-12 |
| Ownership | Single Supabase org; repo `supabase` linked to this ref |

The linked project ref in `supabase/.temp/project-ref` matches the ref baked
into the executors (`https://ujlwuvkrxlvoswwkerdf.supabase.co/...`). The CLI's
`projects list` shows this project as the repo's **linked** project. All six
projects in the account belong to one org; only the Banodoco/Hivemind ref is
in scope for this plan.

## Access surfaces

| Capability | Status | Evidence | Command |
|---|---|---|---|
| Supabase CLI installed | ✅ Ready | `v2.75.0` installed (`v2.110.0` is the latest available; `supabase/.temp/cli-latest`) | `supabase --version` |
| Supabase CLI authenticated | ✅ Ready | listed 6 projects under one org | `supabase projects list` |
| Repo linked to intended project | ✅ Ready | linked ref == executor ref == Banodoco | `supabase projects list`, `supabase/.temp/project-ref` |
| Edge Function deployment | ✅ Ready | Hivemind functions ACTIVE: `contribute`, `contribute-resource`, `refresh-media-urls`, `submit-vibecomfy-rating` | `supabase functions list` |
| Secret-store read | ✅ Ready | secret names readable (values never printed) | `supabase secrets list` |
| Secret-store: service-role | ✅ Ready | `SUPABASE_SERVICE_ROLE_KEY` present (name only) | `supabase secrets list` |
| Secret-store: DB URL secret | ✅ Ready | `SUPABASE_DB_URL` present (name only) | `supabase secrets list` |
| Public read path (PostgREST) | ✅ Ready | `unified_feed` returns live corpus rows (`kind=message`, `source=banodoco-discord`); snowflake `item_id`s preserved as strings | `GET .../rest/v1/unified_feed` with publishable key |
| Database session-mode (authenticated) | ✅ Ready | round-trip OK via CLI login role; see below | `scripts/verify_access.py --db-probe` |

## Session-mode database detail

The session-mode path is what the plan requires for migrations and the
operator-run backfill (it must **refuse** the transaction pooler).

- **Endpoint:** Supabase **session pooler**, `*.pooler.supabase.com`, **port
  `5432`** — i.e. session mode, **not** the transaction pooler on `6543` that
  the backfill must reject. `supabase/.temp/pooler-url` is a session-mode
  locator **without an embedded password** (correct: no secret is committed).
- **Authenticated round-trip (probe):** `supabase db dump --dry-run` obtains a
  short-lived `cli_login_postgres.<ref>` login role via the access token. Using
  that credential once through `psql` (libpq env, never printed):
  - `current_database()` = `postgres`
  - `version()` = `PostgreSQL 15.8` (Supabase-hosted)
  - extensions: `pg_trgm` **present** (plan-expected); `vector` **absent**
    (plan-expected — pgvector enablement is task 2.2, not 0.1).
- This probe needs neither the operator's permanent DB password nor Docker.
- `supabase/.temp/postgres-version` records `15.8.1.054`, consistent with the
  live `version()`.

## Outstanding / non-blocking notes

- **`OPENAI_API_KEY` is not yet in the Edge secret store.** This is the plan's
  documented "remaining setup action", performed before the live embedding smoke
  test (task 2.1). It is **not** part of task 0.1's scope or completion signal.
- **Docker Desktop is not running** on this machine, so the CLI's containerised
  `db dump`/`db pull`/`db push` cannot run right now. This affects only
  CLI-managed migration tooling; the `psql`/session-mode backfill path needs no
  Docker. Start Docker before CLI-driven migrations/index builds.
- **CLI version gap:** installed `v2.75.0`; `v2.110.0` available. Updating is
  recommended before later phases but is not required for 0.1.
- **CLI login credential handling:** the `db dump --dry-run` script embeds a
  short-lived `cli_login_postgres` password. The probe captures it to a temp
  file, uses it once in a child-process env, then deletes the file; it is never
  printed or persisted, and `scripts/verify_access.py` routes all output through
  a redactor (covered by `tests/test_verify_access.py`).

## Boundary confirmation

- **No new Supabase project or database was created**, and none was needed —
  the existing Hivemind/Banodoco project is the only database and deployment
  target.
- **Nothing was copied from Pumpernickel.** Pumpernickel was not accessed during
  this task; it remains a read-only retrieval/evaluation reference for later
  phases, per the plan.
- No secret was rotated, set, printed, or committed. The dirty Hivemind working
  tree (pre-existing edits to `pack.yaml`, `executors/_common.py`,
  `skill/SKILL.md`, `tests/test_common.py`, `supabase/.temp/cli-latest`, and the
  untracked VibeComfy-rating/schema artifacts) was preserved untouched.

## Completion signal (0.1)

> The audited access paths work against the intended Hivemind project; no new
> database or project is created and nothing is copied from Pumpernickel.

**Met.** All five audited surfaces (CLI, database public read path, database
session-mode, Edge deployment, secret-store) resolve to the intended Hivemind
project and were exercised with live read-only commands.

## Next dependency-safe tasks

Task 0.1 has no blockers. Per the plan's dependency graph, the next tasks that
become unblocked and use this same verified access are:

- **0.2** — Inventory source tables, views, eligibility rules, opt-outs,
  deletion behavior, RLS, grants, and ingestion paths. (Session-mode DB read
  access confirmed here is the prerequisite.)
- **0.3** — Measure row counts, text/token length distributions, long-resource
  distribution, workflow prose/Python sizes, and current index sizes. (Depends
  on 0.1; uses the same read-only DB access.)

Do **not** start 0.2/0.3 here — this task stops at 0.1.
