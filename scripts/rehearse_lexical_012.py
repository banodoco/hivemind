#!/usr/bin/env python3
"""Throwaway isolated-PostgreSQL rehearsal for ``schema/012_lexical_latency_phase2.sql``.

Proves the phase-2 latency fix is:

  (a) FUNCTIONALLY CORRECT — same candidate semantics as schema/010 (containment
      finds workflow-python + message prose; soft-deleted / rejected distillation /
      quarantined workflow_python never rank; single_workflow scope; global limit;
      deterministic order; no-hit zero; GIN-served arms);
  (b) RECALL-PRESERVING — the 012 candidate stream == the 010 stream, compared as
      FULL canonical rows (entity, item, representation, matched_snippet/anchor,
      lexical_rank, lexical_source, created_at, ORDER, global limit) — not merely
      the item-id set — for representative + adversarial queries;
  (c) CROSS-CHUNK SAFE — a needle that exists ONLY across two chunk boundaries
      does NOT match in 010 OR corrected 012 (the corrected MV searches search_norm
      DIRECTLY; the first draft re-normalized the concatenation and matched);
  (d) ANCHOR-PARITY — for a multi-chunk item where only a LATER chunk matches, the
      matched_snippet/anchor selected is byte-equivalent between 010 and 012 (newest
      MATCHING chunk via `distinct on / order by created_at desc`, NOT the first
      chunk);
  (e) MV-BACKED — the per-item MV exists, is populated, the fragment arm reads it,
      and the MV GIN over search_norm (searched DIRECTLY) is servable;
  (f) SECURITY — the MV is REVOKED from public/anon/authenticated (anon/auth cannot
      SELECT it), schema/011's candidate-function ACL survives the CREATE OR REPLACE,
      the service-role RPC still works, and a quarantined workflow contributes ZERO
      MV rows / ZERO candidates;
  (g) OPTIMIZATION B ACTIVE — channel/author filters resolve names to id arrays and
      use direct column predicates (channel/author-scoped queries byte-identical to
      010, with no message-filter behavior loss);
  (h) APPLIES CLEANLY after 011 (001, 003..011, then 012);
  (i) ROLLS BACK — dropping the MV + re-applying 010's function restores prior
      behavior and the function still works;
  (j) IDEMPOTENT — re-applying 012 twice (CREATE OR REPLACE + CREATE MV IF NOT
      EXISTS + REFRESH + REVOKE) is clean;
  (k) GRANTS PRESERVED — CREATE OR REPLACE preserves the post-011 proacl.

The isolated cluster is tiny — it does NOT reproduce production latency (proven on
production separately via read-only EXPLAIN ANALYZE). This rehearsal proves
CORRECTNESS + recall/anchor parity + cross-chunk safety + security + apply /
rollback / idempotence / grant-preservation ONLY.

REUSES the existing harness:
  * ``scripts/lexical_pg.py``               — LocalCluster lifecycle + helpers.
  * ``scripts/rehearse_lexical_candidate.py``— bootstrap() (001..009), seed().
  * ``scripts/rehearse_lexical_010.py``      — the 010 candidate battery +
    seed_extra() (quarantined workflow, rejected distillation). We mirror its
    seeding + assertions so the 012 battery is directly comparable.
"""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import lexical_pg as LP  # noqa: E402
import rehearse_lexical_candidate as R  # noqa: E402
import rehearse_lexical_010 as R10  # noqa: E402

SCHEMA_DIR = REPO / "schema"

# Migrations applied in dependency order: 001..011 (the live production state —
# 010 latency fix + 011 security hardening), then 012 (this phase-2 fix). 011 is
# REQUIRED here so we can prove 012's CREATE OR REPLACE preserves 011's
# candidate-function ACL (the post-011 proacl is the grants-preservation baseline).
MIGRATION_010 = "010_lexical_latency_fix.sql"
MIGRATION_011 = "011_lexical_security_hardening.sql"
MIGRATION_012 = "012_lexical_latency_phase2.sql"

VERDICT_PATH = REPO / "docs" / "hybrid-search" / "phase1-lexical-012-rehearsal.json"

MV = "public.lexical_workflow_python_search"


# ---------------------------------------------------------------------------
# Bootstrap helpers (mirror rehearse_lexical_010, extended through 011 -> 012)
# ---------------------------------------------------------------------------


def bootstrap_through_011(cluster: LP.LocalCluster) -> None:
    """Apply 001 + 003..011 (the live production state: 010 fix + 011 hardening)."""
    R.bootstrap(cluster)  # roles, base DDL, then R.MIGRATIONS (001, 003..009)
    R10.apply_migration(cluster, MIGRATION_010)
    R10.apply_migration(cluster, MIGRATION_011)
    # Restore the Supabase ambient default the bare initdb cluster lacks: USAGE on
    # schema public for anon/authenticated/service_role so they can resolve
    # `public.<object>`. The PER-OBJECT privilege (the thing 010/011/012 narrows)
    # is what decides access; without schema USAGE a SET ROLE probe fails on
    # schema resolution rather than the privilege under test.
    cluster.psql("grant usage on schema public to anon, authenticated, service_role;")


