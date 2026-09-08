-- Hivemind knowledge model foundation (T2-T6).
--
-- This migration is intentionally additive.  The legacy external_resources /
-- distillations tables remain available until the separately authorized
-- conversion task.  New writes use the RPCs below; callers never write these
-- tables directly.  IDs are bigint in PostgreSQL and are serialized as text
-- by every RPC response.

create table if not exists public.resources (
  id bigint generated always as identity primary key,
  current_revision_id bigint,
  created_by bigint not null references public.contributors(id),
  origin_source text,
  origin_external_id text,
  canonical_guide boolean not null default false,
  created_at timestamptz not null default now(),
  constraint resources_origin_pair_chk check (
    (origin_source is null and origin_external_id is null)
    or (origin_source is not null and origin_external_id is not null)
  )
);

create unique index if not exists resources_origin_unique
  on public.resources (origin_source, origin_external_id)
  where origin_source is not null and origin_external_id is not null;

create table if not exists public.resource_revisions (
  id bigint generated always as identity primary key,
  resource_id bigint not null references public.resources(id),
  base_revision_id bigint references public.resource_revisions(id),
  kind text not null,
  title text not null,
  body text not null,
  payload jsonb,
  metadata jsonb not null default '{}'::jsonb,
  provenance jsonb not null default '{}'::jsonb,
  reference_spec jsonb not null default '[]'::jsonb,
  submitted_by bigint not null references public.contributors(id),
  submitted_at timestamptz not null default now(),
  rationale text,
  state text not null default 'pending'
    check (state in ('pending', 'accepted', 'rejected', 'withdrawn')),
  decided_by bigint references public.contributors(id),
  decided_at timestamptz,
  decision_reason text,
  constraint resource_revisions_reference_spec_array_chk
    check (jsonb_typeof(reference_spec) = 'array'),
  constraint resource_revisions_metadata_object_chk
    check (jsonb_typeof(metadata) = 'object'),
  constraint resource_revisions_provenance_object_chk
    check (jsonb_typeof(provenance) = 'object')
);

alter table public.resources
  drop constraint if exists resources_current_revision_fk;
alter table public.resources
  add constraint resources_current_revision_fk
  foreign key (current_revision_id) references public.resource_revisions(id)
  deferrable initially deferred;

create index if not exists resource_revisions_resource_idx
  on public.resource_revisions (resource_id, id);
create index if not exists resource_revisions_state_idx
  on public.resource_revisions (state);

-- A caller-scoped token makes retries return the original result and rejects
-- accidental reuse for a different request.  The request hash is deliberately
-- over the complete RPC input, not just the resource identity.
create table if not exists public.knowledge_idempotency (
  contributor_id bigint not null references public.contributors(id),
  action text not null,
  token text not null,
  request_hash text not null,
  response jsonb not null,
  created_at timestamptz not null default now(),
  primary key (contributor_id, action, token)
);

create table if not exists public.message_snapshots (
  id bigint generated always as identity primary key,
  message_id bigint not null,
  content text not null,
  source_metadata jsonb not null default '{}'::jsonb,
  original_author_id bigint,
  original_author_name text,
  observed_at timestamptz not null,
  captured_by bigint not null references public.contributors(id),
  captured_at timestamptz not null default now(),
  constraint message_snapshots_metadata_object_chk
    check (jsonb_typeof(source_metadata) = 'object')
);

create index if not exists message_snapshots_message_idx
  on public.message_snapshots (message_id, id desc);

create table if not exists public.evidence (
  id bigint generated always as identity primary key,
  claim text not null,
  conditions text,
  reported_result text not null,
  basis text not null check (basis in ('reported', 'observed')),
  sources jsonb not null default '[]'::jsonb,
  supersedes_evidence_id bigint references public.evidence(id),
  submitted_by bigint not null references public.contributors(id),
  submitted_at timestamptz not null default now(),
  constraint evidence_sources_array_chk check (jsonb_typeof(sources) = 'array')
);

create table if not exists public.evidence_subjects (
  evidence_id bigint not null references public.evidence(id) on delete cascade,
  target_kind text not null check (target_kind in ('resource', 'message', 'revision', 'evidence')),
  target_id bigint not null,
  target_version_id bigint,
  primary key (evidence_id, target_kind, target_id, target_version_id)
);

create table if not exists public.evidence_sources (
  evidence_id bigint not null references public.evidence(id) on delete cascade,
  source_index integer not null,
  target_kind text check (target_kind in ('resource', 'message', 'revision', 'evidence')),
  target_id bigint,
  target_version_id bigint,
  external_url text,
  label text,
  metadata jsonb not null default '{}'::jsonb,
  primary key (evidence_id, source_index),
  constraint evidence_sources_target_or_url_chk check (
    (target_kind is not null and target_id is not null and external_url is null)
    or (target_kind is null and target_id is null and external_url is not null)
  )
);

