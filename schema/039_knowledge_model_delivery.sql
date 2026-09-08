-- Hivemind delivery cutover (T7/T8).
-- The 038 resources/revisions contract is now the only active resource
-- surface.  Earlier migrations remain historical; this migration replaces
-- their active candidate/RPC/queue definitions without rewriting history.

create extension if not exists pg_trgm;

-- Exact revision/evidence subjects use a nullable version pin.  The foundation
-- table originally included that nullable column in its primary key, which
-- makes PostgreSQL enforce NOT NULL and rejects valid unversioned revision or
-- evidence subjects.  Normalize the key in-place for already-created local
-- databases as well as fresh applies.
alter table if exists public.evidence_subjects
  drop constraint if exists evidence_subjects_pkey;
alter table if exists public.evidence_subjects
  alter column target_version_id drop not null;
alter table if exists public.evidence_subjects
  add constraint evidence_subjects_pkey primary key (evidence_id, target_kind, target_id);

-- The delivery queue has one additional source operation: publication of an
-- exact accepted revision.  Replace the historical constraint rather than
-- weakening it or adding a compatibility branch.
alter table if exists public.embedding_jobs
  drop constraint if exists embedding_jobs_source_op_check;
alter table if exists public.embedding_jobs
  drop constraint if exists embedding_jobs_source_op_delivery_check;
alter table if exists public.embedding_jobs
  add constraint embedding_jobs_source_op_delivery_check check (
    source_op in ('insert','update','delete','soft_delete','status_change',
                  'python_change','semantic_change','opt_out','revision_accepted')
  );

-- Direct cutover: legacy presentation/RPC/write branches are no longer active.
drop view if exists public.unified_feed;
drop function if exists public.check_duplicate_distillation(text, bigint);

-- Repeat imports keep the single contribution surface: a stable source identity
-- is an existing resource, while a content-sensitive retry token (built by the
-- ingestors) makes changed input a pending revision rather than a duplicate
-- resource or an idempotency conflict.
create or replace function public.hivemind_submit_resource(
  p_submitter bigint, p_idempotency_token text, p_kind text, p_title text,
  p_body text, p_payload jsonb default null, p_metadata jsonb default '{}'::jsonb,
  p_provenance jsonb default '{}'::jsonb, p_origin_source text default null,
  p_origin_external_id text default null, p_rationale text default null,
  p_references jsonb default '[]'::jsonb
) returns jsonb
language plpgsql security definer set search_path = public, pg_temp as $$
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
  if exists (select 1 from public.knowledge_idempotency where contributor_id=p_submitter
             and action='submit_resource' and token=p_idempotency_token) then
    return public.hivemind_store_idempotency(p_submitter,'submit_resource',p_idempotency_token,
      public.hivemind_request_hash(input), '{}'::jsonb);
  end if;

  if p_origin_source is not null and p_origin_external_id is not null then
    select * into resource_row
      from public.resources
     where origin_source=p_origin_source and origin_external_id=p_origin_external_id
     for update;
    if found then
      if resource_row.current_revision_id is null then
        raise exception 'resource % has no accepted head; retry the original submission or review it first', resource_row.id
          using errcode = '40001';
      end if;
      insert into public.resource_revisions
        (resource_id,base_revision_id,kind,title,body,payload,metadata,provenance,
         reference_spec,submitted_by,rationale)
      values
        (resource_row.id,resource_row.current_revision_id,p_kind,p_title,p_body,p_payload,
         coalesce(p_metadata,'{}'),coalesce(p_provenance,'{}'),coalesce(p_references,'[]'),
         p_submitter,p_rationale)
      returning * into revision_row;
      perform public.hivemind_reference_array('revision', revision_row.id, p_submitter,
                                              coalesce(p_references,'[]'));
      response := jsonb_build_object('status','pending','resource_id',resource_row.id::text,
        'revision_id',revision_row.id::text,'base_revision_id',resource_row.current_revision_id::text,
        'state',revision_row.state,'repeat_import',true,
        'diff',public.hivemind_revision_diff(revision_row.id));
      return public.hivemind_store_idempotency(p_submitter,'submit_resource',p_idempotency_token,
        public.hivemind_request_hash(input),response);
    end if;
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
end;
$$;

alter table public.resource_revisions
  add column if not exists lexical_tsv tsvector generated always as (
    setweight(to_tsvector('simple'::regconfig, coalesce(title, '')), 'A')
    || setweight(to_tsvector('simple'::regconfig, coalesce(metadata::text, '')), 'B')
    || setweight(to_tsvector('simple'::regconfig, coalesce(body, '')), 'C')
  ) stored;
create index if not exists resource_revisions_accepted_lexical_idx
  on public.resource_revisions using gin (lexical_tsv) where state = 'accepted';
create index if not exists resource_revisions_head_idx
  on public.resource_revisions (resource_id, id) where state = 'accepted';

alter table public.lexical_documents
  add column if not exists source_revision_id bigint references public.resource_revisions(id);
create index if not exists lexical_documents_current_revision_idx
  on public.lexical_documents (entity_type, item_id, source_revision_id, representation_type, chunk_index);

create table if not exists public.knowledge_resource_python_state (
  resource_id bigint primary key references public.resources(id) on delete cascade,
  revision_id bigint not null references public.resource_revisions(id),
  public_state text not null check (public_state in ('safe','quarantined')),
  representation_hash text,
  chunk_count integer not null default 0,
  updated_at timestamptz not null default now()
);

alter table public.content_embeddings
  add column if not exists source_revision_id bigint references public.resource_revisions(id);
create index if not exists content_embeddings_current_revision_idx
  on public.content_embeddings (entity_type, item_id, source_revision_id, representation_type, chunk_index);

alter table public.embedding_jobs
  add column if not exists source_revision_id bigint references public.resource_revisions(id);

