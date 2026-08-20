"""Tests for ``scripts/baseline_search.py`` — the Hivemind legacy-ILIKE baseline.

Pure and offline: no network, no secrets printed. These tests pin (a) that the
probe reproduces the deployed executor's request exactly, (b) the failure /
timeout classifier, (c) the row/metric parsing, and (d) determinism of the
pure aggregation helpers. All HTTP is mocked.
"""

from __future__ import annotations

import io
import json
import socket
import sys
import unittest
import unittest.mock
import urllib.error
import urllib.parse
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

import baseline_search as bs  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes for urlopen — every test mocks the network, never hits it.
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status: int, payload):
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body


def _fake_urlopen_ok(payload_by_kind):
    """Return a urlopen side-effect that serves rows based on the kind= param."""
    def _side(req, timeout=None):  # noqa: ARG001
        qs = urllib.parse.urlparse(req.full_url).query
        params = dict(urllib.parse.parse_qsl(qs))
        kind = params.get("kind", "")
        payload = payload_by_kind.get(kind, [])
        return _FakeResp(200, payload)

    return _side


# ---------------------------------------------------------------------------
# Parity with the deployed legacy executor (frozen snapshot)
# ---------------------------------------------------------------------------

# The search executor was rewritten on 2026-08-19 to query the raw tables
# (message_feed / external_resources / distillations) with per-token OR
# predicates — the unified_feed ILIKE-phrase path it previously used blows the
# anon role's statement budget (HTTP 500 / SQLSTATE 57014) on multi-word
# queries.  This baseline is a HISTORICAL SNAPSHOT of the legacy request
# shape; these tests pin that the snapshot is faithful to what the executor
# actually sent, so the hybrid-search project's baseline measurements stay
# reproducible.  They deliberately do NOT track the new executor.


class ExecutorParityTests(unittest.TestCase):
    """The frozen legacy request shape must stay byte-identical to what the
    old executor sent."""

    def test_ilike_clause_frozen_legacy_phrase(self):
        # The old executor matched the WHOLE query as one contiguous ILIKE
        # substring — the shape that returns zero rows for multi-word
        # queries.  Frozen as-is for baseline reproducibility.
        self.assertEqual(
            bs.legacy_ilike_clause("best upscale model"),
            "(title.ilike.*best upscale model*,body.ilike.*best upscale model*)",
        )
        self.assertEqual(
            bs.legacy_ilike_clause("foo*bar"),
            "(title.ilike.*foo\\*bar*,body.ilike.*foo\\*bar*)",
        )

    def test_build_params_frozen_legacy_shape(self):
        # select=* + the single phrase OR on unified_feed: the legacy shape.
        params = bs.legacy_build_params("test", None, None, None, 20)
        self.assertEqual(
            params,
            {
                "select": "*",
                "limit": "20",
                "or": "(title.ilike.*test*,body.ilike.*test*)",
            },
        )
        params_kinds = bs.legacy_build_params("test", "message,resource", None, None, 5)
        self.assertEqual(params_kinds["kind"], "in.(message,resource)")
        params_sources = bs.legacy_build_params("test", None, "banodoco-discord", None, 20)
        self.assertEqual(params_sources["source"], "in.(banodoco-discord)")
        params_since = bs.legacy_build_params("test", None, None, "2024-01-01T00:00:00Z", 20)
        self.assertEqual(params_since["created_at"], "gte.2024-01-01T00:00:00Z")

    def test_passes_frozen_legacy_selection(self):
        # No kinds -> distillation (eq) + others (neq).
        self.assertEqual(
            bs.legacy_passes(None),
            [("distillation", "eq.distillation"), ("others", "neq.distillation")],
        )
        # kinds=workflow -> distillation excluded, only others=in.(workflow).
        self.assertEqual(bs.legacy_passes("workflow"), [("others", "in.(workflow)")])
        # kinds=distillation only -> only distillation pass.
        self.assertEqual(bs.legacy_passes("distillation"), [("distillation", "eq.distillation")])
        # kinds=message,resource -> only others=in.(message,resource).
        self.assertEqual(
            bs.legacy_passes("message,resource"),
            [("others", "in.(message,resource)")],
        )

    def test_legacy_url_targets_unified_feed(self):
        url = bs.build_url("https://fake.example.com/rest/v1", {"select": "*", "limit": "1"})
        self.assertIn("/unified_feed?", url)

    def test_final_url_and_headers_match_postgrest_get(self):
        """The URL, headers, and method a pass emits must match executors._common.postgrest_get."""
        import executors._common as common

        captured: dict[str, object] = {}

        def capture(req, timeout=None):  # noqa: ARG001
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["headers"] = dict(req.header_items())
            return _FakeResp(200, [])

        endpoint = "http://fake.example.com/rest/v1"
        anon_key = "sb_publishable_XXXXXXXXXXXX"
        entry = {"query": "upscale", "limit": 20}
        params = bs.build_pass_params(entry, "distillation", "eq.distillation")

        with unittest.mock.patch("urllib.request.urlopen", side_effect=capture):
            bs.do_get(bs.build_url(endpoint, params), anon_key, 30.0)
        baseline_view = dict(captured)

        with unittest.mock.patch("urllib.request.urlopen", side_effect=capture):
            common.postgrest_get("unified_feed", params=params, endpoint=endpoint, anon_key=anon_key)
        postgrest_view = dict(captured)

        self.assertEqual(baseline_view["url"], postgrest_view["url"])
        self.assertEqual(baseline_view["method"], postgrest_view["method"])
        # Header keys match (values: apikey == anon_key, Accept == application/json).
        self.assertEqual(
            {k.lower(): v for k, v in baseline_view["headers"].items()},
            {k.lower(): v for k, v in postgrest_view["headers"].items()},
        )


