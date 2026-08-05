"""SQL tests for task 1.2 on an isolated throwaway local PostgreSQL cluster.

These exercise the *storage* layer that offline unit tests cannot: the generated
weighted ``tsvector`` columns, the GIN indexes (proven via ``EXPLAIN``), the
``lexical_documents`` constraints, the workflow-Python precedence/dedup/
quarantine behavior end-to-end against real PostgreSQL, and migration
idempotence. They reuse the shared harness in ``scripts/lexical_pg.py``.

The cluster is a throwaway ``initdb --auth=trust`` instance on an ephemeral port
with a temp data dir; it is torn down in ``tearDownClass``. No Docker, no
network, no production mutation. Skipped entirely when PostgreSQL binaries are
not present on the machine.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import lexical_pg  # noqa: E402


@unittest.skipUnless(lexical_pg.find_pgbins(), "PostgreSQL binaries (initdb/pg_ctl/psql) not found")
class TestLexicalSQLCluster(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cluster = lexical_pg.LocalCluster.start()
        try:
            lexical_pg.seed_cluster(cls.cluster)
        except Exception:
            cls.cluster.tear_down()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.cluster.tear_down()

    def _scalar(self, sql: str) -> str:
        rc, out = self.cluster.psql(sql)
        self.assertEqual(rc, 0, out)
        return out.strip()

    # ------------------------------------------------------------------
    # Full assertion battery (precedence / dedup / quarantine / eligibility /
    # constraints / idempotence) runs against the seeded cluster.
    # ------------------------------------------------------------------
    def test_all_assertions_pass(self) -> None:
        results = lexical_pg.run_assertions(self.cluster)
        failed = [(n, d) for (n, ok, d) in results if not ok]
        self.assertFalse(failed, f"failing assertions: {failed}")

    def test_migration_is_idempotent(self) -> None:
        # Re-applying 003 twice more must not error (if not exists / or replace).
        self.cluster.psql_file(lexical_pg.SCHEMA_003)
        self.cluster.psql_file(lexical_pg.SCHEMA_003)

    # ------------------------------------------------------------------
    # EXPLAIN index usage for the three representative lexical arms.
    # ------------------------------------------------------------------
    def test_explain_uses_gin_indexes(self) -> None:
        ev = lexical_pg.capture_explain_evidence(self.cluster)
        for arm, data in ev.items():
            with self.subTest(arm=arm):
                self.assertIn(
                    data["index_name_expected"], data["plan_forced_index"],
                    f"{arm}: forced plan does not use {data['index_name_expected']}",
                )

    # ------------------------------------------------------------------
    # Safe workflow Python is searchable; quarantined is not.
    # ------------------------------------------------------------------
    def test_safe_workflow_python_symbol_discoverable(self) -> None:
        n = self._scalar(
            "select count(distinct item_id) from lexical_documents "
            "where representation_type='workflow_python' "
            "and tsv @@ websearch_to_tsquery('simple','wanvideosampler')")
        # payload-only, body-only, both, changed, huge all carry the symbol.
        self.assertGreaterEqual(int(n), 3)

    def test_quarantined_python_excluded_from_candidate_query(self) -> None:
        n = self._scalar(
            "select count(*) from lexical_documents where item_id='1008' "
            "and representation_type='workflow_python' "
            "and tsv @@ websearch_to_tsquery('simple','api_key')")
        self.assertEqual(n, "0")

    # ------------------------------------------------------------------
    # No-duplication: code-only symbol indexed once as workflow_python,
    # stripped from prose (the python block is never in the prose vector).
    # ------------------------------------------------------------------
    def test_duplicate_body_payload_indexed_once(self) -> None:
        prose_has = self._scalar(
            "select count(*) from external_resources where id=1003 and "
            "prose_tsv @@ websearch_to_tsquery('simple','num_frames')")
        py_has = self._scalar(
            "select count(*) from lexical_documents where item_id='1003' and "
            "representation_type='workflow_python' "
            "and tsv @@ websearch_to_tsquery('simple','num_frames')")
        self.assertEqual(prose_has, "0", "code-only symbol must be stripped from prose")
        self.assertNotEqual(py_has, "0", "code-only symbol must be in workflow_python docs")

    # ------------------------------------------------------------------
    # Distillation eligibility + weighted ranking.
    # ------------------------------------------------------------------
    def test_distillation_eligibility_and_weighted_rank(self) -> None:
        rej = self._scalar(
            "select count(*) from distillations where status in ('pending','approved') and id=3")
        self.assertEqual(rej, "0")
        top = self._scalar(
            "select id from distillations where lexical_tsv "
            "@@ websearch_to_tsquery('simple','upscale') "
            "order by ts_rank(lexical_tsv, websearch_to_tsquery('simple','upscale'),32) desc "
            "limit 1")
        self.assertEqual(top, "1")  # A-weight (question) outranks C-weight (answer)

    # ------------------------------------------------------------------
    # Constraints enforce the structural invariants.
    # ------------------------------------------------------------------
    def test_constraint_rejects_quarantined_workflow_python(self) -> None:
        rc, _ = self.cluster.psql(
            "insert into lexical_documents "
            "(entity_type,item_id,representation_type,chunk_index,chunk_text,"
            "representation_hash,chunk_hash,quarantine_state) values "
            "('resource','9999','workflow_python',0,'x','h','h','quarantined')")
        self.assertNotEqual(rc, 0)

    def test_workflow_python_state_accessor(self) -> None:
        self.assertEqual(self._scalar("select hivemind_workflow_python_state(1008)"), "quarantined")
        self.assertEqual(self._scalar("select hivemind_workflow_python_state(1007)"), "safe")

    # ------------------------------------------------------------------
    # IMMUTABLE SQL helpers mirror the frozen Python reference (token parity).
    # ------------------------------------------------------------------
    def test_sql_workflow_prose_mirrors_python(self) -> None:
        import json  # noqa: F401
        from executors import workflow_representation as WR
        body = (
            "Intro paragraph about upscaling.\n\n"
            f"{lexical_pg.READY_DELIM}\n{lexical_pg.SAMPLE_PY}"
            "\n\nTrailing prose remains.\n\n"
            "Workflow semantics (rule-based): media=video; task=image_to_video."
        )
        rc, out = self.cluster.psql(
            "select hivemind_workflow_prose($b$" + body + "$b$,'workflow')")
        self.assertEqual(rc, 0, out)
        sql_tokens = set(out.strip().split())
        py_tokens = set(WR.strip_python_blocks(body).split())
        self.assertEqual(sql_tokens, py_tokens, "prose token set differs from Python")
        # The code symbol is stripped in both (no-duplication).
        self.assertNotIn("WanVideoSampler", sql_tokens)
        self.assertNotIn("lora_weight", sql_tokens)

    def test_sql_semantics_text_mirrors_python(self) -> None:
        import json
        from executors import workflow_representation as WR
        meta = lexical_pg.workflow_semantics()
        rc, out = self.cluster.psql(
            "select hivemind_workflow_semantics_text($j$" + json.dumps(meta) + "$j$::jsonb)")
        self.assertEqual(rc, 0, out)
        sql_tokens = set(out.strip().split())
        py_tokens = set(WR.project_semantics(meta).split())
        self.assertEqual(sql_tokens, py_tokens, "semantics token set differs from Python")
        # Raw-cased tokens (lowercasing is to_tsvector('simple',...)'s job, not the
        # helper's): node_types contributes the class; searchable_aliases the version.
        self.assertIn("WanVideoSampler", sql_tokens)  # via node_types
        self.assertIn("wan2.2", sql_tokens)           # via searchable_aliases

    def test_sql_resource_tags_mirrors_expected(self) -> None:
        import json
        meta = {"tags": ["upscaling", "anime video"], "summary": {"tags": ["lora"]}}
        rc, out = self.cluster.psql(
            "select hivemind_resource_tags($j$" + json.dumps(meta) + "$j$::jsonb)")
        self.assertEqual(rc, 0, out)
        tokens = set(out.strip().split())
        # tags + summary.tags leaves, tokenized by simple later; here raw tokens.
        self.assertTrue({"upscaling", "anime", "video", "lora"} <= tokens)


if __name__ == "__main__":
    unittest.main()
