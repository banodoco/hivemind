#!/usr/bin/env python3
"""Real PostgreSQL proof for the T2-T6 knowledge-model foundation.

The runner is intentionally stdlib-only and refuses non-local databases.
Set HIVEMIND_TEST_DATABASE_URL to a run-owned URL such as
postgresql://hannahomalley@localhost:55432/hivemind_test.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = [ROOT / "schema" / "038_knowledge_model.sql", ROOT / "schema" / "039_knowledge_model_delivery.sql"]


def _checked_url(raw: str) -> str:
    parsed = urlparse(raw)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("HIVEMIND_TEST_DATABASE_URL must be a PostgreSQL URL")
    host = parsed.hostname or ""
    socket_host = parse_qs(parsed.query).get("host", [""])[0]
    if host not in {"", "localhost", "127.0.0.1", "::1"} and not socket_host.startswith("/tmp/"):
        raise ValueError("refusing non-disposable database host")
    database = (parsed.path or "").lstrip("/")
    if "test" not in database.lower() and "otto" not in raw.lower():
        raise ValueError("refusing database without a test/otto identity")
    return raw


def _psql(url: str, sql: str, *, file: str | None = None) -> str:
    command = ["psql", "-X", url, "-v", "ON_ERROR_STOP=1", "-q", "-t", "-A"]
    if file:
        command += ["-f", file]
    else:
        command += ["-c", sql]
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result.stdout.strip()


def _bootstrap(url: str) -> None:
    _psql(url, """
      create table if not exists public.contributors (
        id bigint generated always as identity primary key,
        name text not null unique, kind text not null,
        api_key_hash text, revoked_at timestamptz,
        created_at timestamptz not null default now()
      );
      create table if not exists public.discord_messages (
        message_id bigint primary key, content text not null,
        is_deleted boolean not null default false,
        author_id bigint, channel_id bigint, guild_id bigint,
        created_at timestamptz not null default now()
      );
      alter table public.discord_messages
        add column if not exists created_at timestamptz not null default now();
      alter table public.discord_messages add column if not exists author_id bigint;
      alter table public.discord_messages add column if not exists channel_id bigint;
      alter table public.discord_messages add column if not exists guild_id bigint;
      create extension if not exists vector;
      create extension if not exists pgcrypto;
      create table if not exists public.members (
        member_id bigint primary key, global_name text, username text,
        allow_content_sharing boolean not null default true,
        bot boolean not null default false, system boolean not null default false
      );
      create table if not exists public.lexical_documents (
        entity_type text not null, item_id text not null, representation_type text not null,
        chunk_index integer not null, chunk_text text not null, tsv tsvector,
        matched_anchor text, source_offset_start integer not null default 0,
        source_offset_end integer not null default 0, representation_hash text not null,
        chunk_hash text not null, quarantine_state text not null default 'safe',
        lexicalization_version integer not null default 1, canonicalization_version integer not null default 1,
        chunking_version integer not null default 1, secret_scan_version integer not null default 1,
        method text, primary key(entity_type,item_id,representation_type,chunk_index)
      );
      create table if not exists public.embedding_contracts (
        id bigint primary key, dimension integer not null, status text not null,
        provider text, model text, canonicalization_version integer default 1, chunking_version integer default 1
      );
      create table if not exists public.content_embeddings (
        contract_id bigint not null, entity_type text not null, item_id text not null,
        representation_type text not null default 'prose', chunk_index integer not null default 0,
        chunk_text text, embedding vector(384) not null, representation_hash text not null,
        chunk_hash text not null, embedded_at timestamptz not null default now(),
        primary key(contract_id,entity_type,item_id,representation_type,chunk_index)
      );
      create table if not exists public.embedding_jobs (
        id bigserial primary key, entity_type text not null, item_id text not null,
        representation_type text not null, job_kind text not null, contract_id bigint,
        source_revision_id bigint, source_op text, status text not null default 'pending',
        attempts integer not null default 0, max_attempts integer not null default 5,
        next_attempt_at timestamptz not null default now(), locked_at timestamptz,
        locked_by text, lease_expires_at timestamptz, last_error text,
        enqueued_at timestamptz not null default now(), updated_at timestamptz not null default now()
      );
      create unique index if not exists one_pending_job_per_identity_rep on public.embedding_jobs(entity_type,item_id,representation_type) where status='pending';
      create table if not exists public.content_representation_manifest (
        entity_type text not null, item_id text not null, representation_type text not null,
        representation_hash text not null, source_available boolean not null,
        public_state text, chunking_version integer not null default 2,
        chunk_config_identity text not null, chunks jsonb not null default '[]'::jsonb,
        chunk_count integer not null default 0, chunk_method text,
        primary key(entity_type,item_id,representation_type)
      );
      create table if not exists public.embedding_contract_config_registry (
        selected_contract_id bigint primary key, selection_state text not null,
        base_contract_id bigint, provider text, model text, dimension integer,
        canonicalization_version integer, chunking_version integer,
        chunk_config_version integer, prose_target_tokens integer,
        prose_overlap_tokens integer, python_target_tokens integer,
        python_overlap_tokens integer, chunk_config_identity text,
        eval_contract_id text, full_config_sha256 text
      );
      create or replace function public.hivemind_normalize_identifier(value text)
      returns text language sql immutable as $$
        select regexp_replace(lower(coalesce(value,'')), '[^a-z0-9]+', '', 'g')
      $$;
      create or replace function public.hivemind_representation_hash(value text)
      returns text language sql immutable as $$
        select encode(digest(coalesce(value,''),'sha256'),'hex')
      $$;
      create or replace function public.hivemind_canonical_resource_text(p_title text,p_body text,p_tags text)
      returns text language sql immutable as $$
        select btrim(concat_ws(E'\n\n',nullif(btrim(coalesce(p_title,'')),''),nullif(btrim(coalesce(p_body,'')),''),nullif(btrim(coalesce(p_tags,'')),'')))
      $$;
      create or replace function public.hivemind_workflow_prose(p_body text,p_kind text)
      returns text language sql immutable as $$ select coalesce(p_body,'') $$;
      create or replace function public.hivemind_resource_tags(p_metadata jsonb)
      returns text language sql immutable as $$ select coalesce(p_metadata->>'tags','') $$;
      create or replace function public.hivemind_workflow_semantics_text(p_metadata jsonb)
      returns text language sql immutable as $$ select coalesce(p_metadata->'workflow_semantics'->>'task_type','') $$;
      create or replace function public.hivemind_sanitize_error(value text)
      returns text language sql immutable as $$ select left(coalesce(value,''),512) $$;
      create or replace function public.hivemind_drop_embedding_chunks(p_contract_id bigint,p_entity_type text,p_item_id text,p_representation_type text)
      returns int language plpgsql as $$ declare n int; begin
        delete from public.content_embeddings where contract_id=p_contract_id and entity_type=p_entity_type and item_id=p_item_id and (p_representation_type is null or representation_type=p_representation_type);
        get diagnostics n=row_count; return n;
      end $$;
      insert into public.embedding_contract_config_registry(selected_contract_id,selection_state,base_contract_id,provider,model,dimension,canonicalization_version,chunking_version,chunk_config_version,prose_target_tokens,prose_overlap_tokens,python_target_tokens,python_overlap_tokens,chunk_config_identity,eval_contract_id,full_config_sha256)
      values(1360541028304258884,'active',6368594834396668537,'openai','text-embedding-3-small',384,1,2,1,512,50,512,50,'chunk_config'||E'\x1f'||'v1'||E'\x1f'||'prose#512/50'||E'\x1f'||'workflow_python#512/50','12e19cdb566b8744','12e19cdb566b87445ab2d3563e6cb948f58801f78f8395878fc9e0c2457d5462')
      on conflict (selected_contract_id) do update set selection_state='active';
      create or replace function public.hivemind_active_contract_id(p_dimension int default 384)
      returns bigint language sql stable as $$ select id from public.embedding_contracts where dimension=p_dimension and status='active' limit 1 $$;
      create table if not exists public.external_resources (
        id bigint generated by default as identity primary key, kind text not null, source text not null,
        external_id text, title text not null, body text not null, author text, url text,
        metadata jsonb not null default '{}'::jsonb, payload jsonb, created_at timestamptz not null default now()
      );
      create table if not exists public.distillations (
        id bigint generated by default as identity primary key, question text not null, conditions text,
        answer text not null, confidence text not null, status text not null default 'pending',
        author_id bigint, supersedes_id bigint, created_at timestamptz not null default now(), updated_at timestamptz not null default now()
      );
      create table if not exists public.distillation_cites (
        distillation_id bigint not null, item_kind text not null, item_id bigint not null,
        primary key(distillation_id,item_kind,item_id)
      );
      do $$ begin
        if not exists (select 1 from pg_roles where rolname='anon') then execute 'create role anon'; end if;
        if not exists (select 1 from pg_roles where rolname='authenticated') then execute 'create role authenticated'; end if;
        if not exists (select 1 from pg_roles where rolname='service_role') then execute 'create role service_role'; end if;
      end $$;
      drop view if exists public.message_feed cascade;
      create view public.message_feed as
        select message_id, content, null::text as author_name,
          null::text as channel_name, created_at, author_id, channel_id, guild_id
        from public.discord_messages where is_deleted=false;
    """)
    for migration in MIGRATIONS:
        _psql(url, "", file=str(migration))


SCENARIO = r"""
truncate table public.knowledge_idempotency, public.knowledge_references,
  public.evidence_sources, public.evidence_subjects, public.evidence,
  public.message_snapshots, public.resource_revisions, public.resources,
  public.discord_messages, public.contributors restart identity cascade;
