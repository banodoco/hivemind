-- ============================================================
-- 003_lexical_resource_documents.sql
-- Hivemind hybrid search — Phase 1, Task 1.2
--
-- Weighted lexical documents + GIN indexes for resource prose/code
-- and distillations, including workflow-Python precedence, secret
-- scanning/quarantine, deduplication, and bounded code documents.
--
-- Implements the FROZEN Phase-1 lexical contract (task 1.1):
--   docs/hybrid-search/phase1-lexical-contract.{md,json}
-- and the FROZEN workflow-representation contract (task 0.8):
--   docs/hybrid-search/phase0-workflow-representation-contract.{md,json}
-- The IMMUTABLE SQL helpers below mirror executors/workflow_representation.py
-- (strip_python_blocks, project_semantics) and the frozen weighted
-- to_tsvector expressions mirror executors/lexical_contract.py exactly.
--
-- Scope (task 1.2): resources (prose + workflow_python code) and
-- distillations. Discord messages are task 1.3; identifier normalization
-- is task 1.4 — neither is touched here.
--
-- Additive only. Idempotent (safe to re-run). No source rows are read or
-- mutated by this migration; it only creates storage, functions, columns,
-- and indexes. RLS/eligibility are encoded in candidate queries, not here.
-- ============================================================

create extension if not exists pg_trgm;

-- ============================================================
-- IMMUTABLE helper functions — mirror executors/workflow_representation.py
-- ============================================================
-- All are declared IMMUTABLE so they may be used in STORED generated
-- columns and expression indexes. They read only their arguments (no table
-- access, no volatile ops), matching the frozen Python reference byte-for-
-- behaviour. Bumping the frozen *_VERSION constants in the Python contract
-- is a re-derivation event; these helpers mirror the current (v1) behaviour.

-- ----------------------------------------------------------------
-- hivemind_jsonb_leaves(jsonb) -> text
-- Recursively flatten a jsonb value to its scalar leaf values, space-joined,
-- descending object VALUES and arrays (mirrors _flatten). Null / empty
-- leaves are dropped. Used by the tags + semantics projectors.
-- ----------------------------------------------------------------
create or replace function public.hivemind_jsonb_leaves(v jsonb)
returns text
language plpgsql
immutable
as $$
declare
  out text := '';
  item jsonb;
  s text;
begin
  if v is null then
    return '';
  end if;
  case jsonb_typeof(v)
    when 'array' then
      for item in select * from jsonb_array_elements(v) loop
        s := public.hivemind_jsonb_leaves(item);
        if s <> '' then
          out := case when out = '' then s else out || ' ' || s end;
        end if;
      end loop;
      return out;
    when 'object' then
      -- descend by VALUES (keys are not semantic content); mirrors _flatten
      for item in select value from jsonb_each(v) loop
        s := public.hivemind_jsonb_leaves(item);
        if s <> '' then
          out := case when out = '' then s else out || ' ' || s end;
        end if;
      end loop;
      return out;
    when 'null' then
      return '';
    else
      -- string / number / boolean: text value (mirrors str(value) in Python)
      s := v #>> '{}';
      if s is null then
        return '';
      end if;
      return s;
  end case;
end;
$$;

-- ----------------------------------------------------------------
-- hivemind_resource_tags(metadata jsonb) -> text
-- Stable textual tags for the resource-prose B-weight arm. Reads
-- metadata->'tags' and metadata->'summary'->'tags' (mirrors the tag
-- sources used by executors/workflow_semantics.py) and renders them as
-- space-joined text. Absent => ''.
-- ----------------------------------------------------------------
create or replace function public.hivemind_resource_tags(p_metadata jsonb)
returns text
language plpgsql
immutable
as $$
declare
  t_meta text;
  t_summary text;
  parts text[] := array[]::text[];
begin
  if p_metadata is null then
    return '';
  end if;
  t_meta := public.hivemind_jsonb_leaves(p_metadata -> 'tags');
  t_summary := public.hivemind_jsonb_leaves(p_metadata -> 'summary' -> 'tags');
  if t_meta is not null and t_meta <> '' then
    parts := array_append(parts, t_meta);
  end if;
  if t_summary is not null and t_summary <> '' then
    parts := array_append(parts, t_summary);
  end if;
  return array_to_string(parts, ' ');