-- This is a derived index.  It is populated by trusted revision/evidence
-- writes and has no public INSERT/UPDATE/DELETE policy.
create table if not exists public.knowledge_references (
  id bigint generated always as identity primary key,
  source_kind text not null check (source_kind in ('revision', 'evidence')),
  source_id bigint not null,
  target_kind text not null check (target_kind in ('resource', 'message', 'revision', 'evidence')),
  target_id bigint not null,
  target_version_id bigint,
  label text,
  created_by bigint not null references public.contributors(id),
  created_at timestamptz not null default now(),
  unique (source_kind, source_id, target_kind, target_id, target_version_id)
);

create index if not exists knowledge_references_target_idx
  on public.knowledge_references (target_kind, target_id, target_version_id);
create index if not exists knowledge_references_source_idx
  on public.knowledge_references (source_kind, source_id);

-- Add the editor bit to the existing contributor identity without changing
-- current keys or rows.
alter table public.contributors
  add column if not exists is_editor boolean not null default false;

-- ------------------------------ immutable guards -------------------------

create or replace function public.hivemind_revision_guard()
returns trigger
language plpgsql
set search_path = public, pg_temp
as $$
begin
  if tg_op = 'DELETE' then
    raise exception 'revision % is immutable and cannot be deleted', old.id using errcode = '55000';
  end if;
  if tg_op = 'UPDATE' then
    if old.state <> 'pending' then
      raise exception 'terminal revision % is immutable', old.id using errcode = '55000';
    end if;
    if new.resource_id is distinct from old.resource_id
       or new.base_revision_id is distinct from old.base_revision_id
       or new.kind is distinct from old.kind
       or new.title is distinct from old.title
       or new.body is distinct from old.body
       or new.payload is distinct from old.payload
       or new.metadata is distinct from old.metadata
       or new.provenance is distinct from old.provenance
       or new.reference_spec is distinct from old.reference_spec
       or new.submitted_by is distinct from old.submitted_by
       or new.submitted_at is distinct from old.submitted_at
       or new.rationale is distinct from old.rationale then
      raise exception 'revision % content and ownership are immutable', old.id using errcode = '55000';
    end if;
    if new.state not in ('accepted', 'rejected', 'withdrawn') then
      raise exception 'revision % can only move pending to one terminal state', old.id using errcode = '22023';
    end if;
    if new.decided_by is null or new.decided_at is null then
      raise exception 'terminal revision % requires decision attribution', old.id using errcode = '23514';
    end if;
  end if;
  return new;
end;
$$;

drop trigger if exists resource_revisions_immutable on public.resource_revisions;
create trigger resource_revisions_immutable
before update or delete on public.resource_revisions
for each row execute function public.hivemind_revision_guard();

create or replace function public.hivemind_resource_guard()
returns trigger
language plpgsql
set search_path = public, pg_temp
as $$
begin
  if tg_op = 'DELETE' then
    raise exception 'resource % identity is immutable and cannot be deleted', old.id using errcode = '55000';
  end if;
  if old.id is distinct from new.id
     or old.created_by is distinct from new.created_by
     or old.origin_source is distinct from new.origin_source
     or old.origin_external_id is distinct from new.origin_external_id
     or old.created_at is distinct from new.created_at then
    raise exception 'resource identity and provenance are immutable' using errcode = '55000';
  end if;
  return new;
end;
$$;

drop trigger if exists resources_immutable on public.resources;
create trigger resources_immutable before update or delete on public.resources
for each row execute function public.hivemind_resource_guard();

create or replace function public.hivemind_evidence_guard()
returns trigger
language plpgsql
set search_path = public, pg_temp
as $$
begin
  raise exception 'evidence is immutable' using errcode = '55000';
end;
$$;

drop trigger if exists evidence_immutable on public.evidence;
create trigger evidence_immutable before update or delete on public.evidence
for each row execute function public.hivemind_evidence_guard();

drop trigger if exists message_snapshots_immutable on public.message_snapshots;
create trigger message_snapshots_immutable before update or delete on public.message_snapshots
for each row execute function public.hivemind_evidence_guard();

-- ------------------------------ helpers -----------------------------------

create or replace function public.hivemind_active_contributor(p_id bigint)
returns boolean
language sql stable
set search_path = public, pg_temp
as $$
  select exists (
    select 1 from public.contributors
    where id = p_id and revoked_at is null
  )
$$;

create or replace function public.hivemind_request_hash(p_value jsonb)
returns text language sql immutable set search_path = public, pg_temp
as $$ select md5(coalesce(p_value, '{}'::jsonb)::text) $$;

create or replace function public.hivemind_text_diff(p_old text, p_new text)
returns text
language plpgsql immutable
set search_path = public, pg_temp
as $$
declare
  line text;
  result text := '--- base' || E'\n' || '+++ candidate' || E'\n';
