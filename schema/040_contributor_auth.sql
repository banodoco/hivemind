-- Hivemind contributor authentication (post-039).
--
-- Existing contributor rows predate Supabase Auth and may not have an
-- operator-verified auth.users identity.  Those rows are deliberately kept in
-- an explicit claim-pending state instead of being assigned a fake UUID or
-- made impossible to migrate.  Their numeric IDs, attribution, editor bit,
-- revocation state, and legacy hashes remain unchanged.  New contributor rows
-- must have a real auth.users UUID.  Once an operator claims a legacy row,
-- auth_user_id_migration_pending is set false and the same direct FK applies.
--
-- contributor_keys is the only protected key lookup surface.  api_key_hash is
-- retained as historical source state but is never read by the protected
-- lookup functions or edge functions.  This migration is intended to run as
-- one transaction (the Supabase migration runner supplies the transaction).
--
-- Ordering: apply after the base contributors table and Supabase auth.users
-- exist, and before deploying the contributor-auth edge revision that calls
-- these RPCs.  The request/key association below is additive and must be
-- applied before enabling CLI cleanup of a lost redemption response.
-- SAFE ROLLBACK PROCEDURE (mandatory): first disable all contributor writes
-- before backing out the edge/CLI deployment.  If contributor writes must stay
-- available during recovery, retain migration 040's resolver and its
-- service-role-only function/table boundaries until the replacement is live.
-- NEVER restore legacy contributors.api_key_hash authentication: doing so
-- bypasses claim_pending and device revocation, and would re-enable revoked
-- legacy credentials outside contributor_keys.  In particular, rolling back
-- the callers must not mean restoring the old direct api_key_hash lookup.
-- Revoke service-role grants for these RPCs only after writes are disabled or
-- the replacement boundary is ready.  Do not delete contributor rows, legacy
-- api_key_hash values, contributor_keys rows, or contributor_auth_requests
-- audit rows as a rollback shortcut; preserve them for forward reconciliation.
-- On a disposable database, drop additive objects only by tearing down that
-- database, never by deleting production audit history.

create extension if not exists pgcrypto;

alter table public.contributors
  add column if not exists auth_user_id uuid;

alter table public.contributors
  add column if not exists auth_user_id_migration_pending boolean not null default true;

do $$
begin
  if not exists (
    select 1 from pg_constraint
     where conname = 'contributors_auth_user_id_fkey'
       and conrelid = 'public.contributors'::regclass
  ) then
    alter table public.contributors
      add constraint contributors_auth_user_id_fkey
      foreign key (auth_user_id) references auth.users(id);
  end if;
  if not exists (
    select 1 from pg_constraint
     where conname = 'contributors_auth_user_id_key'
       and conrelid = 'public.contributors'::regclass
  ) then
    alter table public.contributors
      add constraint contributors_auth_user_id_key unique (auth_user_id);
  end if;
  if not exists (
    select 1 from pg_constraint
     where conname = 'contributors_auth_user_id_transition_check'
       and conrelid = 'public.contributors'::regclass
  ) then
    alter table public.contributors
      add constraint contributors_auth_user_id_transition_check
      check (auth_user_id is not null or auth_user_id_migration_pending);
  end if;
end
$$;

update public.contributors
   set auth_user_id_migration_pending = (auth_user_id is null)
 where auth_user_id_migration_pending is distinct from (auth_user_id is null);

create or replace function public.hivemind_contributor_identity_guard()
returns trigger
language plpgsql
set search_path = public, pg_temp
as $$
begin
  -- A newly-created contributor is always a direct Auth principal.  Existing
  -- null identities are the documented operator-claim transition only.
  if tg_op = 'INSERT' and new.auth_user_id is null then
    raise exception 'new contributors require auth.users identity' using errcode = '23514';
  end if;
  if new.auth_user_id is null then
    new.auth_user_id_migration_pending := true;
  else
    new.auth_user_id_migration_pending := false;
  end if;
  return new;
end;
$$;

drop trigger if exists contributors_identity_guard on public.contributors;
create trigger contributors_identity_guard
before insert or update of auth_user_id, auth_user_id_migration_pending
on public.contributors
for each row execute function public.hivemind_contributor_identity_guard();

