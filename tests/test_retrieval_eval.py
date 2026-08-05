"""Deterministic offline tests for the Hivemind retrieval evaluation harness.

Covers metric math, tie/order behaviour, schema validation (including malformed
inputs and unsafe IDs), Discord-snowflake string round-trips, adapter error and
timeout accounting, report stability, and the one-command CLI. No network:
remote-adapter tests are either construction-rejection checks or opt-in skips.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))

from eval.retrieval import schema as S  # noqa: E402
from eval.retrieval import metrics as M  # noqa: E402
from eval.retrieval.adapters import (  # noqa: E402
    ADAPTERS,
    ErrorAdapter,
    LegacyIlikeAdapter,
    OracleAdapter,
    RemoteSearchAdapter,
    RetrievalError,
    RetrievalTimeout,
    ReverseAdapter,
    StubAdapter,
    TimeoutAdapter,
    build_adapter,
    register_adapter,
)
from eval.retrieval.compare import compare_systems, render_markdown  # noqa: E402
from eval.retrieval.loader import load_corpus, load_golden_set  # noqa: E402
from eval.retrieval.runner import run_eval  # noqa: E402

FIXTURES = _REPO / "eval" / "retrieval" / "fixtures"


# ---------------------------------------------------------------------------
# Metric math
# ---------------------------------------------------------------------------


class MetricsTests(unittest.TestCase):
    def test_recall_at_k_fraction_and_empty(self):
        self.assertEqual(M.recall_at_k(["a", "b", "c"], ["b", "z"], 5), 0.5)
        self.assertEqual(M.recall_at_k(["a", "b", "c"], [], 5), 0.0)  # nothing to recall
        self.assertEqual(M.recall_at_k(["a", "b"], ["c"], 5), 0.0)  # none found
        self.assertEqual(M.recall_at_k(["a", "b", "c"], ["a", "b", "c"], 3), 1.0)

    def test_reciprocal_rank(self):
        self.assertEqual(M.reciprocal_rank(["a", "b", "c"], ["c"]), 1 / 3)
        self.assertEqual(M.reciprocal_rank(["a", "b", "c"], ["a"]), 1.0)
        self.assertEqual(M.reciprocal_rank(["a", "b", "c"], ["z"]), 0.0)
        self.assertEqual(M.reciprocal_rank(["a"], []), 0.0)

    def test_average_precision(self):
        # ranked [rel, non, rel] over 2 relevant: AP = (1/1 + 2/3) / 2
        ap = M.average_precision(["a", "x", "b"], {"a", "b"})
        self.assertAlmostEqual(ap, (1.0 + 2 / 3) / 2)

    def test_dcg_and_ndcg_graded(self):
        grades = {"x": 3, "z": 1}
        # ranked x(3), y(0), z(1) at k=3
        # DCG = 7/log2(2) + 0 + 1/log2(4) = 7 + 0.5 = 7.5
        # IDCG = 7/log2(2) + 1/log2(3) = 7 + 0.63093
        ndcg = M.ndcg_at_k(["x", "y", "z"], grades, 3)
        self.assertAlmostEqual(M.dcg_at_k([3, 0, 1]), 7.5)
        self.assertAlmostEqual(ndcg, 7.5 / (7 + 1 / 1.584962500721156), places=6)

    def test_ndcg_ideal_order_beats_random(self):
        grades = {"a": 3, "b": 2, "c": 1}
        ideal = M.ndcg_at_k(["a", "b", "c"], grades, 3)
        worst = M.ndcg_at_k(["c", "b", "a"], grades, 3)
        self.assertAlmostEqual(ideal, 1.0)
        self.assertLess(worst, ideal)
        self.assertGreater(worst, 0.0)

    def test_ndcg_no_positive_grades_is_zero(self):
        self.assertEqual(M.ndcg_at_k(["a"], {}, 3), 0.0)
        self.assertEqual(M.ndcg_at_k(["a"], {"a": 0}, 3), 0.0)
        self.assertEqual(M.ndcg_at_k(["a"], {"a": 3}, 0), 0.0)  # k <= 0

    def test_set_precision_recall(self):
        self.assertEqual(M.set_precision(["a", "b"], {"a"}), 0.5)
        self.assertEqual(M.set_precision([], {"a"}), 1.0)  # no claims
        self.assertEqual(M.set_recall(["a", "b"], {"a", "c"}), 0.5)
        self.assertEqual(M.set_recall(["a"], set()), 0.0)

    def test_percentile_exact_and_ordering(self):
        xs = [1, 2, 3, 4]
        self.assertEqual(M.percentile(xs, 50), 2.5)
        self.assertAlmostEqual(M.percentile(xs, 95), 3.85)
        self.assertAlmostEqual(M.percentile(xs, 99), 3.97)
        # monotonic for any sample
        ys = [3.0, 1.0, 4.0, 1.5, 9.0, 0.0]
        self.assertLessEqual(M.percentile(ys, 50), M.percentile(ys, 95))
        self.assertLessEqual(M.percentile(ys, 95), M.percentile(ys, 99))
        self.assertEqual(M.percentile([], 50), 0.0)

    def test_percentile_rejects_bad_q(self):
        with self.assertRaises(ValueError):
            M.percentile([1, 2], 150)

    def test_latency_stats_shape(self):
        stats = M.latency_stats([1.0, 2.0, 3.0, 4.0])
        self.assertEqual(stats["n"], 4)
        self.assertAlmostEqual(stats["mean_ms"], 2.5)
        self.assertLessEqual(stats["p50_ms"], stats["p95_ms"])
        self.assertLessEqual(stats["p95_ms"], stats["p99_ms"])
        empty = M.latency_stats([])
        self.assertEqual(empty["n"], 0)

    def test_aggregate_mean(self):
        rows = [
            {"recall_at_1": 1.0, "recall_at_5": 1.0, "recall_at_10": 1.0,
             "reciprocal_rank": 1.0, "ndcg_at_10": 1.0, "average_precision": 1.0},
            {"recall_at_1": 0.0, "recall_at_5": 0.5, "recall_at_10": 0.5,
             "reciprocal_rank": 0.5, "ndcg_at_10": 0.5, "average_precision": 0.5},
        ]
        agg = M.aggregate(rows)
        self.assertAlmostEqual(agg["recall@10"], 0.75)
        self.assertAlmostEqual(agg["mrr"], 0.75)
        self.assertEqual(agg["n"], 2)

    def test_aggregate_empty(self):
        agg = M.aggregate([])
        self.assertEqual(agg["n"], 0)
        self.assertEqual(agg["recall@10"], 0.0)

    def test_aggregate_by_group_and_category(self):
        rows = [
            {"recall_at_10": 1.0, "reciprocal_rank": 1.0, "ndcg_at_10": 1.0,
             "average_precision": 1.0, "g": "a", "categories": ["x"]},
            {"recall_at_10": 0.0, "reciprocal_rank": 0.0, "ndcg_at_10": 0.0,
             "average_precision": 0.0, "g": "a", "categories": ["x", "y"]},
        ]
        by_g = M.aggregate_by_group(rows, "g")
        self.assertAlmostEqual(by_g["a"]["recall@10"], 0.5)
        by_cat = M.aggregate_by_category(rows)
        # case 0 contributes to x; case 1 to x and y
        self.assertAlmostEqual(by_cat["x"]["recall@10"], 0.5)
        self.assertAlmostEqual(by_cat["y"]["recall@10"], 0.0)
        self.assertEqual(by_cat["y"]["n"], 1)

    def test_rate_safe(self):
        self.assertEqual(M.rate(3, 4), 0.75)
        self.assertEqual(M.rate(3, 0), 0.0)


# ---------------------------------------------------------------------------
# Schema + validation
# ---------------------------------------------------------------------------


class SchemaTests(unittest.TestCase):
    def test_item_id_workflow_resource_alias(self):
        a = S.ItemId(kind="workflow", item_id="2580")
        b = S.ItemId(kind="resource", item_id="2580")
        self.assertEqual(a.key(), b.key())  # workflow aliases resource
        self.assertEqual(a.entity_kind(), "resource")
        self.assertNotEqual(a.key(), ("resource", "2581"))

    def test_unknown_kind_rejected(self):
        with self.assertRaises(S.SchemaError):
            S.ItemId(kind="bogus", item_id="1")

    def test_validate_item_id_rejects_unsafe(self):
        bad = [None, 123, "", "   ", "has space", "a\tb", "a\nb", "x" * 200]
        for v in bad:
            with self.assertRaises(S.SchemaError):
                S.validate_item_id(v)

    def test_validate_item_id_accepts_snowflake(self):
        snowflake = "2987654321098765432"
        self.assertEqual(S.validate_item_id(snowflake), snowflake)

    def test_filters_ambiguous_item_ids_rejected(self):
        # item_ids without exactly one kind → ambiguous (AD-1).
        with self.assertRaises(S.SchemaError):
            S.validate_filters({"item_ids": ["1"]})
        with self.assertRaises(S.SchemaError):
            S.validate_filters({"kinds": ["message", "resource"], "item_ids": ["1"]})

    def test_filters_item_ids_need_one_kind(self):
        ok = S.validate_filters({"kinds": ["workflow"], "item_ids": ["2580"]})
        self.assertEqual(ok["item_ids"], ["2580"])
        self.assertEqual(ok["kinds"], ["workflow"])

    def test_filters_unknown_kind_rejected(self):
        with self.assertRaises(S.SchemaError):
            S.validate_filters({"kinds": ["nonsense"]})

    def test_filters_mode_and_since_validated(self):
        ok = S.validate_filters({"mode": "hybrid", "since": "2024-01-01"})
        self.assertEqual(ok["mode"], "hybrid")
        with self.assertRaises(S.SchemaError):
            S.validate_filters({"mode": "quantum"})
        with self.assertRaises(S.SchemaError):
            S.validate_filters({"since": "   "})

    def test_golden_case_empty_expected_without_no_hit_rejected(self):
        with self.assertRaises(S.SchemaError):
            S.GoldenCase(id="c1", query="q", expected=[])

    def test_golden_case_no_hit_accepts_empty_and_tags_category(self):
        case = S.GoldenCase(id="c1", query="q", expected=[], expect_no_hit=True)
        self.assertTrue(case.expect_no_hit)
        self.assertIn("no_hit", case.categories)
        self.assertFalse(case.is_judged)

    def test_golden_case_no_hit_with_expected_rejected(self):
        with self.assertRaises(S.SchemaError):
            S.GoldenCase(
                id="c1", query="q",
                expected=[S.JudgedItem(kind="message", item_id="1", grade=1)],
                expect_no_hit=True,
            )

    def test_golden_case_dedup_judgments_keeps_max_grade(self):
        case = S.GoldenCase(
            id="c1", query="q",
            expected=[
                S.JudgedItem(kind="workflow", item_id="1", grade=1),  # aliases resource:1
                S.JudgedItem(kind="resource", item_id="1", grade=3),  # same key, higher
            ],
        )
        self.assertEqual(len(case.expected), 1)
        self.assertEqual(case.expected[0].grade, 3)

    def test_golden_case_invalid_grade_rejected(self):
        with self.assertRaises(S.SchemaError):
            S.JudgedItem(kind="message", item_id="1", grade=-1)
        with self.assertRaises(S.SchemaError):
            S.JudgedItem(kind="message", item_id="1", grade=True)  # bool not int

    def test_golden_set_duplicate_id_rejected(self):
        with self.assertRaises(S.SchemaError):
            S.GoldenSet.from_records([
                {"id": "c1", "query": "q", "expected": [{"kind": "message", "item_id": "1"}]},
                {"id": "c1", "query": "q2", "expected": [{"kind": "message", "item_id": "2"}]},
            ])

    def test_corpus_item_numeric_id_coerced_to_string(self):
        item = S.CorpusItem.from_dict(
            {"kind": "message", "item_id": 2987654321098765432, "source": "s", "body": "b"}
        )
        self.assertEqual(item.item_id, "2987654321098765432")
        self.assertIsInstance(item.item_id, str)

    def test_corpus_item_long_body_matchable_anywhere(self):
        body = "x " * 50 + " needle " + " y" * 50
        item = S.CorpusItem(kind="article", item_id="9", source="s", body=body)
        self.assertIn("needle", item.searchable_text())

    def test_golden_case_limit_validated(self):
        with self.assertRaises(S.SchemaError):
            S.GoldenCase(id="c1", query="q", limit=0,
                         expected=[S.JudgedItem(kind="message", item_id="1", grade=1)])


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


def _corp(*items):
    return S.Corpus(items=list(items))


def _msg(item_id, body, created="2024-01-01T00:00:00Z", source="banodoco-discord"):
    return S.CorpusItem(kind="message", source=source, item_id=item_id, body=body, created_at=created)


def _dist(item_id, body, created="2024-01-01T00:00:00Z", status="approved", title=""):
    return S.CorpusItem(kind="distillation", source="h", item_id=item_id, title=title, body=body,
                        status=status, created_at=created)


class LegacyAdapterTests(unittest.TestCase):
    def test_substring_match_on_title_or_body(self):
        corp = _corp(
            _msg("1", "hello world"),
            S.CorpusItem(kind="article", source="s", item_id="2", title="World Atlas", body="zzz"),
        )
        adapter = LegacyIlikeAdapter(corp)
        res = adapter.retrieve(S.Query(query="world", limit=10))
        ids = {r.item_id for r in res}
        self.assertEqual(ids, {"1", "2"})

    def test_case_insensitive(self):
        corp = _corp(_msg("1", "CFG Scale"))
        res = LegacyIlikeAdapter(corp).retrieve(S.Query(query="cfg scale", limit=10))
        self.assertEqual([r.item_id for r in res], ["1"])

    def test_doubled_limit_and_distillations_first(self):
        # 3 distillations + 3 messages all match "term"; limit=2 → 4 results,
        # distillations first (the documented doubled-limit behaviour).
        corp = _corp(
            _dist("d1", "term", created="2024-01-03"),
            _dist("d2", "term", created="2024-01-02"),
            _dist("d3", "term", created="2024-01-01"),
            _msg("m1", "term", created="2024-01-03"),
            _msg("m2", "term", created="2024-01-02"),
            _msg("m3", "term", created="2024-01-01"),
        )
        res = LegacyIlikeAdapter(corp).retrieve(S.Query(query="term", limit=2))
        self.assertEqual(len(res), 4)  # 2 distillations + 2 others
        kinds = [r.kind for r in res]
        self.assertEqual(kinds, ["distillation", "distillation", "message", "message"])

    def test_kinds_source_since_filters(self):
        corp = _corp(
            _msg("m1", "term", created="2024-03-01", source="banodoco-discord"),
            _msg("m2", "term", created="2024-01-01", source="other"),
        )
        adapter = LegacyIlikeAdapter(corp)
        # since filter drops the older row
        res = adapter.retrieve(S.Query(query="term", limit=10, filters={"since": "2024-02-01"}))
        self.assertEqual([r.item_id for r in res], ["m1"])
        # source filter
        res = adapter.retrieve(S.Query(query="term", limit=10, filters={"sources": ["other"]}))
        self.assertEqual([r.item_id for r in res], ["m2"])

    def test_legacy_ignores_item_ids_filter(self):
        # The legacy endpoint never honored item_ids; it must not pretend to.
        corp = _corp(_msg("m1", "term"), _msg("m2", "term"))
        res = LegacyIlikeAdapter(corp).retrieve(
            S.Query(query="term", limit=10, filters={"kinds": ["message"], "item_ids": ["m1"]})
        )
        self.assertEqual({r.item_id for r in res}, {"m1", "m2"})


class FixtureAdapterTests(unittest.TestCase):
    def test_stub_empty(self):
        self.assertEqual(StubAdapter().retrieve(S.Query(query="x", limit=5)), [])

    def test_oracle_grade_order_within_limit(self):
        expected = [
            S.JudgedItem(kind="message", item_id="lo", grade=1),
            S.JudgedItem(kind="message", item_id="hi", grade=3),
            S.JudgedItem(kind="message", item_id="mid", grade=2),
        ]
        res = OracleAdapter().retrieve(S.Query(query="x", limit=2, expected=expected))
        self.assertEqual([r.item_id for r in res], ["hi", "mid"])  # grade desc, limit 2

    def test_oracle_excludes_zero_grade(self):
        expected = [
            S.JudgedItem(kind="message", item_id="rel", grade=2),
            S.JudgedItem(kind="message", item_id="norel", grade=0),
        ]
        res = OracleAdapter().retrieve(S.Query(query="x", limit=5, expected=expected))
        self.assertEqual([r.item_id for r in res], ["rel"])

    def test_reverse_differs_from_legacy_order(self):
        corp = _corp(
            _msg("1", "term", created="2024-01-01"),
            _msg("2", "term", created="2024-03-01"),
            _msg("3", "term", created="2024-02-01"),
        )
        legacy = [r.item_id for r in LegacyIlikeAdapter(corp).retrieve(S.Query(query="term", limit=5))]
        reverse = [r.item_id for r in ReverseAdapter(corp).retrieve(S.Query(query="term", limit=5))]
        # legacy orders by created desc → [2,3,1]; reverse by id asc → [1,2,3]
        self.assertEqual(legacy, ["2", "3", "1"])
        self.assertEqual(reverse, ["1", "2", "3"])

    def test_error_adapter_raises(self):
        with self.assertRaises(RetrievalError):
            ErrorAdapter().retrieve(S.Query(query="x", limit=5))

    def test_timeout_adapter_raises(self):
        with self.assertRaises(RetrievalTimeout):
            TimeoutAdapter().retrieve(S.Query(query="x", limit=5))

    def test_build_adapter_unknown_rejected(self):
        with self.assertRaises(ValueError):
            build_adapter("nope")

    def test_register_adapter(self):
        class Mine:
            name = "mine"

            def retrieve(self, query):
                return []

        register_adapter("mine_test", Mine)
        self.assertIn("mine_test", ADAPTERS)
        inst = build_adapter("mine_test")
        self.assertEqual(inst.name, "mine")
        del ADAPTERS["mine_test"]


# ---------------------------------------------------------------------------
# Runner integration
# ---------------------------------------------------------------------------


class RunnerTests(unittest.TestCase):
    def test_error_adapter_failure_rate_one_recall_zero(self):
        golden = S.GoldenSet(cases=[
            S.GoldenCase(id="c1", query="term",
                         expected=[S.JudgedItem(kind="message", item_id="1", grade=3)]),
            S.GoldenCase(id="c2", query="miss", expect_no_hit=True),
        ])
        report = run_eval(ErrorAdapter(), golden, now=lambda: "t")
        self.assertEqual(report.counts["failure_rate"], 1.0)
        self.assertEqual(report.counts["error_rate"], 1.0)
        self.assertEqual(report.overall["recall@10"], 0.0)
        self.assertEqual(report.counts["n_judged"], 1)

    def test_timeout_adapter_timeout_rate_one(self):
        golden = S.GoldenSet(cases=[
            S.GoldenCase(id="c1", query="term",
                         expected=[S.JudgedItem(kind="message", item_id="1", grade=3)]),
        ])
        report = run_eval(TimeoutAdapter(), golden, now=lambda: "t")
        self.assertEqual(report.counts["timeout_rate"], 1.0)
        self.assertEqual(report.counts["failure_rate"], 1.0)

    def test_stub_zero_result_and_no_hit_satisfied(self):
        golden = S.GoldenSet(cases=[
            S.GoldenCase(id="c1", query="term",
                         expected=[S.JudgedItem(kind="message", item_id="1", grade=3)]),
            S.GoldenCase(id="c2", query="miss", expect_no_hit=True),
        ])
        report = run_eval(StubAdapter(), golden, now=lambda: "t")
        self.assertEqual(report.counts["zero_result_rate"], 1.0)
        self.assertEqual(report.counts["no_hit_satisfied_rate"], 1.0)
        self.assertEqual(report.overall["recall@10"], 0.0)

    def test_no_hit_returning_results_not_satisfied(self):
        # Oracle would "find" things even for a no-hit query only if expected
        # existed; for a true no-hit case expected is empty, so oracle returns [].
        # Use a custom adapter that wrongly returns a result for a no-hit query.
        class Noisy:
            name = "noisy"

            def retrieve(self, query):
                return [S.Result(kind="message", item_id="junk")]

        golden = S.GoldenSet(cases=[S.GoldenCase(id="c1", query="miss", expect_no_hit=True)])
        report = run_eval(Noisy(), golden, now=lambda: "t")
        self.assertEqual(report.counts["no_hit_satisfied_rate"], 0.0)

    def test_oracle_perfect_on_seed_fixtures(self):
        corpus = load_corpus(FIXTURES / "corpus.json")
        golden = load_golden_set(FIXTURES / "golden.json")
        report = run_eval(OracleAdapter(corpus), golden, now=lambda: "t")
        self.assertAlmostEqual(report.overall["recall@10"], 1.0)
        self.assertAlmostEqual(report.overall["mrr"], 1.0)
        self.assertAlmostEqual(report.overall["ndcg@10"], 1.0)
        self.assertEqual(report.counts["failure_rate"], 0.0)

    def test_legacy_seed_metrics_match_hand_computation(self):
        corpus = load_corpus(FIXTURES / "corpus.json")
        golden = load_golden_set(FIXTURES / "golden.json")
        report = run_eval(LegacyIlikeAdapter(corpus), golden, now=lambda: "t")
        # 9 judged cases; only GC04 misses one of two relevant items.
        self.assertEqual(report.counts["n_judged"], 9)
        self.assertEqual(report.counts["n_no_hit"], 1)
        self.assertAlmostEqual(report.overall["recall@10"], 0.9444, places=4)
        self.assertAlmostEqual(report.overall["ndcg@10"], 0.9764, places=3)
        self.assertAlmostEqual(report.overall["mrr"], 1.0)

    def test_snowflake_round_trips_through_runner_and_json(self):
        snowflake = "2987654321098765432"
        corpus = _corp(_msg(snowflake, "CFG scale"))
        golden = S.GoldenSet(cases=[
            S.GoldenCase(id="c1", query="CFG scale",
                         expected=[S.JudgedItem(kind="message", item_id=snowflake, grade=3)],
                         categories=["snowflake"]),
        ])
        report = run_eval(LegacyIlikeAdapter(corpus), golden, now=lambda: "t")
        blob = json.dumps(report.to_dict())
        decoded = json.loads(blob)
        # find the case's ranked list and confirm the id survived as a string
        case = decoded["per_case"][0]
        self.assertEqual(case["ranked"][0][1], snowflake)
        self.assertIsInstance(case["ranked"][0][1], str)
        self.assertAlmostEqual(report.overall["recall@10"], 1.0)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


class LoaderTests(unittest.TestCase):
    def test_load_seed_json(self):
        corpus = load_corpus(FIXTURES / "corpus.json")
        golden = load_golden_set(FIXTURES / "golden.json")
        self.assertEqual(len(corpus.items), 7)
        self.assertEqual(len(golden.cases), 10)
        self.assertEqual(len(golden.judged), 9)
        self.assertEqual(len(golden.no_hit), 1)

    def test_load_yaml_when_available(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not installed")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "g.yaml"
            path.write_text(
                "cases:\n"
                "- id: c1\n"
                "  query: term\n"
                "  expected:\n"
                "    - {kind: message, item_id: '1', grade: 2}\n",
                encoding="utf-8",
            )
            golden = load_golden_set(path)
            self.assertEqual(golden.cases[0].query, "term")
            self.assertEqual(golden.cases[0].expected[0].grade, 2)

    def test_load_strict_references_rejects_dangling(self):
        import eval.retrieval.loader as L

        corpus = _corp(_msg("1", "x"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "g.json"
            path.write_text(json.dumps({
                "cases": [{"id": "c1", "query": "x",
                           "expected": [{"kind": "message", "item_id": "999", "grade": 1}]}]
            }), encoding="utf-8")
            old = L.STRICT_REFERENCES
            L.STRICT_REFERENCES = True
            try:
                with self.assertRaises(ValueError):
                    load_golden_set(path, corpus=corpus)
            finally:
                L.STRICT_REFERENCES = old


# ---------------------------------------------------------------------------
# Comparison + CLI + stability
# ---------------------------------------------------------------------------


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.corpus = load_corpus(FIXTURES / "corpus.json")
        self.golden = load_golden_set(FIXTURES / "golden.json")

    def _strip_latency(self, comparison):
        import copy

        comp = copy.deepcopy(comparison)
        for r in comp["reports"]:
            r["latency"] = {}
            for c in r["per_case"]:
                c.pop("latency_ms", None)
        return comp

    def test_compare_runs_all_systems(self):
        comp = compare_systems(["legacy", "stub", "oracle"], self.corpus, self.golden,
                               now=lambda: "fixed")
        self.assertEqual(comp["systems"], ["legacy", "stub", "oracle"])
        names = {r["system"] for r in comp["reports"]}
        self.assertEqual(names, {"legacy", "stub", "oracle"})
        # oracle best, stub worst
        by = {r["system"]: r["overall"]["recall@10"] for r in comp["reports"]}
        self.assertGreater(by["oracle"], by["legacy"])
        self.assertEqual(by["stub"], 0.0)

    def test_report_stability_ignoring_latency(self):
        a = compare_systems(["legacy", "oracle"], self.corpus, self.golden, now=lambda: "t")
        b = compare_systems(["legacy", "oracle"], self.corpus, self.golden, now=lambda: "t")
        self.assertEqual(self._strip_latency(a), self._strip_latency(b))
        # markdown rendered from identical (latency-stripped) input is byte-identical
        self.assertEqual(
            render_markdown(self._strip_latency(a)),
            render_markdown(self._strip_latency(b)),
        )

    def test_markdown_contains_required_sections(self):
        comp = compare_systems(["legacy", "stub", "oracle"], self.corpus, self.golden,
                               now=lambda: "t")
        md = render_markdown(comp)
        for token in ["Recall@10", "nDCG@10", "MRR", "failure rate",
                      "p50_ms", "p95_ms", "p99_ms", "exact_name", "workflow_code"]:
            self.assertIn(token, md)

    def test_one_command_subprocess_emits_json_and_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [
                    sys.executable, "-m", "eval.retrieval.compare",
                    "--systems", "legacy,stub,oracle",
                    "--corpus", str(FIXTURES / "corpus.json"),
                    "--golden", str(FIXTURES / "golden.json"),
                    "--out-dir", tmp,
                    "--name", "cli",
                    "--generated-at", "2026-07-28T00:00:00+00:00",
                ],
                cwd=str(_REPO),
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            out = Path(tmp)
            json_files = list(out.glob("*_report.json"))
            self.assertTrue(any(p.name == "comparison_cli.json" for p in out.glob("*.json")))
            md = out / "comparison_cli.md"
            self.assertTrue(md.exists())
            self.assertIn("Recall@10", md.read_text(encoding="utf-8"))
            # legacy per-system report present
            self.assertTrue(any(p.name == "legacy_report.json" for p in json_files))

    def test_list_adapters(self):
        proc = subprocess.run(
            [sys.executable, "-m", "eval.retrieval.compare", "--list-adapters"],
            cwd=str(_REPO), capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("legacy", proc.stdout)
        self.assertIn("remote", proc.stdout)


class RemoteAdapterTests(unittest.TestCase):
    def test_remote_without_url_rejected(self):
        import os

        old = os.environ.pop("HIVEMIND_SEARCH_URL", None)
        try:
            with self.assertRaises(Exception):
                RemoteSearchAdapter()
        finally:
            if old is not None:
                os.environ["HIVEMIND_SEARCH_URL"] = old

    def test_remote_network_opt_in_skip(self):
        # Network use is opt-in; without the flag the adapter refuses to call out.
        import os

        os.environ.pop("HIVEMIND_EVAL_NETWORK", None)
        os.environ["HIVEMIND_SEARCH_URL"] = "https://example.invalid/search"
        try:
            adapter = RemoteSearchAdapter()
            with self.assertRaises(Exception):
                adapter.retrieve(S.Query(query="x", limit=5))
        finally:
            os.environ.pop("HIVEMIND_SEARCH_URL", None)


if __name__ == "__main__":
    unittest.main()
