"""Tests for current-message/current-resource search behavior."""

from __future__ import annotations

import io
import json
import sys
import unittest
import unittest.mock
import urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from executors.search import run as search  # noqa: E402


def resource_row(rid="42", revision_id="7", state="accepted", title="Wan guide", body="Use Wan sampler", created_at="2026-01-01T00:00:00Z", embedded_revision_id=None):
    return {
        "id": rid,
        "origin_source": "web",
        "current_revision_id": revision_id,
        "created_at": created_at,
        "resource_revisions": {
            "id": embedded_revision_id or revision_id,
            "state": state,
            "kind": "guide",
            "title": title,
            "body": body,
            "metadata": {},
            "provenance": {"url": "https://example.test/guide"},
        },
    }


class ParserTests(unittest.TestCase):
    def test_current_parser(self):
        args = search.build_parser().parse_args(["--query", "Wan sampler", "--kinds", "resource", "--limit", "5"])
        self.assertEqual(args.query, "Wan sampler")
        self.assertEqual(args.kinds, "resource")
        self.assertEqual(args.limit, 5)

    def test_distillation_is_an_explicit_removed_surface(self):
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = search.main(["--query", "old", "--kinds", "distillation"])
        self.assertEqual(rc, 2)
        self.assertIn("removed", json.loads(out.getvalue())["error"])


class QueryConstructionTests(unittest.TestCase):
    def test_message_scope_is_raw_and_explicit(self):
        params = search._scope_params("message_feed", ["wan", "sampler"], sources=None, since=None, limit=20)
        self.assertEqual(params["select"], search._MESSAGE_COLUMNS)
        self.assertEqual(params["or"], "(content.ilike.*wan*,content.ilike.*sampler*)")
        self.assertEqual(params["order"], "created_at.desc")

    def test_resource_scope_uses_current_revision_columns(self):
        params = search._scope_params("resources", ["wan", "sampler"], sources=["web"], since="2026-01-01", limit=20, mode="and")
        self.assertIn("resource_revisions!resources_current_revision_fk", params["select"])
        self.assertEqual(params["origin_source"], "in.(web)")
        self.assertEqual(params["created_at"], "gte.2026-01-01")
        self.assertIn("resource_revisions.title.ilike.*wan*", params["and"])

    def test_resource_kind_filter_has_no_distillation_branch(self):
        self.assertIsNone(search._resource_kind_filter(["resource"]))
        self.assertEqual(search._resource_kind_filter(["workflow", "article"]), ["workflow", "article"])

    def test_message_source_filter_can_exclude_scope(self):
        self.assertIsNone(search._scope_params("message_feed", ["wan"], sources=["web"], since=None, limit=5))


class FreshnessTests(unittest.TestCase):
    def test_only_accepted_current_head_is_eligible(self):
        self.assertTrue(search._is_current_resource_head(resource_row()))
        self.assertFalse(search._is_current_resource_head(resource_row(revision_id="7", embedded_revision_id="8")))
        self.assertFalse(search._is_current_resource_head(resource_row(state="pending")))

    def test_run_scope_filters_stale_rows(self):
        rows = [resource_row(), resource_row(rid="43", revision_id="9", state="pending")]
        with unittest.mock.patch("executors.search.run._query_table", return_value=rows):
            fetched, errors = search._run_scope(
                "resources", ["wan"], sources=None, since=None, limit=20,
                endpoint="https://api.test", anon_key="anon",
            )
        self.assertEqual(errors, [])
        self.assertEqual({row[1]["id"] for row in fetched}, {"42"})


class RankingTests(unittest.TestCase):
    def test_resource_score_prefers_title_and_parseable_workflow(self):
        row = resource_row(title="Wan sampler", body="sampler",)
        row["resource_revisions"]["metadata"] = {"workflow_semantics": {"promotion_gates": {"parseable_workflow": True}}}
        self.assertEqual(search._score_hit(row, "resources", ["wan", "sampler"], "wan sampler"), 18)

    def test_shape_resource_exposes_revision_not_native_payload(self):
        row = resource_row()
        hit = search._shape_hit(row, "resources")
        self.assertEqual(hit["item_id"], "42")
        self.assertEqual(hit["revision_id"], "7")
        self.assertNotIn("payload", hit)
        self.assertEqual(hit["url"], "https://example.test/guide")

    def test_shape_message_stringifies_snowflake(self):
        hit = search._shape_hit({"message_id": 9007199254740993, "content": "hello", "created_at": "2026-01-01T00:00:00Z"}, "message_feed")
        self.assertEqual(hit["item_id"], "9007199254740993")

    def test_merge_deduplicates_by_source_and_id_and_pages(self):
        rows = [
            ("resources", resource_row()),
            ("resources", resource_row()),
            ("message_feed", {"message_id": "99", "content": "wan", "created_at": "2026-02-01T00:00:00Z"}),
        ]
        result = search._merge_results(rows, ["wan"], None, 1)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["total"], 2)
        self.assertTrue(result["has_more"])


class TransportTests(unittest.TestCase):
    def test_scope_failure_is_recorded(self):
        with unittest.mock.patch("executors.search.run._query_table", side_effect=urllib.error.URLError("offline")):
            rows, errors = search._run_scopes(
                [("message_feed", ["wan"]), ("resources", ["wan"])],
                sources=None, since=None, limit=10, endpoint="https://api.test", anon_key="anon",
            )
        self.assertEqual(rows, [])
        self.assertEqual(len(errors), 2)
        self.assertTrue(all("URLError" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
