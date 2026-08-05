-- ============================================================
-- Hivemind — Phase 1 / Task 1.6 — Full-message exact-identifier path (CHOSEN)
-- ============================================================
--
-- Additive, idempotent migration. Implements the FROZEN, EVIDENCE-BASED CHOICE
-- from task 1.6 (executors.message_identifier_index.CHOICE =
-- 'normalized_full_message_trigram_length_bounded'): ONE normalized, length-
-- bounded GIN trigram index over discord_messages.content, partial on
-- is_deleted=false. NOT a fanned-out identifier side index.
--
-- The decision is measured, not assumed. The task-1.6 production-shaped
-- rehearsal (~1.25M rows, 2026-07-28) implemented BOTH designs:
--   A (CHOSEN, this migration): normalized full-message trigram GIN — ~0.1 GB,
--     GIN-served queries, bridges separator/case/Unicode variants on BOTH sides,
--     auto-maintained by PostgreSQL (no trigger / side table / backfill), and
--     recovers the >2047-char tokens the task-1.3 FTS index drops.
--   B (REJECTED): normalized identifier side index (side table + trigger +
--     backfill) — measured ~1.5 GB (15x larger), candidate query seq-scan at
--     p50≈2.7s, worse on spaced forms ("FLUX 1" splits to flux+1; a full-body
--     normalize concatenates to flux1), and a ~3.25x trigger write slowdown on
--     the externally-owned hot ingestion table.
-- The plan's "avoid an unbounded trigram index over 1.25M full message bodies"
-- concern is resolved by measurement (Hivemind bodies are short, 0.3: mean≈77
-- chars, so the trigram is ~0.1 GB) AND by construction (the length-bounded
-- partial predicate excludes pathological megabyte bodies). See
-- docs/hybrid-search/phase1-message-identifier-index.md for the full record.
--
-- What this builds (additive, online, idempotent):
--   * idx_discord_messages_identifier_trgm — GIN expression index over the
--     IMMUTABLE hivemind_normalize_identifier(content) (schema/005, task 1.4)
--     with gin_trgm_ops and the frozen partial predicate:
--       is_deleted = false AND char_length(content) BETWEEN 1 AND 8000
--   That is the WHOLE migration: one index. No source row is modified; no
--   trigger, side table, or backfill is needed (PostgreSQL maintains the GIN
--   index automatically; the partial predicate handles soft-delete — a row
--   leaves the index when is_deleted flips true).
--
-- Frozen query contract (the exact-identifier candidate arm, task 1.7). The
-- PRIMARY path is index-supported exact NORMALIZED CONTAINMENT (the normalized
-- query is a substring of the normalized whole body); the permissive <% fuzzy
-- path is an OPTIONAL bounded fallback for typo tolerance only, NOT the primary:
--   WITH q AS (SELECT public.hivemind_normalize_identifier(:q) AS k)
--   SELECT m.message_id::text AS item_id, ...
--     FROM public.discord_messages m, q
--    WHERE m.is_deleted = false
--      AND char_length(m.content) BETWEEN 1 AND 8000          -- repeat the partial predicate
--      AND public.hivemind_normalize_identifier(m.content)
--          LIKE '%' || q.k || '%'                            -- substring of indexed haystack
--    ORDER BY (normalize(m.content) = q.k) DESC, m.created_at DESC, m.message_id::text ASC
--    LIMIT :candidate_limit;
-- Containment (not the v2 whole-body equality) retrieves identifiers EMBEDDED in
-- prose; compact normalization bridges dotted/versioned/hyphenated/filename/
-- symbol/keyword-argument forms on both sides, so one predicate preserves every
-- variant class. The GIN index serves the LIKE because the '||'-built pattern
-- constant-folds to '%needle%' so pg_trgm extracts the needle's trigrams (proven
-- by EXPLAIN on production-shaped data + live evidence). PostgreSQL matches an
-- expression index only against a structurally identical query expression
-- (task-1.1 §1.1), so the candidate SQL MUST use this identical expression +
-- partial predicate or it silently falls back to a seq scan.
--
-- HOW TO APPLY (operator, not `supabase db push`):
--   * CREATE INDEX CONCURRENTLY cannot run inside a transaction block, so this
--     file is applied via a session-mode / direct psql connection with autocommit
--     (scripts/live_message_identifier.py), NOT the transactional `db push`
--     wrapper. psql -f does not wrap in a transaction; each statement autocommits.
--   * The driver sets a bounded lock_timeout before the build so a transient
--     ACCESS EXCLUSIVE conflict fails fast instead of wedging writers, and
--     monitors progress via pg_stat_progress_create_index.
--
-- PREREQUISITE: schema/005 (the IMMUTABLE hivemind_normalize_identifier + the
-- deterministic ICU collation). The guard fails closed if absent; the live
-- driver applies schema/005 safely first when needed.
--
-- IDEMPOTENCE: CREATE INDEX CONCURRENTLY IF NOT EXISTS makes a re-run a safe
-- no-op when a *valid* index of this name exists. KNOWN LIMITATION: if a previous
-- concurrent build failed and left an INVALID index of this name, IF NOT EXISTS
-- skips and the invalid remnant remains; preflight (scripts/live_message_identifier.py)
-- detects pg_index.indisvalid=false and the rollback below + a fresh build recovers.
--
-- ROLLBACK (online, outside a transaction; idempotent):
--   DROP INDEX CONCURRENTLY IF EXISTS public.idx_discord_messages_identifier_trgm;
--   (Also: scripts/live_message_identifier.py --rollback.)
--   No source row is touched; the read path returns to its exact pre-1.6 state.
-- ============================================================

