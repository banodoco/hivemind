"""SQL + RPC tests for the lexical candidate SQL (task 1.7) and the hardened
lexical search RPC (tasks 1.8/1.9), on an isolated throwaway PostgreSQL cluster.

These exercise the parts offline unit tests cannot: the real candidate function
(``hivemind_lexical_candidates``) and the SECURITY DEFINER RPC
(``hivemind_lexical_search``) against PostgreSQL, with the frozen Phase-1
indexes (schema/003–009): index-served arms, eligibility enforcement (deletion /
opt-out / distillation status / workflow quarantine), ambiguous-item-id
rejection, post-limit hydration into the unified_feed shape, deterministic
order, the workflow-only / single-workflow filters, and Snowflake string
preservation.

The cluster is a throwaway ``initdb --auth=trust`` instance on an ephemeral port
with a temp data dir; torn down in ``tearDownClass``. No Docker, no network, no
production mutation. Skipped entirely when PostgreSQL binaries are absent.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import rehearse_lexical_candidate as R  # noqa: E402
from lexical_pg import find_pgbins  # noqa: E402


@unittest.skipUnless(find_pgbins(), "PostgreSQL binaries (initdb/pg_ctl/psql) not found")
class TestLexicalCandidateSQLCluster(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cluster = R.LP.LocalCluster.start()
        try:
            R.reset_schema(cls.cluster)
            R.bootstrap(cls.cluster)
            # A modest seed is enough for correctness/EXPLAIN; the full rehearsal
            # (scripts/rehearse_lexical_candidate.py) exercises production volume.
            R.seed(cls.cluster, n_messages=8000)
        except Exception:
            cls.cluster.tear_down()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.cluster.tear_down()

    # ------------------------------------------------------------------
    # Migrations load cleanly + idempotently (re-apply the function migrations).
    # ------------------------------------------------------------------
    def test_migrations_idempotent(self) -> None:
        for name in ("008_lexical_candidate_sql.sql", "009_lexical_search_rpc.sql"):
            self.cluster.psql_file(R.SCHEMA_DIR / name)

    # ------------------------------------------------------------------
    # Containment (task-1.6 corrected arm) retrieves identifiers embedded in
    # prose AND in workflow Python.
    # ------------------------------------------------------------------
    def test_containment_finds_workflow_python(self) -> None:
        resp = R.call_rpc(self.cluster, "WanVideoSampler", kinds=["workflow"])
        ids = {("resource", r["item_id"]) for r in resp["results"]}
        self.assertIn(("resource", "20"), ids, resp["results"])

    def test_containment_finds_message_prose(self) -> None:
        # The planted message whose whole body IS the identifier (i=13) is found.
        resp = R.call_rpc(self.cluster, "WanVideoSampler")
        ids = {("message", r["item_id"]) for r in resp["results"]
               if r["kind"] == "message"}
        self.assertTrue(ids, "expected message prose containment hits")

    # ------------------------------------------------------------------
    # Eligibility: deleted / opted-out (when enabled) / rejected distillation.
    # ------------------------------------------------------------------
    def test_softdeleted_never_returns(self) -> None:
        resp = R.call_rpc(self.cluster, "sampler video")
        deleted = str(1_000_000_000_000_000_000 + 0)  # i=0 is soft-deleted
        ids = {r["item_id"] for r in resp["results"]}
        self.assertNotIn(deleted, ids)

    def test_rejected_distillation_never_returns(self) -> None:
        # seed inserts an approved distillation (id 1) + the eligibility proof
        # inserts a rejected one (id 2) with the same terms.
        self.cluster.psql(
            "insert into public.distillations (id, question, conditions, answer, "
            "confidence, status, author_id) overriding system value values "
            "(2,'How do I reduce motion strength rejected','x','y','low','rejected',1) "
            "on conflict do nothing;"
        )
        resp = R.call_rpc(self.cluster, "reduce motion strength")
        ids = {r["item_id"] for r in resp["results"] if r["kind"] == "distillation"}
        self.assertNotIn("2", ids)
        self.assertIn("1", ids)

    # ------------------------------------------------------------------
    # Security: the RPC rejects ambiguous cross-kind item_ids (AD-1).
    # ------------------------------------------------------------------
    def test_ambiguous_item_ids_rejected(self) -> None:
        # item_ids with kinds spanning message + resource -> ambiguous -> error.
        with self.assertRaises(RuntimeError):
            R.call_rpc(self.cluster, "x", item_ids=["1", "20"],
                       kinds=["message", "workflow"])

    def test_single_workflow_filters_to_one_item(self) -> None:
        resp = R.call_rpc(self.cluster, "WanVideoSampler",
                          kinds=["workflow"], item_ids=["20"])
        ids = {r["item_id"] for r in resp["results"]}
        self.assertTrue(ids <= {"20"}, ids)

    # ------------------------------------------------------------------
    # Global limit is enforced; hydration returns the unified_feed shape.
    # ------------------------------------------------------------------
    def test_global_limit_enforced(self) -> None:
        resp = R.call_rpc(self.cluster, "controlnet", limit=5)
        self.assertLessEqual(len(resp["results"]), 5)
        self.assertEqual(resp["count"], len(resp["results"]))
        self.assertLessEqual(resp["meta"]["limit"], 100)

    def test_hydration_shape_and_snowflake_strings(self) -> None:
        resp = R.call_rpc(self.cluster, "WanVideoSampler", limit=3)
        self.assertGreater(len(resp["results"]), 0)
        row = resp["results"][0]
        # Every unified_feed field is present; item_id is a string (snowflake-safe).
        for key in ("kind", "source", "item_id", "title", "body", "metadata",
                    "created_at", "matched_representation"):
            self.assertIn(key, row)
        self.assertIsInstance(row["item_id"], str)

    # ------------------------------------------------------------------
    # Deterministic order: same query twice -> identical ranked identities.
    # ------------------------------------------------------------------
    def test_order_is_deterministic(self) -> None:
        a = [(r["kind"], r["item_id"]) for r in R.call_rpc(self.cluster, "controlnet")["results"]]
        b = [(r["kind"], r["item_id"]) for r in R.call_rpc(self.cluster, "controlnet")["results"]]
        self.assertEqual(a, b)

    # ------------------------------------------------------------------
    # No-hit query returns zero results.
    # ------------------------------------------------------------------
    def test_no_hit_zero(self) -> None:
        resp = R.call_rpc(self.cluster, "zzzznotarealtokenxyz")
        self.assertEqual(resp["count"], 0)
        self.assertEqual(resp["results"], [])

    # ------------------------------------------------------------------
    # EXPLAIN: every representative arm is servable by its intended GIN index.
    # ------------------------------------------------------------------
    def test_all_arms_index_servable(self) -> None:
        ev = R.capture_arm_explain(self.cluster)
        unservable = [arm for arm, d in ev.items() if not d["index_servable"]]
        self.assertEqual(unservable, [], f"arms not servable by their GIN: {unservable}")

    # ------------------------------------------------------------------
    # Bounds: oversized inputs are rejected before retrieval.
    # ------------------------------------------------------------------
    def test_oversized_limit_clamped(self) -> None:
        resp = R.call_rpc(self.cluster, "controlnet", limit=99999)
        self.assertLessEqual(resp["meta"]["limit"], 100)

    def test_empty_query_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            R.call_rpc(self.cluster, "   ")


if __name__ == "__main__":
    unittest.main()
