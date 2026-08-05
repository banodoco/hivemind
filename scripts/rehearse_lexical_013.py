#!/usr/bin/env python3
"""Throwaway isolated-PostgreSQL rehearsal for ``schema/013_lexical_latency_phase3.sql``.

Proves the phase-3 latency fix is CORRECT, RECALL-PRESERVING, SECURE, REVERSIBLE,
and IDEMPOTENT on a local isolated cluster, and captures a LOCAL-ONLY 012-vs-013
timing comparison + the expected local plan/index shape for the changed hot path.

This is the LOCAL-PROOF rehearsal. It does NOT touch production, does NOT apply
013 remotely, and makes NO production latency claim. The local cluster is tiny
relative to production, so the timing numbers are labeled local-only and are NOT
the 750ms production gate.

REUSES the schema/012 harness (helpers/fixtures) rather than re-deriving it:
  * ``scripts/lexical_pg.py``               — LocalCluster lifecycle + helpers.
  * ``scripts/rehearse_lexical_candidate.py``— bootstrap() (001..009), seed().
  * ``scripts/rehearse_lexical_010.py``      — seed_extra() + the 010 battery.
  * ``scripts/rehearse_lexical_012.py``      — bootstrap_through_011(), the MV
    refresh, the byte-parity capture (call_candidates_json), the adversarial
    fixtures (seed_adversarial, CROSS_BOUNDARY_*, ANCHOR_*), the parity query
    set (PARITY_QUERIES), apply_migration(), get_proacl(), and the body-agnostic
    security proof battery (run_security_proofs).

The rehearsal:
  (a) APPLIES 001..011, then 012, then 013 cleanly (013 AFTER 012).
  (b) FUNCTIONAL CORRECTNESS — the 012 candidate battery (mirroring 010) passes
      against the 013 body.
  (c) FULL canonical-row byte parity 013 == 012 across the representative +
      adversarial parity queries (entity, item, representation, matched_snippet
      /anchor, lexical_rank, lexical_source, created_at, ORDER, global limit) —
      not merely the item-id set.
  (d) CROSS-BOUNDARY negative parity (a needle present only across a chunk
      boundary matches in NEITHER 012 nor 013).
  (e) NEWEST-MATCHING-ANCHOR byte parity (a multi-chunk item where only a later
      chunk matches selects that later chunk's anchor, byte-equal to 012).
  (f) QUARANTINE exclusion (a quarantined workflow never ranks).
  (g) FRAGMENT/MV 1..8000 bound (an out-of-range chunk never reaches the
      fragment surface; behavior byte-equal to 012).
  (h) SECURITY — anon/authenticated/public cannot SELECT the MV nor EXECUTE the
      candidates function; service-role RPC works; proacl preserved (013 == 012).
  (i) ROLLBACK to 012 returns the same rows (byte-equal canonical stream).
  (j) IDEMPOTENCE — applying 013 twice is clean and still correct.
  (k) HOT-PATH SHAPE — the live body contains adaptive dense/sparse paths, a
      bounded dense LATERAL, and a MATERIALIZED sparse match set. The exact
      sparse inner statement is EXPLAINed without disabling sequential scans or
      otherwise forcing an index.
  (l) LOCAL TIMING — a meaningful 012-vs-013 median comparison on a seeded
      corpus large enough to exercise the anchor rewrite (many matched items,
      many chunks per item), with warmup; every insert return code and the
      lexical_resource_python_state -> external_resources FK are checked.

VERDICT SECRET-SAFETY. The written JSON serializes ONLY opaque probe names,
counts, booleans, timings, and schema object names. It NEVER serializes queries,
filter values, snippets, workflow source, SQL, stderr, credentials, or author/
channel values. Result streams / plan text / needle strings are compared in
process and discarded.
"""

from __future__ import annotations

import json
import pathlib
import re
import statistics
import sys
import time
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import lexical_pg as LP  # noqa: E402
import rehearse_lexical_candidate as R  # noqa: E402
import rehearse_lexical_010 as R10  # noqa: E402
import rehearse_lexical_012 as R12  # noqa: E402

SCHEMA_DIR = REPO / "schema"

MIGRATION_012 = "012_lexical_latency_phase2.sql"
MIGRATION_013 = "013_lexical_latency_phase3.sql"

VERDICT_PATH = (
    REPO / "docs" / "hybrid-search" / "production"
    / "phase5-013-local-proof-rehearsal-2026-07-29.json"
)

MV = R12.MV  # public.lexical_workflow_python_search

# The 012 per-item correlated matched_anchor scalar subquery (the phase-3
# optimization target). Present in 012; must be ABSENT in 013's body.
CORRELATED_ANCHOR_RE = re.compile(
    r"\(\s*select\s+ld\.matched_anchor.*?ld\.item_id\s*=\s*mv\.item_id.*?"
    r"order\s+by\s+ld\.created_at\s+desc.*?limit\s+1\s*\)\s+as\s+matched_snippet",
    re.IGNORECASE | re.DOTALL,
)

# Trigram GIN that serves the sparse matching-chunk LIKE access predicate.
# Proven in the exact extracted inner plan without forcing planner settings.
TRGM_IDX = "lexical_documents_python_chunk_trgm_idx"

# ---------------------------------------------------------------------------
# Adaptive-path + sparse-match marker contract.
#
# schema/013 is REQUIRED (later) to carry explicit marker comments in the live
# function body so the rehearsal can (a) tell the dense vs sparse adaptive paths
# apart structurally, and (b) EXTRACT the exact sparse-match inner SELECT and
# EXPLAIN it directly (an ordinary EXPLAIN of a PL/pgSQL function only yields a
# Function Scan and is invalid evidence). Until those markers exist, every
# marker-gated proof reports a structured NOT-YET result and the suite stays RED.
# ---------------------------------------------------------------------------
DENSE_PATH_MARKER = "-- h013_dense_path"
SPARSE_PATH_MARKER = "-- h013_sparse_path"
SPARSE_MATCH_BEGIN = "-- h013_sparse_match_begin"
SPARSE_MATCH_END = "-- h013_sparse_match_end"

