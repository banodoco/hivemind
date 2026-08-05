#!/usr/bin/env python3
"""Unit tests for scripts/probe_golden_latency.py — failure-preservation + secrecy.

No database is required: these exercise the parser, the report builder, and the
plan summarizer on synthetic psql output. The probe is failure-preserving (a
mid-run kill or statement timeout still yields a valid, secret-safe report) and
never emits query strings, message bodies, snippets, or credentials.
"""
from __future__ import annotations

import json
import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import probe_golden_latency as P  # noqa: E402


def _case(cid, query="irrelevant_query_string", cats=None, filters=None):
    return {"id": cid, "query": query, "categories": cats or [],
            "filters": filters or {}}


def _plan_block(exec_ms: str) -> str:
    # Mimic psql -A -t EXPLAIN (ANALYZE) output; only the Execution Time line
    # is load-bearing for parse_latency — the rest is noise it must ignore.
    return (
        "Aggregate  (cost=10.00..10.01 rows=1 width=100) (actual ...)\n"
        "  ->  Seq Scan on public.lexical_documents (cost=0.00..9.00 rows=100 width=100)\n"
        f"Execution Time: {exec_ms} ms\n"
    )


class ParseLatencyTests(unittest.TestCase):
    def test_all_measured(self) -> None:
        cases = [_case("g1"), _case("g2"), _case("g3")]
        raw = (
            "CASE\tg1\n123\n" + _plan_block("10.0") +
            "CASE\tg2\n456\n" + _plan_block("20.0") +
            "CASE\tg3\n789\n" + _plan_block("30.0")
        )
        pc, last = P.parse_latency(raw, cases)
        self.assertEqual([p["status"] for p in pc], ["measured"] * 3)
        self.assertEqual([p["time_ms"] for p in pc], [10.0, 20.0, 30.0])
        self.assertEqual(last, "g3")

    def test_partial_kill_marks_last_started_and_not_reached(self) -> None:
        # Session killed during g3's EXPLAIN: g3 marker present, no Execution
        # Time; g4 marker never emitted.
        cases = [_case("g1"), _case("g2"), _case("g3"), _case("g4")]
        raw = (
            "CASE\tg1\n" + _plan_block("10.0") +
            "CASE\tg2\n" + _plan_block("800.0") +
            "CASE\tg3\n" + "  ->  Bitmap Heap Scan on public.lexical_documents\n"
        )
        pc, last = P.parse_latency(raw, cases)
        self.assertEqual(pc[0]["status"], "measured")
        self.assertEqual(pc[0]["time_ms"], 10.0)
        self.assertEqual(pc[1]["status"], "measured")
        self.assertEqual(pc[1]["time_ms"], 800.0)
        self.assertEqual(pc[2]["status"], "started_not_measured")
        self.assertIsNone(pc[2]["time_ms"])
        self.assertEqual(pc[3]["status"], "not_reached")
        self.assertEqual(last, "g3")

    def test_statement_timeout_then_continues(self) -> None:
        # g2's EXPLAIN hits statement_timeout (canceled -> no Execution Time);
        # ON_ERROR_STOP=0 lets the session continue to g3 which measures fine.
        cases = [_case("g1"), _case("g2"), _case("g3")]
        raw = (
            "CASE\tg1\n" + _plan_block("12.0") +
            "CASE\tg2\n" + "ERROR: canceling statement due to statement timeout\n"
            "CASE\tg3\n" + _plan_block("9.0")
        )
        pc, last = P.parse_latency(raw, cases)
        self.assertEqual(pc[0]["status"], "measured")
        self.assertEqual(pc[1]["status"], "started_not_measured")
        self.assertEqual(pc[2]["status"], "measured")
        self.assertEqual(last, "g3")

    def test_stderr_statement_timeout_marker(self) -> None:
        # stderr is segmented by safe ERRCASE:<opaque-id> markers. g2's EXPLAIN
        # hit statement timeout (the cancel ERROR lands on stderr), and g3 still
        # measures fine on stdout.
        cases = [_case("g1"), _case("g2"), _case("g3")]
        raw = (
            "CASE\tg1\n" + _plan_block("12.0") +
            "CASE\tg2\n" +  # no Execution Time (timed out)
            "CASE\tg3\n" + _plan_block("9.0")
        )
        stderr = ("ERRCASE:g2\n"
                  "ERROR: canceling statement due to statement timeout\n")
        pc, last = P.parse_latency(raw, cases, stderr_text=stderr)
        self.assertEqual(pc[0]["status"], "measured")
        self.assertEqual(pc[1], {"id": "g2", "status": "statement_timeout",
                                 "time_ms": None, "error_kind": "statement_timeout"})
        self.assertEqual(pc[2]["status"], "measured")
        self.assertEqual(last, "g3")

    def test_stderr_other_error_is_statement_error(self) -> None:
        # Any other ERROR: on stderr (not the timeout string) becomes
        # statement_error; no stderr text is retained in the per-case rows.
        cases = [_case("g1"), _case("g2")]
        raw = "CASE\tg1\n" + _plan_block("10.0") + "CASE\tg2\n"
        stderr = ("ERRCASE:g2\n"
                  "ERROR: syntax error at or near \"SECRETNEEDLE\"\n")
        pc, _ = P.parse_latency(raw, cases, stderr_text=stderr)
        self.assertEqual(pc[1], {"id": "g2", "status": "statement_error",
                                 "time_ms": None, "error_kind": "statement_error"})
        self.assertNotIn("SECRETNEEDLE", json.dumps(pc))  # no stderr text retained

    def test_partial_kill_each_status_once(self) -> None:
        # Partial stdout from a Python timeout still yields measured,
        # started_not_measured, and not_reached exactly once each.
        cases = [_case("g1"), _case("g2"), _case("g3")]
        raw = (
            "CASE\tg1\n" + _plan_block("10.0") +
            "CASE\tg2\n" + "  ->  Bitmap Heap Scan on public.lexical_documents\n"
        )
        pc, _ = P.parse_latency(raw, cases)
        statuses = [p["status"] for p in pc]
        self.assertEqual(statuses.count("measured"), 1)
        self.assertEqual(statuses.count("started_not_measured"), 1)
        self.assertEqual(statuses.count("not_reached"), 1)

    def test_each_requested_case_id_appears_once(self) -> None:
        cases = [_case("g1"), _case("g2"), _case("g3")]
        raw = (
            "CASE\tg1\n" + _plan_block("10.0") +
            "CASE\tg2\n" + _plan_block("20.0") +
            "CASE\tg3\n" + _plan_block("30.0")
        )
        pc, _ = P.parse_latency(raw, cases)
        ids = [p["id"] for p in pc]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(sorted(ids), ["g1", "g2", "g3"])

    def test_timed_gt_1_averages_execution_times(self) -> None:
        cases = [_case("g1")]
        raw = "CASE\tg1\n" + _plan_block("10.0") + _plan_block("30.0")
        pc, _ = P.parse_latency(raw, cases)
        self.assertEqual(pc[0]["status"], "measured")
        self.assertEqual(pc[0]["time_ms"], 20.0)  # mean(10, 30)

    def test_empty_raw_all_not_reached(self) -> None:
        cases = [_case("g1"), _case("g2")]
        pc, last = P.parse_latency("", cases)
        self.assertEqual([p["status"] for p in pc], ["not_reached"] * 2)
        self.assertIsNone(last)

    def test_ordering_resilient_to_numeric_noise(self) -> None:
        # Plan numbers (100, 9.00) must NOT be mistaken for Execution Times.
        cases = [_case("g1"), _case("g2")]
        raw = "CASE\tg1\n" + _plan_block("42.0") + "CASE\tg2\n" + _plan_block("7.0")
        pc, _ = P.parse_latency(raw, cases)
        self.assertEqual([p["time_ms"] for p in pc], [42.0, 7.0])


