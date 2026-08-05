#!/usr/bin/env python3
"""Task-1.3 production-shaped rehearsal in an ISOLATED local PostgreSQL cluster.

Builds the canonical ``simple`` Discord-message FTS index on a production-shaped
~1.25M-row table inside a throwaway cluster (``initdb --auth=trust``, temp data
dir, Unix socket, **no network, no shared database**), measures everything the
plan's completion signal asks for, then **tears the cluster down**.

Measured:
  * elapsed wall-clock for ``CREATE INDEX CONCURRENTLY`` (online) vs a plain build;
  * index size (simple vs english), table size, cluster disk headroom;
  * saved ``EXPLAIN (ANALYZE, BUFFERS)`` plans proving representative ``simple``
    queries (``is_deleted = false``, snowflakes-as-text) use the new index and
    that a ``simple`` query cannot use the ``english`` index;
  * locks: a concurrent INSERT completes during a plain (blocking) build path is
    NOT asserted here (CIC semantics are PostgreSQL's); instead the
    cancellation/rollback path is proven directly below;
  * cancellation/rollback: a cancelled concurrent build leaves an INVALID index
    (``indisvalid = false``), ``DROP INDEX CONCURRENTLY`` removes it, and a fresh
    build then succeeds and is valid.

It mutates ONLY the throwaway cluster. The live Hivemind project is untouched
(the separate ``scripts/live_discord_fts.py`` driver does the live build). All
output is routed through the task-0.1 :func:`redact` boundary.

Run::

    python3 scripts/rehearse_discord_fts.py                    # ~1.25M default
    python3 scripts/rehearse_discord_fts.py --rows 200000 --out <dir>
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
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
from verify_access import redact  # noqa: E402 — reuse the 0.1 safety boundary


# ---------------------------------------------------------------------------
# Isolated-cluster lifecycle (no network; Unix socket in a temp dir)
# ---------------------------------------------------------------------------

class RehearsalCluster:
    """A throwaway PostgreSQL cluster owned by this process."""

    def __init__(self, port: int = 55432):
        self.port = port
        self.root = Path(tempfile.mkdtemp(prefix="hivemind_fts_rehearsal_"))
        self.datadir = self.root / "data"
        self.logfile = self.root / "postgres.log"
        self.env = {
            **os.environ,
            "PGHOST": str(self.root),          # unix socket dir
            "PGPORT": str(port),
            "PGUSER": "postgres",
            "PGDATABASE": "postgres",
        }

    def _run(self, cmd: list[str], **kw) -> subprocess.CompletedProcess:
        kw.setdefault("env", self.env)
        kw.setdefault("stdout", subprocess.PIPE)
        kw.setdefault("stderr", subprocess.PIPE)
        kw.setdefault("text", True)
        return subprocess.run(cmd, **kw)

    def start(self) -> None:
        for binname in ("initdb", "pg_ctl", "psql", "postgres"):
            if shutil.which(binname) is None:
                raise RuntimeError(f"required PG binary not on PATH: {binname}")
        init = self._run(["initdb", "-D", str(self.datadir), "-U", "postgres",
                          "-A", "trust", "--no-locale", "-E", "UTF8"], timeout=120)
        if init.returncode != 0:
            raise RuntimeError(f"initdb failed: {redact(init.stderr)}")
        # listen_addresses='' => no TCP; only the Unix socket in self.root.
        opts = f"-c listen_addresses='' -c unix_socket_directories='{self.root}' -p {self.port}"
        start = self._run(["pg_ctl", "-D", str(self.datadir), "-l", str(self.logfile),
                           "-o", opts, "-w", "start"], timeout=120)
        if start.returncode != 0:
            raise RuntimeError(f"pg_ctl start failed: {redact(start.stderr)} "
                               f"see {redact(str(self.logfile))}")

    def sql(self, statement: str, *, timeout: float = 600.0,
            on_error_stop: bool = True) -> subprocess.CompletedProcess:
        """Run one SQL string (autocommit per statement) in the cluster."""
        args = ["psql", "-X", "-q", "-t", "-A", "-P", "pager=off",
                "-v", f"ON_ERROR_STOP={'1' if on_error_stop else '0'}"]
        # Use a temp file so multi-statement / CTE scripts run with autocommit.
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as tf:
            tf.write(statement)
            script = tf.name
        try:
            return self._run(args + ["-f", script], timeout=timeout)
        finally:
            try:
                os.unlink(script)
            except OSError:
                pass

    def sql_t(self, statement: str, *, timeout: float = 600.0) -> str:
        """Run SQL and return stdout (tuples-friendly). Raises on error."""
        r = self.sql(statement, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError(f"psql failed: {redact(r.stderr or r.stdout)}")
        return r.stdout

    def stop(self) -> None:
        if (self.datadir / "postmaster.pid").exists():
            self._run(["pg_ctl", "-D", str(self.datadir), "-m", "fast", "-w", "stop"],
                      timeout=60)

    def destroy(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Measurement helpers
# ---------------------------------------------------------------------------

def _size(label: str, sql_expr: str, db: RehearsalCluster) -> tuple[str, int]:
    out = db.sql_t(f"SELECT pg_relation_size('{sql_expr}');").strip().splitlines()
    bytes_ = int(out[-1]) if out and out[-1].isdigit() else 0
    return (label, bytes_)


def disk_free_bytes(path: Path) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def fmt_mb(b: int) -> str:
    return f"{b / 1e6:.1f} MB"


def run_explain(db: RehearsalCluster, sql: str) -> str:
    return db.sql_t(sql, timeout=120)


def _tail(text: str) -> str:
    """Last non-empty line of *text* as a string (safe for redact)."""
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def count_hits(db: RehearsalCluster, term: str, constructor: str = "websearch_to_tsquery") -> int:
    out = db.sql_t(f"""
        SELECT count(*) FROM {M.fully_qualified_table()} m
         WHERE m.is_deleted = false
           AND to_tsvector('simple'::regconfig, coalesce(m.content,''))
               @@ {constructor}('simple','{term}');
    """).strip().splitlines()
    return int(out[-1]) if out and out[-1].lstrip("-").isdigit() else 0


# ---------------------------------------------------------------------------
# The rehearsal
# ---------------------------------------------------------------------------

def rehearse(rows: int, out_dir: Path) -> dict:
    db = RehearsalCluster()
    evidence: dict = {
        "task": "1.3-rehearsal",
        "rows_requested": rows,
        "index_name": M.INDEX_NAME,
        "index_expression": M.index_expression(),
        "config": M.LEXICAL_CONFIG,
        "steps": [],
    }
    try:
        db.start()
        evidence["pg_version"] = db.sql_t("SHOW server_version;").strip()
        evidence["cluster"] = {"isolated": True, "network": "off (unix socket only)"}

        # 1. schema + seed
        before_seed = disk_free_bytes(db.root)
        t0 = time.time()
        db.sql_t(M.rehearsal_schema_sql())
        db.sql_t(M.rehearsal_seed_sql(rows), timeout=900)
        seed_secs = time.time() - t0
        actual_rows = int(db.sql_t(
            f"SELECT count(*) FROM {M.fully_qualified_table()};").strip().splitlines()[-1])
        deleted = int(db.sql_t(
            f"SELECT count(*) FROM {M.fully_qualified_table()} WHERE is_deleted=true;"
        ).strip().splitlines()[-1])
        null_content = int(db.sql_t(
            f"SELECT count(*) FROM {M.fully_qualified_table()} WHERE content IS NULL;"
        ).strip().splitlines()[-1])
        evidence["seed"] = {
            "elapsed_s": round(seed_secs, 2),
            "actual_rows": actual_rows,
            "deleted_rows": deleted,
            "null_content_rows": null_content,
            "disk_free_before_seed_bytes": before_seed,
        }
        db.sql_t("ANALYZE " + M.fully_qualified_table() + ";")

        # 2. english index first (mirror live schema; the simple query must ignore it)
        t0 = time.time()
        db.sql_t(
            f"CREATE INDEX {M.ENGLISH_INDEX_NAME} ON {M.fully_qualified_table()} "
            f"USING gin (to_tsvector('english'::regconfig, content));", timeout=900)
        english_secs = time.time() - t0
        _, english_bytes = _size("english", M.ENGLISH_INDEX_NAME, db)

        # 3. BASELINE plan: simple query with NO simple index -> must NOT use english
        baseline_plan = run_explain(db, M.baseline_no_simple_index_query())
        baseline_parse = M.parse_explain_plan(baseline_plan)
        evidence["baseline_no_simple_index"] = {
            "plan": baseline_plan,
            "uses_simple_index": baseline_parse["uses_simple_index"],
            "uses_english_index": baseline_parse["uses_english_index"],
            "is_seq_scan": baseline_parse["is_seq_scan"],
            "note": "pre-1.3: a 'simple' query cannot use the 'english' expression index",
        }

        # 4. canonical ONLINE build (CONCURRENTLY), timed
        before_build = disk_free_bytes(db.root)
        t0 = time.time()
        db.sql_t(M.build_statement(), timeout=1800)
        build_secs = time.time() - t0
        _, simple_bytes = _size("simple", M.INDEX_NAME, db)
        is_valid = db.sql_t(
            f"SELECT indisvalid FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
            f"WHERE c.relname='{M.INDEX_NAME}';").strip().splitlines()[-1]
        _, table_total = (None, int(db.sql_t(
            f"SELECT pg_total_relation_size('{M.fully_qualified_table()}');"
        ).strip().splitlines()[-1]))
        evidence["online_build"] = {
            "elapsed_s": round(build_secs, 2),
            "index_bytes": simple_bytes,
            "index_size_pretty": fmt_mb(simple_bytes),
            "english_index_bytes": english_bytes,
            "english_size_pretty": fmt_mb(english_bytes),
            "english_build_elapsed_s": round(english_secs, 2),
            "indisvalid": is_valid,
            "table_total_bytes": table_total,
            "disk_free_before_build_bytes": before_build,
            "disk_free_after_build_bytes": disk_free_bytes(db.root),
        }

        # 5. EVIDENCE plans: representative simple queries use the new index
        plans = {}
        for label, sql in M.evidence_queries():
            plan = run_explain(db, sql)
            plans[label] = {"plan": plan, **M.parse_explain_plan(plan)}
        hits = {term: count_hits(db, term) for term in
                ("WanVideoSampler", "controlnet settings", "FLUX.1")}
        evidence["evidence_plans"] = plans
        evidence["representative_hit_counts"] = hits

        # 6. CANCELLATION / ROLLBACK proof
        # Drop the good index, start a concurrent rebuild, cancel it mid-flight,
        # confirm an INVALID index remains, then DROP CONCURRENTLY it and rebuild.
        db.sql_t(M.rollback_statement(), timeout=300)
        canc = _demonstrate_cancellation(db)
        evidence["cancellation_rollback"] = canc

        verdict = _verdict(evidence)
        evidence["verdict"] = verdict

    finally:
        db.stop()
        db.destroy()

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "phase1-message-fts-rehearsal.json").write_text(
        json.dumps(evidence, indent=2))
    return evidence


def _demonstrate_cancellation(db: RehearsalCluster) -> dict:
    """Cancel a concurrent build mid-flight; prove invalid remnant + clean rollback.

    Deterministic regardless of machine speed: a second connection opens a
    transaction and HOLDS it open. ``CREATE INDEX CONCURRENTLY`` finishes its
    initial build scan but then must block in its validation phase waiting for
    that pre-existing transaction to finish. We cancel the build while it is
    blocked; PostgreSQL leaves the index catalog entry present but INVALID
    (``indisvalid = false``). ``DROP INDEX CONCURRENTLY`` then removes it cleanly
    and a fresh build succeeds and is valid — i.e. the full rollback/recovery
    path a failed live build would need.
    """
    result: dict = {}
    db.sql_t(M.rollback_statement(), timeout=120)  # start clean

    def _quiesce() -> None:
        """Definitively end the holder backend + any foreign open txn, then wait."""
        # Server-side termination is authoritative (kills the sleeping backend even
        # if the psql client ignores SIGTERM). Repeat + poll until clear.
        for _ in range(20):
            db.sql(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE pid <> pg_backend_pid() AND datname = current_database() "
                "  AND (query LIKE '%pg_sleep%' OR state = 'idle in transaction' "
                "       OR xact_start IS NOT NULL);",
                timeout=10, on_error_stop=False)
            left = db.sql_t(
                "SELECT count(*) FROM pg_stat_activity WHERE pid <> pg_backend_pid() "
                "AND datname = current_database() AND xact_start IS NOT NULL;"
            ).strip()
            if (left.splitlines() or ["0"])[-1].strip() == "0":
                return
            time.sleep(0.5)

    cic_sql = (
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {M.INDEX_NAME}\n"
        f"    ON {M.fully_qualified_table()}\n"
        f"    USING gin ({M.index_expression()});\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as tf:
        tf.write(cic_sql)
        cic_script = tf.name

    # Connection A: hold a transaction open (pg_sleep). 40s is far longer than the
    # cancel window needs; _quiesce ends it authoritatively before the rebuild.
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as tf:
        tf.write("BEGIN;\nSELECT pg_sleep(40);\nCOMMIT;\n")
        holder_script = tf.name

    holder = subprocess.Popen(
        ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=0", "-f", holder_script],
        env=db.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        time.sleep(2.0)  # let A's transaction register

        # Connection B: the concurrent build. Popen = non-blocking.
        builder = subprocess.Popen(
            ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=0", "-f", cic_script],
            env=db.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        # Find the builder's backend pid, wait for it to reach the validation wait
        # (blocked on the holder txn), then cancel it.
        cancelled = False
        builder_pid = None
        cancel_by = time.time() + 25
        observed_running_at = None
        while time.time() < cancel_by:
            rows = db.sql(
                "SELECT pid, wait_event_type, wait_event FROM pg_stat_activity "
                "WHERE query LIKE '%CREATE INDEX CONCURRENTLY%' "
                "AND pid <> pg_backend_pid();", timeout=10, on_error_stop=False)
            lines = [l for l in (rows.stdout or "").splitlines() if l.strip()]
            if lines:
                parts = lines[0].split("|")
                if len(parts) >= 3:
                    builder_pid = int(parts[0])
                    if observed_running_at is None:
                        observed_running_at = time.time()
                    # Cancel once blocked on the holder's virtual xact, or after a
                    # short grace once we have seen it running (covers fast machines
                    # where phase 1 + wait both happen quickly).
                    blocked = parts[1] in ("VirtualXact", "Lock")
                    grace_up = (time.time() - observed_running_at) >= 2.0
                    if (blocked or grace_up) and not cancelled:
                        time.sleep(0.8)
                        db.sql_t(f"SELECT pg_cancel_backend({builder_pid});", timeout=10)
                        cancelled = True
                        break
            time.sleep(0.4)

        builder_out, builder_err = builder.communicate(timeout=60)
        result["builder"] = {
            "returncode": builder.returncode,
            "cancelled_via_pg_cancel_backend": cancelled,
            "builder_pid": builder_pid,
            "stderr_tail": redact(_tail(builder_err)),
        }
    finally:
        if holder.poll() is None:
            holder.terminate()
            try:
                holder.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                holder.kill()
        _quiesce()
        for p in (cic_script, holder_script):
            try:
                os.unlink(p)
            except OSError:
                pass

    is_valid_now = db.sql_t(
        f"SELECT indisvalid FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
        f"WHERE c.relname='{M.INDEX_NAME}';").strip().splitlines()[-1]
    result["indisvalid_after_interrupt"] = is_valid_now
    result["interrupt_left_invalid_index"] = (is_valid_now == "f")

    # Rollback: DROP CONCURRENTLY removes the invalid remnant cleanly.
    drop = db.sql(M.rollback_statement(), timeout=300, on_error_stop=False)
    remains = int(db.sql_t(
        f"SELECT count(*) FROM pg_class WHERE relname='{M.INDEX_NAME}';"
    ).strip().splitlines()[-1])
    result["rollback"] = {
        "drop_rc": drop.returncode,
        "stderr_tail": redact(_tail(drop.stderr)),
        "index_remains_after_drop": remains,
    }

    # Fresh rebuild succeeds and is valid (proves recoverability). Generous
    # lock_timeout since the cluster is quiesced; retry once on a transient lock.
    _quiesce()
    t0 = time.time()
    rebuild_sql = M.build_statement(lock_timeout_s=120)
    r = db.sql(rebuild_sql, timeout=1800, on_error_stop=False)
    if r.returncode != 0:
        _quiesce()
        r = db.sql(rebuild_sql, timeout=1800, on_error_stop=False)
    if r.returncode != 0:
        raise RuntimeError(f"rebuild failed after rollback: {redact(_tail(r.stderr))}")
    result["rebuild_elapsed_s"] = round(time.time() - t0, 2)
    result["indisvalid_after_rebuild"] = db.sql_t(
        f"SELECT indisvalid FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
        f"WHERE c.relname='{M.INDEX_NAME}';").strip().splitlines()[-1]
    return result


def _verdict(ev: dict) -> dict:
    plans = ev.get("evidence_plans", {})
    all_use_simple = all(p.get("uses_simple_index") for p in plans.values()) if plans else False
    none_use_english = all(not p.get("uses_english_index") for p in plans.values()) if plans else True
    baseline_ok = (not ev.get("baseline_no_simple_index", {}).get("uses_simple_index")
                   and not ev.get("baseline_no_simple_index", {}).get("uses_english_index"))
    build = ev.get("online_build", {})
    canc = ev.get("cancellation_rollback", {})
    checks = {
        "online_build_valid": build.get("indisvalid") == "t",
        "evidence_uses_simple_index": all_use_simple,
        "evidence_does_not_use_english": none_use_english,
        "baseline_cannot_use_english_or_simple": baseline_ok,
        "cancellation_left_invalid_index": canc.get("interrupt_left_invalid_index") is True,
        "rollback_removed_index": canc.get("rollback", {}).get("index_remains_after_drop") == 0,
        "rebuild_valid_after_rollback": canc.get("indisvalid_after_rebuild") == "t",
    }
    return {"all_pass": all(checks.values()), "checks": checks}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Task-1.3 isolated-cluster rehearsal.")
    ap.add_argument("--rows", type=int, default=1_250_000)
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "hybrid-search")
    args = ap.parse_args(argv)

    print(redact(f"Task 1.3 rehearsal: isolated PG cluster, ~{args.rows:,} rows"))
    ev = rehearse(args.rows, args.out)
    v = ev["verdict"]
    print(redact(f"\nPG version: {ev.get('pg_version','?').strip()}"))
    print(redact(f"Seeded rows: {ev['seed']['actual_rows']:,} "
                 f"(deleted={ev['seed']['deleted_rows']}, null={ev['seed']['null_content_rows']}) "
                 f"in {ev['seed']['elapsed_s']}s"))
    b = ev["online_build"]
    print(redact(f"Online build: {b['elapsed_s']}s, simple={b['index_size_pretty']} "
                 f"(english={b['english_size_pretty']} in {b['english_build_elapsed_s']}s), "
                 f"indisvalid={b['indisvalid']}"))
    print(redact(f"Representative hits: {ev['representative_hit_counts']}"))
    for label, p in ev["evidence_plans"].items():
        print(redact(f"  plan[{label}]: uses_simple={p['uses_simple_index']} "
                     f"uses_english={p['uses_english_index']} seq={p['is_seq_scan']}"))
    c = ev["cancellation_rollback"]
    print(redact(f"Cancellation: invalid_after_interrupt={c.get('indisvalid_after_interrupt')} "
                 f"drop_removed={(c.get('rollback',{}).get('index_remains_after_drop')==0)} "
                 f"rebuild_valid={c.get('indisvalid_after_rebuild')}"))
    print(redact(f"\nVERDICT all_pass={v['all_pass']}"))
    for k, val in v["checks"].items():
        print(redact(f"  [{'PASS' if val else 'FAIL'}] {k}"))
    print(redact(f"Evidence written to {args.out / 'phase1-message-fts-rehearsal.json'}"))
    return 0 if v["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