# A valid base timestamptz for the timing corpus. Every chunk's created_at is
# this base plus a monotonic offset (NO day/month modulo wrapping).
TIMING_BASE_TS = "timestamp '2026-02-01T00:00:00Z'"
TIMING_INDEX_DECOY_ROWS = 100_000

# Timing-corpus needles (in-process only; NEVER serialized). Neutral tokens not
# present in the base seed, planted into a large set of matched workflow_python
# chunks so the anchor rewrite is exercised.
DENSE_NEEDLE = "benchfragneedlexyz"
SPARSE_NEEDLE = "sparsefragneedlexyz"


# ---------------------------------------------------------------------------
# Bootstrap through 012 (reuse the 012 harness), then apply 013.
# ---------------------------------------------------------------------------


def bootstrap_through_012(cluster: LP.LocalCluster) -> None:
    """Apply 001..011 + seed (production-shaped + extra + adversarial), then 012.

    Reuses R12.bootstrap_through_011, R.seed, R10.seed_extra, R12.seed_adversarial.
    Leaves the cluster on the schema/012 body with the MV refreshed.
    """
    R.reset_schema(cluster)
    R12.bootstrap_through_011(cluster)
    R.seed(cluster, n_messages=8000)
    R10.seed_extra(cluster)
    R12.seed_adversarial(cluster)
    R12.apply_migration(cluster, MIGRATION_012)
    R12.refresh_mv(cluster)


def apply_013(cluster: LP.LocalCluster) -> None:
    R12.apply_migration(cluster, MIGRATION_013)


def apply_012(cluster: LP.LocalCluster) -> None:
    R12.apply_migration(cluster, MIGRATION_012)
    R12.refresh_mv(cluster)


# The CREATE OR REPLACE FUNCTION ... $$ ... $$; statement extracted from
# schema/012. Cached after first read.
_012_FUNCTION_SQL: str | None = None


def _extract_function_statement_012() -> str:
    """Extract ONLY the ``create or replace function public.hivemind_lexical_candidates``
    statement from schema/012 (the MV/index/refresh/revoke DDL is dropped). The
    result is a single self-contained statement ending in ``$$;``.

    Used so the 012-vs-013 timing comparison switches ONLY the function body
    (symmetric with apply_013, which already applies one function) and never
    re-runs the MV refresh between samples.
    """
    global _012_FUNCTION_SQL
    if _012_FUNCTION_SQL is not None:
        return _012_FUNCTION_SQL
    text = (SCHEMA_DIR / MIGRATION_012).read_text(encoding="utf-8")
    m = re.search(
        r"create\s+or\s+replace\s+function\s+public\.hivemind_lexical_candidates\b.*?\$\$;",
        text, re.IGNORECASE | re.DOTALL,
    )
    if not m:
        raise RuntimeError("could not isolate hivemind_lexical_candidates in schema/012")
    _012_FUNCTION_SQL = m.group(0)
    return _012_FUNCTION_SQL


def apply_012_function_only(cluster: LP.LocalCluster) -> None:
    """Apply ONLY the 012 candidate function (no MV, no index, no refresh). This
    is the symmetric counterpart of apply_013 (which also applies one function),
    so the timing comparison varies only the function body."""
    cluster.psql(_extract_function_statement_012())


def _opaque_query_id(idx: int) -> str:
    """Opaque parity key — NEVER the query string or filter values."""
    return f"q{idx:02d}"


# ---------------------------------------------------------------------------
# (c) FULL canonical-row byte parity 013 == 012 across the parity queries.
# ---------------------------------------------------------------------------


def capture_parity(cluster: LP.LocalCluster) -> tuple[dict[str, list], dict[str, list]]:
    """Capture FULL-row candidate streams under 013, then under 012 (rollback),
    for every parity query. Returns (streams_013, streams_012) keyed by the
    OPAQUE query id. Restores 013 at the end.
    """
    streams_013: dict[str, list] = {}
    for q in R12.PARITY_QUERIES:
        streams_013[q["name"]] = R12.call_candidates_json(
            cluster, q["query"], kinds=q.get("kinds"), channels=q.get("channels"),
            authors=q.get("authors"), item_ids=q.get("item_ids"))
    apply_012(cluster)  # rollback body to 012 (MV refreshed)
    streams_012: dict[str, list] = {}
    for q in R12.PARITY_QUERIES:
        streams_012[q["name"]] = R12.call_candidates_json(
            cluster, q["query"], kinds=q.get("kinds"), channels=q.get("channels"),
            authors=q.get("authors"), item_ids=q.get("item_ids"))
    apply_013(cluster)  # restore 013
    return streams_013, streams_012


def parity_verdict(streams_013: dict[str, list], streams_012: dict[str, list]) -> dict[str, Any]:
    """Build the SECRET-SAFE parity sub-verdict (opaque id, counts, identical)."""
    out: dict[str, Any] = {}
    all_identical = True
    for idx, q in enumerate(R12.PARITY_QUERIES):
        name = q["name"]
        s13, s12 = streams_013[name], streams_012[name]
        identical = (s13 == s12)
        all_identical = all_identical and identical
        out[_opaque_query_id(idx)] = {
            "identical": identical,
            "n_013": len(s13),
            "n_012": len(s12),
        }
    return {"per_query": out, "parity_all": all_identical}


# ---------------------------------------------------------------------------
# (d) Cross-boundary negative parity (013 vs 012).
# ---------------------------------------------------------------------------


