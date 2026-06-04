"""Tests for the ingest_workflow executor — mocked HTTP, no network."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))

from executors.ingest_workflow.run import (  # noqa: E402
    _iter_nodes,
    build_envelope,
    build_parser,
    derive_name,
    extract_custom_nodes,
    extract_models,
    load_workflow_from_path,
    main,
    summarize_node_types,
)

FIXTURE_SAMPLE_WORKFLOW = _HERE / "fixtures" / "sample_workflow.json"


def _load_fixture() -> dict:
    with open(FIXTURE_SAMPLE_WORKFLOW, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class CLITests(unittest.TestCase):
    def test_path_parsed(self):
        args = build_parser().parse_args(["--path", "wf.json"])
        self.assertEqual(args.path, "wf.json")

    def test_url_parsed(self):
        args = build_parser().parse_args(["--url", "https://x/wf.json"])
        self.assertEqual(args.url, "https://x/wf.json")

    def test_kind_default_workflow(self):
        args = build_parser().parse_args(["--path", "wf.json"])
        self.assertEqual(args.kind, "workflow")

    def test_dry_run_flag(self):
        args = build_parser().parse_args(["--path", "wf.json", "--dry-run"])
        self.assertTrue(args.dry_run)

    def test_out_flag(self):
        args = build_parser().parse_args(["--path", "wf.json", "--out", "/tmp/o.json"])
        self.assertEqual(args.out, "/tmp/o.json")


# ---------------------------------------------------------------------------
# Node iteration / extraction tests
# ---------------------------------------------------------------------------


class IterNodesTests(unittest.TestCase):
    def test_api_format_excludes_top_level_scalars(self):
        wf = _load_fixture()
        nodes = _iter_nodes(wf)
        # 11 real nodes; the "name" string key must not become a node.
        self.assertEqual(len(nodes), 11)
        self.assertTrue(all(n["class_type"] for n in nodes))

    def test_ui_format_supported(self):
        wf = {
            "nodes": [
                {"type": "CheckpointLoaderSimple", "widgets_values": ["model.safetensors"]},
                {"type": "KSampler", "widgets_values": [42, 20, 7.0]},
            ]
        }
        nodes = _iter_nodes(wf)
        self.assertEqual(len(nodes), 2)
        self.assertEqual(nodes[0]["class_type"], "CheckpointLoaderSimple")


class ExtractModelsTests(unittest.TestCase):
    def test_extracts_checkpoint_lora_vae(self):
        nodes = _iter_nodes(_load_fixture())
        models = extract_models(nodes)
        self.assertIn("sd_xl_base_1.0.safetensors", models)
        self.assertIn("add_detail.safetensors", models)
        self.assertIn("sdxl_vae.safetensors", models)

    def test_extracts_pth_model(self):
        nodes = _iter_nodes(_load_fixture())
        models = extract_models(nodes)
        self.assertIn("rife47.pth", models)

    def test_models_deduped(self):
        nodes = [
            {"class_type": "CheckpointLoaderSimple", "widget_values": ["a.safetensors"]},
            {"class_type": "CheckpointLoaderSimple", "widget_values": ["a.safetensors"]},
        ]
        self.assertEqual(extract_models(nodes), ["a.safetensors"])

    def test_non_model_widgets_ignored(self):
        nodes = [
            {"class_type": "KSampler", "widget_values": [42, "karras", "a prompt"]},
        ]
        self.assertEqual(extract_models(nodes), [])


class ExtractCustomNodesTests(unittest.TestCase):
    def test_non_core_class_types_flagged(self):
        nodes = _iter_nodes(_load_fixture())
        custom = extract_custom_nodes(nodes)
        self.assertIn("RIFE VFI", custom)
        self.assertIn("VHS_VideoCombine", custom)

    def test_core_nodes_excluded(self):
        nodes = _iter_nodes(_load_fixture())
        custom = extract_custom_nodes(nodes)
        self.assertNotIn("KSampler", custom)
        self.assertNotIn("CheckpointLoaderSimple", custom)
        self.assertNotIn("VAEDecode", custom)

    def test_custom_nodes_deduped(self):
        nodes = [
            {"class_type": "FooNode", "widget_values": []},
            {"class_type": "FooNode", "widget_values": []},
        ]
        self.assertEqual(extract_custom_nodes(nodes), ["FooNode"])


class SummarizeNodeTypesTests(unittest.TestCase):
    def test_counts_and_sorts(self):
        nodes = _iter_nodes(_load_fixture())
        summary = dict(summarize_node_types(nodes))
        self.assertEqual(summary["CLIPTextEncode"], 2)
        self.assertEqual(summary["KSampler"], 1)


class DeriveNameTests(unittest.TestCase):
    def test_cli_title_wins(self):
        self.assertEqual(derive_name("My WF", {"name": "x"}, "p.json"), "My WF")

    def test_workflow_name_used(self):
        self.assertEqual(derive_name(None, {"name": "Graph A"}, "p.json"), "Graph A")

    def test_basename_fallback(self):
        self.assertEqual(derive_name(None, {}, "/a/b/wf.json"), "wf.json")


# ---------------------------------------------------------------------------
# Envelope building
# ---------------------------------------------------------------------------


class BuildEnvelopeTests(unittest.TestCase):
    def test_envelope_shape(self):
        wf = _load_fixture()
        env = build_envelope(
            wf,
            source_label="comfyui",
            name="Test WF",
            kind="workflow",
            url=None,
            external_id="x.json",
        )
        self.assertEqual(env["action"], "add_resource")
        data = env["data"]
        self.assertEqual(data["kind"], "workflow")
        self.assertEqual(data["source"], "comfyui")
        self.assertEqual(data["title"], "Test WF")
        self.assertEqual(data["metadata"]["node_count"], 11)
        self.assertIn("sd_xl_base_1.0.safetensors", data["metadata"]["models"])
        self.assertIn("RIFE VFI", data["metadata"]["custom_nodes"])
        self.assertEqual(data["payload"]["workflow"], wf)

    def test_body_mentions_name_and_models(self):
        wf = _load_fixture()
        env = build_envelope(
            wf, source_label="comfyui", name="My Graph",
            kind="workflow", url=None, external_id=None,
        )
        body = env["data"]["body"]
        self.assertIn("My Graph", body)
        self.assertIn("11 nodes", body)
        self.assertIn("sd_xl_base_1.0.safetensors", body)
        self.assertIn("RIFE VFI", body)


# ---------------------------------------------------------------------------
# load_workflow_from_path
# ---------------------------------------------------------------------------


class LoadWorkflowTests(unittest.TestCase):
    def test_loads_fixture(self):
        wf = load_workflow_from_path(str(FIXTURE_SAMPLE_WORKFLOW))
        self.assertIn("9", wf)
        self.assertEqual(wf["9"]["class_type"], "KSampler")


# ---------------------------------------------------------------------------
# main() dry-run / error paths
# ---------------------------------------------------------------------------


class MainDryRunTests(unittest.TestCase):
    def test_dry_run_outputs_envelope(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                ret = main(["--path", str(FIXTURE_SAMPLE_WORKFLOW), "--dry-run"])
                self.assertEqual(ret, 0)
                output = json.loads(out.getvalue())
                self.assertIs(output["dry_run"], True)
                data = output["envelope"]["data"]
                self.assertEqual(data["kind"], "workflow")
                self.assertEqual(data["source"], "comfyui")
                self.assertEqual(data["metadata"]["node_count"], 11)

    def test_dry_run_no_key_needed(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                ret = main(["--path", str(FIXTURE_SAMPLE_WORKFLOW), "--dry-run"])
                self.assertEqual(ret, 0)
                self.assertIn("dry_run", json.loads(out.getvalue()))

    def test_requires_path_or_url(self):
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            ret = main(["--dry-run"])
            self.assertEqual(ret, 1)
            self.assertIn("error", json.loads(out.getvalue()))

    def test_rejects_both_path_and_url(self):
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            ret = main(["--path", "a.json", "--url", "http://x", "--dry-run"])
            self.assertEqual(ret, 1)
            self.assertIn("error", json.loads(out.getvalue()))

    def test_missing_file(self):
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            ret = main(["--path", "/nonexistent/wf.json", "--dry-run"])
            self.assertEqual(ret, 1)
            self.assertIn("error", json.loads(out.getvalue()))

    def test_invalid_json(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write("{ not valid json")
            tmp_path = tmp.name
        try:
            with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                ret = main(["--path", tmp_path, "--dry-run"])
                self.assertEqual(ret, 1)
                self.assertIn("error", json.loads(out.getvalue()))
        finally:
            os.unlink(tmp_path)

    def test_dry_run_with_out_file(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as tmp:
            out_path = tmp.name
        try:
            ret = main([
                "--path", str(FIXTURE_SAMPLE_WORKFLOW),
                "--dry-run", "--out", out_path,
            ])
            self.assertEqual(ret, 0)
            with open(out_path, "r", encoding="utf-8") as fh:
                output = json.load(fh)
            self.assertIs(output["dry_run"], True)
        finally:
            os.unlink(out_path)


class MainRealSendTests(unittest.TestCase):
    def _patch_env(self):
        return unittest.mock.patch.dict(
            os.environ,
            {"HIVEMIND_CONTRIBUTOR_KEY": "hm_" + "a" * 64},
            clear=True,
        )

    def test_real_send_success(self):
        with self._patch_env():
            def mock_post(payload, contribute_url=None, contributor_key=None):
                return {"id": 7, "status": "ok"}

            with unittest.mock.patch(
                "executors.ingest_workflow.run.edge_post", side_effect=mock_post
            ):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                    ret = main(["--path", str(FIXTURE_SAMPLE_WORKFLOW)])
                    self.assertEqual(ret, 0)
                    output = json.loads(out.getvalue())
                    self.assertEqual(output["id"], 7)

    def test_missing_key_fails(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                ret = main(["--path", str(FIXTURE_SAMPLE_WORKFLOW)])
                self.assertEqual(ret, 1)
                output = json.loads(out.getvalue())
                self.assertIn("contributor key required", output["error"])

    def test_409_duplicate(self):
        import urllib.error

        with self._patch_env():
            def mock_post(payload, contribute_url=None, contributor_key=None):
                raise urllib.error.HTTPError(
                    "http://fake/", 409, "Conflict", {},
                    io.BytesIO(b'{"error":"duplicate","existing_id":5,"detail":"similar"}'),
                )

            with unittest.mock.patch(
                "executors.ingest_workflow.run.edge_post", side_effect=mock_post
            ):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                    ret = main(["--path", str(FIXTURE_SAMPLE_WORKFLOW)])
                    self.assertEqual(ret, 1)
                    output = json.loads(out.getvalue())
                    self.assertIn("409", output["error"])
                    self.assertIn("existing_id=5", output["error"])

    def test_url_fetch_path(self):
        wf_bytes = json.dumps(_load_fixture()).encode("utf-8")

        class MockResponse:
            @staticmethod
            def read():
                return wf_bytes

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with self._patch_env():
            def mock_urlopen(req, timeout=30):
                return MockResponse()

            def mock_post(payload, contribute_url=None, contributor_key=None):
                return {"id": 8, "status": "ok"}

            with unittest.mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
                with unittest.mock.patch(
                    "executors.ingest_workflow.run.edge_post", side_effect=mock_post
                ):
                    with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                        ret = main(["--url", "https://example.com/wf.json"])
                        self.assertEqual(ret, 0)
                        self.assertEqual(json.loads(out.getvalue())["id"], 8)


if __name__ == "__main__":
    unittest.main()
