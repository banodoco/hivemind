#!/usr/bin/env python3
"""Phase-2 task 2.11 isolated-cluster rehearsal: embedding backfill runs.

Drives an independent, LOCAL-ONLY rehearsal of schema/030_embedding_backfill_runs.sql
on the throwaway PostgreSQL/pgvector cluster from rehearse_embedding_lifecycle.setup_cluster().
The cluster is unix-socket only (no network), seeded with fake/test data, and always
torn down. No embedding provider or production system is ever contacted.

The rehearsal proves:
  * migration 030 applies twice (idempotent / additive);
  * create persists a run plus one cursor row per source (messages + resources);
  * checkpoint advances a text Snowflake cursor and accumulates multiple counters;
  * a killed/resumed process boundary (fresh psql connection) sees the persisted
    cursor and intact counters, then advances cleanly with the persisted version;
  * a stale expected_version cannot mutate cursor/counters/version;
  * pause -> resume is legal;
  * completed and failed runs are terminal (checkpoint refused);
  * raw `Authorization: Bearer sk-secret-DO-NOT-STORE` is absent from DB state and
    emitted evidence, and stored errors are <= 500 characters;
  * source-table counts/hashes are byte-identical before/after;
  * ACL: anon/authenticated have no privileges; service_role retains access; mutation
    RPCs are SECURITY DEFINER with a fixed search_path.

Deterministic, sanitized JSON is written to
docs/hybrid-search/phase2-embedding-backfill-runs-rehearsal.json. Console output goes
through verify_access.redact. Exits 0 only when every check passes, else 1.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from verify_access import redact  # noqa: E402
from rehearse_embedding_lifecycle import setup_cluster  # noqa: E402

SCHEMA_DIR = REPO_ROOT / "schema"
MIGRATION = SCHEMA_DIR / "030_embedding_backfill_runs.sql"
EVIDENCE_NAME = "phase2-embedding-backfill-runs-rehearsal.json"

RPC_NAMES = (
    "hivemind_create_embedding_backfill_run",
    "hivemind_checkpoint_embedding_backfill",
    "hivemind_pause_embedding_backfill_run",
    "hivemind_resume_embedding_backfill_run",
    "hivemind_complete_embedding_backfill_run",
    "hivemind_fail_embedding_backfill_run",
)

# Raw secret-like probe string; never persisted by schema/030 and never emitted.
_SECRET_PROBE = "Authorization: Bearer sk-secret-DO-NOT-STORE"


def _q(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _arr(values) -> str:
    return "ARRAY[" + ",".join(_q(v) for v in values) + "]::text[]"


def _copy(cluster, sql: str) -> str:
    rc, out = cluster.psql("COPY (" + sql + ") TO STDOUT;")
    if rc != 0:
        raise RuntimeError(redact(f"sql rc={rc}: {sql[:80]}"))
    return out or ""


def _one(cluster, sql: str) -> str:
    out = _copy(cluster, sql).strip()
    if out == "":
        raise RuntimeError(redact(f"empty result: {sql[:80]}"))
    return out


def _json(cluster, sql: str):
    return json.loads(_one(cluster, sql))


def _rpc(cluster, call: str):
    return json.loads(_one(cluster, "SELECT (" + call + ")::text"))


def _count(cluster, table: str, where: str = "true") -> int:
    return int(_one(cluster, f"SELECT count(*)::text FROM {table} WHERE {where}"))


def _role_exists(cluster, role: str) -> bool:
    return _one(cluster, f"SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname={_q(role)})::text") == "t"


def _run(cluster, run_id: int):
    return _json(cluster, f"SELECT to_jsonb(r)::text FROM embedding_backfill_runs r WHERE run_id={run_id}")


def _cursor(cluster, run_id: int, source: str):
    return _json(
        cluster,
        f"SELECT to_jsonb(c)::text FROM embedding_backfill_cursors c "
        f"WHERE run_id={run_id} AND source={_q(source)}",
    )


def _active_contract(cluster) -> int:
    return int(_one(cluster, "SELECT hivemind_active_contract_id()::text"))


def _create_run(cluster, sources, mode: str = "rebuild") -> int:
    cid = _active_contract(cluster)
    return int(_one(cluster, f"SELECT hivemind_create_embedding_backfill_run({cid}, {_q(mode)}, {_arr(sources)})::text"))


def _checkpoint(cluster, run_id, source, expected_version, cursor, **kw):
    order = ("high_water", "processed", "skipped", "quarantined",
             "unavailable", "failed", "eligible", "last_error")
    vals = []
    for key in order:
        v = kw.get(key)
        if v is None:
            vals.append("NULL")
        elif isinstance(v, str):
            vals.append(_q(v))
        else:
            vals.append(str(v))
    call = (
        "SELECT hivemind_checkpoint_embedding_backfill("
        + ",".join([str(run_id), _q(source), str(expected_version), _q(cursor)] + vals)
        + ")"
    )
    return _rpc(cluster, call)


def _hash_table(cluster, tbl: str):
    rc, h = cluster.psql(
        f"COPY (SELECT coalesce(md5(string_agg(md5(t::text), ',' ORDER BY t::text)), md5('')) "
        f"FROM {tbl} t) TO STDOUT;"
    )
    rc2, c = cluster.psql(f"COPY (SELECT count(*)::text FROM {tbl}) TO STDOUT;")
    if rc != 0 or rc2 != 0:
        raise RuntimeError(redact(f"hash failed for {tbl}"))
    return (int(c.strip()), h.strip())


def _snapshot_sources(cluster) -> dict:
    out = _copy(
        cluster,
        "SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind='r' AND n.nspname='public' ORDER BY relname",
    )
    snap = {}
    for tbl in [t.strip() for t in out.splitlines() if t.strip()]:
        snap[tbl] = _hash_table(cluster, tbl)
    return snap


def rehearse(out_dir: Path) -> dict:
    """Run the full task-2.11 rehearsal on a throwaway cluster. Returns the evidence dict."""
    cluster, info = setup_cluster()
    ev = {
        "task": "2.11-embedding-backfill-runs-rehearsal",
        "date": str(date.today()),
        "method": (
            "isolated local PostgreSQL + pgvector cluster (unix socket, no network); "
 "migration 030 applied twice; killed/resumed process boundary modeled by fresh "
            "psql invocations; fake/test data only; no provider or production access"
        ),
        "cluster_isolated": True,
        "active_contract_id": info.get("active_contract_id"),
    }
    checks: dict = {}
    before: dict = {}
    run_id = 0
    try:
        # Snapshot source corpus BEFORE applying 030 to prove it is read-only w.r.t.
        # source data.
        before = _snapshot_sources(cluster)

        # Apply migration 030 twice (idempotent / additive).
        cluster.psql_file(MIGRATION)
        cluster.psql_file(MIGRATION)
        checks["migration_applies_twice"] = True

        # --- create ------------------------------------------------------
        run_id = _create_run(cluster, sources=("messages", "resources"), mode="rebuild")
        checks["create_positive_id"] = run_id > 0
        checks["create_one_run_row"] = _count(cluster, "embedding_backfill_runs", f"run_id={run_id}") == 1
        checks["create_two_cursor_rows"] = _count(cluster, "embedding_backfill_cursors", f"run_id={run_id}") == 2
        run = _run(cluster, run_id)
        checks["create_status_running"] = run["status"] == "running"
        checks["create_version_one"] = run["version"] == 1
        checks["create_messages_zeroed"] = _cursor(cluster, run_id, "messages")["processed_count"] == 0
        checks["create_messages_cursor_null"] = _cursor(cluster, run_id, "messages")["cursor"] is None
        checks["create_resources_zeroed"] = _cursor(cluster, run_id, "resources")["processed_count"] == 0

        # --- checkpoint: text Snowflake cursor + multiple counters -------
        snowflake = "9223372036854775000"
        res = _checkpoint(
            cluster, run_id, "messages", 1, snowflake,
            processed=5, skipped=2, quarantined=1, unavailable=1, failed=1, eligible=10,
        )
        checks["checkpoint_ok"] = bool(res["ok"])
        checks["checkpoint_version_bumped"] = res["version"] == 2
        cur = _cursor(cluster, run_id, "messages")
        checks["checkpoint_snowflake_cursor"] = cur["cursor"] == snowflake
        checks["checkpoint_processed_5"] = cur["processed_count"] == 5
        checks["checkpoint_skipped_2"] = cur["skipped_count"] == 2
        checks["checkpoint_quarantined_1"] = cur["quarantined_count"] == 1
        checks["checkpoint_unavailable_1"] = cur["unavailable_count"] == 1
        checks["checkpoint_failed_1"] = cur["failed_count"] == 1
        checks["checkpoint_eligible_10"] = cur["eligible_count"] == 10
        run = _run(cluster, run_id)
        checks["checkpoint_run_total_processed_5"] = run["total_processed"] == 5
        checks["checkpoint_run_total_skipped_2"] = run["total_skipped"] == 2
        checks["checkpoint_run_total_failed_1"] = run["total_failed"] == 1

        # --- killed/resumed process boundary ----------------------------
        # Verify the persisted cursor and intact counters via a FRESH psql
        # invocation/connection (modeling a process restart), then advance again
        # using the persisted version.
        persisted_version = _run(cluster, run_id)["version"]
        persisted_cursor = _cursor(cluster, run_id, "messages")["cursor"]
        persisted_processed = _cursor(cluster, run_id, "messages")["processed_count"]
        checks["resume_cursor_persisted"] = persisted_cursor == snowflake
        checks["resume_counters_intact"] = persisted_processed == 5
        res2 = _checkpoint(cluster, run_id, "messages", persisted_version, "cur-2", processed=3, eligible=2)
        checks["resume_advance_ok"] = bool(res2["ok"])
        cur2 = _cursor(cluster, run_id, "messages")
        checks["resume_cursor_advanced"] = cur2["cursor"] == "cur-2"
        checks["resume_processed_accumulated"] = cur2["processed_count"] == 8
        checks["resume_eligible_accumulated"] = cur2["eligible_count"] == 12
        checks["resume_run_total_8"] = _run(cluster, run_id)["total_processed"] == 8

        # --- stale expected_version cannot mutate -----------------------
        stale = _checkpoint(cluster, run_id, "messages", 1, "cur-stale", processed=99)
        checks["stale_version_rejected"] = not bool(stale["ok"])
        cur3 = _cursor(cluster, run_id, "messages")
        checks["stale_cursor_unchanged"] = cur3["cursor"] == "cur-2"
        checks["stale_processed_unchanged"] = cur3["processed_count"] == 8
        checks["stale_version_unchanged"] = _run(cluster, run_id)["version"] == res2["version"]

        # --- pause / resume ---------------------------------------------
        paused = _rpc(cluster, f"SELECT hivemind_pause_embedding_backfill_run({run_id}, {res2['version']})")
        checks["pause_ok"] = bool(paused["ok"])
        checks["pause_status_paused"] = _run(cluster, run_id)["status"] == "paused"
        resumed = _rpc(cluster, f"SELECT hivemind_resume_embedding_backfill_run({run_id}, {paused['version']})")
        checks["resume_ok"] = bool(resumed["ok"])
        checks["resume_status_running"] = _run(cluster, run_id)["status"] == "running"

        # --- completed run is terminal ----------------------------------
        done = _rpc(cluster, f"SELECT hivemind_complete_embedding_backfill_run({run_id}, {resumed['version']})")
        checks["complete_ok"] = bool(done["ok"])
        checks["complete_status_completed"] = _run(cluster, run_id)["status"] == "completed"
        after = _checkpoint(cluster, run_id, "messages", done["version"], "after", processed=1)
        checks["completed_blocks_checkpoint"] = not bool(after["ok"])
        checks["completed_counter_unchanged"] = _cursor(cluster, run_id, "messages")["processed_count"] == 8

        # --- failed run is terminal -------------------------------------
        f_run = _create_run(cluster, sources=("messages",), mode="incremental")
        err = _rpc(cluster, f"SELECT hivemind_fail_embedding_backfill_run({f_run}, 1, {_q('boom')})")
        checks["fail_ok"] = bool(err["ok"])
        checks["fail_status_failed"] = _run(cluster, f_run)["status"] == "failed"
        checks["fail_error_stored"] = _run(cluster, f_run)["last_error"] == "boom"
        after_f = _checkpoint(cluster, f_run, "messages", err["version"], "after", processed=1)
        checks["failed_blocks_checkpoint"] = not bool(after_f["ok"])

        # --- raw secret is never stored ---------------------------------
        s_run = _create_run(cluster, sources=("messages",), mode="catchup")
        _checkpoint(cluster, s_run, "messages", 1, "cur-1", failed=1, last_error=_SECRET_PROBE)
        stored = _cursor(cluster, s_run, "messages")["last_error"]
        checks["secret_not_in_db"] = (
            stored is not None and "sk-secret" not in stored and stored != _SECRET_PROBE
        )
        checks["stored_error_bounded_500"] = stored is not None and len(stored) <= 500

        # --- secret absent from emitted evidence ------------------------
        # Serialize every DB state string we touched; the probe must not appear.
        state_blob = json.dumps(_run(cluster, s_run)) + json.dumps(_cursor(cluster, s_run, "messages"))
        checks["secret_absent_from_evidence"] = _SECRET_PROBE not in state_blob
        checks["bearer_absent_from_evidence"] = "Bearer sk-secret" not in state_blob

        # --- source tables unchanged ------------------------------------
        # Compare only tables/keys captured in `before` (pre-migration 030) against
        # their current hash; a fresh post-migration snapshot also contains the two
        # state tables added by 030, which must not fail this check.
        source_unchanged = True
        for tbl, (cnt, hsh) in before.items():
            cur_cnt, cur_hsh = _hash_table(cluster, tbl)
            if (cur_cnt, cur_hsh) != (cnt, hsh):
                source_unchanged = False
        checks["source_tables_unchanged"] = source_unchanged

        # --- ACL / SECURITY DEFINER / fixed search_path -----------------
        for role in ("anon", "authenticated"):
            if not _role_exists(cluster, role):
                continue
            for tbl in ("embedding_backfill_runs", "embedding_backfill_cursors"):
                for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    got = _one(
                        cluster,
                        f"SELECT coalesce(has_table_privilege('{role}', {_q(tbl)}, {_q(priv)}), false)::text",
                    )
                    checks[f"acl_{role}_no_{priv}_{tbl}"] = got == "f"
        if _role_exists(cluster, "service_role"):
            got = _one(
                cluster,
                "SELECT coalesce(has_table_privilege('service_role', "
                "'embedding_backfill_runs', 'SELECT'), false)::text",
            )
            checks["acl_service_role_select_runs"] = got == "t"
        leaked = _count(
            cluster,
            "information_schema.routine_privileges",
            "routine_name IN (" + ",".join(_q(n) for n in RPC_NAMES) + ") "
            "AND grantee IN ('anon','authenticated')",
        )
        checks["acl_no_routine_grant_anon_auth"] = leaked == 0

        # Mutation RPCs are SECURITY DEFINER.
        sec_out = _copy(
            cluster,
            "SELECT proname, prosecdef FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='public' AND proname LIKE 'hivemind_%embedding_backfill%' ORDER BY proname",
        )
        secdef_ok = True
        seen_sec = 0
        for line in sec_out.splitlines():
            parts = [p.strip() for p in line.split("\t")]
            if len(parts) == 2 and parts[0] in RPC_NAMES:
                seen_sec += 1
                if parts[1] not in ("t", "true"):
                    secdef_ok = False
        checks["mutation_rpcs_security_definer"] = secdef_ok and seen_sec == len(RPC_NAMES)

        # Mutation RPCs pin search_path = public, pg_temp.
        cfg_out = _copy(
            cluster,
            "SELECT proname, coalesce(proconfig::text, '') FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='public' AND proname LIKE 'hivemind_%embedding_backfill%' ORDER BY proname",
        )
        sp_ok = True
        seen_sp = 0
        for line in cfg_out.splitlines():
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            name = parts[0].strip()
            config = parts[1].strip()
            if name in RPC_NAMES:
                seen_sp += 1
                if "search_path" not in config.lower():
                    sp_ok = False
        checks["mutation_rpcs_fixed_search_path"] = sp_ok and seen_sp == len(RPC_NAMES)
    finally:
        cluster.tear_down()

    ev["checks"] = checks
    ev["metrics"] = {
        "default_cursor_sources_per_run": 2,
        "source_table_count": len(before),
        "mutation_rpc_count": len(RPC_NAMES),
        "check_count": len(checks),
    }
    all_pass = all(checks.values())
    ev["verdict"] = {
        "all_pass": all_pass,
        "passed": sum(1 for v in checks.values() if v),
        "failed": sum(1 for v in checks.values() if not v),
        "failed_checks": sorted(k for k, v in checks.items() if not v),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / EVIDENCE_NAME).write_text(json.dumps(ev, indent=2))
    return ev


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Phase-2 task 2.11 embedding backfill runs rehearsal.")
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "hybrid-search")
    args = ap.parse_args(argv)
    print(redact("Phase-2 task 2.11 rehearsal: isolated PG cluster (pgvector), migration 030."))
    ev = rehearse(args.out)
    for name, ok in ev["checks"].items():
        print(redact(f"{'PASS' if ok else 'FAIL'}  {name}"))
    print(redact(f"\nVERDICT all_pass={ev['verdict']['all_pass']} "
                 f"passed={ev['verdict']['passed']} failed={ev['verdict']['failed']}"))
    print(redact(f"Evidence: {args.out / EVIDENCE_NAME}"))
    return 0 if ev["verdict"]["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