def cross_boundary_proof(cluster: LP.LocalCluster) -> dict[str, Any]:
    out: dict[str, Any] = {}
    item = R12.CROSS_BOUNDARY_ITEM
    needle = R12.CROSS_BOUNDARY_NEEDLE  # in-process only

    rows13 = R12.call_candidates(cluster, needle, kinds=["workflow"])
    out["absent_013"] = item not in {r["item_id"] for r in rows13}

    # Direct demonstration that the vulnerability class is real: the corrected
    # DIRECT (per-chunk) match is False; the buggy re-normalize is True.
    rc, demo = cluster.psql(
        "select "
        "(select string_agg(public.hivemind_normalize_identifier(chunk_text),' ') "
        f"from public.lexical_documents where item_id='{item}' and "
        "representation_type='workflow_python') like '%ksampler%' as corrected_direct, "
        "(select public.hivemind_normalize_identifier(string_agg("
        "public.hivemind_normalize_identifier(chunk_text),' ')) "
        f"from public.lexical_documents where item_id='{item}' and "
        "representation_type='workflow_python') like '%ksampler%' as buggy_renormalize;"
    )
    parts = [p.strip() for p in (demo or "").split("|")]
    out["corrected_direct_match"] = (parts[0] == "t") if len(parts) == 2 else None
    out["buggy_renormalize_match"] = (parts[1] == "t") if len(parts) == 2 else None

    apply_012(cluster)
    rows12 = R12.call_candidates(cluster, needle, kinds=["workflow"])
    out["absent_012"] = item not in {r["item_id"] for r in rows12}
    apply_013(cluster)
    out["cross_ok"] = bool(
        out["absent_013"] and out["absent_012"]
        and out["corrected_direct_match"] is False
        and out["buggy_renormalize_match"] is True
    )
    return out


# ---------------------------------------------------------------------------
# (e) Newest-matching-anchor byte parity (013 vs 012).
# ---------------------------------------------------------------------------


def anchor_proof(cluster: LP.LocalCluster) -> dict[str, Any]:
    out: dict[str, Any] = {}
    item = R12.ANCHOR_ITEM
    needle = R12.ANCHOR_NEEDLE  # in-process only

    stream13 = R12.call_candidates_json(cluster, needle, kinds=["workflow"])
    hit13 = [r for r in stream13 if r["i"] == item]
    newest_matching_013 = bool(hit13) and hit13[0]["s"] == "ANCHOR_TWO"

    # Full-row stream parity 013 vs 012 for the anchor needle.
    apply_012(cluster)
    stream12 = R12.call_candidates_json(cluster, needle, kinds=["workflow"])
    apply_013(cluster)

    out["newest_matching_anchor_013"] = newest_matching_013
    out["full_row_stream_equal"] = (stream13 == stream12)
    out["anchor_ok"] = bool(newest_matching_013 and out["full_row_stream_equal"])
    return out


# ---------------------------------------------------------------------------
# (f) Quarantine exclusion — delegated to the body-agnostic security battery.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# (g) Fragment/MV 1..8000 bound — out-of-range chunk never reaches the surface.
# ---------------------------------------------------------------------------


def fragment_bound_proof(cluster: LP.LocalCluster) -> dict[str, Any]:
    out: dict[str, Any] = {}
    # Plant a safe workflow whose ONLY workflow_python chunk is >8000 chars.
    stmts = [
        "insert into external_resources (id, kind, source, external_id, title, body, "
        "author, url, metadata) overriding system value values "
        "(7700,'workflow','vibecomfy-external','w7700','OversizeWorkflow','d','agent',null,'{}') "
        "on conflict (id) do nothing;",
        "insert into lexical_resource_python_state (resource_id, kind, cohort, "
        "public_state, available, body_duplicate, chunk_count) values "
        "(7700,'workflow','payload_python','safe',true,false,1) on conflict (resource_id) "
        "do update set public_state='safe', available=true;",
    ]
    for stmt in stmts:
        rc, _ = cluster.psql(stmt)
        if rc != 0:
            raise RuntimeError(f"fragment_bound seed failed (rc={rc})")
    overlong = "oversizeneedlexyz " + ("a" * 9000)
    rc, _ = cluster.psql(
        "insert into lexical_documents (entity_type, item_id, representation_type, "
        "chunk_index, chunk_text, matched_anchor, representation_hash, chunk_hash, "
        "quarantine_state) values "
        "('resource','7700','workflow_python',0,'" + overlong.replace("'", "''") + "',"
        "'ANCH7700','h7700','c7700','safe') on conflict do nothing;")
    if rc != 0:
        raise RuntimeError(f"fragment_bound chunk seed failed (rc={rc})")
    R12.refresh_mv(cluster)

    rc, mv = cluster.psql(
        f"select count(*)::text from {MV} where item_id='7700';")
    out["oversize_item_excluded_from_mv"] = ((mv or "0").strip() == "0")

    # Behavior byte-equal to 012 on the same corpus.
    s13 = R12.call_candidates_json(cluster, "oversizeneedlexyz", kinds=["workflow"])
    apply_012(cluster)
    s12 = R12.call_candidates_json(cluster, "oversizeneedlexyz", kinds=["workflow"])
    apply_013(cluster)
    out["stream_equal_013_012"] = (s13 == s12)
    out["fragment_bound_ok"] = bool(out["oversize_item_excluded_from_mv"]
                                    and out["stream_equal_013_012"])
    return out


# ---------------------------------------------------------------------------
# (h) Security — reuse R12's body-agnostic security battery.
# ---------------------------------------------------------------------------


