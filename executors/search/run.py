#!/usr/bin/env python3
"""Search the Hivemind unified corpus — distillations-first merge with truncation."""

from __future__ import annotations

import argparse
import sys
import urllib.error
from typing import Any

# -- dual-import guard (T5 pattern) -------------------------------------------
try:
    from .._common import (
        output_json,
        postgrest_get,
        resolve_anon_key,
        resolve_endpoint,
        truncate_body,
    )
except ImportError:
    import os as _os

    _HERE = _os.path.dirname(_os.path.abspath(__file__))
    _EXECUTORS = _os.path.dirname(_HERE)
    sys.path.insert(0, _EXECUTORS)
    from _common import (  # type: ignore[import-not-found]
        output_json,
        postgrest_get,
        resolve_anon_key,
        resolve_endpoint,
        truncate_body,
    )

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NUDGE = (
    "No distillation results found — consider researching this question "
    "and submitting a cited distillation to help the next person."
)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hivemind.search",
        description="Search the Hivemind unified corpus.",
    )
    parser.add_argument("--query", required=True, help="Search query string.")
    parser.add_argument("--kinds", help="Comma-separated item kinds filter.")
    parser.add_argument("--sources", help="Comma-separated sources filter.")
    parser.add_argument("--since", help="ISO-8601 timestamp lower bound.")
    parser.add_argument(
        "--limit", type=int, default=20, help="Max results (default: 20)."
    )
    parser.add_argument(
        "--out", help="Write JSON output to this file instead of stdout."
    )
    return parser


# ---------------------------------------------------------------------------
# Query construction
# ---------------------------------------------------------------------------


def _ilike_clause(query: str) -> str:
    """Build the value of a PostgREST ``or`` filter that ilike-matches *query* on title and body."""
    encoded = query.replace("*", "\\*")  # escape literal asterisks
    return f"(title.ilike.*{encoded}*,body.ilike.*{encoded}*)"


def _build_params(
    query: str,
    kinds: str | None,
    sources: str | None,
    since: str | None,
    limit: int,
) -> dict[str, str]:
    """Build the common query-string parameters shared by both PostgREST calls."""
    params: dict[str, str] = {
        "select": "*",
        "limit": str(limit),
        "or": _ilike_clause(query),
    }
    if kinds:
        params["kind"] = f"in.({kinds})"
    if sources:
        params["source"] = f"in.({sources})"
    if since:
        params["created_at"] = f"gte.{since}"
    return params


def _query_feed(
    params: dict[str, str],
    *,
    endpoint: str,
    anon_key: str,
) -> list[dict[str, Any]]:
    """Call *postgrest_get* and return the result cast to a list."""
    result = postgrest_get("unified_feed", params=params, endpoint=endpoint, anon_key=anon_key)
    if isinstance(result, list):
        return result  # type: ignore[no-any-return]
    # PostgREST may return a single object when limit=1
    return [result]  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# Merge & shape
# ---------------------------------------------------------------------------


def _truncate_row(row: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *row* with body truncated & ``truncated`` flag."""
    truncated = truncate_body(row.get("body", ""))
    out = dict(row)
    out["body"] = truncated["body"]
    out["truncated"] = truncated["truncated"]
    return out


def _merge_results(
    distillation_rows: list[dict[str, Any]],
    other_rows: list[dict[str, Any]],
) -> dict[str, object]:
    """Merge distillations first, truncate bodies, add nudge if no distillations."""
    rows: list[dict[str, Any]] = []
    for row in distillation_rows:
        rows.append(_truncate_row(row))
    for row in other_rows:
        rows.append(_truncate_row(row))

    result: dict[str, object] = {
        "results": rows,
        "count": len(rows),
    }
    if not distillation_rows:
        result["nudge"] = _NUDGE
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    endpoint = resolve_endpoint()
    anon_key = resolve_anon_key()

    # Determine which kinds to query
    user_kinds = [k.strip() for k in args.kinds.split(",")] if args.kinds else None
    has_distillation = user_kinds is None or "distillation" in user_kinds
    has_others = user_kinds is None or any(k != "distillation" for k in user_kinds)

    distillation_rows: list[dict[str, Any]] = []
    other_rows: list[dict[str, Any]] = []

    # 1. Distillation query (only if user wants distillations)
    if has_distillation:
        dist_params = _build_params(args.query, args.kinds, args.sources, args.since, args.limit)
        dist_params["kind"] = "eq.distillation"
        try:
            distillation_rows = _query_feed(dist_params, endpoint=endpoint, anon_key=anon_key)
        except urllib.error.HTTPError as exc:
            output_json({"error": f"API error: {exc.code} {exc.reason}"}, args.out)
            return 2

    # 2. Non-distillation query
    if has_others:
        other_params = _build_params(args.query, args.kinds, args.sources, args.since, args.limit)
        if user_kinds:
            other_kinds = ",".join(k for k in user_kinds if k != "distillation")
            other_params["kind"] = f"in.({other_kinds})"
        else:
            other_params["kind"] = "neq.distillation"
        try:
            other_rows = _query_feed(other_params, endpoint=endpoint, anon_key=anon_key)
        except urllib.error.HTTPError as exc:
            output_json({"error": f"API error: {exc.code} {exc.reason}"}, args.out)
            return 2

    merged = _merge_results(distillation_rows, other_rows)
    output_json(merged, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