begin
  if coalesce(p_old, '') <> '' then
    for line in select value from regexp_split_to_table(p_old, E'\n') as value loop
      result := result || '-' || line || E'\n';
    end loop;
  end if;
  if coalesce(p_new, '') <> '' then
    for line in select value from regexp_split_to_table(p_new, E'\n') as value loop
      result := result || '+' || line || E'\n';
    end loop;
  end if;
  return result;
end;
$$;

create or replace function public.hivemind_revision_diff(p_revision_id bigint)
returns jsonb
language sql stable
set search_path = public, pg_temp
as $$
  with candidate as (
    select r.*, b.body as base_body, b.kind as base_kind, b.title as base_title,
           b.payload as base_payload, b.metadata as base_metadata,
           b.provenance as base_provenance
      from public.resource_revisions r
      left join public.resource_revisions b on b.id = r.base_revision_id
     where r.id = p_revision_id
  )
  select jsonb_build_object(
    'revision_id', id::text,
    'base_revision_id', case when base_revision_id is null then null else base_revision_id::text end,
    'text_diff', public.hivemind_text_diff(coalesce(base_body, ''), body),
    'json_diff', jsonb_build_object(
      'base', jsonb_build_object('kind', base_kind, 'title', base_title,
                                 'body', base_body, 'payload', base_payload,
                                 'metadata', base_metadata, 'provenance', base_provenance),
      'candidate', jsonb_build_object('kind', kind, 'title', title,
                                      'body', body, 'payload', payload,
                                      'metadata', metadata, 'provenance', provenance)
    )
  )
  from candidate
$$;

create or replace function public.hivemind_validate_target(
  p_kind text, p_id bigint, p_version_id bigint default null
)
returns void
language plpgsql stable
set search_path = public, pg_temp
as $$
declare
  owner_id bigint;
begin
  if p_id is null or p_id < 1 then
    raise exception 'target id must be a positive integer' using errcode = '22023';
  end if;
  if p_kind = 'resource' then
    if not exists (select 1 from public.resources where id = p_id) then
      raise exception 'unknown resource target %', p_id using errcode = '23503';
    end if;
    if p_version_id is not null then
      select resource_id into owner_id from public.resource_revisions where id = p_version_id;
      if owner_id is null then
        raise exception 'unknown resource revision target %', p_version_id using errcode = '23503';
      end if;
      if owner_id <> p_id then
        raise exception 'resource % does not own revision %', p_id, p_version_id using errcode = '23514';
      end if;
    end if;
  elsif p_kind = 'revision' then
    if p_version_id is not null or not exists (select 1 from public.resource_revisions where id = p_id) then
      raise exception 'revision target must be an existing unversioned revision id' using errcode = '23503';
    end if;
  elsif p_kind = 'evidence' then
    if p_version_id is not null or not exists (select 1 from public.evidence where id = p_id) then
      raise exception 'evidence target must be an existing unversioned evidence id' using errcode = '23503';
    end if;
  elsif p_kind = 'message' then
    if p_version_id is not null then
      if not exists (select 1 from public.message_snapshots where id = p_version_id and message_id = p_id) then
        raise exception 'message % does not own snapshot %', p_id, p_version_id using errcode = '23514';
      end if;
    elsif to_regclass('public.message_feed') is null
       or not exists (select 1 from public.message_feed where message_id = p_id) then
      raise exception 'unknown current message target %', p_id using errcode = '23503';
    end if;
  else
    raise exception 'unknown reference target kind %', p_kind using errcode = '22023';
  end if;
end;
$$;

create or replace function public.hivemind_insert_reference(
  p_source_kind text, p_source_id bigint, p_target_kind text,
  p_target_id bigint, p_target_version_id bigint, p_label text, p_created_by bigint
)
returns bigint
language plpgsql
set search_path = public, pg_temp
as $$
declare
  ref_id bigint;
begin
  if p_source_kind = 'revision' then
    if not exists (select 1 from public.resource_revisions where id = p_source_id) then
      raise exception 'unknown source revision %', p_source_id using errcode = '23503';
    end if;
  elsif p_source_kind = 'evidence' then
    if not exists (select 1 from public.evidence where id = p_source_id) then
      raise exception 'unknown source evidence %', p_source_id using errcode = '23503';
    end if;
  else
    raise exception 'unknown reference source kind %', p_source_kind using errcode = '22023';
  end if;
  perform public.hivemind_validate_target(p_target_kind, p_target_id, p_target_version_id);
  insert into public.knowledge_references
    (source_kind, source_id, target_kind, target_id, target_version_id, label, created_by)
  values
    (p_source_kind, p_source_id, p_target_kind, p_target_id, p_target_version_id, p_label, p_created_by)
  on conflict do nothing returning id into ref_id;
  if ref_id is null then
    select id into ref_id from public.knowledge_references
     where source_kind = p_source_kind and source_id = p_source_id
       and target_kind = p_target_kind and target_id = p_target_id
       and target_version_id is not distinct from p_target_version_id;
  end if;
  return ref_id;
end;
$$;