create table if not exists public.contributor_keys (
  id uuid primary key default gen_random_uuid(),
  contributor_id bigint not null references public.contributors(id) on delete cascade,
  key_hash text not null,
  label text,
  created_at timestamptz not null default now(),
  last_used_at timestamptz,
  revoked_at timestamptz,
  constraint contributor_keys_hash_check check (key_hash ~ '^[0-9a-f]{64}$'),
  constraint contributor_keys_hash_key unique (key_hash)
);

create index if not exists contributor_keys_contributor_idx
  on public.contributor_keys (contributor_id, created_at desc);
create index if not exists contributor_keys_active_idx
  on public.contributor_keys (key_hash) where revoked_at is null;

alter table public.contributor_keys enable row level security;
revoke all on table public.contributor_keys from public, anon, authenticated;

-- A hash belonging to two different contributor IDs is ambiguous and must be
-- resolved by an operator rather than silently assigning attribution.
do $$
begin
  if exists (
    select 1
      from public.contributors
     where api_key_hash is not null
     group by api_key_hash
    having count(distinct id) > 1
  ) then
    raise exception 'legacy contributor key hash is assigned to multiple contributors';
  end if;
end
$$;

insert into public.contributor_keys (contributor_id, key_hash, label, created_at, revoked_at)
select distinct on (c.api_key_hash)
       c.id, c.api_key_hash, 'legacy', c.created_at, c.revoked_at
  from public.contributors c
 where c.api_key_hash is not null
   and c.api_key_hash ~ '^[0-9a-f]{64}$'
 order by c.api_key_hash, c.id
on conflict (key_hash) do nothing;

-- This function is intentionally service-role only.  It hashes the supplied
-- key in the database and never returns or stores the raw value.  Legacy
-- claim-pending rows are deliberately excluded: their key rows are preserved
-- for audit/claim continuity, but they cannot perform authenticated writes
-- until an operator binds them to a verified auth.users identity.
create or replace function public.hivemind_resolve_contributor_key(p_key text)
returns table(contributor_id bigint, auth_user_id uuid, is_editor boolean)
language plpgsql security definer
set search_path = public, extensions, pg_temp
as $$
declare
  digest_hex text;
begin
  if p_key is null or p_key !~ '^hm_[0-9a-f]{64}$' then
    return;
  end if;
  digest_hex := encode(digest(convert_to(p_key, 'utf8'), 'sha256'), 'hex');
  return query
  update public.contributor_keys k
     set last_used_at = now()
    from public.contributors c
   where k.key_hash = digest_hex
     and k.revoked_at is null
     and c.id = k.contributor_id
     and c.revoked_at is null
     and c.auth_user_id is not null
  returning c.id, c.auth_user_id, c.is_editor;
end;
$$;

-- Operators can bind a pre-existing numeric contributor to a verified Auth
-- principal without changing any attribution or editor permission.
create or replace function public.hivemind_claim_contributor(
  p_operator text, p_contributor_id bigint, p_auth_user_id uuid
) returns jsonb
language plpgsql security definer
set search_path = public, pg_temp
as $$
begin
  if p_operator not in ('service_role', 'postgres')
     or (current_user not in ('service_role', 'postgres')
         and not pg_has_role(current_user, 'pg_database_owner', 'member')) then
    raise exception 'operator authorization required' using errcode = '42501';
  end if;
  update public.contributors
     set auth_user_id = p_auth_user_id,
         auth_user_id_migration_pending = false
   where id = p_contributor_id;
  if not found then
    raise exception 'unknown contributor %', p_contributor_id using errcode = '23503';
  end if;
  return jsonb_build_object('contributor_id', p_contributor_id::text,
                            'auth_user_id', p_auth_user_id::text);
end;
$$;

-- -------------------------- browser approval broker -----------------------

