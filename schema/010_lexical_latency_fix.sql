-- ============================================================
-- 010_lexical_latency_fix.sql
-- Hivemind hybrid search — Phase 1, Task 1.10/1.11 correction (defect 6: latency)
--
-- Additive, non-destructive fix for the production latency regression in
-- hivemind_lexical_candidates (schema/008). Root cause (proven by live
-- EXPLAIN ANALYZE on production): the two workflow_python arms joined
-- lexical_documents to external_resources with a per-row STABLE call
-- `public.hivemind_workflow_python_state(r.id) = 'safe'`. The planner picked a
-- Nested Loop that re-Seq-Scanned external_resources ONCE PER MATCHED CHUNK
-- (307 loops for WanVideoSampler, 827 for KSampler), re-running the function
-- ~845k-2.3M times per query -> 2.7M-7.4M shared buffers (8.8s / 26.5s cold).
--
-- Fix: compute the safe-workflow resource set ONCE as a MATERIALIZED CTE
-- (safe_wf) and join both workflow_python arms to it. The STABLE function now
-- runs 2,757 times total (once per workflow), not per chunk. The join against
-- the small materialized tuplestore is a cheap hash/probe regardless of the
-- planner's trigram-cardinality estimate, so the bad Nested-Loop plan cannot
-- recur. Forced-hash-join on the old shape already proved 8.8s->0.32s and
-- 26.5s->0.81s (33x); this makes that plan the natural one.
--
-- SAFETY / RECALL PRESERVED (no arm dropped, no gate relaxed):
--   * Quarantined workflow_python never ranks: lexical_documents carries
--     representation_type + quarantine_state; schema/003 CHECK guarantees
--     workflow_python => quarantine_state='safe', and the refresh only writes
--     chunks for safe+available workflows. safe_wf additionally re-confirms
--     hivemind_workflow_python_state(id)='safe' (computed once) AND kind='workflow'.
--     BOTH the lexical_documents-side AND the resource-side safety checks remain.
--   * Identical arm set, predicates, lexical_rank bands, collapse rule, and
--     deterministic ORDER BY/LIMIT as schema/008. The candidate identity stream
--     is unchanged; only the join strategy for the two workflow_python arms changes.
--   * Post-limit hydration (schema/009 RPC) is untouched.
--
-- Idempotent (CREATE OR REPLACE FUNCTION; grants preserved). No source rows
-- read or mutated. STABLE, security INVOKER (called only by the SECURITY
-- DEFINER RPC in 009).
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

  v_is_single := coalesce(btrim(p_query), '') <> ''
                 and position(' ' in btrim(p_query)) = 0
                 and p_query not like '%"%' and p_query not like '%-%';
  if v_is_single or (p_query like '"%"' ) then
    v_phrase := phraseto_tsquery('simple'::regconfig, coalesce(p_query, ''));
  else
    v_phrase := null::tsquery;
  end if;

  return query
  with
  -- === safe workflow resource set, computed ONCE (defect-6 fix) ===
  -- Replaces the per-chunk STABLE call + per-loop Seq Scan of external_resources
  -- in the two workflow_python arms. Both the resource-side safety check
  -- (hivemind_workflow_python_state='safe') and kind='workflow' are evaluated
  -- here, once per workflow, instead of once per matched chunk.
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
       and (not v_has_channels or exists (
              select 1 from public.discord_channels dc
              where dc.channel_id = m.channel_id
                and dc.channel_name = any(coalesce(p_channels,'{}'::text[]))))
       and (not v_has_authors or exists (
              select 1 from public.members mb
              where mb.member_id = m.author_id
                and coalesce(mb.global_name, mb.username) = any(coalesce(p_authors,'{}'::text[]))))
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
       and (not v_has_channels or exists (
              select 1 from public.discord_channels dc where dc.channel_id = m.channel_id
                and dc.channel_name = any(coalesce(p_channels,'{}'::text[]))))
       and (not v_has_authors or exists (
              select 1 from public.members mb where mb.member_id = m.author_id
                and coalesce(mb.global_name, mb.username) = any(coalesce(p_authors,'{}'::text[]))))
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
    -- CHANGED (defect 6): join the ONCE-computed safe_wf instead of external_resources
    -- with a per-row STABLE call. Safety: ld columns (representation_type +
    -- quarantine_state='safe', guaranteed by schema/003 CHECK) AND safe_wf membership
    -- (kind='workflow' AND workflow_python_state='safe', computed once). Rank/source
    -- representation + matched_anchor unchanged.
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
    -- ====== RESOURCE: workflow_python bounded fragment arm (008 trigram index) ======
    -- CHANGED (defect 6): reduce trigram matches to DISTINCT item_ids in a MATERIALIZED
    -- subquery BEFORE joining safe_wf. Two effects: (1) collapses many-chunks-per-item
    -- to one row (exactly what the downstream `collapsed` CTE does anyway, so the final
    -- identity stream, ranks, and representation selection are unchanged); (2) gives the
    -- planner a real, small cardinality so it Hash-Joins to safe_wf instead of
    -- Nested-Looping per chunk (which re-scanned safe_wf once per matched chunk). The
    -- per-chunk STABLE call and per-loop Seq Scan of external_resources are gone. Safety
    -- is ld columns (representation_type + quarantine_state='safe', schema/003 CHECK) AND
    -- safe_wf (kind='workflow' AND workflow_python_state='safe', computed once). Guards
    -- (v_want_res, v_qn<>'') live INSIDE the subquery so it is constant-empty (no scan)
    -- when the fragment arm does not apply. Newest chunk's matched_anchor kept per item.
    select 'resource'::text, fm.item_id, 'workflow_python'::text, fm.matched_anchor,
           0.93::real, 'workflow_python_fragment'::text, sw.created_at
      from (
        select distinct on (ld.item_id) ld.item_id, ld.matched_anchor
          from public.lexical_documents ld
         where (v_want_res) and v_qn <> ''
           and ld.entity_type = 'resource'
           and ld.representation_type = 'workflow_python'
           and ld.quarantine_state = 'safe'
           and char_length(ld.chunk_text) between 1 and 8000
           and public.hivemind_normalize_identifier(ld.chunk_text) like '%' || v_qn || '%'
         order by ld.item_id, ld.created_at desc
      ) fm
      join safe_wf sw on sw.item_id = fm.item_id
     where (not v_has_sources or sw.source = any(coalesce(p_sources,'{}'::text[])))
       and (v_items_entity is distinct from 'resource' or fm.item_id = any(v_item_ids))

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