insert into public.contributors(name,kind) values ('agent','agent'),('editor','human'),('other','human');

do $$
declare first_response jsonb; second_response jsonb; response jsonb;
begin
  begin
    set local role anon;
    perform public.hivemind_submit_resource(1,'unauth','workflow','x','x');
    raise exception 'anonymous write unexpectedly succeeded';
  exception when insufficient_privilege then null;
  end;
  set local role hannahomalley;

  first_response := public.hivemind_submit_resource(1,'initial-1','workflow','Workflow v1','stable input',
    '{"nodes":1}','{}','{"original_author":"community"}','manual','workflow-1','first','[]');
  if first_response->>'resource_id' <> '1' or first_response->>'revision_id' <> '1' then
    raise exception 'initial IDs were not serialized as strings: %', first_response;
  end if;
  if public.hivemind_resolve_reference('resource',1)->>'resolution' <> 'unreviewed_identity'
     or public.hivemind_resolve_reference('resource',1)->>'unreviewed' <> 'true' then
    raise exception 'unpinned pending resource did not remain an unreviewed identity';
  end if;
  if public.hivemind_resolve_reference('resource',1,1)->>'body' <> 'stable input'
     or public.hivemind_resolve_reference('resource',1,1)->>'state' <> 'pending' then
    raise exception 'explicit pending revision did not resolve to exact candidate content';
  end if;
  if (first_response->'diff'->>'text_diff') not like '%+stable input%' then raise exception 'exact text diff missing'; end if;
  if first_response->'diff'->'json_diff'->'candidate'->>'body' <> 'stable input' then raise exception 'exact JSON candidate missing'; end if;
  if (select current_revision_id from public.resources where id=1) is not null then
    raise exception 'pending revision replaced accepted head';
  end if;
  second_response := public.hivemind_submit_resource(1,'initial-1','workflow','Workflow v1','stable input',
    '{"nodes":1}','{}','{"original_author":"community"}','manual','workflow-1','first','[]');
  if second_response->>'idempotent_replay' <> 'true' then raise exception 'retry was not idempotent'; end if;

  perform public.hivemind_set_editor('postgres',2,true);
  response := public.hivemind_decide_revision(2,'approve-1',1,'accepted','exact review');
  if response->>'current_revision_id' <> '1' then raise exception 'approval did not publish revision'; end if;
  if not exists (select 1 from public.embedding_jobs
                 where entity_type='resource' and item_id='1'
                   and source_revision_id=1 and source_op='revision_accepted'
                   and status='pending') then
    raise exception 'accepted revision did not enqueue its exact source revision';
  end if;
  if public.hivemind_resolve_reference('resource',1)->>'resolution' <> 'accepted_head'
     or public.hivemind_resolve_reference('resource',1)->>'resolved_revision_id' <> '1' then
    raise exception 'unpinned accepted resource did not resolve to its head';
  end if;
  insert into public.discord_messages(message_id,content) values (9007199254740993,'source v1');
  perform public.hivemind_capture_message_snapshot(1,'snapshot-1',9007199254740993,'source v1','{"channel":"demo"}',9007199254740994,'Original author',now());
  begin
    perform public.hivemind_capture_message_snapshot(1,'snapshot-mismatch',9007199254740993,'not the source','{"channel":"demo"}',9007199254740994,'Original author',now());
    raise exception 'mismatched source snapshot unexpectedly succeeded';
  exception when sqlstate '22023' then null;
  end;
  perform public.hivemind_submit_evidence(1,'evidence-1','v1 completed','4090','reported result','reported',
    '[{"target_kind":"resource","target_id":"1","target_version_id":"1"},{"target_kind":"message","target_id":"9007199254740993","target_version_id":"1"},{"target_kind":"revision","target_id":"1"}]',
    '[{"external_url":"https://example.test/run/1","label":"ordinary source URL"}]');
  perform public.hivemind_propose_revision(1,'proposal-a',1,1,'workflow','Workflow v2a','candidate a','{"nodes":2}','{}','{}','a','[]');
  perform public.hivemind_propose_revision(3,'proposal-b',1,1,'workflow','Workflow v2b','candidate b','{"nodes":3}','{}','{}','b','[]');
  perform public.hivemind_set_editor('postgres',2,false);
  begin
    perform public.hivemind_decide_revision(2,'revoked-editor',2,'accepted','should fail');
    raise exception 'revoked editor unexpectedly approved';
  exception when insufficient_privilege then null;
  end;
  perform public.hivemind_set_editor('postgres',2,true);
  perform public.hivemind_decide_revision(2,'approve-a',2,'accepted','winner');
  if public.hivemind_resolve_reference('resource',1)->>'resolved_revision_id' <> '2'
     or public.hivemind_resolve_reference('resource',1)->>'body' <> 'candidate a' then
    raise exception 'unpinned resource did not resolve to the later accepted head';
  end if;
  if public.hivemind_resolve_reference('resource',1,1)->>'body' <> 'stable input'
     or public.hivemind_resolve_reference('resource',1,1)->>'state' <> 'accepted' then
    raise exception 'explicit v1 reference did not remain exact after v2 publication';
  end if;
  begin
    perform public.hivemind_decide_revision(2,'approve-b',3,'accepted','competing approval');
    raise exception 'competing approval unexpectedly succeeded';
  exception when serialization_failure then null;
  end;
  begin
    update public.resource_revisions set body='tampered' where id=2;
    raise exception 'terminal content mutation unexpectedly succeeded';
  exception when sqlstate '55000' then null;
  end;
  begin
    perform public.hivemind_decide_revision(2,'approve-a-again',2,'rejected','second decision');
    raise exception 'terminal decision unexpectedly changed';
  exception when sqlstate '55000' then null;
  end;

  begin
    perform public.hivemind_submit_resource(1,'bad-reference','note','bad','bad',null,'{}','{}',null,null,null,
      '[{"target_kind":"resource","target_id":"999"}]');
    raise exception 'invalid reference unexpectedly succeeded';
  exception when foreign_key_violation or raise_exception then null;
  end;
  if exists (select 1 from public.resources where id > 3) then raise exception 'failed submission left a resource'; end if;
