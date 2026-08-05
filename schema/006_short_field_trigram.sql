-- ============================================================
-- Hivemind — Phase 1 / Task 1.5 — Bounded short-field trigram indexes
-- ============================================================
--
-- Additive, idempotent migration. Adds TWO new GIN expression indexes, one each
-- on the bounded, high-value SHORT fields named by the frozen lexical contract
-- (lexical-contract.md §5): ``external_resources.title`` and
-- ``distillations.question``. Each is built over the FROZEN compact identifier
-- form ``public.hivemind_normalize_identifier`` (IMMUTABLE; schema/005 task 1.4)
-- with the ``gin_trgm_ops`` operator class, so the exact-identifier candidate
-- arm (plan 1.7) can run a normalized ``%`` / ``<%`` similarity path that
-- collapses separator/case/Unicode variants (``Wan 2.2`` / ``Wan2.2`` /
-- ``wan_2.2`` → ``wan22``) which FTS misses.
--
-- Frozen indexed expressions (task 1.5; mirrors lexical-contract.md §5 +
-- executors.identifier_normalization.normalize_identifier):
--
--     hivemind_normalize_identifier(external_resources.title)
--     hivemind_normalize_identifier(distillations.question)
--
-- These are the EXACT expressions the eventual candidate query must use too.
-- PostgreSQL expression-index matching requires an identical expression, so the
-- 1.7 candidate SQL MUST normalize the query the same way
-- (``hivemind_normalize_identifier(:q) % hivemind_normalize_identifier(title)``).
--
-- Partial predicates (frozen, repeated verbatim by the candidate query):
--   * title:   non-empty + length-bounded normalized form (no status column on
--              resources; 0.2 §5: all rows eligible). Inventory title max=92 →
--              300 is a ~3× defensive ceiling + overlong protection.
--   * question: status IN ('pending','approved')  (frozen eligibility §8 — so a
--              rejected/superseded distillation can NEVER surface via this arm)
--              AND non-empty + length-bounded normalized form.
--
-- Scope (this task does NOT): trigram-index large message/resource bodies
-- (explicitly out; bodies stay on the raw schema/001 gin_trgm_ops index and the
-- lexical tsvectors); the full-message exact-identifier side index (1.6); the
-- multi-arm candidate SQL / RPC (1.7). The raw schema/001 trigram indexes
-- (``external_resources_title_trgm`` etc.) are retained additively — they are
-- NOT normalized and serve a different (case/Unicode-sensitive ILIKE) path.
--
-- HOW TO APPLY (operator, not `supabase db push`):
--   * ``CREATE INDEX CONCURRENTLY`` cannot run inside a transaction block, so
--     this file is applied via a session-mode / direct psql connection with
--     autocommit (task 0.1 access path: scripts/live_short_field_trigram.py),
--     NOT the transactional ``db push`` wrapper. ``psql -f`` does NOT wrap in a
--     transaction by default, so each statement below autocommits independently.
--   * The driver sets a bounded ``lock_timeout`` before the build so a transient
--     ACCESS EXCLUSIVE conflict fails fast instead of wedging writers.
--   * It builds online: only brief SHARE locks at start/end, never blocks
--     SELECT/INSERT/UPDATE for the build's duration, never mutates a source row.
--
-- PREREQUISITE: schema/005 (the IMMUTABLE ``hivemind_normalize_identifier`` +
-- the deterministic ICU collation it lowercases through). The guard below fails
-- closed if it is absent; the live driver applies schema/005 safely first when
-- needed (task-1.5 instruction).
--
-- IDEMPOTENCE:
--   * ``CREATE INDEX CONCURRENTLY IF NOT EXISTS`` makes a re-run a safe no-op
--     when a *valid* index of this name already exists.
--   * KNOWN LIMITATION: if a *previous concurrent build failed* and left an
--     INVALID index of this name behind, ``IF NOT EXISTS`` will skip and the
--     invalid remnant will remain. The preflight driver
--     (scripts/short_field_trigram.py preflight_queries) detects
--     ``pg_index.indisvalid = false`` remnants; the recovery is the rollback
--     command below, then a fresh build.
--
-- ROLLBACK (online, outside a transaction; idempotent):
--   DROP INDEX CONCURRENTLY IF EXISTS public.idx_external_resources_title_trgm_norm;
--   DROP INDEX CONCURRENTLY IF EXISTS public.idx_distillations_question_trgm_norm;
--   (Also exposed as `scripts/live_short_field_trigram.py --rollback`.)
-- ============================================================