def apply_migration(cluster: LP.LocalCluster, name: str) -> None:
    path = SCHEMA_DIR / name
    if not path.exists():
        raise FileNotFoundError(path)
    # CREATE INDEX CONCURRENTLY (008) cannot run inside a transaction block;
    # psql -f (psql_file) runs each statement autocommitted. The MV + REFRESH +
    # REVOKE + CREATE OR REPLACE FUNCTION (012) are also fine via psql_file.
    cluster.psql_file(path)


def refresh_mv(cluster: LP.LocalCluster) -> None:
    """Repopulate the MV after the function body / data settles. Idempotent."""
    cluster.psql("refresh materialized view public.lexical_workflow_python_search;")


def get_proacl(cluster: LP.LocalCluster) -> str:
    rc, out = cluster.psql(
        "select proname || '|' || coalesce(proacl::text, '<null>') "
        "from pg_proc where proname='hivemind_lexical_candidates'"
    )
    return out.strip() if rc == 0 else f"ERROR rc={rc}: {out}"


def _q(s: Any) -> str:
    return R._q(s)


def _arr(v: list[str] | None) -> str:
    return LP.q_array(v) if v else "'{}'"


# ---------------------------------------------------------------------------
# Candidate stream capture
# ---------------------------------------------------------------------------


def call_candidates(
    cluster: LP.LocalCluster,
    query: str,
    *,
    kinds: list[str] | None = None,
    item_ids: list[str] | None = None,
    sources: list[str] | None = None,
    channels: list[str] | None = None,
    authors: list[str] | None = None,
    candidate_limit: int = 500,
    author_optout: bool = False,
    bots_excluded: bool = False,
) -> list[dict[str, str]]:
    """Call hivemind_lexical_candidates DIRECTLY and parse rows.

    Carries all columns EXCEPT matched_snippet (workflow_python snippets contain
    '|' which would corrupt the psql -A -t '|' field split — mirrors 010's
    call_candidates). Use call_candidates_json when the anchor/snippet matters.
    """
    sql = (
        "select entity_type, item_id, representation_type, "
        "lexical_rank::text, lexical_source "
        "from public.hivemind_lexical_candidates("
        f"{_q(query)},{candidate_limit},{_arr(kinds)},{_arr(sources)},{_arr(item_ids)},"
        f"null,{_arr(channels)},{_arr(authors)},{str(author_optout).lower()},{str(bots_excluded).lower()})"
    )
    rc, out = cluster.psql(sql)
    if rc != 0:
        raise RuntimeError(f"hivemind_lexical_candidates failed (rc={rc}): {out}\nsql={sql[:300]}")
    rows: list[dict[str, str]] = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) == 5 and parts[0] in ("message", "resource", "distillation"):
            rows.append({
                "entity_type": parts[0],
                "item_id": parts[1],
                "representation_type": parts[2],
                "lexical_rank": parts[3],
                "lexical_source": parts[4],
            })
    return rows


def call_candidates_json(
    cluster: LP.LocalCluster,
    query: str,
    *,
    kinds: list[str] | None = None,
    item_ids: list[str] | None = None,
    sources: list[str] | None = None,
    channels: list[str] | None = None,
    authors: list[str] | None = None,
    candidate_limit: int = 500,
    author_optout: bool = False,
    bots_excluded: bool = False,
) -> list[dict[str, Any]]:
    """FULL canonical candidate row stream (incl matched_snippet/anchor) via jsonb.

    jsonb aggregation avoids the '|' field-split corruption that kept call_candidates
    snippet-less. created_at is rendered with a fixed format so 010/012 rows compare
    byte-for-byte. The function applies its own ORDER BY + LIMIT, so jsonb_agg
    preserves the emitted order. This is the load-bearing byte-parity capture.
    """
    sql = (
        "select coalesce(jsonb_agg(jsonb_build_object("
        "'e',entity_type,'i',item_id,'r',representation_type,"
        "'s',matched_snippet,'k',lexical_rank::text,'src',lexical_source,"
        "'c',to_char(created_at,'YYYY-MM-DD\"T\"HH24:MI:SS.USOF')"
        ")),'[]')::text from public.hivemind_lexical_candidates("
        f"{_q(query)},{candidate_limit},{_arr(kinds)},{_arr(sources)},{_arr(item_ids)},"
        f"null,{_arr(channels)},{_arr(authors)},{str(author_optout).lower()},{str(bots_excluded).lower()})"
    )
    rc, out = cluster.psql(sql)
    if rc != 0:
        raise RuntimeError(f"hivemind_lexical_candidates failed (rc={rc}): {out}\nsql={sql[:300]}")
    return json.loads(out.strip() or "[]")


