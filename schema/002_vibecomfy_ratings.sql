create table if not exists public.vibecomfy_ratings (
  id uuid primary key default gen_random_uuid(),
  response_id text not null,
  session_id text not null,
  turn_id text not null,
  rating smallint not null,
  comment text,
  pack_shared boolean not null default false,
  pack_comment text,
  report_url text,
  report_path text,
  contributor_key_hash text not null,
  metadata jsonb not null default '{}'::jsonb,
  client_created_at timestamptz,
  created_at timestamptz not null default now(),
  constraint vibecomfy_ratings_rating_check check (rating between 1 and 10)
);

create index if not exists vibecomfy_ratings_response_id_idx
  on public.vibecomfy_ratings (response_id);

create index if not exists vibecomfy_ratings_created_at_idx
  on public.vibecomfy_ratings (created_at);

alter table if exists public.vibecomfy_ratings enable row level security;

drop policy if exists "vibecomfy_ratings_public_read" on public.vibecomfy_ratings;
create policy "vibecomfy_ratings_public_read"
  on public.vibecomfy_ratings
  for select
  using (true);