# ---------------------------------------------------------------------------
# Failure / timeout classification
# ---------------------------------------------------------------------------


class ClassifyExceptionTests(unittest.TestCase):
    def test_http_error(self):
        cat, detail = bs.classify_exception(
            urllib.error.HTTPError("http://x/", 500, "Internal", {}, None)
        )
        self.assertEqual(cat, "http_error")
        self.assertIn("500", detail)

    def test_timeout_via_urlerror(self):
        err = urllib.error.URLError(socket.timeout("timed out"))
        cat, _ = bs.classify_exception(err)
        self.assertEqual(cat, "timeout")

    def test_bare_socket_timeout(self):
        cat, _ = bs.classify_exception(socket.timeout("timed out"))
        self.assertEqual(cat, "timeout")

    def test_timeouterror_alias(self):
        cat, _ = bs.classify_exception(TimeoutError("timed out"))
        self.assertEqual(cat, "timeout")

    def test_url_error(self):
        err = urllib.error.URLError(OSError("name resolution failed"))
        cat, detail = bs.classify_exception(err)
        self.assertEqual(cat, "url_error")
        self.assertIn("url error", detail)

    def test_parse_error(self):
        cat, detail = bs.classify_exception(json.JSONDecodeError("msg", "doc", 0))
        self.assertEqual(cat, "parse_error")
        self.assertIn("non-JSON", detail)

    def test_other_error(self):
        cat, detail = bs.classify_exception(RuntimeError("boom"))
        self.assertEqual(cat, "other_error")
        self.assertIn("RuntimeError", detail)

    def test_no_secret_leaks_through_detail(self):
        secret = "sb_secret_" + "a" * 32
        err = urllib.error.HTTPError("http://x/", 500, secret, {}, None)
        _, detail = bs.classify_exception(err)
        self.assertNotIn(secret, detail)


# ---------------------------------------------------------------------------
# Row parsing + pure metric helpers (determinism)
# ---------------------------------------------------------------------------


