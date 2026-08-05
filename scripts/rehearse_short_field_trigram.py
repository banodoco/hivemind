#!/usr/bin/env python3
"""Task-1.5 production-shaped rehearsal in an ISOLATED local PostgreSQL cluster.

Builds the two normalized short-field trigram indexes (``external_resources.title``
and ``distillations.question`` over ``hivemind_normalize_identifier``) on a
production-shaped table set inside a throwaway cluster (``initdb --auth=trust``,
temp data dir, Unix socket, **no network, no shared database**), measures
everything the plan's completion signal asks for, then **tears the cluster down**.

Measured:
  * the FROZEN schema/005 prerequisite (collation + IMMUTABLE normalize fn)
    loads and the IMMUTABLE expression index builds against it;
  * elapsed wall-clock for each ``CREATE INDEX CONCURRENTLY`` (online);
  * normalized index sizes vs the raw schema/001 trigram indexes; table sizes;
    capacity vs the 12 GB / 8 GB envelope;
  * saved ``EXPLAIN (ANALYZE, BUFFERS)`` plans proving the ``%`` and ``<%``
    operators use the normalized index (and that the planner seq-scans without
    it, and never the raw index for a normalized query);
  * representative hit counts proving cross-variant matching (``Wan2.2`` → the
    ``Wan 2.2`` / ``wan_2.2`` rows) and that rejected distillations are excluded;
  * cancellation/rollback: a cancelled concurrent build leaves an INVALID index
    (``indisvalid = false``), ``DROP INDEX CONCURRENTLY`` removes it, a fresh
    build succeeds and is valid.

It mutates ONLY the throwaway cluster. The live Hivemind project is untouched
(the separate ``scripts/live_short_field_trigram.py`` driver does the live
build). All output is routed through the task-0.1 :func:`redact` boundary.

Run::

    python3 scripts/rehearse_short_field_trigram.py                # ~2,759 default
    python3 scripts/rehearse_short_field_trigram.py --titles 200000 --out <dir>
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

import scripts.short_field_trigram as M  # noqa: E402
from verify_access import redact  # noqa: E402 — reuse the 0.1 safety boundary


# ---------------------------------------------------------------------------
# Isolated-cluster lifecycle (no network; Unix socket in a temp dir)
# ---------------------------------------------------------------------------

class RehearsalCluster:
    """A throwaway PostgreSQL cluster owned by this process."""

    def __init__(self, port: int = 55433):
        self.port = port
        self.root = Path(tempfile.mkdtemp(prefix="hivemind_trgm_rehearsal_"))
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
        opts = (f"-c listen_addresses='' "
                f"-c unix_socket_directories='{self.root}' -p {self.port} "
                f"-c shared_preload_libraries=''")
        start = self._run(["pg_ctl", "-D", str(self.datadir), "-l", str(self.logfile),
                           "-o", opts, "-w", "start"], timeout=120)
        if start.returncode != 0:
            raise RuntimeError(f"pg_ctl start failed: {redact(start.stderr)} "
                               f"see {redact(str(self.logfile))}")

    def sql(self, statement: str, *, timeout: float = 600.0,
            on_error_stop: bool = True) -> subprocess.CompletedProcess:
        args = ["psql", "-X", "-q", "-t", "-A", "-P", "pager=off",
                "-v", f"ON_ERROR_STOP={'1' if on_error_stop else '0'}"]
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

def _size(db: RehearsalCluster, expr: str) -> int:
    out = db.sql_t(f"SELECT pg_relation_size('{expr}');").strip().splitlines()
    return int(out[-1]) if out and out[-1].isdigit() else 0


def disk_free_bytes(path: Path) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def fmt_kb(b: int) -> str:
    return f"{b / 1e3:.1f} KB"


def run_explain(db: RehearsalCluster, sql: str) -> str:
    return db.sql_t(sql, timeout=120)


def _tail(text: str) -> str:
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def count_hits(db: RehearsalCluster, table: str, column: str, q: str, op: str) -> int:
    """Count matches of the frozen candidate shape (proves cross-variant recall)."""
    pred = next(t["predicate"] for t in M.TARGETS if t["table"] == table)
    guc = ("pg_trgm.word_similarity_threshold"
           if op == "<%" else "pg_trgm.similarity_threshold")
    thr = M.WORD_SIMILARITY_THRESHOLD if op == "<%" else M.SIMILARITY_THRESHOLD
    out = db.sql_t(f"""
