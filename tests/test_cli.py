"""Focused contract tests for the full human CLI facade."""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

import cli
from executors.search.run import _scope_params


class CliTests(unittest.TestCase):
    def test_search_delegates_to_canonical_raw_search_and_keeps_json(self):
        payload = {
            "results": [{"kind": "message", "item_id": "9223372036854775807", "body": "hit"}],
            "count": 1,
            "total": 1,
            "has_more": False,
            "page": 1,
            "pages": 1,
            "next_offset": None,
        }
        seen: list[list[str]] = []

        def fake_main(argv):
            seen.append(argv)
            print(json.dumps(payload))
            return 0

        with mock.patch("executors.search.run.main", side_effect=fake_main):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cli.cmd_search(
                    "wan animate",
                    {"channel": "minimax_h3_chatter", "limit": "7", "offset": "14", "json": True},
                )
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue()), payload)
        self.assertEqual(
            seen,
            [[
                "--query", "wan animate", "--limit", "7", "--channel", "minimax_h3_chatter",
                "--offset", "14",
            ]],
        )

    def test_search_preserves_canonical_stderr_and_exit_code(self):
        def fake_main(_argv):
            print(json.dumps({"error": "backend unavailable"}))
            import sys
            print("retry diagnostics", file=sys.stderr)
            return 2

        with mock.patch("executors.search.run.main", side_effect=fake_main):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = cli.cmd_search("hello", {"json": True})
        self.assertEqual(rc, 2)
        self.assertIn("retry diagnostics", err.getvalue())

    def test_search_human_mode_preserves_snowflake_as_string(self):
        payload = {
            "results": [{
                "kind": "message", "item_id": "1512127379039060118", "body": "hello",
                "author": "A", "context": "minimax_h3_chatter", "created_at": "2026-09-08T00:00:00Z",
                "source": "banodoco-discord",
            }],
            "count": 1, "total": 1, "has_more": False, "page": 1, "pages": 1,
        }

        def fake_main(_argv):
            print(json.dumps(payload))
            return 0

        with mock.patch("executors.search.run.main", side_effect=fake_main) as search_main:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cli.cmd_search("hello", {})
        self.assertEqual(rc, 0)
        self.assertIn("id=1512127379039060118", out.getvalue())
        self.assertNotIn("1512127379039060118.0", out.getvalue())
        search_main.assert_called_once_with(["--query", "hello", "--limit", "20"])

    def test_search_passes_normalized_date_bounds_and_resource_meta_kind(self):
        def fake_main(argv):
            print(json.dumps({"results": [], "total": 0, "page": 1, "pages": 1}))
            return 0

        with mock.patch("executors.search.run.main", side_effect=fake_main) as search_main:
            cli.cmd_search("hello", {"since": "yesterday", "until": "today", "resources": True})
        search_main.assert_called_once_with([
            "--query", "hello", "--limit", "20", "--since", cli._parse_date("yesterday"),
            "--until", cli._parse_date("today"), "--kinds", "resource",
        ])

    def test_recent_preserves_snowflake_filter_as_string(self):
        row = [{"message_id": 1512127379039060118, "content": "hello", "author_name": "A"}]
        with mock.patch.object(cli, "get", return_value=(200, {}, row)) as get:
            rc = cli.cmd_recent({"before-id": "1512127379039060118", "limit": "3"})
        self.assertEqual(rc, 0)
        params = get.call_args.args[1]
        self.assertEqual(params["message_id"], "lt.1512127379039060118")

    def test_recent_repeated_terms_and_keyset_are_forwarded(self):
        rows = [{"message_id": 20, "content": "wan lora", "created_at": "2026-09-08"}]
        with mock.patch.object(cli, "get", side_effect=[(200, {}, rows), (206, {"Content-Range": "0-0/1"}, rows)]) as get:
            rc = cli.cmd_recent({"term": ["wan", "lora"], "after-id": "10", "order": "asc", "json": True})
        self.assertEqual(rc, 0)
        params = get.call_args_list[0].args[1]
        self.assertEqual(params["content"], ["ilike.*wan*", "ilike.*lora*"])
        self.assertEqual(params["message_id"], "gt.10")

    def test_get_json_not_found_returns_nonzero(self):
        with mock.patch.object(cli, "get", return_value=(200, {}, [])):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cli.cmd_get("1512127379039060118", {"json": True})
        self.assertEqual(rc, 1)
        self.assertIn("not found", out.getvalue())

    def test_get_accepts_discord_url_without_numeric_coercion(self):
        row = [{"message_id": 1512127379039060118, "content": "hello", "author_name": "A"}]
        with mock.patch.object(cli, "get", return_value=(200, {}, row)) as get:
            rc = cli.cmd_get("https://discord.com/channels/1/2/1512127379039060118", {})
        self.assertEqual(rc, 0)
        self.assertEqual(get.call_args.args[1]["message_id"], "eq.1512127379039060118")

    def test_search_scope_encodes_repeated_date_bounds(self):
        params = _scope_params(
            "message_feed", ["hello"], sources=None, since="2026-09-01",
            until="2026-09-08", limit=10,
        )
        self.assertEqual(params["created_at"], ["gte.2026-09-01", "lt.2026-09-08"])


if __name__ == "__main__":
    unittest.main()