def security_proof(cluster: LP.LocalCluster) -> dict[str, Any]:
    sec = R12.run_security_proofs(cluster)
    # Curate to the load-bearing booleans + counts (already secret-safe). The
    # informational mv-rows-for-quarantined count is kept as a count.
    curated = {k: sec.get(k) for k in (
        "anon_cannot_select_mv", "authenticated_cannot_select_mv", "public_cannot_select_mv",
        "anon_select_errors", "authenticated_select_errors",
        "anon_no_execute_candidates_after_012", "authenticated_no_execute_candidates_after_012",
        "service_role_rpc_ok", "service_role_rpc_workflow_results",
        "quarantined_zero_candidates", "quarantined_mv_rows_info",
    )}
    # Unsuffixed aliases for the already-proven _after_012 booleans, so callers
    # can assert the no-execute guarantee without referencing the 012-specific
    # internal key names.
    curated["anon_no_execute_candidates"] = curated["anon_no_execute_candidates_after_012"]
    curated["authenticated_no_execute_candidates"] = (
        curated["authenticated_no_execute_candidates_after_012"])
    curated["security_ok"] = bool(
        curated["anon_cannot_select_mv"] and curated["authenticated_cannot_select_mv"]
        and curated["public_cannot_select_mv"] and curated["anon_select_errors"]
        and curated["authenticated_select_errors"]
        and curated["anon_no_execute_candidates_after_012"]
        and curated["authenticated_no_execute_candidates_after_012"]
        and curated["service_role_rpc_ok"]
        and curated["service_role_rpc_workflow_results"]
        and curated["quarantined_zero_candidates"])
    return curated


# ---------------------------------------------------------------------------
# (i) Rollback to 012 returns the same rows.
# ---------------------------------------------------------------------------


def rollback_proof(cluster: LP.LocalCluster) -> dict[str, Any]:
    out: dict[str, Any] = {}
    # Canonical 013 workflow stream (full rows).
    stream13 = R12.call_candidates_json(cluster, "WanVideoSampler", kinds=["workflow"])
    apply_012(cluster)  # rollback to 012
    stream12 = R12.call_candidates_json(cluster, "WanVideoSampler", kinds=["workflow"])
    apply_013(cluster)  # restore 013
    out["rollback_streams_equal"] = (stream13 == stream12)
    out["rollback_ok"] = bool(out["rollback_streams_equal"])
    return out


# ---------------------------------------------------------------------------
# (k) Hot-path shape: live body contract + unforced exact-inner plan proof.
# ---------------------------------------------------------------------------


def _live_body(cluster: LP.LocalCluster) -> str:
    rc, out = cluster.psql(
        "select prosrc from pg_proc p join pg_namespace n on n.oid=p.pronamespace "
        "where n.nspname='public' and p.proname='hivemind_lexical_candidates'"
    )
    return out if rc == 0 else ""


def hot_path_proof(cluster: LP.LocalCluster) -> dict[str, Any]:
    out: dict[str, Any] = {}
    body = _live_body(cluster)
    # Adaptive dense/sparse path markers — schema/013 is REQUIRED to carry both.
    out["has_dense_path_marker"] = DENSE_PATH_MARKER in body
    out["has_sparse_path_marker"] = SPARSE_PATH_MARKER in body
    dense_region = ""
    sparse_region = ""
    if DENSE_PATH_MARKER in body and SPARSE_PATH_MARKER in body:
        dense_pos = body.index(DENSE_PATH_MARKER)
        sparse_pos = body.index(SPARSE_PATH_MARKER)
        if dense_pos < sparse_pos:
            dense_region = body[dense_pos + len(DENSE_PATH_MARKER):sparse_pos]
            sparse_region = body[sparse_pos + len(SPARSE_PATH_MARKER):]
        else:
            sparse_region = body[sparse_pos + len(SPARSE_PATH_MARKER):dense_pos]
            dense_region = body[dense_pos + len(DENSE_PATH_MARKER):]
    # A specifically named MATERIALIZED sparse matching set in the sparse path
    # (not the unrelated safe_wf CTE shared by every implementation).
    out["has_materialized_sparse_set"] = bool(
        re.search(r"\bsparse_matches\s+as\s+materialized\b", sparse_region, re.I))
    # A correlated / LATERAL bounded dense lookup (the dense path bounds the
    # per-item anchor lookup, never an unbounded correlated walk).
    out["has_correlated_or_lateral_dense"] = bool(
        re.search(r"\blateral\b", dense_region, re.I))
    # Newest-anchor selection survives (order by created_at desc).
    out["has_newest_anchor_selection"] = bool(
        re.search(r"order\s+by.*created_at\s+desc", body, re.I | re.DOTALL))
    # The exact predicates that make the sparse path trigram-indexed must be
    # present (these are the secret-safe token forms, not values).
    out["has_exact_predicates"] = all(tok in body for tok in (
        "representation_type = 'workflow_python'",
        "quarantine_state = 'safe'",
        "char_length(ld.chunk_text) between 1 and 8000",
    ))
    # The 012 per-item correlated matched_anchor scalar subquery must be gone.
    out["has_correlated_scalar_anchor"] = bool(CORRELATED_ANCHOR_RE.search(body))
    # The real-plan trigram proof is asserted under the sparse timing case
    # (EXPLAIN of the extracted exact sparse-match inner statement). A forced
    # bare-LIKE probe with enable_seqscan=off is deliberately NOT used as a gate.
    out["bare_like_probe_is_gate"] = None
    out["hot_path_ok"] = bool(
        out["has_dense_path_marker"]
        and out["has_sparse_path_marker"]
        and out["has_materialized_sparse_set"]
        and out["has_correlated_or_lateral_dense"]
        and out["has_newest_anchor_selection"]
        and out["has_exact_predicates"]
        and not out["has_correlated_scalar_anchor"])
    return out


# ---------------------------------------------------------------------------
# (l) Local 012-vs-013 timing comparison on a corpus that exercises the rewrite.
# ---------------------------------------------------------------------------


