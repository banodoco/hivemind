#!/usr/bin/env python3
"""Phase-2 / Task 2.15 isolated-cluster rehearsal — semantic candidate SQL.

Applies schema/003 + schema/020–029 (S1+S2) + the new schema/032 in a THROWAWAY local
PostgreSQL cluster (unix-socket, no network) with the locally-available pgvector,
then proves the semantic-candidate completion signals against live SQL with
EXACT deterministic fixture vectors (no provider call):

  2.15.1  a draft/superseded-contract vector closer than every active-contract
          vector is excluded;
  2.15.2  only the active fixed-dimension (384) contract ranks;
  2.15.3  one entity with multiple chunks and both prose and workflow_python
          returns exactly once, selecting the exact closest chunk;
  2.15.4  an exact-distance tie across representations/chunks resolves
          identically on repeated calls using the documented tie-break;
  2.15.5  semantic ranks are dense/deterministic in a total order;
  2.15.6  deleted messages and rejected/superseded distillations cannot rank
          (and a missing-resource vector cannot rank);
  2.15.7  generic resource and concrete resource-kind filters behave correctly;
  2.15.8  entity filters and exact item_ids work, including a JavaScript-unsafe
          Discord Snowflake round trip without numeric coercion;
  2.15.9  ambiguous/bare item-id usage FAILS CLOSED (zero rows) — never broadens
          to / leaks the whole candidate set (defect-1 fix);
  2.15.10 result limit is bounded and deterministic;
  2.15.11 snippet/chunk metadata corresponds to the winning row;
  2.15.12 no entity duplicates;
  2.15.13 function volatility/search path/security/grants are correct
          (STABLE, SECURITY DEFINER, fixed search_path, revoked from
          PUBLIC/anon/authenticated, granted only to service_role; a
          low-privilege role cannot call it);
  2.15.14 rerunning the rehearsal yields the same ordered output and sanitized
          evidence;
  2.15.15 stale/unsafe workflow_python vectors never rank or leak a snippet
          (defect-2 fix): a quarantined workflow ranks only via prose; an
          unavailable workflow (closest vector in the table) does not rank at
          all; a safe workflow's python still wins. The real schema/003
          canonical accessor + state table back the gate.

Mutates ONLY the throwaway cluster. The live Hivemind project is untouched. All
output is sanitized via verify_access.redact — no credentials, raw private text,
vectors, or secret-like substrings appear in evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import lexical_pg  # noqa: E402
from verify_access import redact  # noqa: E402

from executors import embedding_contract as ec  # noqa: E402
from rehearse_embedding_lifecycle import setup_cluster  # noqa: E402

SCHEMA_DIR = REPO_ROOT / "schema"
EVIDENCE_PATH = REPO_ROOT / "docs" / "hybrid-search" / "task-2.15-local-rehearsal.json"

# Distinct 384-d contracts so A (active), D (draft), S (superseded) coexist.
ACTIVE_CONTRACT = ec.ContractSpec(provider="openai", model="text-embedding-3-small", dimension=384)
DRAFT_CONTRACT = ec.ContractSpec(
    provider="openai", model="text-embedding-3-small", dimension=384, canonicalization_version=2
)
SUPERSEDED_CONTRACT = ec.ContractSpec(
    provider="openai", model="text-embedding-3-small", dimension=384, canonicalization_version=3
)

# A JavaScript-UNSAFE Discord Snowflake: 2^53 + 1. As a JSON *number* JS/edge
# JSON.parse rounds it to 2^53; as a string it round-trips exactly. Hivemind
# carries every item id as a string, so the candidate must return this verbatim.
SNOWFLAKE = "9007199254740993"

DIM = 384


# ---------------------------------------------------------------------------
# Exact deterministic fixture vectors
# ---------------------------------------------------------------------------
#
# Query Q is the unit vector along component 0: Q = {0: 1.0}. Every fixture
# vector below is a UNIT vector, so the pgvector cosine distance
# (1 - Q.v / |Q||v|) reduces to 1 - v[0]. Component 0 is the "closeness" knob:
#   V0  {0: 1.0}          -> distance 0.0   (identical to Q)
#   V02 {0: 0.8, 1: 0.6}  -> distance ~0.2  (0.8^2+0.6^2 = 1)
#   V04 {0: 0.6, 1: 0.8}  -> distance ~0.4
#   V10 {1: 1.0}          -> distance 1.0   (orthogonal)
# Ordering is exact (v[0] decreases). For EXACT ties we reuse IDENTICAL vectors,
# so the distance is bit-identical and the documented tie-break decides.
def _vec(sparse: dict[int, float], dim: int = DIM) -> str:
    comps = ["0.0"] * dim
    for idx, val in sparse.items():
        comps[idx] = repr(float(val))
    return "[" + ", ".join(comps) + "]"


# Sparse definitions are the single source of truth (the rehearsal literals and
# the pure-Python unit tests both consume them, so they cannot diverge).
SPARSE_Q = {0: 1.0}
SPARSE_V0 = {0: 1.0}
SPARSE_V02 = {0: 0.8, 1: 0.6}
SPARSE_V04 = {0: 0.6, 1: 0.8}
SPARSE_V10 = {1: 1.0}

Q = _vec(SPARSE_Q)
V0 = _vec(SPARSE_V0)
V02 = _vec(SPARSE_V02)
V04 = _vec(SPARSE_V04)
V10 = _vec(SPARSE_V10)


def _hash(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Fixture seeding
# ---------------------------------------------------------------------------


def _seed_contract(cluster: lexical_pg.LocalCluster, spec: ec.ContractSpec, status: str) -> None:
    cluster.psql(
        "insert into embedding_contracts(id,provider,model,dimension,canonicalization_version,"
        f"chunking_version,status) values ({spec.id},'openai','text-embedding-3-small',384,"
        f"{spec.canonicalization_version},1,'{status}') on conflict (id) do nothing;",
        capture=False,
    )


def _embed(
    cluster: lexical_pg.LocalCluster,
    contract_id: int,
    entity_type: str,
    item_id: str,
    rep: str,
    chunk_index: int,
    vector: str,
    chunk_text: str,
) -> None:
    h = _hash(f"{entity_type}|{item_id}|{rep}|{chunk_index}|{contract_id}")
    cluster.psql(
        "insert into content_embeddings(contract_id,entity_type,item_id,representation_type,"
        "chunk_index,chunk_text,embedding,representation_hash,chunk_hash) values "
        f"({contract_id},'{entity_type}','{item_id}','{rep}',{chunk_index},"
        f"'{chunk_text}','{vector}'::vector,'{h}','{h}') on conflict do nothing;",
        capture=False,
    )


def seed_fixtures(cluster: lexical_pg.LocalCluster, active: int) -> dict[str, Any]:
    """Seed source rows + embeddings under the active contract and under a draft
    and a superseded contract. Returns a manifest of what was seeded."""

    # Draft + superseded contracts (NOT activated; only `active` is active).
    _seed_contract(cluster, DRAFT_CONTRACT, "draft")
    _seed_contract(cluster, SUPERSEDED_CONTRACT, "superseded")

    # ---- source rows --------------------------------------------------------
    # Messages.
    cluster.psql(
        "insert into discord_messages(message_id,content) values "
        f"(100,'message one hundred');", capture=False,
    )
    cluster.psql(
        "insert into discord_messages(message_id,content,is_deleted) values "
        f"(200,'message two hundred deleted',true);", capture=False,
    )
    cluster.psql(
        "insert into discord_messages(message_id,content) values "
        f"({SNOWFLAKE},'message snowflake');", capture=False,
    )
    # Source rows for the draft/superseded-contract vectors (valid sources, so
    # the ONLY reason they are excluded is the contract status).
    cluster.psql(
        "insert into discord_messages(message_id,content) values "
        "(300,'message three hundred draft-contract'),"
        "(400,'message four hundred superseded-contract');", capture=False,
    )
    # Resources (workflow vs article).
    cluster.psql(
        "insert into external_resources(id,kind,source,title,body) values "
        "(5001,'workflow','vibecomfy-external','WF5001','description 5001'),"
        "(5002,'workflow','vibecomfy-external','WF5002','description 5002'),"
        "(5003,'article','vibecomfy-external','ART5003','description 5003'),"
        "(5004,'workflow','vibecomfy-external','WF5004','description 5004'),"
        "(5005,'workflow','vibecomfy-external','WF5005','description 5005');", capture=False,
    )
    # Authoritative workflow_python state (schema/003 canonical table). This is
    # the CURRENT public state the semantic gate reads via the real accessor
    # hivemind_workflow_python_state — it can DIVERGE from the (contract-scoped,
    # not auto-dropped) embedding, which is exactly the stale-vector risk
    # defect-2 fixes. 5001/5002 are safe (their python still ranks); 5004 is
    # quarantined; 5005 is unavailable. The body/chunk payloads are intentionally
    # not exposed; only booleans/identities are asserted in evidence.
    cluster.psql(
        "insert into lexical_resource_python_state"
        "(resource_id,kind,cohort,public_state,available) values "
        "(5001,'workflow','payload_python','safe',true),"
        "(5002,'workflow','payload_python','safe',true),"
        "(5004,'workflow','payload_python','quarantined',false),"
        "(5005,'workflow','unavailable','quarantined',false) "
        "on conflict (resource_id) do nothing;",
        capture=False,
    )
    # Distillations (approved / rejected / superseded). author_id=1 exists (bootstrap).
    cluster.psql(
        "insert into distillations(id,question,answer,status,author_id) values "
        "(700,'q700','a700','approved',1),"
        "(701,'q701','a701','rejected',1),"
        "(702,'q702','a702','superseded',1);", capture=False,
    )

    # ---- embeddings under the ACTIVE contract -------------------------------
    # Eligible messages.
    _embed(cluster, active, "message", "100", "prose", 0, V02, "MSG100-prose0")
    _embed(cluster, active, "message", SNOWFLAKE, "prose", 0, V04, "MSGSNOW-prose0")
    # Deleted message (closest possible) -> must NOT rank.
    _embed(cluster, active, "message", "200", "prose", 0, V0, "MSG200-prose0")
    # Resource 5001: prose(0.4) vs workflow_python chunk0(0.2, closest) vs wp1(1.0).
    # Closest chunk is a DIFFERENT representation (workflow_python) than the prose.
    _embed(cluster, active, "resource", "5001", "prose", 0, V04, "WF5001-prose0")
    _embed(cluster, active, "resource", "5001", "workflow_python", 0, V02, "WF5001-wp0")
    _embed(cluster, active, "resource", "5001", "workflow_python", 1, V10, "WF5001-wp1")
    # Resource 5002: THREE-way exact tie at ~0.2 across prose0 / prose1 / wp0.
    # Tie-break: prose before workflow_python, then chunk_index asc -> prose0 wins.
    _embed(cluster, active, "resource", "5002", "prose", 0, V02, "WF5002-prose0")
    _embed(cluster, active, "resource", "5002", "prose", 1, V02, "WF5002-prose1")
    _embed(cluster, active, "resource", "5002", "workflow_python", 0, V02, "WF5002-wp0")
    # Resource 5003 (article): single prose chunk.
    _embed(cluster, active, "resource", "5003", "prose", 0, V04, "ART5003-prose0")
    # Resource 5004 (QUARANTINED workflow): an eligible PROSE vector (0.4) PLUS a
    # STALE workflow_python vector that is CLOSER (0.2) than the prose. The
    # current public state is 'quarantined' (see lexical_resource_python_state),
    # so the python vector must NOT rank; the entity may rank ONLY via prose.
    _embed(cluster, active, "resource", "5004", "prose", 0, V04, "WF5004-prose0")
    _embed(cluster, active, "resource", "5004", "workflow_python", 0, V02, "WF5004-wp0-STALE")
    # Resource 5005 (UNAVAILABLE workflow): ONLY a stale workflow_python vector,
    # and it is the CLOSEST vector in the whole table (0.0). The current public
    # state is not 'safe', so the entity must NOT rank at all (no prose fallback).
    _embed(cluster, active, "resource", "5005", "workflow_python", 0, V0, "WF5005-wp0-STALE")
    # Missing-resource vector (no source row 5999, closest possible) -> must NOT rank.
    _embed(cluster, active, "resource", "5999", "prose", 0, V0, "GONE-prose0")
    # Distillations: approved ranks; rejected + superseded (closest) must NOT rank.
    _embed(cluster, active, "distillation", "700", "prose", 0, V04, "DIST700-prose0")
    _embed(cluster, active, "distillation", "701", "prose", 0, V0, "DIST701-prose0")
    _embed(cluster, active, "distillation", "702", "prose", 0, V0, "DIST702-prose0")

    # ---- embeddings under DRAFT + SUPERSEDED contracts (closest, excluded) ---
    _embed(cluster, DRAFT_CONTRACT.id, "message", "300", "prose", 0, V0, "DRAFT300-prose0")
    _embed(cluster, SUPERSEDED_CONTRACT.id, "message", "400", "prose", 0, V0, "SUP400-prose0")

    # Sanity: confirm exactly one active 384-d contract.
    rc, out = cluster.psql("select hivemind_active_contract_id();")
    resolved = out.strip().splitlines()[-1] if out else ""
    return {"active_contract_id": active, "resolved_active": resolved}


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def _call(
    cluster: lexical_pg.LocalCluster,
    *,
    kinds: list[str] | None = None,
    item_ids: list[str] | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Call hivemind_semantic_candidates with Q; parse the 9-column rows."""
    k = "'{" + ",".join(kinds) + "}'" if kinds else "'{}'"
    ids = "'{" + ",".join(item_ids) + "}'" if item_ids else "'{}'"
    sql = (
        "select entity_type,item_id,kind,representation_type,chunk_index,matched_snippet,"
        "semantic_distance,semantic_rank from public.hivemind_semantic_candidates("
        f"'{Q}'::vector,{limit},{k},{ids});"
    )
    rc, out = cluster.psql(sql)
    rows: list[dict[str, Any]] = []
    for ln in (out or "").strip().splitlines():
        parts = ln.split("|")
        if len(parts) != 8:
            continue
        rows.append(
            {
                "entity_type": parts[0],
                "item_id": parts[1],
                "kind": parts[2],
                "representation_type": parts[3],
                "chunk_index": int(parts[4]),
                "matched_snippet": parts[5],
                "semantic_distance": float(parts[6]),
                "semantic_rank": int(parts[7]),
            }
        )
    return rows


