"""Tests for the get_item executor — mocked HTTP, no network."""

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

from executors.get_item.run import (  # noqa: E402
    _assemble_result,
    _fetch_cited_by,
    _fetch_distillation_cites,
    _fetch_row,
    build_parser,
    main,
)


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class CLITests(unittest.TestCase):
    """Argument parsing tests."""

    def test_kind_required(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])

    def test_id_required(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["--kind", "message"])

    def test_kind_and_id_parsed(self):
        parser = build_parser()
        args = parser.parse_args(["--kind", "message", "--id", "12345"])
        self.assertEqual(args.kind, "message")
        self.assertEqual(args.id, 12345)

    def test_kind_resource(self):
        parser = build_parser()
        args = parser.parse_args(["--kind", "resource", "--id", "42"])
        self.assertEqual(args.kind, "resource")
        self.assertEqual(args.id, 42)

    def test_kind_distillation(self):
        parser = build_parser()
        args = parser.parse_args(["--kind", "distillation", "--id", "7"])
        self.assertEqual(args.kind, "distillation")
        self.assertEqual(args.id, 7)

    def test_invalid_kind_rejected(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["--kind", "invalid", "--id", "1"])

    def test_out_parsed(self):
        parser = build_parser()
        args = parser.parse_args(["--kind", "message", "--id", "1", "--out", "/tmp/out.json"])
        self.assertEqual(args.out, "/tmp/out.json")


# ---------------------------------------------------------------------------
# _fetch_row tests
# ---------------------------------------------------------------------------


class FetchRowTests(unittest.TestCase):
    """_fetch_row tests with mocked postgrest_get."""

    def setUp(self):
        self.endpoint = "http://fake.example.com/rest/v1"
        self.anon_key = "fake-anon-key"

    def test_returns_row_from_list(self):
        def mock_get(path, params=None, endpoint=None, anon_key=None):
            return [{"kind": "message", "body": "hello", "item_id": "123"}]

        with unittest.mock.patch("executors.get_item.run.postgrest_get", side_effect=mock_get):
            row = _fetch_row("message", 123, endpoint=self.endpoint, anon_key=self.anon_key)
            self.assertIsNotNone(row)
            self.assertEqual(row["kind"], "message")  # type: ignore[index]
            self.assertEqual(row["body"], "hello")  # type: ignore[index]

    def test_returns_none_for_empty_list(self):
        def mock_get(path, params=None, endpoint=None, anon_key=None):
            return []

        with unittest.mock.patch("executors.get_item.run.postgrest_get", side_effect=mock_get):
            row = _fetch_row("message", 999, endpoint=self.endpoint, anon_key=self.anon_key)
            self.assertIsNone(row)

    def test_returns_single_object(self):
        def mock_get(path, params=None, endpoint=None, anon_key=None):
            return {"kind": "distillation", "body": "answer", "title": "Q?"}

        with unittest.mock.patch("executors.get_item.run.postgrest_get", side_effect=mock_get):
            row = _fetch_row("distillation", 1, endpoint=self.endpoint, anon_key=self.anon_key)
            self.assertIsNotNone(row)
            self.assertEqual(row["kind"], "distillation")  # type: ignore[index]

    def test_passes_correct_params(self):
        captured_params = {}

        def mock_get(path, params=None, endpoint=None, anon_key=None):
            nonlocal captured_params
            captured_params = dict(params or {})
            return []

        with unittest.mock.patch("executors.get_item.run.postgrest_get", side_effect=mock_get):
            _fetch_row("resource", 42, endpoint=self.endpoint, anon_key=self.anon_key)
        # "resource" maps to everything that isn't a message or distillation,
        # because unified_feed carries concrete resource kinds (article, ...).
        self.assertEqual(captured_params.get("kind"), "not.in.(message,distillation)")
        self.assertEqual(captured_params.get("item_id"), "eq.42")


# ---------------------------------------------------------------------------
# _fetch_distillation_cites tests
# ---------------------------------------------------------------------------