end;
$$;

-- ----------------------------------------------------------------
-- hivemind_workflow_semantics_text(metadata jsonb) -> text
-- Project the frozen workflow_semantics fields into canonical prose text
-- (mirrors project_semantics). Field order is frozen; absent/empty fields
-- are omitted; containers are flattened; fields are newline-joined.
-- Frozen fields: media_type, task_type, model_families, adapter_directions,
-- node_types, node_class_multiset, custom_nodes, models, searchable_aliases.
-- ----------------------------------------------------------------
create or replace function public.hivemind_workflow_semantics_text(p_metadata jsonb)
returns text
language plpgsql
immutable
as $$
declare
  sem jsonb;
  field_order text[] := array[
    'media_type', 'task_type', 'model_families', 'adapter_directions',
    'node_types', 'node_class_multiset', 'custom_nodes', 'models',
    'searchable_aliases'
  ];
  f text;
  val jsonb;
  rendered text;
  parts text[] := array[]::text[];
begin
  if p_metadata is null then
    return '';
  end if;
  sem := p_metadata -> 'workflow_semantics';
  if sem is null or jsonb_typeof(sem) <> 'object' then
    return '';
  end if;
  foreach f in array field_order loop
    val := sem -> f;
    if val is null or jsonb_typeof(val) = 'null' then
      continue;
    end if;
    if jsonb_typeof(val) in ('array', 'object') then
      rendered := public.hivemind_jsonb_leaves(val);
    else
      rendered := val #>> '{}';
    end if;
    rendered := btrim(rendered);
    if rendered is not null and rendered <> '' then
      parts := array_append(parts, rendered);
    end if;
  end loop;
  return array_to_string(parts, e'\n');
end;
$$;

-- ----------------------------------------------------------------
-- hivemind_workflow_prose(body text, kind text) -> text
-- The resource-prose C-weight source. For kind='workflow', return body
-- with every recognized delimited Python block removed (no-duplication:
-- the same code is indexed exactly once as workflow_python). Mirrors
-- strip_python_blocks: a block starts at a line whose rstrip() equals a
-- recognized delimiter and runs until the next delimiter, the
-- 'Workflow semantics (rule-based):' marker, or end of body. The delimiter
-- line is removed; trailing blank runs are collapsed to at most two
-- newlines. Non-workflow kinds return body unchanged. Recognized frozen
-- delimiters (0.8): 'Python ready-template source:', 'Python scratchpad source:'.
-- ----------------------------------------------------------------
create or replace function public.hivemind_workflow_prose(p_body text, p_kind text)
returns text
language plpgsql
immutable
as $$
declare
  v_lines text[];
  v_line text;
  v_out text[] := array[]::text[];
  v_i int;
  v_n int;
  v_j int;
  v_lf text;
begin
  if p_body is null then
    return '';
  end if;
  if coalesce(p_kind, '') <> 'workflow' then
    return p_body;
  end if;
  -- Normalize CRLF/CR -> LF (mirrors the Python normalizer).
  v_lf := replace(replace(p_body, e'\r\n', e'\n'), e'\r', e'\n');
  v_lines := string_to_array(v_lf, e'\n');
  v_n := array_length(v_lines, 1);
  v_i := 1;
  while v_i <= v_n loop
    v_line := rtrim(v_lines[v_i]);
    if v_line in ('Python ready-template source:', 'Python scratchpad source:') then
      -- Skip the delimiter line and its block body.
      v_j := v_i + 1;
      while v_j <= v_n loop
        v_line := rtrim(v_lines[v_j]);
        exit when v_line in ('Python ready-template source:', 'Python scratchpad source:');
        exit when v_line like 'Workflow semantics (rule-based):%';
        v_j := v_j + 1;
      end loop;
      -- Resume at the terminating line; it is re-examined next iteration
      -- (a delimiter starts a new block; the semantics marker is kept).
      v_i := v_j;
      continue;
    end if;
    v_out := array_append(v_out, v_lines[v_i]);
    v_i := v_i + 1;
  end loop;
  return btrim(regexp_replace(array_to_string(v_out, e'\n'), '(\n){3,}', e'\n\n', 'g'));
end;
$$;

