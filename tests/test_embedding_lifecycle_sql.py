"""Discoverable SQL lifecycle test for tasks 2.7–2.10 (plan Phase 2 batch S2).

Wraps scripts/rehearse_embedding_lifecycle.py (an isolated throwaway PostgreSQL
cluster + pgvector) so `python3 -m unittest discover` exercises the embedding
job queue, SKIP LOCKED claim/complete/fail/recover/cancel RPCs, the worker SQL
surface, the end-to-end worker protocol with the deterministic fake embedder,
and the cleanup behavior — exactly the signals rehearsed by the script. Auto-
skips when PostgreSQL binaries or pgvector are unavailable locally (mirrors
tests.test_embedding_schema_sql).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import lexical_pg  # noqa: E402


@unittest.skipUnless(lexical_pg.find_pgbins(), "PostgreSQL binaries not found")
class TestEmbeddingLifecycleSQL(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # The rehearsal manages its own throwaway cluster; if pgvector is not
        # installable locally, schema/020 fails and we skip (no separate probe
        # cluster, which would race the rehearsal for the same free port).
        from rehearse_embedding_lifecycle import rehearse
        try:
            cls.ev = rehearse(REPO / "docs" / "hybrid-search", only=None)
        except RuntimeError as exc:
            msg = str(exc).lower()
            if "vector" in msg or "extension" in msg or "pgvector" in msg:
                raise unittest.SkipTest(f"pgvector unavailable locally: {exc}")
            raise

    def test_verdict_all_pass(self) -> None:
        self.assertTrue(self.ev["verdict"]["all_pass"], self.ev["verdict"])

    def test_2_7_triggers(self) -> None:
        checks = self.ev.get("triggers_2_7", {})
        self.assertTrue(checks, "2.7 trigger checks missing")
        self.assertTrue(all(checks.values()), checks)

    def test_2_8_concurrency_no_double_claim(self) -> None:
        checks = self.ev.get("concurrency_2_8", {})
        self.assertTrue(checks.get("concurrency_no_double_claim"), checks)
        self.assertTrue(checks.get("concurrency_all_processed"), checks)

    def test_2_8_state_machine(self) -> None:
        checks = self.ev.get("state_machine_2_8", {})
        self.assertTrue(checks.get("bounded_retries_then_failed"), checks)
        self.assertTrue(checks.get("stale_lease_recovered"), checks)
        self.assertTrue(checks.get("cancelled_not_claimable"), checks)

    def test_2_9_worker_surface_and_protocol(self) -> None:
        ws = self.ev.get("worker_surface_2_9", {})
        self.assertTrue(ws.get("upsert_atomic_replace"), ws)
        self.assertTrue(ws.get("upsert_rejects_wrong_dimension"), ws)
        proto = self.ev.get("protocol_2_9", {})
        self.assertTrue(proto.get("protocol_job_done"), proto)
        self.assertTrue(proto.get("protocol_vectors_stored"), proto)

    def test_2_10_cleanup_and_safe_replacement(self) -> None:
        cl = self.ev.get("cleanup_2_10", {})
        self.assertTrue(cl.get("ineligible_message_gone"), cl)
        self.assertTrue(cl.get("replacement_switch_ok"), cl)
        self.assertTrue(cl.get("active_index_preserved"), cl)
        self.assertTrue(cl.get("drop_active_contract_refused"), cl)


if __name__ == "__main__":
    unittest.main()