class ParsingTests(unittest.TestCase):
    def test_normalize_rows_list(self):
        rows = bs.normalize_rows([{"kind": "message"}, {"kind": "resource"}])
        self.assertEqual(len(rows), 2)

    def test_normalize_rows_single_object(self):
        # PostgREST returns a single object for limit=1.
        rows = bs.normalize_rows({"kind": "distillation"})
        self.assertEqual(len(rows), 1)

    def test_normalize_rows_other(self):
        self.assertEqual(bs.normalize_rows("not json"), [])
        self.assertEqual(bs.normalize_rows(None), [])

    def test_detect_duplicates_none(self):
        rows = [{"kind": "message", "item_id": "1"}, {"kind": "message", "item_id": "2"}]
        self.assertEqual(bs.detect_duplicates(rows)["duplicate_count"], 0)

    def test_detect_duplicates_found(self):
        rows = [
            {"kind": "message", "item_id": "1"},
            {"kind": "message", "item_id": "1"},
            {"kind": "message", "item_id": "1"},
        ]
        out = bs.detect_duplicates(rows)
        self.assertEqual(out["duplicate_count"], 2)
        self.assertEqual(out["duplicate_keys"], [["message", "1"]])

    def test_summarize_row_short_body(self):
        row = {"kind": "message", "source": "s", "item_id": "123", "title": "T", "body": "hi"}
        s = bs.summarize_row(row)
        self.assertTrue(s["item_id_is_string"])
        self.assertFalse(s["body_would_truncate"])
        self.assertEqual(s["body_length"], 2)
        self.assertEqual(s["title"], "T")

    def test_summarize_row_long_body_truncates_flag(self):
        row = {"kind": "resource", "item_id": "9", "body": "x" * 1000}
        s = bs.summarize_row(row)
        self.assertTrue(s["body_would_truncate"])
        self.assertEqual(s["body_length"], 1000)

    def test_summarize_row_title_snippet(self):
        # Space-separated words survive the redactor; a long title is clipped.
        row = {"kind": "resource", "item_id": "1", "title": "the quick brown fox " * 30, "body": ""}
        self.assertEqual(len(bs.summarize_row(row)["title"]), bs.TITLE_SNIPPET_LEN)

    def test_kind_distribution(self):
        rows = [
            {"kind": "message"}, {"kind": "message"}, {"kind": "resource"},
        ]
        self.assertEqual(bs.kind_distribution(rows), {"message": 2, "resource": 1})

    def test_snowflake_check_all_strings(self):
        rows = [{"item_id": "1"}, {"item_id": "2"}]
        out = bs.snowflake_check(rows)
        self.assertTrue(out["all_item_ids_strings"])
        self.assertEqual(out["non_string_item_ids"], [])

    def test_snowflake_check_catches_number(self):
        rows = [{"kind": "message", "item_id": 1493649006067187752}]
        out = bs.snowflake_check(rows)
        self.assertFalse(out["all_item_ids_strings"])
        self.assertEqual(len(out["non_string_item_ids"]), 1)


class DeterminismTests(unittest.TestCase):
    """Pure helpers return identical output for identical input."""

    def setUp(self):
        self.rows = [
            {"kind": "message", "item_id": "1", "title": "a", "body": "b"},
            {"kind": "message", "item_id": "1", "title": "a", "body": "b"},
            {"kind": "resource", "item_id": "2", "title": "c", "body": "d"},
        ]

    def test_summarize_stable(self):
        a = [bs.summarize_row(r) for r in self.rows]
        b = [bs.summarize_row(r) for r in self.rows]
        self.assertEqual(a, b)

    def test_duplicates_stable(self):
        self.assertEqual(bs.detect_duplicates(self.rows), bs.detect_duplicates(self.rows))


# ---------------------------------------------------------------------------
# measure_query — end-to-end with mocked urlopen
# ---------------------------------------------------------------------------