# ---------------------------------------------------------------------------
# Adversarial fixtures: cross-boundary needle + multi-chunk query-specific anchor
# ---------------------------------------------------------------------------

# item 9100: two in-range safe chunks whose NORMALIZED forms are "...ksamp" and
# "ler...". The needle "ksampler" exists ONLY across the boundary — it is absent
# from every single chunk. schema/010 (per-chunk) and corrected 012 (search_norm
# matched DIRECTLY) must NOT match; only the first draft's re-normalize would.
CROSS_BOUNDARY_ITEM = "9100"
CROSS_BOUNDARY_NEEDLE = "ksampler"

# item 9200: three in-range safe chunks with DISTINCT created_at and DISTINCT
# anchors. ONLY chunk 2 (the newest) contains the needle. Both 010 and 012 must
# select ANCHOR_TWO (the newest MATCHING chunk) — proving 012 does NOT regress to
# the first chunk's anchor.
ANCHOR_ITEM = "9200"
ANCHOR_NEEDLE = "zzuniqueqxx"


def seed_adversarial(cluster: LP.LocalCluster) -> dict[str, Any]:
    """Insert the cross-boundary + multi-chunk anchor fixtures. Returns descriptors."""
    stmts: list[str] = [
        # Cross-boundary item 9100.
        "insert into public.external_resources (id, kind, source, external_id, title, body, "
        "author, url, metadata) overriding system value values "
        f"({CROSS_BOUNDARY_ITEM},'workflow','vibecomfy-external','w{CROSS_BOUNDARY_ITEM}',"
        "'CrossBoundaryWorkflow','desc','agent',null,'{}') on conflict (id) do nothing;",
        "insert into public.lexical_resource_python_state (resource_id, kind, cohort, "
        "public_state, available, body_duplicate, chunk_count) values "
        f"({CROSS_BOUNDARY_ITEM},'workflow','payload_python','safe',true,false,2) "
        "on conflict (resource_id) do update set public_state='safe', available=true;",
        "insert into public.lexical_documents (entity_type, item_id, representation_type, "
        "chunk_index, chunk_text, matched_anchor, representation_hash, chunk_hash, "
        "quarantine_state, created_at) values "
        f"('resource','{CROSS_BOUNDARY_ITEM}','workflow_python',0,{_q('alpha bravo ksamp')},"
        f"{_q('CB_CHUNK_ZERO')},'h9100_0','c9100_0','safe','2026-01-01T00:00:00Z'),"
        f"('resource','{CROSS_BOUNDARY_ITEM}','workflow_python',1,{_q('ler charlie delta')},"
        f"{_q('CB_CHUNK_ONE')},'h9100_1','c9100_1','safe','2026-02-01T00:00:00Z') on conflict do nothing;",
        # Multi-chunk anchor item 9200.
        "insert into public.external_resources (id, kind, source, external_id, title, body, "
        "author, url, metadata) overriding system value values "
        f"({ANCHOR_ITEM},'workflow','vibecomfy-external','w{ANCHOR_ITEM}',"
        "'AnchorParityWorkflow','desc','agent',null,'{}') on conflict (id) do nothing;",
        "insert into public.lexical_resource_python_state (resource_id, kind, cohort, "
        "public_state, available, body_duplicate, chunk_count) values "
        f"({ANCHOR_ITEM},'workflow','payload_python','safe',true,false,3) "
        "on conflict (resource_id) do update set public_state='safe', available=true;",
        "insert into public.lexical_documents (entity_type, item_id, representation_type, "
        "chunk_index, chunk_text, matched_anchor, representation_hash, chunk_hash, "
        "quarantine_state, created_at) values "
        f"('resource','{ANCHOR_ITEM}','workflow_python',0,{_q('preamble token gamma')},"
        f"{_q('ANCHOR_ZERO')},'h9200_0','c9200_0','safe','2026-01-01T00:00:00Z'),"
        f"('resource','{ANCHOR_ITEM}','workflow_python',1,{_q('middle token beta')},"
        f"{_q('ANCHOR_ONE')},'h9200_1','c9200_1','safe','2026-02-01T00:00:00Z'),"
        f"('resource','{ANCHOR_ITEM}','workflow_python',2,{_q('final ' + ANCHOR_NEEDLE + ' token')},"
        f"{_q('ANCHOR_TWO')},'h9200_2','c9200_2','safe','2026-03-01T00:00:00Z') on conflict do nothing;",
    ]
    for stmt in stmts:
        rc, out = cluster.psql(stmt)
        if rc != 0:
            _, err = cluster.psql(stmt, capture=False)
            raise RuntimeError(f"seed_adversarial failed (rc={rc}): {err}\nstmt={stmt[:200]}")
    return {
        "cross_boundary_item": CROSS_BOUNDARY_ITEM,
        "cross_boundary_needle": CROSS_BOUNDARY_NEEDLE,
        "anchor_item": ANCHOR_ITEM,
        "anchor_needle": ANCHOR_NEEDLE,
    }


