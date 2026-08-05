-- ============================================================
-- Hivemind — Phase 2 / Task 2.9 (part 1) — embedding-worker SQL surface
-- ============================================================
--
-- The read/write surface the bounded `embedding-worker` Edge Function (this
-- task) drives on each invocation. The worker is THIN: it claims a bounded
-- batch (schema/026), asks this surface for the canonical chunks to embed,
-- calls the provider, validates dimensions, and writes the vectors back through
-- an atomic replace. Chunking happens HERE (one deterministic chunker in SQL)
-- so the worker carries no chunking logic and there is no second algorithm to
-- diverge in TypeScript. The authoritative Python chunker (executors/chunking,
-- task 2.6) is the offline reference / backfill path; the backfill (task 2.12)
-- unifies on this same surface so chunk identity is stable across the
-- backfill -> incremental boundary (named handoff for 2.12).
--
-- Functions:
--   hivemind_chunk_text(text, target_tokens, overlap_tokens)
--       deterministic line-window chunker (single chunk when the normalized
--       source fits the budget; else bounded windows with overlap). plan AD-5
--       permits "stable line or token windows"; never silent truncation.
--   hivemind_embedding_payload(entity_type, item_id, representation_type,
--                              target_tokens, overlap_tokens)
--       SECURITY DEFINER read of the CURRENT source -> canonical chunks to embed.
--       The worker always embeds current source, so completion is source-hash
--       safe (a change since enqueue simply produced a fresh job).
--   hivemind_upsert_embedding_chunks(chunks jsonb)
--       atomic replace of one (contract,entity,item,representation)'s vectors.
--       The vector(384) column + dimension trigger reject a wrong-dim vector.
--   hivemind_drop_embedding_chunks(contract,entity,item,representation)
--       remove an item's vectors (drop / ineligible / cleanup; task 2.10).
--
-- Additive + idempotent. Requires schema/020–026.
--
-- APPLY:    psql "$HIVEMIND_DB_URL" -f schema/027_embedding_worker_surface.sql
-- ROLLBACK: drop function if exists hivemind_drop_embedding_chunks(bigint,text,text,text);
--           drop function if exists hivemind_upsert_embedding_chunks(jsonb);
--           drop function if exists hivemind_embedding_payload(text,text,text,int,int);
--           drop function if exists hivemind_chunk_text(text,int,int);
--           drop function if exists hivemind_trailing_lines(text[],int);
-- ============================================================

do $$
begin
    if not exists (select 1 from pg_catalog.pg_tables where schemaname='public' and tablename='content_embeddings') then
        raise exception 'prerequisite guard: content_embeddings not found — apply schema/022 (S1) first';
    end if;
end $$;

-- ---------------------------------------------------------------------------
-- Deterministic line-window chunker (no silent truncation)
-- ---------------------------------------------------------------------------
create or replace function hivemind_trailing_lines(p_lines text[], p_overlap_chars int)
returns text[]
language plpgsql immutable
set search_path = public
as $$
declare
  out text[] := array[]::text[];
  acc int := 0;
  j int;
  ln text;
begin
  -- Keep the trailing contiguous lines whose cumulative length fits the overlap.
  if p_overlap_chars <= 0 or p_lines is null then return out; end if;
  j := coalesce(array_length(p_lines,1),0);
  while j >= 1 loop
    ln := p_lines[j];
    if acc + length(ln) + 1 <= p_overlap_chars then
      out := array_prepend(ln, out);
      acc := acc + length(ln) + 1;
      j := j - 1;
    else
      exit;
    end if;
  end loop;
  return out;
end;
$$;

create or replace function hivemind_chunk_text(
    p_text text,
    p_target_tokens int default 512,
    p_overlap_tokens int default 50
) returns table(chunk_index int, chunk_text text, chunk_hash text, method text)
language plpgsql immutable
set search_path = public
as $$
declare
    norm text := hivemind_normalize_for_hash(p_text);
    target_chars int := greatest(p_target_tokens, 1) * 4;
    overlap_chars int := greatest(p_overlap_tokens, 0) * 4;
    lines text[] := string_to_array(norm, e'\n');
    n int := coalesce(array_length(lines, 1), 0);
    i int := 1;
    acc text[] := array[]::text[];
    acc_chars int := 0;
    chunk_texts text[] := array[]::text[];
    t text;
    idx int := 0;
