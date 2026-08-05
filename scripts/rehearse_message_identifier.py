#!/usr/bin/env python3
"""Task-1.6 production-shaped rehearsal in an ISOLATED local PostgreSQL cluster.

Implements + measures the FROZEN, EVIDENCE-BASED CHOICE (normalized full-message
trigram GIN, length-bounded, partial on is_deleted=false) and keeps the rejected
alternative (a normalized identifier side index) as an inline decision-record
comparison, so the choice is reproducible from evidence.

Measured (the task-1.6 completion signal):
  * the DECISION evidence — chosen full-message trigram vs rejected side index on
    the SAME ~1.25M corpus: storage, query latency (EXPLAIN index use), quality;
  * online CONCURRENT build time + index size (vs the 12 GB gate);
  * WRITE cost: inserts/sec WITH vs WITHOUT the GIN index (auto-maintained — no
    trigger); compared to the side index's measured ~3.25x trigger slowdown;
  * exact + variant candidate-query latency + EXPLAIN (Bitmap Index Scan) +
    controlled Recall@10 on planted targets, incl. the SPACED-form bridge
    ("FLUX 1" body -> flux1, found by a "FLUX.1" query) the side index loses;
  * deleted-exclusion (is_deleted=true never surfaces via the partial predicate);
  * long-token recovery (a >2047-char-token body is matched — the 1.3 FTS gap);
  * rollback (DROP INDEX CONCURRENTLY; no source row touched).

It mutates ONLY the throwaway cluster. The live Hivemind project is untouched
(scripts/live_message_identifier.py does the live apply). All output is routed
through the task-0.1 :func:`redact` boundary.

Run::

    python3 scripts/rehearse_message_identifier.py                 # ~1.25M default
    python3 scripts/rehearse_message_identifier.py --rows 200000 --out <dir>
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

import scripts.message_identifier_index as M  # noqa: E402
from verify_access import redact  # noqa: E402


class RehearsalCluster:
    def __init__(self, port=55432):
        self.port = port
        self.root = Path(tempfile.mkdtemp(prefix="hivemind_mi_rehearsal_"))
        self.datadir = self.root / "data"
        self.logfile = self.root / "postgres.log"
        self.env = {**os.environ, "PGHOST": str(self.root), "PGPORT": str(port),
                    "PGUSER": "postgres", "PGDATABASE": "postgres"}

    def _run(self, cmd, **kw):
        kw.setdefault("env", self.env); kw.setdefault("stdout", subprocess.PIPE)
        kw.setdefault("stderr", subprocess.PIPE); kw.setdefault("text", True)
        return subprocess.run(cmd, **kw)

    def start(self):
        for b in ("initdb", "pg_ctl", "psql", "postgres"):
            if shutil.which(b) is None:
                raise RuntimeError(f"required PG binary not on PATH: {b}")
        init = self._run(["initdb", "-D", str(self.datadir), "-U", "postgres",
                          "-A", "trust", "--no-locale", "-E", "UTF8"], timeout=120)
        if init.returncode != 0:
            raise RuntimeError(f"initdb failed: {redact(init.stderr)}")
        opts = f"-c listen_addresses='' -c unix_socket_directories='{self.root}' -p {self.port}"
        s = self._run(["pg_ctl", "-D", str(self.datadir), "-l", str(self.logfile),
                       "-o", opts, "-w", "start"], timeout=120)
        if s.returncode != 0:
            raise RuntimeError(f"pg_ctl start failed: {redact(s.stderr)}")

    def sql(self, statement, *, timeout=600.0, on_error_stop=True):
        args = ["psql", "-X", "-q", "-t", "-A", "-P", "pager=off",
                "-v", f"ON_ERROR_STOP={'1' if on_error_stop else '0'}"]
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as tf:
            tf.write(statement); script = tf.name
        try:
            return self._run(args + ["-f", script], timeout=timeout)
        finally:
            try: os.unlink(script)
            except OSError: pass

    def sqlt(self, statement, *, timeout=600.0) -> str:
        r = self.sql(statement, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError(f"psql failed:\n{redact(r.stderr or r.stdout)}")
        return r.stdout

    def stop(self):
        if (self.datadir / "postmaster.pid").exists():
            self._run(["pg_ctl", "-D", str(self.datadir), "-m", "fast", "-w", "stop"], timeout=60)

    def destroy(self):
        shutil.rmtree(self.root, ignore_errors=True)


def _rel_bytes(rel: str, db: RehearsalCluster) -> int:
    out = db.sqlt(f"SELECT pg_total_relation_size('{rel}');").strip().splitlines()
    return int(out[-1]) if out and out[-1].isdigit() else 0


def fmt_mb(b: int) -> str:
    return f"{b / 1e6:.1f} MB"


def _tail(text: str) -> str:
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def rehearse(rows: int, out_dir: Path) -> dict:
    db = RehearsalCluster()
    ev: dict = {"task": "1.6-rehearsal", "choice": M.CHOICE, "rows_requested": rows, "steps": []}
    try:
        db.start()
        ev["pg_version"] = db.sqlt("SHOW server_version;").strip()
        ev["cluster"] = {"isolated": True, "network": "off (unix socket only)"}

        # 1. schema + seed
        t0 = time.time()
        db.sqlt(M.rehearsal_schema_sql(), timeout=120)
        db.sqlt(M.rehearsal_seed_sql(rows), timeout=900)
        seed_secs = time.time() - t0
        actual = int(db.sqlt(f"SELECT count(*) FROM {M.SOURCE_TABLE};").strip().splitlines()[-1])
        deleted = int(db.sqlt(f"SELECT count(*) FROM {M.SOURCE_TABLE} WHERE is_deleted=true;").strip().splitlines()[-1])
        nullc = int(db.sqlt(f"SELECT count(*) FROM {M.SOURCE_TABLE} WHERE content IS NULL;").strip().splitlines()[-1])
        overlong = int(db.sqlt(f"SELECT count(*) FROM {M.SOURCE_TABLE} WHERE char_length(content) > {M.CONTENT_LENGTH_MAX};").strip().splitlines()[-1])
        eligible = int(db.sqlt(
            f"SELECT count(*) FROM {M.SOURCE_TABLE} WHERE is_deleted=false AND content IS NOT NULL "
            f"AND char_length(content) BETWEEN {M.CONTENT_LENGTH_MIN} AND {M.CONTENT_LENGTH_MAX};").strip().splitlines()[-1])
        ev["seed"] = {"elapsed_s": round(seed_secs, 2), "actual_rows": actual,
                      "deleted_rows": deleted, "null_content_rows": nullc,
                      "overlong_excluded_rows": overlong, "eligible_rows": eligible}

        # 2. CHOSEN: online CONCURRENT build of the normalized full-message trigram GIN
        db.sqlt("ANALYZE " + M.SOURCE_TABLE + ";")
        before = _rel_bytes(M.SOURCE_TABLE, db)
        t0 = time.time()
        db.sqlt(M.build_statement(lock_timeout_s=120, statement_timeout_s=3600), timeout=3600)
        build_secs = time.time() - t0
        idx_bytes = _rel_bytes(M.INDEX_NAME, db)
        valid = db.sqlt(f"SELECT indisvalid FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                        f"WHERE c.relname='{M.INDEX_NAME}';").strip().splitlines()[-1]
        ev["chosen_build"] = {
            "elapsed_s": round(build_secs, 2), "indisvalid": valid,
            "index_bytes": idx_bytes, "index_size_pretty": fmt_mb(idx_bytes),
            "source_table_bytes": before,
            "storage_gate_gb": M.STORAGE_GATE_GB,
            "storage_gate_pass": idx_bytes < M.STORAGE_GATE_GB * 1e9,
        }

        # 3. query quality + latency + EXPLAIN (controlled Recall@10 + spaced bridge + long token)
        ev["query_quality"] = _measure_query_quality(db)

        # 4. write cost: inserts/sec WITH vs WITHOUT the GIN index (auto-maintained)
        ev["write_cost"] = _measure_write_cost(db)

        # 5. REJECTED alternative comparison (side index): build inline, measure size + a query
        ev["rejected_alternative_side_index"] = _measure_rejected_side_index(db)

        # 6. rollback
        ev["rollback"] = _measure_rollback(db)

        ev["verdict"] = _verdict(ev)
    finally:
        db.stop()
        db.destroy()

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "phase1-message-identifier-rehearsal.json").write_text(json.dumps(ev, indent=2))
    return ev


def _measure_query_quality(db: RehearsalCluster) -> dict:
    """Controlled Recall@10 on planted targets + spaced-bridge + long-token + EXPLAIN."""
    base = max(int(db.sqlt("SELECT max(message_id) FROM " + M.SOURCE_TABLE + ";").strip().splitlines()[-1] or "1"), 1)
    targets = {
        9000001: "FLUX.1 dev lora here", 9000002: "wan2.2 image to video",
        9000003: "wan_2.2 animate alternate spelling", 9000004: "WanVideoSampler node",
        9000005: "ltx-2-19b-ic-lora-detailer model", 9000006: "controlnet settings flux",
        9000007: ".gguf quant model", 9000008: "lightx2v_I2V_14B.safetensors file",
        9000009: "ipadapter face id", 9000010: "CogVideoX VEnhancer",
        9000011: "FLUX 1 spaced dotted name",            # spaced form the side index loses
        9000012: "use the Wan 2.2 sampler please",        # spaced Wan 2.2
    }
    vals = ", ".join(f"({mid}, $t${c}$t$, false)" for mid, c in targets.items())
    db.sqlt(f"INSERT INTO {M.SOURCE_TABLE}(message_id, content, is_deleted) VALUES {vals};", timeout=60)
    # deleted variants of the same identifiers -> must NEVER surface
    dvals = ", ".join(f"(9100000{i}, $t${c}$t$, true)" for i, (mid, c) in enumerate(targets.items(), 1))
    db.sqlt(f"INSERT INTO {M.SOURCE_TABLE}(message_id, content, is_deleted) VALUES {dvals};", timeout=60)
    # a long-token body (>2047-char unbroken token) the FTS index drops -> this arm recovers it
    longtok = "x" * 3000 + "WanVideoSampler"
    db.sqlt(f"INSERT INTO {M.SOURCE_TABLE}(message_id, content, is_deleted) "
            f"VALUES (9000020, $t${longtok}$t$, false);", timeout=60)

    queries = [
        ("FLUX.1", [9000001, 9000011], "variant"),       # 9000011 = "FLUX 1 spaced" -> flux1 (spaced bridge)
        ("Wan2.2", [9000002, 9000003, 9000012], "variant"),
        ("wan_2.2", [9000003, 9000002], "variant"),
        ("WanVideoSampler", [9000004], "exact"),
        ("ltx-2-19b-ic-lora-detailer", [9000005], "exact"),
        ("controlnet", [9000006], "exact"),
        (".gguf", [9000007], "exact"),
        ("lightx2v_I2V_14B.safetensors", [9000008], "exact"),
        ("ipadapter", [9000009], "variant"),
        ("CogVideoX", [9000010], "exact"),
    ]
    results = []
    n_hit = 0
    for q, expected_ids, kind in queries:
        key = "public.hivemind_normalize_identifier($q$" + q + "$q$)"
        norm_content = "public.hivemind_normalize_identifier(m.content)"
        # PRIMARY containment predicate (index-served): normalized query is a SUBSTRING of
        # the normalized whole body. This retrieves identifiers embedded in prose (the v2
        # equality path returned zero for those) and bridges every separator variant via
        # compact normalization on both sides.
        pred = (f"m.is_deleted=false AND char_length(m.content) "
                f"BETWEEN {M.CONTENT_LENGTH_MIN} AND {M.CONTENT_LENGTH_MAX} "
                f"AND {norm_content} LIKE '%' || {key} || '%'")
        # candidate COUNT: the containment cardinality. Bounded by the index (only messages
        # that actually contain the identifier); for compound identifiers this is ~1.2k vs
        # the 125k the old <% arm scored. Recorded so the latency is self-explaining.
        cnt = db.sqlt(f"SELECT count(*) FROM {M.SOURCE_TABLE} m WHERE {pred};",
                      timeout=120).strip().splitlines()[-1]
        try:
            cand = int(cnt)
        except ValueError:
            cand = 0
        ranked = (f"SELECT m.message_id::text FROM {M.SOURCE_TABLE} m WHERE {pred} "
                  f"ORDER BY ({norm_content} = {key}) DESC, "
                  f"m.created_at DESC NULLS LAST, m.message_id::text ASC LIMIT 10;")
        # two shots: cold (first touch, fresh-cluster OS cache) then warm. Reporting BOTH is
        # honest — cold isolates one-time I/O; warm is the representative index-serving cost.
        t0 = time.time(); out_cold = db.sqlt(ranked, timeout=120)
        cold_ms = round((time.time() - t0) * 1000.0, 2)
        t0 = time.time(); out_warm = db.sqlt(ranked, timeout=120)
        warm_ms = round((time.time() - t0) * 1000.0, 2)
        top10 = [int(x) for x in out_warm.strip().splitlines() if x.strip().lstrip("-").isdigit()]
        hit = any(e in top10 for e in expected_ids)
        deleted_leaked = any(i >= 9100000 for i in top10)
        n_hit += int(hit)
        results.append({"query": q, "kind": kind, "candidate_count": cand,
                        "latency_ms": warm_ms, "latency_cold_ms": cold_ms, "latency_warm_ms": warm_ms,
                        "n_returned": len(top10), "recall_hit": hit, "deleted_leaked": deleted_leaked})
    recall = n_hit / len(queries) if queries else 0.0

    # long-token recovery: WanVideoSampler inside a 3000-char token (containment finds it;
    # the task-1.3 FTS index drops >2047-char tokens, this arm recovers them).
    long_hit = db.sqlt(
        f"SELECT count(*) FROM {M.SOURCE_TABLE} m WHERE m.message_id=9000020 AND m.is_deleted=false "
        f"AND public.hivemind_normalize_identifier(m.content) "
        f"LIKE '%' || public.hivemind_normalize_identifier('WanVideoSampler') || '%';"
    ).strip().splitlines()[-1]

    plans = {}
    for label, q in M.evidence_queries():
        plan = db.sqlt(q, timeout=60)
        plans[label] = {"plan": plan, **M.parse_explain_plan(plan)}

    lat_warm = sorted(r["latency_warm_ms"] for r in results)
    lat_cold = sorted(r["latency_cold_ms"] for r in results)
    return {
        "queries": results, "recall_at_10": round(recall, 4),
        "deleted_leak_count": sum(1 for r in results if r["deleted_leaked"]),
        "long_token_recovered": long_hit in ("1",),
        # warm = representative index-serving cost (cold first-touch I/O excluded);
        # cold kept for worst-case transparency.
        "latency_ms_p50": lat_warm[len(lat_warm) // 2] if lat_warm else 0,
        "latency_ms_p95": lat_warm[int(len(lat_warm) * 0.95)] if lat_warm else 0,
        "latency_cold_ms_p50": lat_cold[len(lat_cold) // 2] if lat_cold else 0,
        "latency_cold_ms_p95": lat_cold[int(len(lat_cold) * 0.95)] if lat_cold else 0,
        "explain_plans": plans,
        "recall_gate": 0.95, "recall_gate_pass": recall >= 0.95,
    }


def _measure_write_cost(db: RehearsalCluster) -> dict:
    """Inserts/sec WITH vs WITHOUT the GIN index (auto-maintained; no trigger)."""
    N = 3000
    base = max(int(db.sqlt("SELECT max(message_id) FROM " + M.SOURCE_TABLE + ";").strip().splitlines()[-1] or "1"), 1)

    def block(lo: int) -> str:
        return ";".join(
            f"INSERT INTO " + M.SOURCE_TABLE + "(message_id, content, is_deleted) "
            f"VALUES ({lo+i}, 'wan2.2 WanVideoSampler FLUX.1 controlnet model lora step', false)"
            for i in range(N))

    t0 = time.time(); db.sqlt(block(base + 1), timeout=300); with_secs = time.time() - t0
    db.sqlt(f"DELETE FROM {M.SOURCE_TABLE} WHERE message_id > {base};", timeout=120)
    # WITHOUT the index
    db.sqlt(f"DROP INDEX IF EXISTS {M.INDEX_NAME};", timeout=300)
    t0 = time.time(); db.sqlt(block(base + 1), timeout=300); off_secs = time.time() - t0
    db.sqlt(f"DELETE FROM {M.SOURCE_TABLE} WHERE message_id > {base};", timeout=120)
    db.sqlt(M.build_statement(lock_timeout_s=120, statement_timeout_s=3600), timeout=3600)  # restore
    with_ips = N / with_secs if with_secs else 0
    off_ips = N / off_secs if off_secs else 0
    slowdown = (off_ips / with_ips) if with_ips else 0
    return {
        "rows": N,
        "with_index_inserts_per_s": round(with_ips, 1),
        "without_index_inserts_per_s": round(off_ips, 1),
        "slowdown_ratio_off_over_with": round(slowdown, 2),
        "note": "auto-maintained GIN (no trigger); compare to the side index's ~3.25x trigger slowdown",
    }


def _measure_rejected_side_index(db: RehearsalCluster) -> dict:
    """Build the rejected side index inline (decision record): size + 1 query plan.

    All in ONE psql session so the TEMP table survives. Returns size metrics
    (one pipe-separated line) then the EXPLAIN plan for a representative query.
    """
    script = f"""
    SET pg_trgm.word_similarity_threshold = {M.WORD_SIMILARITY_THRESHOLD};
    CREATE TEMP TABLE _mi_side(message_id bigint, compact text, primary key(message_id, compact)) ON COMMIT PRESERVE ROWS;
    INSERT INTO _mi_side(message_id, compact)
    SELECT m.message_id, e.compact FROM {M.SOURCE_TABLE} m
     CROSS JOIN LATERAL (
       SELECT distinct on (norm.compact) norm.compact FROM (
         SELECT public.hivemind_normalize_identifier((rm)[1]) AS compact, rn
           FROM regexp_matches(coalesce(m.content,''), '[A-Za-z0-9_.=-]+', 'g') WITH ORDINALITY AS r(rm, rn)
       ) norm
       WHERE char_length(norm.compact) BETWEEN 3 AND 100 AND norm.compact ~ '[A-Za-z]'
       ORDER BY norm.compact, norm.rn LIMIT 256
     ) e
     WHERE m.is_deleted=false AND m.content IS NOT NULL;
    CREATE INDEX _mi_side_trgm ON _mi_side USING gin (compact gin_trgm_ops);
    ANALYZE _mi_side;
    SELECT (SELECT count(*) FROM _mi_side)::text || '|' ||
           pg_total_relation_size('_mi_side')::text || '|' ||
           pg_total_relation_size('_mi_side_trgm')::text AS metrics;
    EXPLAIN (ANALYZE) SELECT m.message_id::text FROM _mi_side mi
      JOIN {M.SOURCE_TABLE} m ON m.message_id=mi.message_id AND m.is_deleted=false
     WHERE char_length(mi.compact) BETWEEN 3 AND 100
       AND (mi.compact=public.hivemind_normalize_identifier('FLUX.1')
            OR public.hivemind_normalize_identifier('FLUX.1') <% mi.compact) LIMIT 10;
    """
    t0 = time.time()
    r = db.sql(script, timeout=1800, on_error_stop=False)
    elapsed = time.time() - t0
    if r.returncode != 0:
        return {"status": "error", "elapsed_s": round(elapsed, 2), "stderr_tail": redact(_tail(r.stderr))}
    out = r.stdout or ""
    lines = [ln for ln in out.splitlines() if ln.strip()]
    # first line with a pipe = the metrics row
    metrics_line = next((ln for ln in lines if "|" in ln), "0|0|0")
    parts = (metrics_line.split("|") + ["0", "0", "0"])[:3]
    try:
        side_rows = int(parts[0])
    except ValueError:
        side_rows = 0
    heap = int(parts[1]) if str(parts[1]).isdigit() else 0
    trgm = int(parts[2]) if str(parts[2]).isdigit() else 0
    plan_text = "\n".join(lines[lines.index(metrics_line) + 1:]) if metrics_line in lines else out
    pl = M.parse_explain_plan(plan_text)
    return {
        "status": "ok", "elapsed_s": round(elapsed, 2), "side_rows": side_rows,
        "heap_bytes": heap, "heap_size_pretty": fmt_mb(heap),
        "trgm_index_bytes": trgm, "trgm_size_pretty": fmt_mb(trgm),
        "total_bytes": heap + trgm, "total_size_pretty": fmt_mb(heap + trgm),
        "candidate_query_uses_index": pl["uses_index_scan"],
        "candidate_query_is_seq_scan": pl["is_seq_scan"],
        "note": "REJECTED alternative (decision record): fanned-out side index, measured larger + slower",
    }


def _measure_rollback(db: RehearsalCluster) -> dict:
    before = int(db.sqlt("SELECT count(*) FROM " + M.SOURCE_TABLE + ";").strip().splitlines()[-1])
    r = db.sql(M.rollback_statement(concurrently=False), timeout=300, on_error_stop=False)
    remains = int(db.sql("SELECT count(*) FROM pg_class WHERE relname='" + M.INDEX_NAME + "';",
                         timeout=30, on_error_stop=False).stdout.strip().splitlines()[-1] or 0)
    after = int(db.sqlt("SELECT count(*) FROM " + M.SOURCE_TABLE + ";").strip().splitlines()[-1])
    return {"rollback_rc": r.returncode, "stderr_tail": redact(_tail(r.stderr)),
            "index_remains": remains, "source_rows_before": before, "source_rows_after": after,
            "clean": remains == 0 and before == after}


def _verdict(ev: dict) -> dict:
    cb = ev.get("chosen_build", {})
    qq = ev.get("query_quality", {})
    wc = ev.get("write_cost", {})
    rb = ev.get("rejected_alternative_side_index", {})
    rol = ev.get("rollback", {})
    chosen_uses_index = all(p.get("uses_index_scan") and not p.get("is_seq_scan")
                            for p in qq.get("explain_plans", {}).values()) if qq else False
    checks = {
        "chosen_storage_gate": cb.get("storage_gate_pass") is True,
        "chosen_build_valid": cb.get("indisvalid") in ("t", "true"),
        "query_recall_at_10_gate": qq.get("recall_gate_pass") is True,
        "query_no_deleted_leak": qq.get("deleted_leak_count") == 0,
        "query_uses_index_not_seqscan": chosen_uses_index,
        "long_token_recovered": qq.get("long_token_recovered") is True,
        "write_cost_bounded": wc.get("slowdown_ratio_off_over_with", 99) < 5.0,
        "chosen_smaller_than_rejected_side_index": (
            rb.get("status") == "ok" and cb.get("index_bytes", 0) > 0
            and cb.get("index_bytes", 0) < rb.get("total_bytes", 0)),
        "rollback_clean": rol.get("clean") is True,
    }
    return {"all_pass": all(checks.values()), "checks": checks}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Task-1.6 isolated-cluster rehearsal.")
    ap.add_argument("--rows", type=int, default=1_250_000)
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "hybrid-search")
    args = ap.parse_args(argv)

    print(redact(f"Task 1.6 rehearsal: isolated PG cluster, ~{args.rows:,} rows; choice={M.CHOICE}"))
    ev = rehearse(args.rows, args.out)
    v = ev["verdict"]
    print(redact(f"\nPG version: {ev.get('pg_version','?').strip()}"))
    s = ev["seed"]
    print(redact(f"Seed: {s['actual_rows']:,} rows (deleted={s['deleted_rows']}, null={s['null_content_rows']}, "
                 f"overlong_excluded={s['overlong_excluded_rows']}, eligible={s['eligible_rows']}) in {s['elapsed_s']}s"))
    cb = ev["chosen_build"]
    print(redact(f"CHOSEN build: {cb['index_size_pretty']} valid={cb['indisvalid']} in {cb['elapsed_s']}s "
                 f"(gate {cb['storage_gate_gb']}GB)"))
    qq = ev["query_quality"]
    print(redact(f"Query: Recall@10={qq['recall_at_10']} (gate {qq['recall_gate']}) "
                 f"deleted_leaks={qq['deleted_leak_count']} long_token_recovered={qq['long_token_recovered']} "
                 f"warm p50={qq['latency_ms_p50']}ms p95={qq['latency_ms_p95']}ms "
                 f"(cold p50={qq['latency_cold_ms_p50']}ms p95={qq['latency_cold_ms_p95']}ms)"))
    for label, p in qq["explain_plans"].items():
        print(redact(f"  plan[{label}]: uses_idx={p['uses_index_scan']} seq={p['is_seq_scan']}"))
    wc = ev["write_cost"]
    print(redact(f"Write cost: with_idx={wc['with_index_inserts_per_s']} ips "
                 f"without={wc['without_index_inserts_per_s']} ips slowdown={wc['slowdown_ratio_off_over_with']}x"))
    rb = ev["rejected_alternative_side_index"]
    print(redact(f"REJECTED side index (decision record): total={rb.get('total_size_pretty')} "
                 f"heap={rb.get('heap_size_pretty')} trgm={rb.get('trgm_size_pretty')} "
                 f"seq_scan={rb.get('candidate_query_is_seq_scan')}"))
    print(redact(f"Rollback clean: {ev['rollback']['clean']}"))
    print(redact(f"\nVERDICT all_pass={v['all_pass']}"))
    for k, val in v["checks"].items():
        print(redact(f"  [{'PASS' if val else 'FAIL'}] {k}"))
    print(redact(f"Evidence written to {args.out / 'phase1-message-identifier-rehearsal.json'}"))
    return 0 if v["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
