"""Tests for the search executor — mocked HTTP, no network.

The executor searches three raw tables (message_feed / external_resources /
distillations) with per-token ILIKE predicates and client-side ranking.  It
MUST never query the ``unified_feed`` view (statement-timeout trap) and never
project ``payload`` (full Comfy JSON).
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
import urllib.error
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))

from executors.search import run as search  # noqa: E402


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class CLITests(unittest.TestCase):
    """Argument parsing tests."""

    def test_query_required(self):
        parser = search.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])

    def test_query_parsed(self):
        parser = search.build_parser()
        args = parser.parse_args(["--query", "upscale model"])
        self.assertEqual(args.query, "upscale model")
        self.assertEqual(args.limit, 20)

    def test_limit_parsed(self):
        parser = search.build_parser()
        args = parser.parse_args(["--query", "test", "--limit", "5"])
        self.assertEqual(args.limit, 5)

    def test_offset_parsed(self):
        parser = search.build_parser()
        args = parser.parse_args(["--query", "test", "--limit", "10", "--offset", "20"])
        self.assertEqual(args.limit, 10)
        self.assertEqual(args.offset, 20)

    def test_offset_defaults_zero(self):
        parser = search.build_parser()
        args = parser.parse_args(["--query", "test"])
        self.assertEqual(args.offset, 0)

    def test_kinds_parsed(self):
        parser = search.build_parser()
        args = parser.parse_args(["--query", "test", "--kinds", "message,resource"])
        self.assertEqual(args.kinds, "message,resource")

    def test_sources_parsed(self):
        parser = search.build_parser()
        args = parser.parse_args(["--query", "test", "--sources", "banodoco-discord,hivemind"])
        self.assertEqual(args.sources, "banodoco-discord,hivemind")

    def test_since_parsed(self):
        parser = search.build_parser()
        args = parser.parse_args(["--query", "test", "--since", "2024-01-01T00:00:00Z"])
        self.assertEqual(args.since, "2024-01-01T00:00:00Z")

    def test_out_parsed(self):
        parser = search.build_parser()
        args = parser.parse_args(["--query", "test", "--out", "/tmp/out.json"])
        self.assertEqual(args.out, "/tmp/out.json")

    def test_all_flags(self):
        parser = search.build_parser()
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
# Tokenization tests
# ---------------------------------------------------------------------------


class TokenizationTests(unittest.TestCase):
    """_query_tokens / _distinctive_tokens."""

    def test_query_tokens_splits_safe_runes(self):
        self.assertEqual(
            search._query_tokens("wan animate workflow FLUX.1 ipadapter+face"),
            ["wan", "animate", "workflow", "FLUX.1", "ipadapter+face"],
        )

    def test_distinctive_tokens_drops_stopwords_and_digits(self):
        tokens = search._distinctive_tokens("how to make a wan video at 16fps", 8)
        self.assertEqual(tokens, ["wan", "16fps"])

    def test_distinctive_tokens_falls_back_to_raw(self):
        tokens = search._distinctive_tokens("how to make a video", 8)
        self.assertEqual(tokens, ["how", "to", "make", "a", "video"])

    def test_distinctive_tokens_caps(self):
        tokens = search._distinctive_tokens("wan ltx hotshot animatediff sdxl flux", 4)
        self.assertEqual(len(tokens), 4)

    def test_distinctive_tokens_empty_query(self):
        self.assertEqual(search._distinctive_tokens("", 8), [])
        self.assertEqual(search._distinctive_tokens("   ", 8), [])

    def test_variants_known_and_default(self):
        self.assertEqual(search._variants("IPADAPTER"), ("ipadapter", "ip-adapter", "ip_adapter"))
        self.assertEqual(search._variants("zzz"), ("zzz",))

    def test_ilike_arms_include_variants_and_skip_spaces(self):
        arms = search._ilike_arms(("title", "body"), "hotshot")
        self.assertIn("title.ilike.*hotshot*", arms)
        self.assertIn("body.ilike.*hotshotxl*", arms)
        # Space-containing variants must never reach SQL (trigram break).
        for arm in arms:
            self.assertNotIn("hot shot", arm)
            self.assertNotIn(" ", arm.split(".*", 1)[1].rsplit("*", 1)[0])

    def test_ilike_arms_cap(self):
        arms = search._ilike_arms(("title", "body"), "ipadapter")
        self.assertLessEqual(len(arms), search._MAX_ILIKE_ARMS)

    def test_phrase_tokens(self):
        self.assertEqual(search._phrase_tokens("wan animate workflow"), "wan animate")
        self.assertIsNone(search._phrase_tokens(""))


# ---------------------------------------------------------------------------
# Query construction tests
# ---------------------------------------------------------------------------


class QueryConstructionTests(unittest.TestCase):
    """_scope_params / _resource_kind_filter."""

    def _params(self, table, query, since=None, **kwargs):
        tokens = search._distinctive_tokens(query, search._SQL_TOKEN_CAP)
        return search._scope_params(table, tokens, sources=None, since=since, limit=20, **kwargs)

    def test_message_scope_shape(self):
        params = self._params("message_feed", "wan animate")
        self.assertEqual(params["select"], search._MESSAGE_COLUMNS)
        self.assertIn("order", params)
        self.assertEqual(params["or"], "(content.ilike.*wan*,content.ilike.*animate*)")

    def test_message_scope_unordered(self):
        params = self._params("message_feed", "wan animate", ordered=False)
        self.assertNotIn("order", params)

    def test_message_scope_source_excluded(self):
        tokens = search._distinctive_tokens("wan", search._SQL_TOKEN_CAP)
        params = search._scope_params("message_feed", tokens, sources=["hivemind"], since=None, limit=20)
        self.assertIsNone(params)

    def test_message_scope_source_included(self):
        tokens = search._distinctive_tokens("wan", search._SQL_TOKEN_CAP)
        params = search._scope_params("message_feed", tokens, sources=["banodoco-discord"], since=None, limit=20)
        self.assertIsNotNone(params)

    def test_resource_scope_and_mode(self):
        params = self._params("external_resources", "wan animate", mode="and")
        self.assertEqual(params["and"], "(title.ilike.*wan*,title.ilike.*animate*)")
        self.assertNotIn("or", params)

    def test_resource_scope_or_mode_title_and_body(self):
        params = self._params("external_resources", "wan animate", mode="or")
        self.assertIn("or", params)
        self.assertIn("title.ilike.*wan*", params["or"])
        # The recall pass covers body too (unordered fetch avoids the 2.1s
        # ordered title+body sort).
        self.assertIn("body.ilike.*wan*", params["or"])

    def test_resource_scope_kind_filter(self):
        tokens = search._distinctive_tokens("lora", search._SQL_TOKEN_CAP)
        params = search._scope_params(
            "external_resources", tokens, sources=None, since=None, limit=20,
            kind_filter=["workflow", "article"],
        )
        self.assertEqual(params["kind"], "in.(workflow,article)")

    def test_resource_scope_sources(self):
        tokens = search._distinctive_tokens("lora", search._SQL_TOKEN_CAP)
        params = search._scope_params(
            "external_resources", tokens, sources=["youtube", "civitai"], since=None, limit=20
        )
        self.assertEqual(params["source"], "in.(youtube,civitai)")

    def test_distillation_scope_status_and_columns(self):
        params = self._params("distillations", "wan")
        self.assertEqual(params["select"], search._DISTILLATION_COLUMNS)
        self.assertEqual(params["status"], "in.(pending,approved)")
        self.assertIn("question.ilike.*wan*", params["or"])
        self.assertIn("answer.ilike.*wan*", params["or"])
        self.assertIn("conditions.ilike.*wan*", params["or"])

    def test_distillation_scope_source_excluded(self):
        tokens = search._distinctive_tokens("wan", search._SQL_TOKEN_CAP)
        params = search._scope_params("distillations", tokens, sources=["youtube"], since=None, limit=20)
        self.assertIsNone(params)

    def test_since_applied(self):
        params = self._params("message_feed", "wan", since="2024-01-01T00:00:00Z")
        self.assertEqual(params["created_at"], "gte.2024-01-01T00:00:00Z")

    def test_channel_filter(self):
        tokens = search._distinctive_tokens("wan", search._SQL_TOKEN_CAP)
        params = search._scope_params(
            "message_feed", tokens, sources=None, since=None, limit=20, channel="wan_chatter"
        )
        self.assertEqual(params["channel_name"], "eq.wan_chatter")
        self.assertIn("content.ilike.*wan*", params["or"])

    def test_author_filter(self):
        tokens = search._distinctive_tokens("wan", search._SQL_TOKEN_CAP)
        params = search._scope_params(
            "message_feed", tokens, sources=None, since=None, limit=20, author="Kijai"
        )
        self.assertEqual(params["author_name"], "eq.Kijai")

    def test_thread_filter(self):
        tokens = search._distinctive_tokens("wan", search._SQL_TOKEN_CAP)
        params = search._scope_params(
            "message_filters", tokens, sources=None, since=None, limit=20, thread="123456789"
        )
        self.assertEqual(params["thread_id"], "eq.123456789")
        self.assertEqual(params["select"], search._THREAD_COLUMNS)
        self.assertIn("content.ilike.*wan*", params["or"])

    def test_resource_kind_filter_meta_kind(self):
        self.assertIsNone(search._resource_kind_filter(["message", "resource"]))
        self.assertIsNone(search._resource_kind_filter(["resource"]))
        self.assertEqual(search._resource_kind_filter(["workflow"]), ["workflow"])
        self.assertEqual(
            search._resource_kind_filter(["message", "workflow", "article"]),
            ["workflow", "article"],
        )

    def test_projections_never_payload_or_star(self):
        for table in ("message_feed", "external_resources", "distillations"):
            params = self._params(table, "wan")
            select = params["select"]
            self.assertNotIn("payload", select)
            self.assertNotEqual(select, "*")
            self.assertNotIn("payload", params["or"])


# ---------------------------------------------------------------------------
# Transport tests
# ---------------------------------------------------------------------------


class TransportTests(unittest.TestCase):
    """_run_scope pass logic and the statement-timeout degrade."""

    def setUp(self):
        self.endpoint = "http://fake.example.com/rest/v1"
        self.anon_key = "fake-anon-key"

    def test_message_scope_queries_or_once(self):
        calls: list[dict] = []

        def mock_get(path, params=None, **kwargs):
            calls.append(dict(params or {}))
            return [{"message_id": 1, "content": "wan!"}]

        with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
            rows, errors = search._run_scope(
                "message_feed",
                ["wan"],
                sources=None,
                since=None,
                limit=100,
                endpoint=self.endpoint,
                anon_key=self.anon_key,
            )
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(calls), 1)  # OR-only, never an AND attempt
        self.assertIn("or", calls[0])
        self.assertNotIn("and", calls[0])

    def test_message_scope_degrades_on_statement_timeout(self):
        calls: list[dict] = []
        timeout_body = io.BytesIO(
            b'{"code":"57014","message":"canceling statement due to statement timeout"}'
        )

        def mock_get(path, params=None, **kwargs):
            calls.append(dict(params or {}))
            if "order" in params:
                raise urllib.error.HTTPError(
                    "http://fake/", 500, "Internal Server Error", {}, timeout_body
                )
            return [{"message_id": 2, "content": "found without order"}]

        with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
            rows, errors = search._run_scope(
                "message_feed",
                ["hotshot"],
                sources=None,
                since=None,
                limit=100,
                endpoint=self.endpoint,
                anon_key=self.anon_key,
            )
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual([c for c in calls if "order" in c], [calls[0]])
        self.assertNotIn("order", calls[1])
        self.assertLessEqual(len(calls), 2)

    def test_message_scope_non_timeout_error_fails_scope(self):
        def mock_get(path, params=None, **kwargs):
            raise urllib.error.HTTPError("http://fake/", 400, "Bad Request", {}, None)

        with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
            rows, errors = search._run_scope(
                "message_feed",
                ["wan"],
                sources=None,
                since=None,
                limit=100,
                endpoint=self.endpoint,
                anon_key=self.anon_key,
            )
        self.assertEqual(rows, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("400", errors[0])

    def test_message_scope_timeout_degrade(self):
        calls: list[dict] = []

        def mock_get(path, params=None, **kwargs):
            calls.append(dict(params or {}))
            if "order" in params:
                raise TimeoutError("read timed out")
            return [{"message_id": 3, "content": "degraded"}]

        with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
            rows, errors = search._run_scope(
                "message_feed",
                ["upscale"],
                sources=None,
                since=None,
                limit=100,
                endpoint=self.endpoint,
                anon_key=self.anon_key,
            )
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 1)

    def test_resource_scope_and_then_or_fallback_when_thin(self):
        calls: list[dict] = []

        def mock_get(path, params=None, **kwargs):
            calls.append(dict(params or {}))
            if "and" in params:
                return []
            return [{"id": 7, "kind": "workflow", "title": "Wan workflow", "body": "x"}]

        with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
            rows, errors = search._run_scope(
                "external_resources",
                ["wan"],
                sources=None,
                since=None,
                limit=100,
                endpoint=self.endpoint,
                anon_key=self.anon_key,
            )
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual([c for c in calls if "and" in c], [calls[0]])
        self.assertIn("or", calls[1])

    def test_resource_scope_skips_or_when_and_rich(self):
        calls: list[dict] = []

        def mock_get(path, params=None, **kwargs):
            calls.append(dict(params or {}))
            return [
                {"id": i, "kind": "workflow", "title": f"Wan {i}", "body": "x"}
                for i in range(5)
            ]

        with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
            rows, errors = search._run_scope(
                "external_resources",
                ["wan"],
                sources=None,
                since=None,
                limit=100,
                endpoint=self.endpoint,
                anon_key=self.anon_key,
            )
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 5)
        self.assertEqual(len(calls), 1)  # no OR fallback after a rich AND

    def test_distillation_scope_single_or(self):
        calls: list[dict] = []

        def mock_get(path, params=None, **kwargs):
            calls.append(dict(params or {}))
            return []

        with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
            rows, errors = search._run_scope(
                "distillations",
                ["wan"],
                sources=None,
                since=None,
                limit=100,
                endpoint=self.endpoint,
                anon_key=self.anon_key,
            )
        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["status"], "in.(pending,approved)")

    def test_run_scopes_merges_parallel_results(self):
        def mock_get(path, params=None, **kwargs):
            if path == "message_feed":
                return [{"message_id": 1, "content": "m"}]
            if path == "external_resources":
                # 5 rows so the AND pass is rich and no OR fallback fires.
                return [
                    {"id": i, "kind": "workflow", "title": f"r{i}", "body": ""}
                    for i in range(5)
                ]
            return [{"id": 3, "question": "q", "answer": "a", "status": "approved"}]

        with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
            rows, errors = search._run_scopes(
                [("message_feed", ["wan"]), ("external_resources", ["wan"]), ("distillations", ["wan"])],
                sources=None,
                since=None,
                limit=100,
                endpoint=self.endpoint,
                anon_key=self.anon_key,
            )
        self.assertEqual(errors, [])
        tables = sorted(t for t, _ in rows)
        self.assertEqual(
            tables,
            ["distillations"] + ["external_resources"] * 5 + ["message_feed"],
        )

    def test_scope_failure_degrades_other_scopes(self):
        def mock_get(path, params=None, **kwargs):
            if path == "message_feed":
                raise urllib.error.HTTPError("http://fake/", 500, "boom", {}, None)
            return [
                {"id": i, "kind": "workflow", "title": f"ok{i}", "body": ""}
                for i in range(5)
            ]

        with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
            rows, errors = search._run_scopes(
                [("message_feed", ["wan"]), ("external_resources", ["wan"])],
                sources=None,
                since=None,
                limit=100,
                endpoint=self.endpoint,
                anon_key=self.anon_key,
            )
        self.assertEqual(len(rows), 5)
        self.assertEqual(len(errors), 1)

    def test_urlerror_recorded_not_silent_success(self):
        """A DNS/connection failure must surface as a scope error, never as
        a silent empty success (threads do not propagate exceptions)."""
        def mock_get(path, params=None, **kwargs):
            raise urllib.error.URLError(OSError("name resolution failed"))

        with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
            rows, errors = search._run_scopes(
                [("message_feed", ["wan"]), ("external_resources", ["wan"])],
                sources=None,
                since=None,
                limit=100,
                endpoint=self.endpoint,
                anon_key=self.anon_key,
            )
        self.assertEqual(rows, [])
        self.assertEqual(len(errors), 2)
        for message in errors:
            self.assertIn("message_feed" if "message" in message else "external_resources", message)
            self.assertIn("URLError", message)

    def test_json_decode_failure_recorded(self):
        def mock_get(path, params=None, **kwargs):
            raise ValueError("no json object could be decoded")

        with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
            rows, errors = search._run_scope(
                "message_feed",
                ["wan"],
                sources=None,
                since=None,
                limit=100,
                endpoint=self.endpoint,
                anon_key=self.anon_key,
            )
        self.assertEqual(rows, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("ValueError", errors[0])


# ---------------------------------------------------------------------------
# Ranking / merge / shape tests
# ---------------------------------------------------------------------------


class RankingMergeTests(unittest.TestCase):
    """_score_hit / _merge_results / _shape_hit."""

    def test_score_title_beats_body(self):
        row = {"title": "wan animate workflow", "body": ""}
        self.assertEqual(search._score_hit(row, "external_resources", ["wan", "animate"], None), 10)
        row2 = {"title": "generic", "body": "wan animate stuff"}
        self.assertEqual(search._score_hit(row2, "external_resources", ["wan", "animate"], None), 6)

    def test_score_approved_distillation_bonus(self):
        row = {"question": "wan?", "answer": "use wan", "status": "approved"}
        # 5 (question token) + 3 (answer token) + 4 (approved) = 12
        self.assertEqual(search._score_hit(row, "distillations", ["wan"], None), 12)
        row_pending = {"question": "wan?", "answer": "use wan", "status": "pending"}
        self.assertEqual(search._score_hit(row_pending, "distillations", ["wan"], None), 8)

    def test_score_parseable_workflow_bonus(self):
        row = {
            "title": "wan", "body": "",
            "metadata": {"workflow_semantics": {"promotion_gates": {"parseable_workflow": True}}},
        }
        self.assertEqual(search._score_hit(row, "external_resources", ["wan"], None), 8)
        row2 = {"title": "wan", "body": "", "metadata": {"has_workflow_json": True}}
        self.assertEqual(search._score_hit(row2, "external_resources", ["wan"], None), 8)

    def test_score_phrase_bonus(self):
        row = {"title": "wan animate video", "body": ""}
        self.assertEqual(search._score_hit(row, "external_resources", ["wan", "animate"], "wan animate"), 12)

    def test_score_matches_variant_spellings(self):
        # Retrieval expands ipadapter -> ip-adapter; scoring must too.
        row = {"title": "IP-Adapter Face", "body": ""}
        self.assertEqual(search._score_hit(row, "external_resources", ["ipadapter"], None), 5)

    def test_score_conditions_count(self):
        row = {"question": "generic", "answer": "generic", "conditions": "wan animate only"}
        self.assertEqual(search._score_hit(row, "distillations", ["wan", "animate"], None), 6)

    def test_score_dedupes_repeated_tokens(self):
        row = {"title": "wan", "body": ""}
        self.assertEqual(search._score_hit(row, "external_resources", ["wan", "wan"], None), 5)

    def test_rank_tokens_use_wider_cap_than_sql_tokens(self):
        query = "wan ltx hotshot animatediff sdxl flux vace kling veo"
        sql_tokens = search._distinctive_tokens(query, search._SQL_TOKEN_CAP)
        rank_tokens = search._distinctive_tokens(query, search._RANK_TOKEN_CAP)
        self.assertEqual(len(sql_tokens), search._SQL_TOKEN_CAP)
        self.assertEqual(len(rank_tokens), search._RANK_TOKEN_CAP)
        self.assertGreater(len(rank_tokens), len(sql_tokens))

    def test_merge_dedupes_by_table_and_id(self):
        rows = [
            ("external_resources", {"id": 1, "kind": "workflow", "title": "wan", "body": ""}),
            ("external_resources", {"id": 1, "kind": "workflow", "title": "wan", "body": ""}),
            ("message_feed", {"message_id": "1", "content": "wan"}),  # same numeric id, different table
        ]
        merged = search._merge_results(rows, ["wan"], None, 10, had_distillations=False)
        self.assertEqual(merged["count"], 2)
        self.assertIn("nudge", merged)

    def test_merge_orders_by_score_then_recency(self):
        rows = [
            ("external_resources", {"id": 1, "kind": "workflow", "title": "wan", "body": "",
                                    "created_at": "2026-01-01T00:00:00Z"}),
            ("external_resources", {"id": 2, "kind": "workflow", "title": "wan animate", "body": "",
                                    "created_at": "2025-01-01T00:00:00Z"}),
        ]
        merged = search._merge_results(rows, ["wan", "animate"], None, 10, had_distillations=False)
        self.assertEqual(merged["results"][0]["item_id"], "2")  # higher score first

    def test_merge_sort_recent_orders_by_recency_first(self):
        rows = [
            ("external_resources", {"id": 1, "kind": "workflow", "title": "wan animate", "body": "",
                                    "created_at": "2025-01-01T00:00:00Z"}),
            ("external_resources", {"id": 2, "kind": "workflow", "title": "wan", "body": "",
                                    "created_at": "2026-06-01T00:00:00Z"}),
        ]
        merged = search._merge_results(
            rows, ["wan", "animate"], None, 10, had_distillations=False, sort="recent"
        )
        # Recency wins even though row 1 scores higher.
        self.assertEqual(merged["results"][0]["item_id"], "2")
        self.assertEqual(merged["results"][1]["item_id"], "1")

    def test_merge_sort_default_is_relevance(self):
        rows = [
            ("message_feed", {"message_id": 1, "content": "wan", "created_at": "2026-06-01T00:00:00Z"}),
            ("external_resources", {"id": 2, "kind": "workflow", "title": "wan animate", "body": "",
                                    "created_at": "2025-01-01T00:00:00Z"}),
        ]
        merged = search._merge_results(rows, ["wan", "animate"], None, 10, had_distillations=False)
        self.assertEqual(merged["results"][0]["item_id"], "2")  # relevance default

    def test_main_sort_flag_parsed(self):
        parser = search.build_parser()
        args = parser.parse_args(["--query", "wan", "--sort", "recent"])
        self.assertEqual(args.sort, "recent")
        args = parser.parse_args(["--query", "wan"])
        self.assertEqual(args.sort, "relevance")

    def test_merge_caps_at_limit(self):
        rows = [
            ("message_feed", {"message_id": i, "content": "wan"})
            for i in range(20)
        ]
        merged = search._merge_results(rows, ["wan"], None, 5, had_distillations=True)
        self.assertEqual(merged["count"], 5)
        self.assertNotIn("nudge", merged)

    def test_merge_offset_pages_through_ranked_pool(self):
        rows = [
            ("message_feed", {"message_id": i, "content": "wan", "created_at": f"2026-01-{i+1:02d}T00:00:00Z"})
            for i in range(1, 21)
        ]
        page1 = search._merge_results(rows, ["wan"], None, 5, had_distillations=True, offset=0)
        page2 = search._merge_results(rows, ["wan"], None, 5, had_distillations=True, offset=5)
        page3 = search._merge_results(rows, ["wan"], None, 5, had_distillations=True, offset=10)
        page4 = search._merge_results(rows, ["wan"], None, 5, had_distillations=True, offset=15)
        ids = [page1, page2, page3, page4]
        all_ids = [r["item_id"] for page in ids for r in page["results"]]
        self.assertEqual(len(all_ids), 20)
        self.assertEqual(len(set(all_ids)), 20)  # disjoint, fully covered
        self.assertEqual(page1["total"], 20)
        for page in (page1, page2, page3):
            self.assertTrue(page["has_more"])
        self.assertFalse(page4["has_more"])  # 15 + 5 = 20 -> exhausted
        # Deterministic ordering: same offset yields the same rows.
        again = search._merge_results(rows, ["wan"], None, 5, had_distillations=True, offset=5)
        self.assertEqual([r["item_id"] for r in again["results"]], [r["item_id"] for r in page2["results"]])

    def test_merge_offset_beyond_pool_is_empty(self):
        rows = [("message_feed", {"message_id": i, "content": "wan"}) for i in range(3)]
        merged = search._merge_results(rows, ["wan"], None, 5, had_distillations=True, offset=10)
        self.assertEqual(merged["count"], 0)
        self.assertFalse(merged["has_more"])
        self.assertEqual(merged["total"], 3)

    def test_annotate_paging_adds_page_numbers(self):
        rows = [("message_feed", {"message_id": i, "content": "wan"}) for i in range(114)]
        merged = search._merge_results(rows, ["wan"], None, 20, had_distillations=True, offset=0)
        search._annotate_paging(merged, 20, 0)
        self.assertEqual(merged["page"], 1)
        self.assertEqual(merged["pages"], 6)  # ceil(114/20)
        self.assertEqual(merged["next_offset"], 20)

    def test_annotate_paging_next_offset_none_at_end(self):
        rows = [("message_feed", {"message_id": i, "content": "wan"}) for i in range(114)]
        merged = search._merge_results(rows, ["wan"], None, 20, had_distillations=True, offset=100)
        search._annotate_paging(merged, 20, 100)
        self.assertEqual(merged["next_offset"], None)
        self.assertFalse(merged["has_more"])

    def test_annotate_paging_mid_page(self):
        rows = [("message_feed", {"message_id": i, "content": "wan"}) for i in range(114)]
        merged = search._merge_results(rows, ["wan"], None, 20, had_distillations=True, offset=40)
        search._annotate_paging(merged, 20, 40)
        self.assertEqual(merged["page"], 3)
        self.assertEqual(merged["pages"], 6)

    def test_annotate_paging_stderr_summary(self):
        rows = [("message_feed", {"message_id": i, "content": "wan"}) for i in range(25)]
        merged = search._merge_results(rows, ["wan"], None, 10, had_distillations=True, offset=10)
        with unittest.mock.patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            search._annotate_paging(merged, 10, 10)
        text = mock_err.getvalue()
        self.assertIn("Showing 11-20 of 25", text)
        self.assertIn("page 2", text)
        self.assertIn("--offset 20", text)

    def test_annotate_paging_stderr_end(self):
        rows = [("message_feed", {"message_id": i, "content": "wan"}) for i in range(25)]
        merged = search._merge_results(rows, ["wan"], None, 10, had_distillations=True, offset=20)
        with unittest.mock.patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            search._annotate_paging(merged, 10, 20)
        self.assertIn("end of results", mock_err.getvalue())

    def test_annotate_paging_empty(self):
        merged = search._merge_results([], ["wan"], None, 10, had_distillations=True, offset=0)
        with unittest.mock.patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            search._annotate_paging(merged, 10, 0)
        self.assertIn("No results found", mock_err.getvalue())
        self.assertEqual(merged["page"], 0)
        self.assertEqual(merged["pages"], 0)

    def test_merge_drops_rows_without_natural_id(self):
        rows = [("message_feed", {"content": "no id"})]
        merged = search._merge_results(rows, ["wan"], None, 10, had_distillations=False)
        self.assertEqual(merged["count"], 0)

    def test_shape_message(self):
        row = {"message_id": "12345", "content": "hello wan", "author_name": "kijai",
               "channel_name": "wan_chatter", "guild_id": 1, "channel_id": 2,
               "created_at": "2026-01-01T00:00:00Z"}
        hit = search._shape_hit(row, "message_feed")
        self.assertEqual(hit["kind"], "message")
        self.assertEqual(hit["item_id"], "12345")
        self.assertEqual(hit["body"], "hello wan")
        self.assertEqual(hit["author"], "kijai")
        self.assertEqual(hit["context"], "wan_chatter")
        self.assertIn("https://discord.com/channels/1/2/12345", hit["url"])

    def test_shape_resource_never_payload(self):
        row = {"id": 9, "kind": "workflow", "title": "Wan", "body": "x", "author": "a",
               "url": "u", "source": "vibecomfy-external", "metadata": {"tags": ["wan"]},
               "created_at": "2026-01-01T00:00:00Z"}
        hit = search._shape_hit(row, "external_resources")
        self.assertEqual(hit["kind"], "workflow")
        self.assertEqual(hit["item_id"], "9")
        self.assertEqual(hit["metadata"], {"tags": ["wan"]})
        self.assertNotIn("payload", hit)

    def test_shape_distillation(self):
        row = {"id": 3, "question": "Q?", "conditions": "c", "answer": "A",
               "confidence": "high", "status": "approved", "created_at": "2026-01-01T00:00:00Z"}
        hit = search._shape_hit(row, "distillations")
        self.assertEqual(hit["kind"], "distillation")
        self.assertEqual(hit["title"], "Q?")
        self.assertEqual(hit["body"], "A")
        self.assertEqual(hit["metadata"], {"status": "approved", "confidence": "high"})

    def test_body_clipped_to_limit(self):
        row = {"message_id": "1", "content": "x" * 2000}
        hit = search._shape_hit(row, "message_feed")
        self.assertLessEqual(len(hit["body"]), search._BODY_LIMIT)
        self.assertTrue(hit["truncated"])

    def test_snowflake_id_stays_string(self):
        big = "1493649006067187752"
        row = {"message_id": int(big), "content": "wan"}
        hit = search._shape_hit(row, "message_feed")
        self.assertEqual(hit["item_id"], big)


# ---------------------------------------------------------------------------
# Hard-rule guard tests (the briefing's acceptance greps)
# ---------------------------------------------------------------------------


class HardRuleTests(unittest.TestCase):
    """The transport must never touch unified_feed or payload for search."""

    def _code_only(self, source: str) -> str:
        """Source with comments and string literals (docstrings) stripped.

        The module docstring explains WHY unified_feed is banned; the code
        itself must never reference it.
        """
        import tokenize as _tokenize

        out: list[str] = []
        for tok in _tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type in (_tokenize.COMMENT, _tokenize.STRING):
                continue
            out.append(tok.string)
        return " ".join(out)

    def test_search_code_never_mentions_unified_feed(self):
        source = Path(_REPO / "executors" / "search" / "run.py").read_text(encoding="utf-8")
        code = self._code_only(source)
        self.assertNotIn("unified_feed", code.casefold())

    def test_no_request_ever_targets_unified_feed(self):
        # End-to-end: no postgrest_get call in a full search names the view.
        with unittest.mock.patch.dict(
            os.environ,
            {"HIVEMIND_API_URL": "http://fake.example.com/rest/v1", "HIVEMIND_ANON_KEY": "k"},
            clear=True,
        ):
            paths: list[str] = []

            def mock_get(path, params=None, **kwargs):
                paths.append(path)
                return []

            with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
                    search.main(["--query", "wan animate workflow"])
            self.assertTrue(paths)
            self.assertTrue(all("unified_feed" not in p for p in paths))
            self.assertEqual(set(paths), {"message_feed", "external_resources", "distillations"})

    def test_search_code_never_projects_payload(self):
        source = Path(_REPO / "executors" / "search" / "run.py").read_text(encoding="utf-8")
        code = self._code_only(source)
        # No select=* and no payload projection in executable code.
        self.assertNotIn('select": "*"', code)
        self.assertNotIn("payload", code)
        for column in (search._MESSAGE_COLUMNS, search._RESOURCE_COLUMNS, search._DISTILLATION_COLUMNS):
            self.assertNotIn("payload", column)
        for table in ("message_feed", "external_resources", "distillations"):
            tokens = search._distinctive_tokens("wan", search._SQL_TOKEN_CAP)
            params = search._scope_params(table, tokens, sources=None, since=None, limit=20)
            self.assertNotEqual(params["select"], "*")


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

    def _mock_feed(self, message_rows=None, resource_rows=None, dist_rows=None):
        def mock_get(path, params=None, **kwargs):
            if path == "message_feed":
                return list(message_rows or [])
            if path == "external_resources":
                return list(resource_rows or [])
            return list(dist_rows or [])

        return mock_get

    def test_main_multiword_query_returns_rows(self):
        with self._patch_env():
            mock_get = self._mock_feed(
                message_rows=[{"message_id": 1, "content": "minimax distillation lora"}],
                resource_rows=[{"id": 2, "kind": "workflow", "title": "Minimax LoRA", "body": ""}],
                dist_rows=[{"id": 3, "question": "distillation?", "answer": "lora", "status": "approved"}],
            )
            with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = search.main(["--query", "Minimax distillation LoRA", "--limit", "10"])
            self.assertEqual(ret, 0)
            output = json.loads(mock_stdout.getvalue())
            self.assertGreaterEqual(output["count"], 1)
            self.assertIn("results", output)

    def test_main_returns_merged_real_results(self):
        with self._patch_env():
            mock_get = self._mock_feed(
                message_rows=[{"message_id": 1, "content": "wan animate message", "author_name": "a",
                               "channel_name": "c", "guild_id": 1, "channel_id": 2}],
                resource_rows=[{"id": 2, "kind": "workflow", "title": "Wan Animate Workflow", "body": "desc",
                                "source": "vibecomfy-external", "metadata": {}, "created_at": "2026-01-01T00:00:00Z"}],
                dist_rows=[],
            )
            with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = search.main(["--query", "wan animate workflow", "--limit", "10"])
            self.assertEqual(ret, 0)
            output = json.loads(mock_stdout.getvalue())
            self.assertGreaterEqual(output["count"], 2)
            kinds = {r["kind"] for r in output["results"]}
            self.assertIn("workflow", kinds)
            self.assertIn("message", kinds)
            self.assertIn("nudge", output)  # no distillations matched

    def test_main_nudge_absent_with_distillations(self):
        with self._patch_env():
            mock_get = self._mock_feed(
                message_rows=[],
                resource_rows=[],
                dist_rows=[{"id": 3, "question": "Q", "answer": "A", "status": "approved"}],
            )
            with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = search.main(["--query", "wan"])
            self.assertEqual(ret, 0)
            output = json.loads(mock_stdout.getvalue())
            self.assertNotIn("nudge", output)

    def test_main_writes_to_file(self):
        with self._patch_env():
            mock_get = self._mock_feed()
            with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
                with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
                    tmp_path = tmp.name
                try:
                    ret = search.main(["--query", "test", "--out", tmp_path])
                    self.assertEqual(ret, 0)
                    with open(tmp_path, "r", encoding="utf-8") as fh:
                        output = json.load(fh)
                    self.assertIn("results", output)
                finally:
                    os.unlink(tmp_path)

    def test_main_channel_queries_messages_only(self):
        with self._patch_env():
            queried: list[str] = []

            def mock_get(path, params=None, **kwargs):
                queried.append(path)
                return []

            with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
                    search.main(["--query", "wan", "--channel", "wan_chatter"])
            self.assertEqual(queried, ["message_feed"])

    def test_main_thread_queries_message_filters(self):
        with self._patch_env():
            queried: list[dict] = []

            def mock_get(path, params=None, **kwargs):
                queried.append((path, dict(params or {})))
                return []

            with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
                    search.main(["--query", "wan", "--thread", "123456789"])
            self.assertEqual(len(queried), 1)
            self.assertEqual(queried[0][0], "message_filters")
            self.assertEqual(queried[0][1]["thread_id"], "eq.123456789")

    def test_main_channel_with_non_message_kinds_is_error(self):
        with self._patch_env():
            with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                ret = search.main(["--query", "wan", "--channel", "wan_chatter", "--kinds", "workflow"])
            self.assertEqual(ret, 2)
            output = json.loads(mock_stdout.getvalue())
            self.assertIn("--channel/--author", output["error"])

    def test_main_channel_plus_author_both_applied(self):
        with self._patch_env():
            seen: list[dict] = []

            def mock_get(path, params=None, **kwargs):
                seen.append(dict(params or {}))
                return []

            with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
                    search.main(["--query", "wan", "--channel", "wan_chatter", "--author", "Kijai"])
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0]["channel_name"], "eq.wan_chatter")
            self.assertEqual(seen[0]["author_name"], "eq.Kijai")

    def test_main_offset_pages_output(self):
        with self._patch_env():
            mock_get = self._mock_feed(
                message_rows=[
                    {"message_id": i, "content": "wan", "created_at": "2026-01-01T00:00:00Z"}
                    for i in range(20)
                ]
            )
            with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = search.main(["--query", "wan", "--limit", "5", "--offset", "10"])
            self.assertEqual(ret, 0)
            output = json.loads(mock_stdout.getvalue())
            self.assertEqual(output["count"], 5)
            self.assertEqual(output["total"], 20)
            self.assertTrue(output["has_more"])

    def test_main_stderr_summary_does_not_pollute_stdout(self):
        with self._patch_env():
            mock_get = self._mock_feed(
                message_rows=[
                    {"message_id": i, "content": "wan", "created_at": "2026-01-01T00:00:00Z"}
                    for i in range(30)
                ]
            )
            with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    with unittest.mock.patch("sys.stderr", new_callable=io.StringIO) as mock_err:
                        ret = search.main(["--query", "wan", "--limit", "10", "--offset", "10"])
            self.assertEqual(ret, 0)
            output = json.loads(mock_stdout.getvalue())  # stdout stays pure JSON
            self.assertEqual(output["count"], 10)
            self.assertEqual(output["page"], 2)
            self.assertEqual(output["pages"], 3)
            self.assertIn("Showing 11-20 of 30", mock_err.getvalue())

    def test_shape_thread_row(self):
        row = {"message_id": "99", "content": "thread msg", "guild_id": 1, "channel_id": 2,
               "thread_id": 55, "created_at": "2026-01-01T00:00:00Z"}
        hit = search._shape_hit(row, "message_filters")
        self.assertEqual(hit["kind"], "message")
        self.assertEqual(hit["item_id"], "99")
        self.assertEqual(hit["metadata"], {"thread_id": 55})
        self.assertEqual(hit["author"], None)

    def test_main_kinds_message_only_queries_message_feed(self):
        with self._patch_env():
            queried: list[str] = []

            def mock_get(path, params=None, **kwargs):
                queried.append(path)
                return []

            with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
                    search.main(["--query", "wan", "--kinds", "message"])
            self.assertEqual(queried, ["message_feed"])

    def test_main_kinds_workflow_filters_kind(self):
        with self._patch_env():
            seen_params: list[dict] = []

            def mock_get(path, params=None, **kwargs):
                if path == "external_resources":
                    seen_params.append(dict(params or {}))
                return []

            with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
                    search.main(["--query", "lora", "--kinds", "workflow"])
            self.assertTrue(seen_params, "external_resources was never queried")
            self.assertEqual(seen_params[0]["kind"], "in.(workflow)")

    def test_main_sources_flag_applies(self):
        with self._patch_env():
            seen: list[dict] = []

            def mock_get(path, params=None, **kwargs):
                seen.append(dict(params or {}))
                return []

            with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
                    search.main(["--query", "wan", "--sources", "banodoco-discord"])
            # message scope kept; distillations skipped (hivemind not in sources);
            # resources filtered by source.
            tables = [s for s in seen if s.get("select", "").startswith("message")]
            self.assertTrue(any("content.ilike" in p.get("or", "") for p in seen))
            self.assertTrue(any(p.get("source") == "in.(banodoco-discord)" for p in seen))
            self.assertTrue(all("status" not in p for p in seen))  # distillations skipped

    def test_main_all_scopes_fail_returns_error(self):
        with self._patch_env():
            def mock_get(path, params=None, **kwargs):
                raise urllib.error.HTTPError("http://fake/", 500, "Internal Server Error", {}, None)

            with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = search.main(["--query", "wan"])
            self.assertEqual(ret, 2)
            output = json.loads(mock_stdout.getvalue())
            self.assertIn("error", output)

    def test_main_all_scopes_network_failure_exits_2(self):
        with self._patch_env():
            def mock_get(path, params=None, **kwargs):
                raise urllib.error.URLError(OSError("no route to host"))

            with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = search.main(["--query", "wan"])
            self.assertEqual(ret, 2)
            output = json.loads(mock_stdout.getvalue())
            self.assertIn("error", output)
            self.assertIn("URLError", output["error"])

    def test_main_tokenless_query_is_an_error(self):
        for bad in ("", "!!!", "12345", "   "):
            with self._patch_env():
                with unittest.mock.patch("executors.search.run.postgrest_get") as mock_get:
                    with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                        ret = search.main(["--query", bad])
                self.assertEqual(ret, 2, f"query {bad!r} should fail validation")
                output = json.loads(mock_stdout.getvalue())
                self.assertIn("no searchable tokens", output["error"])
                mock_get.assert_not_called()

    def test_main_partial_failure_warns_but_returns_results(self):
        with self._patch_env():
            def mock_get(path, params=None, **kwargs):
                if path == "message_feed":
                    raise urllib.error.HTTPError("http://fake/", 500, "boom", {}, None)
                return [{"id": 1, "kind": "workflow", "title": "wan", "body": ""}]

            with unittest.mock.patch("executors.search.run.postgrest_get", side_effect=mock_get):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = search.main(["--query", "wan"])
            self.assertEqual(ret, 0)
            output = json.loads(mock_stdout.getvalue())
            self.assertGreaterEqual(output["count"], 1)
            self.assertIn("warnings", output)


# ===========================================================================
# Discovery
# ===========================================================================

if __name__ == "__main__":
    unittest.main()