def _analyze_checked(cluster: LP.LocalCluster) -> dict[str, bool]:
    """Run ANALYZE on both surfaces the timing comparison reads, with every
    return code checked. Returns {object: ok}."""
    out: dict[str, bool] = {}
    for obj in ("public.lexical_documents", "public.lexical_workflow_python_search"):
        rc, detail = cluster.psql(f"ANALYZE {obj};")
        out[obj] = (rc == 0)
        if rc != 0:
            raise RuntimeError(f"ANALYZE failed for {obj}: rc={rc}; {detail[:200]}")
    return out


def _seed_index_scale_decoys(cluster: LP.LocalCluster) -> int:
    """Give the unforced sparse-plan proof a production-shaped selectivity.

    The rows have no lexical_resource_python_state parent, so they never enter
    the workflow MV or candidate set. They only make lexical_documents large
    enough for PostgreSQL to choose the existing trigram GIN naturally.
    """
    rc, detail = cluster.psql(
        "insert into public.lexical_documents "
        "(entity_type,item_id,representation_type,chunk_index,chunk_text,"
        "matched_anchor,representation_hash,chunk_hash,quarantine_state,created_at) "
        "select 'resource','planner_decoy_'||g::text,'workflow_python',0,"
        "'ordinary filler code without any benchmark target '||g::text,"
        "'planner_filler','planner_rh_'||g::text,'planner_ch_'||g::text,"
        "'safe',timestamp '2025-01-01' + g * interval '1 second' "
        f"from generate_series(1,{TIMING_INDEX_DECOY_ROWS}) g;")
    if rc != 0:
        raise RuntimeError(
            f"planner-scale decoy insert failed: rc={rc}; {detail[:200]}")
    return TIMING_INDEX_DECOY_ROWS


def _seed_timing_corpus(cluster: LP.LocalCluster, *, base_id: int, n_items: int,
                        chunks_per: int, needle: str, matching_chunks: int,
                        matching_position: str) -> dict[str, Any]:
    """Seed n_items safe workflows, each with chunks_per workflow_python chunks.
    matching_chunks of them (1..chunks_per) contain the needle; the rest are
    decoys. matching_position controls WHERE among the created_at ordering the
    matching chunk(s) land: 'all' (every chunk), or 'oldest' (the matching chunk
    is the OLDEST, forcing 012's per-item item_id-index scan to walk every chunk
    of the item re-running normalize() as a non-indexed filter before it hits the
    match). Distinct created_at + anchors force the anchor selection to resolve
    newest-matching per item.

    Every insert return code is checked; the lexical_resource_python_state row is
    inserted AFTER its external_resources parent so the FK holds.
    """
    for it in range(base_id, base_id + n_items):
        rc, _ = cluster.psql(
            "insert into external_resources (id, kind, source, external_id, title, body, "
            "author, url, metadata) overriding system value values "
            f"({it},'workflow','vibecomfy-external','w{it}','t','d','agent',null,'{{}}') "
            "on conflict (id) do nothing;")
        if rc != 0:
            raise RuntimeError(f"timing-corpus resource insert rc={rc} for {it}")
        rc, _ = cluster.psql(
            "insert into lexical_resource_python_state (resource_id, kind, cohort, "
            "public_state, available, body_duplicate, chunk_count) values "
            f"({it},'workflow','payload_python','safe',true,false,{chunks_per}) "
            "on conflict (resource_id) do update set public_state='safe', available=true;")
        if rc != 0:
            raise RuntimeError(f"timing-corpus state insert rc={rc} for {it} (FK parent={it})")
        # created_at increases MONOTONICALLY with chunk_index and is UNIQUE across
        # the whole corpus. There is NO day/month modulo wrapping: every chunk's
        # created_at is the valid base timestamptz plus (item_offset *
        # chunks_per + chunk_index) seconds, expressed as a SQL interval
        # expression so the value is computed by Postgres, not formatted in
        # Python. The anchor selection orders by created_at DESC, so the NEWEST
        # chunk wins. For the sparse/oldest case the matching chunk is chunk 0
        # (oldest), so a strategy that scans created_at-DESC and early-stops at
        # the first match (012's correlated scalar) must walk ALL chunks of the
        # item.
        vals = []
        item_offset = it - base_id
        for c in range(chunks_per):
            if matching_position == "all":
                has_needle = True
            elif matching_position == "oldest":
                has_needle = (c < matching_chunks)  # the oldest chunk(s) match
            else:
                has_needle = False
            token = f"{needle} " if has_needle else ""
            text = f"# decoy chunk {c} {token}tail{c}".replace("'", "''")
            anchor = f"anchor{c}".replace("'", "''")
            monotonic_offset = item_offset * chunks_per + c
            created_at_expr = (
                f"{TIMING_BASE_TS} + ({monotonic_offset}) * interval '1 second'")
            vals.append(
                f"('resource','{it}','workflow_python',{c},'"
                f"{text}','{anchor}','h{it}_{c}','cc{it}_{c}',"
                f"'safe',{created_at_expr})")
        rc, _ = cluster.psql(
            "insert into lexical_documents (entity_type, item_id, representation_type, "
            "chunk_index, chunk_text, matched_anchor, representation_hash, chunk_hash, "
            "quarantine_state, created_at) values " + ",".join(vals) + " on conflict do nothing;")
        if rc != 0:
            raise RuntimeError(f"timing-corpus docs insert rc={rc} for {it}")
    R12.refresh_mv(cluster)
    # ANALYZE both surfaces the planner needs, CHECKED. Recorded so the evidence
    # proves the timing comparison ran against analyzed statistics (not stale).
    analyzed = _analyze_checked(cluster)
    return {"n_items": n_items, "chunks_per_item": chunks_per,
            "n_workflow_python_chunks": n_items * chunks_per,
            "matched_chunks_per_item": matching_chunks if matching_position != "all" else "all",
            "matching_position": matching_position,
            "analyzed": all(analyzed.values()),
            "analyzed_per_object": analyzed}