create or replace function public.hivemind_reference_array(
  p_source_kind text, p_source_id bigint, p_created_by bigint, p_refs jsonb
)
returns integer
language plpgsql
set search_path = public, pg_temp
as $$
declare
  item jsonb;
  n integer := 0;
  kind text;
  label text;
  target_id bigint;
  version_id bigint;
begin
  if p_refs is null or jsonb_typeof(p_refs) <> 'array' then
    raise exception 'references must be an array' using errcode = '22023';
  end if;
  for item in select value from jsonb_array_elements(p_refs) loop
    kind := item->>'target_kind';
    target_id := (item->>'target_id')::bigint;
    version_id := nullif(item->>'target_version_id', '')::bigint;
    label := item->>'label';
    perform public.hivemind_insert_reference(p_source_kind, p_source_id, kind,
                                              target_id, version_id, label, p_created_by);
    n := n + 1;
  end loop;
  return n;
end;
$$;

create or replace function public.hivemind_store_idempotency(
  p_contributor_id bigint, p_action text, p_token text,
  p_request_hash text, p_response jsonb
)
returns jsonb
language plpgsql
set search_path = public, pg_temp
as $$
declare
  prior public.knowledge_idempotency%rowtype;
begin
  if p_token is null or length(btrim(p_token)) = 0 or length(p_token) > 200 then
    raise exception 'idempotency token is required and must be at most 200 characters' using errcode = '22023';
  end if;
  select * into prior from public.knowledge_idempotency
   where contributor_id = p_contributor_id and action = p_action and token = p_token
   for update;
  if found then
    if prior.request_hash <> p_request_hash then
      raise exception 'idempotency token was reused for different content' using errcode = '23505';
    end if;
    return prior.response || jsonb_build_object('idempotent_replay', true);
  end if;
  insert into public.knowledge_idempotency
    (contributor_id, action, token, request_hash, response)
  values (p_contributor_id, p_action, p_token, p_request_hash, p_response);
  return p_response;
end;
$$;

-- ------------------------------ trusted writes ----------------------------

create or replace function public.hivemind_submit_resource(
  p_submitter bigint, p_idempotency_token text, p_kind text, p_title text,
  p_body text, p_payload jsonb default null, p_metadata jsonb default '{}'::jsonb,
  p_provenance jsonb default '{}'::jsonb, p_origin_source text default null,
  p_origin_external_id text default null, p_rationale text default null,
  p_references jsonb default '[]'::jsonb
)
returns jsonb
language plpgsql security definer
set search_path = public, pg_temp
as $$
declare
  resource_row public.resources%rowtype;
  revision_row public.resource_revisions%rowtype;
  input jsonb;
  response jsonb;
begin
  if not public.hivemind_active_contributor(p_submitter) then
    raise exception 'contributor is not authorized' using errcode = '42501';
  end if;
  if nullif(btrim(p_kind), '') is null or nullif(btrim(p_title), '') is null
     or nullif(btrim(p_body), '') is null then
    raise exception 'kind, title, and body are required' using errcode = '22023';
  end if;
  input := jsonb_build_object('kind',p_kind,'title',p_title,'body',p_body,
    'payload',p_payload,'metadata',p_metadata,'provenance',p_provenance,
    'origin_source',p_origin_source,'origin_external_id',p_origin_external_id,
    'rationale',p_rationale,'references',p_references);
  -- A duplicate token must be checked before any side effects.  Replayed
  -- requests return the original response from this same transaction.
  if exists (select 1 from public.knowledge_idempotency where contributor_id=p_submitter
             and action='submit_resource' and token=p_idempotency_token) then
    return public.hivemind_store_idempotency(p_submitter,'submit_resource',p_idempotency_token,
      public.hivemind_request_hash(input), '{}'::jsonb);
  end if;
  insert into public.resources (created_by, origin_source, origin_external_id)
    values (p_submitter, p_origin_source, p_origin_external_id)
    returning * into resource_row;
  insert into public.resource_revisions
    (resource_id, kind, title, body, payload, metadata, provenance, reference_spec,
     submitted_by, rationale)
  values
    (resource_row.id, p_kind, p_title, p_body, p_payload, coalesce(p_metadata,'{}'),
     coalesce(p_provenance,'{}'), coalesce(p_references,'[]'), p_submitter, p_rationale)
  returning * into revision_row;
  perform public.hivemind_reference_array('revision', revision_row.id, p_submitter,
                                          coalesce(p_references,'[]'));
  response := jsonb_build_object('status','pending','resource_id',resource_row.id::text,
    'revision_id',revision_row.id::text,'base_revision_id',null,'state',revision_row.state,
    'diff',public.hivemind_revision_diff(revision_row.id));
  return public.hivemind_store_idempotency(p_submitter,'submit_resource',p_idempotency_token,
    public.hivemind_request_hash(input),response);
exception when unique_violation then
  raise;
end;
$$;