class FetchDistillationCitesTests(unittest.TestCase):
    """_fetch_distillation_cites tests with mocked postgrest_get."""

    def setUp(self):
        self.endpoint = "http://fake.example.com/rest/v1"
        self.anon_key = "fake-anon-key"

    def test_returns_cites_list(self):
        def mock_get(path, params=None, endpoint=None, anon_key=None):
            return [
                {"distillation_id": 1, "item_kind": "message", "item_id": "88123"},
                {"distillation_id": 1, "item_kind": "resource", "item_id": "17"},
            ]

        with unittest.mock.patch("executors.get_item.run.postgrest_get", side_effect=mock_get):
            cites = _fetch_distillation_cites(1, endpoint=self.endpoint, anon_key=self.anon_key)
            self.assertEqual(len(cites), 2)
            self.assertEqual(cites[0]["item_kind"], "message")
            self.assertEqual(cites[1]["item_kind"], "resource")

    def test_empty_cites(self):
        def mock_get(path, params=None, endpoint=None, anon_key=None):
            return []

        with unittest.mock.patch("executors.get_item.run.postgrest_get", side_effect=mock_get):
            cites = _fetch_distillation_cites(1, endpoint=self.endpoint, anon_key=self.anon_key)
            self.assertEqual(cites, [])

    def test_passes_correct_params(self):
        captured_params = {}

        def mock_get(path, params=None, endpoint=None, anon_key=None):
            nonlocal captured_params
            captured_params = dict(params or {})
            return []

        with unittest.mock.patch("executors.get_item.run.postgrest_get", side_effect=mock_get):
            _fetch_distillation_cites(7, endpoint=self.endpoint, anon_key=self.anon_key)
        self.assertEqual(captured_params.get("distillation_id"), "eq.7")


# ---------------------------------------------------------------------------
# _fetch_cited_by tests
# ---------------------------------------------------------------------------


class FetchCitedByTests(unittest.TestCase):
    """_fetch_cited_by tests with mocked postgrest_get."""

    def setUp(self):
        self.endpoint = "http://fake.example.com/rest/v1"
        self.anon_key = "fake-anon-key"

    def test_returns_distillations_that_cite_item(self):
        call_count = 0

        def mock_get(path, params=None, endpoint=None, anon_key=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: find cite rows
                return [
                    {"distillation_id": 5, "item_kind": "message", "item_id": "12345"},
                    {"distillation_id": 8, "item_kind": "message", "item_id": "12345"},
                ]
            else:
                # Subsequent calls: fetch distillation rows
                distillation_id = int(params.get("item_id", "eq.0").replace("eq.", ""))
                return [
                    {
                        "kind": "distillation",
                        "item_id": str(distillation_id),
                        "title": f"Question {distillation_id}",
                        "body": f"Answer {distillation_id}",
                    }
                ]

        with unittest.mock.patch("executors.get_item.run.postgrest_get", side_effect=mock_get):
            result = _fetch_cited_by("message", 12345, endpoint=self.endpoint, anon_key=self.anon_key)
            self.assertEqual(len(result), 2)
            self.assertEqual(result[0]["title"], "Question 5")
            self.assertEqual(result[1]["title"], "Question 8")

    def test_no_cites_returns_empty(self):
        def mock_get(path, params=None, endpoint=None, anon_key=None):
            return []

        with unittest.mock.patch("executors.get_item.run.postgrest_get", side_effect=mock_get):
            result = _fetch_cited_by("message", 99999, endpoint=self.endpoint, anon_key=self.anon_key)
            self.assertEqual(result, [])

    def test_single_cite_row_as_object(self):
        call_count = 0

        def mock_get(path, params=None, endpoint=None, anon_key=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"distillation_id": 3, "item_kind": "resource", "item_id": "17"}
            else:
                return [{"kind": "distillation", "item_id": "3", "title": "Q3", "body": "A3"}]

        with unittest.mock.patch("executors.get_item.run.postgrest_get", side_effect=mock_get):
            result = _fetch_cited_by("resource", 17, endpoint=self.endpoint, anon_key=self.anon_key)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["title"], "Q3")


# ---------------------------------------------------------------------------
# _assemble_result tests
# ---------------------------------------------------------------------------


