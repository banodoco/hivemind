#!/usr/bin/env python3
"""Bounded single-query latency + plan probe for the live lexical RPC (task D investigation).

Read-only. Reuses the SAME session-mode production access path as the other Phase-1 live
drivers (``derive_pg_env`` -> short-lived CLI-login libpq env held only in a child-process
env; output routed through ``redact``). Times a handful of representative exact-name /
workflow-code queries against the production RPC envelope ``{results,count,meta}`` and
captures ``EXPLAIN (ANALYZE, BUFFERS)`` on the internal candidate function to locate slow
arms. No message bodies, snippets, or credentials are logged.

Usage::

    python3 scripts/probe_lexical_latency.py                 # timed RPC smoke + 1 plan
    python3 scripts/probe_lexical_latency.py --explain-only  # plan only (no RPC timing)
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from live_lexical_search import derive_pg_env, elevate, psql  # noqa: E402
from verify_access import redact  # noqa: E402

# Representative of the golden set (G001-G014 exact-name + a workflow-code token).
QUERIES = [
    ("exact_name", "WanVideoSampler"),
    ("exact_name", "FLUX.1"),
    ("exact_name", "CogVideoX"),
    ("exact_name", "Mochi"),
    ("workflow_code", "KSampler"),
]


def _lit(q: str) -> str:
    return "'" + q.replace("'", "''") + "'"


def time_rpc(env: dict, label: str, q: str) -> dict:
    """Time one production RPC call; read count from response->>'count' (envelope-safe)."""
    sql = elevate(
        "select (j->>'count')::int, jsonb_array_length(j->'results') from "
        "(select public.hivemind_lexical_search(" + _lit(q) + ",20,"
        "'{}','{}','{}',null,'{}','{}','lexical') j) s;"
    )
    t0 = time.perf_counter()
    r = psql(env, sql, timeout=40.0, on_error_stop=False)
    dt = (time.perf_counter() - t0) * 1000.0
    out: dict = {"label": label, "query": q, "latency_ms": round(dt, 1), "rc": r.returncode}
    if r.returncode == 0:
        line = (r.stdout.strip().splitlines() or ["|"])[0]
        cnt, _, n = line.partition("|")
        out["count"] = cnt.strip()
        out["results_len"] = n.strip()
    else:
        err = (r.stderr.strip().splitlines() or [""])
        out["error_tail"] = redact(err[-1] if err else "")
    return out


def explain_candidates(env: dict, q: str) -> dict:
    """EXPLAIN (ANALYZE, BUFFERS) on the internal candidate function with a relaxed timeout."""
    sql = elevate(
        "set local statement_timeout='60000ms'; "
        "explain (analyze, buffers, format text) "
        "select * from public.hivemind_lexical_candidates(" + _lit(q) + ",100,"
        "'{}','{}','{}',null,'{}','{}',false,false);"
    )
    t0 = time.perf_counter()
    r = psql(env, sql, timeout=90.0, on_error_stop=False)
    dt = (time.perf_counter() - t0) * 1000.0
    plan = r.stdout or ""
    lines = plan.strip().splitlines()
    return {
        "query": q,
        "rc": r.returncode,
        "wall_ms": round(dt, 1),
        "plan_lines": lines,
        "plan_tail": redact("\n".join(lines[-30:])),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Bounded live lexical latency probe (read-only).")
    p.add_argument("--explain-only", action="store_true")
    args = p.parse_args(argv)

    env, host, port = derive_pg_env()
    rep: dict = {"host_family": "pooler" if "pooler" in host else "session", "port": port}

    if not args.explain_only:
        rep["rpc_timing"] = [time_rpc(env, lbl, q) for lbl, q in QUERIES]

    rep["explain_candidates"] = explain_candidates(env, QUERIES[0][1])
    print(json.dumps(rep, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
