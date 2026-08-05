-- ============================================================
-- Hivemind — Phase 2 / Task 2.10 — embedding cleanup behavior
-- ============================================================
--
-- Removes or deactivates embeddings for deleted / ineligible / stale-source /
-- opt-out / failed-contract rows WITHOUT losing the currently-active contract's
-- index during a safe replacement transition (the transition itself is the
-- atomic switch in schema/023; this task is the AFTER/cleanup half).
--
--   hivemind_cleanup_ineligible_embeddings(batch)
--       drop active-contract vectors whose source is gone or now-ineligible:
--         message: missing or is_deleted=true
--         distillation: missing or status not in (pending, approved)
--         resource: missing
--       Bounded; returns the count removed. (This is the "deleted / opted-out /
--       otherwise ineligible fixture cannot rank" gate made operational.)
--   hivemind_deactivate_item_embeddings(entity_type, item_id, contract_id?)
--       drop ALL of an identity's vectors across representations (opt-out /
--       explicit hard removal); optionally scoped to one contract.
--   hivemind_drop_contract_embeddings(contract_id)
--       drop EVERY vector filed under a contract (failed / retired / abandoned).
--       REFUSES the active contract so a replacement transition can never wipe
--       the live index.
--   hivemind_cleanup_superseded_contracts(keep)
--       drop vectors for superseded contracts beyond the most recent `keep`,
--       retaining recent ones for diagnosis. Never touches the active contract.
--
-- SAFE REPLACEMENT (plan AD-2): activate the replacement via schema/023 (it
-- refuses a low-coverage replacement and atomically supersedes the old active
-- contract), verify, THEN call hivemind_drop_contract_embeddings on the old id
-- (or cleanup_superseded_contracts). The active index is never the target.
--
-- Additive + idempotent. Requires schema/020–027 (content_embeddings + active
-- contract resolver). No source row is mutated; only regenerable vectors.
--
-- APPLY:    psql "$HIVEMIND_DB_URL" -f schema/029_embedding_cleanup.sql
-- ROLLBACK: drop function if exists hivemind_cleanup_superseded_contracts(int);
--           drop function if exists hivemind_drop_contract_embeddings(bigint);
--           drop function if exists hivemind_deactivate_item_embeddings(text,text,bigint);
--           drop function if exists hivemind_cleanup_ineligible_embeddings(int);
-- ============================================================

do $$
begin
    if not exists (select 1 from pg_catalog.pg_tables where schemaname='public' and tablename='content_embeddings') then
        raise exception 'prerequisite guard: content_embeddings not found — apply schema/022 (S1) first';
    end if;
end $$;

-- ---------------------------------------------------------------------------
-- Drop active-contract vectors whose source is gone / ineligible (bounded)
-- ---------------------------------------------------------------------------
create or replace function hivemind_cleanup_ineligible_embeddings(p_batch_size int default 1000)
returns int
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_active bigint;
    v_count int := 0;
    v_removed int;
begin
    v_active := hivemind_active_contract_id();
    if v_active is null then
        return 0;
    end if;

    -- Messages: source row missing or soft-deleted.
    with doomed as (
        select ce.ctid from content_embeddings ce
         where ce.contract_id = v_active and ce.entity_type = 'message'
           and not exists (
               select 1 from discord_messages m
                where m.message_id::text = ce.item_id
                  and coalesce(m.is_deleted, false) = false
           )
         limit p_batch_size
    )
    delete from content_embeddings where ctid in (select ctid from doomed);
    get diagnostics v_removed = row_count;
    v_count := v_count + v_removed;

    -- Distillations: source row missing or no longer eligible (rejected/superseded).
    with doomed as (
        select ce.ctid from content_embeddings ce
         where ce.contract_id = v_active and ce.entity_type = 'distillation'
           and not exists (
               select 1 from distillations d
                where d.id::text = ce.item_id
                  and d.status in ('pending', 'approved')
           )
         limit greatest(p_batch_size - v_count, 0)
    )
    delete from content_embeddings where ctid in (select ctid from doomed);
    get diagnostics v_removed = row_count;
    v_count := v_count + v_removed;

    -- Resources: source row missing (hard delete).
    with doomed as (
        select ce.ctid from content_embeddings ce
         where ce.contract_id = v_active and ce.entity_type = 'resource'
           and not exists (
               select 1 from external_resources r where r.id::text = ce.item_id
           )
         limit greatest(p_batch_size - v_count, 0)
    )
    delete from content_embeddings where ctid in (select ctid from doomed);
    get diagnostics v_removed = row_count;
    v_count := v_count + v_removed;

    return v_count;
