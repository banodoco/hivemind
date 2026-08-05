-- ============================================================
-- 008_lexical_candidate_sql.sql
-- Hivemind hybrid search — Phase 1, Task 1.7
--
-- THE canonical lexical candidate SQL: one duplicate-free,
-- deterministic, ranked identity stream that combines the FTS, phrase,
-- exact-identifier (task-1.6 containment), and bounded workflow-code
-- fragment arms across messages, resources (prose + workflow_python),
-- and distillations.
--
-- This is the single source of truth for the candidate query. The
-- hardened RPC (schema/009, task 1.9), the cluster rehearsal/EXPLAIN
-- evidence (task 1.10), the offline lexical eval adapter, and the live
-- driver ALL consume this ONE function — there is no second, slightly
-- different copy of the candidate SQL in tests, RPC, or drivers.
--
-- Frozen contracts consumed (do not reinterpret here):
--   * executors/lexical_contract.py — regconfig 'simple', weighted specs,
--     websearch/phraseto query arms, ts_rank normalization 32, eligibility
--     predicates, chunk-collapse rule, snowflake-as-text boundary.
--   * executors/message_identifier_index.py (task 1.6) — the exact-
--     identifier arm is INDEX-SUPPORTED normalized CONTAINMENT (the
--     corrected v3 contract), never whole-body equality and never an
--     unbounded <% fuzzy scan. Its candidate_limit() is mirrored here.
--   * schema/003 (resource/distillation/workflow-python tsvectors +
--     lexical_documents), schema/004 (message 'simple' FTS index),
--     schema/005 (hivemind_normalize_identifier + ICU collation),
--     schema/006 (short-field normalized trigram), schema/007 (message
--     identifier containment index).
--
-- Additive + idempotent. STABLE, security INVOKER (it is called only by
-- the SECURITY DEFINER RPC in 009, which sets a trusted search_path and
-- encodes eligibility). No source rows are read or mutated by this
-- migration; it creates one function, one bounded index, and comments.
-- ============================================================

create extension if not exists pg_trgm;

-- ------------------------------------------------------------
-- 0. Bounded normalized-trigram index for the workflow-code
--    fragment arm. The primary workflow-code arm is the FTS arm
--    over lexical_documents.tsv (schema/003 index); this fragment
--    index serves the bounded normalized-containment subsequence
--    path for node-class / symbol fragments that 'simple' FTS
--    tokenizes away. Length-bounded partial so it is BOUNDED by
--    construction (excludes pathological huge chunks). Quarantined
--    workflow_python is structurally absent from lexical_documents
--    (schema/003 CHECK), so it cannot appear here regardless.
-- ------------------------------------------------------------
create index concurrently if not exists lexical_documents_python_chunk_trgm_idx
  on public.lexical_documents
  using gin (hivemind_normalize_identifier(chunk_text) gin_trgm_ops)
  where char_length(chunk_text) between 1 and 8000;

comment on index public.lexical_documents_python_chunk_trgm_idx is
  'Task 1.7: bounded normalized-trigram index for the workflow-code fragment '
  'arm (length-bounded partial). Quarantined workflow_python is structurally '
  'absent from lexical_documents (schema/003 CHECK). Serves the bounded '
  'containment subsequence path that complements the FTS arm.';

