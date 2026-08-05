-- ============================================================
-- Hivemind — identifier normalization + alias reference (hybrid-search task 1.4)
--
-- This migration defines the FROZEN, deterministic, cross-language identifier
-- normalization contract and the explicit, bounded alias reference that the
-- exact-identifier candidate arm (plan tasks 1.5–1.7) consumes. It is the SQL
-- mirror of ``executors/identifier_normalization.py`` (the offline reference);
-- the two are proven byte-for-byte identical on the frozen fixture corpus on an
-- isolated PostgreSQL 14 cluster (see
-- ``docs/hybrid-search/phase1-identifier-normalization.md`` § Evidence).
--
-- Frozen forms (mirrors the reference module + task-1.1 lexical contract):
--   hivemind_normalize_identifier(text)           -> compact key
--     NFC-equivalent* + lower + strip + drop the frozen separator set.
--     *PostgreSQL has no built-in Unicode NFC; the application/ingest layer
--      MUST store identifier text in NFC (the Python reference enforces it).
--      The Python/SQL NFC boundary is the one documented non-equivalence.
--   hivemind_normalize_identifier_preserve(text)  -> punctuation-preserving key
--     lower + strip + collapse whitespace runs to single spaces. KEEPS the
--     identifier's punctuation (``. - _ / :`` …) as visible token separators
--     for the exact FTS phrase arm and a punctuation-aware trigram path.
--
-- Casefold policy (frozen): ``lower(value COLLATE hivemind_unicode)`` where
-- ``hivemind_unicode`` is a DETERMINISTIC ICU collation (``locale = 'und'``).
-- This gives locale-independent Unicode lowercasing — verified to agree with
-- Python ``str.lower()`` across the BMP Latin / Greek / Cyrillic / CJK ranges
-- on a cluster whose default collation is ``C`` (where stock ``lower()`` would
-- NOT lowercase non-ASCII). ``str.casefold()`` is deliberately NOT used: its
-- multi-character expansions (ß->ss, ligatures) and combining-mark insertions
-- (İ->i̇) have no single SQL built-in and would break byte-for-byte parity.
--
-- Scope boundary — what this migration deliberately does NOT create:
--   * trigram / expression indexes on alias_compact / canonical_key  (task 1.5)
--   * the lexical candidate SQL / RPC that consumes aliases          (task 1.7)
-- Both consume these frozen IMMUTABLE functions; neither is implemented here.
--
-- Versions: bumping ``IDENTIFIER_NORMALIZATION_VERSION`` /
-- ``IDENTIFIER_ALIAS_VERSION`` (in the Python reference) re-derives every
-- generated column and every alias row (plan AD-4); it is never a silent edit.
-- ============================================================

-- ------------------------------------------------------------
-- 1. Locale-independent Unicode collation for deterministic lowercasing.
--    ICU must be available (Supabase PostgreSQL is built --with-icu). The
--    collation is deterministic so functions using it can be IMMUTABLE and so
--    it is eligible for expression indexes and STORED generated columns.
-- ------------------------------------------------------------
create collation if not exists public.hivemind_unicode (
  provider = icu,
  locale = 'und',
  deterministic = true
);

-- ------------------------------------------------------------
-- 2. Compact identifier key (the punctuation-separated/removed form).
--    Coalesce so NULL -> '' (matches the Python reference's None -> '').
--    The bracket class is the frozen separator set; ``\\`` is one literal
--    backslash (ARE escaping), ``\s`` is whitespace, ``]`` leads and ``-``
--    trails so neither needs escaping, and the embedded single quote is
--    doubled (``''``) for the single-quoted string literal.
-- ------------------------------------------------------------
create or replace function public.hivemind_normalize_identifier(value text)
returns text
language sql
immutable
parallel safe
as $$
  select coalesce(
    regexp_replace(
      lower(value collate public.hivemind_unicode),
      '[]\s./\\,:;(){}[@"''`_-]',
      '',
      'g'
    ),
    ''
  )
$$;

comment on function public.hivemind_normalize_identifier(text) is
  'Compact identifier key: locale-independent Unicode lowercase (ICU und) + strip '
  '+ drop the frozen separator set. IMMUTABLE — safe for expression indexes and '
  'STORED generated columns. Mirrors executors.identifier_normalization.normalize_identifier.';

-- ------------------------------------------------------------
-- 3. Punctuation-preserving identifier key.
--    lower + strip + collapse whitespace runs to single spaces. Punctuation is
--    KEPT (it is the token shape for the exact phrase / trigram arms).
-- ------------------------------------------------------------
create or replace function public.hivemind_normalize_identifier_preserve(value text)
returns text
language sql
immutable
parallel safe
as $$
  select coalesce(
    btrim(regexp_replace(
      lower(value collate public.hivemind_unicode),
      '\s+',
      ' ',
      'g'
    )),
    ''
  )
