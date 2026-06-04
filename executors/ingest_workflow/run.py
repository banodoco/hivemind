#!/usr/bin/env python3
"""Parse a ComfyUI workflow JSON and submit it as a resource.

Extracts checkpoint / lora / vae model names from node widget values, the set
of non-core ``class_type`` values (custom nodes), and the node count; builds a
searchable text body; stores the full workflow JSON in ``payload``; and submits
as ``kind=workflow`` via the same code path as :mod:`hivemind.contribute`.
Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

# -- dual-import guard (T5 pattern) -------------------------------------------
try:
    from .._common import (
        build_add_resource_envelope,
        dry_run_output,
        edge_post,
        format_error,
        output_json,
        resolve_contributor_key,
    )
except ImportError:
    import os as _os

    _HERE = _os.path.dirname(_os.path.abspath(__file__))
    _EXECUTORS = _os.path.dirname(_HERE)
    sys.path.insert(0, _EXECUTORS)
    from _common import (  # type: ignore[import-not-found]
        build_add_resource_envelope,
        dry_run_output,
        edge_post,
        format_error,
        output_json,
        resolve_contributor_key,
    )


# ---------------------------------------------------------------------------
# Workflow parsing heuristics
# ---------------------------------------------------------------------------

# Core ComfyUI class_types — everything else is treated as a custom node.
# Kept deliberately broad: covers the default node pack shipped with ComfyUI.
_CORE_CLASS_TYPES: frozenset[str] = frozenset({
    "KSampler",
    "KSamplerAdvanced",
    "CheckpointLoaderSimple",
    "CheckpointLoader",
    "CLIPTextEncode",
    "CLIPSetLastLayer",
    "CLIPLoader",
    "VAEDecode",
    "VAEEncode",
    "VAEEncodeForInpaint",
    "VAELoader",
    "EmptyLatentImage",
    "LatentUpscale",
    "LatentUpscaleBy",
    "LoraLoader",
    "LoraLoaderModelOnly",
    "ControlNetLoader",
    "ControlNetApply",
    "ControlNetApplyAdvanced",
    "SaveImage",
    "PreviewImage",
    "LoadImage",
    "ImageScale",
    "ImageScaleBy",
    "UpscaleModelLoader",
    "ImageUpscaleWithModel",
    "ConditioningCombine",
    "ConditioningSetArea",
    "ConditioningConcat",
    "ModelMergeSimple",
    "UNETLoader",
    "DualCLIPLoader",
    "EmptySD3LatentImage",
    "Note",
    "PrimitiveNode",
    "Reroute",
})

# Substrings (lowercased) of widget values that look like model file names.
_MODEL_EXTENSIONS: tuple[str, ...] = (
    ".safetensors",
    ".ckpt",
    ".pt",
    ".pth",
    ".bin",
    ".gguf",
)

# class_type substrings whose widget string values we treat as model names.
_MODEL_BEARING_HINTS: tuple[str, ...] = (
    "checkpoint",
    "lora",
    "vae",
    "unet",
    "clip",
    "controlnet",
    "upscale",
    "loader",
)


def _iter_nodes(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a list of node dicts from either API-format or UI-format workflows.

    ComfyUI has two JSON shapes:

    * **API / prompt format** — a top-level object keyed by node id, each value
      a dict with ``class_type`` and ``inputs``.
    * **UI format** — a top-level object with a ``nodes`` array, each element a
      dict with ``type`` and ``widgets_values``.

    Both are normalised to ``[{"class_type": str, "widget_values": [...]}, ...]``.
    """
    nodes: list[dict[str, Any]] = []

    # UI format: {"nodes": [...]}
    if isinstance(workflow.get("nodes"), list):
        for node in workflow["nodes"]:
            if not isinstance(node, dict):
                continue
            class_type = node.get("type") or node.get("class_type") or ""
            widgets = node.get("widgets_values")
            values: list[Any] = []
            if isinstance(widgets, list):
                values = list(widgets)
            elif isinstance(widgets, dict):
                values = list(widgets.values())
            nodes.append({"class_type": str(class_type), "widget_values": values})
        return nodes

    # API / prompt format: {"<id>": {"class_type": ..., "inputs": {...}}}
    for key, node in workflow.items():
        if not isinstance(node, dict):
            continue
        if "class_type" not in node:
            continue
        class_type = node.get("class_type") or ""
        inputs = node.get("inputs")
        values = []
        if isinstance(inputs, dict):
            # Only scalar (non-link) values; links are [node_id, slot] lists.
            values = [v for v in inputs.values() if not isinstance(v, (list, dict))]
        nodes.append({"class_type": str(class_type), "widget_values": values})
    return nodes


def _looks_like_model(value: Any) -> bool:
    """Return True if *value* looks like a model file name."""
    if not isinstance(value, str):
        return False
    low = value.lower()
    return any(low.endswith(ext) for ext in _MODEL_EXTENSIONS)


def extract_models(nodes: list[dict[str, Any]]) -> list[str]:
    """Collect model file names from node widget values (deduped, ordered)."""
    seen: dict[str, None] = {}
    for node in nodes:
        class_low = node["class_type"].lower()
        model_bearing = any(hint in class_low for hint in _MODEL_BEARING_HINTS)
        for value in node["widget_values"]:
            if _looks_like_model(value):
                seen.setdefault(value, None)
            elif model_bearing and isinstance(value, str) and value.strip():
                # A loader node whose widget is a bare name (no extension),
                # e.g. some VAE/clip entries — keep if it has a model-ish shape.
                stripped = value.strip()
                if "/" in stripped or "\\" in stripped:
                    seen.setdefault(stripped, None)
    return list(seen.keys())


