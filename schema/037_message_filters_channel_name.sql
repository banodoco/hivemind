-- ============================================================
-- 037_message_filters_channel_name.sql
-- Add channel_name to message_filters.
--
-- Agent feedback (2026-08-05 mission tests): mapping a forum/thread by name
-- required a one-hop channel_id resolve through unified_feed. Adding the
-- channel_name column makes it zero-hop:
--
--   GET /rest/v1/message_filters?channel_name=eq.minimax_h3_resources
--
-- Implemented as a LEFT JOIN to discord_channels (1:1 by PK). Simple join view
-- keeps predicate pushdown: filters on the discord_messages columns still hit
-- the indexed base (partial is_pinned, GIN attachments/embeds, btree
-- reference_id/thread_id), and a channel_name filter pushes to the tiny
-- discord_channels side (1,258 rows — no index needed). CREATE OR REPLACE VIEW
-- rule: existing columns keep name/type/order; the new column is appended at
-- the END. Messages whose channel is missing from discord_channels get
-- channel_name null (LEFT JOIN) — same as before for the other columns.
-- ============================================================

create or replace view public.message_filters
with (security_invoker = true) as
  select m.message_id, m.channel_id, m.guild_id, m.author_id,
         m.thread_id, m.reference_id, m.is_pinned, m.reaction_count,
         m.attachments, m.embeds, m.content, m.created_at, c.channel_name
    from public.discord_messages m
    left join public.discord_channels c on c.channel_id = m.channel_id
   where m.is_deleted = false;

grant select on public.message_filters to anon, authenticated;
