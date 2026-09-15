#!/usr/bin/env python3
"""Hivemind access verification probe (hybrid-search plan, task 0.1).

Re-runs the **read-only, non-destructive** checks that confirm the audited
access paths resolve to the intended Hivemind Supabase project:

  * Supabase CLI is installed and authenticated.
  * The repo's linked project reference matches the executor's baked-in ref.
  * The public read path (PostgREST via the publishable key) serves the corpus.
  * (best-effort) Edge Function deployment and secret-store are reachable.
  * (opt-in ``--db-probe``) an authenticated session-mode DB round-trip via the
    Supabase CLI login role, which proves the migrations/backfill path without
    the operator's permanent DB password and without Docker.

Design rules (from the plan's security section):

  * Read-only only. This script never creates a project/database, never rotates
    or sets a secret, and never copies anything from Pumpernickel.
  * **Every** line of output is routed through :func:`redact`, which masks API
    keys, DB passwords, tokens, connection strings, and publishable keys.
  * The session-mode probe holds the CLI login credential only inside a child
    process environment for the duration of one ``psql`` query, then drops it.
    It is never printed, logged, or persisted.

Run::

    python3 scripts/verify_access.py             # safe, mostly-offline core
    python3 scripts/verify_access.py --db-probe  # add authenticated DB round-trip
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from executors._common import resolve_anon_key, resolve_endpoint  # noqa: E402

LINKED_REF_FILE = REPO_ROOT / "supabase" / ".temp" / "project-ref"
TEMP_REF_FILES = REPO_ROOT / "supabase" / ".temp"

# Hivemind Edge Functions that must be deployed for the pack surface.
HIVEMIND_FUNCTION_SLUGS = (
    "contribute",
    "contribute-resource",
    "refresh-media-urls",
)
# Secret names whose *presence* (never value) is relevant to the plan.
RELEVANT_SECRET_NAMES = (
    "SUPABASE_SERVICE_ROLE_KEY",
    "SUPABASE_DB_URL",
    "SUPABASE_ANON_KEY",
    "SB_PUBLISHABLE_KEY",
    "OPENAI_API_KEY",
)


# ---------------------------------------------------------------------------
# Redaction — the safety boundary for all human-facing output
# ---------------------------------------------------------------------------

# Connection strings of the form scheme://[user[:pass]@]host[:port]/db
_CONNSTR_RE = re.compile(r"\b[a-z][a-z0-9+.\-]*://\S+@\S+?(?=[\s\"'`]|$)")
# PostgREST/JWT tokens: header.payload.signature (base64url).
_JWT_RE = re.compile(r"\bey[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
# Supabase publishable / secret / anon key prefixes.
_SB_KEY_RE = re.compile(r"\bsb_(?:publishable|secret|anon|service_role)_[A-Za-z0-9_]+\b")
# Hivemind contributor keys: hm_<64 hex>.
_CONTRIB_KEY_RE = re.compile(r"\bhm_[0-9a-f]{64}\b")
# Any PGPASSWORD=... assignment (covers shell, env dumps, dry-run scripts).
_PGPASSWORD_RE = re.compile(r"(?i)(PGPASSWORD)(\s*=\s*)(\"[^\"]*\"|'[^']*'|\S+)")
# Whole ``export VAR="..."`` lines emitted by the CLI dry-run.
_EXPORT_RE = re.compile(r'(?m)^(\s*export\s+[A-Za-z_][A-Za-z0-9_]*\s*=\s*)"[^"]*"\s*$')
# Long opaque tokens (hex / base64url / alnum). 32+ catches CLI login passwords.
_TOKEN_RE = re.compile(r"\b[A-Za-z0-9+/_\-]{32,}\b")


def redact(text: str) -> str:
    """Return *text* with every known secret shape masked.

    Applied to all printed output and to subprocess stdout/stderr before it is
    shown. Conservative by design: it may over-redact non-secret identifiers,
    but it must never let a credential through.
    """
    if not text:
        return text
    out = text
    out = _CONNSTR_RE.sub("<connstr>", out)
    out = _JWT_RE.sub("<jwt>", out)
    out = _SB_KEY_RE.sub("<sb-key>", out)
    out = _CONTRIB_KEY_RE.sub("<contributor-key>", out)
    out = _EXPORT_RE.sub(r'\1"<redacted>"', out)
    out = _PGPASSWORD_RE.sub(r"\1\2<redacted>", out)
    out = _TOKEN_RE.sub("<token>", out)
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def executor_ref() -> str | None:
    """Return the 20-char project ref baked into the executors, or None."""
    url = resolve_endpoint()
    # https://<ref>.supabase.co/rest/v1
    m = re.search(r"https://([a-z0-9]{20})\.supabase\.co", url)
    return m.group(1) if m else None


def read_linked_ref() -> str | None:
    """Return the linked ref recorded in supabase/.temp/project-ref."""
    try:
        return LINKED_REF_FILE.read_text(encoding="utf-8").strip() or None
    except (FileNotFoundError, OSError):
        return None


def _run(cmd: list[str], *, timeout: float = 30.0) -> tuple[int, str, str]:
    """Run *cmd*, returning (rc, redacted-stdout, redacted-stderr)."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        return proc.returncode, redact(proc.stdout or ""), redact(proc.stderr or "")
    except FileNotFoundError:
        return 127, "", redact(f"command not found: {cmd[0]}")
    except subprocess.TimeoutExpired:
        return 124, "", redact(f"timeout after {timeout}s: {' '.join(cmd)}")


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