-- ============================================================
-- THE candidate SQL — one ranked identity stream, no duplicate items.
-- ============================================================
-- Returns at most p_candidate_limit identities, each appearing exactly
-- once, carrying the matched representation + secret-safe snippet and a
-- frozen deterministic global order. The RPC (009) applies the final
-- (smaller) global limit AFTER this stream and hydrates only those rows.
--
-- Per-arm lexical_rank bands (a single comparable score per identity so a
-- global ORDER BY is well-defined). A DEFINITIONAL hit (the item's title,
-- question, or CODE contains the queried identifier) is a stronger signal than
-- a passing message-prose mention, so title/question/code bands sit above
-- message containment:
--   * exact identifier whole-body equality .......... 1.00 (task-1.6 'exact')
--   * title / question exact-name containment ....... 0.95 (schema/006 family)
--   * workflow_python code-fragment containment ..... 0.93 (this migration)
--   * message identifier containment ................. 0.90 (task-1.6 primary)
--   * FTS ts_rank(tsv, tsq, 32) ...................... its native value
-- A matched identity keeps the MAX score across its arms, and the arm that
-- produced it (representation_type + snippet) is carried. For an exact-name
-- query the workflow that DEFINES the node therefore outranks the messages
-- that merely mention it; FTS ts_rank orders multi-term relevance. This mirrors
-- Pumpernickel's "one lexical rank" model (ported) with Hivemind bands added.
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
  -- Frozen query arms (mirror executors.lexical_contract.query_arms + the
  -- task-1.6 candidate contract). Built once, reused across all entity arms.
  v_qn        text := public.hivemind_normalize_identifier(p_query);  -- ident needle
  v_fts       tsquery := websearch_to_tsquery('simple'::regconfig, coalesce(p_query, ''));
  v_phrase    tsquery;                                                  -- single-name / quoted only
  v_is_single boolean;                                                  -- single-token name shape
  v_limit     int  := greatest(coalesce(p_candidate_limit, 100), 1);

  -- Kind resolution (AD-1 alias): 'workflow' and concrete resource kinds -> resource.
  v_kinds     text[] := coalesce(p_kinds, '{}'::text[]);
  v_has_kinds boolean := coalesce(array_length(v_kinds, 1), 0) > 0;
  v_want_msg      boolean;
  v_want_res      boolean;
  v_want_dist     boolean;

  -- item_ids: a bounded allow-listed identity filter (AD-1). Requires exactly one
  -- kind so each id is unambiguous; the RPC rejects ambiguity (1.9) and re-derives
  -- the single entity here. Never interpolated SQL.
  v_item_ids text[] := coalesce(p_item_ids, '{}'::text[]);
  v_has_items boolean := coalesce(array_length(v_item_ids, 1), 0) > 0;
  v_items_entity text;   -- the single entity an item_ids filter restricts to

  -- Filter-presence flags (cardinality() is null-safe on text[]).
  v_has_sources  boolean := coalesce(array_length(coalesce(p_sources,  '{}'::text[]), 1), 0) > 0;
  v_has_channels boolean := coalesce(array_length(coalesce(p_channels, '{}'::text[]), 1), 0) > 0;
  v_has_authors  boolean := coalesce(array_length(coalesce(p_authors,  '{}'::text[]), 1), 0) > 0;