$$;

comment on function public.hivemind_normalize_identifier_preserve(text) is
  'Punctuation-preserving identifier key: locale-independent Unicode lowercase + '
  'strip + collapse whitespace to single spaces (punctuation kept). IMMUTABLE. '
  'Mirrors executors.identifier_normalization.normalize_identifier_preserve.';

-- ------------------------------------------------------------
-- 4. Ordered, de-duplicated alias forms [compact, preserve] (convenience).
--    Compact first, then preserve when non-empty and distinct; empties dropped.
-- ------------------------------------------------------------
create or replace function public.hivemind_identifier_alias_forms(value text)
returns text[]
language sql
immutable
parallel safe
as $$
  select coalesce(
    array_remove(
      array[
        case when c <> '' then c end,
        case when p <> '' and p <> c then p end
      ],
      null
    ),
    array[]::text[]
  )
  from (
    select public.hivemind_normalize_identifier(value) as c,
           public.hivemind_normalize_identifier_preserve(value) as p
  ) s
$$;

comment on function public.hivemind_identifier_alias_forms(text) is
  'Ordered de-duplicated identifier forms [compact, preserve] for the side index. '
  'IMMUTABLE. Mirrors executors.identifier_normalization.identifier_forms.';

-- ------------------------------------------------------------
-- 5. Deterministic provenance -> priority weight (frozen vocabulary).
-- ------------------------------------------------------------
create or replace function public.hivemind_alias_provenance_priority(p_provenance text)
returns integer
language sql
immutable
parallel safe
as $$
  select case p_provenance
    when 'curated'                                  then 100
    when 'workflow_semantics.searchable_aliases'    then 80
    when 'workflow_semantics.node_class'            then 70
    when 'workflow_semantics.models'                then 60
    when 'derived_canonical'                        then 40
    else 0
  end
$$;

comment on function public.hivemind_alias_provenance_priority(text) is
  'Frozen deterministic priority for an alias provenance label. Mirrors '
  'executors.identifier_normalization.PROVENANCE_PRIORITY.';

-- ------------------------------------------------------------
-- 6. identifier_aliases — the explicit, bounded alias reference.
--    An alias row expands a QUERY (written one way) to a CANONICAL IDENTITY
--    (known another way). It records provenance + version + priority; its
--    compact/preserve/canonical_key columns are STORED GENERATED from the
--    IMMUTABLE functions above (the proof those functions are index-suitable).
--
--    Safety (plan 1.4): aliases only ADD candidate identity edges for the
--    exact-identifier arm. They never rewrite FTS query text, never splice into
--    a prose tsvector, and never relabel one identity as another. Collisions
--    (one alias key -> >=2 distinct identities) are REPORTED, never silently
--    merged; see hivemind_identifier_alias_collisions().
--
--    RLS: this is an internal reference for the service-role candidate RPC
--    (1.7/1.9); it is NOT public-readable (RLS enabled, no anon policy), like
--    contributors. The candidate RPC bypasses RLS as the service role.
-- ------------------------------------------------------------
create table if not exists public.identifier_aliases (
  id                  bigint generated always as identity primary key,
  canonical_kind      text not null
                        check (canonical_kind in ('resource','distillation','message')),
  canonical_id        text not null,
  canonical_name      text not null,
  canonical_key       text not null generated always as
                        (public.hivemind_normalize_identifier(canonical_name)) stored,
  alias_text          text not null,
  alias_compact       text not null generated always as
                        (public.hivemind_normalize_identifier(alias_text)) stored,
  alias_preserve      text not null generated always as
                        (public.hivemind_normalize_identifier_preserve(alias_text)) stored,
  provenance          text not null
                        check (provenance in (
                          'curated',
                          'workflow_semantics.searchable_aliases',
                          'workflow_semantics.node_class',
                          'workflow_semantics.models',
                          'derived_canonical'
                        )),
  provenance_detail   text,
  provenance_version  integer not null default 1,
  alias_version       integer not null default 1
                        check (alias_version >= 1),
  priority            integer not null default 0,
  live                boolean not null default true,
  registered_at       timestamptz not null default now(),
  -- An alias maps one logical (identity, compact key, provenance) upsert key.
  unique (canonical_kind, canonical_id, alias_compact, provenance),
  -- A useful alias must have a non-empty compact key and a non-empty canonical.
  check (alias_compact <> ''),
  check (canonical_key <> '')
);

comment on table public.identifier_aliases is
  'Explicit, bounded, versioned alias reference for the exact-identifier '
  'candidate arm (hybrid-search tasks 1.5–1.7). Mirrors '
  'executors.identifier_normalization.AliasRegistry. NOT indexed here (trigram/'
  'expression indexes are task 1.5; candidate SQL is task 1.7).';

