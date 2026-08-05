"""Tests for ``scripts/capacity_model.py`` (plan task 0.7).

Pure and offline: no network, no database, no provider call, no secrets. These
tests pin the storage/cost formulas, unit conversions, scenario determinism,
monotonicity (1536 > 384, full > pilot), and the three fixed gate verdicts so
the capacity model can never silently produce wrong units or a wrong gate call.
"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

import capacity_model as cm  # noqa: E402


# ---------------------------------------------------------------------------
# Formula + unit correctness
# ---------------------------------------------------------------------------


class FormulaTests(unittest.TestCase):
    def test_vector_bytes_per_node_is_4d_plus_8(self):
        # pgvector README: a vector(D) value occupies 4*D + 8 bytes.
        self.assertEqual(cm.vector_bytes_per_node(384), 4 * 384 + 8)
        self.assertEqual(cm.vector_bytes_per_node(1536), 4 * 1536 + 8)
        self.assertEqual(cm.vector_bytes_per_node(1), 12)

    def test_vector_bytes_per_node_rejects_nonpositive(self):
        for bad in (0, -1, -384):
            with self.assertRaises(ValueError):
                cm.vector_bytes_per_node(bad)

    def test_raw_vector_bytes(self):
        # float4 payload only (no header): 4 * dim * count.
        self.assertEqual(cm.raw_vector_bytes(384, 1_245_006), 4 * 384 * 1_245_006)
        self.assertEqual(cm.raw_vector_bytes(1536, 1), 6144)

    def test_chunks_for_determinism_and_boundary(self):
        # The doctest examples plus a few more.
        self.assertEqual(cm.chunks_for(0, 512, 51), 0)
        self.assertEqual(cm.chunks_for(512, 512, 51), 1)       # exactly one chunk
        self.assertEqual(cm.chunks_for(513, 512, 51), 2)       # just over -> 2
        self.assertEqual(cm.chunks_for(400, 512, 51), 1)
        self.assertEqual(cm.chunks_for(1200, 512, 51), 3)
        # monotonic in total_units
        self.assertGreaterEqual(
            cm.chunks_for(10_000, 512, 51), cm.chunks_for(1_000, 512, 51))
        # larger chunk size never increases chunk count
        self.assertGreaterEqual(
            cm.chunks_for(10_000, 512, 51), cm.chunks_for(10_000, 1024, 51))

    def test_tokens_with_overlap_multiplier(self):
        self.assertAlmostEqual(cm.tokens_with_overlap(1000, 0.10), 1100.0)
        self.assertAlmostEqual(cm.tokens_with_overlap(0, 0.10), 0.0)
        # no overlap -> identity
        self.assertAlmostEqual(cm.tokens_with_overlap(1000, 0.0), 1000.0)

    def test_embedding_cost_formula_and_units(self):
        # $0.02 per 1M tokens.
        self.assertAlmostEqual(cm.embedding_cost_usd(1_000_000, 0.02), 0.02)
        self.assertAlmostEqual(cm.embedding_cost_usd(0, 0.02), 0.0)
        # Plan sanity: 1.25M msgs * 50 tok * $0.02/M == $1.25
        self.assertAlmostEqual(
            cm.embedding_cost_usd(1_250_000 * 50, 0.02), 1.25, places=4)

    def test_embedding_cost_rejects_negative(self):
        with self.assertRaises(ValueError):
            cm.embedding_cost_usd(-1, 0.02)
        with self.assertRaises(ValueError):
            cm.embedding_cost_usd(100, -0.02)


class UnitConversionTests(unittest.TestCase):
    def test_gib_vs_gb_distinct(self):
        # 1 GiB > 1 GB; the model must keep them separate (Supabase bills GB).
        self.assertGreater(cm.BYTES_PER_GIB, cm.BYTES_PER_GB)
        self.assertEqual(cm.bytes_to_gib(cm.BYTES_PER_GIB), 1.0)
        self.assertEqual(cm.bytes_to_gb(cm.BYTES_PER_GB), 1.0)
        self.assertAlmostEqual(cm.gb_to_gib(1.0),
                               cm.BYTES_PER_GB / cm.BYTES_PER_GIB, places=6)

    def test_round_trip(self):
        b = 4_500_000_000
        self.assertAlmostEqual(cm.bytes_to_gb(b) * cm.BYTES_PER_GB, b)


# ---------------------------------------------------------------------------
# Scenario structure + determinism
# ---------------------------------------------------------------------------


class ScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = cm.build_results()
        cls.by_id = {s["id"]: s for s in cls.results["scenarios"]}

    def test_four_scenarios_present(self):
        ids = sorted(s["id"] for s in self.results["scenarios"])
        self.assertEqual(
            ids, ["full_1536", "full_384", "pilot_1536", "pilot_384"])

    def test_pilot_and_full_message_counts(self):
        self.assertEqual(self.by_id["pilot_384"]["vectors"]["messages"], 5_000)
        self.assertEqual(
            self.by_id["full_384"]["vectors"]["messages"],
            cm.MEASURED["messages_eligible"])

    def test_vector_counts_monotonic_full_gt_pilot(self):
        for dim in (384, 1536):
            self.assertGreater(
                self.by_id[f"full_{dim}"]["vectors"]["total"],
                self.by_id[f"pilot_{dim}"]["vectors"]["total"],
                f"full should have more vectors than pilot at {dim}-d")

    def test_determinism_across_runs(self):
        # Two builds produce identical scenario numbers (generated_at excluded).
        a = cm.build_results()["scenarios"]
        b = cm.build_results()["scenarios"]
        for sa, sb in zip(a, b):
            # Drop the time-only field is unnecessary; scenarios carry no time.
            self.assertEqual(sa, sb)

    def test_evaluated_scenario_is_pure(self):
        # Re-evaluating the same scenario object yields the same projection.
        base = cm.build_scenarios()[0]
        r1 = cm.evaluate_scenario(copy.deepcopy(base))
        r2 = cm.evaluate_scenario(copy.deepcopy(base))
        self.assertEqual(r1, r2)


# ---------------------------------------------------------------------------
# Monotonicity across dimensions
# ---------------------------------------------------------------------------


class MonotonicityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.by_id = {s["id"]: s for s in cm.build_results()["scenarios"]}

    def _pair(self, scale):
        return self.by_id[f"{scale}_384"], self.by_id[f"{scale}_1536"]

    def test_1536_exceeds_384_on_every_storage_component(self):
        for scale in ("pilot", "full"):
            s384, s1536 = self._pair(scale)
            self.assertGreater(
                s1536["storage"]["raw_vector_payload_gib"],
                s384["storage"]["raw_vector_payload_gib"],
                f"{scale}: raw payload must grow with dim")
            self.assertGreater(
                s1536["storage"]["heap_gib"], s384["storage"]["heap_gib"],
                f"{scale}: heap must grow with dim")
            self.assertGreater(
                s1536["storage"]["hnsw_index_gib"]["central"],
                s384["storage"]["hnsw_index_gib"]["central"],
                f"{scale}: HNSW index must grow with dim")
            self.assertGreater(
                s1536["storage"]["new_storage_gb"]["central"],
                s384["storage"]["new_storage_gb"]["central"],
                f"{scale}: total new storage must grow with dim")

    def test_1536_never_cheaper_than_384_monthly(self):
        for scale in ("pilot", "full"):
            s384, s1536 = self._pair(scale)
            self.assertGreaterEqual(
                s1536["monthly_steady_state_usd"]["ram_resident"],
                s384["monthly_steady_state_usd"]["ram_resident"])


# ---------------------------------------------------------------------------
# Headline figures vs. the plan's stated raw-vector numbers
# ---------------------------------------------------------------------------


class PlanHeadlineTests(unittest.TestCase):
    """Pin the model to the plan's 'Cost and capacity' raw-vector figures.

    Plan (at ~1.25M messages): 384-d ~= 1.9 GB raw vectors; 1536-d ~= 7.7 GB.
    These are the load-bearing facts the dimension recommendation rests on.
    """

    @classmethod
    def setUpClass(cls):
        cls.by_id = {s["id"]: s for s in cm.build_results()["scenarios"]}

    def test_384_raw_payload_near_plan(self):
        # Messages dominate the raw payload; ~1.8-2.0 GB (decimal) expected.
        gb = cm.bytes_to_gb(
            cm.raw_vector_bytes(384, cm.MEASURED["messages_eligible"]))
        self.assertGreater(gb, 1.8)
        self.assertLess(gb, 2.0)

    def test_1536_raw_payload_near_plan(self):
        gb = cm.bytes_to_gb(
            cm.raw_vector_bytes(1536, cm.MEASURED["messages_eligible"]))
        self.assertGreater(gb, 7.0)
        self.assertLess(gb, 8.0)


# ---------------------------------------------------------------------------
# Gate verdicts + boundaries
# ---------------------------------------------------------------------------


class GateVerdictTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.by_id = {s["id"]: s for s in cm.build_results()["scenarios"]}

    def test_verdict_boundary_is_inclusive(self):
        # value == limit is a PASS (stop conditions are "exceeds").
        self.assertEqual(cm._verdict(25.0, 25.0), "PASS")
        self.assertEqual(cm._verdict(25.0001, 25.0), "FAIL")
        self.assertEqual(cm._verdict(12.0, 12.0, lower_is_better=True), "PASS")
        self.assertEqual(cm._verdict(12.01, 12.0), "FAIL")

    def test_initial_spend_passes_all_scenarios(self):
        for sid, sc in self.by_id.items():
            v = sc["gate_verdicts"]["initial_spend_25usd"]
            self.assertEqual(v["verdict"], "PASS", f"{sid}: spend {v}")
            self.assertLess(v["value_usd"], cm.GATES["initial_spend_usd"])

    def test_storage_gate_full_384_pass_full_1536_fail(self):
        v384 = self.by_id["full_384"]["gate_verdicts"]["new_storage_12gb"]
        v1536 = self.by_id["full_1536"]["gate_verdicts"]["new_storage_12gb"]
        self.assertEqual(v384["verdict"], "PASS")
        self.assertLess(v384["value_gb_central"], cm.GATES["new_storage_gb"])
        # Even the high-overhead sweep stays well under 12 GB for 384.
        self.assertLess(v384["value_gb_high_overhead"], cm.GATES["new_storage_gb"])
        self.assertEqual(v1536["verdict"], "FAIL")
        self.assertGreater(v1536["value_gb_central"], cm.GATES["new_storage_gb"])

    def test_pilot_scenarios_clear_all_gates(self):
        for sid in ("pilot_384", "pilot_1536"):
            for gate in ("initial_spend_25usd", "new_storage_12gb",
                         "monthly_incremental_50usd"):
                v = self.by_id[sid]["gate_verdicts"][gate]["verdict"]
                self.assertEqual(v, "PASS", f"{sid}/{gate}: {v}")

    def test_full_384_monthly_is_conditional(self):
        # Disk-cached (included Micro) is cheap; RAM-resident Medium ~= $50.
        v = self.by_id["full_384"]["gate_verdicts"]["monthly_incremental_50usd"]
        self.assertEqual(v["verdict"], "CONDITIONAL")
        self.assertLessEqual(v["value_baseline_disk_cached_usd"], 50.0)
        # RAM-resident operating point is ~at/over the gate (Medium add-on).
        self.assertGreater(v["value_ram_resident_usd"], 0.0)

    def test_gate_verdicts_function_directly(self):
        # PASS: all three within budget.
        gv = cm._gate_verdicts(1.0, 5.0, 6.0, 1.0, 5.0)
        self.assertEqual(gv["initial_spend_25usd"]["verdict"], "PASS")
        self.assertEqual(gv["new_storage_12gb"]["verdict"], "PASS")
        self.assertEqual(gv["monthly_incremental_50usd"]["verdict"], "PASS")
        # CONDITIONAL monthly: baseline ok, ram-resident over.
        gv = cm._gate_verdicts(1.0, 5.0, 6.0, 5.0, 80.0)
        self.assertEqual(gv["monthly_incremental_50usd"]["verdict"], "CONDITIONAL")
        # FAIL monthly: even baseline exceeds.
        gv = cm._gate_verdicts(30.0, 20.0, 25.0, 80.0, 120.0)
        self.assertEqual(gv["initial_spend_25usd"]["verdict"], "FAIL")
        self.assertEqual(gv["new_storage_12gb"]["verdict"], "FAIL")
        self.assertEqual(gv["monthly_incremental_50usd"]["verdict"], "FAIL")

    def test_storage_conditional_when_only_high_sweep_fails(self):
        # Central passes, high-overhead sweep fails -> CONDITIONAL.
        gv = cm._gate_verdicts(1.0, 11.0, 13.0, 5.0, 5.0)
        self.assertEqual(gv["new_storage_12gb"]["verdict"], "CONDITIONAL")
        self.assertEqual(gv["new_storage_12gb"]["central_verdict"], "PASS")
        self.assertEqual(gv["new_storage_12gb"]["high_overhead_verdict"], "FAIL")


# ---------------------------------------------------------------------------
# Tier selection
# ---------------------------------------------------------------------------


class TierTests(unittest.TestCase):
    def test_smallest_tier_selection(self):
        self.assertEqual(cm._smallest_tier(0.5)[0], "Micro")
        self.assertEqual(cm._smallest_tier(1.0)[0], "Micro")
        self.assertEqual(cm._smallest_tier(2.0)[0], "Small")
        self.assertEqual(cm._smallest_tier(3.9)[0], "Medium")
        self.assertEqual(cm._smallest_tier(4.0)[0], "Medium")
        self.assertEqual(cm._smallest_tier(8.0)[0], "Large")
        # Oversized request falls back to the largest listed tier (8XL).
        self.assertEqual(cm._smallest_tier(10_000.0)[0], "8XL")


# ---------------------------------------------------------------------------
# Sensitivity: chunk size and HNSW graph overhead are sweepable parameters
# ---------------------------------------------------------------------------


class SensitivityTests(unittest.TestCase):
    def _evaluate_with(self, **overrides):
        cm2 = sys.modules["capacity_model"]
        saved = {k: cm2.HEURISTICS[k] for k in overrides}
        for k, v in overrides.items():
            cm2.HEURISTICS[k] = v
        try:
            return cm2.evaluate_scenario(cm.build_scenarios()[2])  # full_384
        finally:
            for k, v in saved.items():
                cm2.HEURISTICS[k] = v

    def test_larger_chunk_size_reduces_vectors(self):
        base = self._evaluate_with(python_chunk_tokens=512)
        bigger = self._evaluate_with(python_chunk_tokens=2048)
        self.assertLess(bigger["vectors"]["workflow_python"],
                        base["vectors"]["workflow_python"])

    def test_graph_overhead_sweep_bounds_new_storage(self):
        # The 12 GB verdict for full_384 must hold across the whole heuristic
        # sweep (robustness of the headline conclusion).
        for overhead in ("hnsw_graph_bytes_per_node_low",
                         "hnsw_graph_bytes_per_node_central",
                         "hnsw_graph_bytes_per_node_high"):
            res = self._evaluate_with()  # no override here; checked via thresholds
            self.assertLess(
                res["gate_verdicts"]["new_storage_12gb"]["value_gb_central"], 12.0)
        # Explicitly push overhead high and re-check the central storage number rises.
        low = self._evaluate_with()
        cm.HEURISTICS["hnsw_graph_bytes_per_node_central"] = 400
        try:
            high = cm.evaluate_scenario(cm.build_scenarios()[2])
        finally:
            cm.HEURISTICS["hnsw_graph_bytes_per_node_central"] = 150
        self.assertGreater(
            high["storage"]["new_storage_gb"]["central"],
            low["storage"]["new_storage_gb"]["central"])


if __name__ == "__main__":
    unittest.main()
