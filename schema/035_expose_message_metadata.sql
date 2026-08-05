-- ============================================================
-- 035_expose_message_metadata.sql
-- Hivemind — expose the full Discord message envelope on the public
-- search surface, and stop surfacing deleted messages.
--
-- Two additive, idempotent overrides (the established schema/034 pattern):
--
--   1. unified_feed (schema/001) — the MESSAGE branch now carries
--      channel_id / reactions / guild_id / author_id / reference_id /
--      thread_id / message_type / edited_at / is_pinned / reaction_count /
--      embeds / channel_type / avatar_url in `metadata`, sourced by lateral
--      joins into discord_messages / discord_channels / members. Messages
--      with is_deleted = true are filtered out of the view entirely.
--
--   2. hivemind_lexical_search (schema/009) — mirrors the SAME metadata
--      shape + deletion filter so RPC results match the feed row-for-row.
--
-- Why this lives here (not in message_feed):
--   * message_feed is an external view (Discord archive; source NOT in this
--     repo) projecting only 8 columns with NO is_deleted filter. The extra
--     fields + the filter therefore ride in these overrides via lateral
--     joins keyed on message_id.
--
-- Deletion coverage: every other read path already filters is_deleted
-- (candidate SQL 008/010/012/013, semantic 032/033, embedding write path
-- 025/029/034). This closes the last two surfaces that leaked deleted
-- messages: unified_feed (get_item / search) and the lexical RPC hydration.
--
-- Backwards compatibility: metadata additions are additive-only —
-- channel_id + reactions keep their exact positions, new keys append.
-- Message representation_hash is content-only (executors/canonical_
-- representations.py + schema/034 mirror), so no embedding/hash is
-- invalidated, and no consumer asserts the exact {channel_id, reactions}
-- shape (verified in the phase0 read-path map).
--
-- Rehearsal harness updated to match: scripts/rehearse_lexical_candidate.py
-- BASE_DDL now carries the full discord_messages / members column shape.
-- ============================================================

create or replace view unified_feed
with (security_invoker = true) as
  select
    'message'                       as kind,
    'banodoco-discord'              as source,
    m.message_id::text              as item_id,
    null::text                      as title,
    m.content                       as body,
    m.author_name                   as author,
    m.channel_name                  as context,
    'https://discord.com/channels/' || m.guild_id || '/' || m.channel_id || '/' || m.message_id
                                    as url,
    jsonb_build_object(
      'channel_id',     m.channel_id,
      'reactions',      m.reactions,
      'guild_id',       m.guild_id,
      'author_id',      d.author_id,
      'reference_id',   d.reference_id,
      'thread_id',      d.thread_id,
      'message_type',   d.message_type,
      'edited_at',      d.edited_at,
      'is_pinned',      d.is_pinned,
      'reaction_count', d.reaction_count,
      'embeds',         d.embeds,
      'channel_type',   cc.channel_type,
      'avatar_url',     mm.avatar_url
    )                                   as metadata,
    m.created_at
  from message_feed m
  left join lateral (
    select dm.embeds, dm.reaction_count, dm.edited_at, dm.is_pinned,
           dm.thread_id, dm.message_type, dm.author_id, dm.reference_id,
           dm.is_deleted
      from discord_messages dm
     where dm.message_id = m.message_id
  ) d on true
  left join lateral (
    select dc.channel_type
      from discord_channels dc
     where dc.channel_id = m.channel_id
  ) cc on true
  left join lateral (
    select me.avatar_url
      from members me
     where me.member_id = d.author_id
  ) mm on true
  where coalesce(d.is_deleted, false) = false
union all
  select
    kind,
    source,
    id::text,
    title,
    body,
    author,
    null::text                      as context,
    url,
    metadata,
    created_at
  from external_resources