alter table public.identifier_aliases enable row level security;
-- No SELECT policy: anon / authenticated cannot read the alias reference directly.
-- The service-role candidate RPC (1.9) bypasses RLS and encodes eligibility.

-- Index the live-set / collision / cleanup queries 1.5/1.7 will need (btree only;
-- no trigram here). These are ordinary btree indexes, NOT the task-1.5 trigram work.
create index if not exists identifier_aliases_compact_idx
  on public.identifier_aliases (alias_compact)
  where live;
create index if not exists identifier_aliases_identity_idx
  on public.identifier_aliases (canonical_kind, canonical_id)
  where live;

-- ------------------------------------------------------------
-- 7. Idempotent alias registration (safe-update aware).
--    Re-registering the same logical alias refreshes version/priority/liveness.
--    Superseding an alias = re-registering the live set at a new version; stale
--    rows become live=false tombstones (never deleted in place). An alias that
--    normalizes to an empty compact key is rejected (fail-closed).
-- ------------------------------------------------------------
create or replace function public.hivemind_register_identifier_alias(
  p_canonical_kind     text,
  p_canonical_id       text,
  p_canonical_name     text,
  p_alias              text,
  p_provenance         text,
  p_provenance_detail  text default null,
  p_provenance_version integer default 1,
  p_priority           integer default null,
  p_alias_version      integer default null
)
returns public.identifier_aliases
language plpgsql
as $$
declare
  v_compact text := public.hivemind_normalize_identifier(p_alias);
  v_priority integer := coalesce(p_priority, public.hivemind_alias_provenance_priority(p_provenance));
  v_row public.identifier_aliases;
begin
  if p_canonical_kind not in ('resource','distillation','message') then
    raise exception 'canonical_kind must be resource|distillation|message: %', p_canonical_kind;
  end if;
  if p_provenance not in ('curated','workflow_semantics.searchable_aliases',
                          'workflow_semantics.node_class','workflow_semantics.models',
                          'derived_canonical') then
    raise exception 'provenance not in frozen vocabulary: %', p_provenance;
  end if;
  if v_compact = '' then
    raise exception 'alias normalizes to an empty compact key (rejected): %', p_alias;
  end if;
  if public.hivemind_normalize_identifier(p_canonical_name) = '' then
    raise exception 'canonical_name normalizes to an empty compact key (rejected): %', p_canonical_name;
  end if;

  insert into public.identifier_aliases (
    canonical_kind, canonical_id, canonical_name, alias_text, provenance,
    provenance_detail, provenance_version, alias_version, priority, live
  )
  values (
    p_canonical_kind, p_canonical_id, p_canonical_name, p_alias, p_provenance,
    p_provenance_detail, p_provenance_version,
    coalesce(p_alias_version, 1), v_priority, true
  )
  on conflict (canonical_kind, canonical_id, alias_compact, provenance)
  do update set
    alias_version      = excluded.alias_version,
    priority           = excluded.priority,
    live               = true,
    provenance_detail  = excluded.provenance_detail,
    provenance_version = excluded.provenance_version,
    registered_at      = now()
  returning * into v_row;

  return v_row;
end;
$$;

comment on function public.hivemind_register_identifier_alias(text,text,text,text,text,text,integer,integer,integer) is
  'Idempotent alias registration. Mirrors AliasRegistry.register. Fails closed on '
  'an alias/canonical that normalizes to empty. Triggers no candidate SQL (1.7).';

-- ------------------------------------------------------------
-- 8. Collision detection — alias keys shared by >=2 distinct identities.
--    REPORTED for operator disambiguation; never silently merged.
-- ------------------------------------------------------------
create or replace function public.hivemind_identifier_alias_collisions()
returns table (
  alias_compact        text,
  n_distinct_canonical bigint,
  canonical_kinds      text[],
  canonical_ids        text[],
  canonical_names      text[]
)
language sql
stable
as $$
  select
    a.alias_compact,
    count(distinct a.canonical_kind || ':' || a.canonical_id) as n_distinct_canonical,
    array_agg(distinct a.canonical_kind order by a.canonical_kind) as canonical_kinds,
    array_agg(a.canonical_id order by a.canonical_id) as canonical_ids,
    array_agg(distinct a.canonical_name order by a.canonical_name) as canonical_names
  from public.identifier_aliases a
  where a.live
  group by a.alias_compact
  having count(distinct a.canonical_kind || ':' || a.canonical_id) > 1
  order by a.alias_compact;
$$;

comment on function public.hivemind_identifier_alias_collisions() is
  'Live alias compact keys shared by two or more distinct canonical identities. '
  'Mirrors AliasRegistry.collisions. Surfaced for disambiguation, never silently '
  'merged. STABLE (read-only).';