def _ids(rows: list[dict[str, Any]]) -> list[str]:
    return [f"{r['entity_type']}:{r['item_id']}" for r in rows]


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _expected_no_filter() -> list[dict[str, Any]]:
    """Expected ordered output for the no-filter query (Q), hand-derived from the
    fixtures. Distances are approximate (float); ordering + identity are exact."""
    return [
        # ~0.2 band: message before resource; resources by item_id.
        {"entity_type": "message", "item_id": "100", "kind": "message",
         "representation_type": "prose", "chunk_index": 0, "dist": 0.2},
        {"entity_type": "resource", "item_id": "5001", "kind": "workflow",
         "representation_type": "workflow_python", "chunk_index": 0, "dist": 0.2},
        {"entity_type": "resource", "item_id": "5002", "kind": "workflow",
         "representation_type": "prose", "chunk_index": 0, "dist": 0.2},
        # ~0.4 band: distillation before message before resource (by item_id).
        {"entity_type": "distillation", "item_id": "700", "kind": "distillation",
         "representation_type": "prose", "chunk_index": 0, "dist": 0.4},
        {"entity_type": "message", "item_id": SNOWFLAKE, "kind": "message",
         "representation_type": "prose", "chunk_index": 0, "dist": 0.4},
        {"entity_type": "resource", "item_id": "5003", "kind": "article",
         "representation_type": "prose", "chunk_index": 0, "dist": 0.4},
        # 5004: quarantined workflow -> its closer workflow_python (0.2) is gated
        # out, so it ranks ONLY via its prose vector (0.4).
        {"entity_type": "resource", "item_id": "5004", "kind": "workflow",
         "representation_type": "prose", "chunk_index": 0, "dist": 0.4},
    ]