create or replace function public.hivemind_propose_revision(
  p_submitter bigint, p_idempotency_token text, p_resource_id bigint,
  p_base_revision_id bigint, p_kind text, p_title text, p_body text,
  p_payload jsonb default null, p_metadata jsonb default '{}'::jsonb,
  p_provenance jsonb default '{}'::jsonb, p_rationale text default null,
  p_references jsonb default '[]'::jsonb
)
returns jsonb
language plpgsql security definer
set search_path = public, pg_temp
as $$
declare
  resource_row public.resources%rowtype;
  revision_row public.resource_revisions%rowtype;
  input jsonb;
  response jsonb;
begin
  if not public.hivemind_active_contributor(p_submitter) then
    raise exception 'contributor is not authorized' using errcode = '42501';
  end if;
  input := jsonb_build_object('resource_id',p_resource_id::text,'base_revision_id',p_base_revision_id::text,
    'kind',p_kind,'title',p_title,'body',p_body,'payload',p_payload,'metadata',p_metadata,
    'provenance',p_provenance,'rationale',p_rationale,'references',p_references);
  if exists (select 1 from public.knowledge_idempotency where contributor_id=p_submitter
             and action='propose_revision' and token=p_idempotency_token) then
    return public.hivemind_store_idempotency(p_submitter,'propose_revision',p_idempotency_token,
      public.hivemind_request_hash(input), '{}'::jsonb);
  end if;
  select * into resource_row from public.resources where id=p_resource_id for update;
  if not found then raise exception 'unknown resource %',p_resource_id using errcode='23503'; end if;
  if resource_row.current_revision_id is null or resource_row.current_revision_id <> p_base_revision_id then
    raise exception 'stale base revision: resource % is at %, not %', p_resource_id,
      coalesce(resource_row.current_revision_id::text,'null'), p_base_revision_id using errcode='40001';
  end if;
  if not exists (select 1 from public.resource_revisions where id=p_base_revision_id
                 and resource_id=p_resource_id and state='accepted') then
    raise exception 'base revision is not the accepted head' using errcode='23514';
  end if;
  insert into public.resource_revisions
    (resource_id,base_revision_id,kind,title,body,payload,metadata,provenance,reference_spec,submitted_by,rationale)
  values (p_resource_id,p_base_revision_id,p_kind,p_title,p_body,p_payload,coalesce(p_metadata,'{}'),
          coalesce(p_provenance,'{}'),coalesce(p_references,'[]'),p_submitter,p_rationale)
  returning * into revision_row;
  perform public.hivemind_reference_array('revision', revision_row.id, p_submitter,
                                          coalesce(p_references,'[]'));
  response := jsonb_build_object('status','pending','resource_id',p_resource_id::text,
    'revision_id',revision_row.id::text,'base_revision_id',p_base_revision_id::text,
    'state',revision_row.state,'diff',public.hivemind_revision_diff(revision_row.id));
  return public.hivemind_store_idempotency(p_submitter,'propose_revision',p_idempotency_token,
    public.hivemind_request_hash(input),response);
end;
$$;

create or replace function public.hivemind_decide_revision(
  p_editor bigint, p_idempotency_token text, p_revision_id bigint,
  p_decision text, p_reason text default null
)
returns jsonb
language plpgsql security definer
set search_path = public, pg_temp
as $$
declare
  revision_row public.resource_revisions%rowtype;
  resource_row public.resources%rowtype;
  input jsonb;
  response jsonb;
begin
  if not exists (select 1 from public.contributors where id=p_editor and revoked_at is null and is_editor) then
    raise exception 'editor authorization required' using errcode = '42501';
  end if;
  if p_decision not in ('accepted','rejected','withdrawn') then
    raise exception 'decision must be accepted, rejected, or withdrawn' using errcode='22023';
  end if;
  input := jsonb_build_object('revision_id',p_revision_id::text,'decision',p_decision,'reason',p_reason);
  if exists (select 1 from public.knowledge_idempotency where contributor_id=p_editor
             and action='decide_revision' and token=p_idempotency_token) then
    return public.hivemind_store_idempotency(p_editor,'decide_revision',p_idempotency_token,
      public.hivemind_request_hash(input), '{}'::jsonb);
  end if;
  select r.* into revision_row from public.resource_revisions r where r.id=p_revision_id for update;
  if not found then raise exception 'unknown revision %',p_revision_id using errcode='23503'; end if;
  select * into resource_row from public.resources where id=revision_row.resource_id for update;
  if revision_row.state <> 'pending' then
    raise exception 'revision % is already terminal',p_revision_id using errcode='55000';
  end if;
  if resource_row.current_revision_id is distinct from revision_row.base_revision_id then
    raise exception 'stale base: revision % was based on %, current head is %',p_revision_id,
      coalesce(revision_row.base_revision_id::text,'null'),coalesce(resource_row.current_revision_id::text,'null') using errcode='40001';
  end if;
  update public.resource_revisions
     set state=p_decision, decided_by=p_editor, decided_at=now(), decision_reason=p_reason
   where id=p_revision_id;
  if p_decision='accepted' then
    update public.resources set current_revision_id=p_revision_id,
      canonical_guide=case when revision_row.kind='guide' then canonical_guide else false end
     where id=revision_row.resource_id;
  end if;
  response := jsonb_build_object('status',p_decision,'resource_id',revision_row.resource_id::text,
    'revision_id',p_revision_id::text,'current_revision_id',
    (case when p_decision='accepted' then p_revision_id else resource_row.current_revision_id end)::text,
    'state',p_decision);
  return public.hivemind_store_idempotency(p_editor,'decide_revision',p_idempotency_token,
    public.hivemind_request_hash(input),response);
