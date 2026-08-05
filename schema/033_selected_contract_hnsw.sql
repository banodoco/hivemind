-- ============================================================
-- Hivemind — Phase 2 / Task 2.16 — selected-contract HNSW pilot
-- ============================================================
--
-- A monotonic, additive migration AFTER 032 that upgrades the SAME canonical
-- semantic-candidate surface so broad natural queries can use a selected-literal
-- partial HNSW index — WITHOUT changing the task-2.15 signature, filters,
-- eligibility, fail-closed semantics, safe workflow-Python gate, best-chunk
-- tie-break, metadata, deterministic final order, fixed search_path, or the
-- SECURITY DEFINER / service-role-only posture. There is still exactly ONE
-- semantic-candidate implementation: the future hybrid RPC, the rehearsal, the
-- benchmark, and the tests all consume THIS function.
--
-- WHY A NEW MIGRATION (the task-2.16 planner prerequisite)
--   schema/032 is exact-cosine correct, but in its shipped form it is not
--   naturally compatible with the required selected-literal partial HNSW index:
--     1. Its vector predicate is `ce.contract_id = v_active`, a PL/pgSQL
--        variable. PostgreSQL cannot prove a runtime variable implies a
--        partial-index predicate containing a specific bigint literal, so the
--        partial HNSW index could never be chosen for the natural plan.
--     2. Its distance order/limit happens only after UNION, source joins,
--        entity collapse, and final sorting — pgvector HNSW needs an indexable
--        `ORDER BY embedding <=> query LIMIT ...` at the vector scan.
--   This migration fixes both, and corrects the first-pass defect where the kNN
--   LIMIT was applied BEFORE the eligibility/source joins (so nearer deleted /
--   rejected / quarantined vectors could consume the over-fetch bound and crowd
--   out a farther valid entity):
--     1. it bakes the SELECTED FULL-LITERAL contract id into the vector-scan
--        predicate (so the planner can match the partial index);
--     2. each vector arm is an indexable kNN `ORDER BY embedding <=> query
--        LIMIT v_overfetch`, and the source join AND EVERY eligibility/filter
--        predicate (deletion gate, distillation status, resource kind, the
--        workflow-Python safe-state gate, kinds, item_ids) live INSIDE that arm
--        so the LIMIT counts only ELIGIBLE rows (with hnsw.iterative_scan =
--        strict_order the partial index scans in distance order and keeps
--        iterating until LIMIT eligible rows survive the join);
--     3. v_overfetch is NULL in EXACT mode (GUC hivemind.semantic_exact_mode=on,
--        fresh session with index scans disabled) and for item-id-scoped arms,
--        so the exact baseline returns the COMPLETE applicable cohort (no
--        distance truncation) — byte-identical in semantics to schema/032.
--
-- THE SELECTED FULL-LITERAL CONTRACT ID (the task-2.16 identity resolution)
--   Production contract ids are DIMENSION-ONLY and cannot distinguish the two
--   384-d candidates the task-2.14 grid compared (384-small and 384-large share
--   the historical bigint 6368594834396668537, the dimension-only id under chunking
--   v2; the v1 chunking dimension-only id was 7571371577804399660). The task-2.16 partial index
--   must be unambiguous, so it is keyed on a frozen selected-contract identity
--   that extends the production preimage with the chunk-config identity:
--       openai \x1f text-embedding-3-small \x1f 384 \x1f 1 \x1f 2
--         \x1f chunk_config \x1f v1 \x1f prose#512/50 \x1f workflow_python#512/50
--   derived to a positive 63-bit bigint with the EXACT production rule
--   (SHA-256, first 8 bytes big-endian, & 0x7fffffffffffffff):
--       selected_contract_id = 1360541028304258884
--       sha256(full preimage) = 12e19cdb566b8744...(the accepted task-2.14
--       eval_contract_id 12e19cdb566b8744 is the first 16 hex chars of this
--       same digest, independently corroborating the preimage).
--   NOTE the two distinct version axes in the preimage above: the base contract
--   carries chunking_version = 2 (the chunker ALGORITHM, bumped 1 -> 2 for the
--   bounded oversized workflow-Python fallback fix), while the chunk-config
--   IDENTITY scheme stays v1 (the fallback fix added no identity axis, so the
--   identity scheme did not bump). An earlier draft bumped the chunk-config
--   identity to v2 too (a double version bump); it is corrected back to v1
--   here, so chunking is v2 but the chunk-config identity is v1.
--   This migration adds the additive identity surface
--   hivemind_selected_contract_id(...) and SELF-VERIFIES it reproduces the
--   frozen literal at apply time. It does NOT alter embedding_contracts, does
--   NOT add a chunk-config column to the contract registry, and does NOT
--   activate or seed anything: the literal is a derived, cross-language
--   constant, not a production-row mutation. Production will later expose the
--   full identity/version surface (task 2.17 known gap); until then this
--   function FAILS CLOSED whenever the active 384-d contract is not the
--   selected full literal, so it can never serve candidates from an unselected
--   or ambiguous contract under an index built for a different literal.
--
-- FROZEN HNSW INDEX CONTRACT (operator-run; NOT executed here)
--   CREATE INDEX CONCURRENTLY may never run inside a migration transaction,
--   function, or DO block. The build/drop below are executed ONLY by the
--   operator/rehearsal as separate autocommit statements (see
--   scripts/rehearse_hnsw_pilot.py). They are documented here so the operator,
--   the rehearsal, and the catalog checks agree on one frozen contract:
--
--     create index concurrently if not exists
--       content_embeddings_hnsw_c1360541028304258884
--     on public.content_embeddings
--     using hnsw (embedding vector_cosine_ops)
--     with (m = 16, ef_construction = 64)
--     where contract_id = 1360541028304258884;
--
--     drop index concurrently if exists
--       content_embeddings_hnsw_c1360541028304258884;
--
--   NOTE: PostgreSQL forbids a schema-qualified name on CREATE INDEX (the index
--   is always created in its parent table's schema, public), so the CREATE name
--   above is unqualified while the table is schema-qualified. The DROP name is
--   also shown unqualified for symmetry (search_path includes public); a
--   schema-qualified DROP name is equally valid.
--
--   Rollback drops ONLY this regenerable index; it retains embeddings,
--   contracts, jobs, backfill/remediation cursors, and every source row.
--
-- Additive + idempotent. Requires schema/003 + schema/020–032. No source row is
-- read or mutated by this migration; it creates/replaces one helper function,
-- upgrades one existing function, and adds comments.
--
-- APPLY:    psql "$HIVEMIND_DB_URL" -f schema/033_selected_contract_hnsw.sql
-- ROLLBACK: psql ... -c "drop function if exists public.hivemind_selected_contract_id(text,text,int,int,int,text);"
--           then re-apply schema/032 to restore the pre-2.16 candidate function.
-- ============================================================

-- Prerequisite guard: this migration consumes content_embeddings, the
-- active-contract resolver, and the canonical task-2.15 candidate function.
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
    if not exists (select 1 from pg_catalog.pg_proc p
                    join pg_catalog.pg_namespace n on n.oid = p.pronamespace
                   where n.nspname='public'
                     and p.proname='hivemind_semantic_candidates') then
        raise exception 'prerequisite guard: hivemind_semantic_candidates not found — '
                        'apply schema/032 (task 2.15) first';
    end if;
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

-- ------------------------------------------------------------
-- (A) Additive identity surface: the selected-contract bigint.
--
-- Mirrors the production hivemind_contract_id rule (schema/021) byte-for-byte,
-- but hashes the FULL preimage (base contract identity + \x1f + chunk-config
-- identity) instead of the dimension-only base. Pure SQL, IMMUTABLE, so Python
-- (executors.selected_contract) and SQL derive the identical bigint. This is
-- the cross-language selected-contract identity; it does NOT replace the
-- production contract id or add a column to embedding_contracts.
-- ------------------------------------------------------------
create or replace function public.hivemind_selected_contract_id(
    p_provider                 text,
    p_model                    text,
    p_dimension                integer,
    p_canonicalization_version integer default 1,
    p_chunking_version         integer default 1,
    p_chunk_config_identity    text    default ''
) returns bigint
language sql
immutable
as $$
    with d as (
        select digest(
            p_provider || E'\x1f' || p_model || E'\x1f' || p_dimension::text
            || E'\x1f' || p_canonicalization_version::text
            || E'\x1f' || p_chunking_version::text
            || E'\x1f' || p_chunk_config_identity,
            'sha256'
        ) as b
    )
    select (
        (get_byte(b, 0)::bigint << 56) | (get_byte(b, 1)::bigint << 48) |
        (get_byte(b, 2)::bigint << 40) | (get_byte(b, 3)::bigint << 32) |
        (get_byte(b, 4)::bigint << 24) | (get_byte(b, 5)::bigint << 16) |
        (get_byte(b, 6)::bigint <<  8) | (get_byte(b, 7)::bigint      )
    ) & 9223372036854775807
    from d;
$$;

comment on function public.hivemind_selected_contract_id(text,text,integer,integer,integer,text) is
  'Task 2.16: the additive selected-contract identity. Same derivation rule as '
  'hivemind_contract_id (SHA-256, first 8 bytes big-endian, & 0x7fffffffffffffff) '
  'but over the FULL preimage (base contract identity + unit separator + frozen '
  'chunk-config identity), so it distinguishes the selected 384-small chunk '
  'configuration from 384-large. For the selected contract it returns the frozen '
  'literal 1360541028304258884. Pure/IMMUTABLE; Python (executors.selected_contract) '
  'and SQL agree. Does not mutate embedding_contracts or activate anything.';

-- Self-verification at apply time: the helper MUST reproduce the frozen selected
-- literal. If derivation ever drifts from the accepted task-2.14 identity, the
-- migration fails rather than silently selecting a different contract.
do $$
declare
    v_derived bigint;
    v_frozen  constant bigint := 1360541028304258884;
begin
    select public.hivemind_selected_contract_id(
        'openai','text-embedding-3-small',384,1,2,
        'chunk_config'||E'\x1f'||'v1'||E'\x1f'||'prose#512/50'||E'\x1f'||'workflow_python#512/50'
    ) into v_derived;
    if v_derived is distinct from v_frozen then
        raise exception 'selected-contract identity drift: derived % does not match '
                        'frozen literal % — DO NOT apply task 2.16', v_derived, v_frozen;
    end if;
end $$;

-- ============================================================
-- (B) Upgrade the SAME canonical task-2.15 surface for the selected contract.
-- Signature, returns table, grants, eligibility, filters, fail-closed
-- item_ids, workflow-Python gate, best-chunk collapse/tie-break, metadata,
-- deterministic final order, fixed search_path, and SECURITY DEFINER posture
-- are ALL preserved from schema/032. Three task-2.16 additions:
--   * a selected-literal fail-closed gate (active 384 contract must equal the
--     frozen full literal 1360541028304258884, else ZERO rows);
--   * an HNSW-DRIVER arm shape: the eligible, EMBEDDED item-id set per entity is
--     resolved from current source state and baked as a LITERAL text[] into each
--     per-arm single-table kNN `ORDER BY embedding <=> query LIMIT v_overfetch`.
--     The eligibility array is a ROW-LOCAL predicate on content_embeddings, so
--     pgvector's iterative scan applies it DURING the distance-ordered scan,
--     BEFORE the per-arm LIMIT (C1: the over-fetch bound counts only eligible
--     rows; a nearer deleted/rejected/quarantined/wrong-kind vector cannot crowd
--     out a farther eligible entity). This is REQUIRED for natural HNSW use:
--     pgvector 0.8.5/PG14 only drives the partial HNSW path for a single-table
--     scan with row-local filters — ANY source JOIN (inner/left/semi/lateral/IN)
--     or parameterized array/limit forces btree content_embeddings_identity_idx +
--     Sort, even when forced. The source JOIN (resource kind / created_at) is a
--     post-collapse DECORATION only (kept out of the kNN path); it is 1:1 by PK
--     so it cannot duplicate/reorder, and the final ORDER BY is byte-identical to
--     schema/032;
--   * an EXACT-MODE switch (GUC hivemind.semantic_exact_mode) that NULLs the
--     per-arm bound so a caller running index-scan-disabled in a fresh session
--     gets the COMPLETE applicable cohort (C2: a true exact baseline, not a
--     LIMIT-truncated one). Exact and ANN share this ONE function/template.
-- ============================================================
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
-- SECURITY DEFINER: unchanged posture from 032. Reads the source tables as the
-- owner; the eligibility predicates below are the sole gate against a BYPASSRLS
-- service-role caller. Fixed trusted search_path so a caller cannot shadow a
-- relation.
security definer
set search_path = public, pg_temp
as $$
declare
    v_active             bigint;

    v_limit              int      := least(greatest(coalesce(p_candidate_limit, 100), 1), 500);

    -- EXACT-MODE switch (task-2.16 C2): a caller (the benchmark) signals a TRUE
    -- exact baseline by setting the custom GUC hivemind.semantic_exact_mode = on
    -- in a FRESH session (a new psql connection) that ALSO disables index scans.
    -- Exact mode NULLs the per-arm bound so each arm returns the COMPLETE
    -- applicable cohort (no distance truncation) before collapse — byte-identical
    -- in semantics to schema/032. ANN mode (the default, GUC off) keeps the frozen
    -- over-fetch bound. The GUC is read into a local (not inline) so it stays a
    -- bind parameter of the cached RETURN QUERY plan; the benchmark uses one fresh
    -- session per mode, so the plan matches that mode. No new function parameter,
    -- so the frozen task-2.15 signature is preserved.
    v_exact_mode         boolean  := coalesce(
        nullif(current_setting('hivemind.semantic_exact_mode', true), ''),
        'off')::boolean;

    -- kNN over-fetch bound for BROAD (unfiltered / kinds-only) arms in ANN mode.
    -- NULL in exact mode (complete cohort) and for item-id-scoped arms (a
    -- selective item_ids query scans its full eligible set via a btree/pk path and
    -- is never distance-truncated). NULL LIMIT in PostgreSQL means "no limit".
    --
    -- The bound is MODEST and pre-registered (a fixed bounded over-fetch policy,
    -- NOT a way to mask the C1 eligibility-before-LIMIT fix): because every
    -- eligibility predicate now lives INSIDE the arm, the bound only needs to
    -- survive the best-chunk-per-entity COLLAPSE (not absorb ineligible rows).
    -- `v_limit * 8` gives ~8x the requested entities worth of nearest eligible
    -- chunks (capped at 80); with hnsw.iterative_scan = strict_order the partial
    -- HNSW index returns that many ELIGIBLE chunks and iterates past filtered-out
    -- rows. Empirically (PG14 + pgvector 0.8.5 at the 23,138-vector local volume)
    -- the planner chooses the HNSW index for per-arm LIMIT <= ~80 and falls back
    -- to a Sort for larger limits — so the bound is kept at/below that natural-
    -- HNSW threshold. The threshold is volume-dependent (production scale favors
    -- HNSW at larger limits); that production-scale behavior is NOT proved here
    -- (local_volume_only).
    v_overfetch          int      := case
        when v_exact_mode then null::int
        when coalesce(array_length(coalesce(p_item_ids, '{}'::text[]), 1), 0) > 0
        then null::int
        else least(greatest(v_limit * 8, 40), 80)
    end;

    v_kinds              text[]   := coalesce(p_kinds, '{}'::text[]);
    v_has_kinds          boolean  := coalesce(array_length(v_kinds, 1), 0) > 0;
    v_want_msg           boolean;
    v_want_dist          boolean;
    v_res_generic        boolean;
    v_res_concrete_kinds text[];
    v_want_res           boolean;

    v_item_ids           text[]   := coalesce(p_item_ids, '{}'::text[]);
    v_has_items          boolean  := coalesce(array_length(v_item_ids, 1), 0) > 0;
    v_items_entity       text;

    -- task-2.16 HNSW-DRIVER state (see body step 3). Decisive planner finding
    -- (PG14 + pgvector 0.8.5): the partial-HNSW kNN path is generated ONLY for a
    -- single-table scan whose filters are ROW-LOCAL on content_embeddings. ANY
    -- source JOIN (inner/left/semi/lateral/IN) — or ANY parameterized array /
    -- limit — forces a non-HNSW driver (btree content_embeddings_identity_idx +
    -- Sort), and that holds even under enable_seqscan=off + enable_indexscan=off
    -- (HNSW is structurally unreachable through a join in this build). So this
    -- surface resolves eligibility to the eligible, EMBEDDED item-id set per
    -- entity (from CURRENT source state), bakes it as a LITERAL text[] into each
    -- per-arm single-table kNN predicate, and keeps the source JOIN (resource
    -- kind / created_at) OUTSIDE the arms in a post-collapse decoration. The
    -- literal array is a row-local predicate, so pgvector's iterative scan applies
    -- it DURING the distance-ordered HNSW scan, before the per-arm LIMIT (C1
    -- preserved). Only p_query_embedding is a bind parameter ($1) — PL/pgSQL
    -- EXECUTE ... USING keeps the vector indexable here (a session-level PREPARE
    -- with an opaque Param vector does NOT; the function uses EXECUTE USING).
    -- All baked tokens are quote_literal-sanitized (SECURITY DEFINER, no
    -- injection surface). item-id-scoped arms use LIMIT NULL (small array -> a
    -- btree/pk path, never distance-truncated); exact mode (GUC) also NULLs the
    -- bound for a complete-cohort baseline byte-identical to schema/032.
    v_msg_ids            text[]   := '{}';  -- live, embedded message item_ids
    v_res_exists_ids     text[]   := '{}';  -- existing, embedded resource item_ids
    v_res_safe_wp_ids    text[]   := '{}';  -- workflow+safe, embedded resource item_ids
    v_dist_ids           text[]   := '{}';  -- pending/approved, embedded distillation item_ids
    v_msg_lit            text;              -- baked text[] literal of v_msg_ids
    v_res_exists_lit     text;
    v_res_safe_wp_lit    text;
    v_dist_lit           text;
    v_limlit             text;            -- 'null' in exact/item-scoped mode, else the int
    v_sql                text;
    v_arm                text[] := '{}';
begin
    v_active := public.hivemind_active_contract_id(384);
    if v_active is null then
        return;  -- no active 384-d contract -> no semantic candidates
    end if;

    -- ------------------------------------------------------------------
    -- SELECTED-LITERAL FAIL-CLOSED GATE (task 2.16).
    -- This surface is selective to the ONE selected full-literal 384-d
    -- contract. The active contract's stored id MUST equal the frozen selected
    -- literal (which embeds the chunk-config disambiguation the dimension-only
    -- id lacks). Otherwise return ZERO rows: never serve candidates from an
    -- unselected/ambiguous contract, and never serve under a partial index
    -- built for a different literal. Production activates this literal as part
    -- of task 2.17; until then the surface is intentionally inert here.
    -- ------------------------------------------------------------------
    if v_active <> 1360541028304258884 then
        return;
    end if;

    -- ------------------------------------------------------------------
    -- 1. kinds -> entities (frozen mapping; identical to 032).
    -- ------------------------------------------------------------------
    v_want_msg    := (not v_has_kinds) or v_kinds && array['message'];
    v_want_dist   := (not v_has_kinds) or v_kinds && array['distillation'];
    v_res_generic := (not v_has_kinds) or v_kinds && array['resource'];
    select array_agg(k) into v_res_concrete_kinds
      from (select unnest(v_kinds) as k) x
     where k not in ('message','distillation','resource');
    v_want_res := v_res_generic
                  or coalesce(array_length(v_res_concrete_kinds, 1), 0) > 0;

    -- ------------------------------------------------------------------
    -- 2. item_ids -> the one entity they restrict to (AD-1). Identical to 032:
    --    bare/ambiguous item_ids FAIL CLOSED (v_items_entity stays NULL).
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
    -- 3. Resolve the eligible, EMBEDDED item-id set per entity from CURRENT
    --    source state, bake each as a LITERAL text[] into a single-table kNN
    --    arm, UNION the arms, collapse, decorate with the source JOIN, and
    --    EXECUTE. See the declare block for the HNSW-driver rationale.
    --
    --    C1 (preserved): eligibility is a ROW-LOCAL array-membership predicate
    --    on content_embeddings, applied DURING the distance-ordered HNSW scan
    --    (hnsw.iterative_scan = strict_order) BEFORE the per-arm LIMIT, so a
    --    nearer deleted/rejected/quarantined/wrong-kind vector is filtered
    --    before it can consume the over-fetch bound. C2: exact mode NULLs the
    --    bound so the COMPLETE eligible cohort is scored (true exact baseline).
    -- ------------------------------------------------------------------
    v_limlit := case when v_overfetch is null then 'null' else v_overfetch::text end;

    -- message: live (non-deleted) AND embedded under the selected contract.
    if v_want_msg and (not v_has_items or v_items_entity = 'message') then
        select coalesce(array_agg(distinct m.message_id::text), '{}'::text[])
          into v_msg_ids
          from public.discord_messages m
         where coalesce(m.is_deleted, false) = false
           and exists (select 1 from public.content_embeddings ce
                        where ce.contract_id = 1360541028304258884
                          and ce.entity_type = 'message'
                          and ce.item_id = m.message_id::text)
           and (not v_has_items or m.message_id::text = any(v_item_ids));
    end if;

    -- resource: existence-eligible (any representation) + workflow-python-safe.
    -- kinds (generic vs concrete) and item_ids scope are folded in here, so each
    -- baked array already encodes the full eligibility predicate for its arm.
    if v_want_res and (not v_has_items or v_items_entity = 'resource') then
        select coalesce(array_agg(distinct r.id::text), '{}'::text[])
          into v_res_exists_ids
          from public.external_resources r
         where (v_res_generic
                or r.kind = any(coalesce(v_res_concrete_kinds, '{}'::text[])))
           and exists (select 1 from public.content_embeddings ce
                        where ce.contract_id = 1360541028304258884
                          and ce.entity_type = 'resource'
                          and ce.item_id = r.id::text)
           and (not v_has_items or r.id::text = any(v_item_ids));

        select coalesce(array_agg(distinct r.id::text), '{}'::text[])
          into v_res_safe_wp_ids
          from public.external_resources r
          left join public.lexical_resource_python_state lps on lps.resource_id = r.id
         where r.kind = 'workflow'
           and lps.public_state = 'safe'
           and (v_res_generic
                or r.kind = any(coalesce(v_res_concrete_kinds, '{}'::text[])))
           and exists (select 1 from public.content_embeddings ce
                        where ce.contract_id = 1360541028304258884
                          and ce.entity_type = 'resource'
                          and ce.representation_type = 'workflow_python'
                          and ce.item_id = r.id::text)
           and (not v_has_items or r.id::text = any(v_item_ids));
    end if;

    -- distillation: pending/approved AND embedded under the selected contract.
    if v_want_dist and (not v_has_items or v_items_entity = 'distillation') then
        select coalesce(array_agg(distinct d.id::text), '{}'::text[])
          into v_dist_ids
          from public.distillations d
         where d.status in ('pending','approved')
           and exists (select 1 from public.content_embeddings ce
                        where ce.contract_id = 1360541028304258884
                          and ce.entity_type = 'distillation'
                          and ce.item_id = d.id::text)
           and (not v_has_items or d.id::text = any(v_item_ids));
    end if;

    -- Bake the four arrays as quote_literal-sanitized text[] literals. Only
    -- p_query_embedding remains a bind parameter ($1); a parameterized array
    -- (or limit) drops the partial HNSW index.
    select quote_literal('{' || coalesce(string_agg(x, ','), '') || '}') || '::text[]'
      into v_msg_lit from unnest(v_msg_ids) as x;
    select quote_literal('{' || coalesce(string_agg(x, ','), '') || '}') || '::text[]'
      into v_res_exists_lit from unnest(v_res_exists_ids) as x;
    select quote_literal('{' || coalesce(string_agg(x, ','), '') || '}') || '::text[]'
      into v_res_safe_wp_lit from unnest(v_res_safe_wp_ids) as x;
    select quote_literal('{' || coalesce(string_agg(x, ','), '') || '}') || '::text[]'
      into v_dist_lit from unnest(v_dist_ids) as x;

    -- MESSAGE arm (single-table kNN; eligibility via the live-message array).
    if v_want_msg and (not v_has_items or v_items_entity = 'message')
       and coalesce(array_length(v_msg_ids, 1), 0) > 0 then
        v_arm := v_arm || array[(
            ' select ''message''::text as entity_type, ce.item_id, ce.representation_type, '
            ' ce.chunk_index, ce.chunk_text, (ce.embedding <=> $1) as semantic_distance '
            ' from public.content_embeddings ce '
            ' where ce.contract_id = 1360541028304258884 '
            ' and ce.entity_type = ''message'' '
            ' and ce.item_id = any(' || v_msg_lit || ') '
            ' order by ce.embedding <=> $1 limit ' || v_limlit
        )];
    end if;

    -- RESOURCE arm (single-table kNN; existence via res_exists, workflow-python
    -- safety via res_safe_wp; NO source join inside the kNN path).
    if v_want_res and (not v_has_items or v_items_entity = 'resource')
       and coalesce(array_length(v_res_exists_ids, 1), 0) > 0 then
        v_arm := v_arm || array[(
            ' select ''resource''::text as entity_type, ce.item_id, ce.representation_type, '
            ' ce.chunk_index, ce.chunk_text, (ce.embedding <=> $1) as semantic_distance '
            ' from public.content_embeddings ce '
            ' where ce.contract_id = 1360541028304258884 '
            ' and ce.entity_type = ''resource'' '
            ' and ce.item_id = any(' || v_res_exists_lit || ') '
            ' and (ce.representation_type <> ''workflow_python'' '
            '      or ce.item_id = any(' || v_res_safe_wp_lit || ')) '
            ' order by ce.embedding <=> $1 limit ' || v_limlit
        )];
    end if;

    -- DISTILLATION arm (single-table kNN; eligibility via pending/approved array).
    if v_want_dist and (not v_has_items or v_items_entity = 'distillation')
       and coalesce(array_length(v_dist_ids, 1), 0) > 0 then
        v_arm := v_arm || array[(
            ' select ''distillation''::text as entity_type, ce.item_id, ce.representation_type, '
            ' ce.chunk_index, ce.chunk_text, (ce.embedding <=> $1) as semantic_distance '
            ' from public.content_embeddings ce '
            ' where ce.contract_id = 1360541028304258884 '
            ' and ce.entity_type = ''distillation'' '
            ' and ce.item_id = any(' || v_dist_lit || ') '
            ' order by ce.embedding <=> $1 limit ' || v_limlit
        )];
    end if;

    -- No arm applicable (bare/ambiguous item_ids, or nothing eligible) => FAIL CLOSED.
    if coalesce(array_length(v_arm, 1), 0) = 0 then
        return;
    end if;

    -- Collapse to ONE best chunk per (entity_type, item_id) across representations
    -- AND chunk indexes; tie-break byte-identical to 032 / 008 / executors.chunking
    -- (closest distance; tie -> prose before workflow_python; tie -> chunk_index
    -- asc). Dense deterministic rank over (distance, entity_type, item_id). The
    -- source JOIN (resource kind / created_at) is a post-collapse DECORATION
    -- (kept out of the kNN arms so it never breaks HNSW); it is 1:1 per
    -- (entity_type, item_id) by PK, so it cannot duplicate or reorder rows — the
    -- final ORDER BY (distance, entity_type, item_id) is the sole output order,
    -- identical to schema/032. The final LIMIT v_limit is the only RESULT bound;
    -- per-arm LIMIT v_limlit is the kNN/over-fetch bound (NULL in exact/item-
    -- scoped mode).
    v_sql :=
        'with arms as ('
        || (select string_agg('(' || a || ')', ' union all ') from unnest(v_arm) as a)
        || '), '
        'collapsed as ( '
        '  select distinct on (arms.entity_type, arms.item_id) '
        '         arms.entity_type, arms.item_id, arms.representation_type, '
        '         arms.chunk_index, arms.chunk_text, arms.semantic_distance '
        '    from arms '
        '   order by arms.entity_type, arms.item_id, '
        '            arms.semantic_distance asc nulls last, '
        '            case when arms.representation_type = ''prose'' then 0 else 1 end, '
        '            arms.chunk_index asc '
        ') '
        'select collapsed.entity_type, collapsed.item_id, '
        '       case collapsed.entity_type '
        '         when ''resource'' then r.kind '
        '         when ''message'' then ''message''::text '
        '         when ''distillation'' then ''distillation''::text '
        '         else null::text end as kind, '
        '       collapsed.representation_type, collapsed.chunk_index, '
        '       left(coalesce(collapsed.chunk_text, ''''), 512) as matched_snippet, '
        '       collapsed.semantic_distance, '
        '       row_number() over ( '
        '           order by collapsed.semantic_distance asc nulls last, '
        '                    collapsed.entity_type asc, collapsed.item_id asc '
        '       )::integer as semantic_rank, '
        '       coalesce(r.created_at, m.created_at, d.created_at) as created_at '
        '  from collapsed '
        '  left join public.external_resources r '
        '         on collapsed.entity_type = ''resource'' and r.id::text = collapsed.item_id '
        '  left join public.discord_messages m '
        '         on collapsed.entity_type = ''message'' and m.message_id::text = collapsed.item_id '
        '  left join public.distillations d '
        '         on collapsed.entity_type = ''distillation'' and d.id::text = collapsed.item_id '
        ' order by collapsed.semantic_distance asc nulls last, '
        '          collapsed.entity_type asc, collapsed.item_id asc '
        ' limit ' || v_limit::text;

    return query execute v_sql using p_query_embedding;
end;
$$;

comment on function public.hivemind_semantic_candidates(vector, integer, text[], text[]) is
  'Task 2.16 (upgrades the task-2.15 canonical surface): STABLE, SECURITY DEFINER '
  'with a fixed trusted search_path. One duplicate-free cosine-ranked identity '
  'stream over the SELECTED full-literal 384-d contract (frozen '
  '1360541028304258884), collapsed to one best chunk per (entity_type, item_id) '
  'across representations (frozen tie-break: closest distance; tie -> prose before '
  'workflow_python; tie -> chunk_index asc). SELECTED-LITERAL FAIL-CLOSED GATE: '
  'when the active 384-d contract is not the selected full literal, returns ZERO '
  'rows (never serves an unselected/ambiguous contract or under a mismatched '
  'partial index). Each vector arm is a PARENTHESIZED UNION branch exposing an '
  'indexable kNN (ORDER BY embedding <=> query LIMIT v_overfetch) over the '
  'selected-literal partial HNSW index for BROAD plans, and the source join AND '
  'EVERY eligibility/filter predicate live INSIDE that branch (C1: the over-fetch '
  'bound counts only eligible rows; with hnsw.iterative_scan = strict_order a '
  'nearer deleted/rejected/quarantined/wrong-kind vector is filtered before it '
  'can crowd out a farther valid entity). EXACT MODE: setting the custom GUC '
  'hivemind.semantic_exact_mode = on in a fresh, index-scan-disabled session '
  'NULLs the per-arm bound so the COMPLETE applicable cohort is scored (C2: a '
  'true exact baseline, byte-identical in semantics to schema/032, sharing this '
  'one template with ANN mode). item-id-scoped arms also use LIMIT NULL (full '
  'eligible scan, btree/pk path, never distance-truncated). Eligible-source-only; Snowflake '
  'item_ids are text throughout. SECURITY unchanged from 032: non-empty item_ids '
  'that do not resolve to exactly one entity FAIL CLOSED; a workflow_python vector '
  'ranks only when the current resource kind is workflow and '
  'hivemind_workflow_python_state = safe. Internal surface: revoked from '
  'PUBLIC/anon/authenticated, granted only to service_role. Read-only.';

-- ------------------------------------------------------------
-- Trust boundary: identical to 032. Revoke direct execution from everyone
-- except the owner, then grant ONLY to service_role where it exists. The
-- revoke/grant are conditional because anon/authenticated/service_role do not
-- exist on a vanilla rehearsal cluster. (CREATE OR REPLACE preserves the
-- existing grants, but these are re-asserted so the posture is explicit and
-- independent of the 032 apply.)
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
-- Verification (read-only): helper literal, function volatility / search_path
-- / security / grants, and that the selected literal != the dimension-only id.
-- ---------------------------------------------------------------------------
select
    public.hivemind_selected_contract_id(
        'openai','text-embedding-3-small',384,1,2,
        'chunk_config'||E'\x1f'||'v1'||E'\x1f'||'prose#512/50'||E'\x1f'||'workflow_python#512/50'
    ) as selected_contract_id,
    p.proname                                            as function_name,
    pg_get_function_arguments(p.oid)                     as arguments,
    p.provolatile = 's'                                  as is_stable,
    p.prosecdef                                          as security_definer,
    p.proconfig                                          as config,
    (select array_agg(acl.grantee order by acl.grantee)
       from aclexplode(p.proacl) as acl)                 as grantees
from pg_catalog.pg_proc p
join pg_catalog.pg_namespace n on n.oid = p.pronamespace
where n.nspname = 'public'
  and p.proname in ('hivemind_semantic_candidates', 'hivemind_selected_contract_id')
order by p.proname;
