-- ============================================================
-- 013_lexical_latency_phase3.sql
-- Hivemind hybrid search — Phase 1, Task 1.10/1.11 phase-3 latency fix
--
-- One recall-preserving ADDITIVE optimization on top of schema/012. ADDITIVE in
-- the strictest sense: the ONLY DDL in this file is a single
-- `create or replace function public.hivemind_lexical_candidates(...)`. No MV,
-- no index, no table, no GRANT, no REVOKE, no DROP is added or changed. The
-- phase-2 MV (`lexical_workflow_python_search`, schema/012) and every existing
-- index are REUSED unchanged. CREATE OR REPLACE preserves proacl (proven in the
-- rehearsal), so schema/011's security posture is untouched.
--
-- THE FIX — adaptive matched_anchor lookup for the workflow_python fragment arm.
--   schema/012's per-item scalar lookup is efficient for dense/common needles
--   because its created_at-desc scan stops immediately, but expensive for
--   sparse needles whose only match is late in each item's chunk history.
--   Conversely, one global trigram scan is excellent for sparse/selective
--   needles but wasteful for dense ones.
--
--   013 classifies density from a bounded eight-row sample of the existing
--   workflow-python MV, then selects one of two semantically identical paths:
--   * dense: a bounded LEFT JOIN LATERAL preserves schema/012's early stop;
--   * sparse: a PL/pgSQL-only branch materializes the direct matching-chunk
--     scan, naturally served by lexical_documents_python_chunk_trgm_idx, then
--     selects the newest matching anchor per item into an in-memory JSONB map.
--   The sparse global scan does not exist in the dense execution branch.
--
-- EXACT 012 SEMANTICS PRESERVED (proven byte-for-byte on the isolated cluster).
--   * Both paths use schema/012's exact per-chunk predicates and newest-match
--     ordering. The dense LATERAL uses ORDER BY created_at DESC LIMIT 1; the
--     sparse path uses DISTINCT ON (item_id) with the same ordering.
--   * The fragment arm's candidate ROW SET is unchanged: same FROM
--     (lexical_workflow_python_search mv join safe_wf sw), same WHERE
--     (v_want_res, v_qn<>'', search_norm like v_qn, source filter, item filter),
--     same lexical_rank band 0.93, same lexical_source
--     'workflow_python_fragment', same created_at (sw.created_at). Only the
--     matched_snippet lookup strategy changes, and both branches yield the
--     identical value for every emitted row.
--   * Identical arm set, predicates, lexical_rank bands, collapse rule, and
--     deterministic ORDER BY/LIMIT as schema/012. The candidate identity stream
--     (entity, item, representation, matched_snippet/anchor, rank, source,
--     created_at, ORDER, global limit) is unchanged.
--   * CROSS-CHUNK safety unchanged: the MV is searched DIRECTLY (search_norm
--     like v_qn), never re-normalized; both anchor paths search normalized
--     chunk_text PER CHUNK (the 010/012 per-chunk containment), so a
--     needle present only across a chunk boundary matches in NEITHER (proven).
--   * Safe-workflow gate, quarantine exclusion, 1..8000 chunk bound, channel/
--     author direct-predicate semantics, empty-resolution behavior, and all
--     other arms are byte-identical to schema/012 (untouched).
--   * Post-limit hydration (schema/009 RPC) is untouched.
--
-- SECURITY. ADDITIVE: no GRANT/REVOKE, no new public surface. The function
-- remains STABLE, SECURITY INVOKER, reached only via the SECURITY DEFINER RPC in
-- schema/009. The phase-2 MV stays REVOKED from public/anon/authenticated
-- (schema/012). CREATE OR REPLACE preserves proacl (rehearsal-proven), so
-- schema/011's revokes (no execute for anon/authenticated/public on the
-- candidates function) survive.
--
-- ADDITIVE + REVERSIBLE + IDEMPOTENT.
--   * `create or replace function` is naturally idempotent; re-running is a
--     no-op. No source row is read or mutated.
--   * ROLLBACK: re-apply schema/012's function (CREATE OR REPLACE FUNCTION
--     public.hivemind_lexical_candidates(...) — schema/012). That restores the
--     per-item correlated scalar anchor and the exact pre-013 read path. No
--     object created by 013 needs dropping (013 creates none). One command:
--       psql -f schema/012_lexical_latency_phase2.sql
-- ============================================================