end;
$$;

-- ---------------------------------------------------------------------------
-- Drop ALL of an identity's vectors (opt-out / explicit removal)
-- ---------------------------------------------------------------------------
create or replace function hivemind_deactivate_item_embeddings(
    p_entity_type text,
    p_item_id text,
    p_contract_id bigint default null
) returns int
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare v_count int;
begin
    -- Opt-out / hard removal: across representations, and across every contract
    -- unless a specific one is named. Used by the opt-out policy path (the
    -- eligibility map notes no live opt-out column today; this is the mechanism a
    -- policy decision wires up, per identity).
    with d as (
        delete from content_embeddings
         where entity_type = p_entity_type
           and item_id = p_item_id
           and (p_contract_id is null or contract_id = p_contract_id)
        returning 1
    )
    select count(*) into v_count from d;
    return v_count;
end;
$$;

-- ---------------------------------------------------------------------------
-- Drop EVERY vector under a contract (failed / retired / abandoned).
-- REFUSES the active contract (safe replacement: never wipe the live index).
-- ---------------------------------------------------------------------------
create or replace function hivemind_drop_contract_embeddings(p_contract_id bigint)
returns int
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_status text;
    v_active bigint;
    v_count int;
begin
    select status into v_status from embedding_contracts where id = p_contract_id;
    if not found then
        raise exception 'contract % not found', p_contract_id;
    end if;
    v_active := hivemind_active_contract_id();
    if p_contract_id = v_active then
        raise exception 'refusing to drop the ACTIVE contract % — activate a '
                        'replacement first (schema/023), verify, then clean up',
            p_contract_id
            using errcode = 'check_violation';
    end if;
    with d as (
        delete from content_embeddings where contract_id = p_contract_id returning 1
    )
    select count(*) into v_count from d;
    return v_count;
end;
$$;

-- ---------------------------------------------------------------------------
-- Drop superseded-contract vectors beyond the most recent `keep` (diagnosis
-- retention). Never touches the active contract.
-- ---------------------------------------------------------------------------
create or replace function hivemind_cleanup_superseded_contracts(p_keep int default 1)
returns int
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_active bigint;
    v_count int := 0;
    c record;
begin
    v_active := hivemind_active_contract_id();
    for c in
        select id from embedding_contracts
         where status = 'superseded'
           and id <> v_active
         order by superseded_at desc
         offset greatest(p_keep, 0)
    loop
        with d as (
            delete from content_embeddings where contract_id = c.id returning 1
        )
        select count(*) + v_count into v_count from d;
    end loop;
    return v_count;
end;
$$;

-- ---------------------------------------------------------------------------
-- Revoke from public + Supabase roles where present.
-- ---------------------------------------------------------------------------
do $$
declare
    sig text;
    role_to_revoke text;
    sigs text[] := array[
        'hivemind_cleanup_ineligible_embeddings(int)',
        'hivemind_deactivate_item_embeddings(text,text,bigint)',
        'hivemind_drop_contract_embeddings(bigint)',
        'hivemind_cleanup_superseded_contracts(int)'
    ];
begin
    foreach sig in array sigs loop
        execute format('revoke execute on function %I.%s from public;', 'public', sig);
        foreach role_to_revoke in array array['anon','authenticated'] loop
            if exists (select 1 from pg_catalog.pg_roles where rolname = role_to_revoke) then
                execute format('revoke execute on function %I.%s from %I;', 'public', sig, role_to_revoke);
            end if;
        end loop;
    end loop;
end $$;

comment on function hivemind_cleanup_ineligible_embeddings(int) is
    'Task 2.10: drop active-contract vectors whose source is gone/ineligible. '
    'The deleted/opted-out/ineligible-rows-cannot-rank gate, made operational.';

-- ---------------------------------------------------------------------------
-- Verification (read-only).
-- ---------------------------------------------------------------------------
select
    p.proname as cleanup_fn,
    p.prosecdef as security_definer
from pg_catalog.pg_proc p
join pg_catalog.pg_namespace n on n.oid = p.pronamespace
where n.nspname = 'public'
  and p.proname in ('hivemind_cleanup_ineligible_embeddings',
                    'hivemind_deactivate_item_embeddings',
                    'hivemind_drop_contract_embeddings',
                    'hivemind_cleanup_superseded_contracts')
order by p.proname;