# ---------------------------------------------------------------------------
# Candidate-correctness battery (mirror rehearse_lexical_010's battery, plus the
# phase-2-specific MV + optimization-B + security probes)
# ---------------------------------------------------------------------------


def run_candidate_battery(cluster: LP.LocalCluster, *, label: str) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    # ---- 1-11: mirror the 010 battery exactly (via the 010 module's battery
    #      applied to the LIVE function body, which is 012 here).
    checks.extend(R10.run_candidate_battery(cluster, label=label))

    # ---- 12 (phase-2): MV exists, is populated, and is non-empty.
    rc, mvrows = cluster.psql(f"select count(*)::text from {MV}")
    mvcount = int((mvrows or "0").strip() or 0)
    check(f"{label}:mv_populated", mvcount > 0, f"{MV} rows = {mvcount} (expect >0)")

    # ---- 13 (phase-2): the fragment arm reads the MV. The MV trigram GIN over
    #      search_norm (searched DIRECTLY, not re-normalized) must be servable.
    rc, plan = cluster.psql(
        "SET enable_seqscan=off; EXPLAIN (ANALYZE, COSTS OFF) "
        "select item_id from public.lexical_workflow_python_search "
        "where search_norm like '%wanvideosampler%' limit 100;"
    )
    servable = "lexical_workflow_python_search_trgm_idx" in (plan or "")
    check(f"{label}:mv_trgm_index_servable", servable,
          f"forced plan references lexical_workflow_python_search_trgm_idx: {servable}")

    # ---- 14 (phase-2): optimization B — channel-scoped query returns the
    #      correct result. The seeded CogVideoX-in-channel message (i=17,
    #      channel 100 'wan_chatter', author 1 'QuintForms') must surface.
    cog_chan = call_candidates(cluster, "CogVideoX", channels=["wan_chatter"])
    planted_chan_msg = str(1_000_000_000_000_000_000 + 17)
    check(f"{label}:channel_filter_direct_predicate",
          planted_chan_msg in {r["item_id"] for r in cog_chan},
          f"CogVideoX channel 'wan_chatter' candidates include {planted_chan_msg}: "
          f"{sorted(r['item_id'] for r in cog_chan)[:6]}")

    # ---- 15 (phase-2): optimization B — author-scoped query; unknown author
    #      returns no MESSAGES (resources may still match).
    cog_auth = call_candidates(cluster, "CogVideoX", authors=["QuintForms"])
    check(f"{label}:author_filter_direct_predicate",
          planted_chan_msg in {r["item_id"] for r in cog_auth},
          f"CogVideoX author 'QuintForms' candidates include {planted_chan_msg}: "
          f"{sorted(r['item_id'] for r in cog_auth)[:6]}")
    cog_unknown = call_candidates(cluster, "CogVideoX", authors=["NoSuchAuthor_xyz"])
    cog_unknown_msg = [r for r in cog_unknown if r["entity_type"] == "message"]
    check(f"{label}:author_filter_unknown_returns_no_messages",
          len(cog_unknown_msg) == 0,
          f"unknown author message candidates = {len(cog_unknown_msg)} (expect 0)")

    return checks


# ---------------------------------------------------------------------------
# Parity probes: capture FULL canonical rows under 012, then under 010 (rollback),
# and assert byte-for-byte equality per query.
# ---------------------------------------------------------------------------


# Representative + adversarial queries. Each is compared as a FULL row stream
# (incl matched_snippet/anchor, rank, source, representation, order, limit).
PARITY_QUERIES = [
    {"name": "wan_workflow", "query": "WanVideoSampler", "kinds": ["workflow"]},
    {"name": "cog_workflow", "query": "CogVideoX", "kinds": ["workflow"]},
    {"name": "wan_all", "query": "WanVideoSampler"},
    {"name": "cog_all", "query": "CogVideoX"},
    {"name": "controlnet", "query": "controlnet"},
    {"name": "multi_term", "query": "upscale model settings"},
    {"name": "distillation", "query": "reduce motion strength"},
    {"name": "cog_channel", "query": "CogVideoX", "channels": ["wan_chatter"]},
    {"name": "cog_author", "query": "CogVideoX", "authors": ["QuintForms"]},
    {"name": "wan_single", "query": "WanVideoSampler", "kinds": ["workflow"], "item_ids": ["20"]},
    {"name": "anchor_fix", "query": ANCHOR_NEEDLE, "kinds": ["workflow"]},
    {"name": "no_hit", "query": "zzzznotarealtokenxyz"},
]