end;
$$;

create or replace function public.hivemind_mark_canonical(
  p_editor bigint, p_idempotency_token text, p_resource_id bigint, p_canonical boolean
)
returns jsonb
language plpgsql security definer
set search_path = public, pg_temp
as $$
declare
  r public.resources%rowtype;
  rev public.resource_revisions%rowtype;
  input jsonb;
  response jsonb;
begin
  if not exists (select 1 from public.contributors where id=p_editor and revoked_at is null and is_editor) then
    raise exception 'editor authorization required' using errcode = '42501';
  end if;
  input := jsonb_build_object('resource_id',p_resource_id::text,'canonical',p_canonical);
  if exists (select 1 from public.knowledge_idempotency where contributor_id=p_editor
             and action='mark_canonical' and token=p_idempotency_token) then
    return public.hivemind_store_idempotency(p_editor,'mark_canonical',p_idempotency_token,
      public.hivemind_request_hash(input), '{}'::jsonb);
  end if;
  select * into r from public.resources where id=p_resource_id for update;
  if not found then raise exception 'unknown resource %',p_resource_id using errcode='23503'; end if;
  if p_canonical then
    select * into rev from public.resource_revisions where id=r.current_revision_id;
    if not found or rev.state <> 'accepted' or rev.kind <> 'guide' then
      raise exception 'only an accepted guide can be canonical' using errcode='23514';
    end if;
  end if;
  update public.resources set canonical_guide=p_canonical where id=p_resource_id;
  response := jsonb_build_object('status','ok','resource_id',p_resource_id::text,
    'canonical_guide',p_canonical);
  return public.hivemind_store_idempotency(p_editor,'mark_canonical',p_idempotency_token,
    public.hivemind_request_hash(input),response);
end;
$$;

create or replace function public.hivemind_capture_message_snapshot(
  p_submitter bigint, p_idempotency_token text, p_message_id bigint,
  p_content text, p_source_metadata jsonb default '{}'::jsonb,
  p_original_author_id bigint default null, p_original_author_name text default null,
  p_observed_at timestamptz default now()
)
returns jsonb
language plpgsql security definer
set search_path = public, pg_temp
as $$
declare
  prior public.message_snapshots%rowtype;
  snapshot public.message_snapshots%rowtype;
  input jsonb;
begin
  if not public.hivemind_active_contributor(p_submitter) then
    raise exception 'contributor is not authorized' using errcode='42501';
  end if;
  if p_message_id is null or p_message_id < 1 or p_content is null then
    raise exception 'message id and content are required' using errcode='22023';
  end if;
  input := jsonb_build_object('message_id',p_message_id::text,'content',p_content,
    'source_metadata',p_source_metadata,'original_author_id',p_original_author_id,
    'original_author_name',p_original_author_name,'observed_at',p_observed_at);
  if exists (select 1 from public.knowledge_idempotency where contributor_id=p_submitter
             and action='capture_message_snapshot' and token=p_idempotency_token) then
    return public.hivemind_store_idempotency(p_submitter,'capture_message_snapshot',p_idempotency_token,
      public.hivemind_request_hash(input), '{}'::jsonb);
  end if;
  select * into prior from public.message_snapshots
   where message_id=p_message_id and content=p_content
     and source_metadata=coalesce(p_source_metadata,'{}')
     and original_author_id is not distinct from p_original_author_id
     and original_author_name is not distinct from p_original_author_name
   order by id desc limit 1;
  if found then
    return public.hivemind_store_idempotency(p_submitter,'capture_message_snapshot',p_idempotency_token,
      public.hivemind_request_hash(input),jsonb_build_object('status','existing','snapshot_id',prior.id::text,
        'message_id',p_message_id::text,'captured_by',prior.captured_by::text));
  end if;
  insert into public.message_snapshots
    (message_id,content,source_metadata,original_author_id,original_author_name,observed_at,captured_by)
  values (p_message_id,p_content,coalesce(p_source_metadata,'{}'),p_original_author_id,
          p_original_author_name,p_observed_at,p_submitter)
  returning * into snapshot;
  return public.hivemind_store_idempotency(p_submitter,'capture_message_snapshot',p_idempotency_token,
    public.hivemind_request_hash(input),jsonb_build_object('status','captured','snapshot_id',snapshot.id::text,
      'message_id',p_message_id::text,'captured_by',p_submitter::text));
