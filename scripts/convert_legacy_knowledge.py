#!/usr/bin/env python3
"""Direct, one-boundary conversion rehearsal for the legacy corpus.

Default is an export/report only. ``--rehearse`` runs the complete conversion
inside one disposable transaction and rolls it back. ``--apply`` commits the
same transaction; the URL guard only accepts a local/test/otto database.
There is no dual write or compatibility endpoint in this script.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]

def checked_url(raw: str) -> str:
    p = urlparse(raw)
    host = p.hostname or ""
    socket_host = parse_qs(p.query).get("host", [""])[0]
    db = (p.path or "").lstrip("/")
    if p.scheme not in {"postgres", "postgresql"} or (host not in {"", "localhost", "127.0.0.1", "::1"} and not socket_host.startswith("/tmp/")):
        raise ValueError("refusing non-disposable database host")
    if "test" not in db.lower() and "otto" not in raw.lower():
        raise ValueError("refusing database without a test/otto identity")
    return raw

def legacy_state(status: str) -> str:
    return {"approved": "accepted", "pending": "pending", "rejected": "rejected", "superseded": "withdrawn"}.get(status, "rejected")

def inert_legacy_metadata(confidence: str | None, conditions: str | None = None) -> dict[str, object]:
    out: dict[str, object] = {"legacy_conversion": True}
    if confidence is not None: out["legacy_confidence"] = confidence
    if conditions is not None: out["legacy_conditions"] = conditions
    return out

def conversion_sql(submitter_id: int, *, commit: bool) -> str:
    finish = "commit;" if commit else "rollback;"
    return f"""
begin;
create temp table legacy_resource_map(kind text, legacy_id bigint, new_id bigint, primary key(kind, legacy_id)) on commit drop;
create temp table legacy_revision_map(kind text, legacy_id bigint, new_id bigint, resource_id bigint, primary key(kind, legacy_id)) on commit drop;
do $$ declare e record; rid bigint; vid bigint; rev_state text; begin
  for e in select * from public.external_resources order by id loop
    select r.id into rid from public.resources r where r.origin_source=e.source and r.origin_external_id is not distinct from e.external_id;
    if rid is null then
      if not exists (select 1 from public.resources where id=e.id) then
        insert into public.resources(id,created_by,origin_source,origin_external_id,created_at)
          overriding system value values(e.id,{submitter_id},e.source,e.external_id,e.created_at) returning id into rid;
      else
        insert into public.resources(created_by,origin_source,origin_external_id,created_at)
          values({submitter_id},e.source,e.external_id,e.created_at) returning id into rid;
      end if;
    end if;
    insert into legacy_resource_map values('resource',e.id,rid) on conflict do nothing;
    insert into public.resource_revisions(resource_id,kind,title,body,payload,metadata,provenance,submitted_by,state,decided_by,decided_at,decision_reason)
      values(rid,e.kind,e.title,e.body,e.payload,coalesce(e.metadata,'{{}}'::jsonb),
        jsonb_strip_nulls(jsonb_build_object('legacy_table','external_resources','source',e.source,'external_id',e.external_id,'author',e.author,'url',e.url)),
        {submitter_id},'accepted',{submitter_id},e.created_at,'converted from legacy visible resource') returning id into vid;
    insert into legacy_revision_map values('resource',e.id,vid,rid) on conflict do nothing;
    update public.resources set current_revision_id=vid where id=rid and current_revision_id is null;
  end loop;
  for e in select * from public.distillations order by id loop
    insert into public.resources(created_by,origin_source,origin_external_id,created_at)
      values({submitter_id},'legacy-distillation','distillation:'||e.id,e.created_at) returning id into rid;
    insert into legacy_resource_map values('distillation',e.id,rid);
    rev_state := case e.status when 'approved' then 'accepted' when 'pending' then 'pending' when 'superseded' then 'withdrawn' else 'rejected' end;
    insert into public.resource_revisions(resource_id,kind,title,body,metadata,provenance,submitted_by,state,decided_by,decided_at,decision_reason)
      values(rid,'knowledge',e.question,e.answer,
        jsonb_build_object('legacy_conversion',true,'legacy_confidence',e.confidence,'legacy_conditions',e.conditions),
        jsonb_strip_nulls(jsonb_build_object('legacy_table','distillations','legacy_id',e.id,'legacy_author_id',e.author_id,'legacy_supersedes_id',e.supersedes_id)),
        {submitter_id},rev_state,case when rev_state='pending' then null else {submitter_id} end,
        case when rev_state='pending' then null else e.updated_at end,'converted from legacy distillation') returning id into vid;
    insert into legacy_revision_map values('distillation',e.id,vid,rid);
    update public.resources set current_revision_id=vid where id=rid and rev_state='accepted';
  end loop;
  -- Preserve each cite as a typed exact reference. No target is invented when
  -- a legacy row is absent; the reference remains a source-custody pointer.
  insert into public.knowledge_references(source_kind,source_id,target_kind,target_id,target_version_id,label,created_by)
  select 'revision',rm.new_id,
    case c.item_kind when 'resource' then 'resource' when 'message' then 'message' else 'resource' end,
    case when c.item_kind='resource' then coalesce(rm2.new_id,c.item_id) when c.item_kind='distillation' then coalesce(rm3.new_id,c.item_id) else c.item_id end,
    case when c.item_kind='resource' then rr2.new_id when c.item_kind='distillation' then rr3.new_id else null end,
    'converted legacy cite', {submitter_id}
  from public.distillation_cites c
  join legacy_revision_map rm on rm.kind='distillation' and rm.legacy_id=c.distillation_id
  left join legacy_resource_map rm2 on rm2.kind='resource' and rm2.legacy_id=c.item_id and c.item_kind='resource'
  left join legacy_resource_map rm3 on rm3.kind='distillation' and rm3.legacy_id=c.item_id and c.item_kind='distillation'
  left join legacy_revision_map rr2 on rr2.kind='resource' and rr2.legacy_id=c.item_id and c.item_kind='resource'
  left join legacy_revision_map rr3 on rr3.kind='distillation' and rr3.legacy_id=c.item_id and c.item_kind='distillation'
  on conflict do nothing;
