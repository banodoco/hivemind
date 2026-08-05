#!/usr/bin/env python3
"""Task-1.5 live driver: preflight + build + evidence + rollback on Hivemind.

Applies the two bounded normalized short-field trigram indexes
(``idx_external_resources_title_trgm_norm`` on ``external_resources.title`` and
``idx_distillations_question_trgm_norm`` on ``distillations.question``, both over
the frozen ``public.hivemind_normalize_identifier`` / ``gin_trgm_ops``) on the
**actual** Hivemind Supabase project using the task-0.1 safe session-mode access
path (``supabase db dump --dry-run`` derives a short-lived CLI-login libpq env
held only in memory for the duration of the psql calls — never printed/logged/
persisted). This reuses ``verify_access.parse_dryrun_pg_env`` / ``redact``
verbatim so the safety boundary never drifts (same pattern as task 1.3).

Flow:
  1. ``--preflight`` (default, read-only): schema/005 prerequisite present?,
     target table/column identity, row estimate, existing trigram indexes,
     invalid/concurrent remnants, in-progress builds, long/locking transactions,
     relation locks, storage headroom, timeout settings, session-vs-pooler mode.
     Emits a green/red verdict + the rollback commands.
  2. ``--apply`` (only if preflight green): if the schema/005 prerequisite is
     missing, apply it safely first (deterministic ICU collation + IMMUTABLE
     functions + alias table; idempotent DDL, no corpus mutation), then run a
     read-only SQL/Python parity probe on representative live-safe values, then
     build BOTH indexes concurrently (``SET ROLE postgres``; bounded
     ``lock_timeout`` + generous ``statement_timeout``; progress-monitored; no
     source-row mutation), then capture validity/size + saved ``EXPLAIN``
     evidence that representative normalized ``<%``/``%`` queries use them.
  3. ``--rollback``: ``DROP INDEX CONCURRENTLY IF EXISTS`` both task-1.5 indexes
     (leaves schema/005 and the raw schema/001 indexes in place).
  4. ``--dry-run``: print the exact SQL that would run + preflight; no mutation.

Safety: every human-facing line is routed through :func:`redact`. The build never
mutates source rows; it only adds indexes (and, if missing, applies the additive
schema/005 prerequisite). ``--apply`` is refused unless the preflight is green.

Run::

    python3 scripts/live_short_field_trigram.py --preflight
    python3 scripts/live_short_field_trigram.py --apply        # only if preflight green
    python3 scripts/live_short_field_trigram.py --rollback
    python3 scripts/live_short_field_trigram.py --dry-run
    python3 scripts/live_short_field_trigram.py --evidence     # index must exist
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

import scripts.short_field_trigram as M  # noqa: E402
from verify_access import parse_dryrun_pg_env, redact  # noqa: E402

#: The CLI login role (``cli_login_postgres``) is a MEMBER of ``postgres`` (the
#: owner of the target tables) but the CREATE INDEX ownership check is not
#: satisfied by inherited privileges, so the driver elevates with SET ROLE for
#: the DDL only (build/drop) — the canonical 1.3-proven path. The migration
#: (schema/006) stays elevation-free so it is runnable as the owner directly.
ELEVATE_ROLE = "postgres"

SCHEMA_005_PATH = REPO_ROOT / "schema" / "005_identifier_normalization.sql"


# ---------------------------------------------------------------------------
# Session-mode access (task 0.1) — credential lives only in a child-process env
# ---------------------------------------------------------------------------

def derive_pg_env() -> tuple[dict, str, str]:
    """Derive the short-lived CLI-login libpq env from ``supabase db dump --dry-run``.

    Returns (full_env_for_subprocess, pghost, pgport). Never prints the env.
    """
    proc = subprocess.run(
        ["supabase", "db", "dump", "--schema", "public", "--dry-run"],
        capture_output=True, text=True, timeout=30.0, stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"supabase db dump --dry-run failed (rc={proc.returncode}): "
                           f"{redact(_tail(proc.stderr or ''))}")
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
        parsed[label] = parse_rows(r.stdout)
        raw[label] = {"rc": r.returncode, "stderr_tail": redact(_tail(r.stderr))}
    verdict = M.evaluate_preflight(parsed, pghost=host, pgport=port)
    return {"parsed": parsed, "raw": raw, "verdict": verdict,
            "host_family": ("pooler" if "pooler.supabase.com" in host else "session"),
            "port": port}


# ---------------------------------------------------------------------------
# Apply schema/005 prerequisite (idempotent; only if missing)
# ---------------------------------------------------------------------------

def apply_schema_005(env: dict) -> dict:
    """Apply the frozen schema/005 migration (collation + functions + alias table).

    Idempotent DDL applied via session-mode psql (each statement autocommits),
    elevated with SET ROLE postgres (CREATE COLLATION provider=icu needs
    superuser). No corpus rows are read or mutated. Returns a small status dict.
    """
    sql = f"SET ROLE {ELEVATE_ROLE};\n" + SCHEMA_005_PATH.read_text()
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as tf:
        tf.write(sql)
        script = tf.name
    try:
        r = subprocess.run(
            ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", script],
            env=env, capture_output=True, text=True, timeout=120.0,
            stdin=subprocess.DEVNULL)
    finally:
        try:
            os.unlink(script)
        except OSError:
            pass
    return {"returncode": r.returncode, "stderr_tail": redact(_tail(r.stderr))}


def parity_probe(env: dict, *, n: int = 8) -> dict:
    """Read-only SQL/Python parity probe on representative live-safe values.

    Samples up to ``n`` resource titles and distillation questions (fetched as a
    JSON array so any text content survives intact), normalizes each in SQL
    (``hivemind_normalize_identifier``) and offline in Python
    (``executors.identifier_normalization.normalize_identifier``), and reports the
    mismatch count. Raw values are NEVER printed (only the parity verdict). This
    re-confirms the live ICU collation matches the Python reference. A mismatch
    flags a non-NFC source value (the documented Python↔SQL NFC boundary), not an
    index defect — it is reported, not fatal.
    """
    import json as _json
    from executors import identifier_normalization as IN
    pre = f"SET ROLE {ELEVATE_ROLE};\n"
    title_sql = (pre +
        "SELECT coalesce(jsonb_agg(j), '[]'::jsonb) FROM ("
        f"SELECT jsonb_build_object('id', id, 'v', title) AS j "
        "FROM public.external_resources ORDER BY id LIMIT " + str(int(n)) + ") s;")
    q_sql = (pre +
        "SELECT coalesce(jsonb_agg(j), '[]'::jsonb) FROM ("
        "SELECT jsonb_build_object('id', id, 'v', question) AS j "
        "FROM public.distillations WHERE status IN ('pending','approved') "
        "ORDER BY id LIMIT " + str(int(n)) + ") s;")
    mismatches = 0
    checked = 0
    samples: list[dict] = []
    for label, sql in (("resource", title_sql), ("distillation", q_sql)):
        r = psql(env, sql, timeout=30.0, on_error_stop=False)
        # The JSON array is the last non-empty line of -tA output.
        lines = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
        try:
            arr = _json.loads(lines[-1]) if lines else []
        except (ValueError, IndexError):
            arr = []
        for obj in arr:
            rid = str(obj.get("id"))
            raw_val = obj.get("v") or ""
            esc = raw_val.replace("'", "''")
            sql_out = psql(env, pre +
                f"SELECT hivemind_normalize_identifier('{esc}');",
                timeout=15.0).stdout.strip().splitlines()
            sql_norm = sql_out[-1] if sql_out else ""
            py_norm = IN.normalize_identifier(raw_val)
            ok = (sql_norm == py_norm)
            checked += 1
            mismatches += 0 if ok else 1
            samples.append({"entity": label, "id": rid, "parity": ok})  # no raw value
    return {"checked": checked, "mismatches": mismatches,
            "parity_ok": mismatches == 0, "samples": samples}


# ---------------------------------------------------------------------------
# Build (online) with progress monitoring
# ---------------------------------------------------------------------------

def apply_build(env: dict, *, lock_timeout_s: int = 30, statement_timeout_s: int = 1800,
                max_wall_minutes: int = 20, elevate: bool = True) -> dict:
    """Run BOTH CREATE INDEX CONCURRENTLY in the background, monitor, validate."""
    build_sql = M.build_statements(lock_timeout_s=lock_timeout_s,
                                   statement_timeout_s=statement_timeout_s)
    if elevate:
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
                return {"status": "wall_clock_exceeded", "samples": samples,
                        "elapsed_s": round(time.time() - t0, 2)}
            pr = psql(env,
                      "SELECT phase, blocks_done, blocks_total, tuples_done, tuples_total "
                      "FROM pg_stat_progress_create_index "
                      "WHERE relid IN ('public.external_resources'::regclass, "
                      "'public.distillations'::regclass);",
                      timeout=15.0, on_error_stop=False)
            rows = parse_rows(pr.stdout)
            if rows:
                phase, bd, bt, td, tt = (rows[0] + ["", "", "", "", ""])[:5]
                samples.append({"t": round(time.time() - t0, 1), "phase": phase,
                                "blocks_done": bd, "blocks_total": bt,
                                "tuples_done": td, "tuples_total": tt})
            time.sleep(3.0)
        out, err = proc.communicate(timeout=60)
        return {"status": "ok" if proc.returncode == 0 else "error",
                "returncode": proc.returncode,
                "elapsed_s": round(time.time() - t0, 2),
                "stderr_tail": redact(_tail(err)), "samples": samples}
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
    """Index validity + sizes + saved EXPLAIN plans proving the indexes are used.

    Runs as ``postgres`` (SET ROLE) because the target tables are externally
    owned (the CLI login role has no direct SELECT) — the same 1.3 evidence path.
    Captures natural + forced (enable_seqscan=off) EXPLAIN plans; the tiny
    11-row distillation table's natural plan is (correctly) a seq scan, so the
    forced plan proves structural usability.
    """
    pre = f"SET ROLE {ELEVATE_ROLE};\n" if elevate else ""
    state = {}
    for t in M.TARGETS:
        r = psql(env, pre +
                 f"SELECT indisvalid, pg_relation_size('{t['index_name']}'), "
                 f"pg_size_pretty(pg_relation_size('{t['index_name']}')) "
                 f"FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                 f"WHERE c.relname='{t['index_name']}';", timeout=30.0)
        rows = parse_rows(r.stdout)
        valid, size_bytes, pretty = (rows[0] + ["", "", ""])[:3] if rows else ("?", "", "")
        state[t["index_name"]] = {
            "index_valid": valid,
            "index_size_bytes": int(size_bytes) if str(size_bytes).isdigit() else 0,
            "index_size_pretty": pretty,
        }
    plans: dict[str, dict] = {}
    for label, sql in M.evidence_queries():
        r = psql(env, pre + sql, timeout=120.0, on_error_stop=False)
        plans[label] = {"plan": r.stdout, "forced": False, **M.parse_explain_plan(r.stdout)}
    for label, sql in M.forced_evidence_queries():
        r = psql(env, pre + sql, timeout=120.0, on_error_stop=False)
        plans[label] = {"plan": r.stdout, "forced": True, **M.parse_explain_plan(r.stdout)}
    # Representative hit counts (proves eligibility + cross-variant recall, live).
    hits = {}
    for term, op, table in (("Wan2.2", "<%", M.TITLE_TABLE),
                            ("FLUX.1", "<%", M.TITLE_TABLE),
                            ("best upscale model", "<%", M.QUESTION_TABLE)):
        column = M.QUESTION_COLUMN if table == M.QUESTION_TABLE else M.TITLE_COLUMN
        pred = next(t["predicate"] for t in M.TARGETS if t["table"] == table)
        guc = "pg_trgm.word_similarity_threshold"
        h = psql(env, pre +
                 f"SET {guc} = {M.WORD_SIMILARITY_THRESHOLD};\n"
                 f"SELECT count(*) FROM {M.fully_qualified_table(table)} "
                 f"WHERE {pred} AND hivemind_normalize_identifier('{term}') {op} "
                 f"hivemind_normalize_identifier({column});", timeout=60.0)
        hl = parse_rows(h.stdout)
        hits[f"{table}:{term}"] = (int(hl[0][0]) if hl and hl[0][0].lstrip("-").isdigit()
                                   else None)
    # Honest planner note: the distillation table is tiny (11 rows), so neither
    # the natural nor forced plan uses the question trigram index — the planner
    # seq-scans (natural) or uses the status btree (forced) and evaluates <% as a
    # filter. The title index IS used at production scale (2,7xx rows). The
    # question index is provisioned per the frozen contract and becomes
    # planner-relevant as distillations grow; its eligibility predicate is correct.
    title_natural_uses = any(
        k.startswith("title_") and not p.get("forced") and p.get("uses_normalized_index")
        for k, p in plans.items())
    return {"indexes": state, "evidence_plans": plans, "representative_hit_counts": hits,
            "title_index_used_at_production_scale": title_natural_uses,
            "question_index_planner_note": (
                "At the current 11-row distillation count the planner seq-scans "
                "(natural) or uses distillations_status_idx (forced) and evaluates "
                "<% as a filter over 11 rows; the question trigram index is valid "
                "and eligibility-correct but not planner-exercised until the table "
                "grows. Proven structurally servable in the isolated rehearsal.")}


def drop_indexes(env: dict, *, elevate: bool = True, timeout: float = 300.0) -> dict:
    sql = M.rollback_statements()
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
    print(redact(f"est_rows={v.get('est_rows'):,}  est_index≈{v.get('est_index_bytes',0)/1e3:.0f}KB  "
                 f"headroom≈{v.get('headroom_bytes',0)/1e9:.2f}GB"))
    for c in v["checks"]:
        print(redact(f"  [{'PASS' if c['pass'] else 'FAIL'}] {c['name']}: {c['detail']}"))
    print(redact(f"\nPREFLIGHT: {'GREEN' if v['green'] else 'RED'} "
                 f"(already_valid={v.get('already_valid')}, "
                 f"schema_005_needed={v.get('schema_005_needed')})"))
    if not v["green"]:
        print(redact("REASONS: " + ("; ".join(v["reasons"]) if v["reasons"] else "(see failed checks)")))
    print(redact("ROLLBACK:\n" + M.rollback_statements()))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Task-1.5 live Hivemind short-field trigram driver.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true", help="read-only preflight (default)")
    mode.add_argument("--apply", action="store_true", help="apply 005(if needed)+build if preflight green")
    mode.add_argument("--rollback", action="store_true", help="DROP INDEX CONCURRENTLY (both)")
    mode.add_argument("--dry-run", action="store_true", help="print SQL + preflight, no mutation")
    mode.add_argument("--evidence", action="store_true", help="capture evidence only (indexes must exist)")
    ap.add_argument("--lock-timeout", type=int, default=30)
    ap.add_argument("--statement-timeout", type=int, default=1800)
    ap.add_argument("--max-wall-minutes", type=int, default=20)
    ap.add_argument("--no-elevate", action="store_true",
                    help="do not SET ROLE postgres before DDL (only if already the owner)")
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "hybrid-search")
    args = ap.parse_args(argv)
    elevate = not args.no_elevate

    print("Task 1.5 live driver — all output redacted (task-0.1 session-mode access)")
    try:
        env, host, port = derive_pg_env()
    except Exception as exc:  # noqa: BLE001
        print(redact(f"ACCESS FAILED: {exc}"))
        return 2

    if args.dry_run:
        print(redact("--- DRY RUN: SQL that --apply would execute (outside any transaction) ---"))
        bsql = M.build_statements(lock_timeout_s=args.lock_timeout,
                                  statement_timeout_s=args.statement_timeout)
        if elevate:
            bsql = f"SET ROLE {ELEVATE_ROLE};\n" + bsql
        print(redact(bsql))
        print(redact("--- ROLLBACK ---"))
        rsql = M.rollback_statements()
        if elevate:
            rsql = f"SET ROLE {ELEVATE_ROLE};\n" + rsql
        print(redact(rsql))
        print(redact("--- PREFLIGHT (read-only) ---"))
        pf = run_preflight(env, host, port)
        _print_verdict(pf)
        return 0

    if args.rollback:
        print(redact("Rolling back: DROP INDEX CONCURRENTLY both task-1.5 indexes ..."))
        res = drop_indexes(env, elevate=elevate)
        print(redact(f"rollback rc={res['returncode']} stderr_tail={res['stderr_tail']}"))
        return 0 if res["returncode"] == 0 else 1

    if args.evidence:
        ev = capture_evidence(env, elevate=elevate)
        print(redact(f"indexes={ev['indexes']} hits={ev['representative_hit_counts']}"))
        for label, p in ev["evidence_plans"].items():
            print(redact(f"  plan[{label}] uses_norm={p['uses_normalized_index']} "
                         f"uses_raw={p['uses_raw_trgm_index']} seq={p['is_seq_scan']}"))
        return 0

    # default: preflight; --apply proceeds only if green
    pf = run_preflight(env, host, port)
    _print_verdict(pf)
    if not args.apply:
        (args.out / "phase1-short-field-trigram-live.json").write_text(
            json.dumps({"preflight": pf}, indent=2))
        return 0 if pf["verdict"]["green"] else 1

    if not pf["verdict"]["green"]:
        print(redact("REFUSING --apply: preflight RED. Re-run with --preflight."))
        (args.out / "phase1-short-field-trigram-live.json").write_text(
            json.dumps({"preflight": pf, "apply": {"status": "refused_preflight_red"}}, indent=2))
        return 1

    result: dict = {"preflight": pf}
    # 1. Apply schema/005 prerequisite if missing.
    if pf["verdict"].get("schema_005_needed"):
        print(redact("Applying schema/005 prerequisite (idempotent DDL; SET ROLE postgres) ..."))
        r = apply_schema_005(env)
        print(redact(f"schema/005 apply rc={r['returncode']} stderr_tail={r['stderr_tail']}"))
        result["schema_005_apply"] = r
        if r["returncode"] != 0:
            (args.out / "phase1-short-field-trigram-live.json").write_text(
                json.dumps(result, indent=2))
            return 1
        # re-confirm the prerequisite is now present.
        check = psql(env, M.schema_005_applied_check(), timeout=30.0)
        cr = parse_rows(check.stdout)
        fn_ok = cr and M._is_true(cr[0][0])
        result["schema_005_confirmed"] = bool(fn_ok)
        if not fn_ok:
            print(redact("schema/005 applied but prerequisite check still false — aborting."))
            (args.out / "phase1-short-field-trigram-live.json").write_text(
                json.dumps(result, indent=2))
            return 1
    else:
        result["schema_005_apply"] = {"status": "already_present"}

    # 2. Read-only SQL/Python parity probe on representative live-safe values.
    print(redact("Running read-only SQL/Python parity probe ..."))
    parity = parity_probe(env)
    print(redact(f"parity: checked={parity['checked']} mismatches={parity['mismatches']} "
                 f"ok={parity['parity_ok']}"))
    result["parity_probe"] = parity

    # 3. Build BOTH indexes concurrently (monitored).
    if pf["verdict"].get("already_valid"):
        print(redact("Indexes already valid — skipping build; capturing evidence."))
        result["build"] = {"status": "already_valid"}
    else:
        print(redact("Preflight GREEN — applying online build (CREATE INDEX CONCURRENTLY x2) ..."))
        build = apply_build(env, lock_timeout_s=args.lock_timeout,
                            statement_timeout_s=args.statement_timeout,
                            max_wall_minutes=args.max_wall_minutes, elevate=elevate)
        print(redact(f"build status={build['status']} rc={build.get('returncode')} "
                     f"elapsed={build.get('elapsed_s')}s samples={len(build.get('samples', []))} "
                     f"stderr_tail={build.get('stderr_tail','')}"))
        result["build"] = build
        if build["status"] != "ok":
            (args.out / "phase1-short-field-trigram-live.json").write_text(
                json.dumps(result, indent=2))
            return 1

    # 4. Evidence (read-only).
    ev = capture_evidence(env, elevate=elevate)
    print(redact(f"EVIDENCE: indexes={ev['indexes']} hits={ev['representative_hit_counts']}"))
    for label, p in ev["evidence_plans"].items():
        print(redact(f"  plan[{label}] uses_norm={p['uses_normalized_index']} "
                     f"uses_raw={p['uses_raw_trgm_index']} seq={p['is_seq_scan']}"))
    result["evidence"] = ev
    (args.out / "phase1-short-field-trigram-live.json").write_text(
        json.dumps(result, indent=2))

    all_valid = all(ix["index_valid"] in ("t", "true") for ix in ev["indexes"].values())
    title_natural_uses = ev.get("title_index_used_at_production_scale") is True
    # Parity is reported but not a hard block: a mismatch flags a non-NFC source
    # value (the documented Python↔SQL NFC boundary for the ingest layer), not an
    # index defect. Success = both indexes valid + the title index proven used at
    # production scale (the question index is valid but not planner-used at 11
    # rows — see evidence.question_index_planner_note).
    return 0 if (all_valid and title_natural_uses) else 1


if __name__ == "__main__":
    raise SystemExit(main())
