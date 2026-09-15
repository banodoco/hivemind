#!/usr/bin/env python3
"""Submit a resource or distillation to Hivemind via the contribute edge function."""

from __future__ import annotations

import argparse
import sys
import urllib.error
from typing import Any

# -- dual-import guard (T5 pattern) -------------------------------------------
try:
    from .._common import (
        build_add_resource_envelope,
        build_submit_distillation_envelope,
        dry_run_output,
        edge_post,
        format_error,
        output_json,
        parse_cites,
        read_body_file,
        resolve_contribute_url,
        resolve_contributor_key,
    )
except ImportError:
    import os as _os

    _HERE = _os.path.dirname(_os.path.abspath(__file__))
    _EXECUTORS = _os.path.dirname(_HERE)
    sys.path.insert(0, _EXECUTORS)
    from _common import (  # type: ignore[import-not-found]
        build_add_resource_envelope,
        build_submit_distillation_envelope,
        dry_run_output,
        edge_post,
        format_error,
        output_json,
        parse_cites,
        read_body_file,
        resolve_contribute_url,
        resolve_contributor_key,
    )

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hivemind.contribute",
        description="Submit a resource or distillation to the Hivemind corpus.",
    )
    parser.add_argument(
        "--type",
        required=True,
        choices=["resource", "distillation"],
        help="Submission type.",
    )
    # Resource fields
    parser.add_argument("--kind", help="Resource kind (for --type resource).")
    parser.add_argument("--title", help="Resource title.")
    parser.add_argument("--body-file", help="File containing the body text.")
    parser.add_argument("--source", help="Source label.")
    parser.add_argument("--url", help="Source URL.")
    parser.add_argument("--author", help="Author name.")
    # Distillation fields
    parser.add_argument("--question", help="The question being answered.")
    parser.add_argument("--answer", help="The answer text.")
    parser.add_argument(
        "--confidence",
        choices=["high", "medium", "low"],
        help="Confidence level.",
    )
    parser.add_argument(
        "--cites",
        help="Comma-separated cites (kind:id, e.g. message:88123,resource:17).",
    )
    parser.add_argument(
        "--supersedes", type=int, help="ID of distillation this supersedes."
    )
    parser.add_argument("--conditions", help="Conditions / caveats string.")
    # Shared
    parser.add_argument("--from-file", help="Read full JSON payload from a file.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print request envelope without sending.",
    )
    parser.add_argument(
        "--out", help="Write JSON output to this file instead of stdout."
    )
    return parser


# ---------------------------------------------------------------------------
# Envelope assembly
# ---------------------------------------------------------------------------


def _build_resource_data(args: argparse.Namespace) -> dict[str, Any]:
    """Build the data dict for an add_resource envelope from CLI args."""
    data: dict[str, Any] = {
        "kind": args.kind or "unknown",
        "source": args.source or "cli",
        "title": args.title or "",
        "body": "",
    }
    if args.body_file:
        data["body"] = read_body_file(args.body_file)
    if args.url:
        data["url"] = args.url
    if args.author:
        data["author"] = args.author
    return data


def _build_distillation_data(args: argparse.Namespace) -> dict[str, Any]:
    """Build the data dict for a submit_distillation envelope from CLI args."""
    data: dict[str, Any] = {
        "question": args.question or "",
        "answer": args.answer or "",
        "confidence": args.confidence or "medium",
        "cites": [],
    }
    if args.cites:
        data["cites"] = parse_cites(args.cites)
    if args.supersedes is not None:
        data["supersedes_id"] = args.supersedes
    if args.conditions:
        data["conditions"] = args.conditions
    return data


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # --from-file: read full payload directly
    if args.from_file:
        import json as _json

        try:
            raw = read_body_file(args.from_file)
            envelope = _json.loads(raw)
        except (FileNotFoundError, OSError) as exc:
            output_json({"error": f"cannot read from-file: {exc}"}, args.out)
            return 1
        except _json.JSONDecodeError as exc:
            output_json({"error": f"invalid JSON in from-file: {exc}"}, args.out)
            return 1

        if args.dry_run:
            dry_run_output(envelope, args.out)
            return 0

        # Real send with from-file — requires contributor key
        contributor_key = resolve_contributor_key()
        if not contributor_key:
            output_json(
                {
                    "error": "contributor key required",
                    "detail": "set HIVEMIND_CONTRIBUTOR_KEY or use --dry-run",
                },
                args.out,
            )
            return 1

        try:
            response = edge_post(envelope, contributor_key=contributor_key)
            output_json(response, args.out)
            return 0
        except urllib.error.HTTPError as exc:
            body = {}
            try:
                body = _json.loads(exc.read().decode("utf-8"))
            except Exception:
                pass
            output_json({"error": format_error(exc.code, body)}, args.out)
            return 1
        return 0

    # Build envelope from CLI args
    if args.type == "resource":
        data = _build_resource_data(args)
        envelope = build_add_resource_envelope(data)
    else:  # distillation
        data = _build_distillation_data(args)
        envelope = build_submit_distillation_envelope(data)

    # --dry-run: print envelope without sending (no key needed)
    if args.dry_run:
        dry_run_output(envelope, args.out)
        return 0

    # Real send — requires contributor key
    contributor_key = resolve_contributor_key()
    if not contributor_key:
        output_json(
            {
                "error": "contributor key required",
                "detail": "set HIVEMIND_CONTRIBUTOR_KEY or use --dry-run",
            },
            args.out,
        )
        return 1

    try:
        response = edge_post(envelope, contributor_key=contributor_key)
        output_json(response, args.out)
        return 0
    except urllib.error.HTTPError as exc:
        import json as _json

        body = {}
        try:
            body = _json.loads(exc.read().decode("utf-8"))
        except Exception:
            pass
        output_json({"error": format_error(exc.code, body)}, args.out)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