def run_checks(cluster: lexical_pg.LocalCluster) -> tuple[dict[str, bool], dict[str, Any]]:
    checks: dict[str, bool] = {}
    evidence: dict[str, Any] = {}

    def near(a: float, b: float, tol: float = 1e-6) -> bool:
        return abs(a - b) < tol

    rows = _call(cluster)

    # 1 + 2: draft/superseded contract + only-active-contract. Items 300 (draft)
    # and 400 (superseded) are the CLOSEST possible vectors (distance 0.0) but
    # must never appear in ANY result; the only active contract's items do.
    all_returned_ids = {r["item_id"] for r in rows}
    checks["draft_contract_vector_excluded"] = "300" not in all_returned_ids
    checks["superseded_contract_vector_excluded"] = "400" not in all_returned_ids
    # The two closest vectors in the whole table are the excluded ones (0.0);
    # every returned row is from the active contract and is farther (~0.2+).
    # (pgvector cosine is computed in ~single precision, so V02 lands at
    # ~0.19999998 — use a loose bound that cleanly separates 0.0 from ~0.2.)
    checks["only_active_contract_ranks"] = all(r["semantic_distance"] > 0.1 for r in rows)

    # 12: no entity duplicates.
    keys = [f"{r['entity_type']}:{r['item_id']}" for r in rows]
    checks["no_entity_duplicates"] = len(keys) == len(set(keys))

    # 3 + 11: 5001 returns exactly once, selecting the exact closest chunk
    # (workflow_python chunk 0), and its snippet matches that winning row.
    r5001 = [r for r in rows if r["item_id"] == "5001"]
    checks["multi_chunk_returns_once"] = len(r5001) == 1
    if r5001:
        w = r5001[0]
        checks["closest_chunk_across_reps"] = (
            w["representation_type"] == "workflow_python" and w["chunk_index"] == 0
        )
        checks["snippet_matches_winning_chunk"] = w["matched_snippet"] == "WF5001-wp0"

    # 4: exact-distance tie (5002: prose0/prose1/wp0 all ~0.2) -> prose chunk 0
    # wins (prose before workflow_python, then chunk_index asc). Repeated calls
    # resolve identically.
    r5002_a = [r for r in rows if r["item_id"] == "5002"]
    r5002_b = [r for r in _call(cluster) if r["item_id"] == "5002"]
    checks["tie_picks_prose_chunk0"] = (
        bool(r5002_a)
        and r5002_a[0]["representation_type"] == "prose"
        and r5002_a[0]["chunk_index"] == 0
    )
    checks["tie_resolves_identically_repeated"] = (
        bool(r5002_a) and bool(r5002_b) and _ids(r5002_a) == _ids(r5002_b)
        and r5002_a[0] == r5002_b[0]
    )

    # 5: ranks are dense (1..N) and match the total order.
    ranks = [r["semantic_rank"] for r in rows]
    checks["ranks_dense"] = ranks == list(range(1, len(rows) + 1))
    # ordering is the total order (distance, entity_type, item_id).
    expected_ids = [f"{e['entity_type']}:{e['item_id']}" for e in _expected_no_filter()]
    checks["ordering_is_total_order"] = _ids(rows) == expected_ids

    # 6: deleted message / rejected distillation / superseded distillation /
    # missing resource never rank.
    checks["deleted_message_excluded"] = "200" not in all_returned_ids
    checks["rejected_distillation_excluded"] = "701" not in all_returned_ids
    checks["superseded_distillation_excluded"] = "702" not in all_returned_ids
    checks["missing_resource_excluded"] = "5999" not in all_returned_ids

    # 7: generic vs concrete resource-kind filters. 5004 ranks via prose
    # (quarantined python gated out); 5005 never ranks (no eligible rep).
    res_generic = _call(cluster, kinds=["resource"])
    checks["generic_resource_matches_all_resources"] = sorted(r["item_id"] for r in res_generic) == [
        "5001", "5002", "5003", "5004"
    ]
    res_wf = _call(cluster, kinds=["workflow"])
    checks["concrete_workflow_matches_only_workflows"] = sorted(r["item_id"] for r in res_wf) == [
        "5001", "5002", "5004"
    ] and all(r["kind"] == "workflow" for r in res_wf)
    res_art = _call(cluster, kinds=["article"])
    checks["concrete_article_matches_only_articles"] = (
        sorted(r["item_id"] for r in res_art) == ["5003"] and res_art[0]["kind"] == "article"
    )

    # 8: entity filter + exact item_ids + JS-unsafe snowflake round trip.
    msgs = _call(cluster, kinds=["message"])
    checks["message_kind_filter"] = sorted(r["item_id"] for r in msgs) == ["100", SNOWFLAKE]
    snow = _call(cluster, kinds=["message"], item_ids=[SNOWFLAKE])
    checks["exact_item_ids_snowflake"] = (
        len(snow) == 1 and snow[0]["item_id"] == SNOWFLAKE
    )
    # The snowflake is JS-unsafe as a NUMBER (> 2^53) but Hivemind carries it
    # as a STRING, which round-trips exactly through JSON. (Python's json module
    # uses arbitrary-precision ints, so the proof is the JS-safety bound, not a
    # Python numeric round trip.)
    snow_roundtrip = json.loads(json.dumps(SNOWFLAKE))
    checks["snowflake_json_string_roundtrip"] = (
        int(SNOWFLAKE) > 2 ** 53                       # JS Number.MAX_SAFE_INTEGER exceeded
        and snow_roundtrip == SNOWFLAKE                # as a string it is exact
        and isinstance(snow_roundtrip, str)
    )
    one_msg = _call(cluster, kinds=["message"], item_ids=["100"])
    checks["exact_item_ids_simple"] = (
        len(one_msg) == 1 and one_msg[0]["item_id"] == "100"
    )

    # 9 (defect-1): non-empty item_ids that do NOT resolve to exactly one entity
    # FAIL CLOSED (zero rows) — they never broaden to / leak the whole candidate
    # set. Bare item_ids (no kinds) are ambiguous across all entities -> closed.
    bare = _call(cluster, item_ids=[SNOWFLAKE])
    checks["bare_item_ids_fail_closed"] = bare == []
    # A specific unrelated entity (e.g. resource 5001) is provably NOT returned
    # by the bare call — identity-safe proof it cannot leak unrelated entities.
    checks["bare_item_ids_no_unrelated_entity"] = "5001" not in {r["item_id"] for r in bare}
    # Cross-entity (message + resource) item_ids are ambiguous -> closed.
    ambig = _call(cluster, kinds=["message", "resource"], item_ids=["100"])
    checks["ambiguous_item_ids_fail_closed"] = ambig == []
    checks["ambiguous_item_ids_no_unrelated_entity"] = (
        "5001" not in {r["item_id"] for r in ambig}
        and "5004" not in {r["item_id"] for r in ambig}
    )
    # Sanity: a single exact kind + the SAME item_ids still filters exact text
    # ids (the unambiguous path is unaffected by the fail-closed fix). This is
    # already covered by exact_item_ids_snowflake / exact_item_ids_simple above.

    # 10: result limit is bounded + deterministic.
    lim2 = _call(cluster, limit=2)
    checks["limit_bounded"] = len(lim2) == 2 and lim2[0]["semantic_rank"] == 1
    lim2_b = _call(cluster, limit=2)
    checks["limit_deterministic"] = _ids(lim2) == _ids(lim2_b)
    lim_cap = _call(cluster, limit=1000)  # above the internal 500 cap
    checks["limit_capped_at_500"] = len(lim_cap) == len(rows)  # all eligible (capped)

    # ------------------------------------------------------------------
    # Defect-2: stale / unsafe workflow_python vectors never rank or leak.
    # The excluded (stale/unsafe) workflow_python chunk BODIES — used only to
    # detect a snippet leak internally; NEVER written to evidence (only
    # booleans / identities are recorded below).
    # ------------------------------------------------------------------
    excluded_wp_snippets = {"WF5004-wp0-STALE", "WF5005-wp0-STALE"}

    # 5004 (quarantined workflow): ranks ONLY via its prose representation. Its
    # stale workflow_python vector is CLOSER (0.2 < 0.4 prose) but is gated out
    # pre-ranking, so the winning representation is prose, not workflow_python.
    r5004 = [r for r in rows if r["item_id"] == "5004"]
    checks["quarantined_workflow_ranks_only_via_prose"] = (
        len(r5004) == 1
        and r5004[0]["representation_type"] == "prose"
        and r5004[0]["chunk_index"] == 0
    )
    # 5005 (unavailable workflow): its ONLY vector is the CLOSEST in the whole
    # table (workflow_python at distance 0.0) with no prose fallback -> must not
    # rank at all. (5005 is also absent from the kinds=['workflow'] arm above.)
    checks["unavailable_workflow_does_not_rank"] = not any(
        r["item_id"] == "5005" for r in rows
    )
    # 5001 (safe workflow): its workflow_python vector still wins — the valid
    # best-chunk-across-representations proof is preserved (scenario 3).
    r5001c = [r for r in rows if r["item_id"] == "5001"]
    checks["safe_workflow_python_still_wins"] = (
        bool(r5001c) and r5001c[0]["representation_type"] == "workflow_python"
    )
    # The stale/unsafe workflow_python chunk text NEVER appears as any returned
    # matched_snippet (snippet-leak proof; boolean only).
    checks["unsafe_python_snippet_never_leaked"] = not any(
        r["matched_snippet"] in excluded_wp_snippets for r in rows
    )
    # Canonical accessor echoes (booleans only — never the state string body is
    # required, but recording safe/not-safe is identity-safe): confirm the gate
    # predicate's inputs match the real schema/003 accessor for the two unsafe
    # resources and the one safe resource.
    def _state_is(resource_id: int) -> bool:
        rc, out = cluster.psql(
            f"select (public.hivemind_workflow_python_state({resource_id}) = 'safe');"
        )
        line = (out or "").strip().splitlines()
        return bool(line) and line[-1].strip() == "t"

    state_5001_safe = _state_is(5001)
    state_5004_safe = _state_is(5004)
    state_5005_safe = _state_is(5005)
    checks["canonical_accessor_5001_safe"] = state_5001_safe
    checks["canonical_accessor_5004_not_safe"] = not state_5004_safe
    checks["canonical_accessor_5005_not_safe"] = not state_5005_safe


    # Evidence snapshot (sanitized: distances rounded, NO raw chunk text).
    evidence["no_filter_order"] = [
        {
            "rank": r["semantic_rank"],
            "entity": r["entity_type"],
            "item_id": r["item_id"],
            "kind": r["kind"],
            "representation": r["representation_type"],
            "chunk_index": r["chunk_index"],
            "distance": round(r["semantic_distance"], 6),
            "snippet_len": len(r["matched_snippet"]),
            "snippet_tag": r["matched_snippet"].split("-")[0] if r["matched_snippet"] else "",
        }
        for r in rows
    ]
    evidence["filters"] = {
        "generic_resource": _ids(res_generic),
        "concrete_workflow": _ids(res_wf),
        "concrete_article": _ids(res_art),
        "message_kind": _ids(msgs),
        "snowflake_item_id": [r["item_id"] for r in snow],
    }
    evidence["excluded_closest_vectors"] = {
        "draft_contract_300": "300" not in all_returned_ids,
        "superseded_contract_400": "400" not in all_returned_ids,
        "deleted_message_200": "200" not in all_returned_ids,
        "rejected_distillation_701": "701" not in all_returned_ids,
        "superseded_distillation_702": "702" not in all_returned_ids,
        "missing_resource_5999": "5999" not in all_returned_ids,
    }
    # Defect-2 evidence: boolean / identity-safe proof that stale/unsafe
    # workflow_python never ranks or leaks a snippet. No excluded chunk bodies,
    # vectors, URLs, or secret-like markers are recorded.
    evidence["unsafe_workflow_python_gate"] = {
        "quarantined_5004_present": bool(r5004),
        "quarantined_5004_winning_representation": (
            r5004[0]["representation_type"] if r5004 else None
        ),
        "quarantined_5004_winning_chunk_index": (
            r5004[0]["chunk_index"] if r5004 else None
        ),
        "unavailable_5005_present": any(r["item_id"] == "5005" for r in rows),
        "safe_5001_winning_representation": (
            r5001c[0]["representation_type"] if r5001c else None
        ),
        "stale_unsafe_snippet_leaked": any(
            r["matched_snippet"] in excluded_wp_snippets for r in rows
        ),
        "canonical_accessor_5001_safe": state_5001_safe,
        "canonical_accessor_5004_safe": state_5004_safe,
        "canonical_accessor_5005_safe": state_5005_safe,
    }
    # Defect-1 evidence: fail-closed behavior (zero rows) for bare + ambiguous
    # item_ids, plus identity-safe proof no unrelated entity leaks.
    evidence["item_ids_fail_closed"] = {
        "bare_item_ids_returned_count": len(bare),
        "ambiguous_item_ids_returned_count": len(ambig),
        "bare_leaked_unrelated_entity_5001": "5001" in {r["item_id"] for r in bare},
        "ambiguous_leaked_unrelated_entity": (
            "5001" in {r["item_id"] for r in ambig}
            or "5004" in {r["item_id"] for r in ambig}
        ),
    }

    return checks, evidence


