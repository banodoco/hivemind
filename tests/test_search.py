"""Tests for the search executor — mocked HTTP, no network."""

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

from executors.search.run import (  # noqa: E402
    _build_params,
    _ilike_clause,
    _merge_results,
    _query_feed,
    _truncate_row,
    build_parser,
    main,
)


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class CLITests(unittest.TestCase):
    """Argument parsing tests."""

    def test_query_required(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])

    def test_query_parsed(self):
        parser = build_parser()
        args = parser.parse_args(["--query", "upscale model"])
        self.assertEqual(args.query, "upscale model")
        self.assertEqual(args.limit, 20)

    def test_limit_parsed(self):
        parser = build_parser()
        args = parser.parse_args(["--query", "test", "--limit", "5"])
        self.assertEqual(args.limit, 5)

    def test_kinds_parsed(self):
        parser = build_parser()
        args = parser.parse_args(["--query", "test", "--kinds", "message,resource"])
        self.assertEqual(args.kinds, "message,resource")

    def test_sources_parsed(self):
        parser = build_parser()
        args = parser.parse_args(["--query", "test", "--sources", "banodoco-discord,hivemind"])
        self.assertEqual(args.sources, "banodoco-discord,hivemind")

    def test_since_parsed(self):
        parser = build_parser()
        args = parser.parse_args(["--query", "test", "--since", "2024-01-01T00:00:00Z"])
        self.assertEqual(args.since, "2024-01-01T00:00:00Z")

    def test_out_parsed(self):
        parser = build_parser()
        args = parser.parse_args(["--query", "test", "--out", "/tmp/out.json"])
        self.assertEqual(args.out, "/tmp/out.json")

    def test_all_flags(self):
        parser = build_parser()
        args = parser.parse_args([
            "--query", "upscale",
            "--kinds", "message,distillation",
            "--sources", "banodoco-discord",
            "--since", "2024-06-01T00:00:00Z",
            "--limit", "10",
            "--out", "/tmp/search.json",
        ])
        self.assertEqual(args.query, "upscale")
        self.assertEqual(args.kinds, "message,distillation")
        self.assertEqual(args.sources, "banodoco-discord")
        self.assertEqual(args.since, "2024-06-01T00:00:00Z")
        self.assertEqual(args.limit, 10)
        self.assertEqual(args.out, "/tmp/search.json")


# ---------------------------------------------------------------------------
# Query construction tests
# ---------------------------------------------------------------------------


class QueryConstructionTests(unittest.TestCase):
    """ilike clause and param building tests."""

    def test_ilike_clause_simple(self):
        clause = _ilike_clause("upscale")
        self.assertIn("title.ilike.", clause)
        self.assertIn("body.ilike.", clause)
        self.assertIn("*upscale*", clause)
        self.assertTrue(clause.startswith("("))

    def test_ilike_clause_with_asterisk(self):
        clause = _ilike_clause("foo*bar")
        self.assertIn("foo\\*bar", clause)

    def test_ilike_clause_multi_word(self):
        clause = _ilike_clause("best upscale model")
        self.assertIn("*best upscale model*", clause)

    def test_build_params_minimal(self):
        params = _build_params("test", None, None, None, 20)
        self.assertEqual(params["select"], "*")
        self.assertEqual(params["limit"], "20")
        # the ilike clause must be the value of the "or" param
        self.assertEqual(params["or"], _ilike_clause("test"))

    def test_build_params_with_kinds(self):
        params = _build_params("test", "message,resource", None, None, 20)
        self.assertEqual(params["kind"], "in.(message,resource)")

    def test_build_params_with_sources(self):
        params = _build_params("test", None, "banodoco-discord", None, 20)
        self.assertEqual(params["source"], "in.(banodoco-discord)")

    def test_build_params_with_since(self):
        params = _build_params("test", None, None, "2024-01-01T00:00:00Z", 20)
        self.assertEqual(params["created_at"], "gte.2024-01-01T00:00:00Z")

    def test_build_params_with_limit(self):
        params = _build_params("test", None, None, None, 5)
        self.assertEqual(params["limit"], "5")


# ---------------------------------------------------------------------------
# Row truncation tests
# ---------------------------------------------------------------------------