create or replace function public.hivemind_lexical_candidates(
  p_query           text,
  p_candidate_limit int      default 100,
  p_kinds           text[]   default '{}',
  p_sources         text[]   default '{}',
  p_item_ids        text[]   default '{}',
  p_since           timestamptz default null,
  p_channels        text[]   default '{}',
  p_authors         text[]   default '{}',
  p_author_optout   boolean  default false,
  p_bots_excluded   boolean  default false
)
returns table (
  entity_type        text,
  item_id            text,
  representation_type text,
  matched_snippet    text,
  lexical_rank       real,
  lexical_source     text,
  created_at         timestamptz
)
language plpgsql
stable
set search_path = public, pg_temp
as $$
declare
  v_qn        text := public.hivemind_normalize_identifier(p_query);
  v_fts       tsquery := websearch_to_tsquery('simple'::regconfig, coalesce(p_query, ''));
  v_phrase    tsquery;
  v_is_single boolean;
  v_dense     boolean := true;
  v_sparse_anchors jsonb := '{}'::jsonb;
  v_limit     int  := greatest(coalesce(p_candidate_limit, 100), 1);

  v_kinds     text[] := coalesce(p_kinds, '{}'::text[]);
  v_has_kinds boolean := coalesce(array_length(v_kinds, 1), 0) > 0;
  v_want_msg      boolean;
  v_want_res      boolean;
  v_want_dist     boolean;

  v_item_ids text[] := coalesce(p_item_ids, '{}'::text[]);
  v_has_items boolean := coalesce(array_length(v_item_ids, 1), 0) > 0;
  v_items_entity text;

  v_has_sources  boolean := coalesce(array_length(coalesce(p_sources,  '{}'::text[]), 1), 0) > 0;
  v_has_channels boolean := coalesce(array_length(coalesce(p_channels, '{}'::text[]), 1), 0) > 0;
  v_has_authors  boolean := coalesce(array_length(coalesce(p_authors,  '{}'::text[]), 1), 0) > 0;

  -- === OPTIMIZATION B (schema/012): resolve channel/author NAMES -> id arrays ONCE. ===
  v_channel_ids bigint[];
  v_author_ids  bigint[];
