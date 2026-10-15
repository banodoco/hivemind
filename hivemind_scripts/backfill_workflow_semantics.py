#!/usr/bin/env python3
"""Backfill workflow_semantics metadata for VibeComfy Hivemind resources."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from executors.workflow_semantics import enrich_resource_data  # noqa: E402


DEFAULT_API_URL = "https://ujlwuvkrxlvoswwkerdf.supabase.co/rest/v1"
DEFAULT_ANON_KEY = "sb_publishable_O38oPBafrBoFrpi_rlWJvA_UJrulFsx"
DEFAULT_SOURCES = ("vibecomfy", "vibecomfy-external")


def _api_url_from_env() -> str:
    explicit = os.environ.get("HIVEMIND_API_URL")
    if explicit:
        return explicit.rstrip("/")
    supabase_url = os.environ.get("SUPABASE_URL")
    if supabase_url:
        return supabase_url.rstrip("/") + "/rest/v1"
    return DEFAULT_API_URL


def _auth_headers(key: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "apikey": key,
        "Authorization": f"Bearer {key}",
    }


def _request_json(
    url: str,
    *,
    key: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> Any:
    headers = _auth_headers(key)
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        headers["Prefer"] = "return=representation"
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=120) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw else None


def _source_filter(sources: list[str]) -> str:
    escaped = [source.replace('"', '\\"') for source in sources]
    return "in.(" + ",".join(f'"{source}"' for source in escaped) + ")"


def fetch_rows(
    *,
    api_url: str,
    anon_key: str,
    sources: list[str],
    limit: int | None,
    page_size: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while limit is None or len(rows) < limit:
        batch_limit = page_size if limit is None else min(page_size, limit - len(rows))
        params = {
            "select": "id,kind,source,external_id,title,body,url,metadata,payload",
            "kind": "eq.workflow",
            "source": _source_filter(sources),
            "order": "id.asc",
            "limit": str(batch_limit),
            "offset": str(offset),
        }
        url = f"{api_url.rstrip('/')}/external_resources?{urllib.parse.urlencode(params)}"
        parsed = _request_json(url, key=anon_key)
        batch = parsed if isinstance(parsed, list) else []
        rows.extend(row for row in batch if isinstance(row, dict))
        if len(batch) < batch_limit:
            break
        offset += len(batch)
    return rows


def _needs_backfill(row: dict[str, Any], *, force: bool) -> bool:
    if force:
        return True
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    return metadata.get("workflow_semantics_version") != 1 or not isinstance(metadata.get("workflow_semantics"), dict)


def _resource_data_from_row(row: dict[str, Any], *, vibecomfy_root: Path | None = None) -> dict[str, Any]:
    data = {
        "kind": row.get("kind") or "workflow",
        "source": row.get("source"),
        "external_id": row.get("external_id"),
        "title": row.get("title") or "",
        "body": row.get("body") or "",
        "metadata": row.get("metadata") if isinstance(row.get("metadata"), dict) else {},
        "payload": row.get("payload") if isinstance(row.get("payload"), dict) else {},
    }
    _attach_local_workflow_json(data, row=row, vibecomfy_root=vibecomfy_root)
    return enrich_resource_data(data)


def _attach_local_workflow_json(
    data: dict[str, Any],
    *,
    row: dict[str, Any],
    vibecomfy_root: Path | None,
) -> None:
    payload = data["payload"] if isinstance(data.get("payload"), dict) else {}
    if isinstance(payload.get("workflow_json"), dict) or isinstance(payload.get("workflow"), dict):
        return
    corpus_path = (
        payload.get("corpus_path")
        or _nested_get(data.get("metadata"), ("corpus_path",))
        or _nested_get(data.get("metadata"), ("provenance", "corpus_path"))
    )
    if not isinstance(corpus_path, str) or not corpus_path:
        url = row.get("url")
        if isinstance(url, str) and url.startswith("file://"):
            corpus_path = url.removeprefix("file://")
    if not isinstance(corpus_path, str) or not corpus_path:
        return
    candidates = [Path(corpus_path)]
    if vibecomfy_root is not None:
        candidates.append(vibecomfy_root / corpus_path)
    for candidate in candidates:
        if not candidate.exists() or not candidate.is_file():
            continue
        try:
            workflow_json = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(workflow_json, dict):
            return
        payload["workflow_json"] = workflow_json
        compiled_api = workflow_json.get("compiled_api")
        if isinstance(compiled_api, dict):
            payload["compiled_api"] = compiled_api
        data["payload"] = payload
        return


def _nested_get(container: Any, keys: tuple[str, ...]) -> Any:
    value = container
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _patch_row(*, api_url: str, service_role_key: str, row_id: int, update: dict[str, Any]) -> Any:
    params = urllib.parse.urlencode({"id": f"eq.{row_id}"})
    url = f"{api_url.rstrip('/')}/external_resources?{params}"
    return _request_json(url, key=service_role_key, method="PATCH", payload=update)


def _brief_for_uncertain(row: dict[str, Any], enriched: dict[str, Any]) -> str | None:
    semantics = enriched.get("metadata", {}).get("workflow_semantics", {})
    if not isinstance(semantics, dict):
        return None
    evidence = semantics.get("evidence") if isinstance(semantics.get("evidence"), dict) else {}
    uncertain = (
        semantics.get("media_type") == "unknown"
        or semantics.get("task_type") == "unknown"
        or evidence.get("confidence") == "low"
    )
    if not uncertain:
        return None
    sample = {
        "id": row.get("id"),
        "source": row.get("source"),
        "external_id": row.get("external_id"),
        "title": row.get("title"),
        "body_excerpt": str(row.get("body") or "")[:2500],
        "metadata": row.get("metadata"),
        "current_semantics": semantics,
    }
    return (
        "Classify this Hivemind VibeComfy workflow resource. Return only JSON with "
        "media_type, task_type, model_families, searchable_aliases, confidence, and rationale.\n\n"
        + json.dumps(sample, indent=2, sort_keys=True)
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = fetch_rows(
        api_url=args.api_url,
        anon_key=args.anon_key,
        sources=args.source or list(DEFAULT_SOURCES),
        limit=args.limit,
        page_size=args.page_size,
    )
    sample_path = out_dir / "backfill_sample.jsonl"
    briefs_dir = out_dir / "llm_briefs"
    if args.write_llm_briefs:
        briefs_dir.mkdir(parents=True, exist_ok=True)

    counts: dict[str, int] = {
        "fetched": len(rows),
        "already_enriched": 0,
        "would_update": 0,
        "updated": 0,
        "llm_briefs": 0,
        "errors": 0,
    }
    with sample_path.open("w", encoding="utf-8") as sample_file:
        for row in rows:
            if not _needs_backfill(row, force=args.force):
                counts["already_enriched"] += 1
                continue
            try:
                enriched = _resource_data_from_row(row, vibecomfy_root=args.vibecomfy_root)
                update = {
                    "metadata": enriched["metadata"],
                    "body": enriched["body"],
                }
                sample_file.write(
                    json.dumps(
                        {
                            "id": row.get("id"),
                            "source": row.get("source"),
                            "external_id": row.get("external_id"),
                            "workflow_semantics": enriched["metadata"]["workflow_semantics"],
                            "body_changed": row.get("body") != enriched["body"],
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
                brief = _brief_for_uncertain(row, enriched)
                if brief and args.write_llm_briefs:
                    (briefs_dir / f"row_{row.get('id')}.md").write_text(brief, encoding="utf-8")
                    counts["llm_briefs"] += 1
                if args.apply:
                    if not args.service_role_key:
                        raise RuntimeError("--apply requires SUPABASE_SERVICE_ROLE_KEY or HIVEMIND_SERVICE_ROLE_KEY")
                    _patch_row(
                        api_url=args.api_url,
                        service_role_key=args.service_role_key,
                        row_id=int(row["id"]),
                        update=update,
                    )
                    counts["updated"] += 1
                else:
                    counts["would_update"] += 1
            except Exception as exc:  # noqa: BLE001
                counts["errors"] += 1
                sample_file.write(
                    json.dumps(
                        {
                            "id": row.get("id"),
                            "source": row.get("source"),
                            "external_id": row.get("external_id"),
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
    summary = {
        "mode": "apply" if args.apply else "dry_run",
        "api_url": args.api_url,
        "sources": args.source or list(DEFAULT_SOURCES),
        "counts": counts,
        "sample_path": str(sample_path),
        "llm_briefs_dir": str(briefs_dir) if args.write_llm_briefs else None,
    }
    (out_dir / "backfill_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default=_api_url_from_env(), help="Supabase PostgREST API URL")
    parser.add_argument("--anon-key", default=os.environ.get("HIVEMIND_ANON_KEY", DEFAULT_ANON_KEY), help="Anon key for dry-run reads")
    parser.add_argument(
        "--service-role-key",
        default=os.environ.get("HIVEMIND_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY"),
        help="Service role key for --apply PATCH updates",
    )
    parser.add_argument("--source", action="append", default=None, help="Workflow source to backfill; repeatable")
    parser.add_argument("--limit", type=int, default=None, help="Maximum rows to inspect; default inspects all matching rows")
    parser.add_argument("--page-size", type=int, default=100, help="Rows per PostgREST page")
    parser.add_argument("--force", action="store_true", help="Recompute semantics even when present")
    parser.add_argument("--apply", action="store_true", help="PATCH rows; default is dry-run only")
    parser.add_argument("--write-llm-briefs", action="store_true", help="Write DeepSeek-ready briefs for low-confidence rows")
    parser.add_argument(
        "--vibecomfy-root",
        type=Path,
        default=Path(os.environ["VIBECOMFY_ROOT"]).resolve() if os.environ.get("VIBECOMFY_ROOT") else None,
        help="Optional VibeComfy checkout root used to resolve payload.corpus_path for old rows",
    )
    parser.add_argument("--out-dir", type=Path, default=Path(".codex_tmp/semantic_backfill"), help="Output directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run(args)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["counts"]["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
