#!/usr/bin/env python3
"""Golden-workload warm latency + plan-shape probe — SINGLE persistent session.

FAILURE-PRESERVING (task 1.11 gate instrument): on a per-statement timeout or a
subprocess timeout it STILL emits a valid, secret-safe JSON report built from the
PARTIAL stdout that survived. psql query output is streamed to a raw temp file
(``-o``) so per-case progress (CASE markers + ``Execution Time`` lines) is durable
even when the session is killed mid-run. Nothing about message bodies, matched
snippets, workflow source, credentials, or raw query strings is ever emitted;
only opaque case ids, categories, filters, per-case timing/timeout state, and
(optionally) sanitized plan node/index names.

Two modes:

  * ``latency`` (default) — prime then time each case via
    ``EXPLAIN (ANALYZE)`` ``Execution Time`` on the live candidate function with
    the case's filters. Per-statement cap is ``--statement-timeout-ms``
    (gate default 120000ms; diagnosis uses <=5000ms so all 112 finish or time out
    in a bounded window).

  * ``explain`` — capture a NON-ANALYZE plan shape (planning only, no execution)
    per selected case and summarize it to node/relation/index names + cost/row
    estimates, with every Filter:/Cond: literal redacted.

Usage::

    # Frozen gate (unchanged methodology, now failure-preserving):
    python3 scripts/probe_golden_latency.py --prime 1 --timed 1

    # Cheap read-only diagnosis (<=5s per statement, no prime):
    python3 scripts/probe_golden_latency.py --prime 0 --timed 1 \\
        --statement-timeout-ms 5000 --probe-timeout 1200

    # Plan shapes for representative cases (planning only, read-only):
    python3 scripts/probe_golden_latency.py --mode explain \\
        --cases g_workflow_code_x,g_exact_name_y
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import time

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from live_lexical_search import derive_pg_env  # noqa: E402
from verify_access import redact  # noqa: E402

GOLDEN = REPO / "eval" / "retrieval" / "golden" / "golden-v1.json"

# A case marker line in raw psql output looks like: CASE<tab><opaque id>.
MARKER_PREFIX = "CASE\t"
# A per-case stderr marker written via ``\warn`` BEFORE each case's work, so a
# per-statement ERROR on stderr can be attributed to its opaque case id while
# ``ON_ERROR_STOP=0`` keeps the session running. The marker carries ONLY the
# opaque id — never any case payload — so it is safe to parse back in memory.
ERR_MARKER_PREFIX = "ERRCASE:"
EXEC_TIME_RE = re.compile(r"Execution Time:\s*([\d.]+)\s*ms")
# The literal substring psql emits when a statement is canceled for timeout.
STMT_TIMEOUT_NEEDLE = "canceling statement due to statement timeout"

# Plan-shape sanitization: keep only known node-operation lines, plus the
# relation/index name and numeric cost/row estimates on them. Every other line
# (Filter:/Index Cond:/Recheck Cond:/Sort Key:/Output:/Buffers:/... and any
# literal-bearing line) is dropped, so query/needle strings never leak. Relation
# & index names are schema objects (allowed "plan node/index names"), not content.
_PLAN_NODE_HEAD = re.compile(
    r"^(Seq Scan|Index Scan|Index Only Scan|Bitmap Index Scan|Bitmap Heap Scan|"
    r"BitmapAnd|BitmapOr|Nested Loop|Hash Join|Merge Join|Incremental Sort|Sort|"
    r"GroupAggregate|HashAggregate|Aggregate|Limit|Unique|Materialize|Memoize|"
    r"Gather Merge|Gather|Merge Append|Append|Result|Subquery Scan|CTE Scan|"
    r"Function Scan|ProjectSet|HashSetOp|SetOp|Recursive Union|LockRows|"
    r"Foreign Scan|Custom Scan|Tid Scan|Tid Range Scan|Sample Scan)\b"
)
_PLAN_COST_RE = re.compile(r"\(cost=([^)]*)\)")
_PLAN_ROWS_RE = re.compile(r"rows=(\d+)")
_PLAN_USING_RE = re.compile(r"\busing\s+(\w+)")
_PLAN_ON_RE = re.compile(r"\bon\s+(public\.\w+|\w+)")
_PLAN_ARROW_RE = re.compile(r"^\s*(?:->\s*)?")


def _lit(q: str) -> str:
    return "E'" + q.replace("'", "''").replace("\\", "\\\\") + "'"


def _sql_array(vals) -> str:
    vals = vals or []
    if not vals:
        return "'{}'"
    return "array[" + ",".join(_lit(str(v)) for v in vals) + "]::text[]"


def _candidates_call(query: str, filters: dict) -> str:
    filters = filters or {}
    kinds = _sql_array(filters.get("kinds"))
    sources = _sql_array(filters.get("sources"))
    item_ids = _sql_array(filters.get("item_ids"))
    channels = _sql_array(filters.get("channels"))
    authors = _sql_array(filters.get("authors"))
    since = "null" if not filters.get("since") else _lit(str(filters["since"]))
    return ("public.hivemind_lexical_candidates(" + _lit(query) + ",100,"
            + ",".join([kinds, sources, item_ids, since, channels, authors])
            + ",false,false)")


def build_latency_script(cases, prime, timed, statement_timeout_ms) -> str:
    parts = [
        "SET ROLE postgres;",
        f"set statement_timeout='{int(statement_timeout_ms)}ms';",
        "set lock_timeout='2000ms';",
    ]
    for c in cases:
        call = _candidates_call(c["query"], c.get("filters") or {})
        # \warn -> stderr, so a per-statement ERROR below is attributed to this
        # opaque id while ON_ERROR_STOP=0 keeps the session running.
        parts.append("\\warn " + ERR_MARKER_PREFIX + str(c.get("id")))
        parts.append("select '" + MARKER_PREFIX + str(c.get("id")) + "';")
        for _ in range(max(0, prime)):
            parts.append("select count(*) from " + call + ";")
        for _ in range(max(0, timed)):
            parts.append("explain (analyze) select * from " + call + ";")
    return "\n".join(parts) + "\n"


def build_explain_script(cases, statement_timeout_ms) -> str:
    parts = [
        "SET ROLE postgres;",
        f"set statement_timeout='{int(statement_timeout_ms)}ms';",
        "set lock_timeout='2000ms';",
    ]
    for c in cases:
        call = _candidates_call(c["query"], c.get("filters") or {})
        parts.append("\\warn " + ERR_MARKER_PREFIX + str(c.get("id")))
        parts.append("select '" + MARKER_PREFIX + str(c.get("id")) + "';")
        parts.append("explain (costs on) select * from " + call + ";")
    return "\n".join(parts) + "\n"


def _pct(xs, p):
    if not xs:
        return None
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def parse_latency(raw: str, cases, stderr_text: str = ""):
    """Map raw psql stdout/stderr -> per-case timing/status. Robust to mid-run kills.

    Returns (per_case, last_started_id). ``per_case`` is aligned to ``cases``;
    each entry is ``{"id", "status", "time_ms"}`` with status in
    {measured, started_not_measured, not_reached}, plus ``error_kind`` when an
    error was attributed. Markers arrive in case order (the script emits them
    so), so we advance a sequence pointer and also keep an id->index map as a
    guard. For ``timed``>1 the case's measured time is the mean of its
    ``Execution Time`` lines.

    ``stderr_text`` is parsed ONLY in memory: an ``ERRCASE:<opaque-id>`` line
    selects the current case, and while selected a line carrying the statement
    cancellation substring sets ``statement_timeout`` while any other ``ERROR:``
    line sets ``statement_error``. No stderr line is ever returned or retained.
    """
    recs = [{"id": str(c.get("id")), "reached": False, "times": [],
             "error_kind": None} for c in cases]
    pos: dict[str, int] = {}
    for i, c in enumerate(cases):
        cid = str(c.get("id"))
        pos.setdefault(cid, i)
    last_started = None
    pending = None  # case index whose timed statements we are currently inside
    next_seq = 0
    for line in (raw or "").splitlines():
        if line.startswith(MARKER_PREFIX):
            cid = line[len(MARKER_PREFIX):].strip()
            if next_seq < len(cases) and str(cases[next_seq].get("id")) == cid:
                idx = next_seq
                next_seq += 1
            else:
                idx = pos.get(cid)
            if idx is not None:
                recs[idx]["reached"] = True
                pending = idx
                last_started = cid
            else:
                pending = None
            continue
        m = EXEC_TIME_RE.search(line)
        if m and pending is not None:
            try:
                recs[pending]["times"].append(float(m.group(1)))
            except ValueError:
                pass

    # stderr is parsed purely in memory to attribute per-statement errors to
    # their opaque case id; the stderr text itself is never retained.
    sel = None
    for line in (stderr_text or "").splitlines():
        if line.startswith(ERR_MARKER_PREFIX):
            sel = pos.get(line[len(ERR_MARKER_PREFIX):].strip())
            continue
        if sel is None or recs[sel]["error_kind"]:
            # Only the FIRST error while selected is attributed.
            continue
        if STMT_TIMEOUT_NEEDLE in line:
            recs[sel]["error_kind"] = "statement_timeout"
        elif "ERROR:" in line:
            recs[sel]["error_kind"] = "statement_error"

    per_case = []
    for r in recs:
        # Status precedence: error > measured > started_not_measured > not_reached.
        if r["error_kind"]:
            status, t = r["error_kind"], None
        elif r["times"]:
            status, t = "measured", round(sum(r["times"]) / len(r["times"]), 3)
        elif r["reached"]:
            status, t = "started_not_measured", None
        else:
            status, t = "not_reached", None
        row = {"id": r["id"], "status": status, "time_ms": t}
        if r["error_kind"]:
            row["error_kind"] = r["error_kind"]
        per_case.append(row)
    return per_case, last_started


def summarize_plan(raw_block: str) -> list[dict]:
    """Reduce a NON-ANALYZE EXPLAIN block to sanitized node/relation/index rows.

    Keeps only recognized node-operation lines; on each, records the node op, the
    relation and/or index name, and the numeric cost/row estimate. Every literal
    line (Filter:/Index Cond:/Recheck Cond:/Sort Key:/Output:/Buffers:/...) is
    dropped, so query/needle strings never leak.
    """
    rows: list[dict] = []
    for line in (raw_block or "").splitlines():
        if not line.strip():
            continue
        # Drop the EXPLAIN tree indent + "-> " arrow so the node op is at start.
        stripped = _PLAN_ARROW_RE.sub("", line, count=1)
        # A real plan node always carries a cost annotation (COSTS ON). Property
        # lines like "Sort Key:", "Hash Cond:", "Filter:", "Recheck Cond:" never
        # do — this gate drops them (and any literal they carry) cleanly.
        if not _PLAN_COST_RE.search(stripped):
            continue
        head = re.split(r"\s+(?:on|using)\s+|\s*\(", stripped, maxsplit=1)[0].strip()
        if not _PLAN_NODE_HEAD.match(head):
            continue
        entry: dict = {"node": head}
        mi = _PLAN_USING_RE.search(stripped)
        if mi:
            entry["index"] = mi.group(1)
        mo = _PLAN_ON_RE.search(stripped)
        if mo:
            tok = mo.group(1)
            if head.lower().startswith("bitmap index scan"):
                entry["index"] = tok
            else:
                entry["relation"] = tok
        mc = _PLAN_COST_RE.search(stripped)
        if mc:
            entry["cost"] = mc.group(1).strip()
        mr = _PLAN_ROWS_RE.search(stripped)
        if mr:
            entry["est_rows"] = int(mr.group(1))
        rows.append(entry)
    return rows


def parse_explain(raw: str, cases):
    """Map raw psql output -> per-case sanitized plan shape."""
    by_id = {str(c.get("id")): c for c in cases}
    cur_id = None
    block_lines: list[str] = []
    out: dict[str, list[dict]] = {}
    for line in (raw or "").splitlines():
        if line.startswith(MARKER_PREFIX):
            # flush previous block
            if cur_id is not None:
                out[cur_id] = summarize_plan("\n".join(block_lines))
            cur_id = line[len(MARKER_PREFIX):].strip()
            block_lines = []
            continue
        if cur_id is not None:
            block_lines.append(line)
    if cur_id is not None:
        out[cur_id] = summarize_plan("\n".join(block_lines))
    return [{"id": str(c.get("id")), "plan": out.get(str(c.get("id")), [])} for c in cases]


def _per_case_public(per_case, cases):
    """Public per-case view: opaque id + categories + status + timing (+ error_kind).

    Filters, query strings, and any payload are intentionally OMITTED
    (redacted-by-omission). Only error statuses carry ``error_kind``.
    """
    pub = []
    for c, pc in zip(cases, per_case):
        row = {
            "id": str(c.get("id")),
            "categories": list(c.get("categories") or []),
            "status": pc["status"],
            "time_ms": (round(pc["time_ms"], 1) if pc["time_ms"] is not None else None),
        }
        if pc.get("error_kind"):
            row["error_kind"] = pc["error_kind"]
        pub.append(row)
    return pub


def build_latency_report(cases, per_case, last_started, *, prime, timed,
                         statement_timeout_ms, host_family, port,
                         python_timeout_fired, psql_rc, elapsed_s, stderr_tail=""):
    """Build the secret-safe latency report.

    ``stderr_tail`` is accepted for backwards compatibility but is IGNORED: stderr
    is parsed for attribution inside ``parse_latency`` and is never serialized.
    """
    latencies = [pc["time_ms"] for pc in per_case if pc["time_ms"] is not None]
    n_measured = len(latencies)
    missing = [pc["id"] for pc in per_case if pc["time_ms"] is None]
    over750 = [pc["id"] for pc in per_case if (pc["time_ms"] or 0) > 750.0]
    status_counts: dict[str, int] = {}
    for pc in per_case:
        status_counts[pc["status"]] = status_counts.get(pc["status"], 0) + 1
    rep = {
        "mode": "latency",
        "golden_path": str(GOLDEN),
        "n_cases": len(cases),
        "n_measured": n_measured,
        "missing_case_ids": missing,
        "last_started_case_id": last_started,
        "python_timeout_fired": bool(python_timeout_fired),
        "psql_rc": psql_rc,
        "elapsed_s": round(elapsed_s, 1) if elapsed_s is not None else None,
        "host_family": host_family,
        "port": port,
        "methodology": (
            "single persistent psql session (one auth); per-case EXPLAIN "
            f"(ANALYZE) Execution Time on candidate fn WITH case filters; "
            f"primed {prime}; {timed} timed; statement_timeout="
            f"{int(statement_timeout_ms)}ms; no bodies/snippets/query-strings "
            "logged."),
        "status_counts": status_counts,
        "p50_ms": round(_pct(latencies, 50), 1) if latencies else None,
        "p95_ms": round(_pct(latencies, 95), 1) if latencies else None,
        "p99_ms": round(_pct(latencies, 99), 1) if latencies else None,
        "max_ms": round(max(latencies), 1) if latencies else None,
        "gate_warm_p95_le_750ms": bool(latencies) and (_pct(latencies, 95) or 0) <= 750.0,
        "gate_all_measured": (n_measured == len(cases)),
        "cases_over_750ms": len(over750),
        "over_750ms_case_ids": over750,
        "per_case": _per_case_public(per_case, cases),
    }
    return rep


def run_psql_streaming(script: str, env: dict, probe_timeout: float):
    """Run psql -f with query output streamed to a raw temp file (durable on kill).

    Returns (raw_text, stderr_text, psql_rc, python_timeout_fired).
    """
    with tempfile.TemporaryDirectory(prefix="hm_probe_") as td:
        tdpath = pathlib.Path(td)
        scriptfile = tdpath / "probe.sql"
        rawfile = tdpath / "out.txt"
        errfile = tdpath / "err.txt"
        scriptfile.write_text(script)
        cmd = ["psql", "-X", "-q", "-t", "-A", "-P", "pager=off",
               "-v", "ON_ERROR_STOP=0", "-o", str(rawfile), "-f", str(scriptfile)]
        with open(errfile, "w") as errfd:
            python_timeout_fired = False
            psql_rc = None
            try:
                proc = subprocess.run(
                    cmd, env=env, stdout=subprocess.DEVNULL, stderr=errfd,
                    text=True, timeout=probe_timeout,
                )
                psql_rc = proc.returncode
            except subprocess.TimeoutExpired:
                python_timeout_fired = True
                psql_rc = None
            raw_text = rawfile.read_text(errors="replace") if rawfile.exists() else ""
            stderr_text = errfile.read_text(errors="replace") if errfile.exists() else ""
        return raw_text, stderr_text, psql_rc, python_timeout_fired


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Golden warm-p95 / plan-shape probe "
                                             "(failure-preserving, secret-safe).")
    ap.add_argument("--prime", type=int, default=1)
    ap.add_argument("--timed", type=int, default=1)
    ap.add_argument("--statement-timeout-ms", type=int, default=120000,
                    help="per-statement cap (gate default 120000; diagnosis <=5000)")
    ap.add_argument("--probe-timeout", type=float, default=1800.0,
                    help="outer subprocess budget (seconds)")
    ap.add_argument("--mode", choices=("latency", "explain"), default="latency")
    ap.add_argument("--cases", default="",
                    help="comma-separated opaque case ids to run (default: all)")
    ap.add_argument("--out", default="", help="write JSON report to this path too")
    args = ap.parse_args(argv)

    all_cases = json.loads(GOLDEN.read_text())["cases"]
    if args.cases:
        wanted = {c.strip() for c in args.cases.split(",") if c.strip()}
        cases = [c for c in all_cases if str(c.get("id")) in wanted]
    else:
        cases = all_cases

    env, host, port = derive_pg_env()
    host_family = "pooler" if "pooler" in (host or "") else "session"

    if args.mode == "explain":
        script = build_explain_script(cases, args.statement_timeout_ms)
        t0 = time.monotonic()
        raw, stderr_text, psql_rc, timeout_fired = run_psql_streaming(
            script, env, args.probe_timeout)
        elapsed = time.monotonic() - t0
        per_case = parse_explain(raw, cases)
        rep = {
            "mode": "explain",
            "n_cases": len(cases),
            "golden_path": str(GOLDEN),
            "host_family": host_family,
            "port": port,
            "methodology": ("single persistent psql session; NON-ANALYZE EXPLAIN "
                            "(planning only, read-only); node/relation/index names "
                            "+ cost/row only; all Filter/Cond literals redacted."),
            "python_timeout_fired": bool(timeout_fired),
            "psql_rc": psql_rc,
            "elapsed_s": round(elapsed, 1),
            "per_case_plan_shape": per_case,
        }
        # stderr is intentionally discarded: it never serializes into the report.
    else:
        script = build_latency_script(
            cases, args.prime, args.timed, args.statement_timeout_ms)
        t0 = time.monotonic()
        raw, stderr_text, psql_rc, timeout_fired = run_psql_streaming(
            script, env, args.probe_timeout)
        elapsed = time.monotonic() - t0
        per_case, last_started = parse_latency(raw, cases, stderr_text)
        rep = build_latency_report(
            cases, per_case, last_started, prime=args.prime, timed=args.timed,
            statement_timeout_ms=args.statement_timeout_ms, host_family=host_family,
            port=port, python_timeout_fired=timeout_fired, psql_rc=psql_rc,
            elapsed_s=elapsed, stderr_tail=stderr_text[-1500:])

    blob = json.dumps(rep, indent=2, default=str)
    # Final secret-safety net: redact the whole serialized report before output.
    print(redact(blob))
    if args.out:
        pathlib.Path(args.out).write_text(redact(blob) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
