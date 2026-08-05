-- ============================================================
-- Hivemind — Phase 1 / Task 1.3 — Canonical Discord message FTS index
-- ============================================================
--
-- Additive, idempotent migration. Adds ONE new GIN expression index on the
-- underlying `public.discord_messages` table using the canonical frozen lexical
-- configuration ('simple'), so the lexical candidate path (plan AD-3, tasks
-- 1.7–1.9) can use an indexed expression that actually matches its query.
--
-- Frozen indexed expression (lexical-contract.md §3, executors/lexical_contract.py):
--
--     to_tsvector('simple'::regconfig, coalesce(content, ''))
--
-- This is the EXACT expression the eventual candidate query must use too.
-- PostgreSQL expression-index matching requires an *identical* expression
-- (lexical-contract.md §1.1): the live `english` index
-- `idx_discord_messages_content_fts` (to_tsvector('english', content), 85 MB)
-- CANNOT serve a `simple` query and is therefore NOT a substitute. This
-- migration neither drops nor depends on that index; it is retained until the
-- task 1.11 gate (additive storage decision; 85 MB is negligible inside the
-- 12 GB envelope, phase0-capacity.md §3).
--
-- HOW TO APPLY (operator, not `supabase db push`):
--   * `CREATE INDEX CONCURRENTLY` cannot run inside a transaction block, so this
--     file is applied via a session-mode / direct psql connection with autocommit
--     (task 0.1 access path: scripts/live_discord_fts.py), NOT via the
--     transactional `db push` migration wrapper. `psql -f` does NOT wrap in a
--     transaction by default, so each statement below autocommits independently.
--   * The driver sets a bounded `lock_timeout` before the build so a transient
--     ACCESS EXCLUSIVE conflict fails fast instead of wedging writers.
--   * It builds online: it takes only brief SHARE locks at start/end, never
--     blocks SELECT/INSERT/UPDATE for the build's duration, and never mutates a
--     source row.
--
-- IDEMPOTENCE:
--   * `CREATE INDEX CONCURRENTLY IF NOT EXISTS` makes a re-run a safe no-op when
--     a *valid* index of this name already exists.
--   * KNOWN LIMITATION: if a *previous concurrent build failed* and left an
--     INVALID index of this name behind, `IF NOT EXISTS` will skip and the
--     invalid remnant will remain. The preflight driver
--     (scripts/discord_message_fts.py `preflight_queries`) detects
--     `pg_index.indisvalid = false` remnants and the recovery is the rollback
--     command below, then a fresh build.
--
-- ROLLBACK (one line, online, outside a transaction):
--   DROP INDEX CONCURRENTLY IF EXISTS public.idx_discord_messages_content_fts_simple;
--   (Also exposed as `scripts/live_discord_fts.py --rollback`.)
-- ============================================================

-- ---------------------------------------------------------------------------
-- 1. Guard: confirm we are building against the intended object, not a drift.
--    Read-only catalog check; raises a clear error if the table/column moved.
--    Runs in its own autocommit statement (no transaction with the CIC below).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    has_table  boolean;
    has_col    boolean;
    col_type   text;
    col_nullable boolean;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = 'discord_messages' AND c.relkind = 'r'
    ) INTO has_table;

    IF NOT has_table THEN
        RAISE EXCEPTION 'preflight guard: public.discord_messages not found — refusing to build';
    END IF;

    SELECT a.atttypid::regtype::text, a.attnotnull
      INTO col_type, col_nullable
      FROM pg_catalog.pg_attribute a
      JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relname = 'discord_messages'
       AND a.attname = 'content' AND NOT a.attisdropped;

    has_col := FOUND;
    IF NOT has_col THEN
        RAISE EXCEPTION 'preflight guard: public.discord_messages.content not found — refusing to build';
    END IF;
    IF col_type <> 'text' THEN
        RAISE EXCEPTION 'preflight guard: content is %, expected text — refusing to build', col_type;
    END IF;
    -- content is nullable in the live schema (attachments-only messages); the
    -- frozen expression coalesces it to '' so this is expected, not a drift.
END $$;

-- ---------------------------------------------------------------------------
-- 2. The canonical index (online, additive, idempotent).
--    Name is frozen so preflight / rollback / the 1.7 candidate SQL reference it.
-- ---------------------------------------------------------------------------
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_discord_messages_content_fts_simple
    ON public.discord_messages
    USING gin (to_tsvector('simple'::regconfig, coalesce(content, '')));

-- ---------------------------------------------------------------------------
-- 3. Verification (read-only). Prints the live object state an operator signs.
--    indisvalid MUST be true after a successful online build; a false value
--    means the concurrent build did not finish and the rollback is required.
-- ---------------------------------------------------------------------------
SELECT
    n.nspname                                     AS schema_name,
    c.relname                                     AS index_name,
    i.indisvalid                                  AS is_valid,
    i.indisready                                  AS is_ready,
    pg_get_indexdef(i.indexrelid)                 AS index_definition,
    pg_size_pretty(pg_relation_size(i.indexrelid)) AS index_size,
    pg_size_pretty(pg_total_relation_size(c2.oid)) AS table_total_size
FROM pg_catalog.pg_index i
JOIN pg_catalog.pg_class c  ON c.oid = i.indexrelid
JOIN pg_catalog.pg_class c2 ON c2.oid = i.indrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relname = 'idx_discord_messages_content_fts_simple'
  AND c2.relname = 'discord_messages';
