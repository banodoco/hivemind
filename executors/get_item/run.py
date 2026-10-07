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


def _parse_item_id(value: str) -> str:
    """Validate a decimal id without coercing it through a numeric type.

    Discord snowflakes are larger than JavaScript's safe integer range.  Keep
    every id as text from argv through the PostgREST filter, while still
    accepting ordinary numeric ids such as resource ``42``.
    """
    if not value.isdigit():
        raise argparse.ArgumentTypeError("item id must be a decimal integer")
    return value


def _item_id_text(item_id: str | int) -> str:
    """Return the caller's id as text, preserving large and numeric ids."""
    if isinstance(item_id, bool):
        raise ValueError("item id must be a decimal integer")
    value = str(item_id)
    if not value.isdigit():
        raise ValueError("item id must be a decimal integer")
    return value


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
    parser.add_argument(
        "--id",
        required=True,
        type=_parse_item_id,
        help="Decimal item id; kept as text for Discord snowflakes.",
    )
    parser.add_argument(
        "--out", help="Write JSON output to this file instead of stdout."
    )
    return parser


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------


def _fetch_row(
    kind: str,
    item_id: str | int,
    *,
    endpoint: str,
    anon_key: str,
) -> dict[str, Any] | None:
    """Fetch a single row from unified_feed by kind and item_id.

    The cite vocabulary uses ``resource`` for anything in external_resources,
    but unified_feed carries each resource's concrete kind (article, workflow,
    transcript, ...) — so ``resource`` maps to "not message/distillation".
    """
    item_id = _item_id_text(item_id)
    if kind == "message":
        # message_feed exposes the indexed Discord bigint directly.  Keep the
        # snowflake quoted in the query and adapt its narrow row to the common
        # unified-feed shape below; searching unified_feed for this row is
        # needlessly expensive and can lose the identifier at JSON clients.
        params = {
            "select": "message_id,content,author_name,channel_name,channel_id,guild_id,created_at,reactions",
            "message_id": f"eq.{item_id}",
        }
        result = postgrest_get("message_feed", params=params, endpoint=endpoint, anon_key=anon_key)
        rows = result if isinstance(result, list) else [result]
        if not rows or not rows[0]:
            return None
        row = dict(rows[0])
        message_id = str(row.get("message_id", item_id))
        guild_id = row.get("guild_id")
        channel_id = row.get("channel_id")
        url = (
            f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
            if guild_id is not None and channel_id is not None
            else None
        )
        row.update(
            {
                "kind": "message",
                "source": "banodoco-discord",
                "item_id": message_id,
                "title": None,
                "body": row.get("content", ""),
                "author": row.get("author_name"),
                "context": row.get("channel_name"),
                "url": url,
            }
        )
        return row
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
    distillation_id: str | int,
    *,
    endpoint: str,
    anon_key: str,
) -> list[dict[str, Any]]:
    """Fetch cites *from* a distillation (what it references)."""
    distillation_id = _item_id_text(distillation_id)
    params = {"distillation_id": f"eq.{distillation_id}"}
    result = postgrest_get("distillation_cites", params=params, endpoint=endpoint, anon_key=anon_key)
    if isinstance(result, list):
        return result  # type: ignore[no-any-return]
    return [result]  # type: ignore[list-item]


def _fetch_cited_by(
    item_kind: str,
    item_id: str | int,
    *,
    endpoint: str,
    anon_key: str,
) -> list[dict[str, Any]]:
    """Fetch distillations that cite a given message/resource.

    Returns a list of distillation rows (from unified_feed) that cite this item.
    """
    item_id = _item_id_text(item_id)
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
        dist_params = {"kind": "eq.distillation", "item_id": f"eq.{dist_id}"}
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
    item_id: str | int,
    *,
    endpoint: str,
    anon_key: str,
) -> dict[str, Any]:
    """Fetch the row plus cite context and return the full result dict."""
    item_id = _item_id_text(item_id)
    row = _fetch_row(kind, item_id, endpoint=endpoint, anon_key=anon_key)
    if row is None:
        return {"error": "not_found", "detail": f"No {kind} found with id {item_id}"}

    result: dict[str, Any] = {"item": dict(row)}

    # Citation context is ancillary to the item row.  Some deployed corpus
    # snapshots do not expose the citation relation; do not turn a successful
    # full-row lookup into a 404 merely because that optional context is absent.
    try:
        if kind == "distillation":
            # Include what this distillation cites
            result["cites"] = _fetch_distillation_cites(
                item_id, endpoint=endpoint, anon_key=anon_key
            )
        else:
            # Include distillations that cite this item
            result["cited_by"] = _fetch_cited_by(
                kind, item_id, endpoint=endpoint, anon_key=anon_key
            )
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        context_key = "cites" if kind == "distillation" else "cited_by"
        result[context_key] = []
        result["references_unavailable"] = {"status": 404, "kind": kind}

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
