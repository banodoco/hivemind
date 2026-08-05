-- ============================================================
-- Hivemind — Phase 2 / Task 2.15 — semantic candidate SQL
-- ============================================================
--
-- THE canonical semantic candidate SQL: one duplicate-free,
-- deterministic, cosine-ranked identity stream over the ACTIVE
-- embedding contract of the active dimension, collapsed to ONE best
-- chunk per (entity_type, item_id) ACROSS representation types before
-- RRF. This is the internal/service-role semantic-candidate surface a
-- later hardened hybrid RPC (task 3.x) will consume; it is NOT a
-- public surface and it performs NO query embedding (the query vector
-- is a caller-supplied vector(384) parameter — no provider call).
--
-- There is no second, slightly different copy of this query anywhere:
-- the future hybrid RPC, the rehearsal, and the tests all consume THIS
-- ONE function.
--
-- Frozen contracts consumed (do not reinterpret here):
--   * schema/021 — embedding_contracts (one ACTIVE per dimension).
--   * schema/022 — content_embeddings, the fixed vector(384) shared
--     index; identity (entity_type, item_id) + representation_type +
--     chunk_index; the dimension-mixing guard.
--   * schema/024 — hivemind_entity_type_for_result_kind (the frozen
--     result_kind -> entity_type mapping).
--   * schema/025 — hivemind_active_contract_id (the authoritative
--     active-contract resolver of the 384-d dimension).
--   * schema/029 — the eligibility map (message deleted / distillation
--     status not in (pending, approved) / resource missing).
--   * executors.entity_identity — resource is the GENERIC resource
--     entity; a concrete result_kind (workflow/article/transcript/...)
--     is a separate public kind that never changes embedding identity.
--   * executors.chunking (select_best_chunk / collapse_by_item) — the
--     frozen best-chunk tie-break this function mirrors at query time:
--     closest distance first; on an exact tie PROSE before
--     workflow_python, then chunk_index ascending.
--   * the lexical candidate conventions (schemas 008–013): kinds ->
--     entity resolution, the generic-vs-concrete resource-kind rule,
--     the AD-1 unambiguous item_ids rule, and Snowflake-as-text.
--
-- WHAT IT DOES
--   1. Resolves the ACTIVE 384-d contract via the authoritative
--      resolver. NULL active contract -> empty result (no guessing).
--      A DRAFT or SUPERSEDED contract's vectors NEVER rank, even when
--      they are the closest vectors in the table: the contract filter
--      (ce.contract_id = v_active) excludes them before any distance
--      comparison.
--   2. Computes pgvector cosine distance (embedding <=> p_query_embedding)
--      restricted to that one active contract.
--   3. Applies the requested kinds + exact item_ids filters INSIDE each
--      arm (pre-ranking, so a filtered-out row is never scored).
--   4. Maps every surviving vector to an ELIGIBLE current source row
--      (live non-deleted message / existing resource / pending-or-
--      approved distillation) by inner-joining the source table — a
--      vector whose source is gone or now-ineligible cannot rank.
--   5. Collapses to ONE best chunk per (entity_type, item_id) across
--      representation types AND chunk indexes (no entity duplicates; a
--      long item cannot gain advantage merely by having more chunks),
--      using the frozen tie-break.
--   6. Emits bounded, deterministic metadata for the future hybrid
--      layer WITHOUT hydrating full source rows: identity, concrete/
--      public kind, matched representation, matched chunk index, a
--      bounded matched snippet, the vector distance, and a dense
--      deterministic semantic rank.
--
-- SECURITY POSTURE — SECURITY DEFINER (documented choice).
--   The sibling lexical candidate (schema/008) is SECURITY INVOKER
--   because it is reachable ONLY through the SECURITY DEFINER RPC in
--   009, so the RPC's owner context carries the privileges. This
--   surface is different: the task explicitly makes it the internal
--   service-role read path ("grant only to service_role"), so the
--   future hybrid RPC (and direct service-role callers) invoke it. It
--   therefore follows the EMBEDDING lane's own privileged-surface
--   posture (schema/023 active-contract switch, schema/025 enqueue,
--   schema/029 cleanup are all SECURITY DEFINER with a fixed
--   search_path): SECURITY DEFINER + fixed trusted search_path, so the
--   function reads the source tables as its owner (postgres) and the
--   eligibility predicates in SQL are the SOLE gate against exposing
--   ineligible rows to a service-role caller that BYPASSRLS. Direct
--   execution is revoked from PUBLIC/anon/authenticated and granted
--   ONLY to service_role (where that role exists); a low-privilege role
--   cannot call it. Read-only (STABLE).
--
-- Additive + idempotent. Requires schema/003 (lexical_resource_python_state +
-- hivemind_workflow_python_state — the workflow_python safety gate) AND
-- schema/020–029 (vector + embedding_contracts + content_embeddings +
-- active-contract resolver). No source row is read or mutated by this
-- migration; it creates one function and comments.
--
-- APPLY:    psql "$HIVEMIND_DB_URL" -f schema/032_semantic_candidate_sql.sql
-- ROLLBACK: drop function if exists public.hivemind_semantic_candidates(vector,int,text[],text[]);
-- ============================================================