def _median_interleaved(cluster: LP.LocalCluster, query: str, *, runs: int, warmup: int) -> dict[str, float]:
    """Time 013 vs 012 with INTERLEAVED body order so cache/warm state cannot
    favour one body. Body switching is SYMMETRIC and FUNCTION-ONLY: each switch
    applies ONLY the candidate function (apply_012_function_only for 012,
    apply_013 for 013 — both a single CREATE OR REPLACE FUNCTION). The MV is NOT
    refreshed per sample/body switch (the data is unchanged; only the body
    differs). Each iteration flips the body, applies it, runs the query once,
    and records the ms. Returns {'013': median, '012': median}.

    The cluster is left on the 013 body at the end.
    """
    s13: list[float] = []
    s12: list[float] = []
    # Warm BOTH bodies once before measuring (function-only switches).
    for _ in range(warmup):
        apply_012_function_only(cluster)
        R12.call_candidates_json(cluster, query, kinds=["workflow"])
        apply_013(cluster)
        R12.call_candidates_json(cluster, query, kinds=["workflow"])
    # Interleaved measurement: alternate which body runs first each iteration.
    for i in range(runs):
        order = ("013", "012") if i % 2 == 0 else ("012", "013")
        for body in order:
            if body == "012":
                apply_012_function_only(cluster)
            else:
                apply_013(cluster)
            t0 = time.perf_counter()
            R12.call_candidates_json(cluster, query, kinds=["workflow"])
            ms = (time.perf_counter() - t0) * 1000.0
            (s13 if body == "013" else s12).append(ms)
    apply_013(cluster)  # restore 013
    return {
        "median_ms_013": round(statistics.median(s13), 3),
        "median_ms_012": round(statistics.median(s12), 3),
        "p95_ms_013": round(sorted(s13)[int(len(s13) * 0.95)], 3),
        "p95_ms_012": round(sorted(s12)[int(len(s12) * 0.95)], 3),
        "body_switch": "function_only",
        "mv_refresh_per_switch": False,
    }


def _real_plan_uses_trgm(cluster: LP.LocalCluster, query: str) -> dict[str, Any]:
    """EXPLAIN the EXACT sparse-match inner SELECT extracted from the live
    function body, and report whether that real plan is served by
    ``lexical_documents_python_chunk_trgm_idx``.

    An ordinary EXPLAIN of a PL/pgSQL function only returns a top-level
    ``Function Scan`` and is INVALID evidence (it never reveals the inner
    index choice), so this function instead:

      * reads the live ``prosrc``;
      * extracts the sparse-match inner SELECT delimited by the explicit
        ``SPARSE_MATCH_BEGIN`` / ``SPARSE_MATCH_END`` marker comments that
        schema/013 is REQUIRED to carry;
      * substitutes the query literal SAFELY (dollar-quoted, never string-
        interpolated into SQL) for the body's ``v_qn`` placeholder;
      * EXPLAINs that exact statement with NO ``enable_seqscan=off`` and NO
        forced index.

    For the CURRENT unmarked schema/013 (markers absent) it returns a structured
    NOT-YET result (``uses_trgm_index`` None, reason
    ``missing_inner_plan_markers``) and NEVER falls back to a bare-LIKE
    substitute. The plan text and the query are consumed in process and never
    serialized.
    """
    body = _live_body(cluster)
    result: dict[str, Any] = {"method": "extracted_exact_inner_statement",
                              "uses_trgm_index": None,
                              "reason": ""}
    if SPARSE_MATCH_BEGIN not in body or SPARSE_MATCH_END not in body:
        result["reason"] = "missing_inner_plan_markers"
        result["bare_like_substitute_used"] = False
        return result

    start = body.index(SPARSE_MATCH_BEGIN) + len(SPARSE_MATCH_BEGIN)
    end = body.index(SPARSE_MATCH_END, start)
    inner = body[start:end].strip()
    if not inner:
        result["reason"] = "empty_inner_statement"
        result["bare_like_substitute_used"] = False
        return result

    # Safe literal substitution: dollar-quote the needle so it can never break
    # out of the extracted statement. v_qn is the body's normalized-needle
    # placeholder.
    norm = _normalize_in_cluster(cluster, query)
    literal = f"$h013${norm}$h013$"
    # The extracted statement is the exact sparse branch, where v_dense is
    # false by definition. Substitute that branch-local PL/pgSQL boolean along
    # with the normalized needle so the statement is independently EXPLAINable.
    explained = inner.replace("v_qn", literal).replace("v_dense", "false")

    rc, plan = cluster.psql(f"EXPLAIN (COSTS OFF) {explained}")
    result["bare_like_substitute_used"] = False
    if rc != 0:
        result["reason"] = "explain_failed"
        return result
    # A top-level Function Scan would mean we accidentally EXPLAINed the wrapper
    # instead of the inner statement — reject it explicitly.
    if re.search(r"^\s*Function Scan", plan or "", re.M):
        result["reason"] = "top_level_function_scan"
        return result
    result["uses_trgm_index"] = (TRGM_IDX in (plan or ""))
    result["reason"] = ("trgm_index_in_inner_plan" if result["uses_trgm_index"]
                        else "trgm_index_absent_from_inner_plan")
    return result


def _normalize_in_cluster(cluster: LP.LocalCluster, query: str) -> str:
    """Normalize a query via the live IMMUTABLE normalizer (the same v_qn the
    body computes). Result is consumed in process, never serialized."""
    rc, out = cluster.psql(
        "select public.hivemind_normalize_identifier("
        f"$q${query}$q$)::text")
    return out.strip() if rc == 0 else query