begin
    if norm = '' then
        return;
    end if;
    if length(norm) <= target_chars then
        return query select 0, norm, hivemind_representation_hash(norm), 'single';
        return;
    end if;
    while i <= n loop
        if acc_chars > 0 and acc_chars + length(lines[i]) + 1 > target_chars then
            chunk_texts := array_append(chunk_texts, array_to_string(acc, e'\n'));
            acc := hivemind_trailing_lines(acc, overlap_chars);
            acc_chars := length(array_to_string(acc, e'\n'));
        end if;
        acc := array_append(acc, lines[i]);
        acc_chars := acc_chars + length(lines[i]) + 1;
        i := i + 1;
    end loop;
    if coalesce(array_length(acc, 1), 0) > 0 then
        chunk_texts := array_append(chunk_texts, array_to_string(acc, e'\n'));
    end if;
    foreach t in array chunk_texts loop
        if btrim(t) <> '' then
            return query select idx, btrim(t), hivemind_representation_hash(btrim(t)), 'window';
            idx := idx + 1;
        end if;
    end loop;
end;
$$;

comment on function hivemind_chunk_text(text,int,int) is
    'Task 2.9: deterministic line-window chunker used by the embedding worker. '
    'Single chunk when the source fits the budget; never silent truncation.';

-- ---------------------------------------------------------------------------
-- Canonical-chunk payload for a claimed job (reads CURRENT source)
-- ---------------------------------------------------------------------------
create or replace function hivemind_embedding_payload(
    p_entity_type text,
    p_item_id text,
    p_representation_type text,
    p_target_tokens int default 512,
    p_overlap_tokens int default 50
) returns table(
    representation_hash text,
    source_available boolean,
    chunk_index int,
    chunk_text text,
    chunk_hash text,
    method text
)
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    canonical text := '';
    avail boolean := false;
