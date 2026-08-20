#!/usr/bin/env python3
"""Hivemind legacy-search baseline capture (hybrid-search plan, task 0.4).

**FROZEN SNAPSHOT (2026-08-19).** The pack search executor no longer queries
``unified_feed``: it was rewritten to search the raw tables (message_feed /
external_resources / distillations) with per-token OR predicates because the
legacy unified_feed ILIKE-phrase path returns zero rows for multi-word
queries and blows the anon role's statement budget (HTTP 500 / SQLSTATE
57014) on per-token ORs.  This script captures the **historical** legacy
behaviour so the hybrid-search project's phase-0 baseline measurements stay
reproducible; the request-construction helpers below are frozen copies of the
old executor and must NOT be updated to track the new one.

It reproduces the exact legacy search path — the two PostgREST ``ILIKE``
passes against ``unified_feed`` and the per-pass (not global) limit — and
measures it, it does **not** implement lexical or hybrid search.

What it captures, per query:

  * the exact PostgREST params sent to each pass (so the run is reproducible
    without storing any secret);
  * per-pass and end-to-end latency;
  * status / failure / timeout classification
    (``success`` | ``http_error`` | ``timeout`` | ``url_error`` | ``parse_error``
    | ``other_error``);
  * requested limit, per-pass fetched counts, total fetched, total returned;
  * duplicate ``(kind, item_id)`` keys in the merged list;
  * result kinds and order, and the ``created_at`` sequence (proves the result
    is unranked view order, not relevance order);
  * body-truncation and no-distillation-nudge behaviour;
  * whether every ``item_id`` survived as a string (Discord snowflake check).

It then emits dated machine-readable results (JSON) and a concise
latency/relevance report (Markdown). Relevance *grades* are deliberately NOT
produced here — the report only records observed rows. Human judgment of those
rows is task 0.6 and is clearly fenced off below.

Design rules (from the plan's security section):

  * **Read-only only.** Every network call is a ``GET`` against ``unified_feed``
    with the publishable anon key — exactly what an installed client does on
    every search. No writes, no schema/function/secret/corpus mutation.
  * **Bounded and rate-conscious.** A small fixed manifest, a configurable
    inter-query delay, a per-request timeout, and a maximum request budget.
  * **Every** line of human-facing output is routed through :func:`redact`
    (reused from task 0.1), which masks API keys, DB passwords, tokens,
    connection strings, and publishable keys.
  * The anon key is never written to results; only a masked prefix is recorded.

Run::

    python3 scripts/baseline_search.py --dry-run            # show plan, no network
    python3 scripts/baseline_search.py                       # live capture to stdout
    python3 scripts/baseline_search.py \\
        --out-json docs/hybrid-search/phase0-baseline-results-2026-07-28.json \\
        --out-md  docs/hybrid-search/phase0-baseline.md
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import platform
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SCRIPTS = REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# Reuse task 0.1's verified redactor — same safety boundary.
import verify_access as va  # noqa: E402
from executors._common import resolve_anon_key, resolve_endpoint  # noqa: E402

redact = va.redact

TOOL_VERSION = "baseline-search/0.4.0"
MANIFEST_VERSION = "legacy-ilike/2026-07-28"
DEFAULT_LIMIT = 20
DEFAULT_TIMEOUT = 30.0          # matches executors/_common._http_get
DEFAULT_SLEEP = 0.5            # seconds between queries (rate-conscious)
DEFAULT_MAX_REQUESTS = 60      # hard cap on total network GETs in one run
BODY_TRUNCATION_LIMIT = 700    # matches executors/_common._BODY_TRUNCATION_LIMIT
TITLE_SNIPPET_LEN = 120        # how much of a title we keep in results (no full bodies)

# ---------------------------------------------------------------------------
# Fixed query manifest — safe, representative, no secrets.
#
# Each entry is a single legacy search. Categories mirror the plan's golden-set
# families so the baseline already exercises the cases hybrid search must beat.
# ---------------------------------------------------------------------------

DEFAULT_MANIFEST: list[dict[str, Any]] = [
    {
        "id": "hit_common",
        "category": "hit",
        "query": "upscale",
        "limit": 20,
        "notes": "Common term expected to match many messages; baseline latency/relevance reference.",
    },
    {
        "id": "hit_multiword",
        "category": "hit",
        "query": "controlnet settings",
        "limit": 20,
        "notes": "Multi-word contiguous substring; legacy ILIKE matches only the exact phrase.",
    },
    {
        "id": "exact_dotted",
        "category": "exact_identifier",
        "query": "FLUX.1",
        "limit": 20,
        "notes": "Dotted model name; punctuation is part of the contiguous ILIKE substring.",
    },
    {
        "id": "exact_versioned",
        "category": "exact_identifier",
        "query": "Wan 2.2",
        "limit": 20,
        "notes": "Versioned model name with a space and a dot.",
    },
    {
        "id": "exact_identifier_word",
        "category": "exact_identifier",
        "query": "WanVideoSampler",
        "limit": 20,
        "notes": "CamelCase symbol likely present in workflow Python source.",
    },
    {
        "id": "workflow_kinds",
        "category": "workflow",
        "query": "lora",
        "kinds": "workflow",
        "limit": 20,
        "notes": "kinds=workflow; only one pass is made (kind=in.(workflow)).",
    },
    {
        "id": "rare_nohit",
        "category": "rare_no_hit",
        "query": "zzqxnotarealterm-99999",
        "limit": 20,
        "notes": "Nonsense string expected to return zero rows; exercises zero-result path.",
    },
    {
        "id": "filter_source",
        "category": "filter",
        "query": "model",
        "sources": "banodoco-discord",
        "limit": 10,
        "notes": "source=in.(banodoco-discord) applied to both passes.",
    },
    {
        "id": "filter_since",
        "category": "filter",
        "query": "model",
        "since": "2024-01-01T00:00:00Z",
        "limit": 10,
        "notes": "created_at=gte.2024-01-01 applied to both passes.",
    },
    {
        "id": "doubled_limit",
        "category": "doubled_limit",
        "query": "model",
        "limit": 3,
        "notes": "Common term + small limit; each pass capped at 3 independently, so the merged list may exceed the requested limit.",
    },
    {
        "id": "timeout_prone",
        "category": "timeout_prone",
        "query": "upscale",
        "limit": 20,
        "client_timeout": 0.01,
        "notes": "Client-side 10 ms timeout on an ordinary query. Reproducibly exercises the legacy executor's *unhandled* timeout path without loading the database.",
    },
]

# Required manifest categories (mirrors the plan's baseline coverage list).
REQUIRED_CATEGORIES = {
    "hit",
    "exact_identifier",
    "workflow",
    "rare_no_hit",
    "timeout_prone",
    "filter",
    "doubled_limit",
}


# ---------------------------------------------------------------------------
# Legacy request construction — a FROZEN, independent copy of the old
# executors/search/run.py (pre-2026-08-19).  Copying (not importing) keeps
# this baseline a fixed snapshot: the new executor moved off unified_feed and
# these helpers intentionally do not track it.
# ---------------------------------------------------------------------------


def legacy_ilike_clause(query: str) -> str:
    """Frozen copy of the legacy executor's _ilike_clause (pre-2026-08-19)."""
    encoded = query.replace("*", "\\*")  # escape literal asterisks
    return f"(title.ilike.*{encoded}*,body.ilike.*{encoded}*)"


