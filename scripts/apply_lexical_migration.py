#!/usr/bin/env python3
"""Serial, preflight-gated apply of an additive lexical migration to production.

Operator-only. Reuses the session-mode access path (``derive_pg_env``). Requires a GREEN
preflight unless ``--skip-preflight``. Applies <migration> as ``SET ROLE postgres`` with
bounded lock/statement timeouts via ``psql -f ON_ERROR_STOP=1``. Reports rc + a redacted
error tail + an optional read-only ``--post-check-sql`` confirmation. Idempotent migrations
(``CREATE OR REPLACE FUNCTION``, ``REVOKE``) are safe to re-run. This is the single,
auditable verb for serialized Phase-1 production applies (schema/010 latency, 011 security).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from live_lexical_search import derive_pg_env, elevate, psql, run_preflight  # noqa: E402
from verify_access import redact  # noqa: E402

PRELUDE = (
    "SET ROLE postgres;\n"
    "set lock_timeout='30s';\n"
    "set statement_timeout='300s';\n"
)


def _tail(s: str) -> str:
    lines = [ln for ln in (s or "").strip().splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Serial preflight-gated apply of a lexical migration.")
    p.add_argument("--migration", required=True, help="Migration .sql path (repo-relative or absolute).")
    p.add_argument("--label", required=True, help="Short label for the result JSON.")
    p.add_argument("--skip-preflight", action="store_true", help="Skip the green-preflight gate (not recommended).")
    p.add_argument("--post-check-sql", default=None, help="Optional read-only SQL to confirm after apply.")
    args = p.parse_args(argv)

    mpath = pathlib.Path(args.migration)
    if not mpath.is_absolute():
        mpath = REPO / mpath
    if not mpath.exists():
        print(json.dumps({"ok": False, "error": f"migration not found: {mpath}"}))
        return 2

    env, host, port = derive_pg_env()
    rep: dict = {"label": args.label, "migration": str(mpath),
                 "host_family": "pooler" if "pooler" in host else "session", "port": port}

    if not args.skip_preflight:
        pf = run_preflight(env, host, port)
        rep["preflight_green"] = pf.get("green")
        if not pf.get("green"):
            rep["error"] = "preflight not green; aborting (no mutation applied)"
            rep["preflight"] = pf
            print(json.dumps(rep, indent=2, default=str))
            return 1

    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as tf:
        tf.write(PRELUDE + mpath.read_text(encoding="utf-8"))
        tmp = pathlib.Path(tf.name)
    try:
        proc = subprocess.run(
            ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", str(tmp)],
            env=env, capture_output=True, text=True, timeout=360,
        )
    finally:
        tmp.unlink(missing_ok=True)

    rep["apply_rc"] = proc.returncode
    rep["ok"] = proc.returncode == 0
    if proc.returncode != 0:
        rep["stderr_tail"] = redact(_tail(proc.stderr))
    elif args.post_check_sql:
        r = psql(env, elevate(args.post_check_sql), timeout=60, on_error_stop=False)
        rep["post_check_rc"] = r.returncode
        rep["post_check_out"] = (r.stdout or "").strip().splitlines()[:5]

    print(json.dumps(rep, indent=2, default=str))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