end;
$$;

create or replace function public.hivemind_submit_evidence(
  p_submitter bigint, p_idempotency_token text, p_claim text, p_conditions text,
  p_reported_result text, p_basis text, p_subjects jsonb, p_sources jsonb default '[]'::jsonb,
  p_supersedes_evidence_id bigint default null
)
returns jsonb
language plpgsql security definer
set search_path = public, pg_temp
as $$
declare
  e public.evidence%rowtype;
  item jsonb;
  source_item jsonb;
  i integer := 0;
  input jsonb;
  kind text;
  target_id bigint;
  version_id bigint;
begin
  if not public.hivemind_active_contributor(p_submitter) then
    raise exception 'contributor is not authorized' using errcode='42501';
  end if;
  if nullif(btrim(p_claim),'') is null or nullif(btrim(p_reported_result),'') is null
     or p_basis not in ('reported','observed') or jsonb_typeof(p_subjects) <> 'array'
     or jsonb_array_length(p_subjects)=0 or jsonb_typeof(coalesce(p_sources,'[]')) <> 'array' then
    raise exception 'claim, result, basis, and at least one subject are required' using errcode='22023';
  end if;
  input := jsonb_build_object('claim',p_claim,'conditions',p_conditions,'reported_result',p_reported_result,
    'basis',p_basis,'subjects',p_subjects,'sources',p_sources,'supersedes',p_supersedes_evidence_id::text);
  if exists (select 1 from public.knowledge_idempotency where contributor_id=p_submitter
             and action='submit_evidence' and token=p_idempotency_token) then
    return public.hivemind_store_idempotency(p_submitter,'submit_evidence',p_idempotency_token,
      public.hivemind_request_hash(input), '{}'::jsonb);
  end if;
  if p_supersedes_evidence_id is not null and not exists
    (select 1 from public.evidence where id=p_supersedes_evidence_id) then
    raise exception 'unknown superseded evidence %',p_supersedes_evidence_id using errcode='23503';
  end if;
  insert into public.evidence (claim,conditions,reported_result,basis,sources,supersedes_evidence_id,submitted_by)
  values (p_claim,p_conditions,p_reported_result,p_basis,coalesce(p_sources,'[]'),p_supersedes_evidence_id,p_submitter)
  returning * into e;
  for item in select value from jsonb_array_elements(p_subjects) loop
    kind := item->>'target_kind'; target_id := (item->>'target_id')::bigint;
    version_id := nullif(item->>'target_version_id','')::bigint;
    if kind in ('resource','message') and version_id is null then
      raise exception 'evidence % subjects must pin % targets to an exact version',e.id,kind using errcode='23514';
    end if;
    perform public.hivemind_validate_target(kind,target_id,version_id);
    insert into public.evidence_subjects (evidence_id,target_kind,target_id,target_version_id)
      values (e.id,kind,target_id,version_id);
    perform public.hivemind_insert_reference('evidence',e.id,kind,target_id,version_id,null,p_submitter);
  end loop;
  for source_item in select value from jsonb_array_elements(coalesce(p_sources,'[]')) loop
    if source_item ? 'external_url' then
      insert into public.evidence_sources (evidence_id,source_index,external_url,label,metadata)
        values (e.id,i,source_item->>'external_url',source_item->>'label',coalesce(source_item->'metadata','{}'));
    else
      kind := source_item->>'target_kind'; target_id := (source_item->>'target_id')::bigint;
      version_id := nullif(source_item->>'target_version_id','')::bigint;
      perform public.hivemind_validate_target(kind,target_id,version_id);
      insert into public.evidence_sources (evidence_id,source_index,target_kind,target_id,target_version_id,label,metadata)
        values (e.id, i, kind,target_id,version_id,source_item->>'label',coalesce(source_item->'metadata','{}'));
      perform public.hivemind_insert_reference('evidence',e.id,kind,target_id,version_id,source_item->>'label',p_submitter);
    end if;
    i := i + 1;
  end loop;
  return public.hivemind_store_idempotency(p_submitter,'submit_evidence',p_idempotency_token,
    public.hivemind_request_hash(input),jsonb_build_object('status','recorded','evidence_id',e.id::text,
      'submitted_by',p_submitter::text,'basis',p_basis));
end;
$$;

-- Editor grant/revoke is operator-only.  Supabase's service_role and the
-- cluster owner are the trusted operators; contributor keys cannot self-grant.
create or replace function public.hivemind_set_editor(p_operator text, p_contributor_id bigint, p_is_editor boolean)
returns jsonb language plpgsql security definer
set search_path = public, pg_temp
as $$
begin
  if p_operator not in ('service_role','postgres')
     or (current_user not in ('service_role','postgres')
         and not pg_has_role(current_user, 'pg_database_owner', 'member')) then
    raise exception 'operator authorization required' using errcode='42501';
  end if;
  update public.contributors set is_editor=p_is_editor where id=p_contributor_id;
  if not found then raise exception 'unknown contributor %',p_contributor_id using errcode='23503'; end if;
  return jsonb_build_object('contributor_id',p_contributor_id::text,'is_editor',p_is_editor);