end $$;

select public.hivemind_submit_resource(1,'guide-1','guide','Community guide','Evidence-backed guide',null,'{}','{"editorial":"community"}',null,null,'cites the observed run',
  '[{"target_kind":"evidence","target_id":"1","label":"observed run"}]');
select public.hivemind_decide_revision(2,'guide-approve',5,'accepted','guide review');
select public.hivemind_mark_canonical(2,'guide-canonical',3,true);
do $$ begin
  perform public.hivemind_submit_evidence(1,'bad-pin','wrong owner','x','x','reported',
    '[{"target_kind":"resource","target_id":"1","target_version_id":"5"}]','[]');
  raise exception 'mismatched resource/revision target unexpectedly succeeded';
exception when sqlstate '23514' then null; end $$;
update public.discord_messages set content='source v2' where message_id=9007199254740993;
select public.hivemind_capture_message_snapshot(1,'snapshot-2',9007199254740993,'source v2','{"channel":"demo"}',9007199254740994,'Original author',now());
select public.hivemind_submit_evidence(1,'evidence-2','contradictory reproduction','different seed','did not complete','observed',
  '[{"target_kind":"resource","target_id":"1","target_version_id":"1"},{"target_kind":"message","target_id":"9007199254740993","target_version_id":"1"},{"target_kind":"evidence","target_id":"1"}]',
  '[{"target_kind":"message","target_id":"9007199254740993","target_version_id":"2"}]','1');

