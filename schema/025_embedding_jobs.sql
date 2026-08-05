-- ============================================================
-- Hivemind — Phase 2 / Task 2.7 — embedding job queue + source triggers
-- ============================================================
--
-- The generalized incremental embedding job table (plan AD-2 / "Embedding
-- lifecycle") and the source-table triggers that ONLY ENQUEUE lightweight work.
-- A source change never calls the embedding provider and never computes a
-- canonical representation inside the source transaction; it inserts one
-- idempotent job per affected (entity_type, item_id, representation_type). The
-- worker (task 2.9) claims, recomputes canonical representations from the CURRENT
-- source, chunks, embeds, and atomically replaces rows — so the queue guarantees
-- "this identity needs (re)processing" and is source-hash safe by construction.
--
-- Job shape (plan "Embedding lifecycle"):
--   entity_type, item_id, representation_type, job_kind (embed|reembed|drop),
--   representation_hash (optional; worker-filled), contract_id, status, attempts,
--   next_attempt_at, locked_at, locked_by, last_error
--
-- IDEMPOTENCE / DEDUP: at most ONE pending job per
-- (entity_type, item_id, representation_type) — a partial UNIQUE index. The
-- enqueue helper UPSERTs on it, so a rapid delete→re-insert or many edits
-- collapse to a single pending job whose job_kind is the LATEST operation. This
-- is the "Supersedes a stale job if the source changed after it was queued" rule
-- made cheap: there is never more than one pending job per identity, and the
-- worker always reads current source, so a superseded state can never become
-- active (the old job is the same row, updated in place).
--
-- SECURITY: triggers run as the source-table owner; the enqueue helper + tables
-- are embedding infrastructure only. Job/log evidence stores IDs, hashes,
-- counts, states, and BOUNDED SANITIZED errors — never credentials, raw private
-- text, vectors, or secret-like substrings (task 2.8 sanitizes last_error).
--
-- Additive + idempotent. Requires schema/020–024 (S1) + the pre-existing source
-- tables (discord_messages, external_resources, distillations). Does not edit
-- 008–019 or 020–024.
--
-- APPLY:    psql "$HIVEMIND_DB_URL" -f schema/025_embedding_jobs.sql
-- ROLLBACK: drop trigger if exists trg_embedding_jobs_discord_messages on discord_messages;
--           drop trigger if exists trg_embedding_jobs_external_resources on external_resources;
--           drop trigger if exists trg_embedding_jobs_distillations on distillations;
--           drop function if exists hivemind_enqueue_embedding_job(text,text,text,text,text,bigint);
--           drop function if exists hivemind_active_contract_id(int);
--           drop table if exists embedding_jobs cascade;
-- ============================================================

-- Guard: the S1 embedding-contract registry must exist (active-contract lookup).
do $$
begin
    if not exists (select 1 from pg_catalog.pg_tables where schemaname='public' and tablename='embedding_contracts') then
        raise exception 'prerequisite guard: embedding_contracts not found — apply schema/021 (S1) first';
    end if;
end $$;

create table if not exists embedding_jobs (
    id                  bigserial primary key,
    entity_type         text not null
                        check (entity_type in ('message', 'resource', 'distillation')),
    item_id             text not null,                       -- snowflake-safe string
    representation_type text not null default 'prose'
                        check (representation_type in ('prose', 'workflow_python')),
    job_kind            text not null
                        check (job_kind in ('embed', 'reembed', 'drop')),
    -- The contract to file under; NULL means "defer to the active contract of the
    -- active dimension at claim time" (e.g. enqueued before a contract is active).
    contract_id         bigint references embedding_contracts(id) on delete set null,
    -- Optional representation hash (worker-filled). The worker always recomputes
    -- from current source, so this is metadata, not the freshness authority.
    representation_hash text,
    source_op           text
        check (source_op in ('insert','update','delete','soft_delete','status_change',
                             'python_change','semantic_change','opt_out')),
    status              text not null default 'pending'
                        check (status in ('pending','processing','done','failed','cancelled')),
    attempts            integer not null default 0,
    max_attempts        integer not null default 5,
    next_attempt_at     timestamptz not null default now(),
    locked_at           timestamptz,
    locked_by           text,
    lease_expires_at    timestamptz,
    last_error          text,                                -- bounded + sanitized (task 2.8)
    enqueued_at         timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);

-- IDEMPOTENCE: one pending job per (entity_type, item_id, representation_type).
create unique index if not exists one_pending_job_per_identity_rep
    on embedding_jobs (entity_type, item_id, representation_type)
    where status = 'pending';

-- Claim surface (task 2.8): pending/retry-due jobs, scoped by status + due time.
create index if not exists embedding_jobs_claim_idx
    on embedding_jobs (next_attempt_at)
    where status = 'pending';

comment on table embedding_jobs is
    'Task 2.7: incremental embedding job queue. Source triggers enqueue ONE '
    'idempotent job per (entity_type, item_id, representation_type); the worker '
    '(2.9) claims via SKIP LOCKED (2.8), recomputes from current source, embeds, '
    'and atomically replaces rows. Dedup: one pending job per identity+rep.';

-- ============================================================
-- Active-contract resolver + idempotent enqueue helper
-- ============================================================

create or replace function hivemind_active_contract_id(p_dimension int default 384)
returns bigint
language sql
stable
set search_path = public
as $$
    -- The single active contract of the requested dimension (schema/021 invariant:
    -- one_active_contract_per_dimension). NULL when no contract is active yet.
    select id from embedding_contracts where dimension = p_dimension and status = 'active' limit 1;
$$;

create or replace function hivemind_enqueue_embedding_job(
    p_entity_type text,
    p_item_id text,
    p_representation_type text,
    p_job_kind text,
    p_source_op text,
    p_contract_id bigint default null
) returns void
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_contract bigint;
begin
    v_contract := coalesce(p_contract_id, hivemind_active_contract_id());
    insert into embedding_jobs (
        entity_type, item_id, representation_type, job_kind, source_op,
        contract_id, status, next_attempt_at, enqueued_at, updated_at
    ) values (
        p_entity_type, p_item_id, p_representation_type, p_job_kind, p_source_op,
        v_contract, 'pending', now(), now(), now()
    )
    on conflict (entity_type, item_id, representation_type) where status = 'pending'
    do update set
        -- Latest operation wins (delete->re-insert, repeated edits, drop->embed).
        job_kind      = excluded.job_kind,
        source_op     = excluded.source_op,
        contract_id   = coalesce(excluded.contract_id, embedding_jobs.contract_id),
        next_attempt_at = now(),
        updated_at    = now();
end;
$$;

revoke execute on function hivemind_enqueue_embedding_job(text,text,text,text,text,bigint) from public;
-- Supabase roles (anon/authenticated) may not exist in a vanilla rehearsal
-- cluster; revoke them only where present (defense in depth — the function is
-- SECURITY DEFINER with a fixed search_path regardless).
do $$
begin
    if exists (select 1 from pg_catalog.pg_roles where rolname = 'anon') then
        revoke execute on function hivemind_enqueue_embedding_job(text,text,text,text,text,bigint) from anon;
    end if;
    if exists (select 1 from pg_catalog.pg_roles where rolname = 'authenticated') then
        revoke execute on function hivemind_enqueue_embedding_job(text,text,text,text,text,bigint) from authenticated;
    end if;
end $$;
comment on function hivemind_enqueue_embedding_job(text,text,text,text,text,bigint) is
    'Task 2.7: idempotently enqueue one embedding job. Lightweight — no provider, '
    'no representation computation. Dedup via one_pending_job_per_identity_rep.';

-- ============================================================
-- Source-table triggers — enqueue ONLY (no provider, no heavy work)
-- ============================================================

-- ---- Discord messages: content drives prose; is_deleted drives drop --------
create or replace function trg_embedding_jobs_discord_messages_fn()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
    if tg_op = 'INSERT' then
        if coalesce(new.is_deleted, false) = false then
            perform hivemind_enqueue_embedding_job('message', new.message_id::text, 'prose', 'embed', 'insert');
        end if;
        return new;
    elsif tg_op = 'UPDATE' then
        if coalesce(new.is_deleted, false) = true and coalesce(old.is_deleted, false) = false then
            perform hivemind_enqueue_embedding_job('message', new.message_id::text, 'prose', 'drop', 'soft_delete');
        elsif new.content is distinct from old.content then
            perform hivemind_enqueue_embedding_job('message', new.message_id::text, 'prose', 'reembed', 'update');
        end if;
        return new;
    elsif tg_op = 'DELETE' then
        perform hivemind_enqueue_embedding_job('message', old.message_id::text, 'prose', 'drop', 'delete');
        return old;
    end if;
    return null;
end;
$$;

drop trigger if exists trg_embedding_jobs_discord_messages on public.discord_messages;
create trigger trg_embedding_jobs_discord_messages
    after insert or update or delete on public.discord_messages
    for each row execute function trg_embedding_jobs_discord_messages_fn();

-- ---- External resources: prose always; workflow also enqueues workflow_python
create or replace function trg_embedding_jobs_external_resources_fn()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
    if tg_op = 'INSERT' then
        perform hivemind_enqueue_embedding_job('resource', new.id::text, 'prose', 'embed', 'insert');
        if new.kind = 'workflow' then
            perform hivemind_enqueue_embedding_job('resource', new.id::text, 'workflow_python', 'embed', 'insert');
        end if;
        return new;
    elsif tg_op = 'UPDATE' then
        -- Prose covers title/body/tags+semantics/kind.
        if new.title is distinct from old.title
           or new.body is distinct from old.body
           or new.metadata is distinct from old.metadata
           or new.kind is distinct from old.kind then
            perform hivemind_enqueue_embedding_job('resource', new.id::text, 'prose', 'reembed', 'update');
        end if;
        -- Workflow Python: payload.python_source, the body block, and semantics.
        if new.kind = 'workflow' and (
            new.payload is distinct from old.payload
            or new.body is distinct from old.body
            or new.metadata is distinct from old.metadata) then
            perform hivemind_enqueue_embedding_job('resource', new.id::text, 'workflow_python', 'reembed', 'python_change');
        end if;
        -- Reclassified away from workflow: drop the code embeddings.
        if old.kind = 'workflow' and new.kind <> 'workflow' then
            perform hivemind_enqueue_embedding_job('resource', new.id::text, 'workflow_python', 'drop', 'semantic_change');
        end if;
        return new;
    elsif tg_op = 'DELETE' then
        perform hivemind_enqueue_embedding_job('resource', old.id::text, 'prose', 'drop', 'delete');
        if old.kind = 'workflow' then
            perform hivemind_enqueue_embedding_job('resource', old.id::text, 'workflow_python', 'drop', 'delete');
        end if;
        return old;
    end if;
    return null;
end;
$$;

drop trigger if exists trg_embedding_jobs_external_resources on public.external_resources;
create trigger trg_embedding_jobs_external_resources
    after insert or update or delete on public.external_resources
    for each row execute function trg_embedding_jobs_external_resources_fn();

-- ---- Distillations: status gates eligibility; q/conditions/answer drive prose
create or replace function trg_embedding_jobs_distillations_fn()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    eligible_old boolean;
    eligible_new boolean;
begin
    if tg_op = 'INSERT' then
        if new.status in ('pending', 'approved') then
            perform hivemind_enqueue_embedding_job('distillation', new.id::text, 'prose', 'embed', 'insert');
        end if;
        return new;
    elsif tg_op = 'UPDATE' then
        eligible_old := (old.status in ('pending', 'approved'));
        eligible_new := (new.status in ('pending', 'approved'));
        if not eligible_new and eligible_old then
            -- rejected / superseded: remove from the semantic index.
            perform hivemind_enqueue_embedding_job('distillation', new.id::text, 'prose', 'drop', 'status_change');
        elsif eligible_new and (
            new.question is distinct from old.question
            or new.conditions is distinct from old.conditions
            or new.answer is distinct from old.answer
            or not eligible_old) then
            perform hivemind_enqueue_embedding_job('distillation', new.id::text, 'prose', 'reembed', 'update');
        end if;
        return new;
    elsif tg_op = 'DELETE' then
        perform hivemind_enqueue_embedding_job('distillation', old.id::text, 'prose', 'drop', 'delete');
        return old;
    end if;
    return null;
end;
$$;

drop trigger if exists trg_embedding_jobs_distillations on public.distillations;
create trigger trg_embedding_jobs_distillations
    after insert or update or delete on public.distillations
    for each row execute function trg_embedding_jobs_distillations_fn();

-- ---------------------------------------------------------------------------
-- Verification (read-only).
-- ---------------------------------------------------------------------------
select
    'embedding_jobs' as table_name,
    (select count(*) from pg_catalog.pg_class c where c.relname='embedding_jobs')::text as exists_01,
    (select count(*) from pg_catalog.pg_class c where c.relname='one_pending_job_per_identity_rep')::text as dedup_idx,
    'triggers' as section,
    (select count(*) from pg_catalog.pg_trigger t
       join pg_catalog.pg_class c on c.oid=t.tgrelid
      where c.relname in ('discord_messages','external_resources','distillations')
        and t.tgname like 'trg_embedding_jobs_%')::text as source_triggers;