def local_timing(cluster: LP.LocalCluster) -> dict[str, Any]:
    out: dict[str, Any] = {}
    runs, warmup = 9, 4
    out["runs"] = runs
    out["warmup"] = warmup
    out["interleaved"] = True
    out["index_scale_decoy_rows"] = _seed_index_scale_decoys(cluster)

    # ---- DENSE / common: every chunk of every matched item matches. ----
    dense_corpus = _seed_timing_corpus(
        cluster, base_id=200000, n_items=120, chunks_per=60,
        needle=DENSE_NEEDLE, matching_chunks=60, matching_position="all")
    dense_t = _median_interleaved(cluster, DENSE_NEEDLE, runs=runs, warmup=warmup)
    dense_ratio = (dense_t["median_ms_013"] / dense_t["median_ms_012"]
                   if dense_t["median_ms_012"] else None)
    out["dense"] = {
        "median_ms_012": dense_t["median_ms_012"],
        "median_ms_013": dense_t["median_ms_013"],
        "p95_ms_012": dense_t["p95_ms_012"],
        "p95_ms_013": dense_t["p95_ms_013"],
        "ratio_013_over_012": round(dense_ratio, 3) if dense_ratio is not None else None,
        "gate_max_ratio": 1.25,
        "corpus_items": dense_corpus["n_items"],
        "chunks_per_item": dense_corpus["chunks_per_item"],
        "matched_chunks_per_item": "all",
        "analyzed": bool(dense_corpus.get("analyzed")),
        "body_switch": dense_t["body_switch"],
        "mv_refresh_per_switch": dense_t["mv_refresh_per_switch"],
        "gate_pass": bool(dense_ratio is not None and dense_ratio <= 1.25),
    }

    # ---- SPARSE / selective: one matching chunk per item, and it is the OLDEST. ----
    sparse_corpus = _seed_timing_corpus(
        cluster, base_id=210000, n_items=120, chunks_per=60,
        needle=SPARSE_NEEDLE, matching_chunks=1, matching_position="oldest")
    # The exact sparse-match inner statement (under THIS corpus+needle) must use
    # the trigram GIN — proven by EXPLAINing the marker-delimited inner SELECT,
    # NOT a forced bare-LIKE probe.
    sparse_trgm = _real_plan_uses_trgm(cluster, SPARSE_NEEDLE)
    sparse_t = _median_interleaved(cluster, SPARSE_NEEDLE, runs=runs, warmup=warmup)
    sparse_ratio = (sparse_t["median_ms_013"] / sparse_t["median_ms_012"]
                    if sparse_t["median_ms_012"] else None)
    uses_trgm = bool(sparse_trgm.get("uses_trgm_index"))
    out["sparse"] = {
        "median_ms_012": sparse_t["median_ms_012"],
        "median_ms_013": sparse_t["median_ms_013"],
        "p95_ms_012": sparse_t["p95_ms_012"],
        "p95_ms_013": sparse_t["p95_ms_013"],
        "ratio_013_over_012": round(sparse_ratio, 3) if sparse_ratio is not None else None,
        "trgm_index_in_real_plan": uses_trgm,
        "trgm_evidence": sparse_trgm,
        "corpus_items": sparse_corpus["n_items"],
        "chunks_per_item": sparse_corpus["chunks_per_item"],
        "matched_chunks_per_item": 1,
        "matching_chunk_position": "oldest",
        "analyzed": bool(sparse_corpus.get("analyzed")),
        "body_switch": sparse_t["body_switch"],
        "mv_refresh_per_switch": sparse_t["mv_refresh_per_switch"],
        "gate_pass": bool(sparse_ratio is not None and sparse_ratio < 1.0 and uses_trgm),
    }

    # LOCAL-ONLY labeling. No production-gate claim.
    out["local_only_label"] = True
    out["claims_production_gate"] = False
    out["note"] = ("Local throwaway cluster, tiny vs production. Timings are a "
                   "relative 012-vs-013 signal on the anchor-rewrite hot path only; "
                   "NOT the production 750ms gate. Bodies are interleaved; medians "
                   "are reported. dense: 013 must be <=1.25x 012. sparse: 013 must "
                   "be faster than 012 AND the real plan must use the trigram GIN.")
    return out


# ---------------------------------------------------------------------------
# Main rehearsal
# ---------------------------------------------------------------------------


