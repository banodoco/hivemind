-- ============================================================
-- Hivemind — Phase 2 / Tasks 2.4 + 2.5 — identity + canonical-representation SQL
-- ============================================================
--
-- SQL-side parity for the entity identity mapping (task 2.4) and the canonical
-- semantic representations + hashes (task 2.5). These IMMUTABLE functions mirror
-- executors.entity_identity and executors.canonical_representations so the
-- lexical SQL, the embedding worker, and the Python canonicalizer all agree on:
--
--   * result_kind -> entity_type            (hivemind_entity_type_for_result_kind)
--   * the canonical TEXT for message / resource / distillation prose
--     (hivemind_canonical_message_text / _resource_text / _distillation_text)
--   * the frozen SHA-256 representation hash (hivemind_representation_hash)
--
-- CROSS-LANGUAGE PARITY (the task 2.5 signal). The canonical-text builders and
-- the hash are the SAME algorithm in Python and SQL on ASCII / already-NFC
-- UTF-8 fixtures (proven in the isolated-cluster rehearsal). One honest caveat:
-- core Postgres has no Unicode NFC normalize(), so hivemind_normalize_for_hash
-- performs every OTHER step of the frozen normalization (CRLF/CR->LF, per-line
-- trailing-whitespace strip, whole-string strip) but NOT NFC. For arbitrary
-- Unicode the AUTHORITATIVE hash is the one the Python embedding job writes
-- (executors.workflow_representation.representation_hash); the SQL function is a
-- lexical/parity helper that matches it on normal text. The stored
-- representation_hash column therefore always reflects the Python value.
--
-- Additive + idempotent. Requires pgcrypto (digest). pgcrypto is present on the
-- live Hivemind project (task 0.2 inventory D9); the rehearsal enables it.
--
-- APPLY:    psql "$HIVEMIND_DB_URL" -f schema/024_identity_and_canonical_representations.sql
-- ROLLBACK: drop function if exists hivemind_representation_hash(text);
--           drop function if exists hivemind_normalize_for_hash(text);
--           drop function if exists hivemind_canonical_distillation_text(text,text,text);
--           drop function if exists hivemind_canonical_resource_text(text,text,text);
--           drop function if exists hivemind_canonical_message_text(text);
--           drop function if exists hivemind_entity_type_for_result_kind(text);
-- ============================================================

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------------
-- Identity: result_kind -> entity_type (mirrors executors.entity_identity)
-- ---------------------------------------------------------------------------
create or replace function hivemind_entity_type_for_result_kind(p_kind text)
returns text
language sql immutable
as $$
    -- message -> message; distillation -> distillation; everything else
    -- (resource, workflow, article, transcript, ...) -> resource.
    -- NULL/empty returns NULL (the Python helper raises; SQL callers validate).
    select case
        when p_kind = 'message' then 'message'::text
        when p_kind = 'distillation' then 'distillation'::text
        when p_kind is null or btrim(p_kind) = '' then null
        else 'resource'::text
    end
$$;

comment on function hivemind_entity_type_for_result_kind(text) is
    'Map a public result_kind to the internal entity_type. Mirrors '
    'executors.entity_identity.entity_type_for_result_kind. NULL/empty -> NULL.';

-- ---------------------------------------------------------------------------
-- Canonical text builders (frozen field order; mirrors Python builders)
-- ---------------------------------------------------------------------------
create or replace function hivemind_canonical_message_text(p_content text)
returns text
language sql immutable
as $$
    select coalesce(p_content, '')
$$;

create or replace function hivemind_canonical_resource_text(
    p_title text, p_body text, p_tags text
) returns text
language sql immutable
as $$
    -- title, then body, then stable textual tags; empty parts skipped (concat_ws
    -- skips NULL). Mirrors canonical_representations.canonical_resource_text.
    select btrim(concat_ws(E'\n\n',
        nullif(btrim(coalesce(p_title, '')), ''),
        nullif(btrim(coalesce(p_body, '')), ''),
        nullif(btrim(coalesce(p_tags, '')), '')
    ))
$$;

create or replace function hivemind_canonical_distillation_text(
    p_question text, p_conditions text, p_answer text
) returns text
language sql immutable
as $$
    -- question, then conditions, then answer. Status/confidence never enter.
    select btrim(concat_ws(E'\n\n',
        nullif(btrim(coalesce(p_question, '')), ''),
        nullif(btrim(coalesce(p_conditions, '')), ''),
        nullif(btrim(coalesce(p_answer, '')), '')
    ))
$$;

-- ---------------------------------------------------------------------------
-- Frozen normalization + SHA-256 representation hash (parity with Python)
-- ---------------------------------------------------------------------------
create or replace function hivemind_normalize_for_hash(p_text text)
returns text
language sql immutable
as $$
    -- Mirrors executors.workflow_representation.normalize_for_hash EXCEPT NFC
    -- (no core PG Unicode normalize). Steps: CRLF/CR -> LF, per-line trailing-
    -- whitespace strip, whole-string strip. Matches Python on ASCII/NFC text.
    with fixed as (
        select regexp_replace(regexp_replace(coalesce(p_text, ''), E'\r\n', E'\n', 'g'),
                              E'\r', E'\n', 'g') as t
    ),
    lines as (
        select regexp_replace(ln, '[ \t\r\f\v]+$', '') as ln
          from fixed, unnest(string_to_array(t, E'\n')) as ln
    )
    select btrim(string_agg(ln, E'\n')) from lines
$$;

create or replace function hivemind_representation_hash(p_text text)
returns text
language sql immutable
as $$
    select encode(digest(hivemind_normalize_for_hash(p_text), 'sha256'), 'hex')
$$;

comment on function hivemind_representation_hash(text) is
    'Frozen SHA-256 representation hash. Mirrors '
    'executors.workflow_representation.representation_hash on ASCII/NFC text. '
    'Authoritative stored hash is Python-computed; this is the SQL parity helper.';

-- ---------------------------------------------------------------------------
-- Verification (read-only): a few parity anchors an operator can eyeball.
-- ---------------------------------------------------------------------------
select
    'message'                                   as kind,
    hivemind_entity_type_for_result_kind('message') as entity_type
union all select
    'workflow', hivemind_entity_type_for_result_kind('workflow')
union all select
    'transcript', hivemind_entity_type_for_result_kind('transcript')
union all select
    'distillation', hivemind_entity_type_for_result_kind('distillation');
