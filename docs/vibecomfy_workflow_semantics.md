# VibeComfy Workflow Semantics

VibeComfy-owned workflow resources in `external_resources` must include structured workflow semantics in `metadata`.

This applies to:

- `kind = "workflow"`
- `source in ("vibecomfy", "vibecomfy-external")`

## Required Metadata

```json
{
  "workflow_semantics_version": 1,
  "workflow_semantics": {
    "media_type": "image|video|audio|3d|multi|unknown",
    "task_type": "text_to_image|image_to_image|image_to_video|text_to_video|video_to_video|audio_to_video|controlnet|compositing|inpainting|upscale|other|unknown",
    "model_families": ["ltx", "wan"],
    "adapter_directions": [
      {"from": ["image"], "to": "video", "confidence": "deterministic"}
    ],
    "node_types": ["LoadImage", "VHS_VideoCombine"],
    "node_class_multiset": {"LoadImage": 1, "VHS_VideoCombine": 1},
    "custom_nodes": ["VHS_VideoCombine"],
    "models": ["ltxv-i2v.safetensors"],
    "searchable_aliases": ["ltx", "ltxv", "i2v"],
    "evidence": {
      "derived_from": ["workflow_json", "compiled_api", "summary"],
      "confidence": "high"
    },
    "promotion_gates": {
      "has_workflow_json": true,
      "has_compiled_api": true,
      "has_python_source": false,
      "parseable_workflow": true
    }
  }
}
```

Use `unknown` or an empty list when evidence is weak. Do not invent a model family or task type from prose alone when no alias or structured field supports it.

The edge validation allows an explicit migration escape hatch with `metadata.workflow_semantics_bypass` set to `"legacy"` or `"backfill"`, but new VibeComfy uploads should not use it.

## Extraction Sources

Deterministic extraction should be preferred over LLM classification:

- `metadata.media_type`, `metadata.summary.media_type`, `metadata.task_type`, `metadata.summary.task_type`
- `metadata.models`, `metadata.custom_nodes`, `metadata.model_family`
- `payload.workflow_json`, `payload.workflow`, `payload.compiled_api`
- `metadata.node_class_multiset`, `metadata.provenance.node_class_multiset`
- title/body/tags/model filenames/node class names for aliases

The uploaders also duplicate compact semantic fields into `body` as `Workflow semantics: ...` so older title/body-only search still works.

## VibeComfy Query Examples

Find LTX workflows:

```http
GET /rest/v1/external_resources?kind=eq.workflow&metadata=cs.{"workflow_semantics":{"model_families":["ltx"]}}
```

Find image-to-video workflows:

```http
GET /rest/v1/external_resources?kind=eq.workflow&metadata=cs.{"workflow_semantics":{"task_type":"image_to_video"}}
```

Find rows that have compiled API available:

```http
GET /rest/v1/external_resources?kind=eq.workflow&metadata=cs.{"workflow_semantics":{"promotion_gates":{"has_compiled_api":true}}}
```

Recommended VibeComfy ranking:

- Promote rows with `promotion_gates.parseable_workflow = true`.
- Promote rows with `promotion_gates.has_compiled_api = true` when direct graph adaptation is needed.
- Prefer `evidence.confidence = "high"` over `"medium"` and `"low"`.
- Demote rows where `task_type` or `media_type` is `unknown` unless the title/body score is very strong.
- Use `adapter_directions` to avoid using text-to-video workflows when the user asked for image-to-video.

## Backfill

Dry-run:

```bash
python3 scripts/backfill_workflow_semantics.py \
  --limit 100 \
  --out-dir .codex_tmp/semantic_backfill \
  --write-llm-briefs
```

Apply, when service-role credentials are available:

```bash
SUPABASE_SERVICE_ROLE_KEY=... python3 scripts/backfill_workflow_semantics.py \
  --apply \
  --limit 1000 \
  --out-dir .codex_tmp/semantic_backfill
```

If `llm_briefs/` contains low-confidence rows, classify only those rows with a bounded Flash fan-out, then manually review the proposed changes before applying them.
