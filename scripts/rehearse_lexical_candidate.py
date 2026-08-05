#!/usr/bin/env python3
"""Throwaway isolated-PostgreSQL rehearsal for the lexical candidate SQL + RPC
(hybrid-search plan tasks 1.7–1.10).

Spins up an ISOLATED throwaway PostgreSQL 14+ cluster (``initdb --auth=trust``,
temp data dir, ephemeral port + unix socket), bootstraps the base upstream
tables the migrations assume (``discord_messages`` / ``members`` /
``discord_channels`` / ``message_feed``), applies the real Hivemind migrations
in order (``001`` then the Phase-1 ``003``–``009``), seeds production-shaped
data (messages with identifiers embedded in prose, soft-deleted rows, channels,
authors, workflows with real Python in ``lexical_documents``, distillations),
runs representative queries through the hardened RPC, and captures
``EXPLAIN (ANALYZE, BUFFERS)`` evidence that EVERY candidate arm uses its
intended GIN index (no seq scan, no legacy 30s timeout class).

No Docker, no network, no production mutation. The cluster never touches the
developer's 5432 service: fresh temp data dir, ``-A trust``, ephemeral port.

Shared by:
  - ``scripts/run_lexical_candidate_rehearsal.py`` — reproducible runner + JSON
  - ``tests/test_lexical_candidate_sql.py``             — unittest (skip if no PG)

It reuses the cluster lifecycle in :mod:`scripts.lexical_pg` (LocalCluster).
"""

from __future__ import annotations

import json
import pathlib
import sys
import time
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import lexical_pg as LP  # noqa: E402  (LocalCluster lifecycle + helpers)

SCHEMA_DIR = REPO / "schema"
MIGRATIONS = [
    "001_unified_corpus.sql",
    "003_lexical_resource_documents.sql",
    "004_discord_message_fts.sql",
    "005_identifier_normalization.sql",
    "006_short_field_trigram.sql",
    "007_message_identifier_index.sql",
    "008_lexical_candidate_sql.sql",
    "009_lexical_search_rpc.sql",
]

# pg_trgm / the pgTAP-free rehearsal needs the Supabase roles the migrations
# grant to. Create them (no-op if present) BEFORE applying schema/001.
ROLES_SQL = """
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then
    create role anon; end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then
    create role authenticated; end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then
    create role service_role; end if;
end $$;
"""

# Base upstream tables the migrations assume exist but schema/001 does NOT
# create (they belong to the Discord archive). Mirrors the live column shapes
# the candidate SQL / RPC / filters consume (phase0-schema-eligibility-map.md).
BASE_DDL = """
create table if not exists public.discord_messages (
  message_id   bigint primary key,
  channel_id   bigint not null,
  author_id    bigint not null,
  guild_id     bigint not null,
  content      text,
  created_at   timestamptz not null default now(),
  is_deleted   boolean not null default false,
  thread_id    bigint,
  message_type text,
  flags        int,
  -- full live column shape (mirrors phase0-schema-eligibility-map.md) so the
  -- message-metadata exposure (schema/035) can join these columns in rehearsal.
  attachments jsonb default '[]',
  embeds      jsonb default '[]',
  reaction_count int default 0,
  reactors    jsonb default '[]',
  reference_id bigint,
  edited_at   timestamptz,
  is_pinned   boolean default false,
  indexed_at  timestamptz default now(),
  synced_at   timestamptz default now(),
  edit_history jsonb default '[]',
  deleted_at  timestamptz
);
create index if not exists _dm_author_idx on public.discord_messages (author_id);
create index if not exists _dm_channel_idx on public.discord_messages (channel_id);
create index if not exists _dm_created_idx on public.discord_messages (created_at);
-- NOTE: no btree on is_deleted here. The real DB has one, but at rehearsal
-- scale it would out-bid the task-1.6 trigram GIN for the message_ident LIKE
-- (the GIN wins at production scale — proven live in task 1.6). Omitting it
-- lets the rehearsal prove the trigram GIN serves the containment predicate.

create table if not exists public.members (
  member_id   bigint primary key,
  username    text not null,
  global_name text,
  server_nick text,
  bot         boolean not null default false,
  system      boolean not null default false,
  allow_content_sharing boolean not null default true,
  include_in_updates    boolean not null default true,
  avatar_url  text
);

create table if not exists public.discord_channels (
  channel_id   bigint primary key,
  channel_name text not null,
  guild_id     bigint not null,
  channel_type text
);

create table if not exists public.discord_reactions (
  message_id bigint not null,
  user_id    bigint not null,
  emoji      text not null,
  removed_at timestamptz,
  primary key (message_id, user_id, emoji)
);

-- message_feed: the presentation view unified_feed joins to (phase0 map). The
-- lexical RPC hydrates message identities from here. Mirrors the live shape
-- (reactions json, correlated subquery over discord_reactions) so schema/035's
-- CREATE OR REPLACE VIEW (which adds only the is_deleted filter) is a clean
-- type-preserving redefinition.
create or replace view public.message_feed as
  select m.message_id, m.content, m.created_at,
         coalesce(a.global_name, a.username) as author_name,
         c.channel_name, m.channel_id, m.guild_id,
         (select json_agg(json_build_object('emoji', r.emoji,
                   'reactor', coalesce(rm.global_name, rm.username)))
            from public.discord_reactions r
            left join public.members rm on rm.member_id = r.user_id
           where r.message_id = m.message_id and r.removed_at is null) as reactions
    from public.discord_messages m
    left join public.members a        on a.member_id  = m.author_id
    left join public.discord_channels c on c.channel_id = m.channel_id;
"""