begin
  -- ------------------------------------------------------------------
  -- 1. Resolve kinds -> entities. Concrete resource kinds collapse to the
  --    resource entity (entity_identity / AD-1). An empty kinds filter means
  --    "all entities".
  -- ------------------------------------------------------------------
  v_want_msg  := (not v_has_kinds) or v_kinds && array['message'];
  v_want_dist := (not v_has_kinds) or v_kinds && array['distillation'];
  -- 'resource' OR 'workflow' OR any known concrete resource kind => resource.
  v_want_res  := (not v_has_kinds) or exists (
    select 1 from unnest(v_kinds) k
    where k in ('resource','workflow','article','blog_post','transcript','repo','guide','doc')
       or k not in ('message','distillation')  -- any other kind is a resource sub-kind
  );

  -- ------------------------------------------------------------------
  -- 2. item_ids -> the one entity they restrict to (AD-1 unambiguous rule).
  --    If item_ids is present but kinds does not resolve to exactly one
  --    entity, collapse to no arms (the RPC has already rejected this case;
  --    the function is defense-in-depth and returns nothing rather than guess).
  -- ------------------------------------------------------------------
  if v_has_items then
    if v_want_msg and not v_want_res and not v_want_dist then
      v_items_entity := 'message';
    elsif v_want_res and not v_want_msg and not v_want_dist then
      v_items_entity := 'resource';
    elsif v_want_dist and not v_want_msg and not v_want_res then
      v_items_entity := 'distillation';
    else
      v_items_entity := '';  -- ambiguous -> refuse to guess
    end if;
  end if;

  -- ------------------------------------------------------------------
  -- 3. Phrase arm: fire only for a single-token name or a whole "quoted"
  --    phrase (mirror lexical_contract.query_arms). phraseto_tsquery on a
  --    full natural-language question would over-constrain, so it is gated.
  -- ------------------------------------------------------------------
  v_is_single := coalesce(btrim(p_query), '') <> ''
                 and position(' ' in btrim(p_query)) = 0
                 and p_query not like '%"%' and p_query not like '%-%';
  if v_is_single or (p_query like '"%"' ) then
    v_phrase := phraseto_tsquery('simple'::regconfig, coalesce(p_query, ''));
  else
    v_phrase := null::tsquery;
  end if;

  -- ------------------------------------------------------------------
  -- 4. Emit every arm (UNION ALL), then collapse to one identity keeping
  --    the best rank + its representation/snippet, then order + limit.
  --    Filters (kind/source/date/channel/author/item_ids) are applied INSIDE
  --    each arm's WHERE (pre-ranking) so we never score filtered-out rows.
  --    Eligibility (deletion / opt-out / distillation status / workflow
  --    quarantine) is encoded here because the service role bypasses RLS.
  -- ------------------------------------------------------------------
  return query
  with arms as (
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
    -- The corrected v3 contract: index-supported normalized CONTAINMENT, with
    -- whole-body equality (1.0) ranked ahead of containment (0.9). Repeats the
    -- 007 partial predicate verbatim so the GIN index is usable. No <% fuzzy scan.
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
    -- Normalized containment over the title; repeats the 006 partial predicate.
    -- A title/question/code DEFINITIONAL hit is a stronger signal than a passing
    -- message-prose mention, so it ranks in a band above message containment.
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
    -- Safe workflow code chunks only (quarantined python is structurally absent,
    -- schema/003). Collapses to the resource identity; carries the redacted anchor.
    select 'resource'::text, ld.item_id, 'workflow_python'::text, ld.matched_anchor,
           ts_rank(ld.tsv, v_fts, 32), 'workflow_python_fts'::text, r.created_at
      from public.lexical_documents ld
      join public.external_resources r on r.id::text = ld.item_id
     where (v_want_res)
       and ld.entity_type = 'resource'
       and ld.representation_type = 'workflow_python'
       and ld.quarantine_state = 'safe'
       and v_fts is not null and v_fts <> to_tsquery('simple'::regconfig, '')
       and ld.tsv @@ v_fts
       and public.hivemind_workflow_python_state(r.id) = 'safe'
       and r.kind = 'workflow'
       and (not v_has_sources or r.source = any(coalesce(p_sources,'{}'::text[])))
       and (v_items_entity is distinct from 'resource' or r.id::text = any(v_item_ids))

    union all
    -- ====== RESOURCE: workflow_python bounded fragment arm (this migration's index) ======
    -- Bounded normalized containment over safe code chunks for subsequence fragments
    -- 'simple' FTS tokenizes away (common-subword / punctuation-bearing node names).
    select 'resource'::text, ld.item_id, 'workflow_python'::text, ld.matched_anchor,
           0.93::real, 'workflow_python_fragment'::text, r.created_at
      from public.lexical_documents ld
      join public.external_resources r on r.id::text = ld.item_id
     where (v_want_res) and v_qn <> ''
       and ld.entity_type = 'resource'
       and ld.representation_type = 'workflow_python'
       and ld.quarantine_state = 'safe'
       and char_length(ld.chunk_text) between 1 and 8000
       and public.hivemind_normalize_identifier(ld.chunk_text) like '%' || v_qn || '%'
       and public.hivemind_workflow_python_state(r.id) = 'safe'
       and r.kind = 'workflow'
       and (not v_has_sources or r.source = any(coalesce(p_sources,'{}'::text[])))
       and (v_items_entity is distinct from 'resource' or r.id::text = any(v_item_ids))

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
  -- Collapse: exactly one row per (entity_type, item_id). Keep the best lexical_rank
  -- and, among equal ranks, prefer workflow_python representation (a code hit is the
  -- more specific evidence), then newer, then stable identity. (Chunk-collapse rule.)
  collapsed as (
    -- Qualify every column with the CTE name: this function's RETURNS TABLE
    -- columns are also PL/pgSQL OUT-parameter variables, so bare names would
    -- be ambiguous. (Chunk-collapse rule: one row per identity.)
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
  'Task 1.7: THE canonical lexical candidate SQL. One duplicate-free deterministic '
  'ranked identity stream combining the FTS, phrase, exact-identifier (task-1.6 '
  'containment), and bounded workflow-code fragment arms across messages, resources '
  '(prose + workflow_python), and distillations. STABLE; called only by the '
  'SECURITY DEFINER RPC in schema/009. Snowflake item_ids are text throughout.';