def rehearse() -> dict[str, Any]:
    cluster = LP.LocalCluster.start()
    ev: dict[str, Any] = {
        "task": "1.10/1.11 phase-3 latency fix (schema/013) — LOCAL-PROOF rehearsal",
        "verdict_path": str(VERDICT_PATH),
    }
    try:
        bootstrap_through_012(cluster)

        # proacl baseline after 012 (before 013).
        proacl_after_012 = R12.get_proacl(cluster)

        # (a) apply 013 cleanly after 012.
        apply_ok, apply_error = True, ""
        try:
            apply_013(cluster)
        except Exception as exc:  # noqa: BLE001
            apply_ok, apply_error = False, str(exc)
        ev["applied_ok"] = apply_ok
        ev["apply_error"] = "" if apply_ok else "apply_failed"

        if not apply_ok:
            ev.update({k: False for k in (
                "parity_all", "cross_ok", "anchor_ok", "fragment_bound_ok",
                "rollback_ok", "rollback_streams_equal", "idempotent_ok",
                "grants_preserved", "all_pass")})
            ev["n_pass"], ev["n_total"] = 0, 0
            _finalize(ev)
            return ev

        proacl_after_013 = R12.get_proacl(cluster)

        # (b) functional battery (name+ok only; details dropped for secret-safety).
        functional = R12.run_candidate_battery(cluster, label="013")
        ev["functional_assertions"] = [{"name": c["name"], "ok": c["ok"]} for c in functional]
        ev["n_pass"] = sum(1 for c in functional if c["ok"])
        ev["n_total"] = len(functional)

        # (c) full-row byte parity 013 == 012.
        s013, s012 = capture_parity(cluster)
        pv = parity_verdict(s013, s012)
        ev["parity"] = pv["per_query"]
        ev["parity_all"] = pv["parity_all"]

        # (d) cross-boundary negative parity.
        ev["cross_boundary"] = cross_boundary_proof(cluster)
        ev["cross_ok"] = ev["cross_boundary"]["cross_ok"]

        # (e) newest-matching-anchor byte parity.
        ev["anchor"] = anchor_proof(cluster)
        ev["anchor_ok"] = ev["anchor"]["anchor_ok"]

        # (g) fragment/MV 1..8000 bound.
        ev["fragment_bound"] = fragment_bound_proof(cluster)
        ev["fragment_bound_ok"] = ev["fragment_bound"]["fragment_bound_ok"]

        # (h) security (anon/auth/public denied + service-role RPC + quarantine).
        ev["security"] = security_proof(cluster)

        # (i) rollback to 012 returns the same rows.
        ev.update(rollback_proof(cluster))

        # (k) hot-path shape.
        ev["hot_path_plan"] = hot_path_proof(cluster)

        # (l) local timing.
        ev["local_timing"] = local_timing(cluster)

        # (j) idempotence: apply 013 twice, re-run the battery.
        idem_ok, idem_error = True, ""
        try:
            apply_013(cluster)
            apply_013(cluster)
        except Exception as exc:  # noqa: BLE001
            idem_ok, idem_error = False, str(exc)
        if idem_ok:
            idem_battery = R12.run_candidate_battery(cluster, label="013_idempotent")
            idem_ok = all(c["ok"] for c in idem_battery)
        ev["idempotent_ok"] = idem_ok

        # (h/grants) proacl preserved (013 == 012 == final).
        proacl_final = R12.get_proacl(cluster)
        ev["proacl_after_012"] = proacl_after_012
        ev["proacl_after_013"] = proacl_after_013
        ev["proacl_final"] = proacl_final
        ev["grants_preserved"] = (proacl_after_012 == proacl_after_013 == proacl_final)

        # verdict.
        functional_ok = all(c["ok"] for c in functional)
        dense_gate = bool(ev["local_timing"]["dense"]["gate_pass"])
        sparse_gate = bool(ev["local_timing"]["sparse"]["gate_pass"])
        ev["timing_dense_gate"] = dense_gate
        ev["timing_sparse_gate"] = sparse_gate
        ev["all_pass"] = bool(
            apply_ok and functional_ok and ev["parity_all"] and ev["cross_ok"]
            and ev["anchor_ok"] and ev["fragment_bound_ok"]
            and ev["security"]["security_ok"] and ev["rollback_ok"]
            and ev["idempotent_ok"] and ev["grants_preserved"]
            and ev["hot_path_plan"]["hot_path_ok"]
            and dense_gate and sparse_gate)
    finally:
        cluster.tear_down()

    _finalize(ev)
    return ev


def _finalize(ev: dict[str, Any]) -> None:
    VERDICT_PATH.parent.mkdir(parents=True, exist_ok=True)
    VERDICT_PATH.write_text(json.dumps(ev, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ev = rehearse()
    sec = ev.get("security", {})
    hp = ev.get("hot_path_plan", {})
    lt = ev.get("local_timing", {})
    print("=" * 72)
    print(f"schema/013 LOCAL-PROOF rehearsal: {'PASS' if ev.get('all_pass') else 'FAIL'}")
    print(f"  applied_ok         = {ev.get('applied_ok')}")
    print(f"  functional battery = {ev.get('n_pass')}/{ev.get('n_total')}")
    print(f"  parity_all (full)  = {ev.get('parity_all')}")
    print(f"  cross_ok           = {ev.get('cross_ok')}")
    print(f"  anchor_ok          = {ev.get('anchor_ok')}")
    print(f"  fragment_bound_ok  = {ev.get('fragment_bound_ok')}")
    print(f"  security_ok        = {sec.get('security_ok')}")
    print(f"  rollback_ok        = {ev.get('rollback_ok')} "
          f"(streams_equal={ev.get('rollback_streams_equal')})")
    print(f"  idempotent_ok      = {ev.get('idempotent_ok')}")
    print(f"  grants_preserved   = {ev.get('grants_preserved')}")
    print(f"  hot_path_ok        = {hp.get('hot_path_ok')} "
          f"(dense_marker={hp.get('has_dense_path_marker')} "
          f"sparse_marker={hp.get('has_sparse_path_marker')} "
          f"mat_sparse={hp.get('has_materialized_sparse_set')} "
          f"no_scalar={not hp.get('has_correlated_scalar_anchor')})")
    if lt:
        d = lt.get("dense", {})
        s = lt.get("sparse", {})
        print(f"  local timing (LOCAL-ONLY, interleaved medians):")
        print(f"    dense  : 012={d.get('median_ms_012')}ms 013={d.get('median_ms_013')}ms "
              f"ratio={d.get('ratio_013_over_012')} gate(<=1.25)={d.get('gate_pass')}")
        print(f"    sparse : 012={s.get('median_ms_012')}ms 013={s.get('median_ms_013')}ms "
              f"ratio={s.get('ratio_013_over_012')} trgm_real={s.get('trgm_index_in_real_plan')} "
              f"gate(<1.0+trgm)={s.get('gate_pass')}")
        print(f"    (production-gate claim={lt.get('claims_production_gate')})")
    print("-" * 72)
    print("full-row parity (013 == 012):")
    for qid, info in ev.get("parity", {}).items():
        flag = "OK " if info["identical"] else "DIFF"
        print(f"  [{flag}] {qid} n_013={info['n_013']} n_012={info['n_012']}")
    print("-" * 72)
    for c in ev.get("functional_assertions", []):
        if not c["ok"]:
            print(f"  FAIL {c['name']}")
    print("=" * 72)
    print(f"verdict written to {VERDICT_PATH}")
    return 0 if ev.get("all_pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