class MeasureQueryTests(unittest.TestCase):
    def setUp(self):
        self.endpoint = "http://fake.example.com/rest/v1"
        self.anon_key = "sb_publishable_XXXXXXXXXXXX"

    def _entry(self, **over):
        e = {"id": "q", "category": "hit", "query": "upscale", "limit": 20}
        e.update(over)
        return e

    def test_success_both_passes_counts_and_order(self):
        entry = self._entry()
        payload = {
            "eq.distillation": [{"kind": "distillation", "item_id": "d1", "title": "Q", "body": "a"}],
            "neq.distillation": [
                {"kind": "message", "item_id": "1", "title": None, "body": "x"},
                {"kind": "resource", "item_id": "2", "title": "R", "body": "y"},
            ],
        }
        with unittest.mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen_ok(payload)):
            rec = bs.measure_query(entry, endpoint=self.endpoint, anon_key=self.anon_key)
        self.assertEqual(rec["outcome"], "success")
        self.assertEqual([p["pass"] for p in rec["passes"]], ["distillation", "others"])
        self.assertEqual(rec["total_fetched"], 3)
        # Distillations-first order preserved.
        self.assertEqual(rec["kind_distribution"], {"distillation": 1, "message": 1, "resource": 1})
        self.assertEqual(rec["created_at_order"][0:0], [])  # no created_at in fixtures -> Nones
        # Nudge absent because distillation pass returned >=1 row.
        self.assertFalse(rec["nudge_present"])
        # Latencies recorded for every pass and e2e.
        for p in rec["passes"]:
            self.assertIsInstance(p["latency_ms"], float)
        self.assertIsInstance(rec["e2e_latency_ms"], float)
        # Reproducibility: params stored, no anon key.
        for p in rec["passes"]:
            self.assertNotIn(self.anon_key, json.dumps(p["params"]))

    def test_nudge_present_when_no_distillations(self):
        entry = self._entry()
        payload = {"eq.distillation": [], "neq.distillation": [{"kind": "message", "item_id": "1", "body": "x"}]}
        with unittest.mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen_ok(payload)):
            rec = bs.measure_query(entry, endpoint=self.endpoint, anon_key=self.anon_key)
        self.assertTrue(rec["nudge_present"])
        self.assertEqual(rec["total_fetched"], 1)

    def test_kinds_workflow_makes_single_pass(self):
        entry = self._entry(category="workflow", query="lora", kinds="workflow")
        payload = {"in.(workflow)": [{"kind": "resource", "item_id": "9", "body": "wf"}]}
        with unittest.mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen_ok(payload)):
            rec = bs.measure_query(entry, endpoint=self.endpoint, anon_key=self.anon_key)
        self.assertEqual([p["pass"] for p in rec["passes"]], ["others"])
        # Nudge mirrors executor: no distillation pass -> distillation_rows=[] -> nudge present.
        self.assertTrue(rec["nudge_present"])

    def test_doubled_limit_exceeds_requested(self):
        # limit=2 but both passes return 2 each -> 4 > 2.
        entry = self._entry(category="doubled_limit", query="model", limit=2)
        payload = {
            "eq.distillation": [
                {"kind": "distillation", "item_id": "d1", "body": "a"},
                {"kind": "distillation", "item_id": "d2", "body": "b"},
            ],
            "neq.distillation": [
                {"kind": "message", "item_id": "1", "body": "x"},
                {"kind": "message", "item_id": "2", "body": "y"},
            ],
        }
        with unittest.mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen_ok(payload)):
            rec = bs.measure_query(entry, endpoint=self.endpoint, anon_key=self.anon_key)
        self.assertEqual(rec["total_fetched"], 4)
        self.assertTrue(rec["exceeds_requested_limit"])
        self.assertEqual(rec["requested_limit"], 2)

    def test_single_object_response_handled(self):
        entry = self._entry(limit=1)

        def side(req, timeout=None):  # noqa: ARG001
            qs = urllib.parse.urlparse(req.full_url).query
            kind = dict(urllib.parse.parse_qsl(qs)).get("kind")
            payload = {"kind": "distillation", "item_id": "d1", "body": "a"} if kind == "eq.distillation" else []
            return _FakeResp(200, payload)

        with unittest.mock.patch("urllib.request.urlopen", side_effect=side):
            rec = bs.measure_query(entry, endpoint=self.endpoint, anon_key=self.anon_key)
        self.assertEqual(rec["total_fetched"], 1)

    def test_timeout_classified_not_crash(self):
        entry = self._entry(category="timeout_prone", client_timeout=0.001)

        def side(req, timeout=None):  # noqa: ARG001
            raise urllib.error.URLError(socket.timeout("timed out"))

        with unittest.mock.patch("urllib.request.urlopen", side_effect=side):
            rec = bs.measure_query(entry, endpoint=self.endpoint, anon_key=self.anon_key)
        self.assertEqual(rec["outcome"], "timeout")
        self.assertEqual(rec["passes"][0]["outcome"], "timeout")
        self.assertEqual(rec["passes"][0]["fetched"], 0)
        self.assertEqual(rec["total_fetched"], 0)

    def test_http_error_classified(self):
        entry = self._entry()

        def side(req, timeout=None):  # noqa: ARG001
            raise urllib.error.HTTPError("http://x/", 500, "Internal", {}, None)

        with unittest.mock.patch("urllib.request.urlopen", side_effect=side):
            rec = bs.measure_query(entry, endpoint=self.endpoint, anon_key=self.anon_key)
        self.assertEqual(rec["outcome"], "http_error")
        self.assertIn("500", rec["passes"][0]["detail"])

    def test_zero_result_query(self):
        entry = self._entry(category="rare_no_hit", query="zzqxnotarealterm-99999")
        payload = {"eq.distillation": [], "neq.distillation": []}
        with unittest.mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen_ok(payload)):
            rec = bs.measure_query(entry, endpoint=self.endpoint, anon_key=self.anon_key)
        self.assertEqual(rec["total_fetched"], 0)
        self.assertTrue(rec["nudge_present"])

    def test_snowflake_string_preserved_through_search(self):
        big = "1493649006067187752"  # > 2^53, must stay a string
        entry = self._entry()
        payload = {"eq.distillation": [], "neq.distillation": [{"kind": "message", "item_id": big, "body": "x"}]}
        with unittest.mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen_ok(payload)):
            rec = bs.measure_query(entry, endpoint=self.endpoint, anon_key=self.anon_key)
        self.assertTrue(rec["snowflake"]["all_item_ids_strings"])
        self.assertEqual(rec["passes"][1]["rows"][0]["item_id"], big)

    def test_secret_in_title_redacted_in_results(self):
        secret = "sb_secret_" + "a" * 32
        entry = self._entry()
        payload = {"eq.distillation": [], "neq.distillation": [{"kind": "message", "item_id": "1", "title": secret, "body": "x"}]}
        with unittest.mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen_ok(payload)):
            rec = bs.measure_query(entry, endpoint=self.endpoint, anon_key=self.anon_key)
        blob = json.dumps(rec)
        self.assertNotIn(secret, blob)
        # Title is stored verbatim (not pre-redacted) but the report renderer redacts;
        # ensure the report path masks it too.
        report = bs.render_report(
            bs.build_meta(self.endpoint, self.anon_key, sleep=0, default_timeout=30.0),
            [rec],
            bs.build_summary([rec]),
        )
        self.assertNotIn(secret, report)


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