end $$;
select jsonb_build_object('resources', (select count(*) from legacy_resource_map where kind='resource'), 'knowledge_resources', (select count(*) from legacy_resource_map where kind='distillation'), 'revisions', (select count(*) from legacy_revision_map), 'references', (select count(*) from public.knowledge_references where label='converted legacy cite'));
{finish}
"""

def psql(url: str, sql: str) -> str:
    r = subprocess.run(["psql", "-X", url, "-v", "ON_ERROR_STOP=1", "-q", "-t", "-A", "-c", sql], cwd=ROOT, text=True, capture_output=True)
    if r.returncode: raise RuntimeError((r.stderr or r.stdout).strip())
    return r.stdout.strip()

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--database-url-env", default="HIVEMIND_TEST_DATABASE_URL")
    ap.add_argument("--submitter-id", type=int, default=1)
    ap.add_argument("--rehearse", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--export", type=Path)
    args = ap.parse_args(argv)
    if args.rehearse and args.apply: ap.error("choose --rehearse or --apply")
    raw = os.environ.get(args.database_url_env)
    if not raw: print(json.dumps({"status":"skipped","reason":"database_url_missing"})); return 0
    try: url = checked_url(raw)
    except ValueError as exc: print(json.dumps({"status":"failed","error":str(exc)})); return 1
    if args.export:
        payload = psql(url, "select jsonb_build_object('external_resources',(select count(*) from public.external_resources),'distillations',(select count(*) from public.distillations),'cites',(select count(*) from public.distillation_cites));")
        args.export.write_text(payload + "\n", encoding="utf-8")
    if not args.rehearse and not args.apply:
        print(json.dumps({"status":"exported" if args.export else "dry_run","boundary":"single transaction","commit":False}, sort_keys=True)); return 0
    try:
        result = psql(url, conversion_sql(args.submitter_id, commit=args.apply))
        print(json.dumps({"status":"applied" if args.apply else "rehearsed","boundary":"single transaction","commit":args.apply,"result":result}, sort_keys=True)); return 0
    except (OSError, RuntimeError) as exc:
        print(json.dumps({"status":"failed","error":str(exc)}, sort_keys=True)); return 1

if __name__ == "__main__": raise SystemExit(main())
