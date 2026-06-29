from __future__ import annotations

from executors.workflow_semantics import (
    append_semantics_to_body,
    build_workflow_semantics,
    enrich_resource_data,
    extract_node_class_multiset,
)


def test_extracts_vibecomfy_nodes_and_compiled_api() -> None:
    workflow = {
        "nodes": {
            "1": {"class_type": "LoadImage"},
            "2": {"class_type": "WANVideoSampler"},
        },
        "compiled_api": {"3": {"class_type": "VHS_VideoCombine", "inputs": {}}},
    }

    assert extract_node_class_multiset(workflow) == {
        "LoadImage": 1,
        "WANVideoSampler": 1,
    }
    assert extract_node_class_multiset(workflow["compiled_api"]) == {"VHS_VideoCombine": 1}


def test_build_workflow_semantics_prefers_structured_fields() -> None:
    semantics = build_workflow_semantics(
        metadata={
            "media_type": "video",
            "task_type": "image_to_video",
            "models": ["ltxv-i2v.safetensors"],
            "custom_nodes": ["LTXVLoader"],
        },
        payload={
            "workflow_json": {
                "nodes": {
                    "1": {"class_type": "LoadImage"},
                    "2": {"class_type": "LTXVSampler"},
                },
                "requirements": {"models": ["wan2_1_i2v.safetensors"]},
            },
            "compiled_api": {"3": {"class_type": "VHS_VideoCombine", "inputs": {}}},
            "python_source": "def build(): pass",
        },
        title="LTX image to video",
        body="Image-to-video workflow.",
    )

    assert semantics["media_type"] == "video"
    assert semantics["task_type"] == "image_to_video"
    assert semantics["model_families"] == ["ltx", "wan"]
    assert semantics["adapter_directions"] == [
        {"from": ["image"], "to": "video", "confidence": "deterministic"}
    ]
    assert semantics["node_class_multiset"] == {
        "LTXVSampler": 1,
        "LoadImage": 1,
        "VHS_VideoCombine": 1,
    }
    assert semantics["promotion_gates"] == {
        "has_workflow_json": True,
        "has_compiled_api": True,
        "has_python_source": True,
        "parseable_workflow": True,
    }


def test_enrich_resource_data_adds_metadata_and_searchable_body() -> None:
    data = {
        "kind": "workflow",
        "source": "vibecomfy-external",
        "title": "Wan I2V",
        "body": "A workflow.",
        "metadata": {"summary": {"task_type": "image_to_video", "media_type": "video"}},
        "payload": {"workflow_json": {"nodes": {"1": {"class_type": "WanVideoSampler"}}}},
    }

    enriched = enrich_resource_data(data)

    assert enriched["metadata"]["workflow_semantics_version"] == 1
    assert enriched["metadata"]["workflow_semantics"]["task_type"] == "image_to_video"
    assert "Workflow semantics" in enriched["body"]
    assert "wan" in enriched["body"].casefold()


def test_append_semantics_to_body_is_idempotent() -> None:
    body = "Workflow semantics: media=video."
    assert append_semantics_to_body(body, {"media_type": "video"}) == body
