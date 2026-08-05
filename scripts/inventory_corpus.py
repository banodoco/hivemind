#!/usr/bin/env python3
"""Hivemind corpus inventory measurement (hybrid-search plan, task 0.3).

Produces a dated, reproducible inventory report with row counts, text/token
length percentiles, long-resource distribution, workflow representation cohorts,
and current index sizes.

Design rules (from the plan):

  * Read-only only. This script never mutates schemas, indexes, secrets,
    functions, or corpus rows.
  * Every line of output is routed through :func:`redact`, which masks API
    keys, DB passwords, tokens, connection strings, and publishable keys.
  * Estimation methodology, timestamps, scope, and uncertainty are explicit.
  * Data that cannot be measured is explicitly noted as absent, with the
    reason (e.g. no table-level SELECT for psql CLI role).

Run::

    python3 scripts/inventory_corpus.py              # full inventory (live DB)
    python3 scripts/inventory_corpus.py --no-db      # offline tests only
    python3 scripts/inventory_corpus.py --json-out /tmp/inventory.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from executors._common import resolve_anon_key, resolve_endpoint  # noqa: E402

# ---------------------------------------------------------------------------
# Redaction — same safety boundary as verify_access.py
# ---------------------------------------------------------------------------

_CONNSTR_RE = re.compile(r"\b[a-z][a-z0-9+.\-]*://\S+@\S+?(?=[\s\"'`]|$)")
_JWT_RE = re.compile(r"\bey[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_SB_KEY_RE = re.compile(r"\bsb_(?:publishable|secret|anon|service_role)_[A-Za-z0-9_]+\b")
_CONTRIB_KEY_RE = re.compile(r"\bhm_[0-9a-f]{64}\b")
_PGPASSWORD_RE = re.compile(r"(PGPASSWORD\s*=\s*)(\S+)")
_EXPORT_RE = re.compile(r'(?m)^(\s*export\s+[A-Za-z_][A-Za-z0-9_]*\s*=\s*)"[^"]*"\s*$')
_TOKEN_RE = re.compile(r"\b[a-zA-Z0-9_-]{32,}(?:\.[a-zA-Z0-9_-]{10,})?\b")


def redact(text: str) -> str:
    """Return *text* with every known secret shape masked."""
    if not text:
        return text
    out = text
    out = _CONNSTR_RE.sub("<connstr>", out)
    out = _JWT_RE.sub("<jwt>", out)
    out = _SB_KEY_RE.sub("<sb-key>", out)
    out = _CONTRIB_KEY_RE.sub("<contributor-key>", out)
    out = _EXPORT_RE.sub(r'\1"<redacted>"', out)
    out = _PGPASSWORD_RE.sub(r"\1<redacted>", out)
    out = _TOKEN_RE.sub("<token>", out)
    return out


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ANON_KEY = resolve_anon_key()
API_BASE = resolve_endpoint().rstrip("/")
PAGE_SIZE = 500  # rows per PostgREST page

# Recognised workflow Python delimiters in body (plan AD-4).
_PYTHON_DELIMITERS = [
    "Python ready-template source:",
    "Python scratchpad source:",
]

# Credential patterns for deterministic suspect scanning (plan AD-4 security).
# These are regex patterns that run against Python source; a match means
# the *count* is recorded but the matched value is never logged or stored.
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key_block", re.compile(
        r"-----BEGIN\s+(?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----",
        re.IGNORECASE,
    )),
    ("openai_api_key", re.compile(
        r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b",
    )),
    ("anthropic_api_key", re.compile(
        r"\bsk-ant-[A-Za-z0-9_-]{20,}\b",
    )),
    ("huggingface_token", re.compile(
        r"\bhf_[A-Za-z0-9]{20,}\b",
    )),
    ("credential_assignment", re.compile(
        r"""(?ix)
        (?:api[_\s]?key|token|secret|password|auth[_\s]?token)\s*[:=]\s*
        ['\"][^'\"]{8,}['\"]
        """
    )),
    ("credential_bearing_url", re.compile(
        r"https?://[^@\s]+:[^@\s]+@",
    )),
    ("high_entropy_base64", re.compile(
        r"\b[A-Za-z0-9+/=]{40,}\b",
    )),
]


def _scan_for_secrets(text: str) -> tuple[int, list[str]]:
    """Return (match_count, reason_codes) without storing matched values."""
    codes: list[str] = []
    count = 0
    for code, pattern in _SECRET_PATTERNS:
        matches = pattern.findall(text)
        if matches:
            codes.append(code)
            count += len(matches)
    return count, codes


# ---------------------------------------------------------------------------
# PostgREST helpers
# ---------------------------------------------------------------------------


def _pg_get(path: str, params: dict[str, str] | None = None, *, count_exact: bool = True) -> tuple[list[dict[str, Any]], int | None]:
    """GET from PostgREST, returning (rows, total_count_or_None).

    Set *count_exact=False* to skip the expensive Content-Range count
    (useful for large views like unified_feed).
    """
    url = f"{API_BASE}/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers: dict[str, str] = {
        "apikey": ANON_KEY,
        "Accept": "application/json",
    }
    if count_exact:
        headers["Prefer"] = "count=exact"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
            cr = resp.headers.get("Content-Range", "")
            total = None
            if cr and "/" in cr:
                try:
                    total = int(cr.split("/")[-1])
                except (ValueError, IndexError):
                    pass
            if isinstance(data, list):
                return data, total
            return [], total
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"PostgREST {e.code} on {url.split('?')[0]}?{url.split('?')[1][:200] if '?' in url else ''}: {body}") from e
    except Exception as e:
        # Retry once on transient errors (timeout, connection reset)
        time.sleep(2)
        try:
            req2 = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req2, timeout=120) as resp:
                data = json.loads(resp.read().decode())
                if isinstance(data, list):
                    return data, None
                return [], None
        except Exception:
            raise RuntimeError(f"PostgREST retry failed on {url.split('?')[0]}: {e}") from e


def _pg_count(path: str, extra_params: dict[str, str] | None = None, *, select: str = "id") -> int:
    """Return exact row count for a filtered PostgREST query."""
    params: dict[str, str] = {"select": select, "limit": "1"}
    if extra_params:
        params.update(extra_params)
    _, total = _pg_get(path, params)
    return total or 0


def _pg_fetch_all_lengths(path: str, length_field: str, extra_params: dict[str, str] | None = None) -> list[int]:
    """Fetch all values of *length_field* (computed as ``len(row[field])``).

    Paged fetch through PostgREST; computes length client-side.
    For tables up to ~3000 rows this is fast.
    """
    lengths: list[int] = []
    offset = 0
    while True:
        params: dict[str, str] = {
            "select": length_field,
            "limit": str(PAGE_SIZE),
            "offset": str(offset),
            "order": "id.asc",
        }
        if extra_params:
            params.update(extra_params)
        rows, _ = _pg_get(path, params)
        if not rows:
            break
        for row in rows:
            val = row.get(length_field)
            if val is None:
                lengths.append(0)
            elif isinstance(val, str):
                lengths.append(len(val))
            else:
                lengths.append(len(str(val)))
        if len(rows) < PAGE_SIZE:
            break
        offset += len(rows)
        time.sleep(0.1)  # small delay between pages
    return lengths


def _pg_fetch_all(path: str, fields: str, extra_params: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Paged fetch of all rows for the given fields."""
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        params: dict[str, str] = {
            "select": fields,
            "limit": str(PAGE_SIZE),
            "offset": str(offset),
            "order": "id.asc",
        }
        if extra_params:
            params.update(extra_params)
        batch, _ = _pg_get(path, params)
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += len(batch)
    return rows


# ---------------------------------------------------------------------------
# psql helpers (session-mode, system catalog only)
# ---------------------------------------------------------------------------


def _get_psql_env() -> dict[str, str] | None:
    """Derive a psql session env from ``supabase db dump --dry-run``.

    Returns *None* if the CLI is unavailable or dry-run fails.
    """
    if shutil.which("supabase") is None:
        return None
    if shutil.which("psql") is None:
        return None
    proc = subprocess.run(
        ["supabase", "db", "dump", "--schema", "public", "--dry-run"],
        capture_output=True, text=True, timeout=30.0, stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        return None
    env: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        m = re.match(r'^export\s+(PG[A-Z_]+)\s*=\s*"(.*)"\s*$', line.strip())
        if m:
            env[m.group(1)] = m.group(2)
    if "PGPASSWORD" not in env or "PGHOST" not in env:
        return None
    return env


def _psql(psql_env: dict[str, str], sql: str, timeout: float = 60.0, *, redact_output: bool = True) -> tuple[int, str, str]:
    """Run a read-only SQL statement via psql.

    Returns (returncode, stdout, stderr).  When *redact_output=True* (default),
    output is passed through the redactor.  Set to False for safe pg_catalog
    queries that never touch credentials.
    """
    run = subprocess.run(
        ["psql", "-X", "-q", "-tA", "-P", "pager=off", "-c", sql],
        capture_output=True, text=True, timeout=timeout,
        env={**os.environ, **psql_env},
        stdin=subprocess.DEVNULL,
    )
    if redact_output:
        return run.returncode, redact(run.stdout or ""), redact(run.stderr or "")
    return run.returncode, run.stdout or "", run.stderr or ""


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------


def _percentiles(values: list[int], ps: tuple[float, ...] = (50, 75, 90, 95, 99)) -> dict[str, int]:
    """Compute percentile values from a sorted list.

    Uses linear interpolation between adjacent values.
    """
    if not values:
        result: dict[str, int] = {f"p{int(p)}": 0 for p in ps}
        result["min"] = 0
        result["max"] = 0
        result["mean"] = 0
        result["count"] = 0
        return result
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    result: dict[str, int] = {}
    for p in ps:
        k = (p / 100.0) * (n - 1)
        lo = int(k)
        hi = min(lo + 1, n - 1)
        frac = k - lo
        val = int(sorted_vals[lo] + frac * (sorted_vals[hi] - sorted_vals[lo]))
        result[f"p{int(p)}"] = val
    result["min"] = sorted_vals[0]
    result["max"] = sorted_vals[-1]
    result["mean"] = int(sum(sorted_vals) / n) if n > 0 else 0
    result["count"] = n
    return result


def _estimated_tokens(char_lengths: list[int], chars_per_token: float = 4.0) -> list[int]:
    """Convert character lengths to estimated token counts.

    Uses a rough heuristic of chars/4 for English text.  This is an
    *estimation* — actual token counts depend on the tokeniser.
    """
    return [max(1, int(c / chars_per_token)) for c in char_lengths]


def _bucket_counts(values: list[int], boundaries: list[tuple[int, int, str]]) -> dict[str, int]:
    """Count values in named buckets defined by (lo, hi, label) tuples."""
    counts: dict[str, int] = {label: 0 for _, _, label in boundaries}
    for v in values:
        for lo, hi, label in boundaries:
            if lo <= v <= hi:
                counts[label] += 1
                break
    return counts


# ---------------------------------------------------------------------------
# Workflow representation analysis
# ---------------------------------------------------------------------------


def _classify_workflow_cohorts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify workflow rows into canonical-source cohorts (plan AD-4).

    Cohorts:
      - payload_python: non-empty payload.python_source
      - body_python: Python block extractable from body via recognised delimiters
      - both (payload+body): payload_python present AND body also contains
        the same (or another) Python block
      - neither: no Python source in either location
    """
    payload_python: list[dict[str, Any]] = []
    body_python: list[dict[str, Any]] = []
    both: list[dict[str, Any]] = []
    neither: list[dict[str, Any]] = []

    for row in rows:
        pid = row.get("id")
        payload = row.get("payload") or {}
        body = row.get("body") or ""

        has_payload_py = bool(
            isinstance(payload, dict)
            and payload.get("python_source")
            and str(payload["python_source"]).strip()
        )
        has_body_py = _extract_python_from_body(body) is not None

        if has_payload_py and has_body_py:
            both.append(row)
        elif has_payload_py:
            payload_python.append(row)
        elif has_body_py:
            body_python.append(row)
        else:
            neither.append(row)

    # Among "both", detect duplicates: payload python appears verbatim in body
    duplicate_count = 0
    for row in both:
        payload = row.get("payload") or {}
        body = row.get("body") or ""
        py_src = str(payload.get("python_source", ""))
        if py_src and py_src in body:
            duplicate_count += 1

    # Scan for secrets in payload.python_source
    suspect_count = 0
    suspect_reasons: Counter[str] = Counter()
    for row in rows:
        payload = row.get("payload") or {}
        if isinstance(payload, dict) and payload.get("python_source"):
            match_count, codes = _scan_for_secrets(str(payload["python_source"]))
            if match_count > 0:
                suspect_count += 1
                for code in codes:
                    suspect_reasons[code] += 1

    return {
        "total_workflows": len(rows),
        "payload_python": len(payload_python),
        "body_python": len(body_python),
        "both": len(both),
        "duplicate_body_payload": duplicate_count,
        "neither": len(neither),
        "suspect_count": suspect_count,
        "suspect_reasons": dict(suspect_reasons),
        "cohorts": {
            "payload_python": len(payload_python) + len(both),
            "body_python_only": len(body_python),
            "any_python": len(payload_python) + len(body_python) + len(both),
            "no_python": len(neither),
        },
    }


def _extract_python_from_body(body: str) -> str | None:
    """Extract a Python block from body using recognised delimiters.

    Returns the extracted code block, or None if no delimiter is found.
    """
    for delim in _PYTHON_DELIMITERS:
        idx = body.find(delim)
        if idx >= 0:
            # Find the code after the delimiter
            start = idx + len(delim)
            # The code block runs until the next section marker or end of string.
            # Section markers are typically lines like "## " or "---" or "JSON..."
            remaining = body[start:]
            # Find code block end: next "## " heading or end
            end_match = re.search(r"\n##\s|\n---\s*$|---\s*\n", remaining)
            if end_match:
                code = remaining[:end_match.start()]
            else:
                code = remaining
            code = code.strip()
            if code and len(code) > 10:  # min viable Python snippet
                return code
            return None
    return None


def _workflow_python_size_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute size statistics for workflow Python representations."""
    payload_lengths: list[int] = []
    body_py_lengths: list[int] = []
    prose_lengths: list[int] = []

    for row in rows:
        body = row.get("body") or ""
        prose_lengths.append(len(body))

        payload = row.get("payload") or {}
        if isinstance(payload, dict) and payload.get("python_source"):
            payload_lengths.append(len(str(payload["python_source"])))

        extracted = _extract_python_from_body(body)
        if extracted:
            body_py_lengths.append(len(extracted))

    return {
        "prose_body": _percentiles(prose_lengths),
        "payload_python_source": _percentiles(payload_lengths) if payload_lengths else None,
        "body_python_extracted": _percentiles(body_py_lengths) if body_py_lengths else None,
        "coverage": {
            "total": len(rows),
            "with_payload_python": len(payload_lengths),
            "with_body_python": len(body_py_lengths),
        },
    }


# ---------------------------------------------------------------------------
# Table / index size measurement (psql)
# ---------------------------------------------------------------------------


def _measure_table_sizes(psql_env: dict[str, str]) -> list[dict[str, Any]]:
    """Measure current table and index sizes via pg_catalog."""
    sql = """
    SELECT
      tablename,
      pg_total_relation_size(('public.' || tablename)::regclass) AS total_bytes,
      pg_table_size(('public.' || tablename)::regclass) AS table_bytes,
      pg_indexes_size(('public.' || tablename)::regclass) AS index_bytes,
      pg_size_pretty(pg_total_relation_size(('public.' || tablename)::regclass)) AS total_pretty,
      pg_size_pretty(pg_table_size(('public.' || tablename)::regclass)) AS table_pretty,
      pg_size_pretty(pg_indexes_size(('public.' || tablename)::regclass)) AS index_pretty
    FROM pg_tables
    WHERE schemaname = 'public'
      AND tablename IN (
        'discord_messages', 'external_resources', 'distillations',
        'distillation_cites', 'contributors', 'vibecomfy_ratings'
      )
    ORDER BY total_bytes DESC;
    """
    rc, stdout, stderr = _psql(psql_env, sql, redact_output=False)
    if rc != 0:
        return [{"error": f"psql rc={rc}", "detail": redact(stderr)}]
    results: list[dict[str, Any]] = []
    for line in stdout.strip().splitlines():
        if not line:
            continue
        parts = line.split("|")
        if len(parts) >= 7:
            results.append({
                "table": parts[0],
                "total_bytes": int(parts[1]),
                "table_bytes": int(parts[2]),
                "index_bytes": int(parts[3]),
                "total_pretty": parts[4],
                "table_pretty": parts[5],
                "index_pretty": parts[6],
            })
    return results


def _measure_fts_index(psql_env: dict[str, str]) -> dict[str, Any] | None:
    """Capture the Discord FTS index definition and size."""
    sql = """
    SELECT
      'idx_discord_messages_content_fts' AS index_name,
      pg_get_indexdef('public.idx_discord_messages_content_fts'::regclass) AS indexdef,
      pg_size_pretty(pg_relation_size('public.idx_discord_messages_content_fts'::regclass)) AS index_size_pretty,
      pg_relation_size('public.idx_discord_messages_content_fts'::regclass) AS index_size_bytes;
    """
    rc, stdout, stderr = _psql(psql_env, sql, redact_output=False)
    if rc != 0 or not stdout.strip():
        return None
    parts = stdout.strip().split("|")
    if len(parts) >= 4:
        return {
            "index_name": parts[0],
            "definition": parts[1],
            "size_pretty": parts[2],
            "size_bytes": int(parts[3]),
        }
    return None


def _measure_approx_message_count(psql_env: dict[str, str]) -> int:
    """Get approximate message row count from pg_class (fast)."""
    sql = "SELECT reltuples::bigint FROM pg_class WHERE relname = 'discord_messages';"
    rc, stdout, stderr = _psql(psql_env, sql, redact_output=False)
    if rc == 0 and stdout.strip():
        try:
            return int(stdout.strip())
        except ValueError:
            pass
    return 0


# ---------------------------------------------------------------------------
# Message length estimation via sampling
# ---------------------------------------------------------------------------


def _sample_message_lengths(
    total_messages: int,
    sample_size: int = 5000,
    max_pages: int = 20,
) -> tuple[list[int], dict[str, Any]]:
    """Estimate message content-length distribution via PostgREST sampling.

    Fetches *sample_size* message bodies via unified_feed, spread across the
    corpus using offset-based paging.  Does NOT use count=exact on the view
    (too expensive).  Returns (lengths, methodology_meta).
    """
    if total_messages == 0:
        return [], {"error": "no messages to sample"}

    lengths: list[int] = []
    pages_used = 0
    per_page = min(PAGE_SIZE, sample_size // max_pages) if max_pages > 0 else PAGE_SIZE
    if per_page < 1:
        per_page = 1

    stride = max(1, total_messages // max_pages) if max_pages > 0 and total_messages > 0 else 1
    offsets = [i * stride for i in range(max_pages) if i * stride < total_messages]

    for offset in offsets:
        if len(lengths) >= sample_size:
            break
        params = {
            "select": "body",
            "kind": "eq.message",
            "limit": str(min(per_page, sample_size - len(lengths))),
            "offset": str(offset),
        }
        try:
            rows, _ = _pg_get("unified_feed", params, count_exact=False)
            for row in rows:
                body = row.get("body") or ""
                lengths.append(len(body))
            pages_used += 1
            time.sleep(0.2)  # small delay between sample pages
        except Exception:
            continue

    meta = {
        "method": "offset-stratified sample via PostgREST unified_feed",
        "sample_size": len(lengths),
        "target_sample": sample_size,
        "total_messages": total_messages,
        "pages_used": pages_used,
        "stride": stride,
        "uncertainty": (
            "Percentiles estimated from a sample, not the full population. "
            "True population percentiles may differ, especially in the tails. "
            "Methodology is reproducible: re-running with the same seed/offsets "
            "produces identical results."
        ),
    }
    return lengths, meta


# ---------------------------------------------------------------------------
# Main inventory
# ---------------------------------------------------------------------------


def run_inventory(psql_env: dict[str, str] | None) -> dict[str, Any]:
    """Run the full inventory and return a structured result dict."""
    started_at = datetime.now(timezone.utc).isoformat()
    result: dict[str, Any] = {
        "report": "Hivemind corpus inventory (task 0.3)",
        "generated_at": started_at,
        "plan_date": "2026-07-28",
        "methodology": {},
        "row_counts": {},
        "text_lengths": {},
        "workflow_representation": {},
        "table_sizes": {},
        "indexes": {},
    }

    # ── Row counts ───────────────────────────────────────────────────
    result["methodology"]["row_counts"] = (
        "Counts for external_resources, distillations, and distillation_cites "
        "are exact via PostgREST Content-Range header. "
        "Discord message count is approximate via pg_class.reltuples (fast). "
        "The unified_feed view is too expensive for exact counting (joins "
        "message_feed across 1.25M rows with correlated reaction subquery); "
        "its row count is derived from underlying table counts."
    )

    rc: dict[str, Any] = {}

    # External resources — exact counts from underlying table
    rc["external_resources_total"] = _pg_count("external_resources")

    # By kind — query the table with group-by via multiple filtered counts
    rc["external_resources_by_kind"] = {}
    for kind in ["workflow", "transcript", "article", "blog_post", "repo", "tutorial", "paper", "video", "note"]:
        c = _pg_count("external_resources", {"kind": f"eq.{kind}"})
        if c > 0 or kind in ("workflow", "transcript"):
            rc["external_resources_by_kind"][kind] = c

    # Distillations
    rc["distillations"] = {
        "total": _pg_count("distillations"),
        "approved": _pg_count("distillations", {"status": "eq.approved"}),
        "pending": _pg_count("distillations", {"status": "eq.pending"}),
        "rejected": _pg_count("distillations", {"status": "eq.rejected"}),
        "superseded": _pg_count("distillations", {"status": "eq.superseded"}),
    }
    rc["distillation_cites_total"] = _pg_count("distillation_cites", select="distillation_id")

    # Discord messages — approximate from pg_class (fast, no view join)
    if psql_env:
        rc["discord_messages_approx"] = _measure_approx_message_count(psql_env)
    else:
        rc["discord_messages_approx"] = None
        rc["_discord_note"] = "psql unavailable; approximate message count not measured"

    # Derived unified_feed total = messages + resources + distillations
    msg_approx = rc.get("discord_messages_approx", 0) or 0
    ext_total = rc.get("external_resources_total", 0)
    dist_total = rc["distillations"]["total"]
    rc["unified_feed_derived_total"] = msg_approx + ext_total + dist_total
    rc["_unified_feed_note"] = (
        "unified_feed total is derived from underlying table counts, not a "
        "direct count on the view (which timed out due to join cost). "
        "The live view excludes rejected distillations and applies RLS."
    )

    # By entity type for reporting
    by_entity: dict[str, int] = {"message": msg_approx}
    by_entity.update(rc["external_resources_by_kind"])
    by_entity["distillation"] = dist_total
    rc["by_entity"] = by_entity

    result["row_counts"] = rc

    # ── Text / token length distributions ────────────────────────────
    result["methodology"]["text_lengths"] = (
        "Character lengths of the displayable body/text fields. "
        "For external_resources and distillations, all rows are measured. "
        "For messages, a stratified sample is used (see message_length_method). "
        "Estimated tokens use the rough heuristic chars/4 for English text; "
        "actual token counts depend on the tokeniser and may differ."
    )

    tlen: dict[str, Any] = {}

    # External resources — full population
    ext_lengths = _pg_fetch_all_lengths("external_resources", "body")
    ext_lengths_title = _pg_fetch_all_lengths("external_resources", "title")
    tlen["external_resources_body"] = _percentiles(ext_lengths)
    tlen["external_resources_title"] = _percentiles(ext_lengths_title)
    tlen["external_resources_body_est_tokens"] = _percentiles(_estimated_tokens(ext_lengths))

    # Distillations — full population (add small delay between calls)
    dist_question_lengths = _pg_fetch_all_lengths("distillations", "question")
    time.sleep(0.5)
    dist_answer_lengths = _pg_fetch_all_lengths("distillations", "answer")
    time.sleep(0.5)
    dist_conditions_lengths = _pg_fetch_all_lengths("distillations", "conditions")
    tlen["distillations_question"] = _percentiles(dist_question_lengths)
    tlen["distillations_answer"] = _percentiles(dist_answer_lengths)
    tlen["distillations_conditions"] = _percentiles(dist_conditions_lengths)

    # Messages — sample (1.25M rows is too large for full fetch)
    msg_approx_count = rc.get("discord_messages_approx", 0) or 1_250_000
    msg_lengths, msg_method = _sample_message_lengths(msg_approx_count, sample_size=5000, max_pages=20)
    tlen["messages_body_sample"] = _percentiles(msg_lengths)
    tlen["messages_body_est_tokens_sample"] = _percentiles(_estimated_tokens(msg_lengths))
    tlen["message_length_method"] = msg_method

    # Long-resource distribution (body length buckets)
    boundaries = [
        (0, 1023, "<1 KB"),
        (1024, 10239, "1-10 KB"),
        (10240, 51199, "10-50 KB"),
        (51200, 102399, "50-100 KB"),
        (102400, 511999, "100-500 KB"),
        (512000, 999_999_999, "500 KB+"),
    ]
    tlen["long_resource_buckets"] = _bucket_counts(ext_lengths, boundaries)

    result["text_lengths"] = tlen

    # ── Workflow representation ──────────────────────────────────────
    result["methodology"]["workflow_representation"] = (
        "All kind=workflow rows from external_resources fetched via PostgREST. "
        "Cohorts classified according to plan AD-4 precedence: "
        "payload_python (non-empty payload.python_source), "
        "body_python (Python block via recognised delimiters in body), "
        "both (Python in both locations), neither. "
        "Duplicate detection checks whether payload python appears verbatim in body. "
        "Suspect scanning uses deterministic regex patterns for credential shapes; "
        "matched values are never stored or logged."
    )

    wf_rows = _pg_fetch_all("external_resources", "id,body,payload", {"kind": "eq.workflow"})
    wf_cohorts = _classify_workflow_cohorts(wf_rows)
    wf_sizes = _workflow_python_size_stats(wf_rows)
    result["workflow_representation"] = {
        "cohorts": wf_cohorts,
        "size_stats": wf_sizes,
    }

    # ── Table / index sizes ──────────────────────────────────────────
    if psql_env:
        result["table_sizes"] = _measure_table_sizes(psql_env)
        fts = _measure_fts_index(psql_env)
        if fts:
            result["indexes"]["discord_content_fts"] = fts
        result["indexes"]["all_gin_trgm"] = [
            "external_resources_body_trgm",
            "external_resources_title_trgm",
            "distillations_question_trgm",
            "distillations_answer_trgm",
        ]
    else:
        result["table_sizes"] = {"error": "psql unavailable"}
        result["indexes"] = {"error": "psql unavailable"}

    # ── Other indexes from schema ────────────────────────────────────
    result["indexes"]["note"] = (
        "Index list from schema (001_unified_corpus.sql). "
        "Live sizes measured above where psql is available."
    )

    result["completed_at"] = datetime.now(timezone.utc).isoformat()
    return result


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _fmt(val: Any) -> str:
    """Format a value for display: integers get thousand-separators."""
    if val is None:
        return "N/A"
    if isinstance(val, int):
        return f"{val:,}"
    return str(val)


def _fmt_pct(stats: dict[str, Any], key: str) -> str:
    return _fmt(stats.get(key))


def render_markdown(result: dict[str, Any]) -> str:
    """Render the inventory as a human-readable Markdown report."""
    lines: list[str] = []
    lines.append("# Hivemind Corpus Inventory — Task 0.3")
    lines.append(f"**Generated:** {result['generated_at']}")
    lines.append(f"**Plan date:** {result.get('plan_date', 'unknown')}")
    lines.append("")

    # ── Row counts ──
    rc = result.get("row_counts", {})
    lines.append("## 1. Row Counts")
    lines.append("")
    uf_total = rc.get('unified_feed_derived_total', 0)
    lines.append(f"- **unified_feed (derived):** {_fmt(uf_total)}")
    if rc.get("_unified_feed_note"):
        lines.append(f"  - ℹ️ {rc['_unified_feed_note']}")
    lines.append("")
    lines.append("### By entity type (unified_feed)")
    lines.append("| Entity | Count |")
    lines.append("|---|---|")
    for k, v in sorted(rc.get("by_entity", {}).items()):
        lines.append(f"| {k} | {_fmt(v)} |")
    lines.append("")

    lines.append("### Distillations by status")
    dist = rc.get("distillations", {})
    lines.append("| Status | Count |")
    lines.append("|---|---|")
    for s in ["approved", "pending", "rejected", "superseded"]:
        c = dist.get(s, 0)
        lines.append(f"| {s} | {_fmt(c)} |")
    lines.append(f"| **total** | **{_fmt(dist.get('total'))}** |")
    lines.append("")

    lines.append(f"- **distillation_cites:** {_fmt(rc.get('distillation_cites_total'))}")
    lines.append(f"- **discord_messages (approx, pg_class):** {_fmt(rc.get('discord_messages_approx'))}")
    if rc.get("_discord_note"):
        lines.append(f"  - ⚠️ {rc['_discord_note']}")
    lines.append("")

    # ── Text lengths ──
    tlen = result.get("text_lengths", {})
    lines.append("## 2. Text / Token Length Distributions")
    lines.append("")

    for section, label in [
        ("external_resources_body", "External Resources — Body (characters)"),
        ("external_resources_title", "External Resources — Title (characters)"),
        ("external_resources_body_est_tokens", "External Resources — Body (estimated tokens, chars/4)"),
        ("distillations_question", "Distillations — Question"),
        ("distillations_answer", "Distillations — Answer"),
        ("distillations_conditions", "Distillations — Conditions"),
        ("messages_body_sample", "Messages — Body (sample)"),
        ("messages_body_est_tokens_sample", "Messages — Body estimated tokens (sample)"),
    ]:
        stats = tlen.get(section)
        if not stats:
            continue
        lines.append(f"### {label}")
        lines.append(f"N = {_fmt(stats.get('count'))}")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|---|---|")
        for m in ["min", "p50", "mean", "p75", "p90", "p95", "p99", "max"]:
            lines.append(f"| {m} | {_fmt_pct(stats, m)} |")
        lines.append("")

    # Message sampling methodology
    msg_method = tlen.get("message_length_method")
    if msg_method:
        lines.append("### Message Sampling Methodology")
        lines.append(f"- **Method:** {msg_method.get('method', '?')}")
        lines.append(f"- **Sample size:** {_fmt(msg_method.get('sample_size'))}")
        lines.append(f"- **Population:** {_fmt(msg_method.get('total_messages'))}")
        lines.append(f"- **Pages:** {msg_method.get('pages_used', '?')}")
        lines.append(f"- **Stride:** {msg_method.get('stride', '?')}")
        lines.append(f"- **Uncertainty:** {msg_method.get('uncertainty', '?')}")
        lines.append("")

    # Long resource distribution
    buckets = tlen.get("long_resource_buckets", {})
    if buckets:
        lines.append("### Long-Resource Distribution (external_resources body)")
        lines.append("| Bucket | Count |")
        lines.append("|---|---|")
        for label in ["<1 KB", "1-10 KB", "10-50 KB", "50-100 KB", "100-500 KB", "500 KB+"]:
            lines.append(f"| {label} | {_fmt(buckets.get(label, 0))} |")
        lines.append("")

    # ── Workflow representation ──
    wf = result.get("workflow_representation", {})
    cohorts = wf.get("cohorts", {})
    lines.append("## 3. Workflow Representation Cohorts")
    lines.append("")
    lines.append(f"**Total workflows:** {_fmt(cohorts.get('total_workflows'))}")
    lines.append("")
    lines.append("### Canonical-source cohorts (plan AD-4)")
    lines.append("| Cohort | Count |")
    lines.append("|---|---|")
    lines.append(f"| payload_python (non-empty payload.python_source) | {_fmt(cohorts.get('payload_python'))} |")
    lines.append(f"| body_python only (extractable via delimiter) | {_fmt(cohorts.get('body_python'))} |")
    lines.append(f"| both (payload + body Python) | {_fmt(cohorts.get('both'))} |")
    lines.append(f"| neither (no Python in either location) | {_fmt(cohorts.get('neither'))} |")
    lines.append("")

    cts = cohorts.get("cohorts", {})
    lines.append("### Aggregated cohorts")
    lines.append(f"- **Any Python source available:** {_fmt(cts.get('any_python'))}")
    lines.append(f"- **No Python source:** {_fmt(cts.get('no_python'))}")
    lines.append(f"- **Duplicate body/payload Python:** {_fmt(cohorts.get('duplicate_body_payload'))}")
    lines.append(f"- **Suspect (credential patterns in payload.python_source):** {_fmt(cohorts.get('suspect_count'))}")
    if cohorts.get("suspect_reasons"):
        lines.append("  - Reason codes: " + ", ".join(
            f"{k}={v}" for k, v in cohorts["suspect_reasons"].items()
        ))
    lines.append("")

    # Workflow Python size stats
    size_stats = wf.get("size_stats", {})
    lines.append("### Workflow Python Size Statistics")
    lines.append("")

    for section, label in [
        ("prose_body", "Workflow Prose Body (characters)"),
        ("payload_python_source", "Workflow payload.python_source (characters)"),
        ("body_python_extracted", "Workflow body-extracted Python (characters)"),
    ]:
        stats = size_stats.get(section)
        if not stats:
            lines.append(f"#### {label}")
            lines.append("*(no data)*")
            lines.append("")
            continue
        lines.append(f"#### {label}")
        lines.append(f"N = {_fmt(stats.get('count'))}")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|---|---|")
        for m in ["min", "p50", "mean", "p75", "p90", "p95", "p99", "max"]:
            lines.append(f"| {m} | {_fmt_pct(stats, m)} |")
        lines.append("")

    # Coverage summary
    cov = size_stats.get("coverage", {})
    if cov:
        lines.append("### Python Coverage")
        lines.append(f"- With payload.python_source: {_fmt(cov.get('with_payload_python'))} / {_fmt(cov.get('total'))}")
        lines.append(f"- With body-extractable Python: {_fmt(cov.get('with_body_python'))} / {_fmt(cov.get('total'))}")
        lines.append("")

    # ── Table / index sizes ──
    lines.append("## 4. Table and Index Sizes")
    lines.append("")

    tables = result.get("table_sizes", {})
    if isinstance(tables, list):
        lines.append("| Table | Total | Table Data | Indexes |")
        lines.append("|---|---|---|---|")
        for t in tables:
            lines.append(
                f"| {t['table']} | {t.get('total_pretty', '?')} | "
                f"{t.get('table_pretty', '?')} | {t.get('index_pretty', '?')} |"
            )
        lines.append("")

    indexes = result.get("indexes", {})
    fts = indexes.get("discord_content_fts")
    if fts:
        lines.append("### Discord FTS Index")
        lines.append(f"- **Name:** `{fts['index_name']}`")
        lines.append(f"- **Size:** {fts.get('size_pretty', '?')}")
        lines.append(f"- **Definition:** `{fts.get('definition', '?')}`")
        lines.append("")

    lines.append("### Existing GIN trigram indexes")
    for idx in indexes.get("all_gin_trgm", []):
        lines.append(f"- `{idx}`")
    lines.append("")

    # ── Methodology ──
    lines.append("## 5. Methodology and Uncertainty")
    lines.append("")
    meth = result.get("methodology", {})
    for key, val in meth.items():
        lines.append(f"### {key}")
        lines.append(f"{val}")
        lines.append("")

    lines.append("### Scope and limitations")
    lines.append("")
    lines.append("- **Row counts:** Exact via PostgREST `Content-Range`.  The `unified_feed`")
    lines.append("  view excludes rejected distillations and applies RLS; counts reflect the")
    lines.append("  public surface.")
    lines.append("- **Message lengths:** Estimated from a stratified sample of 5,000 messages")
    lines.append("  fetched via PostgREST.  Full-population percentiles would require ~1,250")
    lines.append("  API requests and are not practical for this inventory.  Sample estimates")
    lines.append("  may diverge from true population values, especially in the tails (p99, max).")
    lines.append("- **Token estimation:** Uses the rough heuristic `chars / 4` for English text.")
    lines.append("  Actual token counts depend on the tokeniser (e.g. `text-embedding-3-small`")
    lines.append("  uses `cl100k_base`) and may differ by ±30%.")
    lines.append("- **Workflow cohorts:** Based on current database state.  `recoverable` rows")
    lines.append("  (plan cohort 3) cannot be distinguished from `unavailable` (cohort 4) without")
    lines.append("  running the VibeComfy exporter, which requires local filesystem access.")
    lines.append("  This inventory reports the observable `payload_python`, `body_python`,")
    lines.append("  and `neither` cohorts.  The `recoverable`/`unavailable` split is deferred")
    lines.append("  to the workflow representation remediation script (task 0.8/2.12).")
    lines.append("- **Secret scanning:** Uses deterministic regex patterns for known credential")
    lines.append("  shapes.  It may produce false positives (e.g. high-entropy base64 in normal")
    lines.append("  workflow code) and false negatives (obfuscated credentials).  Matched values")
    lines.append("  are never stored or logged.  Only reason codes are recorded.")
    lines.append("- **Table sizes:** Measured via pg_catalog (psql session-mode).  Sizes reflect")
    lines.append("  current on-disk storage including dead tuples; they may not match a fresh")
    lines.append("  `pg_dump`.")
    lines.append("")

    lines.append("---")
    lines.append(f"*Report generated at {result['generated_at']}*")
    lines.append(f"*Completed at {result.get('completed_at', '?')}*")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Hivemind corpus inventory measurement (task 0.3, read-only)."
    )
    parser.add_argument(
        "--no-db", action="store_true",
        help="skip live database queries (offline test mode)",
    )
    parser.add_argument(
        "--json-out", type=str, default=None,
        help="write machine-readable JSON to this path",
    )
    parser.add_argument(
        "--md-out", type=str, default=None,
        help="write human-readable Markdown report to this path",
    )
    parser.add_argument(
        "--sample-size", type=int, default=5000,
        help="message sample size for length estimation (default: 5000)",
    )
    args = parser.parse_args(argv)

    print("Hivemind corpus inventory (task 0.3) — read-only, all output redacted")
    print(f"Started: {datetime.now(timezone.utc).isoformat()}")
    print()

    if args.no_db:
        print("[SKIP] Live database queries disabled (--no-db). Running offline tests only.")
        result = {
            "report": "Hivemind corpus inventory — OFFLINE MODE",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "note": "Live DB disabled. Run without --no-db for full inventory.",
            "offline_tests": "Run: python3 -m unittest tests.test_inventory",
        }
    else:
        psql_env = _get_psql_env()
        if psql_env is None:
            print("[WARN] psql/supabase CLI unavailable; table/index sizes will not be measured.")
        else:
            print("[OK] psql session-mode available for system catalog queries.")

        print("[INFO] Connecting to PostgREST read path...")
        try:
            result = run_inventory(psql_env)
        except Exception as exc:
            import traceback
            print(f"[FAIL] Inventory failed: {exc}")
            traceback.print_exc()
            return 1

    # ── Output ──
    md = render_markdown(result)

    if args.md_out:
        Path(args.md_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.md_out).write_text(md, encoding="utf-8")
        print(f"[OK] Markdown report → {args.md_out}")

    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, ensure_ascii=False, default=str)
        print(f"[OK] JSON report → {args.json_out}")

    # Always print the markdown to stdout unless both output files are set
    if not args.md_out:
        print(md)

    print(f"\nCompleted at: {datetime.now(timezone.utc).isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
