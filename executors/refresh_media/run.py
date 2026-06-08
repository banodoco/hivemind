#!/usr/bin/env python3
"""Refresh expiring Discord CDN attachment URLs for a message."""

from __future__ import annotations

import argparse
import sys
import urllib.error
from typing import Any

# -- dual-import guard (T5 pattern) -------------------------------------------
try:
    from .._common import (
        output_json,
        public_edge_post,
        resolve_anon_key,
        resolve_refresh_media_url,
    )
except ImportError:
    import os as _os

    _HERE = _os.path.dirname(_os.path.abspath(__file__))
    _EXECUTORS = _os.path.dirname(_HERE)
    sys.path.insert(0, _EXECUTORS)
    from _common import (  # type: ignore[import-not-found]
        output_json,
        public_edge_post,
        resolve_anon_key,
        resolve_refresh_media_url,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hivemind.refresh_media",
        description="Refresh Discord CDN attachment URLs for a message.",
    )
    parser.add_argument(
        "--message-id",
        required=True,
        help="Discord message snowflake. Pass as a string; do not coerce to JSON number.",
    )
    parser.add_argument(
        "--out",
        help="Write JSON output to this file instead of stdout.",
    )
    return parser


def _validate_message_id(message_id: str) -> str:
    value = str(message_id).strip()
    if not value:
        raise ValueError("message_id must not be empty")
    if not value.isdigit():
        raise ValueError("message_id must contain only digits")
    return value


def refresh_media(
    message_id: str,
    *,
    refresh_url: str,
    anon_key: str,
) -> dict[str, Any]:
    """Call the refresh-media-urls edge function and return its JSON response."""
    snowflake = _validate_message_id(message_id)
    return public_edge_post(
        {"message_id": snowflake},
        url=refresh_url,
        anon_key=anon_key,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = refresh_media(
            args.message_id,
            refresh_url=resolve_refresh_media_url(),
            anon_key=resolve_anon_key(),
        )
    except ValueError as exc:
        output_json({"error": "validation_error", "detail": str(exc)}, args.out)
        return 2
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        output_json(
            {
                "error": "edge_function_error",
                "status": exc.code,
                "reason": exc.reason,
                "detail": detail,
            },
            args.out,
        )
        return 2

    output_json(result, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
