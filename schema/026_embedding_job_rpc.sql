-- ============================================================
-- Hivemind — Phase 2 / Task 2.8 — hardened embedding-job RPCs (SKIP LOCKED)
-- ============================================================
--
-- The bounded, lease-based job-processing protocol the worker (task 2.9) drives.
-- All functions are SECURITY DEFINER with a fixed search_path so the worker's
-- service-role path cannot resolve caller-created objects; the public/anon/
-- authenticated roles cannot execute them (grant to service-role only in prod).
--
--   hivemind_claim_embedding_jobs(worker, batch, lease)
--       FOR UPDATE SKIP LOCKED; bounded batch; sets a lease. Concurrent workers
--       therefore CANNOT double-process the same job (the 2.8 signal).
--   hivemind_complete_embedding_job(job, worker, chunks_written)
--       marks done; clears lease. Completion is source-hash safe: the worker
--       read CURRENT source to embed, and any change since enqueued a fresh job.
--   hivemind_fail_embedding_job(job, worker, error, retryable)
--       bounded retries (attempts >= max_attempts OR retryable=false -> failed);
--       otherwise pending + exponential backoff. last_error is sanitized+bounded.
--   hivemind_recover_stale_leases(grace)
--       re-queues processing jobs whose lease expired (dead worker recovery).
--   hivemind_cancel_pending_jobs(entity_type, item_id, rep?)
--       cancels pending jobs for an identity (cleanup/supersession). A cancelled
--       job is terminal: the claim set is status='pending', so it can never
--       become active again (the "superseded work cannot become active" rule).
--
-- Bounded: batch_size 1..64; lease >= 1s; attempts counted at claim so retries
-- (including stale-lease recovery -> re-claim) are bounded by max_attempts.
--
-- Additive + idempotent (re-createable). Requires schema/025 (embedding_jobs).
--
-- APPLY:    psql "$HIVEMIND_DB_URL" -f schema/026_embedding_job_rpc.sql
-- ROLLBACK: drop function if exists hivemind_cancel_pending_jobs(text,text,text);
--           drop function if exists hivemind_recover_stale_leases(int);
--           drop function if exists hivemind_fail_embedding_job(bigint,text,text,boolean);
--           drop function if exists hivemind_complete_embedding_job(bigint,text,int);
--           drop function if exists hivemind_claim_embedding_jobs(text,int,int);
--           drop function if exists hivemind_backoff_seconds(int);
--           drop function if exists hivemind_sanitize_error(text);
-- ============================================================

do $$
begin
    if not exists (select 1 from pg_catalog.pg_tables where schemaname='public' and tablename='embedding_jobs') then
        raise exception 'prerequisite guard: embedding_jobs not found — apply schema/025 (task 2.7) first';
    end if;
end $$;

-- ---------------------------------------------------------------------------
-- Helpers: bounded backoff + secret-safe error sanitization
-- ---------------------------------------------------------------------------
create or replace function hivemind_backoff_seconds(p_attempts int)
returns int
language sql immutable
as $$
    -- Exponential backoff capped at 300s; 2^6 keeps it from overflowing before the cap.
    select greatest(5, least(300, (5 * (2 ^ least(greatest(p_attempts,1), 6)))::int));
$$;

create or replace function hivemind_sanitize_error(p_text text)
returns text
language sql immutable
as $$
    -- Defense in depth: bound length and redact credential-shaped substrings so a
    -- provider/transport error can never leak a key into the job log. The worker
    -- also avoids emitting secrets; this is the storage-side guarantee.
    select left(
        regexp_replace(
            regexp_replace(
                regexp_replace(
                    regexp_replace(
                        coalesce(p_text, ''),
                        'sk-(proj-)?[A-Za-z0-9_-]{20,}', '[redacted:openai]', 'g'),
                    'gh[pousr]_[A-Za-z0-9]{20,}', '[redacted:github]', 'g'),
                'xox[abprs]-[A-Za-z0-9-]{10,}', '[redacted:slack]', 'g'),
            'Bearer\s+[A-Za-z0-9._-]{16,}', '[redacted:bearer]', 'g'),
        500);
$$;

comment on function hivemind_sanitize_error(text) is
    'Task 2.8: bound + redact a job error string so logs store no credential.';

-- ---------------------------------------------------------------------------
-- Claim (FOR UPDATE SKIP LOCKED, bounded batch + lease)
-- ---------------------------------------------------------------------------
create or replace function hivemind_claim_embedding_jobs(
    p_worker_id text,
    p_batch_size int default 8,
    p_lease_seconds int default 300
) returns table(
    job_id bigint,
    entity_type text,
    item_id text,
    representation_type text,
    job_kind text,
    contract_id bigint,
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
        select j.id
          from embedding_jobs j
         where j.status = 'pending'
           and j.next_attempt_at <= now()
         order by j.next_attempt_at, j.id
         limit p_batch_size
         for update of j skip locked
    )
    update embedding_jobs j
       set status = 'processing',
           locked_by = p_worker_id,
           locked_at = now(),
           lease_expires_at = now() + make_interval(secs => p_lease_seconds),
           attempts = j.attempts + 1,
           updated_at = now()
      from claim
     where j.id = claim.id
    returning j.id, j.entity_type, j.item_id, j.representation_type,
              j.job_kind, j.contract_id, j.attempts;
