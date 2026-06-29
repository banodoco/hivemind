from __future__ import annotations

import argparse
import json

from scripts import backfill_workflow_semantics as backfill


def _args(tmp_path, *, apply: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        api_url="https://example.test/rest/v1",
        anon_key="anon",
        service_role_key="service",
        source=["vibecomfy-external"],
        limit=10,
        page_size=10,
        force=False,
        apply=apply,
        write_llm_briefs=True,
        vibecomfy_root=None,
        out_dir=tmp_path,
    )


def test_dry_run_writes_sample_without_patch(monkeypatch, tmp_path) -> None:
    rows = [
        {
            "id": 1,
            "kind": "workflow",
            "source": "vibecomfy-external",
            "external_id": "x",
            "title": "LTX I2V",
            "body": "Image-to-video workflow.",
            "metadata": {"summary": {"task_type": "image_to_video", "media_type": "video"}},
            "payload": {"workflow_json": {"nodes": {"1": {"class_type": "LTXVSampler"}}}},
        }
    ]
    monkeypatch.setattr(backfill, "fetch_rows", lambda **_kwargs: rows)
    patched: list[dict] = []
    monkeypatch.setattr(backfill, "_patch_row", lambda **kwargs: patched.append(kwargs))

    summary = backfill.run(_args(tmp_path))

    assert summary["counts"]["would_update"] == 1
    assert summary["counts"]["updated"] == 0
    assert patched == []
    sample = (tmp_path / "backfill_sample.jsonl").read_text(encoding="utf-8")
    assert '"task_type": "image_to_video"' in sample


def test_apply_patches_metadata_and_body(monkeypatch, tmp_path) -> None:
    rows = [
        {
            "id": 1,
            "kind": "workflow",
            "source": "vibecomfy",
            "external_id": "x",
            "title": "Unknown",
            "body": "No structured fields.",
            "metadata": {},
            "payload": {},
        }
    ]
    monkeypatch.setattr(backfill, "fetch_rows", lambda **_kwargs: rows)
    patched: list[dict] = []
    monkeypatch.setattr(backfill, "_patch_row", lambda **kwargs: patched.append(kwargs))

    summary = backfill.run(_args(tmp_path, apply=True))

    assert summary["counts"]["updated"] == 1
    assert patched[0]["row_id"] == 1
    update = patched[0]["update"]
    assert update["metadata"]["workflow_semantics_version"] == 1
    assert json.loads((tmp_path / "backfill_summary.json").read_text(encoding="utf-8"))["mode"] == "apply"
