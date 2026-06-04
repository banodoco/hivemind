-- ============================================================
-- Hivemind unified corpus — v2 data structure
-- Run in the Supabase SQL editor (or via supabase db push).
--
-- Adds, alongside the existing `message_feed`:
--   external_resources  — articles, blog posts, transcripts, workflows, ...
--   contributors        — identity for agents/humans who submit distillations
--   distillations       — question-shaped, approved knowledge (the flywheel)
--   distillation_cites  — provenance edges from a distillation to its evidence
--   unified_feed        — the single searchable view over everything
--
-- Reads stay public (RLS read-only policies, same model as message_feed).
-- Writes are NOT public — agent submissions go through an edge function
-- using the service role (built separately).
-- ============================================================

create extension if not exists pg_trgm;

-- ------------------------------------------------------------
-- external_resources: one table for all low-volume kinds.
-- New kind = new value in `kind`, zero DDL.
-- Rule: `body` always holds the FULL searchable text rendition
-- (article text, transcript text, generated workflow description).
-- `url` is provenance only — never a substitute for content.
-- ------------------------------------------------------------
create table external_resources (
  id          bigint generated always as identity primary key,
  kind        text not null,                    -- 'article' | 'blog_post' | 'transcript' | 'workflow' | 'repo' | ...
  source      text not null,                    -- origin system: 'youtube', 'civitai', 'manual', ...
  external_id text,                             -- natural key in the source system (video id, slug, ...)
  title       text not null,
  body        text not null,                    -- full searchable text rendition
  author      text,
  url         text,                             -- link to the original (provenance)
  metadata    jsonb not null default '{}'::jsonb,  -- kind-specific facts: {models: [...], duration, chapters, ...}
  payload     jsonb,                            -- native structured artifact (e.g. ComfyUI workflow JSON)
  created_at  timestamptz not null default now(),
  unique (source, external_id)
);

create index external_resources_body_trgm on external_resources using gin (body gin_trgm_ops);
create index external_resources_title_trgm on external_resources using gin (title gin_trgm_ops);
create index external_resources_kind_idx on external_resources (kind);
create index external_resources_metadata_idx on external_resources using gin (metadata);

-- ------------------------------------------------------------
-- contributors: who is allowed to submit distillations.
-- Rows issued manually for now. api_key_hash is checked by the
-- submit edge function — this table is NEVER publicly readable.
-- ------------------------------------------------------------
create table contributors (
  id           bigint generated always as identity primary key,
  name         text not null unique,
  kind         text not null check (kind in ('agent', 'human')),
  api_key_hash text,                            -- sha256 of the issued key
  revoked_at   timestamptz,                     -- active keys have revoked_at is null
  created_at   timestamptz not null default now()
);

-- ------------------------------------------------------------
-- distillations: question-shaped knowledge with a review lifecycle.
-- Agents can only ever create rows in `pending` (enforced by the
-- edge function); curators promote to `approved` or edit-then-approve.
-- Supersede, don't edit-in-place: new row points at the old one.
-- ------------------------------------------------------------
create table distillations (
  id            bigint generated always as identity primary key,
  question      text not null,                  -- "what's the best upscale model ..."
  conditions    text,                           -- the "in this case" qualifiers: "... for anime-style video"
  answer        text not null,
  confidence    text not null check (confidence in ('high', 'medium', 'low')),
  status        text not null default 'pending'
                check (status in ('pending', 'approved', 'rejected', 'superseded')),
  author_id     bigint not null references contributors(id),
  approved_by   bigint references contributors(id),
  supersedes_id bigint references distillations(id),
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

create index distillations_question_trgm on distillations using gin (question gin_trgm_ops);
create index distillations_answer_trgm on distillations using gin (answer gin_trgm_ops);
create index distillations_status_idx on distillations (status);

-- ------------------------------------------------------------
-- check_duplicate_distillation: RPC bridge for the edge function.
-- PostgREST cannot call pg_trgm similarity() in REST queries,
-- but it can invoke this function at /rest/v1/rpc/.
-- Returns the best pending/approved match above strict > 0.6
-- similarity, excluding the caller's own supersedes_id.
-- ------------------------------------------------------------
create or replace function check_duplicate_distillation(
  question_text text,
  supersedes_id bigint default null
)
returns table(existing_id bigint, similarity real)
language sql stable
as $$
  select id, similarity(question, question_text) as similarity
  from distillations
  where status in ('pending', 'approved')
    and similarity(question, question_text) > 0.6
    and (supersedes_id is null or id <> supersedes_id)
  order by similarity desc
  limit 1;
$$;

-- ------------------------------------------------------------
-- distillation_cites: provenance. Every distillation must cite ≥1
-- item (enforced by the edge function). Polymorphic reference —
-- a cite points into message_feed, external_resources, or
-- another distillation, so no hard FK on (item_kind, item_id).
-- ------------------------------------------------------------
create table distillation_cites (
  distillation_id bigint not null references distillations(id) on delete cascade,
  item_kind       text not null check (item_kind in ('message', 'resource', 'distillation')),
  item_id         bigint not null,              -- message_feed.message_id | external_resources.id | distillations.id
  primary key (distillation_id, item_kind, item_id)
);

-- ------------------------------------------------------------
-- unified_feed: the one searchable surface.
-- Common shape: (kind, source, item_id, title, body, author,
--                context, url, metadata, created_at)
-- Rejected distillations are excluded; pending ones surface with
-- their status visible in metadata so agents can tier results.
-- ------------------------------------------------------------
create view unified_feed
with (security_invoker = true) as
  select
    'message'                       as kind,
    'banodoco-discord'              as source,
    message_id::text                as item_id,
    null::text                      as title,
    content                         as body,
    author_name                     as author,
    channel_name                    as context,
    'https://discord.com/channels/' || guild_id || '/' || channel_id || '/' || message_id
                                    as url,
    jsonb_build_object('channel_id', channel_id, 'reactions', reactions)
                                    as metadata,
    created_at
  from message_feed
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

-- ------------------------------------------------------------
-- Row-level security: public read on content, nothing else.
-- contributors gets RLS enabled with NO policies → invisible to anon.
-- All writes happen via the service role (edge functions / your scripts).
-- ------------------------------------------------------------
alter table external_resources  enable row level security;
alter table distillations       enable row level security;
alter table distillation_cites  enable row level security;
alter table contributors        enable row level security;

create policy "public read" on external_resources for select using (true);
create policy "public read" on distillations      for select using (status <> 'rejected');
create policy "public read" on distillation_cites for select using (true);
-- (no policy on contributors: deny-all)

grant select on unified_feed to anon, authenticated;