class AggregationTests(unittest.TestCase):
    def test_percentile_known(self):
        self.assertEqual(bs.percentile([1.0, 2.0, 3.0, 4.0], 50), 2.5)
        self.assertEqual(bs.percentile([10.0], 95), 10.0)
        self.assertIsNone(bs.percentile([], 50))

    def test_build_summary_counts(self):
        records = [
            {"outcome": "success", "total_fetched": 5, "exceeds_requested_limit": True,
             "duplicate": {"duplicate_count": 0}, "category": "hit",
             "snowflake": {"all_item_ids_strings": True}, "e2e_latency_ms": 100.0,
             "passes": [{"latency_ms": 50.0}, {"latency_ms": 60.0}]},
            {"outcome": "timeout", "total_fetched": 0, "exceeds_requested_limit": False,
             "duplicate": {"duplicate_count": 1}, "category": "timeout_prone",
             "snowflake": {"all_item_ids_strings": True}, "e2e_latency_ms": 10.0,
             "passes": [{"latency_ms": 10.0}]},
        ]
        s = bs.build_summary(records)
        self.assertEqual(s["queries"], 2)
        self.assertEqual(s["outcomes"], {"success": 1, "timeout": 1})
        self.assertEqual(s["zero_result_queries"], 1)
        self.assertEqual(s["queries_exceeding_requested_limit"], 1)
        self.assertEqual(s["total_duplicate_items"], 1)
        self.assertTrue(s["all_snowflakes_strings"])
        self.assertEqual(s["end_to_end_latency"]["n"], 2)
        self.assertEqual(s["per_pass_latency"]["n"], 3)


