-- ============================================================
-- Hivemind — Phase 2 / Task 2.2 — Enable pgvector (additive, reversible)
-- ============================================================
--
-- Adds the `vector` extension ONLY. No source row is read or written; no
-- embedding table, index, RPC, or content change is made here (those are
-- schema/021–024). This migration is deliberately minimal so the extension
-- enablement — the one piece that must exist before any vector(N) column — is
-- its own reviewable, reversible boundary (plan "Suggested change boundaries",
-- and the 0.8 freeze §9: "vector enablement remains task 2.2").
--
-- APPROVED PRECEDENT. Supabase ships pgvector in its extension catalog and
-- documents `create extension vector;` as the enable path. Hivemind's own
-- schema/001 already uses `create extension if not exists pg_trgm;` as its
-- extension-enablement convention, so this follows it for `vector`.
--
-- WHAT THIS IS NOT. It is NOT a production mutation of corpus data, NOT a
-- backfill, NOT an HNSW build, and NOT a contract activation. Per the batch
-- coordination constraint, production DB mutations are paused while the lexical
-- lane is active; this file is the prepared, rehearsed migration plus the exact
-- apply/rollback commands an operator runs at the approved window.
--
-- REHEARSAL (done, task 2.2): applied + rolled back in an ISOLATED local
-- PostgreSQL 14.15 cluster (unix-socket-only, no network) with pgvector 0.8.5
-- built from source against PG14. Evidence:
-- docs/hybrid-search/phase2-pgvector-rehearsal.json. The rehearsal PROVES:
--   * `create extension vector` succeeds and registers extversion;
--   * the `vector` type is usable (a temp vector(4) round-trips);
--   * the cosine distance operator `<=>` is available (the semantic-distance
--     operator the shared index and search RPC will use);
--   * `drop extension vector cascade` cleanly reverses it and removes the type.
--
-- PRODUCTION VERSION NOTE (honest skew): the rehearsal cluster is PG14.15 +
-- pgvector 0.8.5 because that is what the audited dev machine builds locally.
-- The live Hivemind Supabase project runs Supabase-managed Postgres and
-- pgvector (version operator-controlled); the SQL here (CREATE EXTENSION,
-- vector(N), `<=>`) is stable across pgvector >= 0.5 and PG >= 13, so the
-- rehearsal is valid evidence for the schema design. Operators should record
-- the live extversion at apply time (the verification SELECT prints it).
--
-- HOW TO APPLY (operator, at the approved window; additive, idempotent):
--   supabase db execute --file schema/020_enable_pgvector.sql     # via linked project
--   # — or session-mode psql against the Hivemind project —
--   psql "$HIVEMIND_DB_URL" -f schema/020_enable_pgvector.sql
--   # NOTE: prefer the Supabase Dashboard → Database → Extensions → enable
--   # `vector` when that path is the supported one for the project tier.
--
-- ROLLBACK (reversible; vectors are regenerable infrastructure, NOT source):
--   1. Roll back schema/022 (content_embeddings) and schema/021
--      (embedding_contracts) FIRST — they depend on the vector type.
--   2. drop extension if exists vector cascade;
-- Dropping the extension cascade-removes the vector type and every vector(N)
-- column/index; source rows (messages/resources/distillations) are untouched.
-- Per the plan rollback principle, vectors can always be regenerated from
-- source by the resumable backfill (task 2.12), so this is a safe rollback.
-- ============================================================

create extension if not exists vector;

-- ---------------------------------------------------------------------------
-- Verification (read-only). Prints the extension state an operator signs.
-- ---------------------------------------------------------------------------
select
    e.extname                                              as extension_name,
    e.extversion                                           as extension_version,
    n.nspname                                              as schema,
    exists (
        select 1 from pg_catalog.pg_type t
        join pg_catalog.pg_namespace tn on tn.oid = t.typnamespace
        where tn.nspname = e.extnamespace::regnamespace::text
          and t.typname = 'vector'
    )                                                      as vector_type_available,
    -- The cosine-distance operator the shared semantic index + search RPC use.
    exists (
        select 1 from pg_catalog.pg_operator o
        join pg_catalog.pg_namespace onn on onn.oid = o.oprnamespace
        where onn.nspname = e.extnamespace::regnamespace::text
          and o.oprname = '<=>'
    )                                                      as cosine_operator_available
from pg_catalog.pg_extension e
join pg_catalog.pg_namespace n on n.oid = e.extnamespace
where e.extname = 'vector';
