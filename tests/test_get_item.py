"""Tests for exact message/resource/revision retrieval."""

from __future__ import annotations

import io
import json
import sys
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from executors.get_item.run import (  # noqa: E402
    _assemble_result,
    _fetch_cited_by,
    _fetch_outgoing,
    _fetch_row,
    build_parser,
    main,
)


class ParserTests(unittest.TestCase):
    def test_active_kinds(self):
        parser = build_parser()
        self.assertEqual(parser.parse_args(["--kind", "message", "--id", "123"]).kind, "message")
        self.assertEqual(parser.parse_args(["--kind", "resource", "--id", "42", "--revision-id", "7"]).revision_id, "7")
        self.assertEqual(parser.parse_args(["--kind", "revision", "--id", "7"]).kind, "revision")

    def test_distillation_kind_is_rejected(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["--kind", "distillation", "--id", "7"])


class FetchTests(unittest.TestCase):
    endpoint = "https://api.test"
    anon_key = "anon"

    def test_message_fetch_uses_string_id(self):
        calls = []

        def fake_get(path, params=None, **kwargs):
            calls.append((path, params))
            return [{"message_id": "9007199254740993", "content": "hello"}]

        with unittest.mock.patch("executors.get_item.run.postgrest_get", side_effect=fake_get):
            row = _fetch_row("message", "9007199254740993", endpoint=self.endpoint, anon_key=self.anon_key)
        self.assertEqual(row["content"], "hello")  # type: ignore[index]
        self.assertEqual(calls[0][1]["message_id"], "eq.9007199254740993")

    def test_resource_fetches_current_accepted_revision(self):
        def fake_get(path, params=None, **kwargs):
            if path == "resources":
                return [{"id": "42", "current_revision_id": "7", "origin_source": "web"}]
            if path == "resource_revisions":
                return [{"id": "7", "resource_id": "42", "state": "accepted", "kind": "guide", "title": "Guide", "body": "Body", "metadata": {}, "provenance": {}}]
            raise AssertionError(path)

        with unittest.mock.patch("executors.get_item.run.postgrest_get", side_effect=fake_get):
            row = _fetch_row("resource", "42", endpoint=self.endpoint, anon_key=self.anon_key)
        self.assertEqual(row["revision_id"], "7")  # type: ignore[index]
        self.assertEqual(row["body"], "Body")  # type: ignore[index]

    def test_unpinned_pending_resource_is_identity_only(self):
        def fake_get(path, params=None, **kwargs):
            if path == "resources":
                return [{"id": "42", "current_revision_id": "8"}]
            return [{"id": "8", "resource_id": "42", "state": "pending", "kind": "guide", "title": "Candidate", "body": "Candidate body"}]

        with unittest.mock.patch("executors.get_item.run.postgrest_get", side_effect=fake_get):
            row = _fetch_row("resource", "42", endpoint=self.endpoint, anon_key=self.anon_key)
        self.assertEqual(row, {"resource_id": "42", "current_revision_id": "8", "unreviewed": True})

    def test_explicit_revision_returns_exact_candidate(self):
        def fake_get(path, params=None, **kwargs):
            if path == "resources":
                return [{"id": "42", "current_revision_id": "7", "origin_source": "web"}]
            return [{"id": "8", "resource_id": "42", "state": "pending", "kind": "guide", "title": "Candidate", "body": "Candidate body", "metadata": {}, "provenance": {}}]

        with unittest.mock.patch("executors.get_item.run.postgrest_get", side_effect=fake_get):
            row = _fetch_row("resource", "42", endpoint=self.endpoint, anon_key=self.anon_key, revision_id="8")
        self.assertEqual(row["revision_id"], "8")  # type: ignore[index]
        self.assertEqual(row["unreviewed"], True)  # type: ignore[index]

    def test_revision_fetch_is_direct(self):
        with unittest.mock.patch("executors.get_item.run.postgrest_get", return_value=[{"id": "7", "state": "accepted"}]) as get:
            row = _fetch_row("revision", "7", endpoint=self.endpoint, anon_key=self.anon_key)
        self.assertEqual(row["id"], "7")  # type: ignore[index]
        self.assertEqual(get.call_args.args[0], "resource_revisions")


class ReferenceTests(unittest.TestCase):
    endpoint = "https://api.test"
    anon_key = "anon"

    def test_outgoing_and_backlinks_use_derived_views(self):
        with unittest.mock.patch("executors.get_item.run.postgrest_get", return_value=[{"source_id": "7"}]) as get:
            outgoing = _fetch_outgoing("revision", "7", endpoint=self.endpoint, anon_key=self.anon_key)
        self.assertEqual(outgoing, [{"source_id": "7"}])
        self.assertEqual(get.call_args.args[0], "knowledge_outgoing_references")

        with unittest.mock.patch("executors.get_item.run.postgrest_get", return_value=[{"linked_id": "42"}]) as get:
            backlinks = _fetch_cited_by("resource", "42", endpoint=self.endpoint, anon_key=self.anon_key)
        self.assertEqual(backlinks, [{"linked_id": "42"}])
        self.assertEqual(get.call_args.args[0], "knowledge_backlinks")

    def test_assemble_resource_adds_current_references(self):
        def fake_get(path, params=None, **kwargs):
            if path == "resources":
                return [{"id": "42", "current_revision_id": "7", "origin_source": "web"}]
            if path == "resource_revisions":
                return [{"id": "7", "resource_id": "42", "state": "accepted", "kind": "guide", "title": "Guide", "body": "Body", "metadata": {}, "provenance": {}}]
            if path == "knowledge_backlinks":
                return [{"linked_id": "42"}]
            if path == "knowledge_outgoing_references":
                return [{"source_id": "7", "linked_id": "9"}]
            raise AssertionError(path)

        with unittest.mock.patch("executors.get_item.run.postgrest_get", side_effect=fake_get):
            result = _assemble_result("resource", "42", endpoint=self.endpoint, anon_key=self.anon_key)
        self.assertEqual(result["item"]["revision_id"], "7")
        self.assertEqual(result["cited_by"][0]["linked_id"], "42")
        self.assertEqual(result["references"][0]["linked_id"], "9")


class MainTests(unittest.TestCase):
    def test_not_found_is_json(self):
        with unittest.mock.patch("executors.get_item.run.postgrest_get", return_value=[]):
            with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = main(["--kind", "revision", "--id", "9"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["error"], "not_found")


if __name__ == "__main__":
    unittest.main()