# ---------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------


class ManifestTests(unittest.TestCase):
    def test_default_manifest_valid_and_covers_categories(self):
        problems = bs.validate_manifest(bs.DEFAULT_MANIFEST)
        self.assertEqual(problems, [], f"default manifest invalid: {problems}")
        cats = {e["category"] for e in bs.DEFAULT_MANIFEST}
        self.assertTrue(bs.REQUIRED_CATEGORIES <= cats)

    def test_detects_missing_category(self):
        manifest = [{"id": "x", "category": "hit", "query": "a", "limit": 5}]
        problems = bs.validate_manifest(manifest)
        self.assertTrue(any("missing required categories" in p for p in problems))

    def test_detects_bad_entry(self):
        manifest = [{"id": "x", "category": "hit", "query": "", "limit": 0}]
        manifest += [{"id": c, "category": c, "query": "q", "limit": 5} for c in bs.REQUIRED_CATEGORIES]
        problems = bs.validate_manifest(manifest)
        self.assertTrue(any("empty query" in p for p in problems))
        self.assertTrue(any("limit must be a positive int" in p for p in problems))

    def test_detects_duplicate_id(self):
        manifest = [
            {"id": "dup", "category": "hit", "query": "a", "limit": 5},
            {"id": "dup", "category": "workflow", "query": "b", "limit": 5},
        ]
        manifest += [{"id": c, "category": c, "query": "q", "limit": 5} for c in bs.REQUIRED_CATEGORIES]
        problems = bs.validate_manifest(manifest)
        self.assertTrue(any("duplicate id" in p for p in problems))


# ---------------------------------------------------------------------------
# Dry-run plan + report rendering (no network)
# ---------------------------------------------------------------------------


class OutputTests(unittest.TestCase):
    def test_dry_run_plan_omits_anon_key_and_counts_gets(self):
        plan = bs.render_dry_run(
            bs.DEFAULT_MANIFEST,
            endpoint="https://ujlwuvkrxlvoswwkerdf.supabase.co/rest/v1",
            anon_key="sb_publishable_SUPERSECRETV",
        )
        self.assertNotIn("SUPERSECRETV", plan)
        self.assertIn("total GETs", plan)
        # No kinds excludes distillation -> workflow entry makes 1 pass.
        self.assertIn("others", plan)

    def test_mask_key_never_reveals_full_value(self):
        key = "sb_publishable_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        masked = bs.mask_key(key)
        self.assertNotIn(key, masked)
        self.assertNotIn("QRSTUVWXYZ0123456789", masked)

    def test_render_report_has_required_sections_and_no_judgments(self):
        records = [
            {"id": "r", "category": "hit", "query": "upscale", "outcome": "success",
             "requested_limit": 20, "passes": [
                 {"pass": "distillation", "latency_ms": 50.0, "fetched": 1},
                 {"pass": "others", "latency_ms": 60.0, "fetched": 2}],
             "total_fetched": 3, "exceeds_requested_limit": False, "e2e_latency_ms": 110.0,
             "duplicate": {"duplicate_count": 0}},
            {"id": "dl", "category": "doubled_limit", "query": "model", "outcome": "success",
             "requested_limit": 3, "passes": [
                 {"pass": "others", "latency_ms": 40.0, "fetched": 3}],
             "total_fetched": 3, "exceeds_requested_limit": False, "e2e_latency_ms": 40.0,
             "duplicate": {"duplicate_count": 0}},
            {"id": "tp", "category": "timeout_prone", "query": "upscale", "outcome": "timeout",
             "requested_limit": 20, "client_timeout_s": 0.01, "passes": [
                 {"pass": "distillation", "latency_ms": 10.0, "fetched": 0,
                  "outcome": "timeout", "detail": "timeout: URLError"}],
             "total_fetched": 0, "exceeds_requested_limit": False, "e2e_latency_ms": 10.0,
             "duplicate": {"duplicate_count": 0}},
        ]
        # Pad records so build_summary has the fields it reads.
        for r in records:
            r.setdefault("kind_distribution", {})
            r.setdefault("snowflake", {"all_item_ids_strings": True})
            r.setdefault("nudge_present", False)
            r.setdefault("created_at_order", [])
        meta = bs.build_meta("https://ujlwuvkrxlvoswwkerdf.supabase.co/rest/v1", "sb_publishable_K", sleep=0, default_timeout=30.0)
        report = bs.render_report(meta, records, bs.build_summary(records))
        self.assertIn("Headline findings", report)
        self.assertIn("Doubled-limit detail", report)
        self.assertIn("Timeout classification", report)
        self.assertIn("Relevance judgments", report)
        # No fabricated grades / no anon key.
        self.assertNotIn("recall", report.lower())
        self.assertNotIn("sb_publishable_K", report)