def extract_custom_nodes(nodes: list[dict[str, Any]]) -> list[str]:
    """Collect non-core ``class_type`` values (deduped, ordered)."""
    seen: dict[str, None] = {}
    for node in nodes:
        ct = node["class_type"]
        if ct and ct not in _CORE_CLASS_TYPES:
            seen.setdefault(ct, None)
    return list(seen.keys())


def summarize_node_types(nodes: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """Return ``(class_type, count)`` pairs sorted by descending count."""
    counts: dict[str, int] = {}
    for node in nodes:
        ct = node["class_type"] or "(unknown)"
        counts[ct] = counts.get(ct, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def build_body(
    name: str,
    nodes: list[dict[str, Any]],
    models: list[str],
    custom_nodes: list[str],
) -> str:
    """Build a searchable text description of the workflow."""
    type_summary = summarize_node_types(nodes)
    type_line = ", ".join(f"{ct} x{n}" for ct, n in type_summary)

    lines = [f"ComfyUI workflow: {name}"]
    lines.append(f"{len(nodes)} nodes.")
    if type_line:
        lines.append(f"Node types: {type_line}.")
    if models:
        lines.append("Models: " + ", ".join(models) + ".")
    if custom_nodes:
        lines.append("Custom nodes: " + ", ".join(custom_nodes) + ".")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_workflow_from_path(path: str) -> dict[str, Any]:
    """Read and parse a workflow JSON file. Raises on I/O or JSON errors."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.loads(fh.read())


def load_workflow_from_url(url: str) -> dict[str, Any]:
    """Fetch and parse a workflow JSON from *url*."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; HivemindIngest/2.0; "
                "+https://github.com/banodoco/hivemind)"
            ),
            "Accept": "application/json,*/*",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def derive_name(
    cli_title: str | None,
    workflow: dict[str, Any],
    source: str,
) -> str:
    """Resolve the workflow's display name.

    Priority: ``--title`` flag > a top-level ``name``/``title`` field in the
    workflow JSON > the source path/URL basename.
    """
    if cli_title and cli_title.strip():
        return cli_title.strip()
    for key in ("name", "title"):
        val = workflow.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    base = source.rstrip("/").rsplit("/", 1)[-1]
    return base or source


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hivemind.ingest_workflow",
        description="Parse a ComfyUI workflow JSON and submit as a resource.",
    )
    parser.add_argument(
        "--path", help="Path to a ComfyUI workflow JSON file."
    )
    parser.add_argument(
        "--url", help="URL to fetch a ComfyUI workflow JSON."
    )
    parser.add_argument(
        "--title", help="Override the workflow name/title."
    )
    parser.add_argument(
        "--kind", default="workflow", help="Resource kind (default: workflow)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and print the envelope without submitting.",
    )
    parser.add_argument(
        "--out", help="Write JSON output to this file instead of stdout."
    )
    return parser


def build_envelope(
    workflow: dict[str, Any],
    *,
    source_label: str,
    name: str,
    kind: str,
    url: str | None,
    external_id: str | None,
) -> dict[str, Any]:
    """Assemble the add_resource envelope for a parsed workflow."""
    nodes = _iter_nodes(workflow)
    models = extract_models(nodes)
    custom_nodes = extract_custom_nodes(nodes)
    body = build_body(name, nodes, models, custom_nodes)

    data: dict[str, Any] = {
        "kind": kind,
        "source": source_label,
        "title": name,
        "body": body,
        "metadata": {
            "models": models,
            "custom_nodes": custom_nodes,
            "node_count": len(nodes),
        },
        "payload": {"workflow": workflow},
    }
    if url:
        data["url"] = url
    if external_id:
        data["external_id"] = external_id
    return build_add_resource_envelope(data)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.path and not args.url:
        output_json(
            {"error": "either --path or --url is required"}, args.out
        )
        return 1
    if args.path and args.url:
        output_json(
            {"error": "provide only one of --path or --url"}, args.out
        )
        return 1

    # 1. Load
    try:
        if args.path:
            workflow = load_workflow_from_path(args.path)
            source_ref = args.path
        else:
            workflow = load_workflow_from_url(args.url)
            source_ref = args.url
    except FileNotFoundError:
        output_json({"error": f"file not found: {args.path}"}, args.out)
        return 1
    except (OSError, urllib.error.URLError) as exc:
        output_json({"error": f"failed to read workflow: {exc}"}, args.out)
        return 1
    except json.JSONDecodeError as exc:
        output_json({"error": f"invalid workflow JSON: {exc}"}, args.out)
        return 1

    if not isinstance(workflow, dict):
        output_json(
            {"error": "workflow JSON must be an object (got a non-object)"},
            args.out,
        )
        return 1

    # 2. Build envelope
    name = derive_name(args.title, workflow, source_ref)
    envelope = build_envelope(
        workflow,
        source_label="comfyui",
        name=name,
        kind=args.kind,
        url=args.url,
        external_id=args.url or args.path,
    )

    # 3. Dry-run path
    if args.dry_run:
        dry_run_output(envelope, args.out)
        return 0

    # 4. Real send — requires contributor key
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
        body: dict[str, Any] = {}
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            pass
        output_json({"error": format_error(exc.code, body)}, args.out)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
