-- ============================================================
-- Hivemind — Phase 2 / Task 2.9 (part 2) — Supabase Cron schedule for the worker
-- ============================================================
--
-- Schedules the bounded `embedding-worker` Edge Function (task 2.9) via Supabase
-- Cron (pg_cron) + pg_net so the queue drains without a long-running process.
-- Each tick POSTs once; overlap is prevented by the database claim protocol
-- (FOR UPDATE SKIP LOCKED, schema/026), and each invocation exits inside the
-- platform limit (bounded batch). The schedule is deliberately modest (every 2
-- minutes) so invocations rarely contend.
--
-- CONFIG (operator, before enabling): set the function URL + the shared worker
-- key as database settings (never committed):
--   alter database postgres set app.hivemind_worker_url =
--       'https://<project-ref>.supabase.co/functions/v1/embedding-worker';
--   alter database postgres set app.hivemind_worker_key = '<shared-secret>';
-- (OPENAI_API_KEY and SUPABASE_SERVICE_ROLE_KEY are Edge secrets, not DB settings.)
--
-- GUARDED: pg_cron + pg_net are Supabase-provided; if either is absent (e.g. a
-- vanilla rehearsal cluster) the migration emits a NOTICE and schedules nothing,
-- so it is safe to apply anywhere. Idempotent (unschedule-then-schedule).
--
-- APPLY:    psql "$HIVEMIND_DB_URL" -f schema/028_embedding_worker_cron.sql
-- ROLLBACK: select cron.unschedule('hivemind-embedding-worker');
-- ============================================================

do $$
declare
    has_cron boolean;
    has_net boolean;
    cmd text;
begin
    select exists (select 1 from pg_catalog.pg_extension where extname = 'pg_cron') into has_cron;
    select exists (select 1 from pg_catalog.pg_extension where extname = 'pg_net') into has_net;

    if not has_cron or not has_net then
        raise notice 'pg_cron/pg_net not installed — embedding-worker schedule skipped '
                     '(Supabase provides both; apply there to enable). Has pg_cron=%, pg_net=%',
            has_cron, has_net;
        return;
    end if;

    -- Idempotent: replace any prior schedule of this name.
    perform cron.unschedule('hivemind-embedding-worker');

    -- The scheduled command reads the operator-configured URL + key at run time.
    -- The worker key authorizes the invocation (Edge Function checks x-worker-key);
    -- the OpenAI + service-role keys live in the Edge secret store, not here.
    cmd := format(
        $f$
        select net.http_post(
            url := %L,
            headers := jsonb_build_object(
                'content-type', 'application/json',
                'x-worker-key', %L
            ),
            body := '{}'::jsonb
        );
        $f$,
        coalesce(current_setting('app.hivemind_worker_url', true), ''),
        coalesce(current_setting('app.hivemind_worker_key', true), '')
    );

    perform cron.schedule(
        'hivemind-embedding-worker',
        '*/2 * * * *',          -- every 2 minutes; bounded batch per tick
        cmd
    );

    raise notice 'scheduled hivemind-embedding-worker (*/2 * * * *)';
end $$;

-- ---------------------------------------------------------------------------
-- Verification (read-only). Shows the schedule only when pg_cron is present.
-- ---------------------------------------------------------------------------
do $$
begin
    if exists (select 1 from pg_catalog.pg_extension where extname = 'pg_cron') then
        raise notice 'scheduled job: %',
            (select jobname || ' ' || schedule || ' active=' || active::text
               from cron.job where jobname = 'hivemind-embedding-worker');
    else
        raise notice 'pg_cron absent — no schedule (apply on Supabase to enable).';
    end if;
end $$;