class ReportSecrecyTests(unittest.TestCase):
    def _report(self, per_case, cases, stderr_tail="", last_started="g2"):
        return P.build_latency_report(
            cases, per_case, last_started=last_started, prime=1, timed=1,
            statement_timeout_ms=5000, host_family="pooler", port="6543",
            python_timeout_fired=True, psql_rc=None, elapsed_s=120.0,
            stderr_tail=stderr_tail)

    def test_report_omits_query_strings(self) -> None:
        secret_q = "TOPSECRET_query_with_credentials"
        cases = [_case("g1", query=secret_q, cats=["workflow_code"],
                       filters={"kinds": ["workflow"], "authors": ["a"],
                                "channels": ["c"]}),
                 _case("g2", query="another")]
        per_case = [{"id": "g1", "status": "measured", "time_ms": 10.0},
                    {"id": "g2", "status": "started_not_measured", "time_ms": None}]
        rep = self._report(per_case, cases)
        blob = json.dumps(rep)
        self.assertNotIn(secret_q, blob)              # no query string leaked
        self.assertNotIn("another", blob)             # no query string leaked
        for pc in rep["per_case"]:                    # no per-case query field
            self.assertNotIn("query", pc)
            self.assertNotIn("filters", pc)           # filters never leaked
            self.assertNotIn("author", pc)
            self.assertNotIn("channel", pc)
        # opaque id + categories ARE kept; timing is public.
        self.assertEqual(rep["per_case"][0]["id"], "g1")
        self.assertEqual(rep["per_case"][0]["categories"], ["workflow_code"])

    def test_per_case_public_keys_exact(self) -> None:
        cases = [_case("g1", filters={"kinds": ["workflow"], "authors": ["a"],
                                      "channels": ["c"]})]
        # normal measured row: exactly {id, categories, status, time_ms}
        per_case = [{"id": "g1", "status": "measured", "time_ms": 10.0}]
        pub = P._per_case_public(per_case, cases)
        self.assertEqual(set(pub[0].keys()), {"id", "categories", "status", "time_ms"})
        # error row: those four plus error_kind
        per_case2 = [{"id": "g1", "status": "statement_timeout", "time_ms": None,
                      "error_kind": "statement_timeout"}]
        pub2 = P._per_case_public(per_case2, cases)
        self.assertEqual(set(pub2[0].keys()),
                         {"id", "categories", "status", "time_ms", "error_kind"})

    def test_report_ignores_adversarial_stderr(self) -> None:
        cases = [_case("g1")]
        per_case = [{"id": "g1", "status": "not_reached", "time_ms": None}]
        # Adversarial stderr carrying every sensitive category the report must
        # never echo. The report ignores stderr completely.
        stderr = (
            "CREATE FUNCTION secret_fn() RETURNS void AS $$ BODYSECRET $$;\n"
            "SELECT * FROM secret_query WHERE body='BODYSECRET';\n"
            "snippet with SNIPPETSECRET and needle SECRETNEEDLE\n"
            "author AUTHORSECRET channel CHANNELSECRET\n"
            "postgres://user:supersecret12345678901234567890@host/db"
        )
        rep = self._report(per_case, cases, stderr_tail=stderr)
        blob = json.dumps(rep)
        for needle in ["secret_fn", "secret_query", "BODYSECRET", "SNIPPETSECRET",
                       "SECRETNEEDLE", "AUTHORSECRET", "CHANNELSECRET",
                       "supersecret12345678901234567890"]:
            self.assertNotIn(needle, blob)
        for key in rep:                                # no stderr-bearing key
            self.assertNotIn("stderr", key.lower())

    def test_report_gate_and_percentiles(self) -> None:
        cases = [_case(f"g{i}") for i in range(4)]
        per_case = [{"id": f"g{i}", "status": "measured", "time_ms": t}
                    for i, t in enumerate([10.0, 20.0, 30.0, 800.0])]
        rep = self._report(per_case, cases)
        self.assertEqual(rep["n_measured"], 4)
        self.assertEqual(rep["max_ms"], 800.0)
        self.assertEqual(rep["cases_over_750ms"], 1)
        self.assertEqual(rep["over_750ms_case_ids"], ["g3"])
        self.assertFalse(rep["gate_warm_p95_le_750ms"])
        self.assertTrue(rep["gate_all_measured"])  # all 4 measured here

    def test_report_marks_missing_and_last_started(self) -> None:
        cases = [_case("g1"), _case("g2"), _case("g3")]
        per_case = [{"id": "g1", "status": "measured", "time_ms": 5.0},
                    {"id": "g2", "status": "started_not_measured", "time_ms": None},
                    {"id": "g3", "status": "not_reached", "time_ms": None}]
        rep = self._report(per_case, cases, last_started="g2")
        self.assertEqual(rep["missing_case_ids"], ["g2", "g3"])
        self.assertEqual(rep["last_started_case_id"], "g2")
        self.assertTrue(rep["python_timeout_fired"])
        self.assertFalse(rep["gate_all_measured"])