-- Prerequisite guard: this function consumes content_embeddings and the
-- active-contract resolver, so they must exist.
do $$
begin
    if not exists (select 1 from pg_catalog.pg_tables
                    where schemaname='public' and tablename='content_embeddings') then
        raise exception 'prerequisite guard: content_embeddings not found — '
                        'apply schema/022 (S1) first';
    end if;
    if not exists (select 1 from pg_catalog.pg_proc p
                    join pg_catalog.pg_namespace n on n.oid = p.pronamespace
                   where n.nspname='public' and p.proname='hivemind_active_contract_id') then
        raise exception 'prerequisite guard: hivemind_active_contract_id not found — '
                        'apply schema/025 first';
    end if;
    -- Defect-2 dependency: the stale/unsafe workflow_python gate calls the
    -- CANONICAL state accessor + state table defined in schema/003 (the same
    -- accessor the lexical candidate 008 gates on). 003 is a Phase-1 migration
    -- that precedes this one in every applied ordering, but guard explicitly so
    -- a partial/ordered apply fails fast rather than erroring at first call.
    if not exists (select 1 from pg_catalog.pg_tables
                    where schemaname='public'
                      and tablename='lexical_resource_python_state') then
        raise exception 'prerequisite guard: lexical_resource_python_state not found — '
                        'apply schema/003 first (workflow_python state gate depends on it)';
    end if;
    if not exists (select 1 from pg_catalog.pg_proc p
                    join pg_catalog.pg_namespace n on n.oid = p.pronamespace
                   where n.nspname='public'
                     and p.proname='hivemind_workflow_python_state') then
        raise exception 'prerequisite guard: hivemind_workflow_python_state not found — '
                        'apply schema/003 first (workflow_python state gate depends on it)';
    end if;
end $$;

