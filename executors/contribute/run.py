#!/usr/bin/env python3
"""Submit and review immutable resources/evidence via the contribute edge function."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
from typing import Any

# -- dual-import guard (T5 pattern) -------------------------------------------
try:
    from .._common import (
        build_knowledge_model_envelope,
        dry_run_output,
        edge_post,
        format_error,
        output_json,
        read_body_file,
        resolve_contributor_key,
    )
except ImportError:
    import os as _os

    _HERE = _os.path.dirname(_os.path.abspath(__file__))
    _EXECUTORS = _os.path.dirname(_HERE)
    sys.path.insert(0, _EXECUTORS)
    from _common import (  # type: ignore[import-not-found]
        build_knowledge_model_envelope,
        dry_run_output,
        edge_post,
        format_error,
        output_json,
        read_body_file,
        resolve_contributor_key,
    )

try:
    from ..references import parse_references
except ImportError:
    from references import parse_references  # type: ignore[import-not-found]

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hivemind.contribute",
        description="Submit and review immutable resources and evidence via the contribute edge function.",
    )
    parser.add_argument(
        "--type",
        required=True,
        choices=[
            "submit-resource", "propose-revision",
            "decide-revision", "mark-canonical", "capture-message", "evidence",
        ],
        help="Submission type.",
    )
    # Resource revision fields
    parser.add_argument("--kind", help="Resource kind.")
    parser.add_argument("--title", help="Resource title.")
    parser.add_argument("--body-file", help="File containing the body text.")
    parser.add_argument("--source", help="Source label.")
    parser.add_argument("--external-id", help="Stable source-side identity for a resource.")
    parser.add_argument("--url", help="Source URL.")
    parser.add_argument("--author", help="Author name.")
    parser.add_argument("--conditions", help="Conditions / caveats string.")
    # Knowledge-model fields (all IDs stay strings at JSON boundaries).
    parser.add_argument("--idempotency-token", help="Caller-scoped retry token (required for knowledge-model writes).")
    parser.add_argument("--resource-id", help="Stable resource ID.")
    parser.add_argument("--base-revision-id", help="Accepted head used as the revision base.")
    parser.add_argument("--revision-id", help="Revision ID to decide.")
    parser.add_argument("--decision", choices=["accepted", "rejected", "withdrawn"])
    parser.add_argument("--canonical", action="store_true", help="Mark a guide as canonical.")
    parser.add_argument("--uncanonical", action="store_true", help="Remove the canonical-guide marker.")
    parser.add_argument("--payload-file", help="JSON file containing the native workflow payload.")
    parser.add_argument("--metadata-file", help="JSON file containing revision metadata.")
    parser.add_argument("--provenance-file", help="JSON file containing revision provenance.")
    parser.add_argument("--references-file", help="JSON array of typed references.")
    parser.add_argument("--claim", help="Evidence claim.")
    parser.add_argument("--reported-result", help="Evidence reported result.")
    parser.add_argument("--basis", choices=["reported", "observed"])
    parser.add_argument("--subjects-file", help="JSON array of exact evidence subjects.")
    parser.add_argument("--sources-file", help="JSON array of evidence sources or ordinary URLs.")
    parser.add_argument("--supersedes-evidence", help="Evidence ID being corrected/superseded.")
    parser.add_argument("--message-id", help="Source message ID for a trusted snapshot.")
    parser.add_argument("--content", help="Observed message content.")
    parser.add_argument("--source-metadata-file", help="JSON object of observed message metadata.")
    parser.add_argument("--original-author-id", help="Original message author ID.")
    parser.add_argument("--original-author-name", help="Original message author name.")
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


def _json_file(path: str | None, default: Any) -> Any:
    if not path:
        return default
    import json

    return json.loads(read_body_file(path))


def _build_knowledge_model_envelope(args: argparse.Namespace) -> dict[str, Any]:
    """Build one of the atomic T2-T6 RPC envelopes."""
    action_by_type = {
        "submit-resource": "submit_resource",
        "propose-revision": "propose_revision",
        "decide-revision": "decide_revision",
        "mark-canonical": "mark_canonical",
        "capture-message": "capture_message_snapshot",
        "evidence": "submit_evidence",
    }
    action = action_by_type[args.type]
    token = getattr(args, "idempotency_token", None)
    if not token:
        raise ValueError("--idempotency-token is required for knowledge-model writes")
    data: dict[str, Any] = {"idempotency_token": token}
    if action == "submit_resource":
        body = read_body_file(args.body_file) if args.body_file else ""
        data.update({
            "kind": args.kind or "",
            "title": args.title or "",
            "body": body,
            "metadata": _json_file(getattr(args, "metadata_file", None), {}),
            "provenance": _json_file(getattr(args, "provenance_file", None), {}),
            "references": _json_file(getattr(args, "references_file", None), []) + [
                {key: value for key, value in ref.items() if key != "labelled" and value is not None}
                for ref in parse_references(body)
            ],
        })
        payload = _json_file(getattr(args, "payload_file", None), None)
        if payload is not None:
            data["payload"] = payload
        if args.source:
            data["origin_source"] = args.source
        if getattr(args, "external_id", None):
            data["origin_external_id"] = args.external_id
        if args.conditions:
            data["rationale"] = args.conditions
    elif action == "propose_revision":
        body = read_body_file(args.body_file) if args.body_file else ""
        data.update({
            "resource_id": args.resource_id or "",
            "base_revision_id": args.base_revision_id or "",
            "kind": args.kind or "",
            "title": args.title or "",
            "body": body,
            "metadata": _json_file(getattr(args, "metadata_file", None), {}),
            "provenance": _json_file(getattr(args, "provenance_file", None), {}),
            "references": _json_file(getattr(args, "references_file", None), []) + [
                {key: value for key, value in ref.items() if key != "labelled" and value is not None}
                for ref in parse_references(body)
            ],
        })
        payload = _json_file(getattr(args, "payload_file", None), None)
        if payload is not None:
            data["payload"] = payload
    elif action == "decide_revision":
        data.update({"revision_id": args.revision_id or "", "decision": args.decision or ""})
        if args.conditions:
            data["reason"] = args.conditions
    elif action == "mark_canonical":
        data.update({"resource_id": args.resource_id or "", "canonical": not args.uncanonical})
    elif action == "capture_message_snapshot":
        data.update({
            "message_id": args.message_id or "",
            "content": args.content or "",
            "source_metadata": _json_file(getattr(args, "source_metadata_file", None), {}),
        })
        if args.original_author_id:
            data["original_author_id"] = args.original_author_id
        if args.original_author_name:
            data["original_author_name"] = args.original_author_name
    else:
        data.update({
            "claim": args.claim or "",
            "conditions": args.conditions,
            "reported_result": args.reported_result or "",
            "basis": args.basis or "",
            "subjects": _json_file(getattr(args, "subjects_file", None), []),
            "sources": _json_file(getattr(args, "sources_file", None), []),
        })
        if args.supersedes_evidence:
            data["supersedes_evidence_id"] = args.supersedes_evidence
    return build_knowledge_model_envelope(action, data)


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
    if args.type in {"submit-resource", "propose-revision", "decide-revision", "mark-canonical", "capture-message", "evidence"}:
        try:
            envelope = _build_knowledge_model_envelope(args)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            output_json({"error": str(exc)}, args.out)
            return 1
    else:  # argparse choices make this unreachable
        raise ValueError("unsupported contribution type")

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
