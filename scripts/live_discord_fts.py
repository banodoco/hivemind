#!/usr/bin/env python3
"""Task-1.3 live driver: preflight + online build + evidence + rollback on Hivemind.

Applies the canonical ``simple`` Discord-message FTS index on the **actual**
Hivemind Supabase project using the task-0.1 safe session-mode access path
(``supabase db dump --dry-run`` derives a short-lived CLI-login libpq env that is
held only in memory for the duration of the psql calls — never printed, logged,
or persisted). This reuses ``verify_access.parse_dryrun_pg_env`` / ``redact``
verbatim so the safety boundary never drifts.

Flow:
  1. ``--preflight`` (default, read-only): exact table/index identity, row
     estimate, free storage/headroom, invalid/concurrent remnants, in-progress
     builds, long/locking transactions, lock/statement timeout settings,
     session-vs-pooler mode. Emits a green/red verdict + the rollback command.
  2. ``--apply`` (only if preflight green): ``CREATE INDEX CONCURRENTLY`` outside
     any transaction, bounded ``lock_timeout`` + generous ``statement_timeout``,
    progress monitoring via ``pg_stat_progress_create_index``, no source-row
     mutation. Then captures index validity, size, and saved ``EXPLAIN`` evidence
     that representative ``simple`` queries (``is_deleted = false``,
     snowflakes-as-text) use it.
  3. ``--rollback``: ``DROP INDEX CONCURRENTLY IF EXISTS``.
  4. ``--dry-run``: print the exact SQL that would run + preflight; no mutation.

Safety: every human-facing line is routed through :func:`redact`. The build never
mutates source rows; it only adds an index. ``--apply`` is refused unless the
preflight verdict is green.

Run::

    python3 scripts/live_discord_fts.py --preflight
    python3 scripts/live_discord_fts.py --apply        # only if preflight green
    python3 scripts/live_discord_fts.py --rollback
    python3 scripts/live_discord_fts.py --dry-run
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

import scripts.discord_message_fts as M  # noqa: E402
from verify_access import parse_dryrun_pg_env, redact  # noqa: E402

#: The CLI login role (``cli_login_postgres``) is a MEMBER of ``postgres`` (the
#: owner of ``discord_messages``) but the CREATE INDEX ownership check is not
#: satisfied by inherited privileges, so the driver elevates with SET ROLE for
#: the DDL only (build/drop). Confirmed safe+reversible locally. The canonical
#: migration (schema/004) stays elevation-free so it is runnable as the owner
#: directly (e.g. from the SQL editor).
ELEVATE_ROLE = "postgres"


# ---------------------------------------------------------------------------
# Session-mode access (task 0.1) — credential lives only in a child-process env
# ---------------------------------------------------------------------------

def derive_pg_env() -> tuple[dict, str, str]:
    """Derive the short-lived CLI-login libpq env from `supabase db dump --dry-run`.

    Returns (full_env_for_subprocess, pghost, pgport). Never prints the env.
    Raises if the dry-run fails or no PGHOST/PGPASSWORD can be derived.
    """
    proc = subprocess.run(
        ["supabase", "db", "dump", "--schema", "public", "--dry-run"],
        capture_output=True, text=True, timeout=30.0, stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        last = _tail(proc.stderr or "")
        raise RuntimeError(f"supabase db dump --dry-run failed (rc={proc.returncode}): "
                           f"{redact(last)}")
    pg = parse_dryrun_pg_env(proc.stdout)
    if "PGHOST" not in pg or "PGPASSWORD" not in pg:
        raise RuntimeError("could not derive CLI login env (PGHOST/PGPASSWORD) from dry-run")
    env = {**os.environ, **pg}
    return env, pg.get("PGHOST", ""), pg.get("PGPORT", "")


def _tail(text: str) -> str:
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def psql(env: dict, sql: str, *, timeout: float = 60.0,
         on_error_stop: bool = True) -> subprocess.CompletedProcess:
    """Run one SQL string (autocommit per statement) and return the process result."""
    args = ["psql", "-X", "-q", "-t", "-A", "-P", "pager=off",
            "-v", f"ON_ERROR_STOP={'1' if on_error_stop else '0'}"]
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as tf:
        tf.write(sql)
        script = tf.name
    try:
        return subprocess.run(args + ["-f", script], env=env, capture_output=True,
                              text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    finally:
        try:
            os.unlink(script)
        except OSError:
            pass


def parse_rows(stdout: str) -> list[list[str]]:
    """Parse psql -tA output into a list of pipe-split row lists."""
    rows = []
    for line in (stdout or "").splitlines():
        line = line.rstrip("\n")
        if line == "" or line.startswith("("):
            continue
        rows.append(line.split("|"))
    return rows


# ---------------------------------------------------------------------------
# Preflight (read-only)
# ---------------------------------------------------------------------------

def run_preflight(env: dict, host: str, port: str) -> dict:
    parsed: dict = {}
    raw: dict = {}
    for label, sql in M.preflight_queries():
        r = psql(env, sql, timeout=30.0, on_error_stop=False)
        rows = parse_rows(r.stdout)
        parsed[label] = rows
        raw[label] = {"rc": r.returncode, "stderr_tail": redact(_tail(r.stderr))}
    verdict = M.evaluate_preflight(parsed, pghost=host, pgport=port)
    return {"parsed": parsed, "raw": raw, "verdict": verdict, "host_family": (
        "pooler" if "pooler.supabase.com" in host else "session"), "port": port}


# ---------------------------------------------------------------------------
# Build (online) with progress monitoring
# ---------------------------------------------------------------------------

def apply_build(env: dict, *, lock_timeout_s: int = 30, statement_timeout_s: int = 1800,
                max_wall_minutes: int = 30, elevate: bool = True) -> dict:
    """Run CREATE INDEX CONCURRENTLY in the background, monitor, wait, validate."""
    build_sql = M.build_statement(lock_timeout_s=lock_timeout_s,
                                  statement_timeout_s=statement_timeout_s)
    if elevate:
        # SET ROLE must precede the lock_timeout SET; all in ONE psql session.
        build_sql = f"SET ROLE {ELEVATE_ROLE};\n" + build_sql
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as tf:
        tf.write(build_sql)
        script = tf.name

    samples: list[dict] = []
    t0 = time.time()
    deadline = t0 + max_wall_minutes * 60
    proc = subprocess.Popen(
        ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", script],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        stdin=subprocess.DEVNULL,
    )
    try:
        while True:
            if proc.poll() is not None:
                break
            if time.time() > deadline:
                proc.terminate()
                try:
                    proc.communicate(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return {"status": "wall_clock_exceeded", "max_wall_minutes": max_wall_minutes,
                        "samples": samples, "elapsed_s": round(time.time() - t0, 2)}
            # poll progress
            pr = psql(env,
                      "SELECT phase, blocks_done, blocks_total, tuples_done, tuples_total "
                      "FROM pg_stat_progress_create_index "
                      "WHERE relid = 'public.discord_messages'::regclass;",
                      timeout=15.0, on_error_stop=False)
            rows = parse_rows(pr.stdout)
            if rows:
                phase, bd, bt, td, tt = (rows[0] + ["", "", "", "", ""])[:5]
                samples.append({"t": round(time.time() - t0, 1), "phase": phase,
                                "blocks_done": bd, "blocks_total": bt,
                                "tuples_done": td, "tuples_total": tt})
            time.sleep(5.0)
        out, err = proc.communicate(timeout=60)
        elapsed = round(time.time() - t0, 2)
        return {
            "status": "ok" if proc.returncode == 0 else "error",
            "returncode": proc.returncode,
            "elapsed_s": elapsed,
            "stderr_tail": redact(_tail(err)),
            "samples": samples,
            "build_sql": build_sql,
        }
    finally:
        try:
            os.unlink(script)
        except OSError:
            pass
        if proc.poll() is None:
            proc.kill()


# ---------------------------------------------------------------------------
# Evidence (read-only)
# ---------------------------------------------------------------------------

def capture_evidence(env: dict, *, elevate: bool = True) -> dict:
    """Index validity + size + saved EXPLAIN plans proving the index is used.

    Runs as ``postgres`` (SET ROLE) because ``discord_messages`` is an
    externally-owned table the CLI login role cannot SELECT — and because the
    1.7 candidate RPC will itself run as the service role with ``is_deleted``
    encoded in SQL. The evidence SELECTs are read-only.
    """
    pre = f"SET ROLE {ELEVATE_ROLE};\n" if elevate else ""
    state = psql(env, pre +
                 f"SELECT indisvalid, pg_relation_size('{M.INDEX_NAME}'), "
                 f"pg_size_pretty(pg_relation_size('{M.INDEX_NAME}')) "
                 f"FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                 f"WHERE c.relname='{M.INDEX_NAME}';", timeout=30.0)
    rows = parse_rows(state.stdout)
    valid, size_bytes, pretty = (rows[0] + ["", "", ""])[:3] if rows else ("?", "", "")
    plans = {}
    for label, sql in M.evidence_queries():
        r = psql(env, pre + sql, timeout=120.0, on_error_stop=False)
        plan_text = r.stdout
        plans[label] = {"plan": plan_text, **M.parse_explain_plan(plan_text)}
    # Representative hit counts (proves is_deleted=false is encoded + real hits).
    hits = {}
    for term in ("WanVideoSampler", "controlnet settings", "FLUX.1"):
        h = psql(env, pre +
                 f"SELECT count(*) FROM {M.fully_qualified_table()} m "
                 f"WHERE m.is_deleted = false "
                 f"AND to_tsvector('simple'::regconfig, coalesce(m.content,'')) "
                 f"@@ websearch_to_tsquery('simple','{term}');", timeout=60.0)
        hl = parse_rows(h.stdout)
        hits[term] = int(hl[0][0]) if hl and hl[0][0].lstrip("-").isdigit() else None
    return {
        "index_valid": valid, "index_size_bytes": int(size_bytes) if str(size_bytes).isdigit() else 0,
        "index_size_pretty": pretty, "evidence_plans": plans, "representative_hit_counts": hits,
    }


def drop_index(env: dict, *, concurrent: bool = True, timeout: float = 300.0,
               elevate: bool = True) -> dict:
    sql = M.rollback_statement(concurrent=concurrent)
    if elevate:
        sql = f"SET ROLE {ELEVATE_ROLE};\n" + sql
    r = psql(env, sql, timeout=timeout, on_error_stop=False)
    return {"returncode": r.returncode, "stderr_tail": redact(_tail(r.stderr))}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_verdict(pf: dict) -> None:
    v = pf["verdict"]
    print(redact(f"connection: {pf['host_family']}/port-{pf['port']}"))
    print(redact(f"est_rows={v.get('est_rows'):,}  est_index≈{v.get('est_index_bytes',0)/1e6:.0f}MB  "
                 f"headroom≈{v.get('headroom_bytes',0)/1e9:.2f}GB"))
    for c in v["checks"]:
        print(redact(f"  [{'PASS' if c['pass'] else 'FAIL'}] {c['name']}: {c['detail']}"))
    print(redact(f"\nPREFLIGHT: {'GREEN' if v['green'] else 'RED'} "
                 f"(already_valid={v.get('already_valid')})"))
    if not v["green"]:
        print(redact("REASONS: " + "; ".join(v["reasons"]) if v["reasons"] else "(see failed checks)"))
    print(redact(f"ROLLBACK: {M.rollback_statement()}"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Task-1.3 live Hivemind FTS index driver.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true", help="read-only preflight (default)")
    mode.add_argument("--apply", action="store_true", help="build online if preflight green")
    mode.add_argument("--rollback", action="store_true", help="DROP INDEX CONCURRENTLY")
    mode.add_argument("--dry-run", action="store_true", help="print SQL + preflight, no mutation")
    mode.add_argument("--evidence", action="store_true", help="capture evidence only (index must exist)")
    ap.add_argument("--lock-timeout", type=int, default=30)
    ap.add_argument("--statement-timeout", type=int, default=1800)
    ap.add_argument("--max-wall-minutes", type=int, default=30)
    ap.add_argument("--no-elevate", action="store_true",
                    help="do not SET ROLE postgres before DDL (only if already the owner)")
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "hybrid-search")
    args = ap.parse_args(argv)
    elevate = not args.no_elevate

    print("Task 1.3 live driver — all output redacted (task-0.1 session-mode access)")
    try:
        env, host, port = derive_pg_env()
    except Exception as exc:  # noqa: BLE001
        print(redact(f"ACCESS FAILED: {exc}"))
        return 2

    if args.dry_run:
        print(redact("--- DRY RUN: SQL that --apply would execute (outside any transaction) ---"))
        bsql = M.build_statement(lock_timeout_s=args.lock_timeout,
                                 statement_timeout_s=args.statement_timeout)
        if elevate:
            bsql = f"SET ROLE {ELEVATE_ROLE};\n" + bsql
        print(redact(bsql))
        print(redact("--- ROLLBACK ---"))
        rsql = M.rollback_statement()
        if elevate:
            rsql = f"SET ROLE {ELEVATE_ROLE};\n" + rsql
        print(redact(rsql))
        print(redact("--- PREFLIGHT (read-only) ---"))
        pf = run_preflight(env, host, port)
        _print_verdict(pf)
        return 0

    if args.rollback:
        print(redact("Rolling back: DROP INDEX CONCURRENTLY ..."))
        res = drop_index(env, elevate=elevate)
        print(redact(f"rollback rc={res['returncode']} stderr_tail={res['stderr_tail']}"))
        return 0 if res["returncode"] == 0 else 1

    if args.evidence:
        ev = capture_evidence(env, elevate=elevate)
        print(redact(f"index_valid={ev['index_valid']} size={ev['index_size_pretty']} "
                     f"hits={ev['representative_hit_counts']}"))
        for label, p in ev["evidence_plans"].items():
            print(redact(f"  plan[{label}] uses_simple={p['uses_simple_index']} "
                         f"uses_english={p['uses_english_index']} seq={p['is_seq_scan']}"))
        return 0

    # default: preflight; --apply proceeds only if green
    pf = run_preflight(env, host, port)
    _print_verdict(pf)
    if not args.apply:
        return 0 if pf["verdict"]["green"] else 1

    if not pf["verdict"]["green"]:
        print(redact("REFUSING --apply: preflight RED. Re-run with --preflight."))
        return 1
    if pf["verdict"].get("already_valid"):
        print(redact("Index already valid — skipping build; capturing evidence."))
        ev = capture_evidence(env, elevate=elevate)
        (args.out / "phase1-message-fts-live.json").write_text(
            json.dumps({"preflight": pf, "build": {"status": "already_valid"},
                        "evidence": ev}, indent=2))
        return 0

    print(redact("Preflight GREEN — applying online build (CREATE INDEX CONCURRENTLY) ..."))
    build = apply_build(env, lock_timeout_s=args.lock_timeout,
                        statement_timeout_s=args.statement_timeout,
                        max_wall_minutes=args.max_wall_minutes, elevate=elevate)
    print(redact(f"build status={build['status']} rc={build.get('returncode')} "
                 f"elapsed={build.get('elapsed_s')}s samples={len(build.get('samples', []))} "
                 f"stderr_tail={build.get('stderr_tail','')}"))
    if build["status"] != "ok":
        (args.out / "phase1-message-fts-live.json").write_text(
            json.dumps({"preflight": pf, "build": build}, indent=2))
        return 1
    ev = capture_evidence(env, elevate=elevate)
    print(redact(f"EVIDENCE: index_valid={ev['index_valid']} size={ev['index_size_pretty']} "
                 f"hits={ev['representative_hit_counts']}"))
    for label, p in ev["evidence_plans"].items():
        print(redact(f"  plan[{label}] uses_simple={p['uses_simple_index']} "
                     f"uses_english={p['uses_english_index']} seq={p['is_seq_scan']}"))
    (args.out / "phase1-message-fts-live.json").write_text(
        json.dumps({"preflight": pf, "build": build, "evidence": ev}, indent=2))
    ok = ev["index_valid"] in ("t", "true") and all(
        p["uses_simple_index"] for p in ev["evidence_plans"].values())
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