begin
  v_want_msg  := (not v_has_kinds) or v_kinds && array['message'];
  v_want_dist := (not v_has_kinds) or v_kinds && array['distillation'];
  v_want_res  := (not v_has_kinds) or exists (
    select 1 from unnest(v_kinds) k
    where k in ('resource','workflow','article','blog_post','transcript','repo','guide','doc')
       or k not in ('message','distillation')
  );

  if v_has_items then
    if v_want_msg and not v_want_res and not v_want_dist then
      v_items_entity := 'message';
    elsif v_want_res and not v_want_msg and not v_want_dist then
      v_items_entity := 'resource';
    elsif v_want_dist and not v_want_msg and not v_want_res then
      v_items_entity := 'distillation';
    else
      v_items_entity := '';
    end if;
  end if;

  -- === OPTIMIZATION B resolution (once per call). ===
  if v_has_channels then
    select array_agg(dc.channel_id) into v_channel_ids
      from public.discord_channels dc
     where dc.channel_name = any(coalesce(p_channels, '{}'::text[]));
  end if;
  if v_has_authors then
    select array_agg(mb.member_id) into v_author_ids
      from public.members mb
     where coalesce(mb.global_name, mb.username) = any(coalesce(p_authors, '{}'::text[]));
  end if;

  v_is_single := coalesce(btrim(p_query), '') <> ''
                 and position(' ' in btrim(p_query)) = 0
                 and p_query not like '%"%' and p_query not like '%-%';
  if v_is_single or (p_query like '"%"' ) then
    v_phrase := phraseto_tsquery('simple'::regconfig, coalesce(p_query, ''));
  else
    v_phrase := null::tsquery;
  end if;

  -- Classify the fragment needle from a bounded sample of the existing MV.
  -- Repeated occurrences within each matching workflow imply a dense/common
  -- needle, where schema/012's bounded per-item lookup is cheaper. A sparse
  -- needle uses the global trigram path below. This affects only the anchor
  -- lookup strategy; both paths emit the identical fragment candidate set.
  if v_want_res and v_qn <> '' then
    select coalesce(max(s.occurrences) >= 4, false)
      into v_dense
      from (
        select
          (char_length(mv.search_norm)
           - char_length(replace(mv.search_norm, v_qn, '')))
          / greatest(char_length(v_qn), 1) as occurrences
          from public.lexical_workflow_python_search mv
         where mv.search_norm like '%' || v_qn || '%'
         limit 8
      ) s;
  end if;

  -- h013_sparse_path
  -- The global matching-chunk scan exists only in this PL/pgSQL branch, so it
  -- cannot be planned or executed for a dense/common needle.
  if not v_dense then
    with
    sparse_matches as materialized (
      -- h013_sparse_match_begin
      select ld.item_id, ld.matched_anchor, ld.created_at
        from public.lexical_documents ld
       where ld.entity_type = 'resource'
         and ld.representation_type = 'workflow_python'
         and ld.quarantine_state = 'safe'
         and char_length(ld.chunk_text) between 1 and 8000
         and public.hivemind_normalize_identifier(ld.chunk_text) like '%' || v_qn || '%'
      -- h013_sparse_match_end
    ),
    sparse_newest as (
      select distinct on (sm.item_id)
             sm.item_id, sm.matched_anchor
        from sparse_matches sm
       order by sm.item_id, sm.created_at desc
    )
    select coalesce(
             jsonb_object_agg(sn.item_id, to_jsonb(sn.matched_anchor)),
             '{}'::jsonb)
      into v_sparse_anchors
      from sparse_newest sn;
  end if;

  -- h013_dense_path
  return query
  with
  -- === safe workflow resource set, computed ONCE (schema/010 defect-6 fix) ===
  safe_wf as materialized (
    select r.id::text        as item_id,
           r.created_at      as created_at,
           r.source          as source
      from public.external_resources r
     where r.kind = 'workflow'
       and public.hivemind_workflow_python_state(r.id) = 'safe'
  ),
  arms as (
    -- ===================== MESSAGE: FTS arm (schema/004 index) =====================
    select 'message'::text  as entity_type,
           m.message_id::text as item_id,
           'prose'::text      as representation_type,
           null::text         as matched_snippet,
           ts_rank(to_tsvector('simple'::regconfig, coalesce(m.content,'')), v_fts, 32) as lexical_rank,
           'message_fts'::text as lexical_source,
           m.created_at
      from public.discord_messages m
     where (v_want_msg)
       and v_fts is not null and v_fts <> to_tsquery('simple'::regconfig, '')
       and to_tsvector('simple'::regconfig, coalesce(m.content,'')) @@ v_fts
       and m.is_deleted = false
       and (not p_author_optout or exists (
              select 1 from public.members mb
              where mb.member_id = m.author_id
                and coalesce(mb.allow_content_sharing, true) = true))
       and (not p_bots_excluded or not exists (
              select 1 from public.members mb
              where mb.member_id = m.author_id
                and (coalesce(mb.bot,false) or coalesce(mb.system,false))))
       and (not v_has_sources or 'banodoco-discord' = any(coalesce(p_sources,'{}'::text[])))
       and (p_since is null or m.created_at >= p_since)
       and (not v_has_channels or m.channel_id = any(coalesce(v_channel_ids, '{}'::bigint[])))
       and (not v_has_authors  or m.author_id  = any(coalesce(v_author_ids,  '{}'::bigint[])))
       and (v_items_entity is distinct from 'message'
            or m.message_id::text = any(v_item_ids))

    union all
    -- ============== MESSAGE: exact-identifier containment (task 1.6 / 007) ==============
    select 'message'::text, m.message_id::text, 'prose'::text, null::text,
           case when public.hivemind_normalize_identifier(m.content) = v_qn then 1.0::real else 0.9::real end,
           'message_ident'::text, m.created_at
      from public.discord_messages m
     where (v_want_msg)
       and v_qn <> ''
       and char_length(m.content) between 1 and 8000
       and m.is_deleted = false
       and public.hivemind_normalize_identifier(m.content) like '%' || v_qn || '%'
       and (not p_author_optout or exists (
              select 1 from public.members mb where mb.member_id = m.author_id
                and coalesce(mb.allow_content_sharing, true) = true))
       and (not p_bots_excluded or not exists (
              select 1 from public.members mb where mb.member_id = m.author_id
                and (coalesce(mb.bot,false) or coalesce(mb.system,false))))
       and (p_since is null or m.created_at >= p_since)
       and (not v_has_channels or m.channel_id = any(coalesce(v_channel_ids, '{}'::bigint[])))
       and (not v_has_authors  or m.author_id  = any(coalesce(v_author_ids,  '{}'::bigint[])))
       and (v_items_entity is distinct from 'message'
            or m.message_id::text = any(v_item_ids))

    union all
    -- ============== RESOURCE: prose FTS (schema/003 prose_tsv index) ==============
    select 'resource'::text, r.id::text, 'prose'::text, null::text,
           ts_rank(r.prose_tsv, v_fts, 32), 'resource_prose_fts'::text, r.created_at
      from public.external_resources r
     where (v_want_res)
       and v_fts is not null and v_fts <> to_tsquery('simple'::regconfig, '')
       and r.prose_tsv @@ v_fts
       and (not v_has_sources or r.source = any(coalesce(p_sources,'{}'::text[])))
       and (p_since is null or r.created_at >= p_since)
       and (v_items_entity is distinct from 'resource' or r.id::text = any(v_item_ids))

    union all
    -- ============== RESOURCE: prose phrase arm (tight adjacency for names) ==============
    select 'resource'::text, r.id::text, 'prose'::text, null::text,
           ts_rank(r.prose_tsv, v_phrase, 32), 'resource_prose_phrase'::text, r.created_at
      from public.external_resources r
     where (v_want_res) and v_phrase is not null
       and r.prose_tsv @@ v_phrase
       and (not v_has_sources or r.source = any(coalesce(p_sources,'{}'::text[])))
       and (p_since is null or r.created_at >= p_since)
       and (v_items_entity is distinct from 'resource' or r.id::text = any(v_item_ids))

    union all
    -- ============== RESOURCE: title exact-name trigram (schema/006) ==============
    select 'resource'::text, r.id::text, 'prose'::text, null::text,
           0.95::real, 'resource_title_ident'::text, r.created_at
      from public.external_resources r
     where (v_want_res) and v_qn <> ''
       and char_length(public.hivemind_normalize_identifier(r.title)) between 1 and 300
       and public.hivemind_normalize_identifier(r.title) like '%' || v_qn || '%'
       and (not v_has_sources or r.source = any(coalesce(p_sources,'{}'::text[])))
       and (p_since is null or r.created_at >= p_since)
       and (v_items_entity is distinct from 'resource' or r.id::text = any(v_item_ids))

    union all
    -- ====== RESOURCE: workflow_python FTS arm (schema/003 lexical_documents tsv) ======
    -- Unchanged from schema/010/012: the FTS arm stays over lexical_documents.tsv.
    select 'resource'::text, ld.item_id, 'workflow_python'::text, ld.matched_anchor,
           ts_rank(ld.tsv, v_fts, 32), 'workflow_python_fts'::text, sw.created_at
      from public.lexical_documents ld
      join safe_wf sw on sw.item_id = ld.item_id
     where (v_want_res)
       and ld.entity_type = 'resource'
       and ld.representation_type = 'workflow_python'
       and ld.quarantine_state = 'safe'
       and v_fts is not null and v_fts <> to_tsquery('simple'::regconfig, '')
       and ld.tsv @@ v_fts
       and (not v_has_sources or sw.source = any(coalesce(p_sources,'{}'::text[])))
       and (v_items_entity is distinct from 'resource' or ld.item_id = any(v_item_ids))

    union all
    -- ====== RESOURCE: workflow_python bounded fragment arm ======
    -- === OPTIMIZATION (013): matched_snippet comes from the dense LATERAL
    -- early-stop or the sparse precomputed anchor map, selected by v_dense.
    -- The candidate ROW SET (FROM/WHERE/ rank / source / created_at) is
    -- byte-identical to schema/012; only the matched_snippet expression changes,
    -- and it yields the identical anchor value (proven byte-for-byte vs 012). ===
    select 'resource'::text, mv.item_id, 'workflow_python'::text,
           case when v_dense
                then matched.matched_anchor
                else v_sparse_anchors ->> mv.item_id
            end as matched_snippet,
           0.93::real, 'workflow_python_fragment'::text, sw.created_at
      from public.lexical_workflow_python_search mv
      join safe_wf sw on sw.item_id = mv.item_id
      left join lateral (
        select ld.matched_anchor
          from public.lexical_documents ld
         where v_dense
           and ld.item_id = mv.item_id
           and ld.entity_type = 'resource'
           and ld.representation_type = 'workflow_python'
           and ld.quarantine_state = 'safe'
           and char_length(ld.chunk_text) between 1 and 8000
           and public.hivemind_normalize_identifier(ld.chunk_text) like '%' || v_qn || '%'
         order by ld.created_at desc
         limit 1
      ) matched on true
     where (v_want_res) and v_qn <> ''
       and mv.search_norm like '%' || v_qn || '%'
       and (not v_has_sources or sw.source = any(coalesce(p_sources,'{}'::text[])))
       and (v_items_entity is distinct from 'resource' or mv.item_id = any(v_item_ids))

    union all
    -- ============== DISTILLATION: FTS arm (schema/003 lexical_tsv index) ==============
    select 'distillation'::text, d.id::text, 'prose'::text, null::text,
           ts_rank(d.lexical_tsv, v_fts, 32), 'distillation_fts'::text, d.created_at
      from public.distillations d
     where (v_want_dist)
       and v_fts is not null and v_fts <> to_tsquery('simple'::regconfig, '')
       and d.status in ('pending','approved')
       and d.lexical_tsv @@ v_fts
       and (not v_has_sources or 'hivemind' = any(coalesce(p_sources,'{}'::text[])))
       and (p_since is null or d.created_at >= p_since)
       and (v_items_entity is distinct from 'distillation' or d.id::text = any(v_item_ids))

    union all
    -- ============== DISTILLATION: question exact-name trigram (schema/006) ==============
    select 'distillation'::text, d.id::text, 'prose'::text, null::text,
           0.95::real, 'distillation_question_ident'::text, d.created_at
      from public.distillations d
     where (v_want_dist) and v_qn <> ''
       and d.status in ('pending','approved')
       and char_length(public.hivemind_normalize_identifier(d.question)) between 1 and 300
       and public.hivemind_normalize_identifier(d.question) like '%' || v_qn || '%'
       and (not v_has_sources or 'hivemind' = any(coalesce(p_sources,'{}'::text[])))
       and (p_since is null or d.created_at >= p_since)
       and (v_items_entity is distinct from 'distillation' or d.id::text = any(v_item_ids))
  ),
  collapsed as (
    select distinct on (arms.entity_type, arms.item_id)
           arms.entity_type, arms.item_id, arms.representation_type, arms.matched_snippet,
           arms.lexical_rank, arms.lexical_source, arms.created_at
      from arms
     order by arms.entity_type, arms.item_id,
              arms.lexical_rank desc nulls last,
              case when arms.representation_type = 'workflow_python' then 0 else 1 end,
              arms.created_at desc nulls last,
              arms.item_id
  )
  select collapsed.entity_type, collapsed.item_id, collapsed.representation_type,
         collapsed.matched_snippet, collapsed.lexical_rank, collapsed.lexical_source,
         collapsed.created_at
    from collapsed
   order by collapsed.lexical_rank desc nulls last,
            collapsed.created_at desc nulls last,
            collapsed.entity_type asc,
            collapsed.item_id asc
   limit v_limit;
end;
$$;

comment on function public.hivemind_lexical_candidates(text,int,text[],text[],text[],timestamptz,text[],text[],boolean,boolean) is
'Task 1.10/1.11 phase-3: schema/012 body with the workflow_python fragment '
'arm''s matched_anchor selected adaptively: bounded LATERAL for dense needles, '
'or a materialized trigram-indexed newest-anchor map for sparse needles. '
'Byte-identical results to schema/012 (and schema/010); STABLE; called only by '
'the SECURITY DEFINER RPC in schema/009. Additive: CREATE OR REPLACE FUNCTION '
'only — no MV/index/grant/revoke changes.';
