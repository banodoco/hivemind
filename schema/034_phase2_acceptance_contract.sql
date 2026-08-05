-- ============================================================
-- Hivemind — Phase 2 / Task 2.17 — selected-contract lifecycle acceptance
-- ============================================================
--
-- A monotonic, additive migration AFTER 033 (the task-2.16 selected-contract
-- HNSW surface). It closes the task-2.17 production defects WITHOUT editing the
-- accepted migrations 020-033: it ADDS a canonical chunk-manifest table and
-- OVERRIDES (create or replace) four functions that 026/027/029 own. The legacy
-- bodies remain the shipped behavior for any cluster that has not applied 034
-- (the accepted lifecycle rehearsal applies only 020-029, so it is unaffected).
--
-- What this migration closes (preflight "Known defects that must be closed"):
--
--   D1 — payload python_public_state. The legacy hivemind_embedding_payload
--        (027) reads payload.python_source without requiring
--        payload.python_public_state='safe', so the incremental worker could
--        embed quarantined Python. This override reads a canonical chunk
--        MANIFEST and never returns workflow_python chunks unless the current
--        public state is 'safe' (and kind='workflow', source available).
--
--   D2/D4 — stale vectors + hardcoded config. The zero-chunk path used to leave
--        prior vectors in place; the worker hardcoded 384/512/50. This override
--        binds the payload to the selected 512/50 config (fail-closed otherwise)
--        and the new hivemind_finalize_embedding_job DROPS stale vectors for an
--        unavailable/quarantined/reclassified representation before completing.
--
--   D3 — evaluator/backfill/worker chunker parity. The legacy worker chunked in
--        SQL (hivemind_chunk_text line-window) while backfill used the frozen
--        AST chunker; 512/50 equality is not byte parity, and the SQL chunker
--        never split an oversized line. This override makes the payload read
--        ONE canonical chunk manifest produced by the frozen Python chunker
--        (executors.chunking -> workflow_representation), so backfill and the
--        worker embed byte-identical chunks by construction (single source of
--        truth). The frozen chunker's oversized-line handling is enforced here
--        because the manifest is produced by it.
--
--   D5 — source-hash-safe commit + crash recovery. Vector authority is made
--        conditional on the exact source representation hash + public state +
--        selected contract via the transactional hivemind_finalize_embedding_job
--        (verify lease -> verify contract -> recompute current source -> replace
--        OR drop OR requeue). A source change after payload generation cannot
--        make stale chunks authoritative; a crash after upsert/before complete
--        recovers idempotently because replace+complete are one transaction.
--
--   D6 — historical stale-Python. hivemind_cleanup_ineligible_embeddings is
--        overridden to also drop workflow_python vectors whose source is now
--        quarantined/unavailable, and hivemind_finalize_embedding_job drops a
--        representation's prior vectors whenever its Python disappeared.
--
--   D7 — the worker-core refactor that consumes this surface lives in
--        supabase/functions/embedding-worker/core.ts (importable, injected); it
--        is exercised by the task-2.17 Deno tests and by the Python rehearsal
--        which drives the SAME claim -> payload -> embed -> finalize protocol.
--
-- Selected-contract binding. Every override here is keyed on the ONE selected
-- full-literal contract 1360541028304258884 (the task-2.16 frozen literal that
-- embeds the chunk-config disambiguation the dimension-only bigint lacks). The
-- payload refuses any non-(512/50) target/overlap; finalize refuses any job
-- whose contract_id is not the selected literal; cleanup is scoped to the
-- active contract. No 1536 path is activated.
--
-- Additive + idempotent. Requires schema/003 + schema/020-033. No source row is
-- mutated; only regenerable vectors + the canonical chunk manifest.
--
-- APPLY:    psql "$HIVEMIND_DB_URL" -f schema/034_phase2_acceptance_contract.sql
-- ROLLBACK: drop function if exists hivemind_finalize_embedding_job(bigint,text,jsonb,text,text,boolean);
--           drop function if exists hivemind_current_representation(text,text,text);
--           drop table if exists content_representation_manifest;
--           then re-apply 026/027/029 to restore the legacy claim/payload/cleanup.
-- ============================================================

do $$
begin
    if not exists (select 1 from pg_catalog.pg_tables
                    where schemaname='public' and tablename='content_embeddings') then
        raise exception 'prerequisite guard: content_embeddings not found — apply schema/022 first';
    end if;
    if not exists (select 1 from pg_catalog.pg_proc p
                    join pg_catalog.pg_namespace n on n.oid=p.pronamespace
                   where n.nspname='public' and p.proname='hivemind_embedding_payload') then
        raise exception 'prerequisite guard: hivemind_embedding_payload not found — apply schema/027 first';
    end if;
    if not exists (select 1 from pg_catalog.pg_proc p
                    join pg_catalog.pg_namespace n on n.oid=p.pronamespace
                   where n.nspname='public' and p.proname='hivemind_selected_contract_id') then
        raise exception 'prerequisite guard: hivemind_selected_contract_id not found — apply schema/033 (task 2.16) first';
    end if;
    if not exists (select 1 from pg_catalog.pg_proc p
                    join pg_catalog.pg_namespace n on n.oid=p.pronamespace
                   where n.nspname='public' and p.proname='hivemind_workflow_python_state') then
        raise exception 'prerequisite guard: hivemind_workflow_python_state not found — apply schema/003 first';
    end if;