create or replace function public.hivemind_semantic_candidates(
    p_query_embedding   vector(384),
    p_candidate_limit   int      default 100,
    p_kinds             text[]   default '{}',
    p_item_ids          text[]   default '{}'
)
returns table (
    entity_type         text,
    item_id             text,
    kind                text,
    representation_type text,
    chunk_index         integer,
    matched_snippet     text,
    semantic_distance   double precision,
    semantic_rank       integer,
    created_at          timestamptz
)
language plpgsql
stable
-- SECURITY DEFINER: see the header. Reads the source tables as the owner;
-- the eligibility predicates below are the sole gate against a BYPASSRLS
-- service-role caller. Fixed trusted search_path so a caller cannot shadow
-- a relation.
security definer
set search_path = public, pg_temp
as $$
declare
    -- The ACTIVE contract of THIS table's fixed dimension (384). NULL when no
    -- contract is active yet -> empty result. A draft/superseded contract can
    -- never be this value (the resolver filters status='active'), so its
    -- vectors are excluded by the ce.contract_id = v_active predicate below
    -- regardless of how close they are.
    v_active             bigint;

    v_limit              int      := least(greatest(coalesce(p_candidate_limit, 100), 1), 500);

    -- kinds resolution (frozen result_kind -> entity_type, mirrors
    -- executors.entity_identity / schema/024). 'resource' is the GENERIC
    -- resource entity; a concrete resource kind (workflow/article/...) is a
    -- separate public kind that additionally narrows the resource arm.
    v_kinds              text[]   := coalesce(p_kinds, '{}'::text[]);
    v_has_kinds          boolean  := coalesce(array_length(v_kinds, 1), 0) > 0;
    v_want_msg           boolean;
    v_want_dist          boolean;
    v_res_generic        boolean;
    v_res_concrete_kinds text[];
    v_want_res           boolean;

    -- item_ids: a bounded allow-listed identity filter (AD-1). Requires the
    -- kinds filter to resolve to EXACTLY one entity so each id is unambiguous.
    -- Bare / ambiguous item_ids FAIL CLOSED: when item_ids are non-empty and
    -- kinds do not resolve to exactly one entity, v_items_entity is left NULL
    -- and the per-arm predicate below is FALSE for every arm, so the function
    -- returns ZERO rows rather than silently broadening to the whole candidate
    -- set. (The earlier 'ignore ambiguous item_ids' behavior failed OPEN: an
    -- empty sentinel matched IS DISTINCT FROM '<entity>' for every arm, so the
    -- requested ids were dropped and the unfiltered set leaked back. The
    -- future hybrid RPC rejects ambiguity before calling, but this surface no
    -- longer depends on that.) NULL also when no item_ids are passed (no
    -- restriction; in that case the predicate's `not v_has_items` short-circuit
    -- is true and every arm runs unrestricted).
    v_item_ids           text[]   := coalesce(p_item_ids, '{}'::text[]);
    v_has_items          boolean  := coalesce(array_length(v_item_ids, 1), 0) > 0;
    v_items_entity       text;