class PlanSummarizerTests(unittest.TestCase):
    def test_keeps_nodes_and_indexes_drops_literals(self) -> None:
        block = (
            "Limit  (cost=100.00..100.50 rows=10 width=200)\n"
            "  ->  Sort  (cost=100.00..110.00 rows=1000 width=200)\n"
            "        Sort Key: lexical_rank DESC\n"
            "        ->  Hash Join  (cost=4.50..90.00 rows=100 width=200)\n"
            "              Hash Cond: (ld.item_id = mv.item_id)\n"
            "              ->  Bitmap Heap Scan on public.lexical_documents (cost=4.00..50.00 rows=80 width=150)\n"
            "                    Recheck Cond: (normalize(chunk_text) ~~ '%secretneedle%'::text)\n"
            "                    Filter: ((representation_type)::text = 'workflow_python'::text)\n"
            "                    ->  Bitmap Index Scan on lexical_documents_python_chunk_trgm_idx (cost=0.00..4.00 rows=80 width=0)\n"
            "              ->  Hash  (cost=1.00..1.00 rows=50 width=50)\n"
            "                    ->  Seq Scan on public.lexical_workflow_python_search mv (cost=0.00..1.00 rows=50 width=50)\n"
            "                          Filter: (search_norm ~~ '%secretneedle%'::text)\n"
        )
        nodes = P.summarize_plan(block)
        ops = [(n["node"], n.get("relation"), n.get("index")) for n in nodes]
        self.assertIn(("Limit", None, None), ops)
        self.assertIn(("Sort", None, None), ops)
        self.assertIn(("Hash Join", None, None), ops)
        self.assertIn(("Bitmap Heap Scan", "public.lexical_documents", None), ops)
        self.assertIn(("Bitmap Index Scan", None, "lexical_documents_python_chunk_trgm_idx"), ops)
        self.assertIn(("Seq Scan", "public.lexical_workflow_python_search", None), ops)
        blob = json.dumps(nodes)
        self.assertNotIn("secretneedle", blob)              # needle literal redacted
        self.assertNotIn("representation_type", blob)       # Filter literal line dropped
        self.assertNotIn("normalize(chunk_text)", blob)     # Recheck Cond line dropped
        self.assertNotIn("Hash Cond", blob)                 # property line dropped
        self.assertNotIn("Sort Key", blob)                  # property line dropped