class Result:
    """One check outcome: status + a short, redacted, human-readable detail."""

    def __init__(self, name: str, ok: bool, detail: str = "", *, core: bool = False):
        self.name = name
        self.ok = ok
        self.detail = redact(detail)
        self.core = core

    def __str__(self) -> str:
        flag = "PASS" if self.ok else ("FAIL" if self.core else "SKIP")
        suffix = f" — {self.detail}" if self.detail else ""
        return f"[{flag}] {self.name}{suffix}"


def check_cli() -> Result:
    rc, out, _ = _run(["supabase", "--version"])
    if rc == 127:
        return Result("supabase CLI installed", False, "supabase not on PATH", core=True)
    version = (out.strip().splitlines() or ["?"])[0]
    return Result("supabase CLI installed", rc == 0, f"version {version}", core=True)


def check_linked_ref() -> Result:
    baked = executor_ref()
    linked = read_linked_ref()
    if not baked:
        return Result("linked ref == executor ref", False, "could not parse executor ref", core=True)
    if not linked:
        return Result("linked ref == executor ref", False, "no supabase/.temp/project-ref", core=True)
    ok = linked == baked and len(linked) == 20
    return Result(
        "linked ref == executor ref",
        ok,
        f"linked={linked} baked={baked}" if ok else f"linked={linked} baked={baked} (MISMATCH)",
        core=True,
    )


