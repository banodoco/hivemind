"""Tests for the contribute executor — mocked HTTP, no network."""

from __future__ import annotations

import argparse
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

from executors.contribute.run import (  # noqa: E402
    _build_distillation_data,
    _build_resource_data,
    build_parser,
    main,
)


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class CLITests(unittest.TestCase):
    """Argument parsing tests."""

    def test_type_required(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])

    def test_type_resource_parsed(self):
        parser = build_parser()
        args = parser.parse_args(["--type", "resource"])
        self.assertEqual(args.type, "resource")

    def test_type_distillation_parsed(self):
        parser = build_parser()
        args = parser.parse_args(["--type", "distillation"])
        self.assertEqual(args.type, "distillation")

    def test_invalid_type_rejected(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["--type", "invalid"])

    def test_resource_fields(self):
        parser = build_parser()
        args = parser.parse_args([
            "--type", "resource",
            "--kind", "article",
            "--title", "My Article",
            "--body-file", "/tmp/body.txt",
            "--source", "web",
            "--url", "https://example.com",
            "--author", "Alice",
        ])
        self.assertEqual(args.kind, "article")
        self.assertEqual(args.title, "My Article")
        self.assertEqual(args.body_file, "/tmp/body.txt")
        self.assertEqual(args.source, "web")
        self.assertEqual(args.url, "https://example.com")
        self.assertEqual(args.author, "Alice")

    def test_distillation_fields(self):
        parser = build_parser()
        args = parser.parse_args([
            "--type", "distillation",
            "--question", "Best upscale model?",
            "--answer", "4x-UltraSharp for anime.",
            "--confidence", "high",
            "--cites", "message:88123,resource:17",
            "--supersedes", "5",
            "--conditions", "For anime-style video upscaling.",
        ])
        self.assertEqual(args.question, "Best upscale model?")
        self.assertEqual(args.answer, "4x-UltraSharp for anime.")
        self.assertEqual(args.confidence, "high")
        self.assertEqual(args.cites, "message:88123,resource:17")
        self.assertEqual(args.supersedes, 5)
        self.assertEqual(args.conditions, "For anime-style video upscaling.")

    def test_dry_run_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--type", "resource", "--dry-run"])
        self.assertTrue(args.dry_run)

    def test_dry_run_default_false(self):
        parser = build_parser()
        args = parser.parse_args(["--type", "resource"])
        self.assertFalse(args.dry_run)

    def test_from_file_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--type", "resource", "--from-file", "payload.json"])
        self.assertEqual(args.from_file, "payload.json")

    def test_out_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--type", "resource", "--out", "/tmp/out.json"])
        self.assertEqual(args.out, "/tmp/out.json")


# ---------------------------------------------------------------------------
# Envelope data building tests
# ---------------------------------------------------------------------------