create or replace function public.hivemind_workflow_python_state(p_resource_id bigint)
returns text language sql stable set search_path = public, pg_temp as $$
  select public_state from public.knowledge_resource_python_state where resource_id = $1
$$;

-- Current-head-only lexical candidate stream. This keeps the existing lexical
-- arm set (message FTS + identifier, resource FTS + phrase + title identifier,
-- workflow-Python FTS + fragment), adapting only the resource source to the
-- accepted current revision. Distillations are retired without changing rank,
-- filters, message behavior, or the raw-table search strategy.
create or replace function public.hivemind_lexical_candidates(
  p_query text, p_candidate_limit int default 100, p_kinds text[] default '{}',
  p_sources text[] default '{}', p_item_ids text[] default '{}',
  p_since timestamptz default null, p_channels text[] default '{}',
  p_authors text[] default '{}', p_author_optout boolean default false,
  p_bots_excluded boolean default false
) returns table(entity_type text, item_id text, representation_type text,
  matched_snippet text, lexical_rank real, lexical_source text, created_at timestamptz)
language plpgsql stable security definer set search_path = public, pg_temp as $$
declare
  q tsquery := websearch_to_tsquery('simple'::regconfig, coalesce(p_query, ''));
  phrase tsquery;
  qn text := public.hivemind_normalize_identifier(coalesce(p_query, ''));
  single_query boolean := coalesce(btrim(p_query), '') <> ''
    and position(' ' in btrim(p_query)) = 0
    and p_query not like '%"%' and p_query not like '%-%';
  kinds text[] := coalesce(p_kinds, '{}'::text[]);
  has_kinds boolean := coalesce(array_length(kinds, 1), 0) > 0;
  want_message boolean := not has_kinds or 'message' = any(kinds);
  want_resource boolean := not has_kinds or exists(select 1 from unnest(kinds) k where k <> 'message');
  concrete_kinds text[];
  has_items boolean := coalesce(array_length(coalesce(p_item_ids, '{}'::text[]), 1), 0) > 0;
  item_entity text;