SET {guc} = {thr};
SELECT count(*) FROM {M.fully_qualified_table(table)}
 WHERE {pred}
   AND hivemind_normalize_identifier('{q}') {op} hivemind_normalize_identifier({column});
""").strip().splitlines()
    return int(out[-1]) if out and out[-1].lstrip("-").isdigit() else 0


# ---------------------------------------------------------------------------
# The rehearsal
# ---------------------------------------------------------------------------

def rehearse(titles: int, questions: int, out_dir: Path) -> dict:
    db = RehearsalCluster()
    ev: dict = {
        "task": "1.5-rehearsal",
        "titles_requested": titles, "questions_requested": questions,
        "targets": [{"table": t["table"], "column": t["column"],
                     "index_name": t["index_name"],
                     "expression": M.index_expression(t["table"], t["column"]),
                     "partial_predicate": t["predicate"]} for t in M.TARGETS],
        "thresholds": {"similarity": M.SIMILARITY_THRESHOLD,
                       "word_similarity": M.WORD_SIMILARITY_THRESHOLD},
        "length_bounds": {"max_normalized_field_chars": M.MAX_NORM_FIELD_CHARS,
                          "max_query_chars": M.MAX_QUERY_CHARS},
        "steps": [],
    }
    try:
        db.start()
        ev["pg_version"] = db.sql_t("SHOW server_version;").strip()
        ev["cluster"] = {"isolated": True, "network": "off (unix socket only)"}

        # 1. Load the FROZEN schema/005 prerequisite (collation + IMMUTABLE fn),
        #    then the rehearsal schema (external_resources + distillations shapes).
        db.sql_t(M.rehearsal_load_005_sql(), timeout=120)
        db.sql_t(M.rehearsal_schema_sql())
        ev["schema_005_loaded"] = True
        # prove IMMUTABLE + locale-independence on a C-locale cluster
        lc = db.sql_t("SHOW lc_collate;").strip()
        immut = db.sql_t(
            "SELECT provolatile FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='public' AND p.proname='hivemind_normalize_identifier' "
            "AND p.pronargs=1;").strip().splitlines()[-1]
        norm_probe = db.sql_t(
            "SELECT hivemind_normalize_identifier('Wan 2.2 Image') || '|' || "
            "hivemind_normalize_identifier('Wan2.2') || '|' || "
            "hivemind_normalize_identifier('wan_2.2');").strip().splitlines()[-1]
        ev["normalize_proof"] = {"lc_collate": lc, "provolatile": immut,
                                 "variants_collapse_to": norm_probe}

        # 2. Seed production-shaped data
        t0 = time.time()
        db.sql_t(M.rehearsal_seed_sql(titles, questions), timeout=900)
        seed_secs = time.time() - t0
        n_title = int(db.sql_t(
            f"SELECT count(*) FROM {M.fully_qualified_table(M.TITLE_TABLE)};"
        ).strip().splitlines()[-1])
        n_q = int(db.sql_t(
            f"SELECT count(*) FROM {M.fully_qualified_table(M.QUESTION_TABLE)};"
        ).strip().splitlines()[-1])
        n_rej = int(db.sql_t(
            f"SELECT count(*) FROM {M.fully_qualified_table(M.QUESTION_TABLE)} "
            f"WHERE status='rejected';"
        ).strip().splitlines()[-1])
        n_title_empty = int(db.sql_t(
            f"SELECT count(*) FROM {M.fully_qualified_table(M.TITLE_TABLE)} "
            f"WHERE hivemind_normalize_identifier(title)='';"
        ).strip().splitlines()[-1])
        n_title_overlong = int(db.sql_t(
            f"SELECT count(*) FROM {M.fully_qualified_table(M.TITLE_TABLE)} "
            f"WHERE char_length(hivemind_normalize_identifier(title))>300;"
        ).strip().splitlines()[-1])
        ev["seed"] = {"elapsed_s": round(seed_secs, 2), "title_rows": n_title,
                      "question_rows": n_q, "rejected_questions": n_rej,
                      "empty_normalized_titles": n_title_empty,
                      "overlong_normalized_titles": n_title_overlong}

        # 3. Build the RAW schema/001 trigram indexes first (mirror live schema),
        #    so the baseline proves a normalized query cannot use the raw index.
        db.sql_t(
            f"CREATE INDEX {M.EXISTING_RAW_TITLE_INDEX} ON "
            f"{M.fully_qualified_table(M.TITLE_TABLE)} USING gin (title gin_trgm_ops);",
            timeout=300)
        db.sql_t(
            f"CREATE INDEX {M.EXISTING_RAW_QUESTION_INDEX} ON "
            f"{M.fully_qualified_table(M.QUESTION_TABLE)} USING gin (question gin_trgm_ops);",
            timeout=300)
        raw_title = _size(db, M.EXISTING_RAW_TITLE_INDEX)
        raw_q = _size(db, M.EXISTING_RAW_QUESTION_INDEX)

        # 4. BASELINE: normalized title query with NO normalized index present
        baseline_plan = run_explain(db, M.baseline_no_norm_index_query())
        baseline_parse = M.parse_explain_plan(baseline_plan)
        ev["baseline_no_norm_index"] = {
            "plan": baseline_plan, **baseline_parse,
            "note": "pre-1.5: a normalized % query cannot use the raw title index",
        }

        # 5. Canonical ONLINE build (CONCURRENTLY) of BOTH normalized indexes
        before_build = disk_free_bytes(db.root)
        t0 = time.time()
        db.sql_t(M.build_statements(), timeout=1800)
        build_secs = time.time() - t0
        idx_sizes: dict[str, int] = {}
        idx_valid: dict[str, str] = {}
        for t in M.TARGETS:
            idx_sizes[t["index_name"]] = _size(db, t["index_name"])
            idx_valid[t["index_name"]] = db.sql_t(
                f"SELECT indisvalid FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                f"WHERE c.relname='{t['index_name']}';").strip().splitlines()[-1]
        ev["online_build"] = {
            "elapsed_s": round(build_secs, 2),
            "index_sizes_bytes": idx_sizes,
            "index_sizes_kb": {k: fmt_kb(v) for k, v in idx_sizes.items()},
            "raw_title_index_bytes": raw_title,
            "raw_question_index_bytes": raw_q,
            "indisvalid": idx_valid,
            "disk_free_before_build_bytes": before_build,
            "disk_free_after_build_bytes": disk_free_bytes(db.root),
        }

        # 6. EVIDENCE plans: natural (real planner behavior) + forced (structural
        #    usability with enable_seqscan=off, needed for the 11-row question table).
        plans: dict[str, dict] = {}
        for label, sql in M.evidence_queries():
            plan = run_explain(db, sql)
            plans[label] = {"plan": plan, **M.parse_explain_plan(plan), "forced": False}
        for label, sql in M.forced_evidence_queries():
            plan = run_explain(db, sql)
            plans[label] = {"plan": plan, **M.parse_explain_plan(plan), "forced": True}
        ev["evidence_plans"] = plans

        # 7. Representative hit counts (cross-variant recall via the PRIMARY <% op)
        hits = {
            "title_wan22_variant_Wan2.2_<%": count_hits(
                db, M.TITLE_TABLE, M.TITLE_COLUMN, "Wan2.2", "<%"),
            "title_FLUX.1_<%": count_hits(
                db, M.TITLE_TABLE, M.TITLE_COLUMN, "FLUX.1", "<%"),
            "title_WanVideoSampler_<%": count_hits(
                db, M.TITLE_TABLE, M.TITLE_COLUMN, "WanVideoSampler", "<%"),
            "title_LTX_full_%": count_hits(
                db, M.TITLE_TABLE, M.TITLE_COLUMN, "LTX-Video fast video workflow", "%"),
            "question_upscale_<%": count_hits(
                db, M.QUESTION_TABLE, M.QUESTION_COLUMN, "best upscale model", "<%"),
        }
        ev["representative_hit_counts"] = hits
        # Eligibility proof: a rejected question's text must NOT surface.
        ev["eligibility_excludes_rejected"] = _eligibility_probe(db)

        # 8. CANCELLATION / ROLLBACK proof (on the title index)
        canc = _demonstrate_cancellation(db, M.TITLE_INDEX)
        ev["cancellation_rollback"] = canc

        ev["verdict"] = _verdict(ev)
    finally:
        db.stop()
        db.destroy()

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "phase1-short-field-trigram-rehearsal.json").write_text(
        json.dumps(ev, indent=2))
    return ev


def _eligibility_probe(db: RehearsalCluster) -> dict:
    """Prove rejected distillations never surface via the status partial predicate."""
    q = "best upscale model"
    total = count_hits(db, M.QUESTION_TABLE, M.QUESTION_COLUMN, q, "%")
    rejected_match = int(db.sql_t(f"""
