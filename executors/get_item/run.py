#!/usr/bin/env python3
"""Fetch a single full row from the Hivemind corpus with cite context."""

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
    )

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hivemind.get_item",
        description="Fetch a single full row from the Hivemind corpus.",
    )
    parser.add_argument(
        "--kind",
        required=True,
        choices=["message", "resource", "distillation"],
        help="Item kind.",
    )
    parser.add_argument("--id", required=True, type=int, help="Item id.")
    parser.add_argument(
        "--out", help="Write JSON output to this file instead of stdout."
    )
    return parser


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------


def _fetch_row(
    kind: str,
    item_id: int,
    *,
    endpoint: str,
    anon_key: str,
) -> dict[str, Any] | None:
    """Fetch a single row from unified_feed by kind and item_id.

    The cite vocabulary uses ``resource`` for anything in external_resources,
    but unified_feed carries each resource's concrete kind (article, workflow,
    transcript, ...) — so ``resource`` maps to "not message/distillation".
    """
    if kind == "resource":
        kind_filter = "not.in.(message,distillation)"
    else:
        kind_filter = f"eq.{kind}"
    params = {
        "kind": kind_filter,
        "item_id": f"eq.{item_id}",
    }
    result = postgrest_get("unified_feed", params=params, endpoint=endpoint, anon_key=anon_key)
    if isinstance(result, list):
        return result[0] if result else None  # type: ignore[no-any-return]
    return result if result else None  # type: ignore[no-any-return]


def _fetch_distillation_cites(
    distillation_id: int,
    *,
    endpoint: str,
    anon_key: str,
) -> list[dict[str, Any]]:
    """Fetch cites *from* a distillation (what it references)."""
    params = {
        "distillation_id": f"eq.{distillation_id}",
    }
    result = postgrest_get("distillation_cites", params=params, endpoint=endpoint, anon_key=anon_key)
    if isinstance(result, list):
        return result  # type: ignore[no-any-return]
    return [result]  # type: ignore[list-item]


def _fetch_cited_by(
    item_kind: str,
    item_id: int,
    *,
    endpoint: str,
    anon_key: str,
) -> list[dict[str, Any]]:
    """Fetch distillations that cite a given message/resource.

    Returns a list of distillation rows (from unified_feed) that cite this item.
    """
    # Step 1: find which distillation_ids cite this item
    cite_params = {
        "item_kind": f"eq.{item_kind}",
        "item_id": f"eq.{item_id}",
    }
    cites = postgrest_get("distillation_cites", params=cite_params, endpoint=endpoint, anon_key=anon_key)
    if isinstance(cites, list):
        cite_rows = cites
    else:
        cite_rows = [cites]

    if not cite_rows:
        return []

    # Step 2: fetch those distillation rows from unified_feed
    dist_ids = sorted({row["distillation_id"] for row in cite_rows})
    if not dist_ids:
        return []

    # Fetch each distillation by id (in bulk if possible, one-by-one as fallback)
    distillations: list[dict[str, Any]] = []
    for dist_id in dist_ids:
        dist_params = {
            "item_kind": "eq.distillation",
            "item_id": f"eq.{dist_id}",
        }
        result = postgrest_get("unified_feed", params=dist_params, endpoint=endpoint, anon_key=anon_key)
        if isinstance(result, list):
            if result:
                distillations.append(result[0])
        elif result:
            distillations.append(result)
    return distillations


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def _assemble_result(
    kind: str,
    item_id: int,
    *,
    endpoint: str,
    anon_key: str,
) -> dict[str, Any]:
    """Fetch the row plus cite context and return the full result dict."""
    row = _fetch_row(kind, item_id, endpoint=endpoint, anon_key=anon_key)
    if row is None:
        return {"error": "not_found", "detail": f"No {kind} found with id {item_id}"}

    result: dict[str, Any] = {"item": dict(row)}

    if kind == "distillation":
        # Include what this distillation cites
        cites = _fetch_distillation_cites(item_id, endpoint=endpoint, anon_key=anon_key)
        result["cites"] = cites
    else:
        # Include distillations that cite this item
        cited_by = _fetch_cited_by(kind, item_id, endpoint=endpoint, anon_key=anon_key)
        result["cited_by"] = cited_by

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    endpoint = resolve_endpoint()
    anon_key = resolve_anon_key()

    try:
        assembled = _assemble_result(
            args.kind,
            args.id,
            endpoint=endpoint,
            anon_key=anon_key,
        )
    except urllib.error.HTTPError as exc:
        output_json({"error": f"API error: {exc.code} {exc.reason}"}, args.out)
        return 2

    output_json(assembled, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