-- pg_trgm is required by gin_trgm_ops (also created by schema/001/003).
create extension if not exists pg_trgm;

-- ---------------------------------------------------------------------------
-- 1. Guard: schema/005 prerequisite (the IMMUTABLE normalize fn + ICU collation)
--    must be live, and the target tables/columns must be the expected shape.
--    Read-only catalog checks; raises a clear error on drift. Each statement
--    autocommits (no transaction with the CIC below).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    has_fn       boolean;
    has_coll     boolean;
    title_type   text;
    q_type       text;
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

    SELECT a.atttypid::regtype::text INTO title_type
      FROM pg_catalog.pg_attribute a
      JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relname = 'external_resources'
       AND a.attname = 'title' AND NOT a.attisdropped;
    IF title_type IS NULL THEN
        RAISE EXCEPTION 'preflight guard: public.external_resources.title not found — refusing to build';
    END IF;
    IF title_type <> 'text' THEN
        RAISE EXCEPTION 'preflight guard: external_resources.title is %, expected text — refusing to build', title_type;
    END IF;

    SELECT a.atttypid::regtype::text INTO q_type
      FROM pg_catalog.pg_attribute a
      JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relname = 'distillations'
       AND a.attname = 'question' AND NOT a.attisdropped;
    IF q_type IS NULL THEN
        RAISE EXCEPTION 'preflight guard: public.distillations.question not found — refusing to build';
    END IF;
    IF q_type <> 'text' THEN
        RAISE EXCEPTION 'preflight guard: distillations.question is %, expected text — refusing to build', q_type;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. The two canonical normalized trigram indexes (online, additive, idempotent).
--    Names are frozen so preflight / rollback / the 1.7 candidate SQL reference
--    them. Each is a GIN expression index over the IMMUTABLE compact-normalized
--    field with a frozen partial predicate (eligibility + length bound).
-- ---------------------------------------------------------------------------

-- external_resources.title — compact-normalized trigram similarity.
-- Partial predicate: non-empty + length-bounded normalized form (resources have
-- no status column; all rows eligible per 0.2 §5).
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_external_resources_title_trgm_norm
    ON public.external_resources
    USING gin (hivemind_normalize_identifier(title) gin_trgm_ops)
    WHERE char_length(hivemind_normalize_identifier(title)) BETWEEN 1 AND 300;

-- distillations.question — compact-normalized trigram similarity.
-- Partial predicate: the frozen eligibility status IN ('pending','approved')
-- (lexical-contract.md §8) AND non-empty + length-bounded normalized form, so a
-- rejected/superseded distillation can never surface via this arm.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_distillations_question_trgm_norm
    ON public.distillations
    USING gin (hivemind_normalize_identifier(question) gin_trgm_ops)
    WHERE status IN ('pending','approved')
      AND char_length(hivemind_normalize_identifier(question)) BETWEEN 1 AND 300;

-- ---------------------------------------------------------------------------
-- 3. Verification (read-only). Prints the live object state an operator signs.
--    indisvalid MUST be true after a successful online build; a false value
--    means the concurrent build did not finish and the rollback is required.
-- ---------------------------------------------------------------------------
SELECT
    tc.relname                                     AS table_name,
    c.relname                                      AS index_name,
    i.indisvalid                                   AS is_valid,
    i.indisready                                   AS is_ready,
    pg_get_indexdef(i.indexrelid)                  AS index_definition,
    pg_size_pretty(pg_relation_size(i.indexrelid)) AS index_size,
    pg_size_pretty(pg_total_relation_size(tc.oid)) AS table_total_size
FROM pg_catalog.pg_index i
JOIN pg_catalog.pg_class c  ON c.oid = i.indexrelid
JOIN pg_catalog.pg_class tc ON tc.oid = i.indrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relname IN ('idx_external_resources_title_trgm_norm',
                    'idx_distillations_question_trgm_norm')
ORDER BY tc.relname, c.relname;