create table if not exists public.contributor_auth_requests (
  id uuid primary key default gen_random_uuid(),
  request_token_hash text not null unique,
  poll_secret_hash text not null,
  approval_code_hash text not null,
  machine_label text not null check (char_length(machine_label) between 1 and 200),
  created_at timestamptz not null default now(),
  expires_at timestamptz not null,
  approved_auth_user_id uuid references auth.users(id),
  approved_at timestamptz,
  consumed_at timestamptz,
  revoked_at timestamptz,
  last_polled_at timestamptz,
  poll_count integer not null default 0 check (poll_count >= 0)
);

create index if not exists contributor_auth_requests_expiry_idx
  on public.contributor_auth_requests (expires_at);

alter table public.contributor_auth_requests
  add column if not exists issued_key_id uuid;

do $$
begin
  if not exists (
    select 1 from pg_constraint
     where conname = 'contributor_auth_requests_issued_key_fkey'
       and conrelid = 'public.contributor_auth_requests'::regclass
  ) then
    alter table public.contributor_auth_requests
      add constraint contributor_auth_requests_issued_key_fkey
      foreign key (issued_key_id) references public.contributor_keys(id);
  end if;
end
$$;

create unique index if not exists contributor_auth_requests_issued_key_idx
  on public.contributor_auth_requests (issued_key_id)
  where issued_key_id is not null;

alter table public.contributor_auth_requests enable row level security;
revoke all on table public.contributor_auth_requests from public, anon, authenticated;

create or replace function public.hivemind_secret_hash(p_secret text)
returns text language sql immutable strict
set search_path = public, extensions, pg_temp
as $$ select encode(digest(convert_to(p_secret, 'utf8'), 'sha256'), 'hex') $$;

create or replace function public.hivemind_auth_create_request(
  p_request_token text, p_poll_secret text, p_approval_code text,
  p_machine_label text, p_ttl_seconds integer default 600
) returns jsonb
language plpgsql security definer
set search_path = public, pg_temp
as $$
declare
  request_id uuid;
  expiry timestamptz;
begin
  if p_request_token is null or char_length(p_request_token) < 32
     or p_poll_secret is null or char_length(p_poll_secret) < 32
     or p_approval_code is null or char_length(p_approval_code) < 8
     or nullif(btrim(p_machine_label), '') is null then
    raise exception 'invalid broker request' using errcode = '22023';
  end if;
  if p_ttl_seconds is null or p_ttl_seconds not between 60 and 1800 then
    raise exception 'broker request TTL must be between 60 and 1800 seconds' using errcode = '22023';
  end if;
  expiry := now() + make_interval(secs => p_ttl_seconds);
  insert into public.contributor_auth_requests
    (request_token_hash, poll_secret_hash, approval_code_hash, machine_label, expires_at)
  values
    (public.hivemind_secret_hash(p_request_token), public.hivemind_secret_hash(p_poll_secret),
     public.hivemind_secret_hash(p_approval_code), left(btrim(p_machine_label), 200), expiry)
  returning id into request_id;
  return jsonb_build_object('request_id', request_id::text, 'expires_at', expiry);
end;
$$;

create or replace function public.hivemind_auth_get_request(p_request_token text)
returns jsonb
language plpgsql security definer
set search_path = public, pg_temp
as $$
declare r public.contributor_auth_requests%rowtype;
begin
  select * into r from public.contributor_auth_requests
   where request_token_hash = public.hivemind_secret_hash(p_request_token);
  if not found or r.revoked_at is not null or r.expires_at <= now() then
    raise exception 'broker request unavailable' using errcode = '42501';
  end if;
  -- This is a side-effect-free GET-shaped operation.  The approval code is
  -- carried by the short-lived browser URL, never stored in plaintext here.
  return jsonb_build_object('request_id', r.id::text, 'machine_label', r.machine_label,
                            'expires_at', r.expires_at,
                            'status', case when r.consumed_at is not null then 'consumed'
                                           when r.approved_at is not null then 'approved'
                                           else 'pending' end);
end;
$$;