begin
  if single_query or p_query like '"%"' then
    phrase := phraseto_tsquery('simple'::regconfig, coalesce(p_query, ''));
  end if;
  select array_agg(k) into concrete_kinds
    from unnest(kinds) k where k not in ('message','resource');
  if has_items then
    if want_message and not want_resource then item_entity := 'message';
    elsif want_resource and not want_message then item_entity := 'resource';
    else return;
    end if;
  end if;
  return query
  with arms(entity_type,item_id,representation_type,matched_snippet,lexical_rank,lexical_source,created_at) as (
    select 'message'::text, m.message_id::text, 'prose'::text, null::text,
           ts_rank(to_tsvector('simple'::regconfig, coalesce(m.content,'')), q, 32),
           'message_fts'::text, m.created_at
      from public.message_feed m
      left join public.discord_messages dm on dm.message_id=m.message_id
     where want_message and q <> to_tsquery('simple'::regconfig, '')
       and to_tsvector('simple'::regconfig, coalesce(m.content,'')) @@ q
       and (coalesce(array_length(p_sources,1),0)=0 or 'banodoco-discord'=any(p_sources))
       and (p_since is null or m.created_at >= p_since)
       and (coalesce(array_length(p_channels,1),0)=0 or m.channel_name=any(p_channels))
       and (coalesce(array_length(p_authors,1),0)=0 or m.author_name=any(p_authors))
       and (not p_author_optout or exists (
              select 1 from public.members mb
               where mb.member_id=dm.author_id and coalesce(mb.allow_content_sharing,true)))
       and (not p_bots_excluded or not exists (
              select 1 from public.members mb
               where mb.member_id=dm.author_id and (coalesce(mb.bot,false) or coalesce(mb.system,false))))
       and (not has_items or (item_entity='message' and m.message_id::text=any(p_item_ids)))
    union all
    select 'message'::text, m.message_id::text, 'prose'::text, null::text,
           case when public.hivemind_normalize_identifier(m.content)=qn then 1.0::real else 0.9::real end,
           'message_ident'::text, m.created_at
      from public.message_feed m
      left join public.discord_messages dm on dm.message_id=m.message_id
     where want_message and qn <> ''
       and char_length(m.content) between 1 and 8000
       and public.hivemind_normalize_identifier(m.content) like '%'||qn||'%'
       and (p_since is null or m.created_at >= p_since)
       and (coalesce(array_length(p_channels,1),0)=0 or m.channel_name=any(p_channels))
       and (coalesce(array_length(p_authors,1),0)=0 or m.author_name=any(p_authors))
       and (not p_author_optout or exists (
              select 1 from public.members mb
               where mb.member_id=dm.author_id and coalesce(mb.allow_content_sharing,true)))
       and (not p_bots_excluded or not exists (
              select 1 from public.members mb
               where mb.member_id=dm.author_id and (coalesce(mb.bot,false) or coalesce(mb.system,false))))
       and (coalesce(array_length(p_sources,1),0)=0 or 'banodoco-discord'=any(p_sources))
       and (not has_items or (item_entity='message' and m.message_id::text=any(p_item_ids)))
    union all
    select 'resource'::text, r.id::text, 'prose'::text, null::text,
           ts_rank(rr.lexical_tsv, q, 32), 'resource_revision_fts'::text, r.created_at
      from public.resources r
      join public.resource_revisions rr on rr.id=r.current_revision_id and rr.state='accepted'
     where want_resource and q <> to_tsquery('simple'::regconfig, '') and rr.lexical_tsv @@ q
       and (coalesce(array_length(p_sources,1),0)=0 or r.origin_source=any(p_sources))
       and (p_since is null or r.created_at >= p_since)
       and (not has_items or (item_entity='resource' and r.id::text=any(p_item_ids)))
       and (not exists(select 1 from unnest(kinds) k where k <> 'message' and k <> 'resource')
            or rr.kind=any(concrete_kinds))
    union all
    select 'resource'::text, r.id::text, 'prose'::text, null::text,
           ts_rank(rr.lexical_tsv, phrase, 32), 'resource_revision_phrase'::text, r.created_at
      from public.resources r
      join public.resource_revisions rr on rr.id=r.current_revision_id and rr.state='accepted'
     where want_resource and phrase is not null and rr.lexical_tsv @@ phrase
       and (coalesce(array_length(p_sources,1),0)=0 or r.origin_source=any(p_sources))
       and (p_since is null or r.created_at >= p_since)
       and (not has_items or (item_entity='resource' and r.id::text=any(p_item_ids)))
       and (not exists(select 1 from unnest(kinds) k where k <> 'message' and k <> 'resource')
            or rr.kind=any(concrete_kinds))
    union all
    select 'resource'::text, r.id::text, 'prose'::text, null::text,
           0.95::real, 'resource_title_ident'::text, r.created_at
      from public.resources r
      join public.resource_revisions rr on rr.id=r.current_revision_id and rr.state='accepted'
     where want_resource and qn <> ''
       and char_length(public.hivemind_normalize_identifier(rr.title)) between 1 and 300
       and public.hivemind_normalize_identifier(rr.title) like '%'||qn||'%'
       and (coalesce(array_length(p_sources,1),0)=0 or r.origin_source=any(p_sources))
       and (p_since is null or r.created_at >= p_since)
       and (not has_items or (item_entity='resource' and r.id::text=any(p_item_ids)))
       and (not exists(select 1 from unnest(kinds) k where k <> 'message' and k <> 'resource')
            or rr.kind=any(concrete_kinds))
    union all
    select 'resource'::text, ld.item_id, 'workflow_python'::text,
           left(ld.matched_anchor,512), ts_rank(ld.tsv,q,32),
           'workflow_python_fts'::text, r.created_at
      from public.lexical_documents ld
      join public.resources r on r.id::text=ld.item_id and r.current_revision_id=ld.source_revision_id
      join public.resource_revisions rr on rr.id=r.current_revision_id
      left join public.knowledge_resource_python_state ps
        on ps.resource_id=r.id and ps.revision_id=rr.id
     where want_resource and ld.entity_type='resource'
       and ld.representation_type='workflow_python' and q <> to_tsquery('simple'::regconfig,'')
       and ld.tsv @@ q and char_length(ld.chunk_text) between 1 and 8000
       and rr.state='accepted' and rr.kind='workflow'
       and coalesce(ps.public_state,'quarantined')='safe'
       and (coalesce(array_length(p_sources,1),0)=0 or r.origin_source=any(p_sources))
       and (p_since is null or r.created_at >= p_since)
       and (not has_items or (item_entity='resource' and r.id::text=any(p_item_ids)))
    union all
    select 'resource'::text, ld.item_id, 'workflow_python'::text,
           left(ld.chunk_text,512), 0.93::real,
           'workflow_python_fragment'::text, r.created_at
      from public.lexical_documents ld
      join public.resources r on r.id::text=ld.item_id and r.current_revision_id=ld.source_revision_id
      join public.resource_revisions rr on rr.id=r.current_revision_id
      left join public.knowledge_resource_python_state ps
        on ps.resource_id=r.id and ps.revision_id=rr.id
     where want_resource and ld.entity_type='resource'
       and ld.representation_type='workflow_python'
       and char_length(ld.chunk_text) between 1 and 8000
       and qn <> '' and public.hivemind_normalize_identifier(ld.chunk_text) like '%'||qn||'%'
       and rr.state='accepted' and rr.kind='workflow'
       and coalesce(ps.public_state,'quarantined')='safe'
       and (coalesce(array_length(p_sources,1),0)=0 or r.origin_source=any(p_sources))
       and (p_since is null or r.created_at >= p_since)
       and (not has_items or (item_entity='resource' and r.id::text=any(p_item_ids)))
  ), collapsed as (
    select distinct on (arms.entity_type,arms.item_id) arms.* from arms
    order by arms.entity_type,arms.item_id,arms.lexical_rank desc,
             arms.representation_type,arms.created_at desc
  )
  select * from collapsed order by lexical_rank desc nulls last, created_at desc nulls last,
    entity_type, item_id limit least(greatest(coalesce(p_candidate_limit,100),1),500);
end; $$;

create or replace function public.hivemind_lexical_search(
  p_query text, p_limit int default 20, p_kinds text[] default '{}',
  p_sources text[] default '{}', p_item_ids text[] default '{}',
  p_since timestamptz default null, p_channels text[] default '{}',
  p_authors text[] default '{}', p_mode text default 'lexical') returns jsonb