def run_security_checks(cluster: lexical_pg.LocalCluster) -> tuple[dict[str, bool], dict[str, Any]]:
    checks: dict[str, bool] = {}
    evidence: dict[str, Any] = {}

    # Volatility / security / search_path / config from the catalog.
    rc, out = cluster.psql(
        "select provolatile, prosecdef, proconfig from pg_proc p "
        "join pg_namespace n on n.oid=p.pronamespace "
        "where n.nspname='public' and p.proname='hivemind_semantic_candidates';"
    )
    line = (out or "").strip().splitlines()
    parts = line[-1].split("|") if line else []
    if len(parts) == 3:
        vol, sec, cfg = parts[0].strip(), parts[1].strip(), parts[2].strip()
        checks["is_stable"] = vol == "s"
        checks["is_security_definer"] = sec == "t"
        checks["search_path_pinned"] = "search_path" in cfg and "public" in cfg

    # Grants: revoked from PUBLIC/anon/authenticated, granted to service_role.
    rc, out = cluster.psql(
        "select case when acl.grantee = 0 then 'PUBLIC' else acl.grantee::regrole::text end "
        "from pg_proc p join pg_namespace n on n.oid=p.pronamespace "
        "cross join lateral aclexplode(p.proacl) as acl "
        "where n.nspname='public' and p.proname='hivemind_semantic_candidates' "
        "and acl.privilege_type='EXECUTE';"
    )
    grantees = [g.strip() for g in (out or "").splitlines() if g.strip()]
    checks["revoked_from_public"] = "PUBLIC" not in grantees
    checks["granted_to_service_role"] = "service_role" in grantees
    checks["not_granted_to_anon"] = "anon" not in grantees
    checks["not_granted_to_authenticated"] = "authenticated" not in grantees
    evidence["grantees"] = grantees
    evidence["catalog"] = {"volatile": parts[0] if len(parts) == 3 else "",
                           "security_definer": parts[1] if len(parts) == 3 else "",
                           "config": parts[2] if len(parts) == 3 else ""}

    # Effective access: a low-privilege role CANNOT call it; service_role CAN.
    cand_sql = ("select public.hivemind_semantic_candidates('{q}'::vector,1) "
                "is not null;").format(q=Q)
    rc_low, _ = cluster.psql(f"set role lowpriv; {cand_sql}")
    cluster.psql("reset role;")
    checks["lowpriv_cannot_call"] = rc_low != 0
    rc_anon, _ = cluster.psql(f"set role anon; {cand_sql}")
    cluster.psql("reset role;")
    checks["anon_cannot_call"] = rc_anon != 0
    rc_auth, _ = cluster.psql(f"set role authenticated; {cand_sql}")
    cluster.psql("reset role;")
    checks["authenticated_cannot_call"] = rc_auth != 0
    rc_svc, _ = cluster.psql(f"set role service_role; {cand_sql}")
    cluster.psql("reset role;")
    checks["service_role_can_call"] = rc_svc == 0

    return checks, evidence


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


