-- ============================================================
-- Hivemind — Phase 2 / Task 2.3 (part 2) — content_embeddings (the shared index)
-- ============================================================
--
-- ONE shared, contract-keyed, representation- and chunk-aware embedding table
-- (plan AD-2). No per-source vector columns; messages, resources, and
-- distillations all live here behind a small, stable identity:
--
--   (contract_id, entity_type, item_id, representation_type, chunk_index)
--
-- Identity vocabulary (plan AD-2, frozen):
--   * entity_type ∈ {message, resource, distillation}      — internal, stable
--   * item_id     text                                     — Discord snowflakes
--                                                            and all ids are text
--   * representation_type ∈ {prose, workflow_python}        — part of identity
--   * chunk_index integer >= 0                              — 0 for single-chunk
--
-- FIXED DIMENSION. The production vector column is a literal vector(384):
-- PostgreSQL cannot store 384- and 1536-d vectors in the same constrained
-- column or HNSW index (plan AD-2). 384 is the frozen capacity-preferred pilot
-- dimension (task 0.7: full corpus 4.59 GB, PASS the 12 GB gate). A dimension
-- comparison/migration uses a SIBLING fixed-dimension table + HNSW + an atomic
-- active-contract switch (schema/023); it never mixes into this table.
--
-- DIMENSION MIXING IS REJECTED at two layers:
--   1. Physical: the column is vector(384); a 1536-d (or any !=384) vector is
--      rejected on insert by pgvector.
--   2. Data: a trigger asserts the row's contract_id references a 384-d
--      contract, so a worker cannot file a vector under the wrong contract.
--
-- The semantic search SQL (task 2.15) restricts to the ACTIVE contract of the
-- active dimension and collapses to one best chunk per (entity_type, item_id)
-- across representation types before RRF. The HNSW index is NOT built here
-- (task 2.16 builds it after bulk backfill); identity/cleanup btrees are.
--
-- representation_hash vs chunk_hash (plan AD-2): representation_hash covers the
-- FULL canonical representation (freshness/idempotence at the representation
-- level); chunk_hash covers the individual embedded chunk (chunk-level reuse).
-- Both are SHA-256 hex of the frozen-normalized text
-- (executors.workflow_representation.representation_hash / chunk_hash).
--
-- Additive + idempotent. Requires schema/020 (vector) + schema/021 (contracts).
--
-- APPLY:    psql "$HIVEMIND_DB_URL" -f schema/022_content_embeddings.sql
-- ROLLBACK: drop table if exists content_embeddings cascade;
--           (safe — regenerable infrastructure; source rows untouched)
-- ============================================================

-- The active dimension for THIS table. A dimension change ships a NEW sibling
-- table at a different literal rather than altering this column in place.
-- (Kept as a psql variable for the trigger so the two never drift.)
do $$
begin
    if not exists (select 1 from pg_catalog.pg_extension where extname = 'vector') then
        raise exception 'prerequisite guard: extension vector not found — '
                        'apply schema/020 (task 2.2) first';
    end if;
    if not exists (select 1 from pg_catalog.pg_tables where schemaname='public' and tablename='embedding_contracts') then
        raise exception 'prerequisite guard: embedding_contracts not found — '
                        'apply schema/021 (task 2.3) first';
    end if;
end $$;

create table if not exists content_embeddings (
    contract_id         bigint not null
        references embedding_contracts(id) on delete cascade,
    entity_type         text not null
        check (entity_type in ('message', 'resource', 'distillation')),
    item_id             text not null,
    representation_type text not null default 'prose'
        check (representation_type in ('prose', 'workflow_python')),
    chunk_index         integer not null default 0
        check (chunk_index >= 0),
    chunk_text          text,
    embedding           vector(384) not null,
    representation_hash text not null
        check (representation_hash ~ '^[0-9a-f]{64}$'),
    chunk_hash          text not null
        check (chunk_hash ~ '^[0-9a-f]{64}$'),
    embedded_at         timestamptz not null default now(),
    primary key (contract_id, entity_type, item_id, representation_type, chunk_index)
);

-- Identity lookup / chunk collapse / hydration / deletion all key on the
-- immutable (entity_type, item_id) identity (plan AD-2).
create index if not exists content_embeddings_identity_idx
    on content_embeddings (entity_type, item_id);

-- Contract-scoped operations: drop, coverage, replacement, backfill cursor.
create index if not exists content_embeddings_contract_idx
    on content_embeddings (contract_id);

-- Contract + representation-type scope (workflow prose vs python collapse).
create index if not exists content_embeddings_contract_rep_idx
    on content_embeddings (contract_id, representation_type);

comment on table content_embeddings is
    'Shared contract-keyed embedding index (plan AD-2). Fixed dimension vector(384). '
    'Identity (entity_type, item_id) is immutable; representation_type/chunk_index are '
    'representation identity. HNSW (task 2.16) and semantic candidate SQL (task 2.15) '
    'come later; this table stores the vectors and identity/cleanup indexes only.';

-- Supabase default privileges auto-grant new public-schema tables to
-- anon/authenticated. content_embeddings stores message chunk_text (incl.
-- deleted messages until the async cleanup drains), so it must NOT be publicly
-- readable. service_role (worker/backfill/search) is untouched. Schema/035's
-- guarded revoke covers DBs where this table already existed before 035.
revoke all on table content_embeddings from anon, authenticated;

-- ---------------------------------------------------------------------------
-- Dimension-mixing guard (data layer). The vector(384) column already rejects a
-- wrong-length vector physically; this trigger additionally forbids filing a
-- vector under a contract whose declared dimension is not 384, so a logic error
-- (e.g. a 1536-d contract id) can never produce a row here. This is the belt to
-- the vector(N) suspenders and is what the "forbids dimension mixing" gate (task
-- 2.3) is tested against.
-- ---------------------------------------------------------------------------
create or replace function enforce_content_embeddings_dimension()
returns trigger
language plpgsql
as $$
declare
    v_dimension integer;
begin
    select dimension into v_dimension from embedding_contracts where id = new.contract_id;
    if not found then
        raise exception 'content_embeddings.contract_id % has no embedding_contracts row',
            new.contract_id;
    end if;
    if v_dimension <> 384 then
        raise exception 'dimension mixing rejected: contract % is %-dimensional but '
                        'content_embeddings is fixed vector(384)',
            new.contract_id, v_dimension;
    end if;
    return new;
end;
$$;

drop trigger if exists trg_content_embeddings_dimension on content_embeddings;
create trigger trg_content_embeddings_dimension
before insert or update of contract_id on content_embeddings
for each row execute function enforce_content_embeddings_dimension();

-- ---------------------------------------------------------------------------
-- Verification (read-only).
-- ---------------------------------------------------------------------------
select
    c.relname                                              as table_name,
    (select atttypid::regtype::text from pg_catalog.pg_attribute a
       join pg_catalog.pg_class cc on cc.oid=a.attrelid
       join pg_catalog.pg_namespace nn on nn.oid=cc.relnamespace
      where nn.nspname='public' and cc.relname='content_embeddings'
        and a.attname='embedding' and not a.attisdropped)  as embedding_type,
    exists (
        select 1 from pg_catalog.pg_trigger t
        join pg_catalog.pg_class c2 on c2.oid = t.tgrelid
        where c2.relname = 'content_embeddings' and t.tgname = 'trg_content_embeddings_dimension'
    )                                                      as dimension_guard_present
from pg_catalog.pg_class c
where c.relname = 'content_embeddings';
