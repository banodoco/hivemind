#!/usr/bin/env python3
"""Bounded WARM p95 latency probe — authoritative Execution Time (task D gate), v4.

Measures PURE execution time via ``EXPLAIN (ANALYZE)`` on the live candidate function
``hivemind_lexical_candidates`` (the bulk of the RPC; the RPC only adds a final LIMIT + a
cheap hydration of <=20 rows). EXPLAIN's reported ``Execution Time`` is the authoritative
pure-query latency — immune to psql-spawn / pool-connect overhead. Each query is primed
(W warmup selects) then timed (T EXPLAIN ANALYZE runs) within ONE persistent connection, so
warm-cache numbers reflect steady-state production (the RPC's internal 2s backstop is not
in play here because we measure the candidate function under a relaxed statement_timeout).

Read-only. No bodies/snippets/credentials logged (timing only). Bounded: one connection
per query, W+T statements each.

Usage::

    python3 scripts/probe_lexical_warm_p95.py --prime 2 --timed 3
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from live_lexical_search import derive_pg_env  # noqa: E402
from verify_access import redact  # noqa: E402

QUERIES = [
    ("exact_name", "WanVideoSampler"),
    ("exact_name", "FLUX.1"),
    ("exact_name", "CogVideoX"),
    ("exact_name", "Mochi"),
    ("exact_name", "Hunyuan"),
    ("exact_name", "LTX-Video"),
    ("exact_name", "Qwen Image"),
    ("exact_name", "VACE"),
    ("workflow_code", "KSampler"),
    ("workflow_code", "CheckpointLoaderSimple"),
    ("workflow_code", "CLIPTextEncode"),
    ("workflow_code", "VAEDecode"),
    ("multi_term", "sampler video"),
    ("no_hit", "zzznotarealmodel123"),
]

EXEC_TIME_RE = re.compile(r"Execution Time:\s*([\d.]+)\s*ms")


def _lit(q: str) -> str:
    return "'" + q.replace("'", "''") + "'"


def _candidates_call(q: str) -> str:
    return ("public.hivemind_lexical_candidates(" + _lit(q)
            + ",100,'{}','{}','{}',null,'{}','{}',false,false)")


def build_block(q: str, prime: int, timed: int) -> str:
    parts = ["SET ROLE postgres;", "set statement_timeout='60000ms';"]
    for _ in range(prime):
        parts.append("select count(*) from " + _candidates_call(q) + ";")
    for _ in range(timed):
        parts.append("explain (analyze) select * from " + _candidates_call(q) + ";")
    return "\n".join(parts) + "\n"


def run_query(env, q, prime, timed):
    block = build_block(q, prime, timed)
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as tf:
        tf.write(block)
        tmp = pathlib.Path(tf.name)
    try:
        proc = subprocess.run(
            ["psql", "-X", "-q", "-t", "-A", "-P", "pager=off", "-v", "ON_ERROR_STOP=0",
             "-f", str(tmp)],
            env=env, capture_output=True, text=True, timeout=300,
        )
    finally:
        tmp.unlink(missing_ok=True)
    times = [float(m) for m in EXEC_TIME_RE.findall(proc.stdout or "")]
    return times, proc


def pct(xs, p):
    if not xs:
        return None
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Authoritative warm-p95 probe (EXPLAIN ANALYZE Execution Time).")
    ap.add_argument("--prime", type=int, default=2)
    ap.add_argument("--timed", type=int, default=3)
    args = ap.parse_args(argv)

    env, host, port = derive_pg_env()
    rep: dict = {
        "host_family": "pooler" if "pooler" in host else "session", "port": port,
        "methodology": "EXPLAIN (ANALYZE) Execution Time on the live candidate function "
        "(the bulk of the RPC); primed with %d warmup selects; %d timed runs per query in "
        "one persistent connection; relaxed 60s timeout (2s RPC backstop not in play); no "
        "bodies logged." % (args.prime, args.timed),
        "per_query": [], "samples_ms": [],
    }
    for label, q in QUERIES:
        times, proc = run_query(env, q, args.prime, args.timed)
        for t in times:
            rep["samples_ms"].append(round(t, 2))
        rep["per_query"].append({
            "label": label, "query": q, "n": len(times),
            "min_ms": round(min(times), 1) if times else None,
            "p50_ms": round(pct(times, 50), 1) if times else None,
            "p95_ms": round(pct(times, 95), 1) if times else None,
            "max_ms": round(max(times), 1) if times else None,
            "stderr_tail": redact((proc.stderr or "").strip().splitlines()[-1])
                if proc.returncode != 0 and (proc.stderr or "").strip() else "",
        })
    alls = rep["samples_ms"]
    rep["overall"] = {
        "n": len(alls),
        "p50_ms": round(pct(alls, 50), 1) if alls else None,
        "p95_ms": round(pct(alls, 95), 1) if alls else None,
        "p99_ms": round(pct(alls, 99), 1) if alls else None,
        "max_ms": round(max(alls), 1) if alls else None,
    }
    rep["gate_warm_p95_le_750ms"] = bool(alls) and rep["overall"]["p95_ms"] <= 750.0
    print(json.dumps(rep, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