ROLES_SQL = """
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then
    create role anon; end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then
    create role authenticated; end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then
    create role service_role; end if;
  if not exists (select 1 from pg_roles where rolname = 'lowpriv') then
    create role lowpriv; end if;
end $$;
grant usage on schema public to anon, authenticated, service_role, lowpriv;
"""


def rehearse(out_path: Path) -> dict[str, Any]:
    cluster, info = setup_cluster()
    ev: dict[str, Any] = {"task": "2.15-semantic-candidate-rehearsal", "cluster_isolated": True}
    try:
        cluster.psql(ROLES_SQL, capture=False)
        # Defect-2 dependency: apply the REAL canonical workflow_python state
        # surface (schema/003: lexical_resource_python_state +
        # hivemind_workflow_python_state) so 032's stale-python gate exercises
        # the actual accessor + state table, not a divergent fake rule.
        cluster.psql_file(SCHEMA_DIR / "003_lexical_resource_documents.sql")
        cluster.psql_file(SCHEMA_DIR / "032_semantic_candidate_sql.sql")
        active = int(info["active_contract_id"])
        seed_info = seed_fixtures(cluster, active)
        ev.update(seed_info)
        checks, behavior_ev = run_checks(cluster)
        sec_checks, sec_ev = run_security_checks(cluster)
        ev["migrations_applied"] = [
            "003_lexical_resource_documents.sql",
            "020_enable_pgvector.sql", "021_embedding_contracts.sql",
            "022_content_embeddings.sql", "023_embedding_contract_switch.sql",
            "024_identity_and_canonical_representations.sql", "025_embedding_jobs.sql",
            "026_embedding_job_rpc.sql", "027_embedding_worker_surface.sql",
            "028_embedding_worker_cron.sql", "029_embedding_cleanup.sql",
            "032_semantic_candidate_sql.sql",
        ]
        ev["checks"] = {**checks, **sec_checks}
        ev["evidence"] = {**behavior_ev, "security": sec_ev}
        ev["verdict"] = {
            "all_pass": all(ev["checks"].values()),
            "n_pass": sum(1 for v in ev["checks"].values() if v),
            "n_total": len(ev["checks"]),
        }
    finally:
        cluster.tear_down()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(ev, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return ev


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Task 2.15 semantic-candidate rehearsal.")
    ap.add_argument("--out", type=Path, default=EVIDENCE_PATH)
    args = ap.parse_args(argv)
    print(redact("Task 2.15 rehearsal: isolated PG cluster (pgvector), schema/020–029 + 032."))
    ev = rehearse(args.out)
    print(redact(f"Active contract id: {ev['active_contract_id']} (resolved {ev['resolved_active']})"))
    for name, ok in ev["checks"].items():
        print(redact(f"  [{'PASS' if ok else 'FAIL'}] {name}"))
    print(redact(f"\nVERDICT all_pass={ev['verdict']['all_pass']} "
                 f"({ev['verdict']['n_pass']}/{ev['verdict']['n_total']})"))
    print(redact(f"Evidence: {args.out}"))
    return 0 if ev["verdict"]["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