-- ============================================================
-- Generated weighted tsvector columns (the frozen 1.1 expressions)
-- ============================================================
-- Distillation prose: question (A) / conditions (B) / answer (C).
-- Mirrors DISTILLATION_SPEC.expression().
alter table public.distillations
  add column if not exists lexical_tsv tsvector not null
  generated always as (
    setweight(to_tsvector('simple'::regconfig, coalesce(question,  '')), 'A')
    || setweight(to_tsvector('simple'::regconfig, coalesce(conditions, '')), 'B')
    || setweight(to_tsvector('simple'::regconfig, coalesce(answer,    '')), 'C')
  ) stored;

create index if not exists distillations_lexical_tsv_idx
  on public.distillations using gin (lexical_tsv);

-- Resource prose: title (A) / tags + projected workflow_semantics (B) /
-- body-with-python-blocks-stripped (C). Mirrors RESOURCE_PROSE_SPEC.expression().
alter table public.external_resources
  add column if not exists prose_tsv tsvector not null
  generated always as (
    setweight(to_tsvector('simple'::regconfig, coalesce(title, '')), 'A')
    || setweight(to_tsvector('simple'::regconfig,
         coalesce(
           public.hivemind_resource_tags(metadata)
           || ' ' || public.hivemind_workflow_semantics_text(metadata), '')), 'B')
    || setweight(to_tsvector('simple'::regconfig,
         coalesce(public.hivemind_workflow_prose(body, kind), '')), 'C')
  ) stored;

create index if not exists external_resources_prose_tsv_idx
  on public.external_resources using gin (prose_tsv);

-- ============================================================
-- Per-resource workflow-Python representation state
-- ============================================================
-- Written by the lexical refresh job (task 1.2 helper / later task 2.12
-- remediation). Read by the workflow_python eligibility predicate. Stores
-- ONLY non-secret reason codes + bounded metadata — never the matched
-- secret value (0.8 §7). Quarantined python produces a state row here but
-- NO row in lexical_documents (structural exclusion).
create table if not exists public.lexical_resource_python_state (
  resource_id             bigint primary key
                          references public.external_resources(id) on delete cascade,
  kind                    text not null,
  cohort                  text not null
                          check (cohort in ('payload_python', 'body_python',
                                            'recoverable', 'unavailable')),
  public_state            text not null
                          check (public_state in ('safe', 'quarantined')),
  available               boolean not null,
  body_duplicate          boolean not null default false,
  delimiter               text,
  derivation              text,
  representation_hash     text,
  -- Non-secret reason codes only (never positions/values exposed to clients).
  secret_reason_codes     text[] not null default '{}',
  canonicalization_version integer not null default 1,
  secret_scan_version      integer not null default 1,
  chunking_version         integer not null default 1,
  chunk_count              integer not null default 0,
  updated_at               timestamptz not null default now()
);

comment on table public.lexical_resource_python_state is
  'Task 1.2: per-resource authoritative-Python cohort + safe/quarantined '
  'state for the workflow_python lexical eligibility gate (frozen 0.8 §7).';

create index if not exists lexical_resource_python_state_state_idx
  on public.lexical_resource_python_state (public_state);
create index if not exists lexical_resource_python_state_cohort_idx
  on public.lexical_resource_python_state (cohort);

-- STABLE accessor used by the frozen workflow_python eligibility predicate
-- (lexical_contract.WORKFLOW_PYTHON_ELIGIBLE). Returns NULL when no state
-- row exists (no authoritative python resolved / not yet refreshed).
create or replace function public.hivemind_workflow_python_state(p_resource_id bigint)
returns text
language sql
stable
set search_path = public
as $$
  select lps.public_state
  from public.lexical_resource_python_state lps
  where lps.resource_id = $1;
$$;