def legacy_build_params(
    query: str,
    kinds: str | None,
    sources: str | None,
    since: str | None,
    limit: int,
) -> dict[str, str]:
    """Frozen copy of the legacy executor's _build_params (common params, pre-kind-override)."""
    params: dict[str, str] = {
        "select": "*",
        "limit": str(limit),
        "or": legacy_ilike_clause(query),
    }
    if kinds:
        params["kind"] = f"in.({kinds})"
    if sources:
        params["source"] = f"in.({sources})"
    if since:
        params["created_at"] = f"gte.{since}"
    return params


def legacy_passes(kinds: str | None) -> list[tuple[str, str]]:
    """Replicate executors.search.run.main()'s pass selection.

    Returns a list of ``(pass_name, kind_param_value)`` pairs in execution
    order: the distillation pass first (if requested), then the others pass.
    Mirrors the ``has_distillation`` / ``has_others`` logic exactly.
    """
    user_kinds = [k.strip() for k in kinds.split(",")] if kinds else None
    has_distillation = user_kinds is None or "distillation" in user_kinds
    has_others = user_kinds is None or any(k != "distillation" for k in user_kinds)
    passes: list[tuple[str, str]] = []
    if has_distillation:
        passes.append(("distillation", "eq.distillation"))
    if has_others:
        if user_kinds:
            other_kinds = ",".join(k for k in user_kinds if k != "distillation")
            passes.append(("others", f"in.({other_kinds})"))
        else:
            passes.append(("others", "neq.distillation"))
    return passes


def build_pass_params(
    entry: dict[str, Any],
    pass_name: str,
    kind_param: str,
) -> dict[str, str]:
    """Build the final params for one pass: common params then the per-pass kind override."""
    params = legacy_build_params(
        entry["query"],
        entry.get("kinds"),
        entry.get("sources"),
        entry.get("since"),
        entry["limit"],
    )
    # The executor overrides 'kind' after building common params (see run.py).
    params["kind"] = kind_param
    return params


def build_url(endpoint: str, params: dict[str, str]) -> str:
    """Exact copy of executors._common.postgrest_get URL construction."""
    base = endpoint.rstrip("/")
    url = f"{base}/unified_feed"
    qs = urllib.parse.urlencode(params)
    return f"{url}?{qs}"


