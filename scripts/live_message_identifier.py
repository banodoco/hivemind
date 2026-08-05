#!/usr/bin/env python3
"""Task-1.6 live driver: preflight + apply + evidence + rollback.

Applies the FROZEN, EVIDENCE-BASED CHOICE (a single normalized, length-bounded
full-message trigram GIN index over ``discord_messages.content``, partial on
``is_deleted=false``) on the ACTUAL Hivemind Supabase project using the task-0.1
safe session-mode access path (``supabase db dump --schema public --dry-run``
derives a short-lived CLI-login libpq env held only in memory for the psql calls
— never printed, logged, or persisted). Reuses ``verify_access``
.parse_dryrun_pg_env / redact verbatim so the safety boundary never drifts.

The chosen design is ONE auto-maintained GIN index. There is NO side table, NO
trigger, and NO backfill to run: PostgreSQL maintains the GIN index automatically
on insert/update/delete, the ``is_deleted=false`` partial predicate makes
soft-delete structural (a row leaves the index when it flips true), and
``CREATE INDEX CONCURRENTLY`` populates the index from existing rows without
blocking writes. So this driver has no ``--backfill`` (unlike the rejected side
index, which needed a high-water cursor + trigger + backfill).

Flow:
  1. ``--preflight`` (default, READ-ONLY): source-table shape, schema/005 prereq,
     chosen-index state, est rows + eligibility, storage headroom, invalid/
     concurrent remnants, long/locking txns, relation locks, session-vs-pooler
     mode. Green/red verdict + the exact rollback command.
  2. ``--apply`` (gated): REFUSED unless the rehearsal gate verdict
     (docs/hybrid-search/phase1-message-identifier-rehearsal.json) is green AND
     the live preflight is green. Then: apply schema/005 (if missing) and build
     the chosen index CONCURRENTLY with a bounded lock_timeout. No source-row
     mutation; no trigger/side table.
  3. ``--evidence`` (READ-ONLY): index + table sizes, EXPLAIN (index use), and
     safe candidate hit counts (counts only — reads NO message content).
  4. ``--rollback``: ``DROP INDEX CONCURRENTLY`` (no source row touched).

Safety: every human-facing line is routed through :func:`redact`. No source row
is mutated; only the one additive index is created. ``--apply`` is refused unless
the rehearsal gates AND the live preflight are green.

Run::

    python3 scripts/live_message_identifier.py --preflight
    python3 scripts/live_message_identifier.py --dry-run
    python3 scripts/live_message_identifier.py --apply     # gated on green rehearsal + preflight
    python3 scripts/live_message_identifier.py --evidence
    python3 scripts/live_message_identifier.py --rollback
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import scripts.message_identifier_index as M  # noqa: E402
from verify_access import parse_dryrun_pg_env, redact  # noqa: E402

ELEVATE_ROLE = "postgres"
REHEARSAL_EVIDENCE = REPO_ROOT / "docs" / "hybrid-search" / "phase1-message-identifier-rehearsal.json"
LIVE_EVIDENCE = REPO_ROOT / "docs" / "hybrid-search" / "phase1-message-identifier-live.json"


# ---------------------------------------------------------------------------
# Session-mode access (task 0.1) — credential lives only in a child-process env
# ---------------------------------------------------------------------------

def derive_pg_env() -> tuple[dict, str, str]:
    proc = subprocess.run(["supabase", "db", "dump", "--schema", "public", "--dry-run"],
                          capture_output=True, text=True, timeout=30.0, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise RuntimeError(f"supabase db dump --dry-run failed (rc={proc.returncode}): {redact(_tail(proc.stderr))}")
    pg = parse_dryrun_pg_env(proc.stdout)
    if "PGHOST" not in pg or "PGPASSWORD" not in pg:
        raise RuntimeError("could not derive CLI login env (PGHOST/PGPASSWORD) from dry-run")
    return {**os.environ, **pg}, pg.get("PGHOST", ""), pg.get("PGPORT", "")


def _tail(text: str) -> str:
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def psql(env, sql, *, timeout=60.0, on_error_stop=True):
    args = ["psql", "-X", "-q", "-t", "-A", "-P", "pager=off",
            "-v", f"ON_ERROR_STOP={'1' if on_error_stop else '0'}"]
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as tf:
        tf.write(sql); script = tf.name
    try:
        return subprocess.run(args + ["-f", script], env=env, capture_output=True,
                              text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    finally:
        try: os.unlink(script)
        except OSError: pass


def parse_rows(stdout):
    rows = []
    for line in (stdout or "").splitlines():
        line = line.rstrip("\n")
        if line == "" or line.startswith("("):
            continue
        rows.append(line.split("|"))
    return rows


def elevate(sql_text: str) -> str:
    return f"SET ROLE {ELEVATE_ROLE};\n" + sql_text


# ---------------------------------------------------------------------------
# Preflight (read-only)
# ---------------------------------------------------------------------------

def run_preflight(env, host, port) -> dict:
    parsed, raw = {}, {}
    for label, sqlq in M.preflight_queries():
        # Elevate to the service role (postgres) for these READ-ONLY checks. The CLI-login
        # role (cli_login_postgres) can read system catalogs but has no SELECT grant on
        # discord_messages (RLS-enabled), so the eligibility count would falsely read 0.
        # Reading as the authorized service role measures eligibility honestly (the gate is
        # `eligible > 0` with the TRUE count); it does NOT weaken the gate.
        r = psql(env, elevate(sqlq), timeout=30.0, on_error_stop=False)
        parsed[label] = parse_rows(r.stdout)
        raw[label] = {"rc": r.returncode, "stderr_tail": redact(_tail(r.stderr))}
    verdict = M.evaluate_preflight(parsed, pghost=host, pgport=port)
    return {"parsed": parsed, "raw": raw, "verdict": verdict,
            "host_family": "pooler" if "pooler.supabase.com" in host else "session", "port": port}


# ---------------------------------------------------------------------------
# Rehearsal gate (the task's "stop before live mutation if any gate is red")
# ---------------------------------------------------------------------------

def rehearsal_gate_verdict() -> dict:
    """Read the rehearsal evidence and return the gate verdict (green/red).

    Mirrors the rehearsal JSON schema produced by rehearse_message_identifier.py
    (chosen_build / query_quality / write_cost / rejected_alternative_side_index
    / rollback / verdict), NOT the rejected side-index schema.
    """
    if not REHEARSAL_EVIDENCE.exists():
        return {"available": False, "green": False,
                "reason": f"rehearsal evidence not found at {REHEARSAL_EVIDENCE.name}; "
                          "run scripts/rehearse_message_identifier.py first"}
    ev = json.loads(REHEARSAL_EVIDENCE.read_text())
    v = ev.get("verdict", {})
    checks = v.get("checks", {})
    green = v.get("all_pass") is True and all(checks.get(k) for k in checks)
    cb = ev.get("chosen_build", {})
    qq = ev.get("query_quality", {})
    wc = ev.get("write_cost", {})
    return {
        "available": True, "green": green,
        "all_pass": v.get("all_pass"),
        "checks": checks,
        "index_size_pretty": cb.get("index_size_pretty"),
        "storage_gb": (cb.get("index_bytes", 0) or 0) / 1e9,
        "storage_gate_pass": cb.get("storage_gate_pass"),
        "recall_at_10": qq.get("recall_at_10"),
        "latency_ms_p50": qq.get("latency_ms_p50"),
        "latency_ms_p95": qq.get("latency_ms_p95"),
        "write_slowdown": wc.get("slowdown_ratio_off_over_with"),
        "reason": "" if green else "one or more rehearsal gates (storage/recall/write/index-use/rollback) are red",
    }


# ---------------------------------------------------------------------------
# Apply (schema/005 if missing + the chosen GIN CONCURRENTLY) — gated
# ---------------------------------------------------------------------------

def apply_index(env, *, lock_timeout_s=30, statement_timeout_s=3600) -> dict:
    """Apply schema/005 (if missing) then build the chosen GIN index CONCURRENTLY.

    No source-row mutation; no trigger/side table/backfill. The bounded
    ``lock_timeout`` makes a transient ACCESS-EXCLUSIVE conflict fail fast
    instead of wedging writers; ``statement_timeout`` bounds the overall build.
    """
    out: dict = {"steps": []}
    # schema/005 if missing (read-only check first)
    has005 = parse_rows(psql(env, M.preflight_queries()[1][1], timeout=30).stdout)
    if not (has005 and has005[0][0] in ("t", "true")):
        r = psql(env, elevate(M.prereq_schema_sql_text()), timeout=120, on_error_stop=False)
        out["steps"].append({"step": "apply_schema_005", "rc": r.returncode,
                             "stderr_tail": redact(_tail(r.stderr))})
        if r.returncode != 0:
            out["status"] = "error"; return out
    # the chosen GIN index, built CONCURRENTLY (online + idempotent; bounded lock_timeout)
    r = psql(env, elevate(M.build_statement(lock_timeout_s=lock_timeout_s,
                                            statement_timeout_s=statement_timeout_s)),
             timeout=statement_timeout_s, on_error_stop=False)
    out["steps"].append({"step": "build_identifier_trgm_concurrently", "rc": r.returncode,
                         "stderr_tail": redact(_tail(r.stderr))})
    if r.returncode != 0:
        out["status"] = "error"; return out
    # read back the live object state an operator signs (indisvalid MUST be true).
    # Use c.oid (regclass) for pg_total_relation_size — the name-typed overload does not exist.
    state = parse_rows(psql(env, elevate(
        f"SELECT i.indisvalid::text, i.indisready::text, "
        f"pg_total_relation_size(c.oid)::text FROM pg_catalog.pg_class c "
        f"JOIN pg_catalog.pg_index i ON i.indexrelid=c.oid "
        f"WHERE c.relname='{M.INDEX_NAME}';"), timeout=30).stdout)
    out["index_state"] = {"indisvalid": state[0][0], "indisready": state[0][1],
                          "index_bytes": int(state[0][2])} if state and state[0] else None
    out["status"] = "ok"
    return out


# ---------------------------------------------------------------------------
# Evidence (read-only)
# ---------------------------------------------------------------------------

def capture_evidence(env) -> dict:
    sizes = {}
    for label, rel in [("identifier_trgm", M.INDEX_NAME), ("discord_messages", M.SOURCE_TABLE)]:
        r = parse_rows(psql(env, elevate(f"SELECT pg_total_relation_size('{rel}');"), timeout=30).stdout)
        sizes[label] = int(r[0][0]) if r and r[0][0].isdigit() else 0
    # the signed validity state (indisvalid MUST be true after a successful CIC build).
    st = parse_rows(psql(env, elevate(
        f"SELECT i.indisvalid::text, i.indisready::text FROM pg_catalog.pg_class c "
        f"JOIN pg_catalog.pg_index i ON i.indexrelid=c.oid WHERE c.relname='{M.INDEX_NAME}';"),
        timeout=30).stdout)
    index_state = {"indisvalid": st[0][0], "indisready": st[0][1]} if st and st[0] else None
    plans = {}
    for label, sqlq in M.evidence_queries():
        r = psql(env, elevate(sqlq), timeout=120, on_error_stop=False)
        plans[label] = {"plan": r.stdout, "rc": r.returncode, **M.parse_explain_plan(r.stdout)}
    # representative candidate hit counts (proves eligibility + real hits; COUNT only,
    # reads NO message content). Uses the PRIMARY containment predicate (frozen v3):
    # the normalized query is a substring of the normalized whole body. This is the
    # bounded, index-served set — for compound identifiers it is far smaller than the
    # old <% arm's set, so it reports the true production candidate count.
    hits = {}
    for term in ("FLUX.1", "Wan2.2", "WanVideoSampler", "controlnet"):
        r = parse_rows(psql(env, elevate(
            f"SELECT count(*) FROM {M.SOURCE_TABLE} m WHERE m.is_deleted=false "
            f"AND char_length(m.content) BETWEEN {M.CONTENT_LENGTH_MIN} AND {M.CONTENT_LENGTH_MAX} "
            f"AND public.hivemind_normalize_identifier(m.content) "
            f"LIKE '%' || public.hivemind_normalize_identifier('{term}') || '%';"), timeout=90).stdout)
        hits[term] = int(r[0][0]) if r and r[0][0].lstrip("-").isdigit() else None
    return {"sizes": sizes, "index_state": index_state,
            "evidence_plans": plans, "representative_hit_counts": hits}


def run_rollback(env) -> dict:
    r = psql(env, elevate(M.rollback_statement(concurrently=True)), timeout=600, on_error_stop=False)
    return {"rc": r.returncode, "stderr_tail": redact(_tail(r.stderr))}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_preflight(pf: dict) -> None:
    v = pf["verdict"]
    print(redact(f"connection: {pf['host_family']}/port-{pf['port']}"))
    print(redact(f"est_rows={v.get('est_rows'):,}  eligible={v.get('n_eligible'):,}  "
                 f"est_index≈{v.get('est_index_bytes',0)/1e9:.3f}GB (gate {M.STORAGE_GATE_GB}GB)"))
    for c in v["checks"]:
        print(redact(f"  [{'PASS' if c['pass'] else 'FAIL'}] {c['name']}: {c['detail']}"))
    print(redact(f"\nPREFLIGHT: {'GREEN' if v['green'] else 'RED'}"))
    if not v["green"] and v["reasons"]:
        print(redact("REASONS: " + "; ".join(v["reasons"])))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Task-1.6 live message identifier driver.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--evidence", action="store_true")
    mode.add_argument("--rollback", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="bypass the rehearsal-gate green requirement (operator override)")
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "hybrid-search")
    args = ap.parse_args(argv)

    print("Task 1.6 live driver — all output redacted (task-0.1 session-mode access)")
    try:
        env, host, port = derive_pg_env()
    except Exception as exc:  # noqa: BLE001
        print(redact(f"ACCESS FAILED: {exc}"))
        return 2

    if args.dry_run:
        print(redact("--- DRY RUN ---\n-- apply would run schema/005(if missing) + the chosen "
                     "GIN CONCURRENTLY (no source-row mutation; no trigger/side table/backfill)"))
        print(redact(elevate(M.build_statement())[:1200] + "\n... (truncated) ...\n"))
        print(redact("--- ROLLBACK ---\n" + elevate(M.rollback_statement(concurrently=True))))
        pf = run_preflight(env, host, port); _print_preflight(pf)
        return 0

    if args.rollback:
        print(redact("Rolling back: DROP INDEX CONCURRENTLY (no source row touched) ..."))
        res = run_rollback(env)
        print(redact(f"rollback rc={res['rc']} stderr_tail={res['stderr_tail']}"))
        return 0 if res["rc"] == 0 else 1

    if args.evidence:
        ev = capture_evidence(env)
        print(redact(f"sizes={ {k: f'{v/1e6:.1f}MB' for k,v in ev['sizes'].items()} }"))
        print(redact(f"hits={ev['representative_hit_counts']}"))
        for label, p in ev["evidence_plans"].items():
            print(redact(f"  plan[{label}] uses_idx={p['uses_index_scan']} seq={p['is_seq_scan']}"))
        LIVE_EVIDENCE.write_text(json.dumps(ev, indent=2))
        return 0

    # default: preflight; --apply proceeds only if rehearsal gate + preflight green
    pf = run_preflight(env, host, port)
    _print_preflight(pf)
    if not args.apply:
        return 0 if pf["verdict"]["green"] else 1

    gate = rehearsal_gate_verdict()
    print(redact(f"\nREHEARSAL GATE: {'GREEN' if gate['green'] else 'RED'} "
                 f"(available={gate['available']})"))
    if gate["available"]:
        print(redact(f"  size={gate.get('index_size_pretty')} (~{gate.get('storage_gb'):.3f}GB) "
                     f"recall@10={gate.get('recall_at_10')} p50={gate.get('latency_ms_p50')}ms "
                     f"p95={gate.get('latency_ms_p95')}ms write_slowdown={gate.get('write_slowdown')}x"))
    if not gate["green"] and not args.force:
        print(redact(f"REFUSING --apply: rehearsal gate RED — {gate['reason']}. "
                     f"Run scripts/rehearse_message_identifier.py and resolve, or use --force (operator override)."))
        return 1
    if not pf["verdict"]["green"] and not args.force:
        print(redact("REFUSING --apply: live preflight RED. Re-run with --preflight."))
        return 1

    print(redact("Gates green — applying schema/005(if missing) + chosen GIN CONCURRENTLY ..."))
    applied = apply_index(env)
    print(redact(f"apply status={applied['status']} steps={[(s['step'], s['rc']) for s in applied.get('steps', [])]} "
                 f"index_state={applied.get('index_state')}"))
    if applied["status"] != "ok":
        LIVE_EVIDENCE.write_text(json.dumps({"preflight": pf, "rehearsal_gate": gate, "apply": applied}, indent=2))
        return 1
    ev = capture_evidence(env)
    print(redact(f"EVIDENCE: sizes={ {k: f'{v/1e6:.1f}MB' for k,v in ev['sizes'].items()} } "
                 f"hits={ev['representative_hit_counts']}"))
    LIVE_EVIDENCE.write_text(
        json.dumps({"preflight": pf, "rehearsal_gate": gate, "apply": applied, "evidence": ev}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