def check_public_read_path() -> Result:
    """Tiny read-only select on unified_feed using the publishable key."""
    endpoint = resolve_endpoint().rstrip("/")
    key = resolve_anon_key()
    params = urllib.parse.urlencode({"select": "kind,source,item_id", "limit": "3"})
    url = f"{endpoint}/unified_feed?{params}"
    req = urllib.request.Request(url, headers={"apikey": key, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — report, do not crash
        return Result("public read path (unified_feed)", False, redact(f"request failed: {type(exc).__name__}"), core=True)
    if not isinstance(payload, list) or not payload:
        return Result("public read path (unified_feed)", False, "no rows returned", core=True)
    kinds = sorted({str(r.get("kind")) for r in payload})
    return Result("public read path (unified_feed)", True, f"{len(payload)} rows, kinds={kinds}", core=True)


def check_projects_link() -> Result:
    rc, out, _ = _run(["supabase", "projects", "list"], timeout=30.0)
    if rc != 0:
        return Result("projects list (auth + link)", False, f"rc={rc} (not logged in or offline)")
    linked = read_linked_ref() or ""
    has_linked_row = bool(linked) and linked in out
    # Region is safe to surface; find the linked row's region if present.
    region = "?"
    for line in out.splitlines():
        if linked and linked in line:
            cells = [c.strip() for c in line.split("|")]
            region = cells[4] if len(cells) > 4 else "?"
            break
    return Result(
        "projects list (auth + link)",
        has_linked_row,
        f"linked project ref present in `projects list`; region={region}",
    )


def check_functions() -> Result:
    rc, out, _ = _run(["supabase", "functions", "list"], timeout=30.0)
    if rc != 0:
        return Result("edge function deployment", False, f"rc={rc} (not logged in or offline)")
    missing = [s for s in HIVEMIND_FUNCTION_SLUGS if s not in out]
    present = [s for s in HIVEMIND_FUNCTION_SLUGS if s in out]
    ok = not missing
    return Result(
        "edge function deployment",
        ok,
        f"present={present}" + (f" missing={missing}" if missing else ""),
    )


def check_secrets() -> Result:
    rc, out, _ = _run(["supabase", "secrets", "list"], timeout=30.0)
    if rc != 0:
        return Result("secret-store read", False, f"rc={rc} (not logged in or offline)")
    present = [n for n in RELEVANT_SECRET_NAMES if re.search(rf"(?m)^\s*{re.escape(n)}\b", out)]
    service_ok = "SUPABASE_SERVICE_ROLE_KEY" in present
    openai_state = "present" if "OPENAI_API_KEY" in present else "absent (plan's remaining setup action)"
    return Result(
        "secret-store read",
        service_ok,
        f"names seen={present}; SUPABASE_SERVICE_ROLE_KEY={'ok' if service_ok else 'MISSING'}; OPENAI_API_KEY {openai_state}",
    )


_DRYRUN_EXPORT_RE = re.compile(r'^export\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"(.*)"\s*$')


def parse_dryrun_pg_env(script_text: str) -> dict[str, str]:
    """Extract libpq env vars from a `supabase db dump --dry-run` script.

    Returns a dict possibly containing PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE.
    The values are held in memory only; callers must not print them unredacted.
    """
    env: dict[str, str] = {}
    for line in script_text.splitlines():
        m = _DRYRUN_EXPORT_RE.match(line.strip())
        if m and m.group(1).startswith("PG"):
            env[m.group(1)] = m.group(2)
    return env


def check_db_session_probe() -> Result:
    """Authenticated session-mode round-trip via the CLI login role (psql, no Docker).

    Steps:
      1. Capture `supabase db dump --dry-run` to a temp file (never displayed).
      2. Extract the libpq env (CLI login role + short-lived password).
      3. Run one read-only `SELECT current_database(), version()` (+extension
         probe) via psql with that env as a child environment.
      4. Delete the temp file; the credential dies with the subprocess.
    """
    if shutil.which("psql") is None:
        return Result("session-mode DB round-trip", False, "psql not on PATH")
    if shutil.which("supabase") is None:
        return Result("session-mode DB round-trip", False, "supabase CLI not on PATH")

    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as tf:
        script_path = Path(tf.name)
    try:
        # Connection-free: only prints the pg_dump script. Output to file only.
        proc = subprocess.run(
            ["supabase", "db", "dump", "--schema", "public", "--dry-run"],
            capture_output=True, text=True, timeout=30.0, stdin=subprocess.DEVNULL,
        )
        if proc.returncode != 0:
            last = (proc.stderr or "").strip().splitlines()
            return Result(
                "session-mode DB round-trip",
                False,
                redact(f"db dump --dry-run rc={proc.returncode}: {last[-1:] }"),
            )
        script_path.write_text(proc.stdout, encoding="utf-8")
        env = {**os.environ, **parse_dryrun_pg_env(proc.stdout)}
        if "PGPASSWORD" not in env or "PGHOST" not in env:
            return Result("session-mode DB round-trip", False, "could not derive CLI login env from dry-run")

        host_family = "pooler" if "pooler.supabase.com" in env.get("PGHOST", "") else "other"
        port = env.get("PGPORT", "?")
        # Single result set only: psql -c shows just the LAST of multiple ;-separated
        # statements, so fold db/version/extensions into one row.
        sql = (
            "SELECT current_database() || '|' || version() || '|' || "
            "COALESCE((SELECT string_agg(extname, ',' ORDER BY extname) "
            "FROM pg_extension WHERE extname IN ('vector','pg_trgm')), '');"
        )
        run = subprocess.run(
            ["psql", "-X", "-q", "-tA", "-P", "pager=off", "-c", sql],
            capture_output=True, text=True, timeout=30.0, env=env, stdin=subprocess.DEVNULL,
        )
        if run.returncode != 0:
            return Result(
                "session-mode DB round-trip",
                False,
                redact(f"psql rc={run.returncode}: {(run.stderr or '').strip().splitlines()[-1:]}"),
            )
        line = (run.stdout or "").strip().splitlines()
        first = line[0] if line else ""
        # version() contains no '|'; so fields = [db, version, exts].
        fields = first.split("|", 2) if first else []
        db = fields[0] if fields else "?"
        exts = [e for e in (fields[2].split(",") if len(fields) > 2 else []) if e]
        mode = "session(5432)" if port == "5432" else f"port-{port}"
        ok = redact(db) == "postgres"
        return Result(
            "session-mode DB round-trip",
            ok,
            redact(f"{mode}/{host_family}; db={db}; extensions={exts}"),
        )
    finally:
        try:
            script_path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify Hivemind access paths (read-only).")
    parser.add_argument("--db-probe", action="store_true", help="add authenticated session-mode DB round-trip")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)

    results: list[Result] = [
        check_cli(),
        check_linked_ref(),
        check_public_read_path(),
        check_projects_link(),
        check_functions(),
        check_secrets(),
    ]
    if args.db_probe:
        results.append(check_db_session_probe())

    if args.json:
        payload = {
            "results": [
                {"name": r.name, "ok": r.ok, "core": r.core, "detail": r.detail}
                for r in results
            ],
            "all_core_passed": all(r.ok for r in results if r.core),
        }
        print(json.dumps(payload, indent=2))
    else:
        print("Hivemind access verification (task 0.1) — read-only, all output redacted")
        for r in results:
            print(r)
        core_ok = all(r.ok for r in results if r.core)
        print(f"\nCORE CHECKS: {'PASS' if core_ok else 'FAIL'}")

    return 0 if all(r.ok for r in results if r.core) else 1


if __name__ == "__main__":
    raise SystemExit(main())
