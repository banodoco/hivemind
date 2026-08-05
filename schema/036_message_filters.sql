-- ============================================================
-- 036_message_filters.sql
-- Fast, filterable message surface — avoids the jsonb-path-filter
-- timeout on unified_feed's derived metadata.
--
-- unified_feed builds message metadata via jsonb_build_object + lateral
-- joins inside a UNION ALL view; PostgREST cannot push a filter on the
-- derived `metadata` to any index, so `metadata->>is_pinned=eq.true` /
-- `metadata->reference_id=eq.X` / `metadata->attachments@>{...}` scan the
-- 1.28M corpus and hit the anon role's 3s statement_timeout (HTTP 500
-- `code 57014`; verified NOT a data bug — jsonb_typeof(metadata) is object
-- on every row).
--
-- This adds a SIMPLE single-SELECT view (no UNION, no lateral, no
-- jsonb_build_object) so PostgREST filters push down to the indexed base
-- table, plus the indexes those filters need. `security_invoker = true`
-- keeps the discord_messages RLS (deleted-row hiding) in force for anon
-- reads (a plain view would otherwise run as the definer and bypass RLS —
-- Codex blocker).
--
-- Indexes are CREATE INDEX CONCURRENTLY (1.28M rows): must run outside a
-- transaction block (psql -f autocommits each statement, so OK).
-- reference_id is already btree-indexed on discord_messages — no duplicate.
-- ============================================================

create or replace view public.message_filters
with (security_invoker = true) as
  select message_id, channel_id, guild_id, author_id, thread_id, reference_id,
         is_pinned, reaction_count, attachments, embeds, content, created_at
    from public.discord_messages
   where is_deleted = false;

grant select on public.message_filters to anon, authenticated;

-- Filtering syntax (PostgREST):
--   is_pinned:    /message_filters?is_pinned=eq.true
--   thread:       /message_filters?thread_id=eq.X
--   reference:    /message_filters?reference_id=eq.X   (btree, pre-existing)
--   attachments:  /message_filters?attachments=cs.<urlencoded-json>   (contains, @>)
--   embeds:       /message_filters?embeds=cs.<urlencoded-json>
-- Full enriched rows: /unified_feed?item_id=eq.<id>  or get_item.

create index concurrently if not exists idx_discord_messages_is_pinned
  on public.discord_messages (is_pinned)
  where is_deleted = false and is_pinned;

-- thread_id is ALREADY btree-indexed (idx_discord_messages_thread_id) — not re-created.

create index concurrently if not exists idx_discord_messages_attachments_gin
  on public.discord_messages using gin (attachments jsonb_path_ops);

create index concurrently if not exists idx_discord_messages_embeds_gin
  on public.discord_messages using gin (embeds jsonb_path_ops);