do $$
begin
  if (select content from public.message_snapshots where id=1) <> 'source v1' then raise exception 'old snapshot changed'; end if;
  if (select target_version_id from public.evidence_subjects where evidence_id=1 and target_kind='message') <> 1 then raise exception 'old evidence pin changed'; end if;
  if (select target_version_id from public.evidence_subjects where evidence_id=1 and target_kind='resource') <> 1 then raise exception 'old workflow evidence pin changed'; end if;
  if (select target_version_id from public.evidence_subjects where evidence_id=1 and target_kind='revision') is not null then raise exception 'exact revision subject was not unversioned'; end if;
  if (select target_version_id from public.evidence_subjects where evidence_id=2 and target_kind='evidence') is not null then raise exception 'exact evidence subject was not unversioned'; end if;
  if (select submitted_by from public.evidence where id=1) <> 1
     or (select captured_by from public.message_snapshots where id=1) <> 1
     or (select original_author_id from public.message_snapshots where id=1) <> 9007199254740994 then
    raise exception 'reporter and original-author attribution missing';
  end if;
  if (select basis from public.evidence where id=2) <> 'observed' or (select supersedes_evidence_id from public.evidence where id=2) <> 1 then raise exception 'contradictory evidence attribution missing'; end if;
  if (select count(*) from public.knowledge_references where source_kind='evidence') <> 7 then raise exception 'derived evidence links missing'; end if;
  if not (select canonical_guide from public.resources where id=3) then raise exception 'canonical guide marker missing'; end if;
  if not exists (select 1 from public.knowledge_references where source_kind='revision' and source_id=5 and target_kind='evidence' and target_id=1) then raise exception 'guide to evidence link missing'; end if;
  if not exists (select 1 from public.knowledge_outgoing_references
                 where source_kind='revision' and source_id='5' and target_kind='evidence'
                   and target_resolution->>'resolution'='exact_evidence') then
    raise exception 'outgoing projection did not resolve its target';
  end if;
  if not exists (select 1 from public.knowledge_backlinks
                 where linked_kind='evidence' and linked_id='1'
                   and backlink_source_kind='revision' and backlink_source_id='5') then
    raise exception 'backlink projection did not expose inbound source semantics';
  end if;