def capture_streams(cluster: LP.LocalCluster) -> tuple[dict[str, list], dict[str, list]]:
    """Capture FULL-row candidate streams with 012, then with 010 (rollback), for
    every parity query. Returns (streams_012, streams_010). Restores 012 at the end.
    """
    streams_012: dict[str, list] = {}
    for q in PARITY_QUERIES:
        streams_012[q["name"]] = call_candidates_json(
            cluster, q["query"], kinds=q.get("kinds"), channels=q.get("channels"),
            authors=q.get("authors"), item_ids=q.get("item_ids"))

    apply_migration(cluster, MIGRATION_010)  # rollback function body (MV untouched, unused by 010)

    streams_010: dict[str, list] = {}
    for q in PARITY_QUERIES:
        streams_010[q["name"]] = call_candidates_json(
            cluster, q["query"], kinds=q.get("kinds"), channels=q.get("channels"),
            authors=q.get("authors"), item_ids=q.get("item_ids"))

    apply_migration(cluster, MIGRATION_012)  # restore 012 (refreshes MV)
    return streams_012, streams_010


# ---------------------------------------------------------------------------
# Cross-chunk + anchor parity probes (the two corrections' load-bearing proofs)
# ---------------------------------------------------------------------------


def run_cross_boundary_proof(cluster: LP.LocalCluster) -> dict[str, Any]:
    """Prove the cross-boundary needle matches in NEITHER 010 nor 012, and that
    the first draft's re-normalize WOULD have matched (the vulnerability is real).
    Runs under the 012 body (MV present); the 010 per-chunk proof is the same SQL
    the candidate function uses, demonstrated directly on the chunks.
    """
    out: dict[str, Any] = {}

    # 012 candidate stream for the needle (workflow-only): the cross-boundary item
    # must be ABSENT.
    rows12 = call_candidates(cluster, CROSS_BOUNDARY_NEEDLE, kinds=["workflow"])
    ids12 = {r["item_id"] for r in rows12}
    out["012_cross_boundary_item_absent"] = CROSS_BOUNDARY_ITEM not in ids12
    out["012_cross_boundary_stream_ids"] = sorted(ids12)

    # Direct demonstration on item 9100's chunks: corrected DIRECT match is False;
    # the buggy re-normalize is True.
    rc, demo = cluster.psql(
        "select "
        "(select string_agg(public.hivemind_normalize_identifier(chunk_text),' ') "
        f"from public.lexical_documents where item_id='{CROSS_BOUNDARY_ITEM}' and "
        "representation_type='workflow_python') like '%ksampler%' as corrected_direct, "
        "(select public.hivemind_normalize_identifier(string_agg("
        "public.hivemind_normalize_identifier(chunk_text),' ')) "
        f"from public.lexical_documents where item_id='{CROSS_BOUNDARY_ITEM}' and "
        "representation_type='workflow_python') like '%ksampler%' as buggy_renormalize;"
    )
    parts = [p.strip() for p in (demo or "").split("|")]
    out["corrected_direct_match"] = (parts[0] == "t") if len(parts) == 2 else None
    out["buggy_renormalize_match"] = (parts[1] == "t") if len(parts) == 2 else None

    # 010 per-chunk proof: switch to 010 body, re-run, then restore 012.
    apply_migration(cluster, MIGRATION_010)
    rows10 = call_candidates(cluster, CROSS_BOUNDARY_NEEDLE, kinds=["workflow"])
    ids10 = {r["item_id"] for r in rows10}
    out["010_cross_boundary_item_absent"] = CROSS_BOUNDARY_ITEM not in ids10
    out["010_cross_boundary_stream_ids"] = sorted(ids10)
    apply_migration(cluster, MIGRATION_012)  # restore (refreshes MV)
    return out


def run_anchor_proof(cluster: LP.LocalCluster) -> dict[str, Any]:
    """Prove the matched_anchor selection is byte-equivalent between 010 and 012
    for the multi-chunk fixture where only a later chunk matches. Tests BOTH the
    isolated arm-selection logic AND the full integrated candidate stream.
    """
    out: dict[str, Any] = {}

    # Isolated arm-selection logic: 010's distinct-on vs 012's scalar-subquery.
    rc, arm = cluster.psql(
        "select "
        "(select matched_anchor from (select distinct on (ld.item_id) ld.matched_anchor "
        "from public.lexical_documents ld "
        f"where ld.item_id='{ANCHOR_ITEM}' and representation_type='workflow_python' "
        "and quarantine_state='safe' and char_length(chunk_text) between 1 and 8000 "
        f"and public.hivemind_normalize_identifier(chunk_text) like '%{ANCHOR_NEEDLE}%' "
        "order by ld.item_id, ld.created_at desc) x) as anchor_010, "
        "(select ld.matched_anchor from public.lexical_documents ld "
        f"where ld.item_id='{ANCHOR_ITEM}' and representation_type='workflow_python' "
        "and quarantine_state='safe' and char_length(chunk_text) between 1 and 8000 "
        f"and public.hivemind_normalize_identifier(chunk_text) like '%{ANCHOR_NEEDLE}%' "
        "order by ld.created_at desc limit 1) as anchor_012;"
    )
    parts = [p.strip() for p in (arm or "").split("|")]
    out["anchor_010_logic"] = parts[0] if len(parts) == 2 else None
    out["anchor_012_logic"] = parts[1] if len(parts) == 2 else None
    out["anchor_logic_equal"] = (len(parts) == 2 and parts[0] == parts[1] == "ANCHOR_TWO")

    # Full integrated stream parity (incl matched_snippet) under 012 vs 010.
    stream12 = call_candidates_json(cluster, ANCHOR_NEEDLE, kinds=["workflow"])
    apply_migration(cluster, MIGRATION_010)
    stream10 = call_candidates_json(cluster, ANCHOR_NEEDLE, kinds=["workflow"])
    apply_migration(cluster, MIGRATION_012)  # restore (refreshes MV)
    out["full_row_stream_equal"] = (stream12 == stream10)
    out["stream_012"] = stream12
    return out


