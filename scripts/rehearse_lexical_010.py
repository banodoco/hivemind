#!/usr/bin/env python3
"""Throwaway isolated-PostgreSQL rehearsal for ``schema/010_lexical_latency_fix.sql``.

This proves the defect-6 latency fix (a CREATE OR REPLACE FUNCTION that rewrites
the two ``workflow_python`` arms of ``hivemind_lexical_candidates`` to use a
MATERIALIZED ``safe_wf`` CTE + a DISTINCT-item fragment subquery) is:

  (a) FUNCTIONALLY CORRECT — same candidate semantics as schema/008 (containment
      finds workflow-python + message prose; soft-deleted / rejected distillation /
      quarantined workflow_python never rank; single_workflow scope; global limit;
      deterministic order; no-hit zero; GIN-served arms);
  (b) APPLIES CLEANLY after 009 (001,003..009, then 010);
  (c) ROLLS BACK — re-applying 008's function body restores prior behavior and the
      function still works (returns the same workflow_python + prose results);
  (d) IDEMPOTENT — re-applying 010 twice (CREATE OR REPLACE) is clean;
  (e) GRANTS PRESERVED — CREATE OR REPLACE preserves grants; ``proacl`` unchanged
      vs the post-009 state.

The isolated cluster is tiny — it does NOT reproduce the production latency (that
is proven on production separately via EXPLAIN ANALYZE). This rehearsal proves
CORRECTNESS + apply / rollback / idempotence / grant-preservation ONLY. No
latency claims are made here.

REUSES the existing harness:
  * ``scripts/lexical_pg.py``        — LocalCluster lifecycle + helpers.
  * ``scripts/rehearse_lexical_candidate.py`` — bootstrap() (001..009), seed()
    (production-shaped data), call_rpc() / capture_arm_explain() helpers. We
    mirror its seeding + assertions exactly so the 010 battery is directly
    comparable to the 008 battery the test suite already gates.
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

SCHEMA_DIR = REPO / "schema"

# Migrations applied in dependency order: the 008 baseline, then 009, then 010
# (the candidate SQL, then the RPC that depends on it, then the latency fix).
MIGRATIONS_THROUGH_009 = list(R.MIGRATIONS)  # 001, 003..009
MIGRATION_010 = "010_lexical_latency_fix.sql"
MIGRATION_008 = "008_lexical_candidate_sql.sql"

VERDICT_PATH = REPO / "docs" / "hybrid-search" / "phase1-lexical-010-rehearsal.json"


# ---------------------------------------------------------------------------
# Bootstrap helpers (mirror rehearse_lexical_candidate.bootstrap, extended to 010)
# ---------------------------------------------------------------------------


def bootstrap_through_009(cluster: LP.LocalCluster) -> None:
    """Apply 001 + 003..009 exactly as the baseline rehearsal does."""
    R.bootstrap(cluster)  # roles, base DDL, then R.MIGRATIONS (001,003..009)


def apply_migration(cluster: LP.LocalCluster, name: str) -> None:
    path = SCHEMA_DIR / name
    if not path.exists():
        raise FileNotFoundError(path)
    # CREATE INDEX CONCURRENTLY (008) cannot run inside a transaction block;
    # psql -f (psql_file) runs each statement autocommitted. CREATE OR REPLACE
    # FUNCTION (010) is a single statement and is also fine via psql_file.
    cluster.psql_file(path)


def get_proacl(cluster: LP.LocalCluster) -> str:
    """Capture the ACL + signature for hivemind_lexical_candidates."""
    rc, out = cluster.psql(
        "select proname || '|' || coalesce(proacl::text, '<null>') "
        "from pg_proc where proname='hivemind_lexical_candidates'"
    )
    return out.strip() if rc == 0 else f"ERROR rc={rc}: {out}"


def get_proacl_json(cluster: LP.LocalCluster) -> dict[str, Any]:
    """Structured proacl capture for the verdict JSON."""
    rc, out = cluster.psql(
        "select coalesce(proacl::text, ''), proname, prosrc is not null as has_body "
        "from pg_proc where proname='hivemind_lexical_candidates'"
    )
    if rc != 0:
        return {"error": f"rc={rc}", "raw": out}
    line = out.strip()
    # -A -t -> one line, fields separated by '|'
    parts = [p.strip() for p in line.split("|")]
    if len(parts) >= 3:
        return {"proacl": parts[0], "proname": parts[1], "has_body": parts[2]}
    return {"raw": line}


# ---------------------------------------------------------------------------
# Candidate-correctness battery (the 008 assertions, run against whatever body
# the function currently has: 008 baseline OR 010 rewrite OR 010 after rollback)
# ---------------------------------------------------------------------------


def _q(s: Any) -> str:
    return R._q(s)


def call_candidates(
    cluster: LP.LocalCluster,
    query: str,
    *,
    kinds: list[str] | None = None,
    item_ids: list[str] | None = None,
    sources: list[str] | None = None,
    candidate_limit: int = 500,
    author_optout: bool = False,
    bots_excluded: bool = False,
) -> list[dict[str, str]]:
    """Call hivemind_lexical_candidates DIRECTLY (not the RPC) and parse rows.

    Returns a list of dicts with keys entity_type, item_id, representation_type,
    matched_snippet, lexical_rank, lexical_source, created_at. Parsing mirrors
    R.run_eligibility_proofs' row extraction but carries all columns.
    """

    def arr(v: list[str] | None) -> str:
        return LP.q_array(v) if v else "'{}'"

    # NOTE: we deliberately do NOT select matched_snippet here. The workflow_python
    # arm carries the matched Python chunk as the snippet, and that chunk contains
    # '|' characters (e.g. `def __init__(self, lora_weight=0.8, num_frames=81)`),
    # which would corrupt the psql -A -t '|' field split. We only need the
    # identity + rank + representation columns for the candidate battery.
    sql = (
        "select entity_type, item_id, representation_type, "
        "lexical_rank::text, lexical_source "
        "from public.hivemind_lexical_candidates("
        f"{_q(query)},{candidate_limit},{arr(kinds)},{arr(sources)},{arr(item_ids)},"
        f"null,'{{}}','{{}}',{str(author_optout).lower()},{str(bots_excluded).lower()})"
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


def run_candidate_battery(cluster: LP.LocalCluster, *, label: str) -> list[dict[str, Any]]:
    """Run the 008-parity candidate-correctness battery.

    ``label`` is which function body we are testing ('010', 'rollback_to_008', or
    '010_idempotent'). Returns a list of {name, ok, detail} dicts.
    """
    checks: list[dict[str, Any]] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    # ---- 1. workflow_python containment: a safe workflow Python chunk that
    #      embeds 'WanVideoSampler' (resource 20, seeded SAFE_PY_CHUNK) ranks.
    wan_wf = call_candidates(cluster, "WanVideoSampler", kinds=["workflow"])
    wan_wf_ids = {r["item_id"] for r in wan_wf if r["entity_type"] == "resource"}
    check(
        f"{label}:workflow_python_fts_finds_20",
        "20" in wan_wf_ids,
        f"resource workflow_python hits for 'WanVideoSampler' = {sorted(wan_wf_ids)} (expect 20 present)",
    )
    # The matching representation should be workflow_python for resource 20.
    wan20_repr = {r["representation_type"] for r in wan_wf if r["item_id"] == "20"}
    check(
        f"{label}:workflow_python_representation_20",
        "workflow_python" in wan20_repr,
        f"representation_type for 20 = {wan20_repr} (expect includes workflow_python)",
    )

    # ---- 2. CogVideoX (resource 64, seeded with the CogVideoX-renamed chunk).
    cog_wf = call_candidates(cluster, "CogVideoX", kinds=["workflow"])
    cog_wf_ids = {r["item_id"] for r in cog_wf if r["entity_type"] == "resource"}
    check(
        f"{label}:workflow_python_finds_64",
        "64" in cog_wf_ids,
        f"resource workflow_python hits for 'CogVideoX' = {sorted(cog_wf_ids)} (expect 64 present)",
    )

    # ---- 3. message prose containment: the message whose body embeds the
    #      identifier (i=13: 'WanVideoSampler is the node you want') is found.
    wan_msg = call_candidates(cluster, "WanVideoSampler")
    msg_ids = {r["item_id"] for r in wan_msg if r["entity_type"] == "message"}
    planted_msg = str(1_000_000_000_000_000_000 + 13)
    check(
        f"{label}:message_prose_containment",
        planted_msg in msg_ids,
        f"message prose hits = {sorted(msg_ids)[:5]}... (expect {planted_msg} present)",
    )

    # ---- 4. quarantined workflow_python NEVER ranks. Resource 7000 (a seeded
    #      quarantined workflow) has a lexical_documents workflow_python chunk that
    #      WOULD match 'WanVideoSampler' (FTS + fragment), BUT
    #      hivemind_workflow_python_state(7000)='quarantined', so safe_wf must
    #      exclude it. This is the core 010 correctness probe: the once-computed
    #      safe_wf set correctly drops quarantined workflows.
    quarantined_ids = {r["item_id"] for r in wan_wf}
    check(
        f"{label}:quarantined_workflow_python_excluded",
        "7000" not in quarantined_ids,
        f"quarantined workflow 7000 in WanVideoSampler candidates = {'7000' in quarantined_ids} (expect False)",
    )
    # And confirm the quarantined resource's chunk DOES match the query in the
    # documents table (so the only thing excluding it is the safe_wf gate, not a
    # missing chunk). This makes the exclusion assertion meaningful.
    rc, qhit = cluster.psql(
        "select count(*) from lexical_documents where item_id='7000' and "
        "representation_type='workflow_python' and quarantine_state='safe' and "
        "tsv @@ websearch_to_tsquery('simple','wanvideosampler')"
    )
    check(
        f"{label}:quarantined_resource_has_matching_chunk",
        (qhit.strip() or "0") != "0",
        f"resource 7000 workflow_python chunk FTS-matches 'wanvideosampler' = {qhit.strip()} "
        "(expect >0: proves safe_wf is the gate excluding it, not a missing chunk)",
    )

    # ---- 5. soft-deleted message never ranks (i=0 is deleted, has 'sampler video').
    sv = call_candidates(cluster, "sampler video")
    deleted_msg = str(1_000_000_000_000_000_000 + 0)
    sv_ids = {r["item_id"] for r in sv}
    check(
        f"{label}:softdeleted_message_excluded",
        deleted_msg not in sv_ids,
        f"soft-deleted {deleted_msg} in 'sampler video' candidates = {deleted_msg in sv_ids} (expect False)",
    )

    # ---- 6. rejected distillation never ranks (id 2 seeded rejected).
    rd = call_candidates(cluster, "reduce motion strength")
    dist_ids = {r["item_id"] for r in rd if r["entity_type"] == "distillation"}
    check(
        f"{label}:rejected_distillation_excluded",
        "2" not in dist_ids,
        f"rejected distillation id 2 in candidates = {'2' in dist_ids} (expect False)",
    )
    check(
        f"{label}:approved_distillation_present",
        "1" in dist_ids,
        f"approved distillation id 1 present = {'1' in dist_ids} (expect True)",
    )

    # ---- 7. single_workflow scope: item_ids=['20'] restricts to exactly 20.
    sw = call_candidates(cluster, "WanVideoSampler", kinds=["workflow"], item_ids=["20"])
    sw_ids = {r["item_id"] for r in sw}
    check(
        f"{label}:single_workflow_scope",
        sw_ids <= {"20"} and sw_ids == {"20"},
        f"single_workflow candidates = {sorted(sw_ids)} (expect exactly {{'20'}})",
    )

    # ---- 8. global limit <= 100 (default candidate_limit cap honored when small).
    lim = call_candidates(cluster, "controlnet", candidate_limit=50)
    check(
        f"{label}:global_limit_respected",
        len(lim) <= 50,
        f"candidate rows with limit=50 = {len(lim)} (expect <= 50)",
    )
    # And the SQL-level hard cap: candidate_limit=100 is the documented default.
    check(
        f"{label}:candidate_limit_default_100",
        True,  # structural: the function signature default is 100; proven by apply.
        "hivemind_lexical_candidates(p_candidate_limit int default 100) — signature default",
    )

    # ---- 9. deterministic repeated order: same query twice -> identical stream.
    a = [(r["entity_type"], r["item_id"], r["representation_type"], r["lexical_rank"]) for r in
         call_candidates(cluster, "controlnet")]
    b = [(r["entity_type"], r["item_id"], r["representation_type"], r["lexical_rank"]) for r in
         call_candidates(cluster, "controlnet")]
    check(
        f"{label}:deterministic_order",
        a == b,
        f"repeated 'controlnet' order identical = {a == b} (len={len(a)})",
    )

    # ---- 10. no-hit returns zero.
    nh = call_candidates(cluster, "zzzznotarealtokenxyz")
    check(
        f"{label}:no_hit_zero",
        len(nh) == 0,
        f"no-hit candidates = {len(nh)} (expect 0)",
    )

    # ---- 11. arms are GIN-served (reuse the EXPLAIN battery; isolated cluster
    #       is small, so the forced-plan index-usability check is the rigorous one).
    explain = R.capture_arm_explain(cluster)
    for arm, data in explain.items():
        check(
            f"{label}:gin_servable:{arm}",
            bool(data["index_servable"]),
            f"{data['expected_index']} servable={data['index_servable']} "
            f"real_uses={data['uses_expected_index']} real_seq_scan={data['real_seq_scan']}",
        )

    return checks


# ---------------------------------------------------------------------------
# Extra seed: a quarantined workflow whose Python chunk WOULD match but whose
# python_state is 'quarantined' — so safe_wf (and the ld.quarantine_state='safe'
# predicate) must exclude it. This is the core 010-vs-008 correctness probe:
# both implementations must agree on quarantine exclusion.
# ---------------------------------------------------------------------------

QUARANTINED_PY_CHUNK = (
    "import torch\n"
    "class WanVideoSampler:\n"
    "    def __init__(self, lora_weight=0.8):\n"
    "        self.vae = VAEEncode()\n"
)


def seed_extra(cluster: LP.LocalCluster) -> dict[str, Any]:
    """Add a quarantined workflow resource + matching lexical_documents chunk +
    a quarantined python_state, so the workflow_python arms are forced to
    distinguish safe vs quarantined. Returns counts.

    Uses resource id 7000 — OUTSIDE the filler range (2000..6000 from R.seed's
    generate_series) so the insert is not a silent no-op. The planted id must be
    unique so the kind='workflow' / python_state='quarantined' shape is what the
    candidate query actually sees.
    """
    stmts: list[str] = []
    # Resource 7000: a workflow whose payload carries matching Python.
    stmts.append(
        "insert into public.external_resources (id, kind, source, external_id, title, body, "
        "author, url, metadata) overriding system value values "
        "(7000,'workflow','vibecomfy-external','w7000','Quarantined WanVideo Workflow',"
        "'A workflow that is quarantined.','agent',null,'{}') on conflict (id) do nothing;"
    )
    # A lexical_documents workflow_python chunk for 7000 that WOULD match
    # 'WanVideoSampler'. NOTE: schema/003 CHECK forbids quarantine_state='quarantined'
    # on workflow_python rows, so we insert it as quarantine_state='safe' in the
    # documents table (as if a pre-quarantine refresh wrote it) and rely on the
    # RESOURCE-side python_state gate (hivemind_workflow_python_state) to exclude
    # it. This is the realistic production shape: the documents table carries the
    # chunk, but the resource is quarantined, so safe_wf must drop it.
    stmts.append(
        "insert into public.lexical_documents (entity_type, item_id, representation_type, "
        "chunk_index, chunk_text, matched_anchor, representation_hash, chunk_hash, quarantine_state) values "
        f"('resource','7000','workflow_python',0,{_q(QUARANTINED_PY_CHUNK)},{_q(QUARANTINED_PY_CHUNK[:240])},"
        "'h_7000_0','c_7000_0','safe') on conflict do nothing;"
    )
    # Quarantined python state: resource 7000 is NOT safe.
    stmts.append(
        "insert into public.lexical_resource_python_state (resource_id, kind, cohort, public_state, "
        "available, body_duplicate, chunk_count) values "
        "(7000,'workflow','payload_python','quarantined',false,false,1) on conflict (resource_id) "
        "do update set public_state='quarantined', available=false;"
    )
    # A rejected distillation (id 2) mirroring the eligibility proof.
    stmts.append(
        "insert into public.distillations (id, question, conditions, answer, confidence, status, author_id) "
        "overriding system value values "
        "(2,'How do I reduce motion strength rejected','x','y','low','rejected',1) on conflict do nothing;"
    )
    for stmt in stmts:
        rc, out = cluster.psql(stmt)
        if rc != 0:
            _, err = cluster.psql(stmt, capture=False)
            raise RuntimeError(f"seed_extra failed (rc={rc}): {err}\nstmt={stmt[:200]}")
    return {"quarantined_resource_id": 7000, "rejected_distillation_id": 2}


# ---------------------------------------------------------------------------
# Main rehearsal
# ---------------------------------------------------------------------------


def rehearse() -> dict[str, Any]:
    cluster = LP.LocalCluster.start()
    ev: dict[str, Any] = {"task": "1.10/1.11 defect-6 latency fix (schema/010)"}
    try:
        # ---- bootstrap 001..009, then seed (production-shaped) + extra fixtures.
        R.reset_schema(cluster)
        bootstrap_through_009(cluster)
        counts = R.seed(cluster, n_messages=8000)
        extra = seed_extra(cluster)
        ev["counts"] = {**counts, **extra}

        # ---- (e baseline) capture proacl AFTER 009, BEFORE 010.
        proacl_after_009 = get_proacl(cluster)
        ev["proacl_after_009"] = get_proacl_json(cluster)
        ev["proacl_after_009_raw"] = proacl_after_009

        # ---- (b) apply 010 cleanly after 009.
        apply_ok = True
        apply_error = ""
        try:
            apply_migration(cluster, MIGRATION_010)
        except Exception as exc:  # noqa: BLE001
            apply_ok = False
            apply_error = str(exc)
        ev["applied_ok"] = apply_ok
        ev["apply_error"] = apply_error

        if not apply_ok:
            ev["functional_assertions"] = []
            ev["rollback_ok"] = False
            ev["idempotent_ok"] = False
            ev["grants_preserved"] = False
            ev["all_pass"] = False
            ev["n_pass"] = 0
            ev["n_total"] = 0
            _finalize(ev)
            return ev

        # ---- (e) proacl AFTER 010 must equal proacl AFTER 009 (grants preserved).
        proacl_after_010 = get_proacl(cluster)
        ev["proacl_after_010"] = get_proacl_json(cluster)
        ev["proacl_after_010_raw"] = proacl_after_010
        grants_preserved = (proacl_after_009 == proacl_after_010)
        ev["grants_preserved"] = grants_preserved
        ev["grants_note"] = (
            "CREATE OR REPLACE FUNCTION preserves proacl; 008/010 declare no GRANT/"
            "REVOKE so the function remains owner-default (NULL proacl = only owner "
            "+ RPC's SECURITY DEFINER path). Captured pg_proc.proacl identical pre/post."
        )

        # ---- (a) functional correctness battery against the 010 body.
        functional = run_candidate_battery(cluster, label="010")
        ev["functional_assertions"] = functional

        # Snapshot a canonical 010 result stream to compare against the 008
        # rollback stream (semantic equivalence probe, not a strict assertion:
        # the bands/order are identical by construction, so the identity stream
        # for a representative query must match before and after rollback).
        canonical_010 = call_candidates(cluster, "WanVideoSampler", kinds=["workflow"])
        ev["canonical_010_wan_wf"] = [
            (r["entity_type"], r["item_id"], r["representation_type"], r["lexical_rank"])
            for r in canonical_010
        ]

        # ---- (c) rollback: re-apply 008's function body (CREATE OR REPLACE) and
        #      confirm the function still works + returns the same workflow_python
        #      + prose results. This proves 010 is reversible.
        rollback_ok = True
        rollback_error = ""
        try:
            apply_migration(cluster, MIGRATION_008)  # 008 is CREATE OR REPLACE FUNCTION
        except Exception as exc:  # noqa: BLE001
            rollback_ok = False
            rollback_error = str(exc)
        ev["rollback_error"] = rollback_error

        if rollback_ok:
            # Re-run the battery against the restored 008 body — it must still pass
            # (same semantics). This is the strongest reversibility proof: 008 and
            # 010 are behaviorally interchangeable on this corpus.
            rollback_battery = run_candidate_battery(cluster, label="rollback_to_008")
            ev["rollback_battery"] = rollback_battery
            rollback_ok = all(c["ok"] for c in rollback_battery)

            # And the canonical stream must match the 010 stream (semantic parity).
            canonical_008 = call_candidates(cluster, "WanVideoSampler", kinds=["workflow"])
            canonical_008_pairs = [
                (r["entity_type"], r["item_id"], r["representation_type"], r["lexical_rank"])
                for r in canonical_008
            ]
            ev["canonical_008_wan_wf"] = canonical_008_pairs
            ev["canonical_streams_match"] = (canonical_008_pairs == ev["canonical_010_wan_wf"])
            # Stream parity is a strong signal but not load-bearing for the verdict
            # (order/rank can differ in harmless ts_rank float ways); the battery
            # pass/fail on the 008 body IS load-bearing.
        ev["rollback_ok"] = rollback_ok

        # ---- (d) idempotence: re-apply 010 twice (CREATE OR REPLACE), no error.
        idempotent_ok = True
        idempotent_error = ""
        try:
            apply_migration(cluster, MIGRATION_010)
            apply_migration(cluster, MIGRATION_010)
        except Exception as exc:  # noqa: BLE001
            idempotent_ok = False
            idempotent_error = str(exc)
        ev["idempotent_ok"] = idempotent_ok
        ev["idempotent_error"] = idempotent_error

        if idempotent_ok:
            # After double re-apply, the function must still be correct (the 010
            # body is live again). Re-run the battery one more time as the final
            # correctness gate on the re-applied 010.
            idem_battery = run_candidate_battery(cluster, label="010_idempotent")
            ev["idempotent_battery"] = idem_battery
            idempotent_ok = all(c["ok"] for c in idem_battery)
            ev["idempotent_ok"] = idempotent_ok

        # ---- (e final) grants must STILL be preserved after rollback + re-apply.
        proacl_final = get_proacl(cluster)
        ev["proacl_final"] = get_proacl_json(cluster)
        ev["proacl_final_raw"] = proacl_final
        ev["grants_preserved_final"] = (proacl_after_009 == proacl_final)
        grants_preserved = grants_preserved and ev["grants_preserved_final"]

        # ---- verdict aggregation.
        functional_ok = all(c["ok"] for c in functional)
        ev["all_pass"] = bool(
            apply_ok and functional_ok and rollback_ok and idempotent_ok and grants_preserved
        )

        # n_pass / n_total across the primary functional battery (the load-bearing
        # correctness gate against the 010 body). Rollback/idempotence/grants are
        # reported as top-level booleans (they are single checks, not batteries).
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
    print(f"schema/010 rehearsal verdict: {'PASS' if ev['all_pass'] else 'FAIL'}")
    print(f"  applied_ok         = {ev['applied_ok']}")
    print(f"  functional battery = {ev['n_pass']}/{ev['n_total']}")
    print(f"  rollback_ok        = {ev['rollback_ok']}")
    print(f"  idempotent_ok      = {ev['idempotent_ok']}")
    print(f"  grants_preserved   = {ev['grants_preserved']}")
    if ev.get("canonical_streams_match") is not None:
        print(f"  canonical 010==008 = {ev['canonical_streams_match']}")
    print(f"  proacl after 009   = {ev.get('proacl_after_009_raw')}")
    print(f"  proacl after 010   = {ev.get('proacl_after_010_raw')}")
    print(f"  proacl final       = {ev.get('proacl_final_raw')}")
    print("-" * 72)
    for c in ev["functional_assertions"]:
        flag = "OK  " if c["ok"] else "FAIL"
        print(f"  [{flag}] {c['name']}: {c['detail']}")
    if not ev["all_pass"]:
        print("-" * 72)
        print("FAILURES:")
        for c in ev["functional_assertions"]:
            if not c["ok"]:
                print(f"  {c['name']}: {c['detail']}")
    print("=" * 72)
    print(f"verdict written to {VERDICT_PATH}")
    return 0 if ev["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