end $$;

do $$
declare repeat_response jsonb;
begin
  repeat_response := public.hivemind_submit_resource(1,'repeat-import-1','workflow','Workflow reimport','changed source body',
    '{"nodes":9}','{}','{"import":"repeat"}','manual','workflow-1','repeat import','[]');
  if repeat_response->>'resource_id' <> '1'
     or repeat_response->>'repeat_import' <> 'true'
     or (select count(*) from public.resources where origin_source='manual' and origin_external_id='workflow-1') <> 1 then
    raise exception 'repeat import did not create a pending revision on the stable resource';
  end if;
end $$;
"""

FRESHNESS_SCENARIO = r"""
insert into public.contributors(name,kind) values ('fixture-agent','agent'),('fixture-editor','human') on conflict (name) do nothing;
update public.contributors set is_editor=true where id=(select min(id) from public.contributors where kind='human');
truncate table public.embedding_jobs, public.content_embeddings, public.lexical_documents,
  public.content_representation_manifest,
  public.knowledge_references, public.resource_revisions, public.resources restart identity cascade;
insert into public.resources(id,created_by,origin_source,origin_external_id) overriding system value values (10,1,'fixture','freshness');
insert into public.resource_revisions(id,resource_id,kind,title,body,submitted_by,state,decided_by,decided_at) overriding system value
  values (100,10,'article','Head','headtoken body',1,'accepted',2,now()),(101,10,'article','Historical','headtoken body',1,'accepted',2,now()),(102,10,'article','Pending','headtoken body',1,'pending',null,null);
