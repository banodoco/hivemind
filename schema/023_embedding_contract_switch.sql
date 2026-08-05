-- ============================================================
-- Hivemind — Phase 2 / Task 2.3 (part 3) — atomic active-contract switch
-- ============================================================
--
-- `hivemind_set_active_embedding_contract` performs the atomic, same-dimension
-- active-contract transition required by plan AD-2 and the task-0.8 freeze §9:
--
--   "A dimension migration uses a separate fixed-dimension table and HNSW index,
--    followed by an atomic active-contract switch in the search function. Do not
--    mix dimensions in one table or overwrite the active contract before
--    replacement coverage passes."
--
-- In one transaction it: (1) refuses a superseded or missing contract; (2) when
-- a coverage check is requested, refuses to activate a replacement that covers
-- FEWER distinct (entity_type, item_id) identities than the currently-active
-- contract of that dimension (the "do not overwrite the active contract before
-- replacement coverage passes" rule, made data-checkable); (3) supersedes the
-- old active contract of that dimension; (4) activates the new one.
--
-- The `one_active_contract_per_dimension` partial unique index (schema/021) is
-- the hard backstop: even a buggy caller cannot leave two active contracts of
-- one dimension, because step (3) runs before step (4) in the same transaction.
--
-- SECURITY: the function is the trust boundary an embedding operator/worker
-- uses. It is read/write ONLY on embedding infrastructure tables
-- (embedding_contracts, content_embeddings counts); it never touches source
-- rows and never needs source credentials. It uses a fixed search_path so a
-- caller cannot shadow the relations. (The public search RPC, a separate
-- SECURITY DEFINER, is task 3.3.)
--
-- Additive + idempotent (re-createable). Requires schema/021 + schema/022.
--
-- APPLY:    psql "$HIVEMIND_DB_URL" -f schema/023_embedding_contract_switch.sql
-- ROLLBACK: drop function if exists hivemind_set_active_embedding_contract(bigint, boolean);
-- ============================================================

create or replace function hivemind_set_active_embedding_contract(
    p_contract_id bigint,
    p_require_coverage boolean default true
) returns void
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_dimension integer;
    v_status text;
    v_old_active bigint;
    v_new_items bigint;
    v_old_items bigint;
begin
    select dimension, status
      into v_dimension, v_status
      from embedding_contracts
     where id = p_contract_id;

    if not found then
        raise exception 'contract % not found', p_contract_id;
    end if;

    if v_status = 'superseded' then
        raise exception 'cannot activate a superseded contract (%)', p_contract_id;
    end if;

    select id into v_old_active
      from embedding_contracts
     where dimension = v_dimension
       and status = 'active'
       and id <> p_contract_id;

    -- Replacement-coverage guard (plan AD-2 / task 0.8 §9): the replacement must
    -- cover at least the identities the outgoing active contract covered.
    if p_require_coverage and v_old_active is not null then
        select count(*) into v_new_items from (
            select distinct entity_type, item_id from content_embeddings where contract_id = p_contract_id
        ) s;
        select count(*) into v_old_items from (
            select distinct entity_type, item_id from content_embeddings where contract_id = v_old_active
        ) s;
        if v_new_items < v_old_items then
            raise exception 'replacement coverage failed: contract % covers % identities, '
                            'the active contract % covers % — activate only after coverage passes',
                p_contract_id, v_new_items, v_old_active, v_old_items
                using errcode = 'check_violation';
        end if;
    end if;

    -- Atomic transition: supersede the old, activate the new.
    if v_old_active is not null then
        update embedding_contracts
           set status = 'superseded', superseded_at = now()
         where id = v_old_active;
    end if;

    update embedding_contracts
       set status = 'active', activated_at = coalesce(activated_at, now())
     where id = p_contract_id;
end;
$$;

comment on function hivemind_set_active_embedding_contract(bigint, boolean) is
    'Atomic same-dimension active-contract switch (plan AD-2). Refuses to overwrite '
    'the active contract before replacement coverage passes. Backstopped by the '
    'one_active_contract_per_dimension partial unique index.';

-- ---------------------------------------------------------------------------
-- Verification (read-only).
-- ---------------------------------------------------------------------------
select
    n.nspname as schema,
    p.proname as function_name,
    pg_get_function_arguments(p.oid) as args,
    p.prosecdef as security_definer,
    p.proconfig as config
from pg_catalog.pg_proc p
join pg_catalog.pg_namespace n on n.oid = p.pronamespace
where n.nspname = 'public' and p.proname = 'hivemind_set_active_embedding_contract';