create or replace function public.hivemind_auth_poll_request(
  p_request_token text, p_poll_secret text
) returns jsonb
language plpgsql security definer
set search_path = public, pg_temp
as $$
declare r public.contributor_auth_requests%rowtype; state text;
begin
  select * into r from public.contributor_auth_requests
   where request_token_hash = public.hivemind_secret_hash(p_request_token)
   for update;
  if not found or r.poll_secret_hash <> public.hivemind_secret_hash(p_poll_secret) then
    raise exception 'broker request unavailable' using errcode = '42501';
  end if;
  if r.last_polled_at is not null and r.last_polled_at > now() - interval '1 second' then
    raise exception 'broker polling rate limited' using errcode = '55P03';
  end if;
  update public.contributor_auth_requests
     set last_polled_at = now(), poll_count = poll_count + 1
   where id = r.id;
  state := case when r.revoked_at is not null then 'revoked'
                when r.consumed_at is not null then 'consumed'
                when r.expires_at <= now() then 'expired'
                when r.approved_at is not null then 'approved'
                else 'pending' end;
  return jsonb_build_object('status', state, 'expires_at', r.expires_at);
end;
$$;

create or replace function public.hivemind_auth_approve_request(
  p_request_token text, p_approval_code text, p_auth_user_id uuid
) returns jsonb
language plpgsql security definer
set search_path = public, pg_temp
as $$
declare r public.contributor_auth_requests%rowtype; contributor_id bigint;
begin
  select * into r from public.contributor_auth_requests
   where request_token_hash = public.hivemind_secret_hash(p_request_token)
   for update;
  if not found or r.approval_code_hash <> public.hivemind_secret_hash(p_approval_code)
     or r.revoked_at is not null or r.expires_at <= now() or r.consumed_at is not null then
    raise exception 'broker request unavailable' using errcode = '42501';
  end if;
  if r.approved_auth_user_id is not null and r.approved_auth_user_id <> p_auth_user_id then
    raise exception 'broker request already approved' using errcode = '23505';
  end if;
  if r.approved_auth_user_id is null then
    select id into contributor_id from public.contributors where auth_user_id = p_auth_user_id;
    if contributor_id is null then
      insert into public.contributors (name, kind, auth_user_id, auth_user_id_migration_pending)
      values ('auth:' || p_auth_user_id::text, 'human', p_auth_user_id, false)
      returning id into contributor_id;
    end if;
    update public.contributor_auth_requests
       set approved_auth_user_id = p_auth_user_id, approved_at = now()
     where id = r.id;
  else
    select id into contributor_id from public.contributors where auth_user_id = p_auth_user_id;
  end if;
  return jsonb_build_object('status', 'approved', 'contributor_id', contributor_id::text);
end;
$$;

create or replace function public.hivemind_auth_redeem_request(
  p_request_token text, p_poll_secret text
) returns jsonb
language plpgsql security definer
set search_path = public, extensions, pg_temp
as $$
declare r public.contributor_auth_requests%rowtype; key_value text; key_id uuid;
begin
  select * into r from public.contributor_auth_requests
   where request_token_hash = public.hivemind_secret_hash(p_request_token)
   for update;
  if not found or r.poll_secret_hash <> public.hivemind_secret_hash(p_poll_secret)
     or r.revoked_at is not null or r.expires_at <= now()
     or r.approved_auth_user_id is null or r.consumed_at is not null then
    raise exception 'broker request unavailable' using errcode = '42501';
  end if;
  key_value := 'hm_' || encode(gen_random_bytes(32), 'hex');
  insert into public.contributor_keys (contributor_id, key_hash, label)
  select c.id, public.hivemind_secret_hash(key_value), r.machine_label
    from public.contributors c where c.auth_user_id = r.approved_auth_user_id
  returning id into key_id;
  if key_id is null then
    raise exception 'approved contributor unavailable' using errcode = '42501';
  end if;
  update public.contributor_auth_requests
     set consumed_at = now(), issued_key_id = key_id
   where id = r.id;
  -- key_value exists only as a local return value; it is never persisted.
  return jsonb_build_object('status', 'redeemed', 'key', key_value, 'key_id', key_id::text);
end;
$$;

