#!/usr/bin/env python3
"""Fetch a message or stable resource with exact revision/reference context."""
from __future__ import annotations
import argparse
import sys
import urllib.error
from typing import Any

try:
    from .._common import output_json, postgrest_get, resolve_anon_key, resolve_endpoint
except ImportError:
    import os as _os
    _HERE = _os.path.dirname(_os.path.abspath(__file__))
    _EXECUTORS = _os.path.dirname(_HERE)
    sys.path.insert(0, _EXECUTORS)
    from _common import output_json, postgrest_get, resolve_anon_key, resolve_endpoint  # type: ignore[import-not-found]

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hivemind.get_item", description="Fetch a full message or current resource.")
    p.add_argument("--kind", required=True, choices=["message", "resource", "revision"])
    p.add_argument("--id", required=True, help="Positive decimal id.")
    p.add_argument("--revision-id", help="For a resource, fetch this exact pinned revision.")
    p.add_argument("--out")
    return p

def _one(result: Any) -> dict[str, Any] | None:
    if isinstance(result, list): return result[0] if result else None
    return result if isinstance(result, dict) and result else None

def _fetch_row(kind: str, item_id: int | str, *, endpoint: str, anon_key: str, revision_id: int | str | None = None) -> dict[str, Any] | None:
    ident = str(item_id)
    if kind == "message":
        return _one(postgrest_get("message_feed", params={"message_id": f"eq.{ident}"}, endpoint=endpoint, anon_key=anon_key))
    if kind == "revision":
        return _one(postgrest_get("resource_revisions", params={"id": f"eq.{ident}"}, endpoint=endpoint, anon_key=anon_key))
    resource = _one(postgrest_get("resources", params={"id": f"eq.{ident}"}, endpoint=endpoint, anon_key=anon_key))
    if not resource: return None
    rid = str(revision_id or resource.get("current_revision_id") or "")
    if not rid: return {"resource_id": ident, "current_revision_id": None, "unreviewed": True}
    rev = _one(postgrest_get("resource_revisions", params={"id": f"eq.{rid}", "resource_id": f"eq.{ident}"}, endpoint=endpoint, anon_key=anon_key))
    if not rev: return None
    if revision_id is None and (rev.get("state") != "accepted" or str(resource.get("current_revision_id")) != str(rev.get("id"))):
        return {"resource_id": ident, "current_revision_id": resource.get("current_revision_id"), "unreviewed": True}
    return {"kind": rev.get("kind"), "source": resource.get("origin_source"), "item_id": ident,
            "title": rev.get("title"), "body": rev.get("body"), "metadata": rev.get("metadata"),
            "provenance": rev.get("provenance"), "payload": rev.get("payload"),
            "created_at": resource.get("created_at"), "revision_id": str(rev.get("id")),
            "state": rev.get("state"), "current_revision_id": str(resource.get("current_revision_id")) if resource.get("current_revision_id") is not None else None,
            "canonical_guide": resource.get("canonical_guide", False), "unreviewed": rev.get("state") != "accepted"}

def _fetch_cited_by(item_kind: str, item_id: int | str, *, endpoint: str, anon_key: str) -> list[dict[str, Any]]:
    rows = postgrest_get("knowledge_backlinks", params={"linked_kind": f"eq.{item_kind}", "linked_id": f"eq.{item_id}"}, endpoint=endpoint, anon_key=anon_key)
    return rows if isinstance(rows, list) else ([rows] if isinstance(rows, dict) and rows else [])

def _fetch_outgoing(source_kind: str, source_id: int | str, *, endpoint: str, anon_key: str) -> list[dict[str, Any]]:
    rows = postgrest_get("knowledge_outgoing_references", params={"source_kind": f"eq.{source_kind}", "source_id": f"eq.{source_id}"}, endpoint=endpoint, anon_key=anon_key)
    return rows if isinstance(rows, list) else ([rows] if isinstance(rows, dict) and rows else [])

def _assemble_result(kind: str, item_id: int | str, *, endpoint: str, anon_key: str, revision_id: int | str | None = None) -> dict[str, Any]:
    row = _fetch_row(kind, item_id, endpoint=endpoint, anon_key=anon_key, revision_id=revision_id)
    if row is None: return {"error": "not_found", "detail": f"No {kind} found with id {item_id}"}
    result: dict[str, Any] = {"item": dict(row)}
    if kind == "revision": result["references"] = _fetch_outgoing("revision", item_id, endpoint=endpoint, anon_key=anon_key)
    elif kind == "resource":
        result["cited_by"] = _fetch_cited_by("resource", item_id, endpoint=endpoint, anon_key=anon_key)
        if row.get("revision_id"): result["references"] = _fetch_outgoing("revision", row["revision_id"], endpoint=endpoint, anon_key=anon_key)
    else: result["cited_by"] = _fetch_cited_by("message", item_id, endpoint=endpoint, anon_key=anon_key)
    return result

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try: result = _assemble_result(args.kind, args.id, endpoint=resolve_endpoint(), anon_key=resolve_anon_key(), revision_id=args.revision_id)
    except urllib.error.HTTPError as exc:
        output_json({"error": f"API error: {exc.code} {exc.reason}"}, args.out); return 2
    output_json(result, args.out); return 0

if __name__ == "__main__": raise SystemExit(main())