create extension if not exists pg_trgm;

-- ---------------------------------------------------------------------------
-- 1. Guard: schema/005 prerequisite + discord_messages shape (read-only).
--    Each statement autocommits (no transaction with the CIC below).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    has_fn        boolean;
    has_coll      boolean;
    content_type  text;
    isdel_type    text;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.proname = 'hivemind_normalize_identifier'
          AND p.pronargs = 1
    ) INTO has_fn;
    IF NOT has_fn THEN
        RAISE EXCEPTION 'prerequisite guard: public.hivemind_normalize_identifier(text) '
                        'not found — apply schema/005 first (task 1.4 prerequisite)';
    END IF;

    SELECT EXISTS (
        SELECT 1 FROM pg_catalog.pg_collation
        WHERE collnamespace = 'public'::regnamespace AND collname = 'hivemind_unicode'
    ) INTO has_coll;
    IF NOT has_coll THEN
        RAISE EXCEPTION 'prerequisite guard: public.hivemind_unicode collation '
                        'not found — apply schema/005 first (task 1.4 prerequisite)';
    END IF;

    SELECT a.atttypid::regtype::text INTO content_type
      FROM pg_catalog.pg_attribute a
      JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relname = 'discord_messages'
       AND a.attname = 'content' AND NOT a.attisdropped;
    IF content_type IS NULL OR content_type NOT IN ('text') THEN
        RAISE EXCEPTION 'preflight guard: discord_messages.content must be text (got %)', content_type;
    END IF;

    SELECT a.atttypid::regtype::text INTO isdel_type
      FROM pg_catalog.pg_attribute a
      JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relname = 'discord_messages'
       AND a.attname = 'is_deleted' AND NOT a.attisdropped;
    IF isdel_type IS NULL THEN
        RAISE EXCEPTION 'preflight guard: discord_messages.is_deleted not found';
    END IF;
    IF isdel_type NOT IN ('boolean') THEN
        RAISE EXCEPTION 'preflight guard: discord_messages.is_deleted must be boolean (got %)', isdel_type;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. The chosen index: normalized full-message trigram GIN, length-bounded,
--    partial on is_deleted=false. Built CONCURRENTLY (online) + idempotent.
--    Frozen expression + partial predicate (mirrors the reference module).
-- ---------------------------------------------------------------------------
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_discord_messages_identifier_trgm
    ON public.discord_messages
    USING gin (hivemind_normalize_identifier(content) gin_trgm_ops)
    WHERE is_deleted = false
      AND char_length(content) BETWEEN 1 AND 8000;

-- ---------------------------------------------------------------------------
-- 3. Verification (read-only). Prints the live object state an operator signs.
--    indisvalid MUST be true after a successful online build; a false value
--    means the concurrent build did not finish and the rollback is required.
-- ---------------------------------------------------------------------------
SELECT
    c.relname                                       AS index_name,
    i.indisvalid                                    AS is_valid,
    i.indisready                                    AS is_ready,
    pg_get_indexdef(i.indexrelid)                   AS index_definition,
    pg_size_pretty(pg_relation_size(i.indexrelid))  AS index_size,
    pg_size_pretty(pg_total_relation_size(tc.oid))  AS table_total_size
FROM pg_catalog.pg_index i
JOIN pg_catalog.pg_class c  ON c.oid = i.indexrelid
JOIN pg_catalog.pg_class tc ON tc.oid = i.indrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relname = 'idx_discord_messages_identifier_trgm';