class RowTruncationTests(unittest.TestCase):
    """_truncate_row tests."""

    def test_truncate_short_body(self):
        row = {"kind": "message", "body": "hello", "title": None}
        result = _truncate_row(row)
        self.assertEqual(result["body"], "hello")
        self.assertIs(result["truncated"], False)

    def test_truncate_long_body(self):
        row = {"kind": "resource", "body": "x" * 800, "title": "T"}
        result = _truncate_row(row)
        self.assertEqual(len(result["body"]), 700)
        self.assertIs(result["truncated"], True)

    def test_truncate_preserves_other_fields(self):
        row = {"kind": "message", "body": "hello", "title": None, "author": "alice", "item_id": "123"}
        result = _truncate_row(row)
        self.assertEqual(result["kind"], "message")
        self.assertEqual(result["author"], "alice")
        self.assertEqual(result["item_id"], "123")

    def test_truncate_missing_body(self):
        row: dict = {"kind": "distillation", "title": "Q?"}
        result = _truncate_row(row)
        self.assertEqual(result["body"], "")
        self.assertIs(result["truncated"], False)


# ---------------------------------------------------------------------------
# Merge tests
# ---------------------------------------------------------------------------


class MergeTests(unittest.TestCase):
    """_merge_results tests."""

    def test_distillations_first(self):
        dist_rows = [
            {"kind": "distillation", "body": "answer 1", "title": "Q1"},
            {"kind": "distillation", "body": "answer 2", "title": "Q2"},
        ]
        other_rows = [
            {"kind": "message", "body": "msg body", "title": None},
        ]
        result = _merge_results(dist_rows, other_rows)
        self.assertEqual(len(result["results"]), 3)  # type: ignore[arg-type]
        self.assertEqual(result["results"][0]["kind"], "distillation")  # type: ignore[index]
        self.assertEqual(result["results"][1]["kind"], "distillation")  # type: ignore[index]
        self.assertEqual(result["results"][2]["kind"], "message")  # type: ignore[index]

    def test_no_distillations_includes_nudge(self):
        dist_rows: list = []
        other_rows = [
            {"kind": "message", "body": "msg body", "title": None},
            {"kind": "resource", "body": "res body", "title": "R"},
        ]
        result = _merge_results(dist_rows, other_rows)
        self.assertIn("nudge", result)
        self.assertIsInstance(result["nudge"], str)
        self.assertIn("No distillation results found", result["nudge"])  # type: ignore[arg-type]

    def test_distillations_present_no_nudge(self):
        dist_rows = [{"kind": "distillation", "body": "answer", "title": "Q"}]
        other_rows: list = []
        result = _merge_results(dist_rows, other_rows)
        self.assertNotIn("nudge", result)

    def test_both_empty(self):
        result = _merge_results([], [])
        self.assertEqual(result["count"], 0)
        self.assertIn("nudge", result)

    def test_count_is_total(self):
        dist_rows = [{"kind": "distillation", "body": "a", "title": "Q"}]
        other_rows = [
            {"kind": "message", "body": "b", "title": None},
            {"kind": "resource", "body": "c", "title": "R"},
        ]
        result = _merge_results(dist_rows, other_rows)
        self.assertEqual(result["count"], 3)  # type: ignore[arg-type]

    def test_merge_truncates_all_rows(self):
        dist_rows = [{"kind": "distillation", "body": "x" * 800, "title": "Q"}]
        other_rows = [{"kind": "message", "body": "y" * 800, "title": None}]
        result = _merge_results(dist_rows, other_rows)
        for row in result["results"]:  # type: ignore[operator]
            self.assertIn("truncated", row)
            self.assertEqual(len(row["body"]), 700)


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

    def test_main_success_with_distillations(self):
        with self._patch_env():
            def mock_get(path, params=None, endpoint=None, anon_key=None):
                if params and params.get("kind") == "eq.distillation":
                    return [{"kind": "distillation", "body": "the answer", "title": "Best upscale?"}]
                return [{"kind": "message", "body": "some message", "title": None}]

            with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = main(["--query", "upscale"])
                    self.assertEqual(ret, 0)
                    output = json.loads(mock_stdout.getvalue())
                    self.assertIn("results", output)
                    self.assertEqual(len(output["results"]), 2)
                    self.assertNotIn("nudge", output)

    def test_main_no_distillations_includes_nudge(self):
        with self._patch_env():
            def mock_get(path, params=None, endpoint=None, anon_key=None):
                if params and params.get("kind") == "eq.distillation":
                    return []
                return [{"kind": "message", "body": "msg", "title": None}]

            with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = main(["--query", "upscale"])
                    self.assertEqual(ret, 0)
                    output = json.loads(mock_stdout.getvalue())
                    self.assertIn("nudge", output)

    def test_main_writes_to_file(self):
        with self._patch_env():
            def mock_get(path, params=None, endpoint=None, anon_key=None):
                return []

            with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
                with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
                    tmp_path = tmp.name
                try:
                    ret = main(["--query", "test", "--out", tmp_path])
                    self.assertEqual(ret, 0)
                    with open(tmp_path, "r", encoding="utf-8") as fh:
                        output = json.load(fh)
                    self.assertIn("results", output)
                finally:
                    os.unlink(tmp_path)

    def test_main_with_kinds_flag(self):
        with self._patch_env():
            all_params: list[dict[str, str]] = []

            def mock_get(path, params=None, endpoint=None, anon_key=None):
                all_params.append(dict(params or {}))
                return []

            with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
                    main(["--query", "test", "--kinds", "message,resource"])
                    # --kinds=message,resource excludes distillation, so only one query
                    # with kind=in.(message,resource) should be made
                    param_strs = [str(p) for p in all_params]
                    self.assertTrue(
                        any("in.(message,resource)" in ps for ps in param_strs),
                        f"Expected kind=in.(message,resource) in params: {all_params}",
                    )

    def test_main_with_sources_flag(self):
        with self._patch_env():
            all_params: list[dict[str, str]] = []

            def mock_get(path, params=None, endpoint=None, anon_key=None):
                all_params.append(dict(params or {}))
                return []

            with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
                    main(["--query", "test", "--sources", "hivemind"])
                    param_strs = [str(p) for p in all_params]
                    self.assertTrue(
                        any("hivemind" in ps for ps in param_strs),
                        f"Expected source filtering in params: {all_params}",
                    )

    def test_main_with_since_flag(self):
        with self._patch_env():
            all_params: list[dict[str, str]] = []

            def mock_get(path, params=None, endpoint=None, anon_key=None):
                all_params.append(dict(params or {}))
                return []

            with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
                    main(["--query", "test", "--since", "2024-06-01T00:00:00Z"])
                    param_strs = [str(p) for p in all_params]
                    self.assertTrue(
                        any("2024-06-01" in ps for ps in param_strs),
                        f"Expected since filtering in params: {all_params}",
                    )

    def test_main_with_limit_flag(self):
        with self._patch_env():
            all_params: list[dict[str, str]] = []

            def mock_get(path, params=None, endpoint=None, anon_key=None):
                all_params.append(dict(params or {}))
                return []

            with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
                    main(["--query", "test", "--limit", "5"])
            self.assertIn("limit", all_params[0])
            self.assertEqual(all_params[0]["limit"], "5")

    def test_main_distillation_query_has_kind_filter(self):
        """Verify the distillation query explicitly sets kind=eq.distillation."""
        with self._patch_env():
            distillation_params = None

            def mock_get(path, params=None, endpoint=None, anon_key=None):
                nonlocal distillation_params
                if params and params.get("kind") == "eq.distillation":
                    distillation_params = dict(params)
                return []

            with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
                    main(["--query", "test"])
            self.assertIsNotNone(distillation_params, "Distillation query was never made")
            self.assertEqual(distillation_params["kind"], "eq.distillation")

    def test_main_non_distillation_query_has_kind_filter(self):
        """Verify the non-distillation query uses kind=neq.distillation."""
        with self._patch_env():
            other_params = None

            def mock_get(path, params=None, endpoint=None, anon_key=None):
                nonlocal other_params
                if params and params.get("kind") == "neq.distillation":
                    other_params = dict(params)
                return []

            with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
                    main(["--query", "test"])
            self.assertIsNotNone(other_params, "Non-distillation query was never made")
            self.assertEqual(other_params["kind"], "neq.distillation")

    def test_main_handles_single_object_response(self):
        """PostgREST may return a single object instead of array for limit=1."""
        with self._patch_env():
            def mock_get(path, params=None, endpoint=None, anon_key=None):
                if params and params.get("kind") == "eq.distillation":
                    return {"kind": "distillation", "body": "answer", "title": "Q?"}
                return []

            with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = main(["--query", "test", "--limit", "1"])
                    self.assertEqual(ret, 0)
                    output = json.loads(mock_stdout.getvalue())
                    self.assertEqual(len(output["results"]), 1)

    def test_main_http_error(self):
        import urllib.error

        with self._patch_env():
            def mock_get(path, params=None, endpoint=None, anon_key=None):
                raise urllib.error.HTTPError(
                    "http://fake/", 500, "Internal Server Error", {}, None
                )

            with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = main(["--query", "test"])
                    self.assertEqual(ret, 2)
                    output = json.loads(mock_stdout.getvalue())
                    self.assertIn("error", output)
                    self.assertIn("500", output["error"])


# ===========================================================================
# Discovery
# ===========================================================================

if __name__ == "__main__":
    unittest.main()