union all
  select
    'distillation'                  as kind,
    'hivemind'                      as source,
    id::text                        as item_id,
    question                        as title,
    answer                          as body,
    null::text                      as author,   -- author resolved via get-item, not exposed in feed
    conditions                      as context,
    null::text                      as url,
    jsonb_build_object('status', status, 'confidence', confidence)
                                    as metadata,
    created_at
  from distillations
  where status in ('pending', 'approved');


-- ============================================================
-- 009_lexical_search_rpc.sql
-- Hivemind hybrid search — Phase 1, Tasks 1.8 + 1.9
--
-- The hardened lexical search RPC. A narrowly scoped SECURITY DEFINER
-- function that is the ONLY server-side read path over the candidate SQL
-- (schema/008, task 1.7). It:
--   * validates and bounds every input (query length, limit, arrays, mode);
--   * rejects ambiguous item_ids rather than guessing (AD-1);
--   * applies the global limit AFTER the candidate stream and BEFORE
--     hydration (plan "hydrate afterward");
--   * hydrates only the surviving identities into the public unified_feed
--     row shape, carrying the matched representation + secret-safe snippet;
--   * encodes corpus eligibility (deletion, opt-out, distillation status,
--     workflow quarantine) inside the candidate SQL, because the service
--     role bypasses RLS;
--   * sets a trusted fixed search_path and revokes direct execution from
--     anon / authenticated / public so anon MUST NOT run it directly.
--
-- The Edge Function (Phase 3) calls this with the server-side service role;
-- the public pack never touches it. Read-only: it does not change any row.
--
-- Additive + idempotent. No source rows are mutated.
-- ============================================================

-- ------------------------------------------------------------
-- Frozen bounds (mirror the Edge validation in task 3.1; encoded in SQL
-- so they hold even if Edge validation is bypassed).
-- ------------------------------------------------------------
--   MAX_QUERY_LEN      2000 chars
--   MAX_LIMIT          100 results
--   CANDIDATE_MULT     5    CANDIDATE_CAP 500   (task 1.6 candidate_limit())
--   array caps         kinds/sources/channels/authors <= 64 ; item_ids <= 256
--   statement backstop 2000 ms  (hard safety bound; the indexed path targets
--                               warm p95 <= 750 ms — task 1.11 latency gate)

