"""Deterministic offline tests for the production golden set (task 0.6).

Covers: schema/loader loading of golden-v1.json, the required coverage/grade/
uniqueness/filter/snowflake/snapshot-integrity contracts, the validator's offline
checks, the probe's pure helpers, the untouched seed fixture (contract smoke
test), and one-command harness reportability over the production set
(legacy-vs-oracle on the bounded evidence snapshot). No network: the opt-in live
identity check is exercised only as a construction/flag test.
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
sys.path.insert(0, str(_REPO / "scripts"))

from eval.retrieval.adapters import LegacyIlikeAdapter, OracleAdapter  # noqa: E402
from eval.retrieval.compare import compare_systems, render_markdown  # noqa: E402
from eval.retrieval.loader import load_corpus, load_golden_set  # noqa: E402
from eval.retrieval.runner import run_eval  # noqa: E402
from eval.retrieval.schema import normalize_kind  # noqa: E402

import validate_golden as VG  # noqa: E402
import golden_probe as GP  # noqa: E402

GOLDEN_DIR = _REPO / "eval" / "retrieval" / "golden"
GOLDEN = GOLDEN_DIR / "golden-v1.json"
CORPUS = GOLDEN_DIR / "corpus-v1.json"
EVIDENCE = GOLDEN_DIR / "evidence-v1.json"
SEED_GOLDEN = _REPO / "eval" / "retrieval" / "fixtures" / "golden.json"
SEED_CORPUS = _REPO / "eval" / "retrieval" / "fixtures" / "corpus.json"


def _raw_cases(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw["cases"] if isinstance(raw, dict) else raw


class GoldenSetContractTests(unittest.TestCase):
    """The production golden set meets the task-0.6 contract."""

    @classmethod
    def setUpClass(cls):
        cls.cases = _raw_cases(GOLDEN)
        cls.judged = [c for c in cls.cases if not c.get("expect_no_hit")]
        cls.no_hit = [c for c in cls.cases if c.get("expect_no_hit")]
        cls.gs = load_golden_set(GOLDEN)
        cls.corpus = load_corpus(CORPUS)

    def test_loads_through_schema_loader(self):
        # reportability: the task-0.5 loader/schema accept the production set.
        self.assertEqual(len(self.gs.cases), len(self.cases))
        self.assertGreaterEqual(len(self.gs.cases), 100)

    def test_minimum_count_and_balance(self):
        self.assertGreaterEqual(len(self.cases), 100)
        self.assertGreaterEqual(len(self.judged), 90)
        self.assertGreaterEqual(len(self.no_hit), 5)

    def test_required_category_coverage(self):
        present = set()
        for c in self.cases:
            present.update(c.get("categories", []))
        missing = VG.REQUIRED_CATEGORIES - present
        self.assertEqual(missing, set(), f"missing categories: {missing}")

    def test_unique_case_ids(self):
        ids = [c["id"] for c in self.cases]
        self.assertEqual(len(ids), len(set(ids)))

    def test_no_duplicate_query_filters(self):
        seen = set()
        for c in self.cases:
            key = json.dumps({"q": c["query"], "f": c.get("filters", {})}, sort_keys=True)
            self.assertNotIn(key, seen, f"duplicate (query,filters) at {c['id']}")
            seen.add(key)

    def test_grades_valid_and_rubric_distinguished(self):
        buckets = {0: 0, 1: 0, 2: 0, 3: 0}
        for c in self.judged:
            grades = [j["grade"] for j in c["expected"]]
            self.assertTrue(all(g in VG.VALID_GRADES for g in grades))
            self.assertTrue(any(g >= 1 for g in grades), f"{c['id']} has no grade>=1")
            for g in grades:
                buckets[g] += 1
        # rubric must distinguish primary / strong / marginal
        self.assertGreaterEqual(buckets[3], 20)
        self.assertGreaterEqual(buckets[2], 5)
        self.assertGreaterEqual(buckets[1], 2)

    def test_item_ids_filters_valid(self):
        for c in self.cases:
            f = c.get("filters") or {}
            if f.get("item_ids") is not None:
                self.assertEqual(len(f["kinds"]), 1, f"{c['id']}: item_ids needs one kind")

    def test_no_hit_consistency(self):
        for c in self.no_hit:
            self.assertEqual(c.get("expected", []), [])

    def test_snowflakes_are_strings_in_file(self):
        # raw JSON must carry item_ids as strings (no float64 rounding).
        for c in self.cases:
            for j in c.get("expected", []) or []:
                self.assertIsInstance(j["item_id"], str, f"{c['id']} non-string item_id")
            for iid in (c.get("filters", {}) or {}).get("item_ids", []) or []:
                self.assertIsInstance(iid, str, f"{c['id']} non-string filters.item_ids")

    def test_snapshot_contains_every_judged_identity(self):
        snap = {(normalize_kind(it["kind"]), str(it["item_id"])) for it in
                json.loads(CORPUS.read_text(encoding="utf-8"))["items"]}
        for c in self.judged:
            for j in c["expected"]:
                key = (normalize_kind(j["kind"]), str(j["item_id"]))
                self.assertIn(key, snap, f"{c['id']}: {key} not in snapshot")

    def test_snapshot_ids_anchored_in_live_evidence(self):
        ev = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        ev_ids = set()
        for w in ev["workflows"]["items"]:
            ev_ids.add(("resource", str(w["item_id"])))
        for d in ev["distillations"]["items"]:
            ev_ids.add(("distillation", str(d["item_id"])))
        for hits in ev["messages"]["map"].values():
            for h in hits:
                if "error" not in h and h.get("item_id"):
                    ev_ids.add(("message", str(h["item_id"])))
        snap_items = json.loads(CORPUS.read_text(encoding="utf-8"))["items"]
        for it in snap_items:
            if (it.get("metadata") or {}).get("distractor"):
                continue
            key = (normalize_kind(it["kind"]), str(it["item_id"]))
            self.assertIn(key, ev_ids, f"snapshot {key} not anchored in evidence")


class ValidatorTests(unittest.TestCase):
    def test_offline_validator_no_problems(self):
        result = VG.validate_offline(GOLDEN, CORPUS, EVIDENCE)
        self.assertEqual(result["problems"], [], msg=result["problems"])

    def test_validator_flags_bad_grade(self):
        bad = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"cases": [{
            "id": "B1", "query": "x", "categories": ["exact_name"],
            "expected": [{"kind": "message", "item_id": "1", "grade": 9}],
        }]}, bad)
        bad.close()
        result = VG.validate_offline(Path(bad.name), CORPUS, None)
        self.assertTrue(any("outside 0..3" in p for p in result["problems"]))

    def test_validator_flags_missing_category(self):
        bad = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"cases": [{
            "id": "B1", "query": "x", "categories": ["exact_name"],
            "expected": [{"kind": "message", "item_id": "1", "grade": 3}],
        }]}, bad)
        bad.close()
        result = VG.validate_offline(Path(bad.name), CORPUS, None)
        self.assertTrue(any("missing required categories" in p for p in result["problems"]))

    def test_validator_flags_ambiguous_item_ids(self):
        bad = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"cases": [{
            "id": "B1", "query": "x", "categories": ["single_workflow"],
            "filters": {"item_ids": ["1"]},  # no kinds
            "expected": [{"kind": "workflow", "item_id": "1", "grade": 3}],
        }]}, bad)
        bad.close()
        result = VG.validate_offline(Path(bad.name), CORPUS, None)
        self.assertTrue(any("exactly one kinds" in p for p in result["problems"]))

    def test_network_gate_off_by_default(self):
        import os
        old = os.environ.pop("HIVEMIND_EVAL_NETWORK", None)
        try:
            self.assertFalse(VG.network_enabled())
        finally:
            if old is not None:
                os.environ["HIVEMIND_EVAL_NETWORK"] = old


class ProbeHelperTests(unittest.TestCase):
    def test_bound_snippet_collapses_and_truncates(self):
        self.assertEqual(GP.bound_snippet("   a   b   ", 100), "a b")
        out = GP.bound_snippet("x" * 500, 10)
        self.assertEqual(len(out), 11)  # 10 chars + ellipsis
        self.assertTrue(out.endswith("…"))
        self.assertEqual(GP.bound_snippet(None, 10), "")
        self.assertEqual(GP.bound_snippet("", 10), "")

    def test_safe_symbols_drops_long_and_keeps_identifiers(self):
        syms = ["WanVideoSampler", "a" * 200, "", "VAEDecode", "123"]
        out = GP.safe_symbols(syms)
        self.assertEqual(out, ["WanVideoSampler", "VAEDecode"])  # long junk dropped, deduped

    def test_short_hash_stable_and_short(self):
        h = GP.short_hash("Wan2.2 Image-to-Video")
        self.assertEqual(h, GP.short_hash("Wan2.2 Image-to-Video"))
        self.assertEqual(len(h), 12)
        self.assertNotEqual(h, GP.short_hash("different"))

    def test_identity_key_stringifies(self):
        self.assertEqual(GP.identity_key("message", 123), ("message", "123"))

    def test_endpoint_ref_parses(self):
        self.assertEqual(GP.endpoint_ref("https://abc123.supabase.co/rest/v1"), None)
        ref = "ujlwuvkrxlvoswwkerdf"
        self.assertEqual(GP.endpoint_ref(f"https://{ref}.supabase.co/rest/v1"), ref)


class SeedFixtureUntouchedTests(unittest.TestCase):
    """The small seed fixture stays a valid contract smoke test."""

    def test_seed_fixture_loads_and_counts(self):
        seed = load_golden_set(SEED_GOLDEN)
        corp = load_corpus(SEED_CORPUS)
        self.assertEqual(len(seed.cases), 10)
        self.assertEqual(len(corp.items), 7)
        self.assertEqual(len(seed.judged), 9)
        self.assertEqual(len(seed.no_hit), 1)

    def test_oracle_perfect_on_seed(self):
        report = run_eval(OracleAdapter(load_corpus(SEED_CORPUS)),
                          load_golden_set(SEED_GOLDEN), now=lambda: "t")
        self.assertAlmostEqual(report.overall["recall@10"], 1.0)


class HarnessReportabilityTests(unittest.TestCase):
    """The production set is reportable through the one-command task-0.5 harness."""

    @classmethod
    def setUpClass(cls):
        cls.corpus = load_corpus(CORPUS)
        cls.golden = load_golden_set(GOLDEN)

    def test_oracle_is_perfect_ceiling_on_production_set(self):
        report = run_eval(OracleAdapter(self.corpus), self.golden, now=lambda: "t")
        # perfect ceiling over every judged case
        self.assertAlmostEqual(report.overall["recall@10"], 1.0, places=6)
        self.assertAlmostEqual(report.overall["mrr"], 1.0, places=6)
        self.assertAlmostEqual(report.overall["ndcg@10"], 1.0, places=6)
        self.assertEqual(report.counts["failure_rate"], 0.0)

    def test_legacy_runs_and_oracle_beats_or_matches_legacy(self):
        comp = compare_systems(["legacy", "oracle"], self.corpus, self.golden, now=lambda: "t")
        by = {r["system"]: r["overall"]["recall@10"] for r in comp["reports"]}
        self.assertGreaterEqual(by["oracle"], by["legacy"])
        self.assertGreater(by["oracle"], 0.0)

    def test_markdown_report_renders_production_categories(self):
        comp = compare_systems(["legacy", "oracle"], self.corpus, self.golden, now=lambda: "t")
        md = render_markdown(comp)
        for token in ["exact_name", "workflow_code", "single_workflow", "no_hit",
                      "nDCG@10", "failure rate"]:
            self.assertIn(token, md)

    def test_one_command_cli_over_production_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, "-m", "eval.retrieval.compare",
                 "--systems", "legacy,oracle",
                 "--corpus", str(CORPUS), "--golden", str(GOLDEN),
                 "--out-dir", tmp, "--name", "phase0-golden-v1",
                 "--generated-at", "2026-07-28T00:00:00+00:00"],
                cwd=str(_REPO), capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertTrue((Path(tmp) / "comparison_phase0-golden-v1.json").exists())
            self.assertTrue((Path(tmp) / "comparison_phase0-golden-v1.md").exists())


if __name__ == "__main__":
    unittest.main()