class BuildResourceDataTests(unittest.TestCase):
    """_build_resource_data tests."""

    def test_minimal_resource(self):
        ns = argparse.Namespace(
            kind=None, source=None, title=None,
            body_file=None, url=None, author=None,
        )
        data = _build_resource_data(ns)
        self.assertEqual(data["kind"], "unknown")
        self.assertEqual(data["source"], "cli")
        self.assertEqual(data["title"], "")
        self.assertEqual(data["body"], "")

    def test_full_resource(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as tmp:
            tmp.write("Article body text.")
            tmp_path = tmp.name
        try:
            ns = argparse.Namespace(
                kind="article", source="web", title="Test",
                body_file=tmp_path, url="https://example.com", author="Alice",
            )
            data = _build_resource_data(ns)
            self.assertEqual(data["kind"], "article")
            self.assertEqual(data["source"], "web")
            self.assertEqual(data["title"], "Test")
            self.assertEqual(data["body"], "Article body text.")
            self.assertEqual(data["url"], "https://example.com")
            self.assertEqual(data["author"], "Alice")
        finally:
            os.unlink(tmp_path)

    def test_resource_without_url_author(self):
        ns = argparse.Namespace(
            kind="note", source="manual", title="Note",
            body_file=None, url=None, author=None,
        )
        data = _build_resource_data(ns)
        self.assertEqual(data["kind"], "note")
        self.assertNotIn("url", data)
        self.assertNotIn("author", data)


class BuildDistillationDataTests(unittest.TestCase):
    """_build_distillation_data tests."""

    def test_minimal_distillation(self):
        ns = argparse.Namespace(
            question=None, answer=None, confidence=None,
            cites=None, supersedes=None, conditions=None,
        )
        data = _build_distillation_data(ns)
        self.assertEqual(data["question"], "")
        self.assertEqual(data["answer"], "")
        self.assertEqual(data["confidence"], "medium")
        self.assertEqual(data["cites"], [])

    def test_full_distillation(self):
        ns = argparse.Namespace(
            question="Best upscale model?",
            answer="4x-UltraSharp.",
            confidence="high",
            cites="message:88123,resource:17",
            supersedes=5,
            conditions="For anime.",
        )
        data = _build_distillation_data(ns)
        self.assertEqual(data["question"], "Best upscale model?")
        self.assertEqual(data["answer"], "4x-UltraSharp.")
        self.assertEqual(data["confidence"], "high")
        self.assertEqual(len(data["cites"]), 2)
        self.assertEqual(data["cites"][0], {"item_kind": "message", "item_id": "88123"})
        self.assertEqual(data["cites"][1], {"item_kind": "resource", "item_id": "17"})
        self.assertEqual(data["supersedes_id"], 5)
        self.assertEqual(data["conditions"], "For anime.")

    def test_distillation_without_optional_fields(self):
        ns = argparse.Namespace(
            question="Q?", answer="A.", confidence="low",
            cites=None, supersedes=None, conditions=None,
        )
        data = _build_distillation_data(ns)
        self.assertNotIn("supersedes_id", data)
        self.assertNotIn("conditions", data)

    def test_cites_parsing_message_resource(self):
        """Parse --cites message:88123,resource:17 correctly."""
        ns = argparse.Namespace(
            question="Q?", answer="A.", confidence="high",
            cites="message:88123,resource:17",
            supersedes=None, conditions=None,
        )
        data = _build_distillation_data(ns)
        self.assertEqual(data["cites"], [
            {"item_kind": "message", "item_id": "88123"},
            {"item_kind": "resource", "item_id": "17"},
        ])

    def test_cites_with_distillation_kind(self):
        ns = argparse.Namespace(
            question="Q?", answer="A.", confidence="high",
            cites="distillation:3",
            supersedes=None, conditions=None,
        )
        data = _build_distillation_data(ns)
        self.assertEqual(data["cites"], [
            {"item_kind": "distillation", "item_id": "3"},
        ])


# ---------------------------------------------------------------------------
# Main integration tests — dry-run (no key needed)
# ---------------------------------------------------------------------------


class MainDryRunTests(unittest.TestCase):
    """main() tests in dry-run mode — no contributor key needed."""

    def setUp(self):
        self.contribute_url = "http://fake.example.com/functions/v1/contribute"

    def _patch_env(self):
        return unittest.mock.patch.dict(
            os.environ,
            {"HIVEMIND_CONTRIBUTE_URL": self.contribute_url},
            clear=True,
        )

    def test_resource_dry_run_outputs_envelope(self):
        with self._patch_env():
            with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                ret = main([
                    "--type", "resource",
                    "--kind", "article",
                    "--source", "web",
                    "--title", "Test Article",
                    "--dry-run",
                ])
                self.assertEqual(ret, 0)
                output = json.loads(mock_stdout.getvalue())
                self.assertIs(output["dry_run"], True)
                self.assertIn("envelope", output)
                self.assertEqual(output["envelope"]["action"], "add_resource")
                self.assertEqual(output["envelope"]["data"]["kind"], "article")

    def test_distillation_dry_run_outputs_envelope(self):
        with self._patch_env():
            with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                ret = main([
                    "--type", "distillation",
                    "--question", "Best model?",
                    "--answer", "4x-UltraSharp.",
                    "--confidence", "high",
                    "--cites", "message:88123",
                    "--dry-run",
                ])
                self.assertEqual(ret, 0)
                output = json.loads(mock_stdout.getvalue())
                self.assertIs(output["dry_run"], True)
                self.assertEqual(output["envelope"]["action"], "submit_distillation")
                self.assertEqual(output["envelope"]["data"]["question"], "Best model?")

    def test_dry_run_does_not_require_contributor_key(self):
        """Dry-run should NOT require a contributor key."""
        with self._patch_env():
            with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                ret = main([
                    "--type", "resource",
                    "--kind", "note",
                    "--source", "cli",
                    "--title", "Test",
                    "--dry-run",
                ])
                self.assertEqual(ret, 0)
                output = json.loads(mock_stdout.getvalue())
                self.assertIn("dry_run", output)

    def test_dry_run_with_from_file_does_not_require_key(self):
        with self._patch_env():
            payload = {"action": "add_resource", "data": {"kind": "article", "source": "web", "title": "T", "body": "B"}}
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tmp:
                json.dump(payload, tmp)
                tmp_path = tmp.name
            try:
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = main([
                        "--type", "resource",
                        "--from-file", tmp_path,
                        "--dry-run",
                    ])
                    self.assertEqual(ret, 0)
                    output = json.loads(mock_stdout.getvalue())
                    self.assertIs(output["dry_run"], True)
                    self.assertEqual(output["envelope"]["action"], "add_resource")
            finally:
                os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Main integration tests — real send (requires key)
# ---------------------------------------------------------------------------


class MainRealSendTests(unittest.TestCase):
    """main() tests for real sends with mocked edge_post."""

    def setUp(self):
        self.contribute_url = "http://fake.example.com/functions/v1/contribute"
        self.contributor_key = "hm_" + "a" * 64

    def _patch_env(self):
        return unittest.mock.patch.dict(
            os.environ,
            {
                "HIVEMIND_CONTRIBUTE_URL": self.contribute_url,
                "HIVEMIND_CONTRIBUTOR_KEY": self.contributor_key,
            },
            clear=True,
        )

    def test_resource_send_success(self):
        with self._patch_env():
            def mock_post(payload, contribute_url=None, contributor_key=None):
                return {"id": 42, "status": "ok"}

            with unittest.mock.patch("executors.contribute.run.edge_post", side_effect=mock_post):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = main([
                        "--type", "resource",
                        "--kind", "article",
                        "--source", "web",
                        "--title", "Test",
                    ])
                    self.assertEqual(ret, 0)
                    output = json.loads(mock_stdout.getvalue())
                    self.assertEqual(output["id"], 42)
                    self.assertEqual(output["status"], "ok")

    def test_distillation_send_success(self):
        with self._patch_env():
            def mock_post(payload, contribute_url=None, contributor_key=None):
                self.assertEqual(payload["action"], "submit_distillation")
                return {"id": 7, "status": "ok"}

            with unittest.mock.patch("executors.contribute.run.edge_post", side_effect=mock_post):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = main([
                        "--type", "distillation",
                        "--question", "Best model?",
                        "--answer", "4x-UltraSharp.",
                        "--confidence", "high",
                        "--cites", "message:88123",
                    ])
                    self.assertEqual(ret, 0)
                    output = json.loads(mock_stdout.getvalue())
                    self.assertEqual(output["id"], 7)

    def test_missing_contributor_key_fails(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                ret = main([
                    "--type", "resource",
                    "--kind", "article",
                    "--source", "web",
                    "--title", "Test",
                ])
                self.assertEqual(ret, 1)
                output = json.loads(mock_stdout.getvalue())
                self.assertIn("error", output)
                self.assertIn("contributor key required", output["error"])

    def test_409_duplicate_surfaces_existing_id(self):
        """409 responses should surface existing_id in the error message."""
        import urllib.error

        with self._patch_env():
            def mock_post(payload, contribute_url=None, contributor_key=None):
                raise urllib.error.HTTPError(
                    "http://fake/", 409, "Conflict", {},
                    io.BytesIO(b'{"error":"duplicate","existing_id":42,"detail":"similar question exists"}'),
                )

            with unittest.mock.patch("executors.contribute.run.edge_post", side_effect=mock_post):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = main([
                        "--type", "distillation",
                        "--question", "Best model?",
                        "--answer", "4x-UltraSharp.",
                        "--confidence", "high",
                        "--cites", "message:88123",
                    ])
                    self.assertEqual(ret, 1)
                    output = json.loads(mock_stdout.getvalue())
                    self.assertIn("error", output)
                    self.assertIn("409", output["error"])
                    self.assertIn("existing_id=42", output["error"])

    def test_400_validation_error(self):
        import urllib.error

        with self._patch_env():
            def mock_post(payload, contribute_url=None, contributor_key=None):
                raise urllib.error.HTTPError(
                    "http://fake/", 400, "Bad Request", {},
                    io.BytesIO(b'{"error":"validation","detail":"field title required"}'),
                )

            with unittest.mock.patch("executors.contribute.run.edge_post", side_effect=mock_post):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = main([
                        "--type", "resource",
                        "--kind", "article",
                        "--source", "web",
                        "--title", "Test",
                    ])
                    self.assertEqual(ret, 1)
                    output = json.loads(mock_stdout.getvalue())
                    self.assertIn("400", output["error"])
                    self.assertIn("field title required", output["error"])

    def test_401_unauthorized(self):
        import urllib.error

        with self._patch_env():
            def mock_post(payload, contribute_url=None, contributor_key=None):
                raise urllib.error.HTTPError(
                    "http://fake/", 401, "Unauthorized", {},
                    io.BytesIO(b'{"error":"unauthorized"}'),
                )

            with unittest.mock.patch("executors.contribute.run.edge_post", side_effect=mock_post):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = main([
                        "--type", "resource",
                        "--kind", "article",
                        "--source", "web",
                        "--title", "Test",
                    ])
                    self.assertEqual(ret, 1)
                    output = json.loads(mock_stdout.getvalue())
                    self.assertIn("401", output["error"])

    def test_500_internal_error(self):
        import urllib.error

        with self._patch_env():
            def mock_post(payload, contribute_url=None, contributor_key=None):
                raise urllib.error.HTTPError(
                    "http://fake/", 500, "Internal Server Error", {},
                    io.BytesIO(b"{}"),
                )

            with unittest.mock.patch("executors.contribute.run.edge_post", side_effect=mock_post):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = main([
                        "--type", "resource",
                        "--kind", "article",
                        "--source", "web",
                        "--title", "Test",
                    ])
                    self.assertEqual(ret, 1)
                    output = json.loads(mock_stdout.getvalue())
                    self.assertIn("500", output["error"])

    def test_sends_x_contributor_key_header(self):
        with self._patch_env():
            captured_key = None

            def mock_post(payload, contribute_url=None, contributor_key=None):
                nonlocal captured_key
                captured_key = contributor_key
                return {"id": 1, "status": "ok"}

            with unittest.mock.patch("executors.contribute.run.edge_post", side_effect=mock_post):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
                    main([
                        "--type", "resource",
                        "--kind", "article",
                        "--source", "web",
                        "--title", "Test",
                    ])
            self.assertEqual(captured_key, self.contributor_key)

    def test_send_distillation_includes_cites_in_envelope(self):
        with self._patch_env():
            captured_payload = None

            def mock_post(payload, contribute_url=None, contributor_key=None):
                nonlocal captured_payload
                captured_payload = payload
                return {"id": 1, "status": "ok"}

            with unittest.mock.patch("executors.contribute.run.edge_post", side_effect=mock_post):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
                    main([
                        "--type", "distillation",
                        "--question", "Q?",
                        "--answer", "A.",
                        "--confidence", "high",
                        "--cites", "message:88123,resource:17",
                    ])
            self.assertIsNotNone(captured_payload)
            self.assertEqual(captured_payload["action"], "submit_distillation")
            data = captured_payload["data"]
            self.assertEqual(len(data["cites"]), 2)
            self.assertEqual(data["cites"][0], {"item_kind": "message", "item_id": "88123"})
            self.assertEqual(data["cites"][1], {"item_kind": "resource", "item_id": "17"})


# ---------------------------------------------------------------------------
# from-file tests
# ---------------------------------------------------------------------------


class FromFileTests(unittest.TestCase):
    """Tests for the --from-file path."""

    def setUp(self):
        self.contribute_url = "http://fake.example.com/functions/v1/contribute"
        self.contributor_key = "hm_" + "a" * 64

    def _patch_env(self):
        return unittest.mock.patch.dict(
            os.environ,
            {
                "HIVEMIND_CONTRIBUTE_URL": self.contribute_url,
                "HIVEMIND_CONTRIBUTOR_KEY": self.contributor_key,
            },
            clear=True,
        )

    def test_from_file_dry_run_outputs_envelope(self):
        payload = {
            "action": "add_resource",
            "data": {"kind": "article", "source": "web", "title": "T", "body": "B"},
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            json.dump(payload, tmp)
            tmp_path = tmp.name
        try:
            with self._patch_env():
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = main(["--type", "resource", "--from-file", tmp_path, "--dry-run"])
                    self.assertEqual(ret, 0)
                    output = json.loads(mock_stdout.getvalue())
                    self.assertIs(output["dry_run"], True)
                    self.assertEqual(output["envelope"]["action"], "add_resource")
        finally:
            os.unlink(tmp_path)

    def test_from_file_real_send_success(self):
        payload = {
            "action": "submit_distillation",
            "data": {
                "question": "Q?", "answer": "A.", "confidence": "high",
                "cites": [{"item_kind": "message", "item_id": "12345"}],
            },
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            json.dump(payload, tmp)
            tmp_path = tmp.name
        try:
            with self._patch_env():
                def mock_post(payload, contribute_url=None, contributor_key=None):
                    return {"id": 99, "status": "ok"}

                with unittest.mock.patch("executors.contribute.run.edge_post", side_effect=mock_post):
                    with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                        ret = main(["--type", "resource", "--from-file", tmp_path])
                        self.assertEqual(ret, 0)
                        output = json.loads(mock_stdout.getvalue())
                        self.assertEqual(output["id"], 99)
        finally:
            os.unlink(tmp_path)

    def test_from_file_missing_key_fails(self):
        payload = {"action": "add_resource", "data": {"kind": "n", "source": "s", "title": "t", "body": "b"}}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            json.dump(payload, tmp)
            tmp_path = tmp.name
        try:
            with unittest.mock.patch.dict(os.environ, {}, clear=True):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = main(["--type", "resource", "--from-file", tmp_path])
                    self.assertEqual(ret, 1)
                    output = json.loads(mock_stdout.getvalue())
                    self.assertIn("contributor key required", output["error"])
        finally:
            os.unlink(tmp_path)

    def test_from_file_not_found(self):
        with self._patch_env():
            with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                ret = main(["--type", "resource", "--from-file", "/nonexistent/path.json"])
                self.assertEqual(ret, 1)
                output = json.loads(mock_stdout.getvalue())
                self.assertIn("cannot read from-file", output["error"])

    def test_from_file_invalid_json(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            tmp.write("not valid json {{{")
            tmp_path = tmp.name
        try:
            with self._patch_env():
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = main(["--type", "resource", "--from-file", tmp_path])
                    self.assertEqual(ret, 1)
                    output = json.loads(mock_stdout.getvalue())
                    self.assertIn("invalid JSON in from-file", output["error"])
        finally:
            os.unlink(tmp_path)


# ===========================================================================
# Discovery
# ===========================================================================

if __name__ == "__main__":
    unittest.main()