create or replace function public.hivemind_lexical_search(
  p_query    text,
  p_limit    int          default 20,
  p_kinds    text[]       default '{}',
  p_sources  text[]       default '{}',
  p_item_ids text[]       default '{}',
  p_since    timestamptz  default null,
  p_channels text[]       default '{}',
  p_authors  text[]       default '{}',
  p_mode     text         default 'lexical'
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_query    text := btrim(coalesce(p_query, ''));
  v_limit    int;
  v_cand     int;
  v_kinds    text[] := coalesce(p_kinds,   '{}'::text[]);
  v_sources  text[] := coalesce(p_sources, '{}'::text[]);
  v_items    text[] := coalesce(p_item_ids,'{}'::text[]);
  v_channels text[] := coalesce(p_channels,'{}'::text[]);
  v_authors  text[] := coalesce(p_authors, '{}'::text[]);
  v_items_entity text;
  v_n_items_entity int;
  v_results jsonb;
begin
  -- Hard statement backstop so a runaway query fails fast instead of the
  -- legacy 30s hang. SET LOCAL scopes it to this transaction.
  set local statement_timeout = '2000ms';

  -- ----------------------------------------------------------------
  -- 1. Validate + bound inputs. Fail predictably BEFORE any retrieval.
  -- ----------------------------------------------------------------
  if v_query = '' then
    raise exception 'query must be non-empty';
  end if;
  if char_length(v_query) > 2000 then
    raise exception 'query too long (max 2000 chars)';
  end if;
  if p_mode is null or p_mode <> 'lexical' then
    raise exception 'mode must be ''lexical'' for this RPC';
  end if;

  v_limit := least(greatest(coalesce(p_limit, 20), 1), 100);
  v_cand  := least(5 * v_limit, 500);   -- mirror task-1.6 candidate_limit()

  if coalesce(array_length(v_kinds,    1), 0) > 64 or
     coalesce(array_length(v_sources,  1), 0) > 64 or
     coalesce(array_length(v_channels, 1), 0) > 64 or
     coalesce(array_length(v_authors,  1), 0) > 64 then
    raise exception 'filter array exceeds 64 entries';
  end if;
  if coalesce(array_length(v_items, 1), 0) > 256 then
    raise exception 'item_ids exceeds 256 entries';
  end if;

  -- item_ids require exactly one compatible kind so every id is unambiguous
  -- (AD-1). Resolve the single entity the kinds filter implies and reject
  -- anything cross-kind / bare. Never guess.
  if coalesce(array_length(v_items, 1), 0) > 0 then
    select count(distinct e) into v_n_items_entity
    from (
      select case
        when k in ('message') then 'message'
        when k in ('distillation') then 'distillation'
        else 'resource'   -- resource / workflow / article / ... all -> resource
      end as e
      from unnest(v_kinds) k
    ) s;
    if v_n_items_entity is null then
      raise exception 'item_ids require exactly one kinds value (none given)';
    end if;
    if v_n_items_entity > 1 then
      raise exception 'ambiguous item_ids: kinds span more than one entity (AD-1)';
    end if;
  end if;

  -- ----------------------------------------------------------------
  -- 2. Candidate stream (schema/008) -> global limit -> hydrate.
  --    Hydration happens ONLY over the surviving top-v_limit identities,
  --    joining back to the source tables to build the unified_feed shape.
  --    The matched representation + redacted snippet are carried additively.
  -- ----------------------------------------------------------------
  with cand as (
    select entity_type, item_id, representation_type, matched_snippet,
           lexical_rank, lexical_source, created_at
      from public.hivemind_lexical_candidates(
             p_query := v_query,
             p_candidate_limit := v_cand,
             p_kinds := v_kinds,
             p_sources := v_sources,
             p_item_ids := v_items,
             p_since := p_since,
             p_channels := v_channels,
             p_authors := v_authors
           )
  ),
  top as (
    -- Global limit applied here, BEFORE hydration (plan "hydrate afterward").
    select * from cand limit v_limit
  ),
  hydrated as (
    select
      t.entity_type,
      t.item_id,
      t.representation_type,
      t.matched_snippet,
      t.lexical_rank,
      t.lexical_source,
      h.kind,
      h.source,
      h.title,
      h.body,
      h.author,
      h.context,
      h.url,
      h.metadata,
      h.created_at,
      row_number() over (order by t.lexical_rank desc nulls last,
                               t.created_at desc nulls last,
                               t.entity_type asc, t.item_id asc) as keyword_rank
    from top t
    left join lateral (
      -- MESSAGE branch (mirrors unified_feed exactly; schema/001 as overridden
      -- by schema/035). New metadata keys ride alongside the original
      -- channel_id/reactions; deleted messages are re-filtered here.
      (select 'message'::text         as kind,
             'banodoco-discord'::text as source,
             m.message_id::text       as item_id,
             null::text               as title,
             m.content                as body,
             m.author_name            as author,
             m.channel_name           as context,
             ('https://discord.com/channels/' || m.guild_id || '/' || m.channel_id
              || '/' || m.message_id) as url,
             jsonb_build_object(
               'channel_id',     m.channel_id,
               'reactions',      m.reactions,
               'guild_id',       m.guild_id,
               'author_id',      d.author_id,
               'reference_id',   d.reference_id,
               'thread_id',      d.thread_id,
               'message_type',   d.message_type,
               'edited_at',      d.edited_at,
               'is_pinned',      d.is_pinned,
               'reaction_count', d.reaction_count,
               'embeds',         d.embeds,
               'channel_type',   cc.channel_type,
               'avatar_url',     mm.avatar_url
             )                              as metadata,
             m.created_at
        from public.message_feed m
        left join lateral (
          select dm.embeds, dm.reaction_count, dm.edited_at, dm.is_pinned,
                 dm.thread_id, dm.message_type, dm.author_id, dm.reference_id,
                 dm.is_deleted
            from public.discord_messages dm
           where dm.message_id = m.message_id
        ) d on true
        left join lateral (
          select dc.channel_type
            from public.discord_channels dc
           where dc.channel_id = m.channel_id
        ) cc on true
        left join lateral (
          select me.avatar_url
            from public.members me
           where me.member_id = d.author_id
        ) mm on true
       where t.entity_type = 'message'
         and m.message_id::text = t.item_id
         and coalesce(d.is_deleted, false) = false
       limit 1)
      union all
      -- RESOURCE branch (concrete kind preserved; e.g. 'workflow')
      (select r.kind,
             r.source,
             r.id::text,
             r.title,
             r.body,
             r.author,
             null::text,
             r.url,
             r.metadata,
             r.created_at
        from public.external_resources r
       where t.entity_type = 'resource'
         and r.id::text = t.item_id
       limit 1)
      union all
      -- DISTILLATION branch
      (select 'distillation'::text,
             'hivemind'::text,
             d.id::text,
             d.question,
             d.answer,
             null::text,
             d.conditions,
             null::text,
             jsonb_build_object('status', d.status, 'confidence', d.confidence),
             d.created_at
        from public.distillations d
       where t.entity_type = 'distillation'
         and d.id::text = t.item_id
         and d.status in ('pending','approved')
       limit 1)
    ) h on true
  )
  select coalesce(jsonb_agg(jsonb_build_object(
    'kind',        hydrated.kind,
    'source',      hydrated.source,
    'item_id',     hydrated.item_id,
    'title',       hydrated.title,
    'body',        hydrated.body,
    'author',      hydrated.author,
    'context',     hydrated.context,
    'url',         hydrated.url,
    'metadata',    hydrated.metadata,
    'created_at',  to_char(hydrated.created_at at time zone 'UTC',
                           'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'),
    'match_type',  case when hydrated.entity_type is not null then 'keyword'
                        else null end,
    'keyword_rank', hydrated.keyword_rank,
    'rrf_score',   hydrated.lexical_rank,
    'matched_representation', hydrated.representation_type,
    'matched_snippet', hydrated.matched_snippet
  ) order by hydrated.lexical_rank desc nulls last,
               hydrated.created_at desc nulls last,
               hydrated.entity_type asc, hydrated.item_id asc), '[]'::jsonb)
    into v_results
  from hydrated;

  return jsonb_build_object(
    'results', v_results,
    'count',   coalesce(jsonb_array_length(v_results), 0),
    'meta',    jsonb_build_object(
                 'mode_requested', p_mode,
                 'mode_used',      'lexical',
                 'semantic_degraded', false,
                 'limit',          v_limit,
                 'candidate_limit', v_cand
               )
  );
end;
$$;

comment on function public.hivemind_lexical_search(text,int,text[],text[],text[],timestamptz,text[],text[],text) is
  'Tasks 1.8/1.9: hardened lexical search RPC. SECURITY DEFINER with a fixed '
  'trusted search_path; the only server-side read path over the canonical '
  'candidate SQL (schema/008). Bounded inputs, ambiguous-item-id rejection, '
  'post-limit hydration into the unified_feed shape, eligibility encoded in SQL. '
  'Read-only. Anon/authenticated must not execute it directly (grants below).';

-- ------------------------------------------------------------
-- Trust boundary: revoke direct execution from everyone, then grant ONLY
-- the service-role path the Edge Function uses. Anon cannot call the RPC.
-- (Supabase SECURITY DEFINER funcs default to PUBLIC-executable; this undoes
--  that and narrows to the service role.)
-- ------------------------------------------------------------
revoke execute on function public.hivemind_lexical_search(text,int,text[],text[],text[],timestamptz,text[],text[],text)
  from public, anon, authenticated;
grant  execute on function public.hivemind_lexical_search(text,int,text[],text[],text[],timestamptz,text[],text[],text)
  to service_role;
