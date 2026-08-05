"""Task 2.17 — isolated PostgreSQL/pgvector Phase-2 acceptance (T4/T5/T6).

Wraps scripts/rehearse_phase2_acceptance.rehearse(), which runs the COMPLETE
selected-contract lifecycle on a throwaway local cluster (schema/003 + 020-033 +
034): source/remediation -> manifest -> enqueue -> claim -> payload -> fake embed/
drop -> source-hash-safe finalize -> complete -> semantic candidates, plus
concurrency, crash/lease recovery, and source-change races. Skipped if no local
PostgreSQL binaries. The rehearsal writes the allow-listed evidence JSON; these
tests assert its machine-readable verdicts.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))


def _have_pg() -> bool:
    try:
        import lexical_pg
        return lexical_pg.find_pgbins() is not None
    except Exception:  # noqa: BLE001
        return False


@unittest.skipUnless(_have_pg(), "PostgreSQL binaries (initdb/pg_ctl/psql) not found")
class Phase2AcceptanceSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import rehearse_phase2_acceptance as r
        cls.ev = r.rehearse(r.EVIDENCE_PATH)

    def _check(self, name: str) -> None:
        names = {c["name"]: c["ok"] for c in self.ev["checks"]}
        self.assertIn(name, names, f"missing check {name}")
        self.assertTrue(names[name], f"check {name} failed")

    def test_overall_verdict(self):
        self.assertTrue(self.ev["verdict"], "not all rehearsal checks passed")

    def test_evidence_allow_list_clean(self):
        self.assertTrue(self.ev["evidence_scan"]["ok"], self.ev["evidence_scan"]["problems"])

    def test_selected_contract_from_artifacts(self):
        self.assertEqual(self.ev["selected"]["selected_contract_id"], "1360541028304258884")
        self.assertFalse(self.ev["selected"]["production_activated"])

    def test_selected_literal_applied(self):
        self.assertEqual(self.ev["selected_literal_applied"], "1360541028304258884")

    def test_python_sql_chunk_parity(self):
        self.assertTrue(all(self.ev["parity_python_sql"].values()))

    def test_oversized_line_bounded(self):
        self._check("oversized_line_bounded")

    def test_worker_protocol(self):
        self._check("worker_one_embed_per_safe_python_rep")
        self._check("safe_python_vectors_written")

    def test_quarantined_unavailable_zero_vectors(self):
        self._check("unavailable_zero_python_vectors")
        self._check("quarantined_zero_python_vectors")

    def test_changed_reembed(self):
        self._check("changed_new_marker_stored")
        self._check("changed_old_marker_replaced")

    def test_concurrency(self):
        self._check("concurrency_no_duplicate_processing")
        self._check("concurrency_all_six_converged")

    def test_recovery(self):
        self._check("crash_lease_recovered")
        self._check("crash_lease_reprocessed_terminal")

    def test_source_change(self):
        self._check("source_change_detected")
        self._check("source_change_no_stale_authority")

    def test_wrong_dimension(self):
        self._check("wrong_dimension_rejected")
        self._check("wrong_dimension_no_partial_wipe")

    def test_semantic(self):
        self._check("semantic_later_workflow_python_chunk_can_win")
        self._check("semantic_one_row_per_item")
        self._check("wrong_contract_vector_excluded")

    def test_snowflake(self):
        self._check("snowflake_survives_full_protocol")

    def test_hnsw_index(self):
        self._check("hnsw_index_present")
        self._check("semantic_function_binds_selected_literal")

    def test_no_production_network_provider_action(self):
        self.assertFalse(self.ev["production_mutated"])
        self.assertEqual(self.ev["network_calls"], 0)
        self.assertEqual(self.ev["embedding_provider_calls"], 0)
        self.assertTrue(self.ev["isolated_cluster"])


if __name__ == "__main__":
    unittest.main()