-- ============================================================
-- Chunk-aware lexical document storage (frozen 1.1 §6)
-- ============================================================
-- Durable identity (entity_type, item_id, representation_type, chunk_index),
-- the same shape as the 0.8 embedding identity so a lexical chunk and a
-- future embedding chunk share an addressable, collapsible identity. This
-- is the LEXICAL table; it holds no vectors and does not collide with the
-- task-2 content_embeddings table (which is keyed by contract_id).
--
-- Holds:
--   * workflow_python code chunks (one row per chunk; uniform C weight).
--   * over-long resource-prose fallback chunks (rare; pilot path).
-- Distillation prose and normal resource prose live in generated columns on
-- their source tables; messages stay on the discord_messages index (1.3).
create table if not exists public.lexical_documents (
  entity_type             text not null,
  item_id                 text not null,
  representation_type     text not null default 'prose',
  chunk_index             integer not null default 0,

  -- The frozen workflow_python expression is realized in SQL here: the
  -- chunk's tsvector is generated from chunk_text at uniform weight C.
  chunk_text              text not null,
  tsv                     tsvector not null
                          generated always as (
                            setweight(to_tsvector('simple'::regconfig,
                              coalesce(chunk_text, '')), 'C')
                          ) stored,

  -- <=240-char secret-redacted snippet anchor (0.8 redact_snippet).
  matched_anchor          text,
  -- Best-effort char offsets of the chunk text in the normalized source
  -- (overlap means these are anchors, not a strict partition).
  source_offset_start     integer not null default 0,
  source_offset_end       integer not null default 0,

  representation_hash     text not null,
  chunk_hash              text not null,

  -- Eligibility / quarantine (explicit because service role bypasses RLS).
  quarantine_state        text not null default 'safe'
                          check (quarantine_state in ('safe', 'quarantined')),

  -- Frozen freshness versions (mirror 0.8). A bump re-derives documents.
  lexicalization_version  integer not null default 1,
  canonicalization_version integer not null default 1,
  chunking_version        integer not null default 1,
  secret_scan_version     integer not null default 1,

  method                  text,   -- chunker method: ast | ast_fallback | paragraph | sentence | fixed_window
  created_at              timestamptz not null default now(),

  constraint lexical_documents_pk
    primary key (entity_type, item_id, representation_type, chunk_index),
  constraint lexical_documents_entity_chk
    check (entity_type in ('message', 'resource', 'distillation')),
  constraint lexical_documents_rep_chk
    check (representation_type in ('prose', 'workflow_python')),
  constraint lexical_documents_chunk_idx_chk
    check (chunk_index >= 0),
  -- Structural no-duplication / quarantine guarantee: a workflow_python
  -- lexical document is always safe (quarantined python has NO row here).
  constraint lexical_documents_workflow_python_safe_chk
    check (representation_type <> 'workflow_python' or quarantine_state = 'safe')
);

comment on table public.lexical_documents is
  'Task 1.2: chunk-aware lexical documents keyed by '
  '(entity_type, item_id, representation_type, chunk_index). Holds '
  'workflow_python code chunks + over-long prose fallback; quarantined '
  'python is structurally absent. Mirrors frozen 1.1 §6 + 0.8 identity.';

-- GIN search index over the generated weighted tsvector (the primary arm).
create index if not exists lexical_documents_tsv_idx
  on public.lexical_documents using gin (tsv);

-- Identity / chunk-collapse indexes (best ts_rank per item/representation).
create index if not exists lexical_documents_identity_idx
  on public.lexical_documents (entity_type, item_id, representation_type);
create index if not exists lexical_documents_item_idx
  on public.lexical_documents (entity_type, item_id);

-- Freshness / staleness (representation-hash skip during refresh).
create index if not exists lexical_documents_repr_hash_idx
  on public.lexical_documents (representation_hash);

-- Fast collapse of safe workflow_python chunks per resource.
create index if not exists lexical_documents_workflow_python_idx
  on public.lexical_documents (item_id)
  where representation_type = 'workflow_python' and quarantine_state = 'safe';

-- ============================================================
-- Grants / RLS
-- ============================================================
-- These tables are maintained by the service role (refresh jobs) and read by
-- the hardened search RPC (task 1.9). Public clients never query them
-- directly. Enable RLS with no public policy (deny to anon/authenticated);
-- the service role bypasses RLS and the RPC is SECURITY DEFINER (1.9).
alter table public.lexical_documents enable row level security;
alter table public.lexical_resource_python_state enable row level security;

-- No SELECT policy for anon/authenticated => invisible publicly by default.
-- (The hardened search RPC in 1.9 will be the only read path; service role
--  bypasses RLS for maintenance.)
revoke all on public.lexical_documents from public;
revoke all on public.lexical_resource_python_state from public;