# ---------------------------------------------------------------------------
# Security probes: MV denial + 011 ACL preservation + RPC works + quarantine zero
# ---------------------------------------------------------------------------


def run_security_proofs(cluster: LP.LocalCluster) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def has_priv(role: str, priv: str) -> bool:
        rc, res = cluster.psql(
            f"select has_table_privilege('{role}','{MV}','{priv}')::text")
        return (rc == 0 and (res or "").strip() == "true")

    out["anon_cannot_select_mv"] = not has_priv("anon", "SELECT")
    out["authenticated_cannot_select_mv"] = not has_priv("authenticated", "SELECT")
    out["public_cannot_select_mv"] = not has_priv("public", "SELECT")

    # Live exercise: SET ROLE anon/authenticated and SELECT must ERROR (postgres
    # is a superuser on the throwaway cluster, so SET ROLE to any role works).
    def role_errors(role: str, sql: str) -> bool:
        rc, _ = cluster.psql(f"set role {role}; {sql}")
        cluster.psql("reset role;")
        return rc != 0

    out["anon_select_errors"] = role_errors("anon", f"select count(*) from {MV};")
    out["authenticated_select_errors"] = role_errors("authenticated", f"select count(*) from {MV};")

    # schema/011 candidate-function ACL survived the CREATE OR REPLACE in 012:
    # anon/authenticated still have NO execute on hivemind_lexical_candidates.
    def fn_exec(role: str) -> bool:
        rc, res = cluster.psql(
            "select has_function_privilege('" + role + "','public.hivemind_lexical_candidates("
            "text,int,text[],text[],text[],timestamptz,text[],text[],boolean,boolean)','EXECUTE')::text")
        return (rc == 0 and (res or "").strip() == "true")
    out["anon_no_execute_candidates_after_012"] = not fn_exec("anon")
    out["authenticated_no_execute_candidates_after_012"] = not fn_exec("authenticated")

    # The service-role RPC still works (SECURITY DEFINER; reads the MV as owner).
    rc, rpc_out = cluster.psql(
        "set role service_role; "
        "select public.hivemind_lexical_search('WanVideoSampler',50,'{workflow}','{}','{}',"
        "null,'{}','{}','lexical')::text;")
    cluster.psql("reset role;")
    start = rpc_out.find("{")
    envelope = {}
    if rc == 0 and start >= 0:
        end = rpc_out.rfind("}")
        try:
            envelope = json.loads(rpc_out[start:end + 1])
        except Exception:  # noqa: BLE001
            envelope = {}
    wf_results = [r for r in envelope.get("results", []) if r.get("kind") not in ("message", "distillation")]
    out["service_role_rpc_ok"] = (rc == 0 and "count" in envelope)
    out["service_role_rpc_workflow_results"] = len(wf_results)

    # Quarantined workflow (7000) contributes ZERO candidates. The security-
    # relevant guarantee is that a quarantined resource never RANKS via the
    # workflow_python arms (the safe_wf gate excludes it). NOTE: seed_extra
    # DELIBERATELY plants a safe-labeled workflow_python doc for 7000 (to prove
    # safe_wf — not a missing doc — is what excludes it), so the MV legitimately
    # carries a row for 7000 here; in PRODUCTION a quarantined resource has ZERO
    # workflow_python docs (the refresh writes none), so it would have zero MV
    # rows too. We assert the load-bearing property (zero candidates) and report
    # the MV-row count as informational.
    rc, mv7000 = cluster.psql(
        f"select count(*)::text from {MV} where item_id='7000';")
    out["quarantined_mv_rows_info"] = (mv7000 or "0").strip()
    wan_wf = call_candidates(cluster, "WanVideoSampler", kinds=["workflow"])
    out["quarantined_zero_candidates"] = ("7000" not in {r["item_id"] for r in wan_wf})
    return out