begin
    v_active := public.hivemind_active_contract_id(384);
    if v_active is null then
        return;  -- no active 384-d contract -> no semantic candidates
    end if;

    -- ------------------------------------------------------------------
    -- 1. kinds -> entities (frozen mapping).
    -- ------------------------------------------------------------------
    v_want_msg    := (not v_has_kinds) or v_kinds && array['message'];
    v_want_dist   := (not v_has_kinds) or v_kinds && array['distillation'];
    -- 'resource' is the generic resource kind (matches ALL resource kinds).
    v_res_generic := (not v_has_kinds) or v_kinds && array['resource'];
    -- concrete resource kinds requested: every requested kind that is not
    -- message / distillation / the generic 'resource' (e.g. workflow, article).
    select array_agg(k) into v_res_concrete_kinds
      from (select unnest(v_kinds) as k) x
     where k not in ('message','distillation','resource');
    v_want_res := v_res_generic
                  or coalesce(array_length(v_res_concrete_kinds, 1), 0) > 0;

    -- ------------------------------------------------------------------
    -- 2. item_ids -> the one entity they restrict to (AD-1). Refuse to guess.
    --    When item_ids are present but kinds do NOT resolve to exactly one
    --    entity, leave v_items_entity NULL: every arm's predicate below
    --    evaluates FALSE, so the function FAILS CLOSED (zero rows). A bare
    --    item_ids list (no kinds) is ambiguous in exactly this way -> closed.
    -- ------------------------------------------------------------------
    if v_has_items then
        if v_want_msg and not v_want_res and not v_want_dist then
            v_items_entity := 'message';
        elsif v_want_res and not v_want_msg and not v_want_dist then
            v_items_entity := 'resource';
        elsif v_want_dist and not v_want_msg and not v_want_res then
            v_items_entity := 'distillation';
        else
            v_items_entity := null;  -- ambiguous/bare -> FAIL CLOSED (zero rows)
        end if;
    end if;

    -- ------------------------------------------------------------------
    -- 3. Emit every eligible arm (UNION ALL), then collapse to one best chunk
    --    per (entity_type, item_id), then rank + limit.
    --
    --    Filters (kind / item_ids) and eligibility (source exists + eligible)
    --    are applied INSIDE each arm's WHERE (pre-ranking) so a filtered-out
    --    or ineligible row is never scored. The active-contract filter is the
    --    FIRST predicate, so a draft/superseded contract's vectors never reach
    --    the distance operator. The cosine distance uses pgvector's <=>.
    -- ------------------------------------------------------------------
    return query
    with arms as (
        -- =================== MESSAGE arm (live, non-deleted) ===================
        select 'message'::text     as entity_type,
               m.message_id::text  as item_id,
               'message'::text     as kind,
               ce.representation_type,
               ce.chunk_index,
               ce.chunk_text,
               (ce.embedding <=> p_query_embedding) as semantic_distance,
               m.created_at
          from public.content_embeddings ce
          join public.discord_messages m on m.message_id::text = ce.item_id
         where ce.contract_id = v_active
           and ce.entity_type = 'message'
           and coalesce(m.is_deleted, false) = false
           and (v_want_msg)
           and (not v_has_items
                or (v_items_entity = 'message'
                    and ce.item_id = any(v_item_ids)))

        union all
        -- =================== RESOURCE arm (existing source; kind preserved) ===================
        -- 'resource' (generic) matches every resource kind; a concrete requested
        -- kind narrows to resources whose source kind equals it. The matched
        -- kind is the source's ACTUAL kind (e.g. 'workflow'), never collapsed.
        select 'resource'::text as entity_type,
               r.id::text       as item_id,
               r.kind           as kind,
               ce.representation_type,
               ce.chunk_index,
               ce.chunk_text,
               (ce.embedding <=> p_query_embedding) as semantic_distance,
               r.created_at
          from public.content_embeddings ce
          join public.external_resources r on r.id::text = ce.item_id
         where ce.contract_id = v_active
           and ce.entity_type = 'resource'
           and (v_want_res)
           and (v_res_generic
                or r.kind = any(coalesce(v_res_concrete_kinds, '{}'::text[])))
           -- Stale / unsafe workflow-Python gate (defect-2 fix). A stale
           -- content_embeddings row whose representation_type is
           -- 'workflow_python' can outlive the workflow's CURRENT public state:
           -- the embedding is contract-scoped and is NOT dropped when the
           -- workflow's python becomes quarantined or unavailable. A prose
           -- vector of the SAME resource remains eligible (prose is not gated
           -- by python safety), but a workflow_python vector may rank ONLY when
           -- the resource's current kind is 'workflow' AND the canonical state
           -- accessor (schema/003, the same accessor the lexical candidate 008
           -- gates on) reports public_state='safe'. This is applied PRE-ranking
           -- so a stale/unsafe python vector is never scored, and its chunk
           -- text can therefore never be returned as matched_snippet.
           and (ce.representation_type <> 'workflow_python'
                or (r.kind = 'workflow'
                    and public.hivemind_workflow_python_state(r.id) = 'safe'))
           and (not v_has_items
                or (v_items_entity = 'resource'
                    and ce.item_id = any(v_item_ids)))

        union all
        -- =================== DISTILLATION arm (pending/approved only) ===================
        select 'distillation'::text as entity_type,
               d.id::text           as item_id,
               'distillation'::text as kind,
               ce.representation_type,
               ce.chunk_index,
               ce.chunk_text,
               (ce.embedding <=> p_query_embedding) as semantic_distance,
               d.created_at
          from public.content_embeddings ce
          join public.distillations d on d.id::text = ce.item_id
         where ce.contract_id = v_active
           and ce.entity_type = 'distillation'
           and d.status in ('pending','approved')
           and (v_want_dist)
           and (not v_has_items
                or (v_items_entity = 'distillation'
                    and ce.item_id = any(v_item_ids)))
    ),
    -- Collapse: exactly ONE row per (entity_type, item_id) across representation
    -- types AND chunk indexes. Tie-break mirrors executors.chunking.collapse_by_item
    -- / select_best_chunk: closest distance first; on an exact tie PROSE before
    -- workflow_python, then chunk_index ascending. (DISTINCT ON keeps the first
    -- row per identity under this order.) No entity duplicates.
    collapsed as (
        select distinct on (arms.entity_type, arms.item_id)
               arms.entity_type, arms.item_id, arms.kind,
               arms.representation_type, arms.chunk_index, arms.chunk_text,
               arms.semantic_distance, arms.created_at
          from arms
         order by arms.entity_type, arms.item_id,
                  arms.semantic_distance asc nulls last,
                  case when arms.representation_type = 'prose' then 0 else 1 end,
                  arms.chunk_index asc
    )
    select collapsed.entity_type,
           collapsed.item_id,
           collapsed.kind,
           collapsed.representation_type,
           collapsed.chunk_index,
           -- Bounded matched snippet (the winning chunk's text, defensively
           -- bounded; the stored chunk_text is already chunk-budget-bounded).
           left(coalesce(collapsed.chunk_text, ''), 512) as matched_snippet,
           collapsed.semantic_distance,
           -- Dense deterministic semantic rank over a total order
           -- (distance, entity_type, item_id). entity_type+item_id is unique
           -- after collapse, so this is a strict total order with no gaps.
           row_number() over (
               order by collapsed.semantic_distance asc nulls last,
                        collapsed.entity_type asc,
                        collapsed.item_id asc
           )::integer as semantic_rank,
           collapsed.created_at
      from collapsed
     order by collapsed.semantic_distance asc nulls last,
              collapsed.entity_type asc,
              collapsed.item_id asc
     limit v_limit;