def build_headers(anon_key: str) -> dict[str, str]:
    """Exact copy of executors._common.postgrest_get headers."""
    return {"apikey": anon_key, "Accept": "application/json"}


# ---------------------------------------------------------------------------
# Low-level GET + outcome classification
# ---------------------------------------------------------------------------


def do_get(url: str, anon_key: str, timeout: float) -> tuple[int, Any]:
    """Perform a GET and return ``(http_status, parsed_json)``.

    Raises on any error (network/timeout/HTTP/non-2xx/parse); callers classify.
    Mirrors executors._common._http_get but accepts a timeout.
    """
    req = urllib.request.Request(url, headers=build_headers(anon_key), method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        # Avoid eagerly evaluating a fallback default; real HTTPResponse has .status.
        status = getattr(resp, "status", None)
        if status is None:
            status = resp.getcode()
        body = resp.read().decode("utf-8")
    return status, json.loads(body)


def classify_exception(exc: BaseException) -> tuple[str, str]:
    """Map a raised exception to an outcome category + redacted detail.

    Categories: ``http_error`` | ``timeout`` | ``url_error`` | ``parse_error``
    | ``other_error``. Detail is routed through :func:`redact`.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return "http_error", redact(f"HTTP {exc.code} {exc.reason}")
    # A request timeout surfaces as URLError(socket.timeout) or socket.timeout
    # (which is itself an alias of TimeoutError on 3.10+).
    is_timeout = isinstance(exc, (socket.timeout, TimeoutError)) or (
        isinstance(exc, urllib.error.URLError)
        and isinstance(getattr(exc, "reason", None), (socket.timeout, TimeoutError))
    )
    if is_timeout:
        return "timeout", redact(f"timeout: {type(exc).__name__}")
    if isinstance(exc, urllib.error.URLError):
        return "url_error", redact(f"url error: {type(exc.reason).__name__}: {exc.reason}")
    if isinstance(exc, json.JSONDecodeError):
        return "parse_error", "non-JSON response body"
    return "other_error", redact(f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Row parsing + derived metrics (pure; unit-tested)
# ---------------------------------------------------------------------------


def normalize_rows(body: Any) -> list[dict[str, Any]]:
    """Coerce a PostgREST response body into a list of row dicts.

    PostgREST returns an array normally, but may return a single object for
    ``limit=1``; mirrors the legacy executor's _query_feed.
    """
    if isinstance(body, list):
        return [r for r in body if isinstance(r, dict)]
    if isinstance(body, dict):
        return [body]
    return []


def _identity_key(row: dict[str, Any]) -> tuple[str, str]:
    """Stable per-item identity used for duplicate detection."""
    return (str(row.get("kind", "")), str(row.get("item_id", "")))


def detect_duplicates(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Return duplicate count and the duplicated ``(kind, item_id)`` keys."""
    seen: dict[tuple[str, str], int] = {}
    for row in rows:
        key = _identity_key(row)
        seen[key] = seen.get(key, 0) + 1
    dup_keys = [list(k) for k, n in seen.items() if n > 1]
    return {"duplicate_count": sum(n - 1 for n in seen.values() if n > 1), "duplicate_keys": dup_keys}


def summarize_row(row: dict[str, Any]) -> dict[str, Any]:
    """Compact, secret-safe row summary — never stores full bodies.

    The title snippet is corpus-derived free text, so it is routed through
    :func:`redact` before persistence; ``item_id`` is a short Discord snowflake
    and is kept verbatim (it is below the redactor's token-length threshold and
    is the stable identity task 0.6 needs).
    """
    body = row.get("body", "")
    body_len = len(body) if isinstance(body, str) else 0
    title = row.get("title")
    if isinstance(title, str):
        title_snip = redact(title[:TITLE_SNIPPET_LEN])
    else:
        title_snip = title
    iid = row.get("item_id")
    return {
        "kind": row.get("kind"),
        "source": row.get("source"),
        "item_id": iid,
        "item_id_is_string": isinstance(iid, str),
        "title": title_snip,
        "body_length": body_len,
        "body_would_truncate": body_len > BODY_TRUNCATION_LIMIT,
        "created_at": row.get("created_at"),
    }


def kind_distribution(rows: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        k = str(row.get("kind", "<null>"))
        out[k] = out.get(k, 0) + 1
    return out


def snowflake_check(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Confirm every item_id is a string (Discord snowflakes must not round-trip as numbers)."""
    non_string = [
        {"kind": r.get("kind"), "item_id": r.get("item_id")}
        for r in rows
        if not isinstance(r.get("item_id"), str)
    ]
    return {
        "all_item_ids_strings": len(non_string) == 0,
        "non_string_item_ids": non_string[:10],
    }


# ---------------------------------------------------------------------------
# Per-query measurement
# ---------------------------------------------------------------------------


def measure_query(
    entry: dict[str, Any],
    *,
    endpoint: str,
    anon_key: str,
    default_timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Run one legacy search (both passes) and return a full measurement record."""
    limit = entry["limit"]
    kinds = entry.get("kinds")
    timeout = float(entry.get("client_timeout", default_timeout))
    passes = legacy_passes(kinds)

    record: dict[str, Any] = {
        "id": entry["id"],
        "category": entry["category"],
        "query": entry["query"],
        "kinds": kinds,
        "sources": entry.get("sources"),
        "since": entry.get("since"),
        "requested_limit": limit,
        "client_timeout_s": timeout,
        "notes": entry.get("notes", ""),
        "passes_planned": [p[0] for p in passes],
        "passes": [],
    }

    merged_rows: list[dict[str, Any]] = []
    distillation_rows: list[dict[str, Any]] = []

    e2e_start = time.perf_counter()
    for pass_name, kind_param in passes:
        params = build_pass_params(entry, pass_name, kind_param)
        url = build_url(endpoint, params)
        pass_record: dict[str, Any] = {
            "pass": pass_name,
            "kind_param": kind_param,
            "params": params,  # reproducible; contains no secret (anon key is a header)
            "outcome": None,
            "status": None,
            "detail": None,
            "latency_ms": None,
            "fetched": 0,
            "rows": [],
        }
        t0 = time.perf_counter()
        try:
            status, body = do_get(url, anon_key, timeout)
            rows = normalize_rows(body)
            outcome = "success"
            detail = ""
        except BaseException as exc:  # noqa: BLE001 — classify every failure, never crash
            status, rows, outcome, detail = None, [], *classify_exception(exc)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        pass_record.update(
            outcome=outcome,
            status=status,
            detail=detail,
            latency_ms=round(latency_ms, 2),
            fetched=len(rows),
            rows=[summarize_row(r) for r in rows],
        )
        record["passes"].append(pass_record)
        merged_rows.extend(rows)
        if pass_name == "distillation":
            distillation_rows = rows
    e2e_ms = (time.perf_counter() - e2e_start) * 1000.0

    # Merge fidelity: distillations first, then others (legacy order).
    dup = detect_duplicates(merged_rows)
    record["e2e_latency_ms"] = round(e2e_ms, 2)
    record["total_fetched"] = len(merged_rows)
    record["total_returned"] = len(merged_rows)  # legacy executor applies no global trim
    record["exceeds_requested_limit"] = len(merged_rows) > limit
    record["kind_distribution"] = kind_distribution(merged_rows)
    record["duplicate"] = dup
    record["snowflake"] = snowflake_check(merged_rows)
    record["nudge_present"] = len(distillation_rows) == 0  # mirrors the legacy executor's nudge rule
    record["truncated_rows"] = sum(
        1 for r in (summarize_row(r) for r in merged_rows) if r["body_would_truncate"]
    )
    # created_at sequence proves ordering is the view's default, not relevance.
    record["created_at_order"] = [r.get("created_at") for r in merged_rows][:50]
    # Overall query outcome: success only if every planned pass succeeded;
    # otherwise report the first failing pass's outcome (in execution order).
    if all(p["outcome"] == "success" for p in record["passes"]):
        record["outcome"] = "success"
    else:
        record["outcome"] = next(
            p["outcome"] for p in record["passes"] if p["outcome"] != "success"
        )
    return record


# ---------------------------------------------------------------------------
# Aggregation / latency stats (pure; stdlib only)
# ---------------------------------------------------------------------------


def percentile(values: list[float], pct: float) -> float | None:
    """Linear-interpolated percentile (pct in [0,100]); None for empty input."""
    if not values:
        return None
    xs = sorted(float(v) for v in values)
    if len(xs) == 1:
        return round(xs[0], 2)
    k = (len(xs) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    frac = k - lo
    return round(xs[lo] + (xs[hi] - xs[lo]) * frac, 2)


def _latencies(records: list[dict[str, Any]], field: str) -> list[float]:
    out: list[float] = []
    for r in records:
        v = r.get(field)
        if isinstance(v, (int, float)):
            out.append(float(v))
    return out


def _pass_latencies(records: list[dict[str, Any]]) -> list[float]:
    out: list[float] = []
    for r in records:
        for p in r.get("passes", []):
            v = p.get("latency_ms")
            if isinstance(v, (int, float)):
                out.append(float(v))
    return out


def build_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate latency, outcome, zero-result, duplicate, and limit stats."""
    e2e = _latencies(records, "e2e_latency_ms")
    per_pass = _pass_latencies(records)

    def stat_block(vals: list[float]) -> dict[str, Any]:
        return {
            "n": len(vals),
            "min_ms": min(vals) if vals else None,
            "p50_ms": percentile(vals, 50),
            "p95_ms": percentile(vals, 95),
            "max_ms": max(vals) if vals else None,
        }

    outcomes: dict[str, int] = {}
    zero_result = 0
    exceeds_limit = 0
    total_duplicates = 0
    categories_seen: dict[str, int] = {}
    for r in records:
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
        if r["total_fetched"] == 0:
            zero_result += 1
        if r["exceeds_requested_limit"]:
            exceeds_limit += 1
        total_duplicates += r["duplicate"]["duplicate_count"]
        cat = r["category"]
        categories_seen[cat] = categories_seen.get(cat, 0) + 1

    return {
        "queries": len(records),
        "categories": categories_seen,
        "outcomes": outcomes,
        "zero_result_queries": zero_result,
        "queries_exceeding_requested_limit": exceeds_limit,
        "total_duplicate_items": total_duplicates,
        "all_snowflakes_strings": all(r["snowflake"]["all_item_ids_strings"] for r in records),
        "end_to_end_latency": stat_block(e2e),
        "per_pass_latency": stat_block(per_pass),
    }


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------


def endpoint_ref(endpoint: str) -> str | None:
    import re

    m = re.search(r"https://([a-z0-9]{20})\.supabase\.co", endpoint)
    return m.group(1) if m else None


def mask_key(anon_key: str) -> str:
    """Return a non-reversible mask of the anon key for the results file."""
    if not anon_key:
        return "<unset>"
    if len(anon_key) <= 16:
        return f"{anon_key[:4]}…({len(anon_key)} chars)"
    return f"{anon_key[:14]}…({len(anon_key)} chars, last4=…{anon_key[-4:]})"


def build_meta(endpoint: str, anon_key: str, *, sleep: float, default_timeout: float) -> dict[str, Any]:
    return {
        "tool_version": TOOL_VERSION,
        "manifest_version": MANIFEST_VERSION,
        "captured_at_utc": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "endpoint_ref": endpoint_ref(endpoint),
        "anon_key_masked": mask_key(anon_key),
        "config": {
            "default_limit": DEFAULT_LIMIT,
            "default_timeout_s": default_timeout,
            "sleep_s": sleep,
            "body_truncation_limit": BODY_TRUNCATION_LIMIT,
            "per_request_default_timeout_s": default_timeout,
        },
        "behavior_replicated": (
            "Two PostgREST GET passes against unified_feed (kind=eq.distillation, then "
            "kind=neq.distillation / in.(...)), each capped independently at the requested "
            "limit; results merged distillations-first; no global limit; no relevance ranking."
        ),
        "secret_policy": (
            "Read-only GETs only. No full bodies, no anon key, no token is written; "
            "all human-facing text is routed through verify_access.redact."
        ),
    }


def write_results_json(path: str, meta: dict[str, Any], records: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    payload = {
        "meta": meta,
        "summary": summary,
        "results": records,
    }
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_report_md(path: str, meta: dict[str, Any], records: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report(meta, records, summary), encoding="utf-8")


def _fmt_ms(v: Any) -> str:
    return f"{v:.0f} ms" if isinstance(v, (int, float)) else "—"


def _healthy_pass_latencies(records: list[dict[str, Any]]) -> list[float]:
    """Per-pass latencies for successful passes only (excludes timeouts/errors)."""
    out: list[float] = []
    for r in records:
        for p in r.get("passes", []):
            if p.get("outcome") == "success" and isinstance(p.get("latency_ms"), (int, float)):
                out.append(float(p["latency_ms"]))
    return out


def _real_failures(records: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Failed passes on *real* queries — i.e. excluding the artificial ``timeout_prone`` probe.

    Returns ``[(record, pass), ...]``. The ``timeout_prone`` manifest entry uses a
    tiny client-side timeout purely to exercise the classification path, so it is
    reported separately from genuine 30 s timeouts and server errors.
    """
    out: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for r in records:
        if r.get("category") == "timeout_prone":
            continue
        for p in r.get("passes", []):
            po = p.get("outcome")
            if po is not None and po != "success":
                out.append((r, p))
    return out


def render_report(meta: dict[str, Any], records: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Phase 0 — Task 0.4 Legacy `ILIKE` Search Baseline")
    lines.append("")
    lines.append(f"**Captured (UTC):** {meta['captured_at_utc']}")
    lines.append(f"**Tool:** `{meta['tool_version']}` · **Manifest:** `{meta['manifest_version']}`")
    lines.append(f"**Python:** {meta['python_version']} · **Endpoint ref:** `{meta['endpoint_ref']}`")
    lines.append(f"**Behavior replicated:** {meta['behavior_replicated']}")
    lines.append("")
    lines.append(
        "This is an **observed** baseline only. It records what the deployed "
        "two-pass `ILIKE` search returns and how long it takes. It does **not** "
        "assign relevance grades — that human judgment is task 0.6 and is fenced "
        "off at the end. No secret, full body, or anon key is recorded here."
    )
    lines.append("")
    lines.append("## Headline findings")
    lines.append("")
    e2e = summary["end_to_end_latency"]
    pp = summary["per_pass_latency"]
    healthy = _healthy_pass_latencies(records)
    real_failures = _real_failures(records)
    lines.append(
        f"- **Latency:** end-to-end p50 {_fmt_ms(e2e['p50_ms'])} / p95 {_fmt_ms(e2e['p95_ms'])} "
        f"(max {_fmt_ms(e2e['max_ms'])}) across {e2e['n']} queries; per-pass p50 "
        f"{_fmt_ms(pp['p50_ms'])} / p95 {_fmt_ms(pp['p95_ms'])} across {pp['n']} GETs. "
        f"The p95 is dominated by genuine 30 s timeouts (see below); **healthy** successful "
        f"passes alone run p50 {_fmt_ms(percentile(healthy, 50))} / p95 {_fmt_ms(percentile(healthy, 95))} "
        f"(n={len(healthy)})."
    )
    if real_failures:
        lines.append(
            f"- **Failures on real queries:** {len(real_failures)} pass(es) on ordinary "
            "queries (not the artificial probe) failed — genuine 30 s timeouts and a server "
            "500. Under the deployed executor each is an unhandled exception. See "
            "\"Failures on real queries\" below."
        )
    else:
        lines.append(
            "- **Failures on real queries:** none (only the artificial `timeout_prone` "
            "probe timed out, by construction)."
        )
    lines.append(
        f"- **No global limit:** {summary['queries_exceeding_requested_limit']} of "
        f"{summary['queries']} queries returned more rows than the requested limit "
        "— each of the two passes is capped independently at `limit`, so the merged "
        "list is bounded by `2 × limit`, not `limit`. See the doubled-limit row below."
    )
    lines.append(
        f"- **Unranked:** results come back in `unified_feed`'s default order "
        "(see per-query `created_at_order`); there is no lexical or relevance ranking."
    )
    lines.append(
        f"- **Contiguous-substring only:** `ILIKE` matches the single phrase "
        "`*<query>*` on title/body, so multi-term queries (e.g. \"controlnet settings\") "
        "miss rows that contain both terms non-contiguously."
    )
    lines.append(
        f"- **Duplicates:** {summary['total_duplicate_items']} duplicate "
        "`(kind, item_id)` items observed across merged lists (expected ~0; the two "
        "passes query disjoint `kind` sets)."
    )
    lines.append(
        f"- **Snowflakes:** {'all item_ids preserved as strings' if summary['all_snowflakes_strings'] else 'NON-STRING item_ids observed — see results'}."
    )
    lines.append(
        f"- **Outcomes:** {summary['outcomes']} ({summary['zero_result_queries']} zero-result queries)."
    )
    lines.append(
        "- **Timeout handling:** the deployed executor catches only `HTTPError`; a "
        "network/`URLError`/timeout propagates as an unhandled traceback. This tool "
        "classifies it instead (see the `timeout_prone` row)."
    )
    lines.append("")
    lines.append("## Per-query results")
    lines.append("")
    lines.append("| id | category | query | passes | outcome | fetched | >limit? | dup | e2e | pA / pB (ms) |")
    lines.append("|---|---|---|---|---|---:|:--:|---:|---:|---|")
    for r in records:
        q = redact(r["query"])
        passes = "/".join(p["pass"][0].upper() for p in r["passes"]) or "—"
        pA = next((p for p in r["passes"] if p["pass"] == "distillation"), None)
        pB = next((p for p in r["passes"] if p["pass"] == "others"), None)
        def _l(p: dict[str, Any] | None) -> str:
            return f"{p['latency_ms']:.0f}" if p and isinstance(p.get("latency_ms"), (int, float)) else "—"
        pa_pb = f"{_l(pA)} / {_l(pB)}"
        gt = "yes" if r["exceeds_requested_limit"] else ""
        lines.append(
            f"| {r['id']} | {r['category']} | `{q}` | {passes} | {r['outcome']} | "
            f"{r['total_fetched']} | {gt} | {r['duplicate']['duplicate_count']} | "
            f"{_fmt_ms(r['e2e_latency_ms'])} | {pa_pb} |"
        )
    lines.append("")
    lines.append("Categories covered: " + ", ".join(sorted(summary["categories"])) + ".")
    lines.append("")
    lines.append("## Doubled-limit detail")
    lines.append("")
    dl = next((r for r in records if r["category"] == "doubled_limit"), None)
    if dl:
        per_pass_fetched = ", ".join(f"{p['pass']}={p['fetched']}" for p in dl["passes"])
        lines.append(
            f"Query `{dl['query']}` with `limit={dl['requested_limit']}` fetched "
            f"{dl['total_fetched']} rows ({per_pass_fetched}). Each pass is capped "
            f"independently at {dl['requested_limit']}; the executor applies no global "
            "trim, so a query that hits both distillations and other kinds can return up "
            "to `2 × limit`. Task 1.8 / 3.3 enforces one global limit."
        )
    lines.append("")
    lines.append("## Failures on real queries")
    lines.append("")
    if real_failures:
        lines.append(
            "These are failures on **ordinary** queries (default 30 s client timeout) — not "
            "the artificial `timeout_prone` probe. They are the strongest evidence that "
            "`ILIKE` over the un-indexed `unified_feed` view is unstable for some queries, "
            "which is the core problem the indexed lexical work in Phase 1 addresses."
        )
        lines.append("")
        lines.append("| query | pass | outcome | latency | detail |")
        lines.append("|---|---|---|---:|---|")
        for r, p in real_failures:
            lines.append(
                f"| `{redact(r['query'])}` | {p.get('pass')} | `{p.get('outcome')}` | "
                f"{_fmt_ms(p.get('latency_ms'))} | {p.get('detail') or '—'} |"
            )
        lines.append("")
        lines.append(
            "Under the deployed `executors/search/run.py`, only `urllib.error.HTTPError` is "
            "caught (→ exit code 2). A `URLError`/`socket.timeout` (the 30 s timeouts above) "
            "is **not** caught and surfaces as an unhandled traceback. The 500 is caught but "
            "yields zero results. This baseline tool classifies every outcome instead."
        )
    else:
        lines.append("No ordinary query failed in this run.")
    lines.append("")
    lines.append("## Timeout classification (artificial probe)")
    lines.append("")
    tp = next((r for r in records if r["category"] == "timeout_prone"), None)
    if tp:
        to = tp["passes"][0]
        lines.append(
            f"`timeout_prone` used a client-side {tp['client_timeout_s']} s timeout on an "
            f"ordinary query (`{redact(tp['query'])}`). Outcome: `{to['outcome']}` "
            f"({to['detail'] or 'no detail'}) at {_fmt_ms(to['latency_ms'])}. This only "
            "exercises the classification path; it does not load the database. It is "
            "reported separately from the genuine timeouts above."
        )
    lines.append("")
    lines.append("## Reproduce")
    lines.append("")
    lines.append("```bash")
    lines.append("python3 scripts/baseline_search.py --dry-run                       # review the request plan")
    lines.append("python3 scripts/baseline_search.py \\")
    lines.append("  --out-json docs/hybrid-search/phase0-baseline-results-2026-07-28.json \\")
    lines.append("  --out-md  docs/hybrid-search/phase0-baseline.md")
    lines.append("python3 -m unittest tests.test_baseline_search                     # offline tests")
    lines.append("```")
    lines.append("")
    lines.append("## Relevance judgments — deliberately deferred (task 0.6)")
    lines.append("")
    lines.append(
        "No relevance grades are fabricated here. The rows above are **observed**. "
        "Task 0.6 will judge the stable `(kind, item_id)` identities returned by these "
        "queries (and ~100 more) and feed them to the evaluation harness from task 0.5. "
        "Until then this file contains measurements only."
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dry-run plan (no network)
# ---------------------------------------------------------------------------


def render_dry_run(manifest: list[dict[str, Any]], endpoint: str, anon_key: str) -> str:
    lines: list[str] = []
    lines.append("baseline_search.py — DRY RUN (no network requests will be made)")
    lines.append(f"endpoint_ref={endpoint_ref(endpoint)}  anon_key={mask_key(anon_key)}")
    lines.append(f"manifest_version={MANIFEST_VERSION}  queries={len(manifest)}")
    lines.append("")
    total_gets = 0
    for entry in manifest:
        passes = legacy_passes(entry.get("kinds"))
        total_gets += len(passes)
        lines.append(f"[{entry['id']}] category={entry['category']} limit={entry['limit']}")
        for pass_name, kind_param in passes:
            params = build_pass_params(entry, pass_name, kind_param)
            lines.append(f"    {pass_name:<13} kind={kind_param:<20} {redact(build_url('<endpoint>', params))}")
    lines.append("")
    lines.append(f"total GETs if run live: {total_gets} (cap={DEFAULT_MAX_REQUESTS})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Manifest validation (pure; unit-tested)
# ---------------------------------------------------------------------------


def validate_manifest(manifest: list[dict[str, Any]]) -> list[str]:
    """Return a list of human-readable problems; empty list means valid."""
    problems: list[str] = []
    if not manifest:
        return ["manifest is empty"]
    seen_ids: set[str] = set()
    seen_cats: set[str] = set()
    required_fields = ("id", "category", "query", "limit")
    for i, entry in enumerate(manifest):
        if not isinstance(entry, dict):
            problems.append(f"entry {i}: not a dict")
            continue
        for field in required_fields:
            if field not in entry:
                problems.append(f"entry {i} ({entry.get('id', '?')}): missing required field '{field}'")
        eid = str(entry.get("id", ""))
        if eid in seen_ids:
            problems.append(f"entry {i}: duplicate id '{eid}'")
        seen_ids.add(eid)
        seen_cats.add(str(entry.get("category", "")))
        if not isinstance(entry.get("limit"), int) or entry["limit"] < 1:
            problems.append(f"entry {i} ({eid}): limit must be a positive int")
        if not str(entry.get("query", "")).strip():
            problems.append(f"entry {i} ({eid}): empty query")
    missing = REQUIRED_CATEGORIES - seen_cats
    if missing:
        problems.append("manifest missing required categories: " + ", ".join(sorted(missing)))
    return problems


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def load_manifest(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return [dict(e) for e in DEFAULT_MANIFEST]
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"manifest {path} must be a JSON list of query entries")
    return data


def run_baseline(
    manifest: list[dict[str, Any]],
    *,
    endpoint: str,
    anon_key: str,
    sleep: float,
    default_timeout: float,
    max_requests: int = DEFAULT_MAX_REQUESTS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run the manifest live; return (records, summary). Read-only, rate-conscious."""
    planned_gets = sum(len(legacy_passes(e.get("kinds"))) for e in manifest)
    if planned_gets > max_requests:
        raise ValueError(
            f"manifest plans {planned_gets} GETs but the cap is {max_requests}; "
            "pass --max-requests to raise it"
        )
    records: list[dict[str, Any]] = []
    for i, entry in enumerate(manifest):
        rec = measure_query(entry, endpoint=endpoint, anon_key=anon_key, default_timeout=default_timeout)
        records.append(rec)
        # Rate-conscious pause between queries (not within a query's two passes,
        # which the deployed executor fires back-to-back).
        if sleep > 0 and i < len(manifest) - 1:
            time.sleep(sleep)
    return records, build_summary(records)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture the Hivemind legacy ILIKE search baseline (read-only).")
    parser.add_argument("--manifest", help="path to an alternate JSON manifest (defaults to the built-in one)")
    parser.add_argument("--limit", type=int, default=None, help="override the limit on every manifest entry")
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP, help=f"seconds between queries (default {DEFAULT_SLEEP})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help=f"default per-request timeout in seconds (default {DEFAULT_TIMEOUT})")
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS, help=f"hard cap on total GETs (default {DEFAULT_MAX_REQUESTS})")
    parser.add_argument("--dry-run", action="store_true", help="print the request plan and exit; make no network calls")
    parser.add_argument(
        "--from-json",
        help="re-render the Markdown report from an existing results JSON snapshot (no network, no manifest). "
        "Requires --out-md; pairs with --out-json to rewrite the JSON too.",
    )
    parser.add_argument("--out-json", help="write dated machine-readable JSON results to this path")
    parser.add_argument("--out-md", help="write the concise Markdown report to this path")
    args = parser.parse_args(argv)

    endpoint = resolve_endpoint()
    anon_key = resolve_anon_key()

    # Re-render path: regenerate the report from a captured snapshot, no network.
    if args.from_json:
        snapshot = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
        meta = snapshot.get("meta", {}) or build_meta(endpoint, anon_key, sleep=args.sleep, default_timeout=args.timeout)
        records = snapshot.get("results", [])
        summary = snapshot.get("summary") or build_summary(records)
        if args.out_md:
            write_report_md(args.out_md, meta, records, summary)
            print(redact(f"wrote {args.out_md}"))
        if args.out_json:
            write_results_json(args.out_json, meta, records, summary)
            print(redact(f"wrote {args.out_json}"))
        if not (args.out_md or args.out_json):
            print(json.dumps({"meta": meta, "summary": summary}, indent=2, ensure_ascii=False))
        return 0

    manifest = load_manifest(args.manifest)
    if args.limit is not None:
        for e in manifest:
            e["limit"] = args.limit

    problems = validate_manifest(manifest)
    if problems:
        print("INVALID manifest:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(redact(render_dry_run(manifest, endpoint, anon_key)))
        return 0

    if not (args.out_json or args.out_md):
        # Default to stdout JSON so the tool is useful without flags, but warn.
        print("baseline_search.py: capturing live (read-only). Pass --out-json/--out-md to persist.", file=sys.stderr)

    try:
        records, summary = run_baseline(
            manifest,
            endpoint=endpoint,
            anon_key=anon_key,
            sleep=args.sleep,
            default_timeout=args.timeout,
            max_requests=args.max_requests,
        )
    except ValueError as exc:
        print(f"aborted: {exc}", file=sys.stderr)
        return 2

    meta = build_meta(endpoint, anon_key, sleep=args.sleep, default_timeout=args.timeout)

    if args.out_json:
        write_results_json(args.out_json, meta, records, summary)
        print(redact(f"wrote {args.out_json}"))
    if args.out_md:
        write_report_md(args.out_md, meta, records, summary)
        print(redact(f"wrote {args.out_md}"))
    if not (args.out_json or args.out_md):
        print(json.dumps({"meta": meta, "summary": summary, "results": records}, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