end;
$$;

-- ---------------------------------------------------------------------------
-- Complete (source-hash safe: marks the work done after current-source embed)
-- ---------------------------------------------------------------------------
create or replace function hivemind_complete_embedding_job(
    p_job_id bigint,
    p_worker_id text,
    p_chunks_written int default 0
) returns void
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
    update embedding_jobs
       set status = 'done',
           locked_by = null, locked_at = null, lease_expires_at = null,
           last_error = null, updated_at = now()
     where id = p_job_id
       and locked_by = p_worker_id
       and status = 'processing';
    if not found then
        raise exception 'complete: job % is not currently processing for worker %', p_job_id, p_worker_id;
    end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- Fail (bounded retry + backoff, or permanent failure; sanitized error)
-- ---------------------------------------------------------------------------
create or replace function hivemind_fail_embedding_job(
    p_job_id bigint,
    p_worker_id text,
    p_error text,
    p_retryable boolean default true
) returns void
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_attempts int;
    v_max int;
begin
    select attempts, max_attempts into v_attempts, v_max
      from embedding_jobs
     where id = p_job_id and locked_by = p_worker_id and status = 'processing';
    if not found then
        raise exception 'fail: job % is not currently processing for worker %', p_job_id, p_worker_id;
    end if;

    if not p_retryable or v_attempts >= v_max then
        update embedding_jobs
           set status = 'failed',
               last_error = hivemind_sanitize_error(p_error),
               locked_by = null, locked_at = null, lease_expires_at = null,
               updated_at = now()
         where id = p_job_id;
    else
        update embedding_jobs
           set status = 'pending',
               last_error = hivemind_sanitize_error(p_error),
               next_attempt_at = now() + make_interval(secs => hivemind_backoff_seconds(v_attempts)),
               locked_by = null, locked_at = null, lease_expires_at = null,
               updated_at = now()
         where id = p_job_id;
    end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- Recover stale leases (dead-worker recovery -> re-queue for claiming)
-- ---------------------------------------------------------------------------
create or replace function hivemind_recover_stale_leases(p_grace_seconds int default 0)
returns int
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare v_count int;
begin
    with recovered as (
        update embedding_jobs
           set status = 'pending',
               next_attempt_at = now(),
               locked_by = null, locked_at = null, lease_expires_at = null,
               updated_at = now()
         where status = 'processing'
           and lease_expires_at + make_interval(secs => greatest(p_grace_seconds, 0)) < now()
        returning 1
    )
    select count(*) into v_count from recovered;
    return v_count;
end;
$$;

-- ---------------------------------------------------------------------------
-- Cancel pending jobs for an identity (cleanup / supersession). Terminal: a
-- cancelled job is outside the status='pending' claim set, so it can never
-- reactivate. A later source change enqueues a FRESH pending row (dedup index
-- only covers pending), leaving the cancelled row cancelled.
-- ---------------------------------------------------------------------------
create or replace function hivemind_cancel_pending_jobs(
    p_entity_type text,
    p_item_id text,
    p_representation_type text default null
) returns int
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare v_count int;
begin
    with cancelled as (
        update embedding_jobs
           set status = 'cancelled',
               locked_by = null, locked_at = null, lease_expires_at = null,
               updated_at = now()
         where entity_type = p_entity_type
           and item_id = p_item_id
           and status = 'pending'
           and (p_representation_type is null or representation_type = p_representation_type)
        returning 1
    )
    select count(*) into v_count from cancelled;
    return v_count;
end;
$$;

-- ---------------------------------------------------------------------------
-- Revoke execution from public + Supabase roles where present. The worker uses
-- the service-role path; anon/authenticated must not drive the queue directly.
-- ---------------------------------------------------------------------------
do $$
declare
    sig text;
    role_to_revoke text;
    sigs text[] := array[
        'hivemind_claim_embedding_jobs(text,int,int)',
        'hivemind_complete_embedding_job(bigint,text,int)',
        'hivemind_fail_embedding_job(bigint,text,text,boolean)',
        'hivemind_recover_stale_leases(int)',
        'hivemind_cancel_pending_jobs(text,text,text)'
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

-- ---------------------------------------------------------------------------
-- Verification (read-only).
-- ---------------------------------------------------------------------------
select
    p.proname as rpc,
    p.prosecdef as security_definer,
    p.proconfig as config
from pg_catalog.pg_proc p
join pg_catalog.pg_namespace n on n.oid = p.pronamespace
where n.nspname = 'public'
  and p.proname in ('hivemind_claim_embedding_jobs','hivemind_complete_embedding_job',
                    'hivemind_fail_embedding_job','hivemind_recover_stale_leases',
                    'hivemind_cancel_pending_jobs')
order by p.proname;