update public.resources set current_revision_id=100 where id=10;
delete from public.embedding_jobs;
update public.embedding_contracts set status='inactive' where dimension=384;
insert into public.embedding_contracts(id,provider,model,dimension,canonicalization_version,chunking_version,status)
  values(1360541028304258884,'fixture','fixture-384',384,1,2,'active')
  on conflict (id) do update set status='active',dimension=384;
insert into public.content_embeddings(contract_id,entity_type,item_id,representation_type,chunk_index,chunk_text,embedding,representation_hash,chunk_hash,source_revision_id)
  values(1360541028304258884,'resource','10','prose',0,'headtoken body',array_fill(0::real,ARRAY[384])::vector,repeat('a',64),repeat('b',64),101);
do $$ declare n int; begin
  select count(*) into n from public.hivemind_lexical_candidates('headtoken',100,'{resource}');
  if n <> 1 then raise exception 'freshness candidate count expected 1, got %',n; end if;
  if not exists(select 1 from public.hivemind_lexical_candidates('headtoken',100,'{resource}') where item_id='10') then raise exception 'current head did not rank'; end if;
  if exists(select 1 from public.hivemind_lexical_candidates('headtoken',100,'{resource}') where item_id in ('101','102')) then raise exception 'historical or pending revision ranked'; end if;
  if (select count(*) from public.hivemind_semantic_candidates(array_fill(0::real,ARRAY[384])::vector,100,'{resource}','{}')) <> 0 then
    raise exception 'stale semantic revision ranked';
  end if;
  update public.content_embeddings set source_revision_id=100 where contract_id=1360541028304258884 and item_id='10';
  if (select count(*) from public.hivemind_semantic_candidates(array_fill(0::real,ARRAY[384])::vector,100,'{resource}','{}')) <> 1 then
    raise exception 'current semantic revision did not rank';
  end if;
  insert into public.content_representation_manifest(
    entity_type,item_id,representation_type,representation_hash,source_available,
    public_state,chunking_version,chunk_config_identity,chunks,chunk_count,chunk_method)
  values('resource','10','prose',
    public.hivemind_representation_hash('Head' || E'\n\n' || 'headtoken body'),true,null,2,
    'chunk_config'||E'\x1f'||'v1'||E'\x1f'||'prose#512/50'||E'\x1f'||'workflow_python#512/50',
    jsonb_build_array(jsonb_build_object(
      'chunk_index',0,'chunk_text','Head' || E'\n\n' || 'headtoken body',
      'chunk_hash',public.hivemind_representation_hash('Head' || E'\n\n' || 'headtoken body'),
      'embedding',(array_fill(0::real,ARRAY[384])::vector)::text)),1,'fixture');
  insert into public.embedding_jobs(entity_type,item_id,representation_type,job_kind,contract_id,source_revision_id,status,locked_by)
    values('resource','10','prose','reembed',1360541028304258884,100,'processing','good-worker');
  perform * from public.hivemind_finalize_embedding_job(
    (select max(id) from public.embedding_jobs),'good-worker',
    jsonb_build_array(jsonb_build_object(
      'entity_type','resource','item_id','10','representation_type','prose',
      'chunk_index',0,'chunk_text','Head' || E'\n\n' || 'headtoken body',
      'chunk_hash',public.hivemind_representation_hash('Head' || E'\n\n' || 'headtoken body'),
      'representation_hash',public.hivemind_representation_hash('Head' || E'\n\n' || 'headtoken body'),
      'contract_id','1360541028304258884',
      'embedding',(array_fill(0::real,ARRAY[384])::vector)::text)),
    public.hivemind_representation_hash('Head' || E'\n\n' || 'headtoken body'),null,true);
  if (select status from public.embedding_jobs order by id desc limit 1) <> 'done'
     or (select source_revision_id from public.content_embeddings where contract_id=1360541028304258884 and item_id='10' and representation_type='prose' limit 1) <> 100 then
    raise exception 'current-head finalizer did not publish the pinned revision';
  end if;
  insert into public.resource_revisions(resource_id,kind,title,body,submitted_by,state,decided_by,decided_at) values(10,'article','Head v2','headtoken body',1,'accepted',2,now()) returning id into n;
  update public.resources set current_revision_id=n where id=10;
  if (select count(*) from public.hivemind_lexical_candidates('headtoken',100,'{resource}') where item_id='10') <> 1 then raise exception 'same-content revision lost stable resource identity'; end if;
  if (select count(*) from public.hivemind_semantic_candidates(array_fill(0::real,ARRAY[384])::vector,100,'{resource}','{}')) <> 0 then
    raise exception 'semantic vector from historical head ranked after head change';
  end if;
  insert into public.embedding_jobs(entity_type,item_id,representation_type,job_kind,contract_id,source_revision_id,status,locked_by)
    values('resource','10','prose','reembed',1360541028304258884,100,'processing','late-worker');
  perform * from public.hivemind_finalize_embedding_job((select max(id) from public.embedding_jobs),'late-worker','[]','',null,false);
  if (select status from public.embedding_jobs order by id desc limit 1) <> 'cancelled'
     or (select last_error from public.embedding_jobs order by id desc limit 1) <> 'source_changed_replaced'
     or (select count(*) from public.embedding_jobs
         where entity_type='resource' and item_id='10' and representation_type='prose'
           and status='pending' and source_revision_id=n) <> 1 then
    raise exception 'late finalizer did not preserve the replacement pending job';
  end if;
