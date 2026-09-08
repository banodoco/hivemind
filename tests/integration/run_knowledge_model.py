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
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "schema" / "038_knowledge_model.sql"


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
        is_deleted boolean not null default false
      );
      do $$ begin
        if not exists (select 1 from pg_roles where rolname='anon') then execute 'create role anon'; end if;
        if not exists (select 1 from pg_roles where rolname='authenticated') then execute 'create role authenticated'; end if;
        if not exists (select 1 from pg_roles where rolname='service_role') then execute 'create role service_role'; end if;
      end $$;
      do $$ begin
        if to_regclass('public.message_feed') is null then
          execute 'create view public.message_feed as select message_id, content from public.discord_messages where is_deleted=false';
        end if;
      end $$;
    """)
    _psql(url, "", file=str(MIGRATION))


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
  if public.hivemind_resolve_reference('resource',1)->>'resolution' <> 'accepted_head'
     or public.hivemind_resolve_reference('resource',1)->>'resolved_revision_id' <> '1' then
    raise exception 'unpinned accepted resource did not resolve to its head';
  end if;
  insert into public.discord_messages(message_id,content) values (9007199254740993,'source v1');
  perform public.hivemind_capture_message_snapshot(1,'snapshot-1',9007199254740993,'source v1','{"channel":"demo"}',9007199254740994,'Original author',now());
  perform public.hivemind_submit_evidence(1,'evidence-1','v1 completed','4090','reported result','reported',
    '[{"target_kind":"resource","target_id":"1","target_version_id":"1"},{"target_kind":"message","target_id":"9007199254740993","target_version_id":"1"}]',
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
  '[{"target_kind":"resource","target_id":"1","target_version_id":"1"},{"target_kind":"message","target_id":"9007199254740993","target_version_id":"1"}]',
  '[{"target_kind":"message","target_id":"9007199254740993","target_version_id":"2"}]','1');

do $$
begin
  if (select content from public.message_snapshots where id=1) <> 'source v1' then raise exception 'old snapshot changed'; end if;
  if (select target_version_id from public.evidence_subjects where evidence_id=1 and target_kind='message') <> 1 then raise exception 'old evidence pin changed'; end if;
  if (select target_version_id from public.evidence_subjects where evidence_id=1 and target_kind='resource') <> 1 then raise exception 'old workflow evidence pin changed'; end if;
  if (select submitted_by from public.evidence where id=1) <> 1
     or (select captured_by from public.message_snapshots where id=1) <> 1
     or (select original_author_id from public.message_snapshots where id=1) <> 9007199254740994 then
    raise exception 'reporter and original-author attribution missing';
  end if;
  if (select basis from public.evidence where id=2) <> 'observed' or (select supersedes_evidence_id from public.evidence where id=2) <> 1 then raise exception 'contradictory evidence attribution missing'; end if;
  if (select count(*) from public.knowledge_references where source_kind='evidence') <> 5 then raise exception 'derived evidence links missing'; end if;
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
"""


def run(scenario: str) -> dict[str, object]:
    raw_url = os.environ.get("HIVEMIND_TEST_DATABASE_URL")
    if not raw_url:
        raise ValueError("set HIVEMIND_TEST_DATABASE_URL to the disposable PostgreSQL URL")
    url = _checked_url(raw_url)
    _bootstrap(url)
    if scenario not in {"all", "revisions", "evidence"}:
        raise ValueError("scenario must be all, revisions, or evidence")
    _psql(url, SCENARIO)
    return {"status": "passed", "scenario": scenario, "migration": str(MIGRATION)}


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