class EndToEndMainDryRunTests(unittest.TestCase):
    def test_main_dry_run_no_network_exit_zero(self):
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = bs.main(["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("DRY RUN", out.getvalue())

    def test_main_from_json_renders_report_without_network(self):
        # Build a tiny snapshot inline and regenerate the MD from it — no urlopen patch
        # needed because --from-json never touches the network.
        records = [
            {"id": "r", "category": "hit", "query": "upscale", "outcome": "success",
             "requested_limit": 20, "total_fetched": 2, "exceeds_requested_limit": False,
             "e2e_latency_ms": 100.0, "duplicate": {"duplicate_count": 0},
             "kind_distribution": {}, "snowflake": {"all_item_ids_strings": True},
             "created_at_order": [], "nudge_present": False,
             "passes": [{"pass": "distillation", "outcome": "success", "latency_ms": 40.0, "fetched": 1},
                        {"pass": "others", "outcome": "success", "latency_ms": 60.0, "fetched": 1}]},
        ]
        meta = bs.build_meta("https://ujlwuvkrxlvoswwkerdf.supabase.co/rest/v1", "sb_publishable_K", sleep=0, default_timeout=30.0)
        snapshot = {"meta": meta, "summary": bs.build_summary(records), "results": records}
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "in.json"
            dst = Path(d) / "out.md"
            src.write_text(json.dumps(snapshot), encoding="utf-8")
            with unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
                rc = bs.main(["--from-json", str(src), "--out-md", str(dst)])
            self.assertEqual(rc, 0)
            text = dst.read_text(encoding="utf-8")
            self.assertIn("Headline findings", text)
            self.assertIn("Failures on real queries", text)


class FailureHelpersTests(unittest.TestCase):
    def _rec(self, category, pass_outcomes):
        return {
            "id": category, "category": category, "query": "q", "outcome": "success" if all(o == "success" for o in pass_outcomes.values()) else "fail",
            "passes": [{"pass": k, "outcome": v, "latency_ms": 10.0} for k, v in pass_outcomes.items()],
        }

    def test_real_failures_excludes_timeout_prone_probe(self):
        records = [
            self._rec("hit", {"distillation": "success", "others": "timeout"}),       # real
            self._rec("timeout_prone", {"distillation": "timeout", "others": "timeout"}),  # artificial
        ]
        rf = bs._real_failures(records)
        self.assertEqual(len(rf), 1)
        self.assertEqual(rf[0][0]["category"], "hit")

    def test_healthy_latencies_filters_failures(self):
        records = [
            self._rec("hit", {"distillation": "success", "others": "timeout"}),
        ]
        self.assertEqual(bs._healthy_pass_latencies(records), [10.0])


# ===========================================================================
# Discovery
# ===========================================================================

if __name__ == "__main__":
    unittest.main()