end $$;
"""

CONVERSION_SEED = r"""
insert into public.contributors(name,kind) values ('fixture-agent','agent') on conflict (name) do nothing;
truncate table public.embedding_jobs, public.content_embeddings, public.lexical_documents,
  public.knowledge_references, public.resource_revisions, public.resources,
  public.external_resources, public.distillations, public.distillation_cites restart identity cascade;
insert into public.external_resources(id,kind,source,external_id,title,body,author,url,metadata,payload) overriding system value
  values(1,'article','fixture',null,'Legacy nullable-origin','nullable body','Author','https://example.test/null','{"tag":"null"}',null),
        (900,'workflow','fixture','wf-900','Legacy workflow','workflow body','Author','https://example.test/wf','{"tag":"x"}','{"nodes":1}');
insert into public.distillations(id,question,conditions,answer,confidence,status,author_id) overriding system value
  values(901,'Legacy question','only on fixture','Legacy answer','high','approved',1);
insert into public.distillation_cites values(901,'resource',900);
"""


def run(scenario: str) -> dict[str, object]:
    raw_url = os.environ.get("HIVEMIND_TEST_DATABASE_URL")
    if not raw_url:
        raise ValueError("set HIVEMIND_TEST_DATABASE_URL to the disposable PostgreSQL URL")
    url = _checked_url(raw_url)
    _bootstrap(url)
    if scenario not in {"all", "revisions", "references", "evidence", "search_freshness", "conversion"}:
        raise ValueError("scenario must be all, revisions, references, evidence, search_freshness, or conversion")
    if scenario in {"all", "revisions", "references", "evidence"}:
        _psql(url, SCENARIO)
    if scenario in {"all", "search_freshness"}:
        _psql(url, FRESHNESS_SCENARIO)
    if scenario in {"all", "conversion"}:
        _psql(url, CONVERSION_SEED)
        env = dict(os.environ); env["HIVEMIND_TEST_DATABASE_URL"] = url
        with tempfile.TemporaryDirectory(prefix="hivemind-conversion-export-") as export_dir:
            export_path = Path(export_dir) / "legacy.json"
            exported = subprocess.run(
                ["python3", str(ROOT / "scripts" / "convert_legacy_knowledge.py"), "--export", str(export_path)],
                cwd=ROOT, env=env, text=True, capture_output=True,
            )
            if exported.returncode:
                raise RuntimeError(exported.stderr or exported.stdout)
            export_payload = json.loads(export_path.read_text(encoding="utf-8"))
            if len(export_payload.get("external_resources", [])) != 2 or len(export_payload.get("distillations", [])) != 1 or len(export_payload.get("cites", [])) != 1:
                raise RuntimeError("conversion export was not recoverable row data")
        rehearsal = subprocess.run(["python3", str(ROOT / "scripts" / "convert_legacy_knowledge.py"), "--rehearse"], cwd=ROOT, env=env, text=True, capture_output=True)
        if rehearsal.returncode:
            raise RuntimeError(rehearsal.stderr or rehearsal.stdout)
        if _psql(url, "select count(*) from public.resources") != "0":
            raise RuntimeError("conversion rehearsal did not roll back")
        applied = subprocess.run(["python3", str(ROOT / "scripts" / "convert_legacy_knowledge.py"), "--apply"], cwd=ROOT, env=env, text=True, capture_output=True)
        if applied.returncode:
            raise RuntimeError(applied.stderr or applied.stdout)
        check = _psql(url, "select count(*) from public.resources r join public.resource_revisions v on v.id=r.current_revision_id where r.origin_source='legacy-distillation' and v.state='accepted'; select count(*) from public.knowledge_references where label='converted legacy cite'; select (to_regclass('public.external_resources') is null)::int; select (to_regclass('public.distillations') is null)::int; select (to_regclass('public.distillation_cites') is null)::int;")
        if check.splitlines() != ["1", "1", "1", "1", "1"]:
            raise RuntimeError("conversion assertions failed: " + check)
        nullable = _psql(url, "select count(*) from public.resources where origin_source is null and origin_external_id is null and id=1; select count(*) from public.resource_revisions where provenance->>'legacy_id'='1' and provenance->>'external_id' is null;")
        if nullable.splitlines() != ["1", "1"]:
            raise RuntimeError("nullable legacy origin was not preserved: " + nullable)
    return {"status": "passed", "scenario": scenario, "migrations": [str(x) for x in MIGRATIONS]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url-env", default="HIVEMIND_TEST_DATABASE_URL")
    parser.add_argument("--scenario", default="all")
    args = parser.parse_args(argv)
    if args.database_url_env != "HIVEMIND_TEST_DATABASE_URL":
        os.environ["HIVEMIND_TEST_DATABASE_URL"] = os.environ.get(args.database_url_env, "")
    try:
        print(json.dumps(run(args.scenario), sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