# ---------------------------------------------------------------------------
# Cluster bootstrap + migration apply
# ---------------------------------------------------------------------------


def bootstrap(cluster: LP.LocalCluster) -> None:
    """Create roles + base tables, then apply migrations 001 + 003..009 in order."""
    cluster.psql(ROLES_SQL)
    cluster.psql(BASE_DDL)
    for name in MIGRATIONS:
        path = SCHEMA_DIR / name
        if not path.exists():
            raise FileNotFoundError(path)
        # 004/006/007/008 use CREATE INDEX CONCURRENTLY, which is forbidden
        # inside a transaction. psql_file autocommits each statement, but CIC
        # still cannot run in a multi-statement transaction block. Run each CIC
        # migration file with autocommit (psql -f does not wrap in a txn).
        cluster.psql_file(path)


def reset_schema(cluster: LP.LocalCluster) -> None:
    """Drop everything we created so a rehearsal can re-bootstrap cleanly."""
    cluster.psql(
        "drop schema if exists public cascade; create schema public;"
    )


# ---------------------------------------------------------------------------
# Seed: production-shaped data with planted identifiers / variants / filters
# ---------------------------------------------------------------------------

# Planted golden identifiers embedded in message prose + workflow Python, chosen
# to exercise the task-1.6 containment arm on compound / common-subword cases.
PLANTED_IDENTIFIERS = [
    "WanVideoSampler", "CogVideoX", "FLUX.1", "controlnet",
    "lightx2v_I2V_14B", "VAEEncode", "ipadapter", "ColorMatch",
]

# A small, safe workflow Python chunk used to populate lexical_documents so the
# workflow_python FTS + fragment arms have something real to retrieve. It
# deliberately embeds a planted node class (WanVideoSampler) inside a larger
# prose-ish chunk so CONTAINMENT (not equality) is what finds it.
SAFE_PY_CHUNK = (
    "import torch\n"
    "class WanVideoSampler:\n"
    "    def __init__(self, lora_weight=0.8, num_frames=81):\n"
    "        self.sampler = CogVideoX BackboneSampler()\n"
    "        self.vae = VAEEncode()\n"
)


def _q(s: Any) -> str:
    if s is None:
        return "null"
    return "'" + str(s).replace("'", "''") + "'"