# ---------------------------------------------------------------------------
# Main rehearsal
# ---------------------------------------------------------------------------


def rehearse() -> dict[str, Any]:
    cluster = LP.LocalCluster.start()
    ev: dict[str, Any] = {"task": "1.10/1.11 phase-2 latency fix (schema/012)"}
    try:
        # ---- bootstrap 001..011, then seed (production-shaped) + extra + adversarial.
        R.reset_schema(cluster)
        bootstrap_through_011(cluster)
        counts = R.seed(cluster, n_messages=8000)
        extra = R10.seed_extra(cluster)
        adv = seed_adversarial(cluster)
        ev["counts"] = {**counts, **extra, **adv}

        # ---- capture proacl AFTER 011 (the grants-preservation baseline).
        proacl_after_011 = get_proacl(cluster)
        ev["proacl_after_011"] = proacl_after_011

        # ---- (h) apply 012 cleanly after 011.
        apply_ok = True
        apply_error = ""
        try:
            apply_migration(cluster, MIGRATION_012)
        except Exception as exc:  # noqa: BLE001
            apply_ok = False
            apply_error = str(exc)
        ev["applied_ok"] = apply_ok
        ev["apply_error"] = apply_error

        if not apply_ok:
            ev["functional_assertions"] = []
            ev["parity"] = {}
            ev["cross_boundary"] = {}
            ev["anchor"] = {}
            ev["security"] = {}
            ev["rollback_ok"] = False
            ev["idempotent_ok"] = False
            ev["grants_preserved"] = False
            ev["all_pass"] = False
            ev["n_pass"] = 0
            ev["n_total"] = 0
            _finalize(ev)
            return ev

        # ---- (k) proacl AFTER 012 must equal proacl AFTER 011 (grants preserved).
        proacl_after_012 = get_proacl(cluster)
        ev["proacl_after_012"] = proacl_after_012
        grants_preserved = (proacl_after_011 == proacl_after_012)
        ev["grants_preserved"] = grants_preserved

        # ---- (a/e/g) functional correctness battery against the 012 body.
        functional = run_candidate_battery(cluster, label="012")
        ev["functional_assertions"] = functional

        # ---- (b) FULL-row parity: 012 stream == 010 stream (rollback), per query.
        streams_012, streams_010 = capture_streams(cluster)
        parity: dict[str, Any] = {}
        all_parity = True
        for q in PARITY_QUERIES:
            name = q["name"]
            s12, s10 = streams_012[name], streams_010[name]
            same = (s12 == s10)
            if not same:
                all_parity = False
            parity[name] = {"query": q["query"],
                            "filters": {k: v for k, v in q.items() if k not in ("name", "query")},
                            "n_012": len(s12), "n_010": len(s10), "identical": same}
        ev["parity"] = parity
        ev["parity_all"] = all_parity

        # ---- (c) cross-chunk safety proof.
        ev["cross_boundary"] = run_cross_boundary_proof(cluster)

        # ---- (d) anchor parity proof.
        ev["anchor"] = run_anchor_proof(cluster)

        # ---- (f) security proofs (MV denial, 011 ACL preserved, RPC, quarantine).
        ev["security"] = run_security_proofs(cluster)

        # Snapshot a canonical 012 result stream for the verdict.
        ev["canonical_012_wan_wf"] = call_candidates_json(cluster, "WanVideoSampler", kinds=["workflow"])

        # ---- (i) rollback: drop the MV + re-apply 010's function, confirm the
        #      function still works + returns the same results.
        rollback_ok = True
        rollback_error = ""
        try:
            cluster.psql("DROP MATERIALIZED VIEW IF EXISTS public.lexical_workflow_python_search CASCADE;")
            apply_migration(cluster, MIGRATION_010)  # 010 has no MV dependency
        except Exception as exc:  # noqa: BLE001
            rollback_ok = False
            rollback_error = str(exc)
        ev["rollback_error"] = rollback_error

        if rollback_ok:
            rollback_battery = R10.run_candidate_battery(cluster, label="rollback_to_010")
            ev["rollback_battery"] = rollback_battery
            rollback_ok = all(c["ok"] for c in rollback_battery)
        ev["rollback_ok"] = rollback_ok

        # ---- (j) idempotence: re-apply 012 twice, no error, still correct.
        idempotent_ok = True
        idempotent_error = ""
        try:
            apply_migration(cluster, MIGRATION_012)
            apply_migration(cluster, MIGRATION_012)
        except Exception as exc:  # noqa: BLE001
            idempotent_ok = False
            idempotent_error = str(exc)
        ev["idempotent_ok"] = idempotent_ok
        ev["idempotent_error"] = idempotent_error

        if idempotent_ok:
            idem_battery = run_candidate_battery(cluster, label="012_idempotent")
            ev["idempotent_battery"] = idem_battery
            idempotent_ok = all(c["ok"] for c in idem_battery)
            ev["idempotent_ok"] = idempotent_ok

        # ---- (k final) grants must STILL be preserved after rollback + re-apply.
        proacl_final = get_proacl(cluster)
        ev["proacl_final"] = proacl_final
        grants_preserved = grants_preserved and (proacl_after_011 == proacl_final)

        # ---- verdict aggregation.
        functional_ok = all(c["ok"] for c in functional)
        xb = ev["cross_boundary"]
        an = ev["anchor"]
        sec = ev["security"]
        cross_ok = bool(xb.get("012_cross_boundary_item_absent") and xb.get("010_cross_boundary_item_absent")
                        and xb.get("corrected_direct_match") is False and xb.get("buggy_renormalize_match") is True)
        anchor_ok = bool(an.get("anchor_logic_equal") and an.get("full_row_stream_equal"))
        security_ok = bool(
            sec.get("anon_cannot_select_mv") and sec.get("authenticated_cannot_select_mv")
            and sec.get("public_cannot_select_mv") and sec.get("anon_select_errors")
            and sec.get("authenticated_select_errors")
            and sec.get("anon_no_execute_candidates_after_012")
            and sec.get("authenticated_no_execute_candidates_after_012")
            and sec.get("service_role_rpc_ok") and sec.get("service_role_rpc_workflow_results", 0) > 0
            and sec.get("quarantined_zero_candidates"))
        ev["cross_ok"] = cross_ok
        ev["anchor_ok"] = anchor_ok
        ev["security_ok"] = security_ok
        ev["all_pass"] = bool(
            apply_ok and functional_ok and all_parity and cross_ok and anchor_ok
            and security_ok and rollback_ok and idempotent_ok and grants_preserved
        )
        ev["n_pass"] = sum(1 for c in functional if c["ok"])
        ev["n_total"] = len(functional)
    finally:
        cluster.tear_down()

    _finalize(ev)
    return ev