language plpgsql security definer set search_path = public, pg_temp as $$
declare out_rows jsonb; n int := least(greatest(coalesce(p_limit,20),1),100);
begin
  if btrim(coalesce(p_query,''))='' then raise exception 'query must be non-empty'; end if;
  if p_mode <> 'lexical' then raise exception 'mode must be lexical'; end if;
  with c as (select * from public.hivemind_lexical_candidates(p_query,n*5,p_kinds,p_sources,p_item_ids,p_since,p_channels,p_authors)),
  h as (
    select c.*, case when c.entity_type='message' then m.content else rr.body end body,
      case when c.entity_type='message' then null::text else rr.title end title,
      case when c.entity_type='message' then m.author_name else null::text end author,
      case when c.entity_type='message' then m.channel_name else null::text end context,
      case when c.entity_type='message' then m.created_at else r.created_at end row_created,
      case when c.entity_type='message' then 'banodoco-discord' else r.origin_source end source,
      case when c.entity_type='message' then null::jsonb else rr.metadata end metadata,
      case when c.entity_type='message' then null::text else r.id::text end resource_id
      from c
      left join public.message_feed m on c.entity_type='message' and m.message_id::text=c.item_id
      left join public.resources r on c.entity_type='resource' and r.id::text=c.item_id
      left join public.resource_revisions rr on c.entity_type='resource' and rr.id=r.current_revision_id
    ), ranked as (select h.*, row_number() over(order by lexical_rank desc nulls last,row_created desc nulls last,entity_type,item_id) keyword_rank from h limit n)
  select coalesce(jsonb_agg(jsonb_build_object('kind',case when entity_type='message' then 'message' else rr_kind end,
    'source',source,'item_id',item_id,'title',title,'body',left(body,400),'author',author,'context',context,
    'url',null,'metadata',metadata,'created_at',to_char(row_created at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'),
    'match_type','keyword','keyword_rank',keyword_rank,'rrf_score',lexical_rank,
    'matched_representation',representation_type,'matched_snippet',matched_snippet) order by keyword_rank),'[]'::jsonb) into out_rows
  from (select ranked.*, case when entity_type='message' then 'message' else (select kind from public.resource_revisions x where x.id=(select current_revision_id from public.resources y where y.id=ranked.item_id::bigint)) end rr_kind from ranked) z;
  return jsonb_build_object('results',out_rows,'count',jsonb_array_length(out_rows),'meta',jsonb_build_object('mode_used','lexical','limit',n));
end; $$;

-- Semantic candidates retain the selected-contract HNSW driver and exact-mode
-- behavior from schema/033. The only source adaptation is the accepted current
-- resource revision; distillation arms are removed. Revision-qualified keys are
-- baked into the row-local vector predicate so a stale vector for a stable
-- resource ID cannot consume an HNSW result slot or rank after a head change.
create or replace function public.hivemind_semantic_candidates(
  p_query_embedding vector(384), p_candidate_limit int default 100,
  p_kinds text[] default '{}', p_item_ids text[] default '{}')
returns table(entity_type text,item_id text,kind text,representation_type text,
  chunk_index integer,matched_snippet text,semantic_distance double precision,
  semantic_rank integer,created_at timestamptz)
language plpgsql stable security definer set search_path=public,pg_temp as $$
declare
  v_active bigint := public.hivemind_active_contract_id(384);
  v_limit int := least(greatest(coalesce(p_candidate_limit,100),1),500);
  v_kinds text[] := coalesce(p_kinds,'{}'::text[]);
  v_items text[] := coalesce(p_item_ids,'{}'::text[]);
  v_has_kinds boolean := coalesce(array_length(v_kinds,1),0) > 0;
  v_has_items boolean := coalesce(array_length(v_items,1),0) > 0;
  v_want_message boolean := not v_has_kinds or 'message'=any(v_kinds);
  v_want_resource boolean := not v_has_kinds or exists(select 1 from unnest(v_kinds) k where k <> 'message');
  v_concrete_kinds text[];
  v_item_entity text;
  v_exact boolean := coalesce(nullif(current_setting('hivemind.semantic_exact_mode',true),''),'off')::boolean;
  v_overfetch int := case when v_exact or v_has_items then null else least(greatest(v_limit*8,40),80) end;
  v_msg_ids text[] := '{}';
  v_res_keys text[] := '{}';
  v_safe_wp_keys text[] := '{}';
  v_msg_lit text;
  v_res_lit text;
  v_safe_wp_lit text;
  v_limlit text := coalesce(v_overfetch::text,'null');
  v_sql text;
  v_arm text[] := '{}';
begin
  if v_active is null or v_active <> 1360541028304258884 then return; end if;
  select array_agg(k) into v_concrete_kinds
    from unnest(v_kinds) k where k not in ('message','resource');
  if v_has_items then
    if v_want_message and not v_want_resource then v_item_entity := 'message';
    elsif v_want_resource and not v_want_message then v_item_entity := 'resource';
    else v_item_entity := null;
    end if;
  end if;
  if v_want_message and (not v_has_items or v_item_entity='message') then
    select coalesce(array_agg(distinct m.message_id::text),'{}'::text[]) into v_msg_ids
      from public.discord_messages m
     where coalesce(m.is_deleted,false)=false
       and exists (select 1 from public.content_embeddings e
                    where e.contract_id=1360541028304258884 and e.entity_type='message'
                      and e.item_id=m.message_id::text)
       and (not v_has_items or m.message_id::text=any(v_items));
  end if;
  if v_want_resource and (not v_has_items or v_item_entity='resource') then
    select coalesce(array_agg(distinct r.id::text||':'||rr.id::text),'{}'::text[])
      into v_res_keys
      from public.resources r
      join public.resource_revisions rr on rr.id=r.current_revision_id and rr.state='accepted'
     where (not exists(select 1 from unnest(v_kinds) k where k <> 'message' and k <> 'resource')
            or rr.kind=any(coalesce(v_concrete_kinds,'{}'::text[])))
       and exists (select 1 from public.content_embeddings e
                    where e.contract_id=1360541028304258884 and e.entity_type='resource'
                      and e.item_id=r.id::text and e.source_revision_id=rr.id)
       and (not v_has_items or r.id::text=any(v_items));
    select coalesce(array_agg(distinct r.id::text||':'||rr.id::text),'{}'::text[])
      into v_safe_wp_keys
      from public.resources r
      join public.resource_revisions rr on rr.id=r.current_revision_id
                                        and rr.state='accepted' and rr.kind='workflow'
      join public.knowledge_resource_python_state s on s.resource_id=r.id and s.revision_id=rr.id
     where s.public_state='safe'
       and (not exists(select 1 from unnest(v_kinds) k where k <> 'message' and k <> 'resource')
            or rr.kind=any(coalesce(v_concrete_kinds,'{}'::text[])))
       and exists (select 1 from public.content_embeddings e
                    where e.contract_id=1360541028304258884 and e.entity_type='resource'
                      and e.representation_type='workflow_python'
                      and e.item_id=r.id::text and e.source_revision_id=rr.id)
       and (not v_has_items or r.id::text=any(v_items));
  end if;
  if coalesce(array_length(v_msg_ids,1),0)>0 then
    select quote_literal('{'||string_agg(x,',')||'}')||'::text[]' into v_msg_lit from unnest(v_msg_ids) x;
    v_arm := v_arm || array['select ''message''::text as entity_type, e.item_id, e.representation_type, e.chunk_index, e.chunk_text, (e.embedding <=> $1) as semantic_distance from public.content_embeddings e where e.contract_id=1360541028304258884 and e.entity_type=''message'' and e.item_id=any('||v_msg_lit||') order by e.embedding <=> $1 limit '||v_limlit];
  end if;
  if coalesce(array_length(v_res_keys,1),0)>0 then
    select quote_literal('{'||string_agg(x,',')||'}')||'::text[]' into v_res_lit from unnest(v_res_keys) x;
    select quote_literal('{'||coalesce(string_agg(x,','),'')||'}')||'::text[]' into v_safe_wp_lit from unnest(v_safe_wp_keys) x;
    v_arm := v_arm || array['select ''resource''::text as entity_type, e.item_id, e.representation_type, e.chunk_index, e.chunk_text, (e.embedding <=> $1) as semantic_distance from public.content_embeddings e where e.contract_id=1360541028304258884 and e.entity_type=''resource'' and (e.item_id||'':''||e.source_revision_id::text)=any('||v_res_lit||') and (e.representation_type<>''workflow_python'' or (e.item_id||'':''||e.source_revision_id::text)=any('||v_safe_wp_lit||')) order by e.embedding <=> $1 limit '||v_limlit];
  end if;
  if coalesce(array_length(v_arm,1),0)=0 then return; end if;
  v_sql := 'with arms as ('||
    (select string_agg('('||a||')',' union all ') from unnest(v_arm) a)||
    '), collapsed as (select distinct on (arms.entity_type,arms.item_id) arms.* from arms order by arms.entity_type,arms.item_id,arms.semantic_distance asc nulls last,case when arms.representation_type=''prose'' then 0 else 1 end,arms.chunk_index asc) select c.entity_type,c.item_id,case when c.entity_type=''resource'' then rr.kind else ''message'' end as kind,c.representation_type,c.chunk_index,left(coalesce(c.chunk_text,'''') ,512),c.semantic_distance,row_number() over(order by c.semantic_distance,c.entity_type,c.item_id)::int,case when c.entity_type=''resource'' then r.created_at else m.created_at end from collapsed c left join public.resources r on c.entity_type=''resource'' and r.id::text=c.item_id left join public.resource_revisions rr on rr.id=r.current_revision_id and rr.state=''accepted'' left join public.discord_messages m on c.entity_type=''message'' and m.message_id::text=c.item_id order by c.semantic_distance,c.entity_type,c.item_id limit '||v_limit::text;
  return query execute v_sql using p_query_embedding;
end; $$;

revoke execute on function public.hivemind_semantic_candidates(vector,integer,text[],text[]) from public;
do $$ begin
  if exists (select 1 from pg_roles where rolname='anon') then revoke execute on function public.hivemind_semantic_candidates(vector,integer,text[],text[]) from anon; end if;
  if exists (select 1 from pg_roles where rolname='authenticated') then revoke execute on function public.hivemind_semantic_candidates(vector,integer,text[],text[]) from authenticated; end if;
  if exists (select 1 from pg_roles where rolname='service_role') then grant execute on function public.hivemind_semantic_candidates(vector,integer,text[],text[]) to service_role; end if;
end $$;

-- Replace the historical worker source resolver with the accepted current
-- resource/revision source.  The function signature stays stable for the
-- existing finalize/payload surfaces, but no active path reads
-- external_resources or distillations anymore.
create or replace function public.hivemind_current_representation(
  p_entity_type text, p_item_id text, p_representation_type text
) returns table(representation_hash text, source_available boolean,
                public_state text, kind_ok boolean)
language plpgsql stable security definer set search_path=public,pg_temp as $$
declare
  v_text text := '';
  v_state text := null;
  v_available boolean := false;
  v_kind_ok boolean := true;
  v_kind text;
  v_resource_id bigint;
  v_revision_id bigint;
begin
  if p_entity_type='message' then
    select coalesce(m.content,'') into v_text
      from public.discord_messages m
     where m.message_id=p_item_id::bigint and coalesce(m.is_deleted,false)=false;
    v_available := found and btrim(v_text) <> '';
  elsif p_entity_type='resource' then
    select r.id, r.current_revision_id, rr.kind,
           public.hivemind_canonical_resource_text(
             rr.title,
             public.hivemind_workflow_prose(rr.body,rr.kind),
             public.hivemind_resource_tags(rr.metadata)||' '||
               public.hivemind_workflow_semantics_text(rr.metadata)),
           case when p_representation_type='workflow_python'
                then coalesce(s.public_state,'quarantined') else null end
      into v_resource_id, v_revision_id, v_kind, v_text, v_state
      from public.resources r
      join public.resource_revisions rr on rr.id=r.current_revision_id
      left join public.knowledge_resource_python_state s
        on s.resource_id=r.id and s.revision_id=rr.id
     where r.id=p_item_id::bigint and rr.state='accepted';
    if p_representation_type='workflow_python' then
      select coalesce(rr.payload->>'python_source',''), rr.kind='workflow',
             coalesce(s.public_state,'safe')
        into v_text, v_kind_ok, v_state
        from public.resources r
        join public.resource_revisions rr on rr.id=r.current_revision_id
        left join public.knowledge_resource_python_state s
          on s.resource_id=r.id and s.revision_id=rr.id
       where r.id=p_item_id::bigint and rr.state='accepted';
      v_available := found and v_kind_ok and btrim(v_text)<>'' and v_state='safe';
    else
      v_available := found and btrim(v_text)<>'';
    end if;
  end if;
  if v_available then
    return query select public.hivemind_representation_hash(v_text),true,v_state,v_kind_ok;
  else
    return query select case when btrim(v_text)<>'' then public.hivemind_representation_hash(v_text) else '' end,
      false,v_state,v_kind_ok;
  end if;
end; $$;

-- Keep the manifest-backed worker surface, but resolve it against the same
-- current resource/revision source as search. Prose has no Python public state;
-- returning NULL for that state is important because NULL is the expected value
-- for a prose payload, while workflow_python must be explicitly safe.
drop function if exists public.hivemind_embedding_payload(text,text,text,int,int);
create or replace function public.hivemind_embedding_payload(
  p_entity_type text, p_item_id text, p_representation_type text,
  p_target_tokens int default 512, p_overlap_tokens int default 50
) returns table(
  representation_hash text, source_available boolean, public_state text,
  chunk_index int, chunk_text text, chunk_hash text, method text
)
language plpgsql security definer set search_path=public,pg_temp as $$
declare
  m record;
  cur record;
begin
  if p_target_tokens <> 512 or p_overlap_tokens <> 50 then
    return query select ''::text,false,null::text,null::int,null::text,null::text,null::text;
    return;
  end if;
  select * into m
    from public.content_representation_manifest
   where entity_type=p_entity_type and item_id=p_item_id
     and representation_type=p_representation_type;
  if not found or m.chunking_version <> 2
     or m.chunk_config_identity <> 'chunk_config'||E'\x1f'||'v1'||E'\x1f'||'prose#512/50'||E'\x1f'||'workflow_python#512/50'
     or not exists (select 1 from public.embedding_contract_config_registry c
                     where c.selected_contract_id=1360541028304258884
                       and c.selection_state in ('selected_draft','active')) then
    return query select ''::text,false,null::text,null::int,null::text,null::text,null::text;
    return;
  end if;
  select * into cur from public.hivemind_current_representation(
    p_entity_type,p_item_id,p_representation_type);
  if m.representation_hash is distinct from cur.representation_hash
     or m.source_available is distinct from cur.source_available
     or (p_representation_type='workflow_python'
         and (m.public_state is distinct from cur.public_state or not cur.kind_ok)) then
    return query select coalesce(cur.representation_hash,''),false,
      case when p_representation_type='workflow_python' then cur.public_state else null end,
      null::int,null::text,null::text,null::text;
    return;
  end if;
  if not cur.source_available or
     (p_representation_type='workflow_python' and cur.public_state <> 'safe') then
    return query select '',false,
      case when p_representation_type='workflow_python' then cur.public_state else null end,
      null::int,null::text,null::text,null::text;
    return;
  end if;
  return query
    select m.representation_hash,true,
      case when p_representation_type='workflow_python' then m.public_state else null end,
      (chunk->>'chunk_index')::int,chunk->>'chunk_text',chunk->>'chunk_hash',m.chunk_method
      from jsonb_array_elements(m.chunks) chunk;
end; $$;

-- The finalizer is the atomic freshness boundary. It checks the job's pinned
-- resource revision before any drop/delete/replace, then validates the manifest
-- payload and writes source_revision_id on every stored vector. A late worker
-- therefore requeues without deleting the newer head's vectors.
create or replace function public.hivemind_finalize_embedding_job(
  p_job_id bigint, p_worker_id text, p_chunks jsonb,
  p_expected_representation_hash text, p_expected_public_state text,
  p_expected_source_available boolean
) returns table(chunks_written int, outcome text, job_status text)
language plpgsql security definer set search_path=public,pg_temp as $$
declare
  j record;
  current_head bigint;
  cur record;
  manifest record;
  chunk jsonb;
  written int := 0;
begin
  select * into j from public.embedding_jobs
   where id=p_job_id for update;
  if not found then
    return query select 0,'not_processing'::text,'missing'::text;
    return;
  end if;
  if j.status <> 'processing' or j.locked_by is distinct from p_worker_id then
    return query select 0,'not_processing'::text,j.status::text;
    return;
  end if;
  if j.contract_id is distinct from 1360541028304258884
     or not exists (select 1 from public.embedding_contract_config_registry c
                     where c.selected_contract_id=j.contract_id
                       and c.selection_state in ('selected_draft','active')) then
    update public.embedding_jobs set status='failed',last_error='contract_mismatch',
      locked_by=null,locked_at=null,lease_expires_at=null,updated_at=now() where id=j.id;
    return query select 0,'contract_mismatch'::text,'failed'::text;
    return;
  end if;
  if j.entity_type='resource' then
    select r.current_revision_id into current_head
      from public.resources r where r.id=j.item_id::bigint;
    if j.source_revision_id is null or current_head is distinct from j.source_revision_id then
      update public.embedding_jobs set status='pending',next_attempt_at=now(),
        locked_by=null,locked_at=null,lease_expires_at=null,updated_at=now() where id=j.id;
      return query select 0,'source_changed'::text,'pending'::text;
      return;
    end if;
  end if;
  if j.job_kind='drop' then
    delete from public.content_embeddings
     where contract_id=j.contract_id and entity_type=j.entity_type
       and item_id=j.item_id and representation_type=j.representation_type;
    update public.embedding_jobs set status='done',locked_by=null,locked_at=null,
      lease_expires_at=null,updated_at=now() where id=j.id;
    return query select 0,'dropped'::text,'done'::text;
    return;
  end if;
  select * into cur from public.hivemind_current_representation(
    j.entity_type,j.item_id,j.representation_type);
  if not cur.source_available or
     (j.representation_type='workflow_python' and
      ((not cur.kind_ok) or cur.public_state <> 'safe')) then
    delete from public.content_embeddings
     where contract_id=j.contract_id and entity_type=j.entity_type
       and item_id=j.item_id and representation_type=j.representation_type;
    update public.embedding_jobs set status='done',locked_by=null,locked_at=null,
      lease_expires_at=null,updated_at=now() where id=j.id;
    return query select 0,'dropped'::text,'done'::text;
    return;
  end if;
  if p_expected_source_available is distinct from cur.source_available
     or p_expected_representation_hash is distinct from cur.representation_hash
     or p_expected_public_state is distinct from cur.public_state then
    update public.embedding_jobs set status='pending',next_attempt_at=now(),
      locked_by=null,locked_at=null,lease_expires_at=null,updated_at=now() where id=j.id;
    return query select 0,'source_changed'::text,'pending'::text;
    return;
  end if;
  select * into manifest from public.content_representation_manifest m
   where m.entity_type=j.entity_type and m.item_id=j.item_id
     and m.representation_type=j.representation_type
     and m.representation_hash=cur.representation_hash
     and m.source_available=true and m.chunking_version=2
     and m.chunk_config_identity='chunk_config'||E'\x1f'||'v1'||E'\x1f'||'prose#512/50'||E'\x1f'||'workflow_python#512/50';
  if not found or p_chunks is null or jsonb_typeof(p_chunks)<>'array'
     or jsonb_array_length(p_chunks) <> manifest.chunk_count then
    update public.embedding_jobs set status='pending',next_attempt_at=now(),
      locked_by=null,locked_at=null,lease_expires_at=null,updated_at=now() where id=j.id;
    return query select 0,'source_changed'::text,'pending'::text;
    return;
  end if;
  for chunk in select value from jsonb_array_elements(p_chunks) loop
    if coalesce(chunk->>'entity_type','')<>j.entity_type
       or coalesce(chunk->>'item_id','')<>j.item_id
       or coalesce(chunk->>'representation_type','')<>j.representation_type
       or coalesce(chunk->>'contract_id','')<>j.contract_id::text
       or coalesce(chunk->>'representation_hash','')<>cur.representation_hash
       or coalesce(chunk->>'chunk_text','')=''
       or coalesce(chunk->>'chunk_hash','') <> public.hivemind_representation_hash(chunk->>'chunk_text')
       or not exists (select 1 from jsonb_array_elements(manifest.chunks) expected
                       where expected->>'chunk_index'=chunk->>'chunk_index'
                         and expected->>'chunk_hash'=chunk->>'chunk_hash'
                         and expected->>'chunk_text'=chunk->>'chunk_text')
       or (chunk->>'embedding') ~* '(nan|inf)' then
      update public.embedding_jobs set status='failed',last_error='vector_validation_failed',
        locked_by=null,locked_at=null,lease_expires_at=null,updated_at=now() where id=j.id;
      return query select 0,'validation_failed'::text,'failed'::text;
      return;
    end if;
    perform (chunk->>'embedding')::vector(384);
  end loop;
  delete from public.content_embeddings
   where contract_id=j.contract_id and entity_type=j.entity_type
     and item_id=j.item_id and representation_type=j.representation_type;
  for chunk in select value from jsonb_array_elements(p_chunks) loop
    insert into public.content_embeddings(
      contract_id,entity_type,item_id,representation_type,chunk_index,chunk_text,
      embedding,representation_hash,chunk_hash,source_revision_id)
    values(j.contract_id,j.entity_type,j.item_id,j.representation_type,
      (chunk->>'chunk_index')::int,left(chunk->>'chunk_text',1024),
      (chunk->>'embedding')::vector(384),chunk->>'representation_hash',
      chunk->>'chunk_hash',case when j.entity_type='resource' then j.source_revision_id else null end);
    written := written + 1;
  end loop;
  update public.embedding_jobs set status='done',locked_by=null,locked_at=null,
    lease_expires_at=null,last_error=null,updated_at=now() where id=j.id;
  return query select written,'completed'::text,'done'::text;
end; $$;

-- Cleanup follows the same current-head source contract.  Historical
-- distillation/external-resource cleanup is deliberately replaced, not kept
-- as an active fallback.
create or replace function public.hivemind_cleanup_ineligible_embeddings(
  p_batch_size int default 1000
) returns int
language plpgsql security definer set search_path=public,pg_temp as $$
declare
  v_active bigint := public.hivemind_active_contract_id();
  v_removed int := 0;
  v_n int;
begin
  if v_active is null then return 0; end if;
  with doomed as (
    select ce.ctid from public.content_embeddings ce
     where ce.contract_id=v_active and ce.entity_type='message'
       and not exists (select 1 from public.discord_messages m
                        where m.message_id::text=ce.item_id and coalesce(m.is_deleted,false)=false)
     limit p_batch_size)
  delete from public.content_embeddings where ctid in (select ctid from doomed);
  get diagnostics v_n=row_count; v_removed:=v_removed+v_n;
  with doomed as (
    select ce.ctid from public.content_embeddings ce
     where ce.contract_id=v_active and ce.entity_type='resource'
       and not exists (
         select 1 from public.resources r
         join public.resource_revisions rr on rr.id=r.current_revision_id and rr.state='accepted'
          where r.id::text=ce.item_id and ce.source_revision_id=rr.id
            and (ce.representation_type <> 'workflow_python'
                 or (rr.kind='workflow' and coalesce((select s.public_state
                    from public.knowledge_resource_python_state s
                   where s.resource_id=r.id and s.revision_id=rr.id),'quarantined')='safe')))
     limit greatest(p_batch_size-v_removed,0))
  delete from public.content_embeddings where ctid in (select ctid from doomed);
  get diagnostics v_n=row_count; v_removed:=v_removed+v_n;
  return v_removed;
end; $$;

-- Resource-head changes are the only source of searchable resource/index work.
drop trigger if exists trg_embedding_jobs_external_resources on public.external_resources;
drop trigger if exists trg_embedding_jobs_distillations on public.distillations;
create or replace function public.trg_embedding_jobs_resources_fn() returns trigger
language plpgsql security definer set search_path=public,pg_temp as $$
begin
  if new.current_revision_id is distinct from old.current_revision_id and new.current_revision_id is not null then
    perform public.hivemind_enqueue_embedding_job('resource',new.id::text,'prose','reembed','revision_accepted',NULL::bigint,new.current_revision_id);
    if exists(select 1 from public.resource_revisions where id=new.current_revision_id and kind='workflow') then
      perform public.hivemind_enqueue_embedding_job('resource',new.id::text,'workflow_python','reembed','revision_accepted',NULL::bigint,new.current_revision_id);
    end if;
  end if;
  return new;
end; $$;
drop trigger if exists trg_embedding_jobs_resources on public.resources;
create trigger trg_embedding_jobs_resources after update of current_revision_id on public.resources for each row execute function public.trg_embedding_jobs_resources_fn();

drop function if exists public.hivemind_enqueue_embedding_job(text,text,text,text,text,bigint);
drop function if exists public.hivemind_enqueue_embedding_job(text,text,text,text,text,bigint,bigint);
create or replace function public.hivemind_enqueue_embedding_job(
  p_entity_type text,p_item_id text,p_representation_type text,p_job_kind text,p_source_op text,
  p_contract_id bigint default null,p_source_revision_id bigint default null) returns void
language plpgsql security definer set search_path=public,pg_temp as $$
declare c bigint; begin c:=coalesce(p_contract_id,public.hivemind_active_contract_id());
  insert into public.embedding_jobs(entity_type,item_id,representation_type,job_kind,source_op,contract_id,source_revision_id,status,next_attempt_at,enqueued_at,updated_at)
  values(p_entity_type,p_item_id,p_representation_type,p_job_kind,p_source_op,c,p_source_revision_id,'pending',now(),now(),now())
  on conflict (entity_type,item_id,representation_type) where status='pending' do update set
    job_kind=excluded.job_kind,source_op=excluded.source_op,contract_id=coalesce(excluded.contract_id,embedding_jobs.contract_id),source_revision_id=excluded.source_revision_id,next_attempt_at=now(),updated_at=now();
end; $$;

-- The queue is an internal worker surface. Recreating the function must not
-- reintroduce the default PUBLIC execute grant that PostgreSQL gives a new
-- SECURITY DEFINER function.
revoke execute on function public.hivemind_enqueue_embedding_job(text,text,text,text,text,bigint,bigint)
  from public;
do $$
begin
  if exists (select 1 from pg_roles where rolname='anon') then
    revoke execute on function public.hivemind_enqueue_embedding_job(text,text,text,text,text,bigint,bigint) from anon;
  end if;
  if exists (select 1 from pg_roles where rolname='authenticated') then
    revoke execute on function public.hivemind_enqueue_embedding_job(text,text,text,text,text,bigint,bigint) from authenticated;
  end if;
  if exists (select 1 from pg_roles where rolname='service_role') then
    grant execute on function public.hivemind_enqueue_embedding_job(text,text,text,text,text,bigint,bigint) to service_role;
  end if;
end $$;

-- Legacy finalization code writes the embedding row while its queue row is
-- processing.  Fill the exact accepted revision at that storage boundary when
-- an older caller did not pass the new column explicitly; newer backfill paths
-- already provide it.  This keeps revision identity attached to every active
-- resource vector without restoring a legacy source branch.
create or replace function public.trg_content_embeddings_revision_fn() returns trigger
language plpgsql security definer set search_path=public,pg_temp as $$
begin
  if new.entity_type='resource' and new.source_revision_id is null then
    select j.source_revision_id into new.source_revision_id
      from public.embedding_jobs j
     where j.entity_type='resource' and j.item_id=new.item_id
       and j.representation_type=new.representation_type
       and j.contract_id=new.contract_id and j.status='processing'
       and j.source_revision_id is not null
     order by j.id desc limit 1;
  end if;
  return new;
end; $$;
drop trigger if exists trg_content_embeddings_revision on public.content_embeddings;
create trigger trg_content_embeddings_revision
  before insert on public.content_embeddings
  for each row execute function public.trg_content_embeddings_revision_fn();

-- A claimed resource job is stale if its pinned revision is no longer the head.
create or replace function public.hivemind_complete_embedding_job(p_job_id bigint,p_worker_id text,p_chunks_written int default 0) returns void
language plpgsql security definer set search_path=public,pg_temp as $$
declare j public.embedding_jobs%rowtype; current_head bigint; begin
  select * into j from public.embedding_jobs where id=p_job_id and locked_by=p_worker_id and status='processing' for update;
  if not found then raise exception 'complete: job is not currently processing for worker'; end if;
  if j.entity_type='resource' then select current_revision_id into current_head from public.resources where id=j.item_id::bigint;
    if current_head is distinct from j.source_revision_id then update public.embedding_jobs set status='cancelled',locked_by=null,locked_at=null,lease_expires_at=null,updated_at=now() where id=j.id; return; end if;
  end if;
  update public.embedding_jobs set status='done',locked_by=null,locked_at=null,lease_expires_at=null,last_error=null,updated_at=now() where id=j.id;
end; $$;