-- Recovery for a client that may have lost the successful redemption response
-- after the transaction committed.  The poll secret is the same local
-- capability used for redemption; the request must already be consumed, and
-- this function only revokes its recorded key.  It never inserts or mints a
-- replacement key and is idempotent after the first successful cleanup.
create or replace function public.hivemind_auth_cleanup_request(
  p_request_token text, p_poll_secret text
) returns jsonb
language plpgsql security definer
set search_path = public, pg_temp
as $$
declare r public.contributor_auth_requests%rowtype; changed integer;
begin
  select * into r from public.contributor_auth_requests
   where request_token_hash = public.hivemind_secret_hash(p_request_token)
   for update;
  if not found or r.poll_secret_hash <> public.hivemind_secret_hash(p_poll_secret)
     or r.consumed_at is null then
    raise exception 'broker request unavailable' using errcode = '42501';
  end if;
  update public.contributor_keys
     set revoked_at = coalesce(revoked_at, now())
   where id = r.issued_key_id
     and revoked_at is null;
  get diagnostics changed = row_count;
  return jsonb_build_object(
    'status', case when changed > 0 then 'cleaned_up' else 'cleanup_not_needed' end,
    'revoked', changed > 0,
    'key_id', r.issued_key_id::text
  );
end;
$$;

create or replace function public.hivemind_auth_revoke_request(
  p_request_token text, p_poll_secret text
) returns jsonb
language plpgsql security definer
set search_path = public, pg_temp
as $$
declare changed integer;
begin
  update public.contributor_auth_requests
     set revoked_at = coalesce(revoked_at, now())
   where request_token_hash = public.hivemind_secret_hash(p_request_token)
     and poll_secret_hash = public.hivemind_secret_hash(p_poll_secret)
     and revoked_at is null and consumed_at is null;
  get diagnostics changed = row_count;
  return jsonb_build_object('revoked', changed > 0);
end;
$$;

create or replace function public.hivemind_auth_revoke_key(p_key text)
returns jsonb
language plpgsql security definer
set search_path = public, pg_temp
as $$
declare changed integer;
begin
  update public.contributor_keys
     set revoked_at = coalesce(revoked_at, now())
   where key_hash = public.hivemind_secret_hash(p_key)
     and revoked_at is null;
  get diagnostics changed = row_count;
  return jsonb_build_object('revoked', changed > 0);
end;
$$;

create or replace function public.hivemind_auth_key_status(p_key text)
returns jsonb
language plpgsql security definer
set search_path = public, pg_temp
as $$
declare r record;
begin
  select k.id, k.created_at, k.last_used_at, k.revoked_at, c.revoked_at as contributor_revoked,
         c.auth_user_id
    into r from public.contributor_keys k
    join public.contributors c on c.id = k.contributor_id
   where k.key_hash = public.hivemind_secret_hash(p_key);
  if not found then return jsonb_build_object('status', 'unknown'); end if;
  return jsonb_build_object('status', case when r.revoked_at is not null or r.contributor_revoked is not null then 'revoked'
                                           when r.auth_user_id is null then 'claim_pending'
                                           else 'active' end,
                            'key_id', r.id::text, 'created_at', r.created_at,
                            'last_used_at', r.last_used_at, 'revoked_at', r.revoked_at);
end;
$$;

do $$
declare fn text;
begin
  foreach fn in array array[
    'hivemind_secret_hash(text)',
    'hivemind_resolve_contributor_key(text)',
    'hivemind_claim_contributor(text,bigint,uuid)',
    'hivemind_auth_create_request(text,text,text,text,integer)',
    'hivemind_auth_get_request(text)',
    'hivemind_auth_poll_request(text,text)',
    'hivemind_auth_approve_request(text,text,uuid)',
    'hivemind_auth_redeem_request(text,text)',
    'hivemind_auth_cleanup_request(text,text)',
    'hivemind_auth_revoke_request(text,text)',
    'hivemind_auth_revoke_key(text)',
    'hivemind_auth_key_status(text)'
  ] loop
    execute format('revoke all on function public.%s from public, anon, authenticated;', fn);
    if exists (select 1 from pg_roles where rolname = 'service_role') then
      execute format('grant execute on function public.%s to service_role;', fn);
    end if;
  end loop;
end
$$;