class BuildScriptTests(unittest.TestCase):
    def test_latency_script_has_statement_timeout_and_markers(self) -> None:
        cases = [_case("g1", filters={"kinds": ["workflow"]}), _case("g2")]
        script = P.build_latency_script(cases, prime=1, timed=1,
                                        statement_timeout_ms=5000)
        self.assertIn("set statement_timeout='5000ms';", script)
        self.assertIn("CASE\tg1", script)
        self.assertIn("CASE\tg2", script)
        self.assertEqual(script.count("explain (analyze)"), 2)
        self.assertEqual(script.count("select count(*) from"), 2)  # prime per case

    def test_explain_script_is_non_analyze(self) -> None:
        cases = [_case("g1")]
        script = P.build_explain_script(cases, statement_timeout_ms=5000)
        self.assertIn("explain (costs on)", script)
        self.assertNotIn("analyze", script)

    def test_latency_script_emits_errcase_warn(self) -> None:
        # \warn ERRCASE:<opaque-id> lands on stderr before each case's work, so
        # per-statement errors can be attributed while ON_ERROR_STOP=0 continues.
        cases = [_case("g1"), _case("g2")]
        script = P.build_latency_script(cases, prime=1, timed=1,
                                        statement_timeout_ms=5000)
        for cid in ("g1", "g2"):
            self.assertIn("\\warn ERRCASE:" + cid, script)
        # the warn precedes the stdout CASE marker for its case
        self.assertLess(script.index("\\warn ERRCASE:g1"), script.index("CASE\tg1"))

    def test_explain_script_emits_errcase_warn(self) -> None:
        cases = [_case("g1"), _case("g2")]
        script = P.build_explain_script(cases, statement_timeout_ms=5000)
        for cid in ("g1", "g2"):
            self.assertIn("\\warn ERRCASE:" + cid, script)


if __name__ == "__main__":
    unittest.main()