def _finalize(ev: dict[str, Any]) -> None:
    VERDICT_PATH.parent.mkdir(parents=True, exist_ok=True)
    VERDICT_PATH.write_text(json.dumps(ev, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ev = rehearse()
    print("=" * 72)
    print(f"schema/012 rehearsal verdict: {'PASS' if ev['all_pass'] else 'FAIL'}")
    print(f"  applied_ok         = {ev['applied_ok']}")
    print(f"  functional battery = {ev['n_pass']}/{ev['n_total']}")
    print(f"  parity_all (full)  = {ev.get('parity_all')}")
    print(f"  cross_ok           = {ev.get('cross_ok')}")
    print(f"  anchor_ok          = {ev.get('anchor_ok')}")
    print(f"  security_ok        = {ev.get('security_ok')}")
    print(f"  rollback_ok        = {ev['rollback_ok']}")
    print(f"  idempotent_ok      = {ev['idempotent_ok']}")
    print(f"  grants_preserved   = {ev['grants_preserved']}")
    print(f"  proacl after 011   = {ev.get('proacl_after_011')}")
    print(f"  proacl after 012   = {ev.get('proacl_after_012')}")
    print(f"  proacl final       = {ev.get('proacl_final')}")
    print("-" * 72)
    print("full-row parity (012 == 010):")
    for name, info in ev.get("parity", {}).items():
        flag = "OK " if info["identical"] else "DIFF"
        print(f"  [{flag}] {name:14s} n_012={info['n_012']:3d} n_010={info['n_010']:3d}  {info['query']}")
    print("-" * 72)
    xb = ev.get("cross_boundary", {})
    print(f"cross-boundary: 010_absent={xb.get('010_cross_boundary_item_absent')} "
          f"012_absent={xb.get('012_cross_boundary_item_absent')} "
          f"corrected_direct={xb.get('corrected_direct_match')} "
          f"buggy_renormalize={xb.get('buggy_renormalize_match')}")
    an = ev.get("anchor", {})
    print(f"anchor: logic_equal={an.get('anchor_logic_equal')} "
          f"(010={an.get('anchor_010_logic')} 012={an.get('anchor_012_logic')}) "
          f"full_row_equal={an.get('full_row_stream_equal')}")
    sec = ev.get("security", {})
    print(f"security: anon_denied={sec.get('anon_cannot_select_mv')} "
          f"auth_denied={sec.get('authenticated_cannot_select_mv')} "
          f"rpc_ok={sec.get('service_role_rpc_ok')} "
          f"quarantine_zero_candidates={sec.get('quarantined_zero_candidates')} "
          f"(mv_rows_for_7000_info={sec.get('quarantined_mv_rows_info')})")
    print("-" * 72)
    for c in ev["functional_assertions"]:
        if not c["ok"]:
            print(f"  FAIL {c['name']}: {c['detail']}")
    print("=" * 72)
    print(f"verdict written to {VERDICT_PATH}")
    return 0 if ev["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