end;
$$;

comment on function public.hivemind_semantic_candidates(vector, integer, text[], text[]) is
  'Task 2.15: THE canonical semantic candidate SQL. STABLE, SECURITY DEFINER with a '
  'fixed trusted search_path. One duplicate-free cosine-ranked identity stream over the '
  'ACTIVE 384-d contract, collapsed to one best chunk per (entity_type, item_id) across '
  'representation types (frozen tie-break: closest distance; tie -> prose before '
  'workflow_python; tie -> chunk_index asc). Draft/superseded contracts never rank; '
  'eligible-source-only; Snowflake item_ids are text throughout. SECURITY: non-empty '
  'item_ids that do not resolve to exactly one entity FAIL CLOSED (zero rows) rather '
  'than broaden; a workflow_python vector ranks only when the current resource kind is '
  'workflow and hivemind_workflow_python_state = safe (stale/quarantined/unavailable '
  'python never ranks or leaks a snippet), while prose remains eligible. Internal '
  'surface: revoked from PUBLIC/anon/authenticated, granted only to service_role (task '
  '3.x hybrid RPC consumes it). Read-only.';

-- ------------------------------------------------------------
-- Trust boundary: revoke direct execution from everyone except the
-- owner, then grant ONLY to service_role where it exists. anon /
-- authenticated / a low-privilege role cannot call it. (Supabase
-- SECURITY DEFINER funcs default to PUBLIC-executable; this narrows it
-- to the internal service-role read path.) The revoke/grant are
-- conditional because anon/authenticated/service_role do not exist on a
-- vanilla rehearsal cluster.
-- ------------------------------------------------------------
revoke execute on function public.hivemind_semantic_candidates(vector, integer, text[], text[])
  from public;
do $$
declare
    sig constant text := 'hivemind_semantic_candidates(vector, integer, text[], text[])';
    r  text;
begin
    foreach r in array array['anon','authenticated'] loop
        if exists (select 1 from pg_catalog.pg_roles where rolname = r) then
            execute format('revoke execute on function public.%s from %I;', sig, r);
        end if;
    end loop;
    if exists (select 1 from pg_catalog.pg_roles where rolname = 'service_role') then
        execute format('grant execute on function public.%s to service_role;', sig);
    end if;
end $$;

-- ---------------------------------------------------------------------------
-- Verification (read-only): volatility / search_path / security / grants.
-- ---------------------------------------------------------------------------
select
    p.proname                                               as function_name,
    pg_get_function_arguments(p.oid)                        as arguments,
    p.provolatile = 's'                                     as is_stable,
    p.prosecdef                                             as security_definer,
    p.proconfig                                             as config,
    (select array_agg(acl.grantee order by acl.grantee)
       from aclexplode(p.proacl) as acl)                    as grantees
from pg_catalog.pg_proc p
join pg_catalog.pg_namespace n on n.oid = p.pronamespace
where n.nspname = 'public' and p.proname = 'hivemind_semantic_candidates';
