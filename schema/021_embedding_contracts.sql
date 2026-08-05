-- ============================================================
-- Hivemind — Phase 2 / Task 2.3 (part 1) — embedding_contracts
-- ============================================================
--
-- The contract registry for the shared embedding index (plan AD-2, task 0.8 §9).
-- Every vector in content_embeddings (schema/022) belongs to exactly one
-- contract; the search path restricts to the atomically-selected ACTIVE
-- contract of the active dimension. One active contract per dimension is
-- enforced by a partial unique index below.
--
-- Contract identity (frozen):
--   contract_id = provider + model + dimension + canonicalization_version
--                 + chunking_version
-- The integer `id` is the DETERMINISTIC bigint derived from that identity
-- (executors.embedding_contract.contract_id), reproduced byte-for-byte in SQL
-- by the seeding helper below (pgcrypto sha256, truncated to 63 bits). This is
-- what makes the Python backfill/worker and the SQL seeding agree on an id
-- WITHOUT coordination: identical spec => identical id => identical row.
--
-- Status lifecycle (frozen):
--   draft -> active -> superseded   (one `active` per dimension at any time)
-- A dimension migration activates a sibling fixed-dimension table+HNSW and
-- switches the active contract atomically (schema/023); it never overwrites an
-- active contract before replacement coverage passes (the switch fn guards it).
--
-- Additive + idempotent. No source row is touched. Requires schema/020 (vector).
-- The table does not itself store vectors, so it does not depend on the vector
-- *type*, but it is logically grouped with the vector index (task 2.3).
--
-- APPLY:    psql "$HIVEMIND_DB_URL" -f schema/021_embedding_contracts.sql
-- ROLLBACK: drop table if exists embedding_contracts cascade;
--           (roll back schema/022–024 first; safe — regenerable infrastructure)
-- ============================================================

-- Guard: the vector extension must be enabled first (schema/020). We do not use
-- the vector type here, but this guard keeps the 020->021 ordering explicit.
-- pgcrypto is required by hivemind_contract_id (digest); it is present on the
-- live Hivemind project (task 0.2 inventory D9) and enabled idempotently here.
create extension if not exists pgcrypto;

do $$
declare
    has_vector boolean;
begin
    select exists (
        select 1 from pg_catalog.pg_extension where extname = 'vector'
    ) into has_vector;
    if not has_vector then
        raise exception 'prerequisite guard: extension vector not found — '
                        'apply schema/020 (task 2.2) first';
    end if;
end $$;

create table if not exists embedding_contracts (
    id                        bigint primary key,
    provider                  text not null,
    model                     text not null,
    dimension                 integer not null check (dimension > 0),
    canonicalization_version  integer not null check (canonicalization_version > 0),
    chunking_version          integer not null check (chunking_version > 0),
    status                    text not null default 'draft'
                              check (status in ('draft', 'active', 'superseded')),
    created_at                timestamptz not null default now(),
    activated_at              timestamptz,
    superseded_at             timestamptz,
    -- Identity uniqueness: identical spec => one row.
    constraint embedding_contracts_identity_unique
        unique (provider, model, dimension, canonicalization_version, chunking_version)
);

-- At most one ACTIVE contract per dimension (frozen invariant, task 0.8 §9).
create unique index if not exists one_active_contract_per_dimension
    on embedding_contracts (dimension)
    where status = 'active';

create index if not exists embedding_contracts_dimension_status_idx
    on embedding_contracts (dimension, status);

comment on table embedding_contracts is
    'Embedding contract registry (plan AD-2). Each content_embeddings row belongs to one contract; '
    'the search path restricts to the active contract of the active dimension. `id` is the '
    'deterministic bigint from provider+model+dimension+canon_version+chunk_version.';

-- ---------------------------------------------------------------------------
-- Seeding helper: deterministic bigint id from a contract spec (parity anchor).
-- Reproduces executors.embedding_contract.contract_id byte-for-byte so Python
-- (backfill/worker) and SQL agree on the row id without coordination. The
-- preimage uses the unit separator \x1f exactly as the Python side does.
-- ---------------------------------------------------------------------------
create or replace function hivemind_contract_id(
    p_provider text,
    p_model text,
    p_dimension integer,
    p_canonicalization_version integer default 1,
    p_chunking_version integer default 1
) returns bigint
language sql immutable
as $$
    -- Mirrors executors.embedding_contract.contract_id byte-for-byte:
    --   int.from_bytes(sha256(preimage)[:8], 'big') & 0x7FFFFFFFFFFFFFFF
    -- composed from the digest's first 8 bytes (big-endian), then masked to a
    -- positive 63-bit bigint. Two's-complement bitwise ops make the high-bit
    -- case correct exactly as in Python.
    with d as (
        select digest(
            p_provider || E'\x1f' || p_model || E'\x1f' || p_dimension::text
            || E'\x1f' || p_canonicalization_version::text
            || E'\x1f' || p_chunking_version::text,
            'sha256'
        ) as b
    )
    select (
        (get_byte(b, 0)::bigint << 56) |
        (get_byte(b, 1)::bigint << 48) |
        (get_byte(b, 2)::bigint << 40) |
        (get_byte(b, 3)::bigint << 32) |
        (get_byte(b, 4)::bigint << 24) |
        (get_byte(b, 5)::bigint << 16) |
        (get_byte(b, 6)::bigint <<  8) |
        (get_byte(b, 7)::bigint      )
    ) & 9223372036854775807
    from d;
$$;

comment on function hivemind_contract_id(text, text, integer, integer, integer) is
    'Deterministic bigint id for an embedding contract spec. Mirrors '
    'executors.embedding_contract.contract_id (Python) byte-for-byte.';

-- ---------------------------------------------------------------------------
-- Verification (read-only).
-- ---------------------------------------------------------------------------
select
    'hivemind_contract_id(openai,text-embedding-3-small,384,1,1)' as anchor,
    hivemind_contract_id('openai', 'text-embedding-3-small', 384, 1, 1) as sql_id,
    'one_active_contract_per_dimension' as invariant_index,
    exists (
        select 1 from pg_catalog.pg_index i
        join pg_catalog.pg_class c on c.oid = i.indexrelid
        where c.relname = 'one_active_contract_per_dimension'
    ) as invariant_index_exists;
