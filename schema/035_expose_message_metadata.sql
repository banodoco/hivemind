-- ============================================================
-- 035_expose_message_metadata.sql
-- Hivemind — expose the full Discord message envelope on the public
-- search surface, and stop surfacing deleted messages anywhere.
--
-- Five additive, idempotent overrides:
--
--   1. RLS on discord_messages        — public SELECT now filters
--      is_deleted = true at the SOURCE. Direct PostgREST reads of
--      discord_messages (a public surface) can no longer return deleted
--      messages. service_role bypasses RLS, so the write path is untouched.
--
--   2. message_feed (external view)   — redefined in-repo with the exact
--      live shape (phase0-schema-eligibility-map.md) plus
--      ``WHERE m.is_deleted = false``. message_feed is the documented
--      raw-message public surface; it previously had NO deletion filter.
--
--   3. unified_feed (schema/001)      — the MESSAGE branch carries the full
--      envelope in `metadata` (original channel_id/reactions preserved;
--      new guild_id / author_id / reference_id / thread_id / message_type /
--      edited_at / is_pinned / reaction_count / embeds / channel_type /
--      avatar_url) sourced by lateral joins into discord_messages /
--      discord_channels / members, and filters deleted messages.
--      Discord snowflakes are STRINGIFIED (JS-safe > 2^53).
--
--   4. hivemind_lexical_search (009)  — mirrors the same metadata shape +
--      deletion filter so RPC results match the feed row-for-row, and drops
--      phantom rows if a candidate disappears between rank and hydrate.
--
--   5. content_embeddings guard       — when the embedding migrations deploy,
--      revoke anon/authenticated access so message chunk_text (incl. deleted
--      messages pre-cleanup) is not publicly readable. No-op while absent.
--
-- Deletion coverage: candidate SQL (008/010/012/013), semantic (032/033),
-- and the embedding write path (025/029/034) already filter is_deleted.
-- This closes every remaining public surface: discord_messages (RLS),
-- message_feed (view), unified_feed (view), and the lexical RPC.
--
-- Backwards compatibility: metadata additions are additive; channel_id +
-- reactions keep their positions. representation_hash is content-only, so no
-- embedding/hash is invalidated, and no consumer asserts the exact old shape.
--
-- Rehearsal harness (scripts/rehearse_lexical_candidate.py) BASE_DDL now
-- mirrors the full live discord_messages / members / discord_reactions shape.
-- ============================================================


-- Hide deleted messages from public reads at the source (discord_messages is
-- directly readable via PostgREST; message_feed reads through it). service_role
-- bypasses RLS, so the write path is unaffected. Idempotent drop+create.
drop policy if exists "Allow public read access to messages" on public.discord_messages;
do $$
begin
  if not exists (
    select 1 from pg_policy pol
    join pg_class c on c.oid = pol.polrelid
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relname = 'discord_messages'
      and pol.polname = 'hivemind public read non-deleted messages'
  ) then
    create policy "hivemind public read non-deleted messages" on public.discord_messages
      for select to public using (coalesce(is_deleted, false) = false);
  end if;
end $$;

-- content_embeddings stores message chunk_text (incl. deleted messages until
-- the async cleanup runs). Supabase default privileges grant new public-schema
-- tables to anon/authenticated, so when the embedding migrations (020-034)
-- deploy, close that surface preemptively. service_role keeps its grant for the
-- worker/backfill/search path. Guarded: no-op on DBs where the table is absent.
do $$
begin
  if to_regclass('public.content_embeddings') is not null then
    revoke all on table public.content_embeddings from anon, authenticated;
  end if;
end $$;

create or replace view public.message_feed as
  select m.message_id, m.content, m.created_at,
         coalesce(a.global_name, a.username) as author_name,
         c.channel_name, m.channel_id, m.guild_id,
         (select json_agg(json_build_object('emoji', r.emoji,
                   'reactor', coalesce(rm.global_name, rm.username)))
            from discord_reactions r
            left join members rm on rm.member_id = r.user_id
           where r.message_id = m.message_id and r.removed_at is null) as reactions
    from discord_messages m
    left join members a        on a.member_id  = m.author_id
    left join discord_channels c on c.channel_id = m.channel_id
   where m.is_deleted = false;

-- Make the RLS-on-discord_messages fallback explicit: even if the external
-- archive later replaces message_feed with an UNFILTERED definition, running
-- it as security_invoker means anon reads still hit the discord_messages RLS
-- policy (which hides deleted rows at the source).
alter view public.message_feed set (security_invoker = true);

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
               'guild_id',       m.guild_id::text,
               'author_id',      d.author_id::text,
               'reference_id',   d.reference_id::text,
               'thread_id',      d.thread_id::text,
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
  where d.is_deleted = false
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
      -- by schema/035). Discord ids are stringified at the JSON boundary
      -- (snowflakes exceed 2^53 — JS number rounding). Deleted messages are
      -- re-filtered here so a race can't hydrate a just-deleted message.
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
               'guild_id',       m.guild_id::text,
               'author_id',      d.author_id::text,
               'reference_id',   d.reference_id::text,
               'thread_id',      d.thread_id::text,
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
         and d.is_deleted = false
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
  from hydrated
 where hydrated.kind is not null;

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