begin
    if p_entity_type = 'message' then
        select coalesce(m.content, '') into canonical
          from discord_messages m where m.message_id = p_item_id::bigint
             and coalesce(m.is_deleted, false) = false;
        avail := canonical is not null and found and btrim(canonical) <> '';

    elsif p_entity_type = 'distillation' then
        select hivemind_canonical_distillation_text(d.question, d.conditions, d.answer)
          into canonical
          from distillations d
         where d.id = p_item_id::bigint and d.status in ('pending', 'approved');
        avail := found;

    elsif p_entity_type = 'resource' and p_representation_type = 'prose' then
        select hivemind_canonical_resource_text(
                   r.title,
                   hivemind_workflow_prose(r.body, r.kind),
                   hivemind_resource_tags(r.metadata) || ' ' || hivemind_workflow_semantics_text(r.metadata))
          into canonical
          from external_resources r
         where r.id = p_item_id::bigint;
        avail := found and btrim(canonical) <> '';

    elsif p_entity_type = 'resource' and p_representation_type = 'workflow_python' then
        -- Authoritative payload.python_source (the remediation job, task 2.12,
        -- materializes this; body-block recovery is that job's responsibility).
        select coalesce(r.payload->>'python_source', '') into canonical
          from external_resources r
         where r.id = p_item_id::bigint and r.kind = 'workflow';
        avail := found and btrim(canonical) <> '';
    end if;

    if not avail or canonical is null or btrim(canonical) = '' then
        -- Nothing to embed (deleted/ineligible/empty/unavailable): signal the
        -- worker to complete with zero chunks (or drop, per job_kind).
        return query select ''::text, false, null::int, null::text, null::text, null::text;
        return;
    end if;

    return query
    select hivemind_representation_hash(canonical), true, c.chunk_index, c.chunk_text,
           c.chunk_hash, c.method
      from hivemind_chunk_text(canonical, p_target_tokens, p_overlap_tokens) c;
end;
$$;

comment on function hivemind_embedding_payload(text,text,text,int,int) is
    'Task 2.9: canonical chunks to embed for a claimed job, read from CURRENT '
    'source. SECURITY DEFINER; fixed search_path. source_available=false means '
    'the item is empty/ineligible (worker completes with zero chunks).';

-- ---------------------------------------------------------------------------
-- Atomic replace of one (contract,entity,item,representation)'s vectors
-- ---------------------------------------------------------------------------
create or replace function hivemind_upsert_embedding_chunks(p_chunks jsonb)
returns int
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    c jsonb;
    v_contract bigint;
    v_et text;
    v_id text;
    v_rep text;
    n int := 0;
begin
    if p_chunks is null or jsonb_typeof(p_chunks) <> 'array' or jsonb_array_length(p_chunks) = 0 then
        return 0;
    end if;
    c := p_chunks -> 0;
    v_contract := (c ->> 'contract_id')::bigint;
    v_et := c ->> 'entity_type';
    v_id := c ->> 'item_id';
    v_rep := c ->> 'representation_type';
    -- Atomic replace: delete the identity's existing rows for this contract, then
    -- insert the new chunk set. A wrong-dimension vector raises here (vector(384)
    -- + the dimension trigger), aborting the whole replace in one transaction.
    delete from content_embeddings
     where contract_id = v_contract and entity_type = v_et
       and item_id = v_id and representation_type = v_rep;
    for c in select * from jsonb_array_elements(p_chunks) loop
        insert into content_embeddings (
            contract_id, entity_type, item_id, representation_type, chunk_index,
            chunk_text, embedding, representation_hash, chunk_hash
        ) values (
            v_contract, v_et, v_id, v_rep, (c ->> 'chunk_index')::int,
            left(coalesce(c ->> 'chunk_text', ''), 1024),
            (c ->> 'embedding')::vector,
            c ->> 'representation_hash',
            c ->> 'chunk_hash'
        );
        n := n + 1;
    end loop;
    return n;
end;
$$;

comment on function hivemind_upsert_embedding_chunks(jsonb) is
    'Task 2.9: atomically replace one representation chunk set + vectors. '
    'vector(384) + the dimension trigger reject a wrong-dimension vector.';

-- ---------------------------------------------------------------------------
-- Drop an item's vectors (drop jobs / cleanup / task 2.10)
-- ---------------------------------------------------------------------------
create or replace function hivemind_drop_embedding_chunks(
    p_contract_id bigint,
    p_entity_type text,
    p_item_id text,
    p_representation_type text
) returns int
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare v_count int;
begin
    with d as (
        delete from content_embeddings
         where contract_id = p_contract_id
           and entity_type = p_entity_type
           and item_id = p_item_id
           and (p_representation_type is null or representation_type = p_representation_type)
        returning 1
    )
    select count(*) into v_count from d;
    return v_count;
end;
$$;

-- ---------------------------------------------------------------------------
-- Revoke the write surfaces from public + Supabase roles where present.
-- ---------------------------------------------------------------------------
do $$
declare
    sig text;
    role_to_revoke text;
    sigs text[] := array[
        'hivemind_embedding_payload(text,text,text,int,int)',
        'hivemind_upsert_embedding_chunks(jsonb)',
        'hivemind_drop_embedding_chunks(bigint,text,text,text)'
    ];
begin
    foreach sig in array sigs loop
        execute format('revoke execute on function %I.%s from public;', 'public', sig);
        foreach role_to_revoke in array array['anon','authenticated'] loop
            if exists (select 1 from pg_catalog.pg_roles where rolname = role_to_revoke) then
                execute format('revoke execute on function %I.%s from %I;', 'public', sig, role_to_revoke);
            end if;
        end loop;
    end loop;
end $$;

-- ---------------------------------------------------------------------------
-- Verification (read-only).
-- ---------------------------------------------------------------------------
select
    p.proname as fn,
    p.prosecdef as security_definer,
    p.proconfig as config
from pg_catalog.pg_proc p
join pg_catalog.pg_namespace n on n.oid = p.pronamespace
where n.nspname = 'public'
  and p.proname in ('hivemind_chunk_text','hivemind_embedding_payload',
                    'hivemind_upsert_embedding_chunks','hivemind_drop_embedding_chunks')
order by p.proname;