end $$;

-- ------------------------------------------------------------
-- (0) True Python<->SQL normalization parity for the source-hash gate.
-- schema/024's hivemind_normalize_for_hash uses btrim(), which strips only
-- SPACES — so a representation ending in a newline (every Python source) kept
-- that newline and hashed differently than Python's normalize_for_hash (.strip
-- removes ALL whitespace). That trailing-whitespace gap broke the source-hash
-- gate (hivemind_current_representation vs the Python-computed manifest hash).
-- This override trims the full whitespace set like Python's .strip(), so on
-- ASCII/NFC text hivemind_representation_hash == the Python representation_hash
-- byte-for-byte (the parity 024 claimed). Same signature; layering, not an edit
-- to 024. Only in effect once 034 is applied (the lifecycle rehearsal, which
-- applies only 020-029, keeps 024's shipped helper).
-- ------------------------------------------------------------
create or replace function public.hivemind_normalize_for_hash(p_text text)
returns text
language sql
immutable
as $$
    with fixed as (
        select regexp_replace(regexp_replace(coalesce(p_text, ''), E'\r\n', E'\n', 'g'),
                              E'\r', E'\n', 'g') as t
    ),
    lines as (
        select regexp_replace(ln, '[ \t\r\f\v]+$', '') as ln
          from fixed, unnest(string_to_array(t, E'\n')) as ln
    )
    select trim(both E' \t\n\r\f\v' from string_agg(ln, E'\n')) from lines
$$;

comment on function public.hivemind_normalize_for_hash(text) is
    'Task 2.17 override: trims the full whitespace set (like Python .strip), so '
    'hivemind_representation_hash matches executors.workflow_representation.'
    'representation_hash on ASCII/NFC text — the parity 024 intended.';

-- ------------------------------------------------------------
-- (A) Canonical chunk manifest — the ONE source of truth for chunks.
-- Produced solely by the frozen Python chunker (executors.chunking). Both the
-- historical backfill and the incremental worker embed from these rows, so chunk
-- identity (count/index/text/hash/method) is byte-identical across the
-- backfill -> worker boundary by construction. Quarantined/unavailable Python
-- is recorded here as a state row with NO chunk bytes (it never crosses the
-- provider/vector boundary).
-- ------------------------------------------------------------
create table if not exists public.content_representation_manifest (
    entity_type         text not null,
    item_id             text not null,
    representation_type text not null,
    representation_hash text not null,
    source_available    boolean not null,
    public_state        text not null default 'safe',
    unavailable_reason  text,
    chunking_version    integer not null,
    chunk_config_identity text not null,
    chunk_method        text,
    chunks              jsonb not null default '[]'::jsonb,
    chunk_count         integer not null default 0,
    updated_at          timestamptz not null default now(),
    constraint content_representation_manifest_pk primary key (entity_type, item_id, representation_type),
    constraint content_representation_manifest_state_chk
        check (public_state in ('safe','quarantined'))
);

comment on table public.content_representation_manifest is
    'Task 2.17: the canonical chunk manifest. Sole producer is the frozen Python '
    'chunker (executors.chunking); both backfill and the embedding worker embed from '
    'these rows so chunk identity is byte-identical across the boundary. Quarantined/'
    'unavailable Python is a state-only row (no chunk bytes).';

-- ------------------------------------------------------------
-- (A2) Full selected configuration registry.
--
-- embedding_contracts.id remains the historical dimension-only compatibility
-- identity.  It is deliberately not used as the selected-contract identity:
-- both 384-small and 384-large share that bigint.  This registry is the one
-- machine-readable configuration surface consumed by the Phase-2 paths.  The
-- row is seeded as draft metadata only; this migration never activates it.
-- ------------------------------------------------------------
create table if not exists public.embedding_contract_config_registry (
    selected_contract_id       bigint primary key,
    base_contract_id           bigint not null,
    provider                   text not null,
    model                      text not null,
    dimension                  integer not null check (dimension > 0),
    canonicalization_version   integer not null check (canonicalization_version > 0),
    chunking_version           integer not null check (chunking_version > 0),
    chunk_config_version       integer not null check (chunk_config_version > 0),
    prose_target_tokens        integer not null check (prose_target_tokens > 0),
    prose_overlap_tokens       integer not null check (prose_overlap_tokens >= 0 and prose_overlap_tokens < prose_target_tokens),
    python_target_tokens       integer not null check (python_target_tokens > 0),
    python_overlap_tokens      integer not null check (python_overlap_tokens >= 0 and python_overlap_tokens < python_target_tokens),
    chunk_config_identity      text not null,
    eval_contract_id           text not null,
    full_config_sha256         text not null check (full_config_sha256 ~ '^[0-9a-f]{64}$'),
    selection_state             text not null default 'selected_draft'
                               check (selection_state in ('selected_draft','evaluation_only','active')),
    created_at                 timestamptz not null default now(),
    updated_at                 timestamptz not null default now(),
    constraint embedding_contract_config_registry_identity_unique
        unique (provider, model, dimension, canonicalization_version,
                chunking_version, chunk_config_version, chunk_config_identity),
    constraint embedding_contract_config_registry_selected_values_chk check (
        selected_contract_id = 1360541028304258884
        and base_contract_id = 6368594834396668537
        and provider = 'openai'
        and model = 'text-embedding-3-small'
        and dimension = 384
        and canonicalization_version = 1
        and chunking_version = 2
        and chunk_config_version = 1
        and prose_target_tokens = 512
        and prose_overlap_tokens = 50
        and python_target_tokens = 512
        and python_overlap_tokens = 50
        and chunk_config_identity = 'chunk_config'||E'\x1f'||'v1'||E'\x1f'||'prose#512/50'||E'\x1f'||'workflow_python#512/50'
        and eval_contract_id = '12e19cdb566b8744'
        and full_config_sha256 = '12e19cdb566b87445ab2d3563e6cb948f58801f78f8395878fc9e0c2457d5462'
    )
);

insert into public.embedding_contract_config_registry (
    selected_contract_id, base_contract_id, provider, model, dimension,
    canonicalization_version, chunking_version, chunk_config_version,
    prose_target_tokens, prose_overlap_tokens, python_target_tokens,
    python_overlap_tokens, chunk_config_identity, eval_contract_id,
    full_config_sha256, selection_state
) values (
    1360541028304258884, 6368594834396668537, 'openai', 'text-embedding-3-small', 384,
    1, 2, 1, 512, 50, 512, 50,
    'chunk_config'||E'\x1f'||'v1'||E'\x1f'||'prose#512/50'||E'\x1f'||'workflow_python#512/50',
    '12e19cdb566b8744',
    '12e19cdb566b87445ab2d3563e6cb948f58801f78f8395878fc9e0c2457d5462',
    'selected_draft'
) on conflict (selected_contract_id) do update set
    base_contract_id = excluded.base_contract_id,
    provider = excluded.provider,
    model = excluded.model,
    dimension = excluded.dimension,
    canonicalization_version = excluded.canonicalization_version,
    chunking_version = excluded.chunking_version,
    chunk_config_version = excluded.chunk_config_version,
    prose_target_tokens = excluded.prose_target_tokens,
    prose_overlap_tokens = excluded.prose_overlap_tokens,
    python_target_tokens = excluded.python_target_tokens,
    python_overlap_tokens = excluded.python_overlap_tokens,
    chunk_config_identity = excluded.chunk_config_identity,
    eval_contract_id = excluded.eval_contract_id,
    full_config_sha256 = excluded.full_config_sha256,
    -- Never downgrade an already-active row and never activate a draft row as
    -- a side effect of this migration. Activation remains an explicit rollout.
    selection_state = case when embedding_contract_config_registry.selection_state = 'active'
                           then 'active' else excluded.selection_state end,
    updated_at = now();

create or replace function public.hivemind_selected_contract_config()
returns table (
    selected_contract_id bigint,
    base_contract_id bigint,
    provider text,
    model text,
    dimension integer,
    canonicalization_version integer,
    chunking_version integer,
    chunk_config_version integer,
    prose_target_tokens integer,
    prose_overlap_tokens integer,
    python_target_tokens integer,
    python_overlap_tokens integer,
    chunk_config_identity text,
    eval_contract_id text,
    full_config_sha256 text,
    selection_state text
)
language sql
stable
security definer
set search_path = public, pg_temp
as $$
    select c.selected_contract_id, c.base_contract_id, c.provider, c.model,
           c.dimension, c.canonicalization_version, c.chunking_version,
           c.chunk_config_version, c.prose_target_tokens, c.prose_overlap_tokens,
           c.python_target_tokens, c.python_overlap_tokens,
           c.chunk_config_identity, c.eval_contract_id, c.full_config_sha256,
           c.selection_state
      from public.embedding_contract_config_registry c
     where c.selected_contract_id = 1360541028304258884
       and c.selection_state in ('selected_draft','active');
$$;

comment on table public.embedding_contract_config_registry is
    'Task 2.17 canonical full selected configuration identity. The legacy '
    'embedding_contracts bigint remains dimension-only and insufficient.';

comment on function public.hivemind_selected_contract_config() is
    'Task 2.17 canonical selected config lookup. Returns zero rows on registry '
    'drift or non-selected state; never activates production.';

-- ------------------------------------------------------------
-- (B) Recompute the CURRENT source representation hash + availability + state.
-- Mirrors executors.canonical_representations / workflow_representation on
-- ASCII/NFC text (same caveat as schema/024: no core-PG NFC, so the rehearsal
-- fixtures are ASCII). Used by hivemind_finalize_embedding_job for the
-- source-hash gate and by the payload when a manifest row is absent.
-- ------------------------------------------------------------
create or replace function public.hivemind_current_representation(
    p_entity_type text,
    p_item_id text,
    p_representation_type text
) returns table (
    representation_hash text,
    source_available boolean,
    public_state text,
    kind_ok boolean
)
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $$
declare
    v_text text := '';
    v_state text := null;
    v_avail boolean := false;
    v_kind_ok boolean := true;
begin
    if p_entity_type = 'message' then
        select coalesce(m.content,'') into v_text
          from public.discord_messages m
         where m.message_id = p_item_id::bigint
           and coalesce(m.is_deleted,false) = false;
        v_avail := found and btrim(v_text) <> '';

    elsif p_entity_type = 'distillation' then
        select public.hivemind_canonical_distillation_text(d.question,d.conditions,d.answer) into v_text
          from public.distillations d
         where d.id = p_item_id::bigint;
        v_avail := found and btrim(v_text) <> '';

    elsif p_entity_type = 'resource' and p_representation_type = 'prose' then
        select public.hivemind_canonical_resource_text(
                   r.title,
                   public.hivemind_workflow_prose(r.body,r.kind),
                   public.hivemind_resource_tags(r.metadata)||' '||public.hivemind_workflow_semantics_text(r.metadata))
          into v_text
          from public.external_resources r
         where r.id = p_item_id::bigint;
        v_avail := found and btrim(v_text) <> '';

    elsif p_entity_type = 'resource' and p_representation_type = 'workflow_python' then
        select coalesce(r.payload->>'python_source',''),
               (r.kind = 'workflow'),
               coalesce(public.hivemind_workflow_python_state(r.id),'safe')
          into v_text, v_kind_ok, v_state
          from public.external_resources r
         where r.id = p_item_id::bigint;
        if not found then
            v_kind_ok := false;
            v_avail := false;
        else
            -- v_kind_ok is already (kind='workflow'); embeddable only when safe.
            v_avail := v_kind_ok and btrim(v_text) <> '' and v_state = 'safe';
        end if;
    end if;

    if p_representation_type = 'workflow_python' and not exists (
        select 1 from public.embedding_contract_config_registry c
         where c.selected_contract_id = 1360541028304258884
           and c.selection_state in ('selected_draft','active')
           and c.chunk_config_identity = 'chunk_config'||E'\x1f'||'v1'||E'\x1f'||'prose#512/50'||E'\x1f'||'workflow_python#512/50'
    ) then
        v_kind_ok := false;
        v_avail := false;
    end if;

    if v_avail and btrim(v_text) <> '' then
        return query select public.hivemind_representation_hash(v_text), true, v_state, v_kind_ok;
        return;
    end if;
    -- Not embeddable now. Still report the hash of whatever text exists (so a
    -- finalize that expected a now-gone source sees a mismatch) and the state.
    return query select case when btrim(coalesce(v_text,'')) <> ''
                             then public.hivemind_representation_hash(v_text) else ''::text end,
                        false, v_state, v_kind_ok;
end;
$$;

comment on function public.hivemind_current_representation(text,text,text) is
    'Task 2.17: recompute the CURRENT source representation hash + availability + '
    'public state. The source-hash gate for hivemind_finalize_embedding_job. Mirrors '
    'the Python canonicalizer on ASCII/NFC text (schema/024 caveat).';

-- ------------------------------------------------------------
-- (C) Override hivemind_embedding_payload: read the canonical chunk manifest,
-- state/availability/contract gated. (Closes D1, D3, D4.)
--
-- The return shape gains a public_state column, so the legacy OUT-parameter
-- row type differs; PostgreSQL requires an explicit DROP before CREATE OR
-- REPLACE in that case. The DROP is signature-pinned to the legacy 027 shape.
-- ------------------------------------------------------------
drop function if exists public.hivemind_embedding_payload(text,text,text,int,int);

create or replace function public.hivemind_embedding_payload(
    p_entity_type text,
    p_item_id text,
    p_representation_type text,
    p_target_tokens int default 512,
    p_overlap_tokens int default 50
) returns table (
    representation_hash text,
    source_available boolean,
    public_state text,
    chunk_index int,
    chunk_text text,
    chunk_hash text,
    method text
)
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    m record;
    v_cur_hash text;
    v_cur_avail boolean;
    v_cur_state text;
    v_cur_kind_ok boolean;
begin
    -- Selected config only (D4): refuse any non-(512/50) target/overlap.
    if p_target_tokens <> 512 or p_overlap_tokens <> 50 then
        return query select ''::text, false, null::text, null::int, null::text, null::text, null::text;
        return;
    end if;

    select * into m
      from public.content_representation_manifest
     where entity_type = p_entity_type
       and item_id = p_item_id
       and representation_type = p_representation_type;

    if not found or m.chunking_version <> 2 or m.chunk_config_identity <>
        'chunk_config'||E'\x1f'||'v1'||E'\x1f'||'prose#512/50'||E'\x1f'||'workflow_python#512/50'
       or not exists (select 1 from public.embedding_contract_config_registry c
                       where c.selected_contract_id = 1360541028304258884
                         and c.base_contract_id = 6368594834396668537
                         and c.provider = 'openai'
                         and c.model = 'text-embedding-3-small'
                         and c.dimension = 384
                         and c.canonicalization_version = 1
                         and c.chunking_version = 2
                         and c.chunk_config_version = 1
                         and c.prose_target_tokens = 512 and c.prose_overlap_tokens = 50
                         and c.python_target_tokens = 512 and c.python_overlap_tokens = 50
                         and c.chunk_config_identity = 'chunk_config'||E'\x1f'||'v1'||E'\x1f'||'prose#512/50'||E'\x1f'||'workflow_python#512/50'
                         and c.eval_contract_id = '12e19cdb566b8744'
                         and c.full_config_sha256 = '12e19cdb566b87445ab2d3563e6cb948f58801f78f8395878fc9e0c2457d5462'
                         and c.selection_state in ('selected_draft','active')) then
        -- No manifest row: there is no byte-parity chunk guarantee, so fail
        -- closed (never embed from a re-chunked source). Remediation populates
        -- the manifest; until it does, the representation is not embeddable.
        return query select ''::text, false, null::text, null::int, null::text, null::text, null::text;
        return;
    end if;

    -- The manifest is a cache, not source authority. Recompute the current
    -- source identity before returning even one chunk; a stale manifest must
    -- cause a no-provider-call requeue at finalize, never an embedding request.
    select cr.representation_hash, cr.source_available, cr.public_state, cr.kind_ok
      into v_cur_hash, v_cur_avail, v_cur_state, v_cur_kind_ok
      from public.hivemind_current_representation(
          p_entity_type, p_item_id, p_representation_type) as cr;
    if m.representation_hash is distinct from v_cur_hash
       or m.source_available is distinct from v_cur_avail
       or (p_representation_type = 'workflow_python'
           and (m.public_state is distinct from v_cur_state or not v_cur_kind_ok)) then
        return query select coalesce(v_cur_hash,''), false,
                            case when p_representation_type = 'workflow_python' then v_cur_state else null end,
                            null::int, null::text, null::text, null::text;
        return;
    end if;

    -- D1/D2: never return workflow_python chunks unless current state is 'safe'.
    if (not m.source_available)
       or (p_representation_type = 'workflow_python' and m.public_state <> 'safe') then
        return query select ''::text, false,
                            case when p_representation_type = 'workflow_python' then m.public_state else null end,
                            null::int, null::text, null::text, null::text;
        return;
    end if;

    return query
        select m.representation_hash, true,
               case when p_representation_type = 'workflow_python' then m.public_state else null end,
               (c ->> 'chunk_index')::int,
               c ->> 'chunk_text',
               c ->> 'chunk_hash',
               m.chunk_method
          from jsonb_array_elements(m.chunks) as c;
end;
$$;

comment on function public.hivemind_embedding_payload(text,text,text,int,int) is
    'Task 2.17 override: canonical chunks from the manifest, selected (512/50) '
    'config only, workflow_python gated on public_state=safe. Fails closed when '
    'no manifest row exists (no byte-parity guarantee).';

-- ------------------------------------------------------------
-- (D) Source-hash-safe atomic commit. (Closes D2, D5, D6.)
-- One transaction: verify lease -> verify selected contract -> recompute current
-- source -> atomic replace OR drop OR requeue -> complete. A source change after
-- payload generation cannot make stale chunks authoritative; a crash after the
-- replace recovers idempotently because replace+complete are one statement block.
-- ------------------------------------------------------------
create or replace function public.hivemind_finalize_embedding_job(
    p_job_id bigint,
    p_worker_id text,
    p_chunks jsonb,
    p_expected_representation_hash text,
    p_expected_public_state text,
    p_expected_source_available boolean
) returns table (
    chunks_written int,
    outcome text,
    job_status text
)
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_status text;
    v_locked text;
    v_et text;
    v_id text;
    v_rep text;
    v_contract bigint;
    v_kind text;
    v_cur_hash text;
    v_cur_avail boolean;
    v_cur_state text;
    v_kind_ok boolean;
    v_n int := 0;
    c jsonb;
    v_embed text;
    v_manifest jsonb;
    v_expected_count int;
begin
    select j.status, j.locked_by, j.entity_type, j.item_id, j.representation_type,
           j.contract_id, j.job_kind
      into v_status, v_locked, v_et, v_id, v_rep, v_contract, v_kind
      from public.embedding_jobs j
     where j.id = p_job_id;

    -- (1) lease/worker gate.
    if not found or v_status <> 'processing' or v_locked is distinct from p_worker_id then
        return query select 0, 'not_processing'::text, coalesce(v_status,'missing')::text;
        return;
    end if;

    -- (2) selected-contract gate (defense in depth; the worker pre-gates).
    if v_contract is distinct from 1360541028304258884 then
        update public.embedding_jobs
           set status='failed', last_error=public.hivemind_sanitize_error('contract_mismatch'),
               locked_by=null, locked_at=null, lease_expires_at=null, updated_at=now()
         where id = p_job_id;
        return query select 0, 'contract_mismatch'::text, 'failed'::text;
        return;
    end if;

    -- The selected bigint is insufficient by itself.  Require the complete
    -- registry identity before any drop, replacement, or completion action.
    if not exists (
        select 1 from public.embedding_contract_config_registry c
         where c.selected_contract_id = v_contract
           and c.base_contract_id = 6368594834396668537
           and c.provider = 'openai'
           and c.model = 'text-embedding-3-small'
           and c.dimension = 384
           and c.canonicalization_version = 1
           and c.chunking_version = 2
           and c.chunk_config_version = 1
           and c.prose_target_tokens = 512 and c.prose_overlap_tokens = 50
           and c.python_target_tokens = 512 and c.python_overlap_tokens = 50
           and c.chunk_config_identity = 'chunk_config'||E'\x1f'||'v1'||E'\x1f'||'prose#512/50'||E'\x1f'||'workflow_python#512/50'
           and c.eval_contract_id = '12e19cdb566b8744'
           and c.full_config_sha256 = '12e19cdb566b87445ab2d3563e6cb948f58801f78f8395878fc9e0c2457d5462'
           and c.selection_state in ('selected_draft','active')
    ) then
        update public.embedding_jobs
           set status='failed', last_error=public.hivemind_sanitize_error('contract_config_missing'),
               locked_by=null, locked_at=null, lease_expires_at=null, updated_at=now()
         where id = p_job_id;
        return query select 0, 'contract_mismatch'::text, 'failed'::text;
        return;
    end if;

    -- (3) drop job kind: drop then complete.
    if v_kind = 'drop' then
        perform public.hivemind_drop_embedding_chunks(v_contract, v_et, v_id, v_rep);
        update public.embedding_jobs
           set status='done', locked_by=null, locked_at=null, lease_expires_at=null,
               last_error=null, updated_at=now()
         where id = p_job_id;
        return query select 0, 'dropped'::text, 'done'::text;
        return;
    end if;

    -- (4) recompute CURRENT source representation + state.
    select * into v_cur_hash, v_cur_avail, v_cur_state, v_kind_ok
      from public.hivemind_current_representation(v_et, v_id, v_rep);

    -- (5) ineligible now (unavailable / quarantined / reclassified): DROP stale
    --     vectors for this representation, then complete. (D2/D6)
    if (not v_cur_avail)
       or (v_rep = 'workflow_python'
           and ((not v_kind_ok) or v_cur_state <> 'safe')) then
        delete from public.content_embeddings
         where contract_id = v_contract and entity_type = v_et
           and item_id = v_id and representation_type = v_rep;
        update public.embedding_jobs
           set status='done', locked_by=null, locked_at=null, lease_expires_at=null,
               last_error=null, updated_at=now()
         where id = p_job_id;
        return query select 0, 'dropped'::text, 'done'::text;
        return;
    end if;

    -- Availability is an expected-source assertion.  A source becoming
    -- available after an empty payload must be requeued, never completed as an
    -- empty success; a source disappearing follows the drop path above.
    if p_expected_source_available is distinct from v_cur_avail then
        update public.embedding_jobs
           set status='pending', next_attempt_at=now(),
               locked_by=null, locked_at=null, lease_expires_at=null, updated_at=now()
         where id = p_job_id;
        return query select 0, 'source_changed'::text, 'pending'::text;
        return;
    end if;

    -- (6) source-hash / state gate (D5): a source change after payload generation
    --     must NOT let stale chunks become authoritative -> requeue fresh work.
    if (p_expected_representation_hash is distinct from v_cur_hash)
       or (p_expected_public_state is distinct from v_cur_state) then
        update public.embedding_jobs
           set status='pending', next_attempt_at=now(),
               locked_by=null, locked_at=null, lease_expires_at=null, updated_at=now()
         where id = p_job_id;
        return query select 0, 'source_changed'::text, 'pending'::text;
        return;
    end if;

    select m.chunks, m.chunk_count into v_manifest, v_expected_count
      from public.content_representation_manifest m
     where m.entity_type = v_et and m.item_id = v_id
       and m.representation_type = v_rep
       and m.representation_hash = v_cur_hash
       and m.source_available = true
       and m.chunking_version = 2
       and m.chunk_config_identity = 'chunk_config'||E'\x1f'||'v1'||E'\x1f'||'prose#512/50'||E'\x1f'||'workflow_python#512/50';
    if not found then
        update public.embedding_jobs
           set status='pending', next_attempt_at=now(),
               locked_by=null, locked_at=null, lease_expires_at=null, updated_at=now()
         where id = p_job_id;
        return query select 0, 'source_changed'::text, 'pending'::text;
        return;
    end if;

    -- (7) validate + atomic replace. Wrong dimension raises on the vector(384)
    --     cast; non-finite is rejected by an explicit guard; everything happens
    --     BEFORE the delete so a bad payload cannot cause a partial write.
    if p_chunks is null
       or jsonb_typeof(p_chunks) <> 'array'
       or jsonb_array_length(p_chunks) = 0 then
        update public.embedding_jobs
           set status='failed', last_error=public.hivemind_sanitize_error('empty_chunks'),
               locked_by=null, locked_at=null, lease_expires_at=null, updated_at=now()
         where id = p_job_id;
        return query select 0, 'validation_failed'::text, 'failed'::text;
        return;
    end if;

    -- Validate every chunk BEFORE the delete: missing embedding, non-finite
    -- values, or a wrong-dimension literal (vector(384) cast raises) all fail
    -- closed here, deterministically, with no partial write.
    begin
        if jsonb_array_length(p_chunks) <> v_expected_count then
            raise exception 'chunk_count_mismatch';
        end if;
        if exists (
            select 1 from jsonb_array_elements(p_chunks) x
             where coalesce(x->>'entity_type','') <> v_et
                or coalesce(x->>'item_id','') <> v_id
                or coalesce(x->>'representation_type','') <> v_rep
                or coalesce(x->>'contract_id','') <> v_contract::text
                or coalesce(x->>'representation_hash','') <> v_cur_hash
                or coalesce(x->>'chunk_index','') !~ '^[0-9]+$'
                or coalesce(x->>'chunk_hash','') !~ '^[0-9a-f]{64}$'
                or coalesce(x->>'chunk_text','') = ''
        ) then
            raise exception 'chunk_identity_mismatch';
        end if;
        if exists (
            select 1 from jsonb_array_elements(p_chunks) x
             group by (x->>'chunk_index')::int having count(*) <> 1
        ) then
            raise exception 'duplicate_chunk_index';
        end if;
        if exists (
            select 1 from generate_series(0, v_expected_count - 1) expected(i)
             where not exists (
                 select 1 from jsonb_array_elements(p_chunks) x
                  where (x->>'chunk_index')::int = expected.i
             )
        ) then
            raise exception 'non_contiguous_chunk_indexes';
        end if;
        for c in select * from jsonb_array_elements(p_chunks) loop
            v_embed := coalesce(c ->> 'embedding', '');
            if v_embed = '' then
                raise exception 'missing_embedding';
            end if;
            if v_embed ~* 'nan' or v_embed ~* 'inf' then
                raise exception 'non_finite_vector';
            end if;
            if (c ->> 'chunk_hash') <> public.hivemind_representation_hash(c ->> 'chunk_text') then
                raise exception 'chunk_hash_mismatch';
            end if;
            if not exists (
                select 1 from jsonb_array_elements(v_manifest) expected
                 where expected->>'chunk_index' = c->>'chunk_index'
                   and expected->>'chunk_hash' = c->>'chunk_hash'
                   and expected->>'chunk_text' = c->>'chunk_text'
            ) then
                raise exception 'manifest_chunk_mismatch';
            end if;
            -- raises on a wrong-dimension literal (e.g. a 1536-d vector).
            perform v_embed::vector(384);
        end loop;
    exception when others then
        update public.embedding_jobs
           set status='failed', last_error=public.hivemind_sanitize_error('vector_validation_failed'),
               locked_by=null, locked_at=null, lease_expires_at=null, updated_at=now()
         where id = p_job_id;
        return query select 0, 'validation_failed'::text, 'failed'::text;
        return;
    end;

    delete from public.content_embeddings
     where contract_id = v_contract and entity_type = v_et
       and item_id = v_id and representation_type = v_rep;

    for c in select * from jsonb_array_elements(p_chunks) loop
        insert into public.content_embeddings (
            contract_id, entity_type, item_id, representation_type, chunk_index,
            chunk_text, embedding, representation_hash, chunk_hash
        ) values (
            v_contract, v_et, v_id, v_rep, (c ->> 'chunk_index')::int,
            left(coalesce(c ->> 'chunk_text',''), 1024),
            (c ->> 'embedding')::vector,
            c ->> 'representation_hash', c ->> 'chunk_hash'
        );
        v_n := v_n + 1;
    end loop;

    update public.embedding_jobs
       set status='done', locked_by=null, locked_at=null, lease_expires_at=null,
           last_error=null, updated_at=now()
     where id = p_job_id;
    return query select v_n, 'completed'::text, 'done'::text;
end;
$$;

comment on function public.hivemind_finalize_embedding_job(bigint,text,jsonb,text,text,boolean) is
    'Task 2.17: source-hash-safe atomic commit. Verifies lease + selected contract, '
    'recomputes current source, then atomically replaces OR drops OR requeues + '
    'completes in one transaction. Wrong-dimension/non-finite/empty payloads fail '
    'closed before any write. Outcome in {completed,dropped,source_changed,'
    'contract_mismatch,validation_failed,not_processing}.';

-- ------------------------------------------------------------
-- (E) Override hivemind_cleanup_ineligible_embeddings: ALSO drop workflow_python
-- vectors whose source is now quarantined/unavailable (representation-scoped;
-- prose + other items + other contracts + run/remediation records untouched).
-- (Closes D6.)
-- ------------------------------------------------------------
create or replace function public.hivemind_cleanup_ineligible_embeddings(
    p_batch_size int default 1000
) returns int
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_active bigint;
    v_count int := 0;
    v_removed int;
begin
    v_active := public.hivemind_active_contract_id();
    if v_active is null then
        return 0;
    end if;

    -- Messages: source row missing or soft-deleted.
    with doomed as (
        select ce.ctid from public.content_embeddings ce
         where ce.contract_id = v_active and ce.entity_type = 'message'
           and not exists (
               select 1 from public.discord_messages m
                where m.message_id::text = ce.item_id
                  and coalesce(m.is_deleted,false) = false)
         limit p_batch_size)
    delete from public.content_embeddings where ctid in (select ctid from doomed);
    get diagnostics v_removed = row_count; v_count := v_count + v_removed;

    -- Distillations: source row missing or no longer eligible.
    with doomed as (
        select ce.ctid from public.content_embeddings ce
         where ce.contract_id = v_active and ce.entity_type = 'distillation'
           and not exists (
               select 1 from public.distillations d
                where d.id::text = ce.item_id and d.status in ('pending','approved'))
         limit greatest(p_batch_size - v_count, 0))
    delete from public.content_embeddings where ctid in (select ctid from doomed);
    get diagnostics v_removed = row_count; v_count := v_count + v_removed;

    -- Resources: source row missing (hard delete) — all representations.
    with doomed as (
        select ce.ctid from public.content_embeddings ce
         where ce.contract_id = v_active and ce.entity_type = 'resource'
           and not exists (
               select 1 from public.external_resources r where r.id::text = ce.item_id)
         limit greatest(p_batch_size - v_count, 0))
    delete from public.content_embeddings where ctid in (select ctid from doomed);
    get diagnostics v_removed = row_count; v_count := v_count + v_removed;

    -- Task 2.17 (D6): workflow_python whose source is now quarantined or whose
    -- authoritative Python disappeared. Prose for the same item is retained.
    with doomed as (
        select ce.ctid from public.content_embeddings ce
         where ce.contract_id = v_active
           and ce.entity_type = 'resource'
           and ce.representation_type = 'workflow_python'
           and not exists (
               select 1 from public.external_resources r
                where r.id::text = ce.item_id
                  and r.kind = 'workflow'
                  and coalesce(public.hivemind_workflow_python_state(r.id),'safe') = 'safe'
                  and coalesce(r.payload->>'python_source','') <> '')
         limit greatest(p_batch_size - v_count, 0))
    delete from public.content_embeddings where ctid in (select ctid from doomed);
    get diagnostics v_removed = row_count; v_count := v_count + v_removed;

    return v_count;
end;
$$;

comment on function public.hivemind_cleanup_ineligible_embeddings(int) is
    'Task 2.17 override: drop active-contract vectors whose source is gone/ineligible. '
    'Adds representation-scoped removal of workflow_python whose source is now '
    'quarantined or whose authoritative Python disappeared; prose is retained.';

-- ------------------------------------------------------------
-- (F) Override hivemind_claim_embedding_jobs: return contract_id as TEXT so a
-- JS consumer never loses precision on the selected bigint (1360541028304258884
-- > 2^53). Same claim semantics (SKIP LOCKED, bounded batch + lease). The legacy
-- bigint-typed return remains in effect for clusters without 034.
--
-- contract_id changes from bigint to text in the return shape, so the legacy
-- OUT-parameter row type differs; DROP (signature-pinned to the 026 shape) first.
-- ------------------------------------------------------------
drop function if exists public.hivemind_claim_embedding_jobs(text,int,int);

create or replace function public.hivemind_claim_embedding_jobs(
    p_worker_id text,
    p_batch_size int default 8,
    p_lease_seconds int default 300
) returns table (
    job_id bigint,
    entity_type text,
    item_id text,
    representation_type text,
    job_kind text,
    contract_id text,
    attempts int
)
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
    if p_worker_id is null or btrim(p_worker_id) = '' then
        raise exception 'claim: worker_id is required';
    end if;
    if p_batch_size < 1 or p_batch_size > 64 then
        raise exception 'claim: batch_size must be 1..64, got %', p_batch_size;
    end if;
    if p_lease_seconds < 1 then
        raise exception 'claim: lease_seconds must be >= 1';
    end if;

    return query
    with claim as (
        select j.id from public.embedding_jobs j
         where j.status = 'pending' and j.next_attempt_at <= now()
         order by j.next_attempt_at, j.id
         limit p_batch_size
         for update of j skip locked
    )
    update public.embedding_jobs j
       set status = 'processing',
           locked_by = p_worker_id,
           locked_at = now(),
           lease_expires_at = now() + make_interval(secs => p_lease_seconds),
           attempts = j.attempts + 1,
           updated_at = now()
      from claim
     where j.id = claim.id
    returning j.id, j.entity_type, j.item_id, j.representation_type,
              j.job_kind, j.contract_id::text, j.attempts;
end;
$$;

comment on function public.hivemind_claim_embedding_jobs(text,int,int) is
    'Task 2.17 override: same SKIP LOCKED claim, but contract_id is returned as '
    'TEXT so a JS consumer cannot lose precision on the selected bigint (> 2^53).';

-- ------------------------------------------------------------
-- Trust boundary: revoke the new + overridden surfaces from public/anon/
-- authenticated, grant only to service_role where it exists.
-- ------------------------------------------------------------
do $$
declare
    sig text;
    r  text;
    sigs text[] := array[
        'hivemind_finalize_embedding_job(bigint,text,jsonb,text,text,boolean)',
        'hivemind_current_representation(text,text,text)',
        'hivemind_embedding_payload(text,text,text,int,int)',
        'hivemind_cleanup_ineligible_embeddings(int)',
        'hivemind_claim_embedding_jobs(text,int,int)',
        'hivemind_selected_contract_config()'
    ];
begin
    foreach sig in array sigs loop
        execute format('revoke execute on function public.%s from public;', sig);
        foreach r in array array['anon','authenticated'] loop
            if exists (select 1 from pg_catalog.pg_roles where rolname = r) then
                execute format('revoke execute on function public.%s from %I;', sig, r);
            end if;
        end loop;
        if exists (select 1 from pg_catalog.pg_roles where rolname = 'service_role') then
            execute format('grant execute on function public.%s to service_role;', sig);
        end if;
    end loop;
end $$;

-- ------------------------------------------------------------
-- Verification (read-only): the selected literal, the new/overridden surfaces,
-- and the manifest table.
-- ------------------------------------------------------------
select
    public.hivemind_selected_contract_id(
        'openai','text-embedding-3-small',384,1,2,
        'chunk_config'||E'\x1f'||'v1'||E'\x1f'||'prose#512/50'||E'\x1f'||'workflow_python#512/50'
    ) as selected_contract_id,
    p.proname as function_name,
    pg_get_function_arguments(p.oid) as arguments,
    p.prosecdef as security_definer
from pg_catalog.pg_proc p
join pg_catalog.pg_namespace n on n.oid = p.pronamespace
where n.nspname = 'public'
  and p.proname in ('hivemind_finalize_embedding_job','hivemind_current_representation',
                    'hivemind_embedding_payload','hivemind_cleanup_ineligible_embeddings',
                    'hivemind_claim_embedding_jobs')
order by p.proname;