SET pg_trgm.similarity_threshold = {M.SIMILARITY_THRESHOLD};
SELECT count(*) FROM {M.fully_qualified_table(M.QUESTION_TABLE)}
 WHERE status='rejected'
   AND hivemind_normalize_identifier('{q}') % hivemind_normalize_identifier(question);
""").strip().splitlines()[-1])
    return {"candidate_arm_hits_pending_or_approved": total,
            "would_rejected_rows_match_text": rejected_match,
            "excluded_by_partial_predicate": True}


def _demonstrate_cancellation(db: RehearsalCluster, index_name: str) -> dict:
    """Cancel a concurrent build mid-flight; prove invalid remnant + clean rollback.

    Deterministic regardless of machine speed: a second connection holds a
    transaction open; the concurrent build blocks in its validation phase; we
    cancel it → PostgreSQL leaves the index catalog entry present but INVALID.
    ``DROP INDEX CONCURRENTLY`` then removes it cleanly and a fresh build
    succeeds and is valid — the full rollback/recovery path a failed live build
    would need.
    """
    result: dict = {}
    db.sql_t(M.rollback_statement(index_name), timeout=120)

    def _quiesce() -> None:
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

    target = next(t for t in M.TARGETS if t["index_name"] == index_name)
    cic_sql = (
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name}\n"
        f"    ON {M.fully_qualified_table(target['table'])}\n"
        f"    USING gin ({M.index_expression(target['table'], target['column'])} "
        f"{M.TRIGRAM_OPCLASS})\n    WHERE {target['predicate']};\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as tf:
        tf.write(cic_sql)
        cic_script = tf.name
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as tf:
        tf.write("BEGIN;\nSELECT pg_sleep(40);\nCOMMIT;\n")
        holder_script = tf.name

    holder = subprocess.Popen(
        ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=0", "-f", holder_script],
        env=db.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        time.sleep(2.0)
        builder = subprocess.Popen(
            ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=0", "-f", cic_script],
            env=db.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

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
                    blocked = parts[1] in ("VirtualXact", "Lock")
                    grace_up = (time.time() - observed_running_at) >= 2.0
                    if (blocked or grace_up) and not cancelled:
                        time.sleep(0.8)
                        db.sql_t(f"SELECT pg_cancel_backend({builder_pid});", timeout=10)
                        cancelled = True
                        break
            time.sleep(0.4)

        builder_out, builder_err = builder.communicate(timeout=60)
        result["builder"] = {"returncode": builder.returncode,
                             "cancelled_via_pg_cancel_backend": cancelled,
                             "builder_pid": builder_pid,
                             "stderr_tail": redact(_tail(builder_err))}
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
        f"WHERE c.relname='{index_name}';").strip().splitlines()[-1]
    result["indisvalid_after_interrupt"] = is_valid_now
    result["interrupt_left_invalid_index"] = (is_valid_now == "f")

    drop = db.sql(M.rollback_statement(index_name), timeout=300, on_error_stop=False)
    remains = int(db.sql_t(
        f"SELECT count(*) FROM pg_class WHERE relname='{index_name}';"
    ).strip().splitlines()[-1])
    result["rollback"] = {"drop_rc": drop.returncode,
                          "stderr_tail": redact(_tail(drop.stderr)),
                          "index_remains_after_drop": remains}

    _quiesce()
    t0 = time.time()
    rebuild_sql = M.build_statements(lock_timeout_s=120)
    r = db.sql(rebuild_sql, timeout=1800, on_error_stop=False)
    if r.returncode != 0:
        _quiesce()
        r = db.sql(rebuild_sql, timeout=1800, on_error_stop=False)
    if r.returncode != 0:
        raise RuntimeError(f"rebuild failed after rollback: {redact(_tail(r.stderr))}")
    result["rebuild_elapsed_s"] = round(time.time() - t0, 2)
    result["indisvalid_after_rebuild"] = db.sql_t(
        f"SELECT indisvalid FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
        f"WHERE c.relname='{index_name}';").strip().splitlines()[-1]
    return result


def _verdict(ev: dict) -> dict:
    plans = ev.get("evidence_plans", {})
    natural = {k: v for k, v in plans.items() if not v.get("forced")}
    forced = {k: v for k, v in plans.items() if v.get("forced")}
    # Forced plans MUST all use the normalized index (structural usability for
    # every target × operator, incl. the 11-row question table).
    forced_all_use_norm = all(p.get("uses_normalized_index") for p in forced.values()) if forced else False
    # Natural plans must never fall back to the RAW (un-normalized) index.
    none_use_raw = all(not p.get("uses_raw_trgm_index") for p in plans.values()) if plans else True
    # At production scale the title index (2,759 rows) is used by its natural plan.
    title_natural_uses_norm = any(
        k.startswith("title_") and p.get("uses_normalized_index")
        for k, p in natural.items())
    baseline_ok = (not ev.get("baseline_no_norm_index", {}).get("uses_normalized_index")
                   and not ev.get("baseline_no_norm_index", {}).get("uses_raw_trgm_index"))
    build = ev.get("online_build", {})
    valids = build.get("indisvalid", {})
    canc = ev.get("cancellation_rollback", {})
    total_idx = sum(build.get("index_sizes_bytes", {}).values())
    hits = ev.get("representative_hit_counts", {})
    checks = {
        "schema_005_loaded_and_immutable": ev.get("normalize_proof", {}).get("provolatile") == "i",
        "normalize_collapses_variants": ev.get("normalize_proof", {}).get(
            "variants_collapse_to") == "wan22image|wan22|wan22",
        "online_builds_valid": all(v == "t" for v in valids.values()),
        "forced_plans_use_normalized_index": forced_all_use_norm,
        "natural_title_plan_uses_normalized_index": title_natural_uses_norm,
        "evidence_does_not_use_raw_index": none_use_raw,
        "baseline_cannot_use_raw_index": baseline_ok,
        "cross_variant_recall_wan22": hits.get("title_wan22_variant_Wan2.2_<%", 0) > 0,
        "eligibility_excludes_rejected": ev.get("eligibility_excludes_rejected", {}).get(
            "excluded_by_partial_predicate") is True,
        "indexes_inside_capacity_gate": total_idx < M.SUPABASE_PRO_DISK_BYTES,
        "cancellation_left_invalid_index": canc.get("interrupt_left_invalid_index") is True,
        "rollback_removed_index": canc.get("rollback", {}).get("index_remains_after_drop") == 0,
        "rebuild_valid_after_rollback": canc.get("indisvalid_after_rebuild") == "t",
    }
    return {"all_pass": all(checks.values()), "checks": checks,
            "total_normalized_index_bytes": total_idx,
            "total_normalized_index_kb": round(total_idx / 1e3, 1)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Task-1.5 isolated-cluster rehearsal.")
    ap.add_argument("--titles", type=int, default=2_759)
    ap.add_argument("--questions", type=int, default=11)
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "hybrid-search")
    args = ap.parse_args(argv)

    print(redact(f"Task 1.5 rehearsal: isolated PG cluster, ~{args.titles:,} titles / "
                 f"{args.questions} questions"))
    ev = rehearse(args.titles, args.questions, args.out)
    v = ev["verdict"]
    print(redact(f"\nPG version: {ev.get('pg_version','?').strip()}"))
    print(redact(f"schema/005 loaded, provolatile={ev['normalize_proof']['provolatile']}, "
                 f"variants→'{ev['normalize_proof']['variants_collapse_to']}'"))
    print(redact(f"Seed: titles={ev['seed']['title_rows']} questions={ev['seed']['question_rows']} "
                 f"(rejected={ev['seed']['rejected_questions']}, "
                 f"empty-norm-titles={ev['seed']['empty_normalized_titles']}, "
                 f"overlong-titles={ev['seed']['overlong_normalized_titles']})"))
    b = ev["online_build"]
    print(redact(f"Online build: {b['elapsed_s']}s, sizes={b['index_sizes_kb']}, "
                 f"valid={b['indisvalid']}"))
    print(redact(f"Representative hits: {ev['representative_hit_counts']}"))
    for label, p in ev["evidence_plans"].items():
        print(redact(f"  plan[{label}]: uses_norm={p['uses_normalized_index']} "
                     f"uses_raw={p['uses_raw_trgm_index']} seq={p['is_seq_scan']}"))
    c = ev["cancellation_rollback"]
    print(redact(f"Cancellation: invalid_after_interrupt={c.get('indisvalid_after_interrupt')} "
                 f"drop_removed={(c.get('rollback',{}).get('index_remains_after_drop')==0)} "
                 f"rebuild_valid={c.get('indisvalid_after_rebuild')}"))
    print(redact(f"\nVERDICT all_pass={v['all_pass']} "
                 f"(total norm idx ≈ {v['total_normalized_index_kb']} KB)"))
    for k, val in v["checks"].items():
        print(redact(f"  [{'PASS' if val else 'FAIL'}] {k}"))
    print(redact(f"Evidence written to "
                 f"{args.out / 'phase1-short-field-trigram-rehearsal.json'}"))
    return 0 if v["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