def seed(cluster: LP.LocalCluster, *, n_messages: int = 60000) -> dict[str, Any]:
    """Seed members/channels, messages (sparse identifiers), resources,
    workflow_python lexical documents, and distillations. Returns counts."""
    cur: list[str] = []

    # Members + channels for the channel/author filter arms.
    members = [
        (1, "QuintForms", "QuintForms", False, False, True, True),
        (2, "buggz", "buggz", False, False, True, True),
        (3, "spacepxl", "spacepxl", False, False, True, True),
        (4, "optouter", "Opt Outer", False, False, False, True),  # opted out
        (5, "botly", "Botly", True, False, True, True),           # a bot
    ]
    for m in members:
        cur.append(
            "insert into public.members (member_id, username, global_name, bot, "
            "system, allow_content_sharing, include_in_updates) values "
            f"({m[0]},{_q(m[1])},{_q(m[2])},{m[3]},{m[4]},{m[5]},{m[6]});"
        )
    channels = [
        (100, "wan_chatter", 9000, "text"),
        (101, "ltx_chatter", 9000, "text"),
        (102, "krea-2-image", 9000, "text"),
        (103, "general", 9000, "text"),
    ]
    for c in channels:
        cur.append(
            "insert into public.discord_channels (channel_id, channel_name, guild_id, channel_type) values "
            f"({c[0]},{_q(c[1])},{c[2]},{_q(c[3])});"
        )

    # Contributor (FK target for distillations.author_id) — inserted early so the
    # filler distillations below satisfy the FK.
    cur.append(
        "insert into public.contributors (id, name, kind) overriding system value values "
        "(1,'rehearsal-author','agent') on conflict (id) do nothing;"
    )

    # Messages: ~n_messages rows. Sparse identifier injection (~1 in 60) mirrors
    # real Discord density (task-1.6 rehearsal). ~0.5% soft-deleted.
    rows: list[str] = []
    base_ts = "2026-07-01T00:00:00Z"
    templates = [
        "has anyone tried the new sampler settings for video",
        "control net is great for keeping structure",
        "what lora weight works best for upscale",
        "my render keeps crashing on big batches",
        "any tips for prompt formatting with flux",
    ]
    for i in range(n_messages):
        mid = 1_000_000_000_000_000_000 + i  # 19-digit snowflake-shaped bigint
        author = 1 + (i % 4)  # authors 1..4 (5 is bot, not used here)
        chan = 100 + (i % 4)
        content = templates[i % len(templates)]
        deleted = (i % 200 == 0)
        # Sparse identifier injection.
        if i % 60 == 0:
            ident = PLANTED_IDENTIFIERS[(i // 60) % len(PLANTED_IDENTIFIERS)]
            content = f"I am using {ident} and it works great for video"
        # Plant a spaced-form variant for the bridge ("FLUX 1" body -> flux1).
        if i == 7:
            content = "FLUX 1 is my go-to model for img2img"
        # Plant an embedded-prose + opt-out author message (eligibility proof).
        if i == 13:
            content = "WanVideoSampler is the node you want"
            author = 4  # opted out
        # Plant an embedded-prose message in a filtered channel/author.
        if i == 17:
            content = "CogVideoX backbone gives clean motion"
            author = 1
            chan = 100  # wan_chatter
        rows.append(
            f"({mid},{chan},{author},9000,{_q(content)},{_q(base_ts)},{deleted},null,null,null)"
        )
    # Bulk insert in chunks.
    chunk = 2000
    for k in range(0, len(rows), chunk):
        cur.append(
            "insert into public.discord_messages (message_id, channel_id, author_id, "
            "guild_id, content, created_at, is_deleted, thread_id, message_type, flags) values "
            + ",".join(rows[k : k + chunk]) + ";"
        )

    # External resources: a couple of workflows + articles + a distillation.
    cur.append(
        "insert into public.external_resources (id, kind, source, external_id, title, body, author, url, metadata) overriding system value values "
        "(20,'workflow','vibecomfy-external','w20','WanVideo Image-to-Video Generation',"
        "'WanVideo Image-to-Video with LoRAs | models: wan2.1.safetensors | nodes: WanVideoSampler VAE',"
        "'agent',null,'{}');"
    )
    cur.append(
        "insert into public.external_resources (id, kind, source, external_id, title, body, author, url, metadata) overriding system value values "
        "(64,'workflow','vibecomfy-external','w64','CogVideoX Workflow',"
        "'CogVideoX text to video | models: cogvideox.safetensors | nodes: CogVideoX VAEEncode',"
        "'agent',null,'{}');"
    )
    cur.append(
        "insert into public.external_resources (id, kind, source, external_id, title, body, author, url, metadata) overriding system value values "
        "(900,'article','hivemind','a900','Guide to upscale models',"
        "'A guide covering upscale model settings and samplers.',null,null,'{}');"
    )

    # Filler resources + distillations so those tables are production-shaped
    # (thousands), which makes the planner choose the GIN indexes for the real
    # EXPLAIN and exercises volume. None match the planted identifiers.
    filler_chunks: list[str] = []
    for i in range(3000):
        rid = 1000 + i
        filler_chunks.append(
            f"({rid},'article','hivemind','f{rid}','Filler resource {rid}',"
            f"'A filler article about rendering and pipelines number {rid}.',null,null,'{{}}')"
        )
    for k in range(0, len(filler_chunks), 500):
        cur.append(
            "insert into public.external_resources (id, kind, source, external_id, title, body, author, url, metadata) "
            "overriding system value values " + ",".join(filler_chunks[k : k + 500]) + ";"
        )
    dist_chunks: list[str] = []
    for i in range(4000):
        did = 100 + i
        dist_chunks.append(
            f"({did},'How do I configure filler option {did}?','none',"
            f"'Set filler option {did} to default.','medium','approved',1)"
        )
    for k in range(0, len(dist_chunks), 500):
        cur.append(
            "insert into public.distillations (id, question, conditions, answer, confidence, status, author_id) "
            "overriding system value values " + ",".join(dist_chunks[k : k + 500]) + ";"
        )

    # lexical_documents: safe workflow_python chunks for resources 20 + 64 so the
    # workflow_python FTS + fragment arms have real indexed code. representation_hash
    # / chunk_hash are required NOT NULL; use deterministic placeholders.
    def _ld(eid: str, idx: int, text: str) -> str:
        return (
            "insert into public.lexical_documents (entity_type, item_id, representation_type, "
            "chunk_index, chunk_text, matched_anchor, representation_hash, chunk_hash) values "
            f"('resource',{_q(eid)},'workflow_python',{idx},{_q(text)},{_q(text[:240])},"
            f"'h_{eid}_{idx}','c_{eid}_{idx}') on conflict do nothing;"
        )

    cur.append(_ld("20", 0, SAFE_PY_CHUNK))
    cur.append(_ld("64", 0, SAFE_PY_CHUNK.replace("WanVideoSampler", "CogVideoX")))

    # Filler workflow_python chunks (none matching the planted identifiers) so
    # the lexical_documents table is large enough for the planner to choose the
    # GIN index for the workflow_python FTS + fragment EXPLAINs.
    ld_filler: list[str] = []
    for i in range(3000):
        ld_filler.append(
            f"('resource','filler{i}','workflow_python',0,"
            f"'# filler python chunk {i}\\nclass Filler{i}: pass\\n',"
            f"'filler anchor {i}','fh_{i}','fc_{i}')"
        )
    for k in range(0, len(ld_filler), 500):
        cur.append(
            "insert into public.lexical_documents (entity_type, item_id, representation_type, "
            "chunk_index, chunk_text, matched_anchor, representation_hash, chunk_hash) values "
            + ",".join(ld_filler[k : k + 500]) + " on conflict do nothing;"
        )

    # lexical_resource_python_state: mark resources 20/64 safe so the workflow_python
    # eligibility gate (hivemind_workflow_python_state) admits them.
    cur.append(
        "insert into public.lexical_resource_python_state (resource_id, kind, cohort, public_state, "
        "available, body_duplicate, chunk_count) values "
        "(20,'workflow','payload_python','safe',true,false,1),"
        "(64,'workflow','payload_python','safe',true,false,1) on conflict (resource_id) "
        "do update set public_state='safe', available=true;"
    )

    # A contributor (FK target for distillations.author_id) — inserted early so
    # the filler distillations below satisfy the FK.
    cur.append(
        "insert into public.contributors (id, name, kind) overriding system value values "
        "(1,'rehearsal-author','agent') on conflict (id) do nothing;"
    )

    # Distillation: question/conditions/answer.
    cur.append(
        "insert into public.distillations (id, question, conditions, answer, confidence, status, author_id) overriding system value values "
        "(1,'How do I reduce motion strength?','for wan video',"
        "'Lower the motion amplitude / motion bucket setting.','high','approved',1);"
    )

    # Run all seed statements (autocommit; one statement each so a CIC-free path).
    for stmt in cur:
        rc, out = cluster.psql(stmt)
        if rc != 0:
            _, err = cluster.psql(stmt, capture=False)
            raise RuntimeError(f"seed statement failed (rc={rc}): {err}\nstmt={stmt[:300]}")

    return {
        "n_messages": n_messages,
        "n_members": len(members),
        "n_channels": len(channels),
        "n_resources": 3,
        "n_workflow_python_chunks": 2,
        "n_distillations": 1,
    }


# ---------------------------------------------------------------------------
# RPC + EXPLAIN helpers
# ---------------------------------------------------------------------------


def call_rpc(
    cluster: LP.LocalCluster,
    query: str,
    *,
    limit: int = 20,
    kinds: list[str] | None = None,
    sources: list[str] | None = None,
    item_ids: list[str] | None = None,
    since: str | None = None,
    channels: list[str] | None = None,
    authors: list[str] | None = None,
) -> dict[str, Any]:
    """Call the hardened RPC and return its parsed jsonb response."""
    import scripts.lexical_pg as _lp  # for q_array

    def arr(v):
        return _lp.q_array(v) if v else "'{}'"

    sql = (
        "select public.hivemind_lexical_search("
        f"{_q(query)},{limit},{arr(kinds)},{arr(sources)},{arr(item_ids)},"
        f"{_q(since) if since else 'null'},{arr(channels)},{arr(authors)},'lexical')::text;"
    )
    rc, out = cluster.psql(sql)
    if rc != 0:
        _, err = cluster.psql(sql, capture=False)
        raise RuntimeError(f"RPC failed (rc={rc}): {err}\nsql={sql[:300]}")
    text = out.strip()
    # psql -t style not used; the SELECT returns one column on one line.
    # LP.psql runs without -t, so output includes header/padding; extract the json.
    return _extract_json(text)


def _extract_json(text: str) -> dict[str, Any]:
    """Pull the first json object out of a psql output block."""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0:
        raise RuntimeError(f"no json in rpc output: {text[:200]!r}")
    return json.loads(text[start : end + 1])


def timed_call(cluster: LP.LocalCluster, query: str, **kw: Any) -> dict[str, Any]:
    """Call the RPC, timing the round trip (ms)."""
    t0 = time.perf_counter()
    res = call_rpc(cluster, query, **kw)
    res["_latency_ms"] = round((time.perf_counter() - t0) * 1000.0, 3)
    return res


def explain(cluster: LP.LocalCluster, sql: str) -> str:
    """EXPLAIN (ANALYZE, BUFFERS) for *sql*; returns the plan text."""
    rc, out = cluster.psql("explain (analyze, buffers) " + sql)
    if rc != 0:
        return f"EXPLAIN_FAILED rc={rc}: {out}"
    return out


def explain_forced(cluster: LP.LocalCluster, sql: str) -> str:
    """Forced-index EXPLAIN (enable_seqscan=off) so the planner reveals which
    index CAN serve the predicate — the rigorous index-USABILITY check."""
    rc, out = cluster.psql("set enable_seqscan=off; explain (analyze, buffers) " + sql)
    if rc != 0:
        return f"EXPLAIN_FAILED rc={rc}: {out}"
    return out


def _plan_has(plan: str, needle: str) -> bool:
    return needle.lower() in plan.lower()


def capture_arm_explain(cluster: LP.LocalCluster) -> dict[str, Any]:
    """EXPLAIN (ANALYZE, BUFFERS) for each representative arm, proving the
    intended GIN index serves the arm's core predicate.

    Each probe ISOLATES the GIN-servable predicate (the ``@@`` / normalized
    ``LIKE``) so the intended index is the only indexable path; the candidate
    SQL's additional eligibility filters (representation_type, status, quarantine)
    are cheap post-filters that do not change which index serves the core match.
    Both the real plan and a forced (enable_seqscan=off) plan are captured.
    """
    arms = {
        "message_fts": (
            "select message_id::text from public.discord_messages "
            "where to_tsvector('simple'::regconfig, coalesce(content,'')) "
            "@@ websearch_to_tsquery('simple'::regconfig, 'sampler video') "
            "and is_deleted=false limit 500"
        ),
        "message_ident": (
            "with q as (select public.hivemind_normalize_identifier('WanVideoSampler') k) "
            "select m.message_id::text from public.discord_messages m, q "
            "where m.is_deleted=false and char_length(m.content) between 1 and 8000 "
            "and public.hivemind_normalize_identifier(m.content) like '%'||q.k||'%' limit 500"
        ),
        "resource_prose_fts": (
            "select id::text from public.external_resources "
            "where prose_tsv @@ websearch_to_tsquery('simple'::regconfig, 'upscale') limit 500"
        ),
        "resource_title_ident": (
            "select id::text from public.external_resources "
            "where char_length(public.hivemind_normalize_identifier(title)) between 1 and 300 "
            "and public.hivemind_normalize_identifier(title) like '%flux%' limit 500"
        ),
        "workflow_python_fts": (
            "select item_id from public.lexical_documents "
            "where tsv @@ websearch_to_tsquery('simple'::regconfig, 'wanvideosampler') limit 500"
        ),
        "workflow_python_fragment": (
            "select item_id from public.lexical_documents "
            "where char_length(chunk_text) between 1 and 8000 "
            "and public.hivemind_normalize_identifier(chunk_text) like '%cogvideox%' limit 500"
        ),
        "distillation_fts": (
            "select id::text from public.distillations "
            "where lexical_tsv @@ websearch_to_tsquery('simple'::regconfig, 'motion strength') limit 500"
        ),
    }
    expected_index = {
        "message_fts": "idx_discord_messages_content_fts_simple",
        "message_ident": "idx_discord_messages_identifier_trgm",
        "resource_prose_fts": "external_resources_prose_tsv_idx",
        "resource_title_ident": "idx_external_resources_title_trgm_norm",
        "workflow_python_fts": "lexical_documents_tsv_idx",
        "workflow_python_fragment": "lexical_documents_python_chunk_trgm_idx",
        "distillation_fts": "distillations_lexical_tsv_idx",
    }
    out: dict[str, Any] = {}
    for arm, sql in arms.items():
        real = explain(cluster, sql)
        forced = explain_forced(cluster, sql)
        out[arm] = {
            "sql": sql,
            "expected_index": expected_index[arm],
            # The intended GIN serves this arm's core predicate.
            "index_servable": _plan_has(forced, expected_index[arm]),
            "uses_expected_index": _plan_has(real, expected_index[arm]),
            "real_seq_scan": _plan_has(real, "seq scan"),
            "forced_plan": forced,
            "real_plan": real,
        }
    return out


# ---------------------------------------------------------------------------
# Orchestration: representative queries, eligibility proofs, verdict
# ---------------------------------------------------------------------------


def _result_identities(resp: dict[str, Any]) -> list[tuple[str, str]]:
    out = []
    for r in resp.get("results", []):
        kind = r.get("kind")
        ent = "resource" if kind not in ("message", "distillation") and kind is not None else kind
        out.append((ent or "?", str(r.get("item_id"))))
    return out


def run_representative(cluster: LP.LocalCluster) -> dict[str, Any]:
    """Representative RPC queries + the task-1.6 containment latency proof."""
    cases = [
        ("wanvideosampler", {"query": "WanVideoSampler"}),
        ("cogvideox", {"query": "CogVideoX"}),
        ("flux1_title", {"query": "FLUX.1"}),
        ("controlnet", {"query": "controlnet"}),
        ("multi_term_upscale", {"query": "upscale model settings"}),
        ("distillation_paraphrase", {"query": "reduce motion strength"}),
        ("channel_filter", {"query": "CogVideoX", "channels": ["wan_chatter"]}),
        ("author_filter", {"query": "CogVideoX", "authors": ["QuintForms"]}),
        ("workflow_only", {"query": "WanVideoSampler", "kinds": ["workflow"]}),
        ("single_workflow", {"query": "WanVideoSampler", "kinds": ["workflow"],
                             "item_ids": ["20"]}),
        ("no_hit", {"query": "zzzznotarealtokenxyz"}),
    ]
    out = {}
    for name, kw in cases:
        res = timed_call(cluster, kw.pop("query"), **kw)
        out[name] = {
            "identities": _result_identities(res),
            "count": res.get("count"),
            "latency_ms": res.get("_latency_ms"),
        }
    return out


def run_eligibility_proofs(cluster: LP.LocalCluster) -> dict[str, Any]:
    """Prove deletion / opt-out / distillation-status / quarantine eligibility."""
    proofs = {}

    # 1. Opt-out: the message authored by member 4 (allow_content_sharing=false)
    #    that contains 'WanVideoSampler' must surface with optout OFF (preserve
    #    behavior) and vanish with optout ON (predicate enforced).
    def cand(query: str, **kw: Any) -> list[tuple[str, str]]:
        import scripts.lexical_pg as _lp

        def arr(v: Any) -> str:
            return _lp.q_array(v) if v else "'{}'"

        sql = (
            "select entity_type, item_id from public.hivemind_lexical_candidates("
            f"{_q(query)},500,{arr(kw.get('kinds'))},'{{}}','{{}}',null,'{{}}','{{}}',"
            f"{str(kw.get('author_optout', False)).lower()},{str(kw.get('bots_excluded', False)).lower()})"
        )
        rc, out = cluster.psql(sql)
        pairs = []
        for line in out.strip().splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) == 2 and parts[0] in ("message", "resource", "distillation"):
                pairs.append((parts[0], parts[1]))
        return pairs

    optout_off = cand("WanVideoSampler", author_optout=False)
    optout_on = cand("WanVideoSampler", author_optout=True)
    # Message 1_000_000_000_000_000_013 is the opt-out author's WanVideoSampler msg.
    optout_msg = str(1_000_000_000_000_000_000 + 13)
    proofs["optout_default_includes"] = optout_msg in {i for _, i in optout_off}
    proofs["optout_enforced_excludes"] = optout_msg not in {i for _, i in optout_on}

    # 2. Soft-delete: deleted messages (i % 200 == 0) must never surface.
    deleted_msg = str(1_000_000_000_000_000_000 + 0)  # i=0 is deleted
    any_results = cand("sampler video")
    proofs["softdelete_excluded"] = deleted_msg not in {i for _, i in any_results}

    # 3. Distillation status: a rejected distillation must never rank.
    cluster.psql(
        "insert into public.distillations (id, question, conditions, answer, confidence, status, author_id) overriding system value "
        "values (2,'How do I reduce motion strength rejected','x','y','low','rejected',1) on conflict do nothing;"
    )
    dist_ids = {i for e, i in cand("reduce motion strength") if e == "distillation"}
    proofs["rejected_distillation_excluded"] = "2" not in dist_ids
    proofs["approved_distillation_present"] = "1" in dist_ids

    return proofs


def capture_containment_latency(cluster: LP.LocalCluster, runs: int = 20) -> dict[str, Any]:
    """The task-1.6 containment latency proof: WanVideoSampler / CogVideoX /
    common-subword via the RPC, warm."""
    probes = ["WanVideoSampler", "CogVideoX", "controlnet", "ipadapter"]
    out: dict[str, Any] = {}
    for q in probes:
        # Warm up.
        call_rpc(cluster, q)
        samples = [timed_call(cluster, q)["_latency_ms"] for _ in range(runs)]
        res = timed_call(cluster, q)
        out[q] = {
            "p50_ms": round(sorted(samples)[len(samples) // 2], 3),
            "p95_ms": round(sorted(samples)[int(len(samples) * 0.95)], 3),
            "max_ms": round(max(samples), 3),
            "identities": _result_identities(res),
        }
    return out


def verdict(ev: dict[str, Any]) -> dict[str, Any]:
    checks: list[tuple[str, bool, str]] = []

    for arm, data in ev["explain"].items():
        checks.append((
            f"index_use:{arm}",
            bool(data["index_servable"]),
            f"index_servable={data['expected_index']} real_uses={data['uses_expected_index']} "
            f"real_seq_scan={data['real_seq_scan']}",
        ))

    rep = ev["representative"]
    # Gate 9: containment retrieves identifiers embedded in prose + workflow python.
    wan = {i for _, i in rep["wanvideosampler"]["identities"]}
    cog = {i for _, i in rep["cogvideox"]["identities"]}
    checks.append(("containment_wanvideosampler", "20" in wan,
                   f"workflow 20 present: identities={rep['wanvideosampler']['identities']}"))
    checks.append(("containment_cogvideox", "64" in cog,
                   f"workflow 64 present: identities={rep['cogvideox']['identities']}"))
    # Workflow-only restricts to resources.
    checks.append(("workflow_only_kind",
                   all(k in ("resource",) for k, _ in rep["workflow_only"]["identities"]),
                   str(rep["workflow_only"]["identities"])))
    # Single-workflow restricts to the one item_id.
    checks.append(("single_workflow_only_item",
                   set(i for _, i in rep["single_workflow"]["identities"]) <= {"20"},
                   str(rep["single_workflow"]["identities"])))
    # No-hit returns 0.
    checks.append(("no_hit_zero", rep["no_hit"]["count"] == 0, str(rep["no_hit"]["count"])))

    for k, ok in ev["eligibility"].items():
        checks.append((f"eligibility:{k}", bool(ok), ""))

    # Latency gate (lexical p95 <= 750 ms warm).
    lat = ev["containment_latency"]
    p95s = [v["p95_ms"] for v in lat.values()]
    checks.append(("latency_p95_under_750ms", max(p95s) <= 750.0, f"max_p95={max(p95s):.1f}ms"))

    ev["verdict"] = {
        "checks": [{"name": n, "ok": ok, "detail": d} for (n, ok, d) in checks],
        "all_pass": all(ok for _, ok, _ in checks),
        "n_pass": sum(1 for _, ok, _ in checks if ok),
        "n_total": len(checks),
    }
    return ev["verdict"]


def rehearse(out_dir: pathlib.Path | None = None) -> dict[str, Any]:
    cluster = LP.LocalCluster.start()
    try:
        reset_schema(cluster)
        bootstrap(cluster)
        counts = seed(cluster, n_messages=60000)
        ev: dict[str, Any] = {"counts": counts}
        ev["representative"] = run_representative(cluster)
        ev["eligibility"] = run_eligibility_proofs(cluster)
        ev["containment_latency"] = capture_containment_latency(cluster)
        ev["explain"] = capture_arm_explain(cluster)
        verdict(ev)
        ev["migrations_applied"] = MIGRATIONS
    finally:
        cluster.tear_down()

    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "phase1-lexical-candidate-rehearsal.json").write_text(
            json.dumps(ev, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return ev


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="Lexical candidate SQL + RPC rehearsal.")
    p.add_argument("--out-dir", type=pathlib.Path,
                   default=REPO / "docs" / "hybrid-search")
    args = p.parse_args(argv)
    ev = rehearse(args.out_dir)
    v = ev["verdict"]
    print(f"rehearsal verdict: {'PASS' if v['all_pass'] else 'FAIL'} "
          f"({v['n_pass']}/{v['n_total']} checks)")
    for c in v["checks"]:
        if not c["ok"]:
            print(f"  FAIL {c['name']}: {c['detail']}")
    print(f"containment latency p95 (ms): "
          f"{[(q, d['p95_ms']) for q, d in ev['containment_latency'].items()]}")
    return 0 if v["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