end;
$$;

revoke all on table public.resources, public.resource_revisions, public.message_snapshots,
  public.evidence, public.evidence_subjects, public.evidence_sources,
  public.knowledge_references, public.knowledge_idempotency from public, anon, authenticated;

-- ------------------------------ read projections --------------------------

drop view if exists public.knowledge_outgoing_references;
create view public.knowledge_outgoing_references
with (security_invoker = true) as
select id::text as reference_id, source_kind, source_id::text as source_id,
       target_kind, target_id::text as target_id,
       case when target_version_id is null then null else target_version_id::text end as target_version_id,
       label, created_at
  from public.knowledge_references;

drop view if exists public.knowledge_backlinks;
create view public.knowledge_backlinks
with (security_invoker = true) as
select id::text as reference_id, source_kind, source_id::text as source_id,
       target_kind, target_id::text as target_id,
       case when target_version_id is null then null else target_version_id::text end as target_version_id,
       label, created_at
  from public.knowledge_references;

-- Public reads are intentionally status-bearing.  No public write policy is
-- created; only the RPCs above are executable by service_role.
alter table public.resources enable row level security;
alter table public.resource_revisions enable row level security;
alter table public.message_snapshots enable row level security;
alter table public.evidence enable row level security;
alter table public.evidence_subjects enable row level security;
alter table public.evidence_sources enable row level security;
alter table public.knowledge_references enable row level security;

do $$
begin
  if not exists (select 1 from pg_policies where schemaname='public' and tablename='resources' and policyname='public read knowledge resources') then
    create policy "public read knowledge resources" on public.resources for select using (true);
  end if;
  if not exists (select 1 from pg_policies where schemaname='public' and tablename='resource_revisions' and policyname='public read knowledge revisions') then
    create policy "public read knowledge revisions" on public.resource_revisions for select using (true);
  end if;
  if not exists (select 1 from pg_policies where schemaname='public' and tablename='message_snapshots' and policyname='public read message snapshots') then
    create policy "public read message snapshots" on public.message_snapshots for select using (true);
  end if;
  if not exists (select 1 from pg_policies where schemaname='public' and tablename='evidence' and policyname='public read evidence') then
    create policy "public read evidence" on public.evidence for select using (true);
  end if;
  if not exists (select 1 from pg_policies where schemaname='public' and tablename='evidence_subjects' and policyname='public read evidence subjects') then
    create policy "public read evidence subjects" on public.evidence_subjects for select using (true);
  end if;
  if not exists (select 1 from pg_policies where schemaname='public' and tablename='evidence_sources' and policyname='public read evidence sources') then
    create policy "public read evidence sources" on public.evidence_sources for select using (true);
  end if;
  if not exists (select 1 from pg_policies where schemaname='public' and tablename='knowledge_references' and policyname='public read knowledge references') then
    create policy "public read knowledge references" on public.knowledge_references for select using (true);
  end if;
end $$;

do $$
declare
  fn text;
begin
  foreach fn in array array[
    'hivemind_submit_resource(bigint,text,text,text,text,jsonb,jsonb,jsonb,text,text,text,jsonb)',
    'hivemind_propose_revision(bigint,text,bigint,bigint,text,text,text,jsonb,jsonb,jsonb,text,jsonb)',
    'hivemind_decide_revision(bigint,text,bigint,text,text)',
    'hivemind_mark_canonical(bigint,text,bigint,boolean)',
    'hivemind_capture_message_snapshot(bigint,text,bigint,text,jsonb,bigint,text,timestamptz)',
    'hivemind_submit_evidence(bigint,text,text,text,text,text,jsonb,jsonb,bigint)'
  ] loop
    execute format('revoke all on function public.%s from public, anon, authenticated;', fn);
    if exists (select 1 from pg_roles where rolname='service_role') then
      execute format('grant execute on function public.%s to service_role;', fn);
    end if;
  end loop;
  revoke all on function public.hivemind_set_editor(text,bigint,boolean) from public, anon, authenticated;
  if exists (select 1 from pg_roles where rolname='service_role') then
    grant execute on function public.hivemind_set_editor(text,bigint,boolean) to service_role;
  end if;
end $$;

grant select on public.resources, public.resource_revisions, public.message_snapshots,
  public.evidence, public.evidence_subjects, public.evidence_sources,
  public.knowledge_references, public.knowledge_outgoing_references,
  public.knowledge_backlinks to anon, authenticated;

comment on table public.resource_revisions is
  'Immutable full candidate content. Only pending -> one terminal decision is mutable.';
comment on table public.evidence is
  'Immutable reported/observed claim about exact subjects; editorial acceptance is not truth.';
comment on table public.message_snapshots is
  'Trusted observations of current messages; captured_by is distinct from original author.';
