-- ============================================================
-- 012_lexical_latency_phase2.sql
-- Hivemind hybrid search — Phase 1, Task 1.10/1.11 phase-2 latency fix
--
-- Two recall-preserving ADDITIVE optimizations on top of schema/010:
--
--   OPTIMIZATION A — per-item workflow_python materialized view.
--     The workflow_python FRAGMENT arm (schema/010) scans ALL 15,265
--     lexical_documents workflow_python chunks via a normalized-trigram
--     bitmap, then distincts to ~135 items. This migration materializes the
--     per-item concatenated normalized code ONCE into a tiny (~221-row)
--     materialized view `lexical_workflow_python_search` and a GIN trigram
--     index over it. The fragment arm now scans 221 rows instead of 15,265
--     (a ~69x reduction), eliminating the dominant cost of the code-token
--     queries (LTX 2.8s, KSampler 1.16s, VAEDecode 1.03s, VACE 1.6s).
--
--     RECALL IS EXACT (proven byte-for-byte on the isolated cluster for
--     representative + adversarial queries, including a cross-boundary
--     needle and a query-specific anchor fixture): an item matches the
--     fragment arm iff ANY of its chunks' normalized text contains the
--     normalized needle. search_norm is the per-chunk NORMALIZED chunk_text,
--     space-joined; the LIKE is evaluated DIRECTLY on search_norm with the
--     already-normalized v_qn. See the CROSS-CHUNK + ANCHOR notes below for
--     why this is byte-equivalent to schema/010 and free of false positives.
--
--   OPTIMIZATION B — message-arm channel/author filters as direct predicates.
--     The two MESSAGE arms (FTS + ident) expressed channel/author filters as
--     correlated EXISTS subqueries against discord_channels / members. The
--     planner could not always merge these with the trigram/FTS GIN bitmap,
--     so for broad filtered cases (vae channel-scoped) it built a BitmapAnd
--     of the trigram bitmap AND the channel_id bitmap (~98k rows for one
--     channel), or fell back to a Nested Loop over the channel lookup. This
--     migration resolves the channel/author NAMES to id arrays ONCE at
--     function start and expresses the filters as DIRECT column predicates
--     `m.channel_id = any(<resolved>)` / `m.author_id = any(<resolved>)`.
--     The planner now uses the (already-existing) btree indexes only when
--     they help, and otherwise cheaply post-filters the GIN bitmap.
--     Read-only production proof: vae channel ident arm 148ms->14ms (10x),
--     sampler channel 74ms->33ms, with identical selected sets (88562==88562
--     messages for the channel, 435==435 for the author).
--
-- CROSS-CHUNK FALSE-POSITIVE SAFETY (correction vs the first draft).
--   The first draft stored `string_agg(normalize(chunk_text),' ')` and then
--   re-applied hivemind_normalize_identifier() to the WHOLE concatenation in
--   both the GIN index and the LIKE. Because normalize() strips ALL whitespace,
--   that re-normalization erased the inter-chunk space, so a needle present
--   ONLY across two chunk boundaries (chunk0 ends "...ksamp", chunk1 starts
--   "ler...", needle "ksampler") could match — a false positive schema/010
--   never produces (010 tests EACH chunk independently).
--   CORRECTION:
--     * search_norm stores the per-chunk NORMALIZED text, space-joined. We
--       index and search search_norm DIRECTLY — never re-normalizing the
--       concatenation. normalize() strips every whitespace char, so a space
--       never appears INSIDE any normalized chunk (it is a clean separator)
--       and v_qn = normalize(query) never contains a space, so a needle can
--       never span two chunks. Formally: `search_norm LIKE '%v_qn%'` iff
--       v_qn is a substring of some normalize(chunk_i) — exactly schema/010's
--       per-chunk containment. (Adversarial proof in the rehearsal: a needle
--       that exists only across a boundary does NOT match in 010 or 012.)
--     * The MV source keeps schema/010's `char_length(chunk_text) between 1
--       and 8000` predicate, so out-of-range chunks never reach search_norm
--       (an item cannot match the MV via a chunk 010's fragment arm ignores).
--
-- EXACT matched_anchor PARITY (correction vs the first draft).
--   schema/010's fragment arm selects, per item, the matched_anchor of the
--   NEWEST chunk that MATCHES the needle: `distinct on (item_id) matched_anchor
--   ... where normalize(chunk_text) like v_qn ... order by item_id, created_at
--   desc`. The first draft instead stored the FIRST chunk's anchor regardless
--   of which chunk matched — not equivalent.
--   CORRECTION: the fragment arm finds matching ITEMS via the tiny MV, then a
--   bounded scalar subquery re-selects the matched_anchor of the newest
--   MATCHING lexical_documents chunk for that item (same WHERE + `order by
--   created_at desc limit 1` as 010). It is indexed (the
--   lexical_documents_workflow_python_idx partial index serves the per-item
--   lookup) and bounded by the candidate-item count. The candidate row's
--   created_at is still the RESOURCE created_at (sw.created_at), as in 010.
--   (Anchor parity is proven byte-for-byte on a multi-chunk fixture where only
--   a later chunk matches, in both the isolated arm logic and the full stream.)
--
-- SAFETY / RECALL PRESERVED (no arm dropped, no gate relaxed):
--   * Both message arms keep EVERY existing predicate: is_deleted=false,
--     author_optout, bots_excluded, source, since, items_entity, item_ids.
--     Only the channel/author filter MECHANISM changes (EXISTS-on-name ->
--     column=any(resolved-ids)), and the resolved ids are EXACTLY those
--     rows whose name matches (proven: identical counts read-only).
--   * The fragment arm keeps its safety gate: it joins safe_wf (computed
--     once: kind='workflow' AND hivemind_workflow_python_state='safe') AND
--     the MV is itself built only from quarantine_state='safe' workflow_python
--     chunks (schema/003 CHECK guarantees workflow_python => safe; the MV's
--     WHERE re-confirms it). Quarantined workflows still never rank.
--   * Identical arm set, predicates, lexical_rank bands, collapse rule, and
--     deterministic ORDER BY/LIMIT as schema/010. The candidate identity
--     stream is unchanged; only the join strategy for the fragment arm and
--     the filter mechanism for the message arms change.
--   * No new btree indexes: idx_discord_messages_channel_id /
--     idx_discord_messages_author_id / idx_discord_messages_created_at
--     already exist on production (verified read-only). Nothing is added to
--     discord_messages.
--   * Post-limit hydration (schema/009 RPC) is untouched.
--
-- SECURITY (correction vs the first draft).
--   The MV is REVOKED from public/anon/authenticated (same posture as the
--   lexical tables in schema/011): it carries normalized code text and must
--   never be SELECT-able by public clients. The candidate function is SECURITY
--   INVOKER and is reached only via the SECURITY DEFINER RPC in 009, which
--   runs as its owner (postgres); the MV read therefore happens with the
--   owner's privileges regardless of the caller. schema/011's function/table
--   ACLs are preserved through this migration's CREATE OR REPLACE FUNCTION
--   (Postgres preserves proacl on REPLACE; proven in the rehearsal).
--
-- ADDITIVE + REVERSIBLE + IDEMPOTENT.
--   * The MV + its indexes are CREATE IF NOT EXISTS; re-running is a no-op
--     (the function is CREATE OR REPLACE; the REVOKE is idempotent).
--   * The MV is REFRESHed once at the end of this migration so it is populated
--     on apply (plain REFRESH — CONCURRENTLY cannot populate an empty MV).
--     Going forward the lexical refresh job REFRESHes it CONCURRENTLY
--     (deterministic plain-REFRESH fallback) alongside the lexical_documents
--     rebuild; the MV depends only on lexical_documents, so a REFRESH after a
--     documents rebuild is always consistent.
--   * ROLLBACK: DROP MATERIALIZED VIEW lexical_workflow_python_search;
--     then re-apply schema/010's function (CREATE OR REPLACE). No source row
--     is touched; the read path returns to its exact pre-012 state.
--
-- No source rows are read or mutated by the DDL. STABLE, security INVOKER
-- (called only by the SECURITY DEFINER RPC in 009).
-- ============================================================

-- ============================================================
-- OPTIMIZATION A: per-item workflow_python materialized view
-- ============================================================
-- One row per safe workflow resource that has in-range workflow_python
-- documents (~221 rows on production vs 15,265 chunks in lexical_documents).
-- search_norm is the per-chunk NORMALIZED chunk_text, space-joined and ordered
-- by chunk_index. It is indexed and searched DIRECTLY (see CROSS-CHUNK note):
-- because normalize() strips all whitespace, the space is a clean separator
-- that a normalized needle can never cross, so a single containment test over
-- search_norm is byte-equivalent to schema/010's "any chunk matches". The MV
-- is built only from quarantine_state='safe' rows with char_length in 1..8000
-- (the exact predicate 010's fragment arm uses), so the structural exclusion
-- of quarantined / out-of-range python is preserved at the source.
create materialized view if not exists public.lexical_workflow_python_search as
  select ld.item_id                                              as item_id,
         string_agg(public.hivemind_normalize_identifier(ld.chunk_text), ' '
                    order by ld.chunk_index)                     as search_norm
    from public.lexical_documents ld
   where ld.entity_type       = 'resource'
     and ld.representation_type = 'workflow_python'
     and ld.quarantine_state    = 'safe'
     and char_length(ld.chunk_text) between 1 and 8000
   group by ld.item_id;

comment on materialized view public.lexical_workflow_python_search is
  'Task 1.10/1.11 phase-2: per-item workflow_python search surface. One row '
  'per safe workflow resource (~221 rows). search_norm is the per-chunk '
  'NORMALIZED chunk_text, space-joined; it is indexed and searched DIRECTLY '
  '(never re-normalized), so a containment test is byte-equivalent to '
  'schema/010 per-chunk containment with NO cross-chunk false positives. Built '
  'only from quarantine_state=''safe'' rows with char_length in 1..8000. '
  'REVOKED from public/anon/authenticated (012); REFRESHed by the lexical '
  'refresh job.';

-- GIN trigram index over search_norm DIRECTLY (NOT normalize(search_norm)).
-- Re-normalizing search_norm would erase the inter-chunk space and reintroduce
-- cross-boundary false positives; searching the column directly preserves the
-- separator. Mirrors the schema/008 lexical_documents_python_chunk_trgm_idx
-- expression but over the per-item normalized concatenation in the tiny MV.
create index if not exists lexical_workflow_python_search_trgm_idx
  on public.lexical_workflow_python_search
  using gin (search_norm gin_trgm_ops);

-- A unique index makes REFRESH MATERIALIZED VIEW CONCURRENTLY possible (the
-- refresh job uses it) and is harmless for the read path.
create unique index if not exists lexical_workflow_python_search_item_uidx
  on public.lexical_workflow_python_search (item_id);

-- Owner + security posture: maintained by the service role (refresh job), read
-- only by the SECURITY DEFINER RPC in 009 (which runs as the owner). Public
-- clients never query it directly — REVOKED like the lexical tables in 011.
alter materialized view public.lexical_workflow_python_search owner to postgres;
revoke all on public.lexical_workflow_python_search from public, anon, authenticated;

-- ============================================================
-- THE candidate SQL — schema/010 body + BOTH optimizations
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

  -- === OPTIMIZATION B: resolve channel/author NAMES -> id arrays ONCE. ===
  -- The EXISTS-on-name filters become direct column=any(resolved) predicates,
  -- letting the planner BitmapAnd (or post-filter) with the existing btree
  -- indexes instead of a correlated EXISTS it cannot always merge. Resolution
  -- is null-safe: an unresolved name yields an empty array, and the predicate
  -- `col = any('{}')` is false (no rows), matching the EXISTS form's behavior
  -- for a name that matches nothing. NULL channel/author ids are dropped.
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
  -- Channel names -> channel_ids (discord_channels.channel_name).
  if v_has_channels then
    select array_agg(dc.channel_id) into v_channel_ids
      from public.discord_channels dc
     where dc.channel_name = any(coalesce(p_channels, '{}'::text[]));
  end if;
  -- Author display names -> member_ids (coalesce(global_name, username)).
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
    -- OPTIMIZATION B: channel/author filters are now DIRECT column predicates
    -- over the once-resolved id arrays (m.channel_id = any(v_channel_ids) /
    -- m.author_id = any(v_author_ids)) instead of correlated EXISTS subqueries.
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
    -- OPTIMIZATION B: same channel/author direct-predicate rewrite as the FTS arm.
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
    -- Unchanged from schema/010: the FTS arm stays over lexical_documents.tsv
    -- (it needs the per-chunk tsv for ts_rank; the MV is a fragment/containment
    -- surface, not an FTS surface). Safe via ld columns AND safe_wf membership.
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
    -- === OPTIMIZATION A: scan the per-item MV (lexical_workflow_python_search,
    -- ~221 rows) instead of ALL lexical_documents chunks (15,265) to find
    -- matching ITEMS. The GIN trigram index over the MV's search_norm serves the
    -- containment LIKE.
    -- CROSS-CHUNK SAFETY: we match search_norm DIRECTLY (`mv.search_norm like
    -- '%' || v_qn || '%'`) — NOT normalize(search_norm). search_norm is already
    -- the space-joined per-chunk NORMALIZED text, and v_qn is already normalized;
    -- because normalize() strips every whitespace char, a needle can never cross
    -- the inter-chunk space. This is byte-equivalent to schema/010's per-chunk
    -- `normalize(chunk_text) like v_qn` containment (adversarial proof: a needle
    -- that exists only across a boundary matches in neither 010 nor 012).
    -- ANCHOR PARITY: matched_snippet is a bounded scalar subquery that re-selects
    -- the matched_anchor of the NEWEST chunk matching the needle for this item —
    -- exactly schema/010's `distinct on (item_id) ... order by item_id,
    -- created_at desc` rule (NOT the first chunk's anchor). The partial index
    -- lexical_documents_workflow_python_idx serves the per-item lookup.
    -- created_at is the RESOURCE created_at (sw.created_at); rank band 0.93 and
    -- lexical_source are unchanged from schema/010. Safety: safe_wf membership
    -- AND the MV is built only from quarantine_state='safe' in-range chunks. ===
    select 'resource'::text, mv.item_id, 'workflow_python'::text,
           (select ld.matched_anchor
              from public.lexical_documents ld
             where ld.item_id = mv.item_id
               and ld.entity_type = 'resource'
               and ld.representation_type = 'workflow_python'
               and ld.quarantine_state = 'safe'
               and char_length(ld.chunk_text) between 1 and 8000
               and public.hivemind_normalize_identifier(ld.chunk_text) like '%' || v_qn || '%'
             order by ld.created_at desc
             limit 1) as matched_snippet,
           0.93::real, 'workflow_python_fragment'::text, sw.created_at
      from public.lexical_workflow_python_search mv
      join safe_wf sw on sw.item_id = mv.item_id
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
  'Task 1.10/1.11 phase-2: schema/010 body + (A) per-item workflow_python MV '
  'fragment arm (search_norm matched DIRECTLY — no cross-chunk false positives; '
  'newest-matching-chunk anchor via bounded scalar subquery, byte-equivalent to '
  '010) + (B) direct channel/author id predicates in the message arms. STABLE; '
  'called only by the SECURITY DEFINER RPC in schema/009.';

-- ============================================================
-- Populate the MV ONCE so the fragment arm has data on apply (plain REFRESH —
-- CONCURRENTLY cannot populate an empty MV). Going forward the lexical refresh
-- job REFRESHes it CONCURRENTLY (with a deterministic plain-REFRESH fallback)
-- alongside the lexical_documents rebuild. This runs OUTSIDE the function
-- (autocommit); REFRESH is non-transactional but safe to run via psql -f (each
-- statement autocommits).
-- ============================================================
refresh materialized view public.lexical_workflow_python_search;