class AssembleResultTests(unittest.TestCase):
    """_assemble_result tests with mocked HTTP."""

    def setUp(self):
        self.endpoint = "http://fake.example.com/rest/v1"
        self.anon_key = "fake-anon-key"

    def test_message_includes_cited_by(self):
        call_count = 0

        def mock_get(path, params=None, endpoint=None, anon_key=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: fetch the message row
                return [{"kind": "message", "body": "full message body", "item_id": "12345", "author": "alice"}]
            else:
                # Subsequent call: fetch cited_by — empty list
                return []

        with unittest.mock.patch("executors.get_item.run.postgrest_get", side_effect=mock_get):
            result = _assemble_result("message", 12345, endpoint=self.endpoint, anon_key=self.anon_key)
            self.assertIn("item", result)
            self.assertEqual(result["item"]["kind"], "message")
            # Message body should NOT be truncated — full body
            self.assertEqual(result["item"]["body"], "full message body")
            self.assertIn("cited_by", result)

    def test_resource_includes_cited_by(self):
        call_count = 0

        def mock_get(path, params=None, endpoint=None, anon_key=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [{"kind": "resource", "body": "full article text", "item_id": "42", "title": "Article"}]
            else:
                return []

        with unittest.mock.patch("executors.get_item.run.postgrest_get", side_effect=mock_get):
            result = _assemble_result("resource", 42, endpoint=self.endpoint, anon_key=self.anon_key)
            self.assertIn("item", result)
            self.assertEqual(result["item"]["body"], "full article text")
            self.assertIn("cited_by", result)

    def test_distillation_includes_cites(self):
        call_count = 0

        def mock_get(path, params=None, endpoint=None, anon_key=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [{"kind": "distillation", "body": "full answer", "item_id": "7", "title": "Best model?"}]
            else:
                return [
                    {"distillation_id": 7, "item_kind": "message", "item_id": "88123"},
                    {"distillation_id": 7, "item_kind": "resource", "item_id": "17"},
                ]

        with unittest.mock.patch("executors.get_item.run.postgrest_get", side_effect=mock_get):
            result = _assemble_result("distillation", 7, endpoint=self.endpoint, anon_key=self.anon_key)
            self.assertIn("item", result)
            self.assertIn("cites", result)
            self.assertEqual(len(result["cites"]), 2)
            self.assertEqual(result["cites"][0]["item_kind"], "message")

    def test_not_found_returns_error(self):
        def mock_get(path, params=None, endpoint=None, anon_key=None):
            return []

        with unittest.mock.patch("executors.get_item.run.postgrest_get", side_effect=mock_get):
            result = _assemble_result("message", 99999, endpoint=self.endpoint, anon_key=self.anon_key)
            self.assertIn("error", result)
            self.assertEqual(result["error"], "not_found")

    def test_distillation_body_is_full_not_truncated(self):
        """Get-item returns full untruncated rows — body is preserved as-is."""
        full_body = "This is a very long answer " * 100  # ~2800 chars

        def mock_get(path, params=None, endpoint=None, anon_key=None):
            return [{"kind": "distillation", "body": full_body, "item_id": "7", "title": "Q?"}]

        with unittest.mock.patch("executors.get_item.run.postgrest_get", side_effect=mock_get):
            result = _assemble_result("distillation", 7, endpoint=self.endpoint, anon_key=self.anon_key)
            self.assertEqual(result["item"]["body"], full_body)
            # No truncated flag on get-item results
            self.assertNotIn("truncated", result["item"])

    def test_kind_resource_uses_item_kind_resource(self):
        """For resources, the item_kind in the API is 'resource'."""
        call_count = 0
        captured_params = {}

        def mock_get(path, params=None, endpoint=None, anon_key=None):
            nonlocal call_count, captured_params
            call_count += 1
            if call_count == 1:
                captured_params = dict(params or {})
                return [{"kind": "resource", "body": "text", "item_id": "1"}]
            else:
                return []

        with unittest.mock.patch("executors.get_item.run.postgrest_get", side_effect=mock_get):
            _assemble_result("resource", 1, endpoint=self.endpoint, anon_key=self.anon_key)
        self.assertEqual(captured_params.get("kind"), "not.in.(message,distillation)")


# ---------------------------------------------------------------------------
# Main integration tests (mocked HTTP)
# ---------------------------------------------------------------------------


class MainIntegrationTests(unittest.TestCase):
    """End-to-end main() tests with mocked postgrest_get."""

    def setUp(self):
        self.endpoint = "http://fake.example.com/rest/v1"
        self.anon_key = "fake-anon-key"

    def _patch_env(self):
        return unittest.mock.patch.dict(
            os.environ,
            {"HIVEMIND_API_URL": self.endpoint, "HIVEMIND_ANON_KEY": self.anon_key},
            clear=True,
        )

    def test_main_message_success(self):
        with self._patch_env():
            call_count = 0

            def mock_get(path, params=None, endpoint=None, anon_key=None):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return [{"kind": "message", "body": "hello world", "item_id": "12345", "author": "bob"}]
                else:
                    return []

            with unittest.mock.patch("executors.get_item.run.postgrest_get", side_effect=mock_get):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = main(["--kind", "message", "--id", "12345"])
                    self.assertEqual(ret, 0)
                    output = json.loads(mock_stdout.getvalue())
                    self.assertIn("item", output)
                    self.assertEqual(output["item"]["kind"], "message")
                    self.assertIn("cited_by", output)

    def test_main_distillation_success(self):
        with self._patch_env():
            call_count = 0

            def mock_get(path, params=None, endpoint=None, anon_key=None):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return [{"kind": "distillation", "body": "answer text", "item_id": "7", "title": "Q?"}]
                return [
                    {"distillation_id": 7, "item_kind": "message", "item_id": "88123"},
                ]

            with unittest.mock.patch("executors.get_item.run.postgrest_get", side_effect=mock_get):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = main(["--kind", "distillation", "--id", "7"])
                    self.assertEqual(ret, 0)
                    output = json.loads(mock_stdout.getvalue())
                    self.assertIn("item", output)
                    self.assertIn("cites", output)

    def test_main_not_found(self):
        with self._patch_env():
            def mock_get(path, params=None, endpoint=None, anon_key=None):
                return []

            with unittest.mock.patch("executors.get_item.run.postgrest_get", side_effect=mock_get):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = main(["--kind", "message", "--id", "99999"])
                    self.assertEqual(ret, 0)
                    output = json.loads(mock_stdout.getvalue())
                    self.assertEqual(output["error"], "not_found")

    def test_main_writes_to_file(self):
        with self._patch_env():
            call_count = 0

            def mock_get(path, params=None, endpoint=None, anon_key=None):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return [{"kind": "resource", "body": "article", "item_id": "1"}]
                else:
                    return []

            with unittest.mock.patch("executors.get_item.run.postgrest_get", side_effect=mock_get):
                with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
                    tmp_path = tmp.name
                try:
                    ret = main(["--kind", "resource", "--id", "1", "--out", tmp_path])
                    self.assertEqual(ret, 0)
                    with open(tmp_path, "r", encoding="utf-8") as fh:
                        output = json.load(fh)
                    self.assertIn("item", output)
                finally:
                    os.unlink(tmp_path)

    def test_main_http_error(self):
        import urllib.error

        with self._patch_env():
            def mock_get(path, params=None, endpoint=None, anon_key=None):
                raise urllib.error.HTTPError(
                    "http://fake/", 500, "Internal Server Error", {}, None
                )

            with unittest.mock.patch("executors.get_item.run.postgrest_get", side_effect=mock_get):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = main(["--kind", "message", "--id", "1"])
                    self.assertEqual(ret, 2)
                    output = json.loads(mock_stdout.getvalue())
                    self.assertIn("error", output)

    def test_main_resource_success(self):
        with self._patch_env():
            call_count = 0

            def mock_get(path, params=None, endpoint=None, anon_key=None):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return [{"kind": "resource", "body": "full article", "item_id": "42", "title": "Article"}]
                else:
                    return []

            with unittest.mock.patch("executors.get_item.run.postgrest_get", side_effect=mock_get):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = main(["--kind", "resource", "--id", "42"])
                    self.assertEqual(ret, 0)
                    output = json.loads(mock_stdout.getvalue())
                    self.assertIn("item", output)
                    self.assertEqual(output["item"]["kind"], "resource")


# ===========================================================================
# Discovery
# ===========================================================================

if __name__ == "__main__":
    unittest.main()
