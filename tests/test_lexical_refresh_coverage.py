"""Unit tests for the live lexical refresh coverage verifier (task A fix).

The bug: ``verify_coverage`` compared ``python_distinct_items`` (distinct
workflow_python item_ids in ``lexical_documents``) against
``by_public_state.safe`` — which counts EVERY safe state row, including the
intentionally-UNAVAILABLE workflows that correctly produce ZERO docs. Chunks
are written only for ``public_state='safe' AND available AND cohort IN
(payload_python, body_python, recoverable)``. So ``ok`` was always false.

These tests pin the fix at two layers:

1. **Pure logic** (:func:`evaluate_coverage_ok`): no DB, no I/O. Drives the
   decision across the real 221-vs-2,756 scenario and every failing-check path.
2. **Shape**: the coverage SQL string carries every counter name the pure
   function reads, so the SQL and the decision stay in sync.
3. **PG-gated** (``@skipUnless``): on an isolated throwaway cluster, applies
   schema/003, seeds a tiny safe+available workflow + an unavailable-safe
   workflow, runs the coverage SQL, and asserts ``ok``. Mirrors
   ``tests/test_lexical_candidate_sql.py``'s pattern.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

# Import the live refresh module (the module name starts with a digit-free path
# but lives under scripts/; load it by file path to avoid any package clash).
_SPEC = importlib.util.spec_from_file_location(
    "live_lexical_refresh", REPO / "scripts" / "live_lexical_refresh.py"
)
assert _SPEC is not None and _SPEC.loader is not None
LLR = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(LLR)

evaluate_coverage_ok = LLR.evaluate_coverage_ok
COVERAGE_COUNTER_NAMES = LLR.COVERAGE_COUNTER_NAMES


def _base_ok_cov() -> dict:
    """A coverage dict that PASSES every check (the idempotent rerun shape).

    Models the real 221-vs-2,756 scenario at miniature scale: 3 workflows total,
    1 safe+available (indexed, 1 distinct item), 2 unavailable-safe (zero docs).
    """
    return {
        "workflows_total": 3,
        "state_rows": 3,
        "by_cohort": {"payload_python": 1, "unavailable": 2},
        "by_public_state": {"safe": 3},
        "python_chunks": 1,
        "python_distinct_items": 1,
        "quarantined_docs": 0,
        "body_duplicate_state_rows": 0,
        "unrefreshed_workflows": 0,
        "safe_available_python_rows": 1,
        "unavailable_safe_rows": 2,
        "quarantined_rows": 0,
        "chunk_count_mismatches": 0,
        "safe_plus_quarantined_minus_unavailable_safe": 3,
        # duplicate_chunk_hashes_within_item is emitted by a separate query and
        # may be a string ("0") from SQL; the pure function normalizes via int().
        "duplicate_chunk_hashes_within_item": "0",
    }


def _big_ok_cov() -> dict:
    """The exact scenario from the bug report, now expected to PASS.

    2,757 workflows; 221 safe+available python (indexed), 2,535 unavailable-safe
    (zero docs), 1 quarantined. ``by_public_state.safe`` = 2,756 (every safe
    state row, available OR not) — the old code compared distinct_items against
    this and always failed.
    """
    return {
        "workflows_total": 2757,
        "state_rows": 2757,
        "by_cohort": {"payload_python": 221, "unavailable": 2535},
        "by_public_state": {"safe": 2756, "quarantined": 1},
        "python_chunks": 221,
        "python_distinct_items": 221,
        "quarantined_docs": 0,
        "body_duplicate_state_rows": 0,
        "unrefreshed_workflows": 0,
        "safe_available_python_rows": 221,
        "unavailable_safe_rows": 2535,
        "quarantined_rows": 1,
        "chunk_count_mismatches": 0,
        "safe_plus_quarantined_minus_unavailable_safe": 2757,  # 2756 + 1
        "duplicate_chunk_hashes_within_item": "0",
    }


class TestEvaluateCoverageOk(unittest.TestCase):
    # ------------------------------------------------------------------
    # The fix: the bug-report scenario now PASSES.
    # ------------------------------------------------------------------
    def test_ok_true_with_unavailable_safe_rows_present(self) -> None:
        cov = _big_ok_cov()
        ok, reasons = evaluate_coverage_ok(cov)
        self.assertTrue(ok, f"expected ok True for the 221-vs-2756 scenario; reasons={reasons}")
        self.assertEqual(reasons, [])

    def test_ok_true_minimal_safe_available(self) -> None:
        ok, reasons = evaluate_coverage_ok(_base_ok_cov())
        self.assertTrue(ok, reasons)
        self.assertEqual(reasons, [])

    # ------------------------------------------------------------------
    # Each failing check is named with actual vs expected.
    # ------------------------------------------------------------------
    def test_ok_false_when_distinct_items_drift(self) -> None:
        cov = _base_ok_cov()
        cov["python_distinct_items"] = 0  # indexed drifted below safe_available
        cov["safe_available_python_rows"] = 1
        ok, reasons = evaluate_coverage_ok(cov)
        self.assertFalse(ok)
        joined = " | ".join(reasons)
        self.assertIn("python_distinct_items=0", joined)
        self.assertIn("safe_available_python_rows=1", joined)

    def test_ok_false_when_distinct_items_above_safe_available(self) -> None:
        cov = _base_ok_cov()
        cov["python_distinct_items"] = 2
        cov["safe_available_python_rows"] = 1
        ok, reasons = evaluate_coverage_ok(cov)
        self.assertFalse(ok)
        self.assertTrue(any("python_distinct_items=2" in r for r in reasons))

    def test_ok_false_on_quarantined_docs(self) -> None:
        cov = _base_ok_cov()
        cov["quarantined_docs"] = 1
        ok, reasons = evaluate_coverage_ok(cov)
        self.assertFalse(ok)
        self.assertTrue(any("quarantined_docs=1" in r for r in reasons))

    def test_ok_false_on_dup_chunk_hash(self) -> None:
        cov = _base_ok_cov()
        cov["duplicate_chunk_hashes_within_item"] = "2"  # string from SQL
        ok, reasons = evaluate_coverage_ok(cov)
        self.assertFalse(ok)
        self.assertTrue(any("duplicate_chunk_hashes_within_item=2" in r for r in reasons))

    def test_ok_false_on_unrefreshed(self) -> None:
        cov = _base_ok_cov()
        cov["unrefreshed_workflows"] = 4
        ok, reasons = evaluate_coverage_ok(cov)
        self.assertFalse(ok)
        self.assertTrue(any("unrefreshed_workflows=4" in r for r in reasons))

    def test_ok_false_on_state_total_mismatch(self) -> None:
        cov = _base_ok_cov()
        cov["state_rows"] = 5  # workflows_total stays 3
        # The partition check must also fail when state_rows drifts, but the
        # state-total check itself must fire and name the mismatch.
        ok, reasons = evaluate_coverage_ok(cov)
        self.assertFalse(ok)
        self.assertTrue(any("state_rows=5" in r and "workflows_total=3" in r for r in reasons))

    def test_ok_false_on_chunk_count_mismatch(self) -> None:
        cov = _base_ok_cov()
        cov["chunk_count_mismatches"] = 7
        ok, reasons = evaluate_coverage_ok(cov)
        self.assertFalse(ok)
        self.assertTrue(any("chunk_count_mismatches=7" in r for r in reasons))

    def test_ok_false_on_partition_sum_mismatch(self) -> None:
        cov = _base_ok_cov()
        cov["safe_plus_quarantined_minus_unavailable_safe"] = 2  # state_rows is 3
        ok, reasons = evaluate_coverage_ok(cov)
        self.assertFalse(ok)
        joined = " | ".join(reasons)
        self.assertIn("safe_plus_quarantined_minus_unavailable_safe=2", joined)
        self.assertIn("state_rows=3", joined)

    # ------------------------------------------------------------------
    # Idempotent rerun: all derived counts equal their stored counterparts.
    # ------------------------------------------------------------------
    def test_hash_skip_no_churn(self) -> None:
        """A fully-fresh rerun where every stored count matches the derived one.

        Models the hash-skip path: nothing was rewritten, so every counter is
        internally consistent and ok is True (no churn => coverage still green).
        """
        cov = _base_ok_cov()
        # Stored chunk_count matches actual doc counts (chunk_count_mismatches=0),
        # distinct indexed items == safe+available rows, partition sums — all hold.
        ok, reasons = evaluate_coverage_ok(cov)
        self.assertTrue(ok, reasons)

    def test_string_counters_normalized(self) -> None:
        """Every numeric counter may arrive as a string from SQL; int-normalize."""
        cov = _base_ok_cov()
        for k in (
            "workflows_total", "state_rows", "python_distinct_items",
            "safe_available_python_rows", "quarantined_docs",
            "unrefreshed_workflows", "chunk_count_mismatches",
            "safe_plus_quarantined_minus_unavailable_safe",
        ):
            cov[k] = str(cov[k])
        ok, reasons = evaluate_coverage_ok(cov)
        self.assertTrue(ok, reasons)

    def test_old_bug_comparison_would_fail(self) -> None:
        """Sanity: the OLD comparison (distinct vs by_public_state.safe) fails
        on the fix scenario, proving the bug is real and the fix changes behavior."""
        cov = _big_ok_cov()
        old_ok = cov["python_distinct_items"] == cov["by_public_state"]["safe"]
        self.assertFalse(old_ok, "the old comparison must be false here (221 != 2756)")


class TestCoverageSqlShape(unittest.TestCase):
    """The coverage SQL string must contain every counter the pure function reads."""

    def test_coverage_sql_contains_counters(self) -> None:
        # Re-read the SQL from the module source so we test the actual string,
        # not a paraphrase. Inspect verify_coverage's source via the module.
        import inspect

        src = inspect.getsource(LLR.verify_coverage)
        for name in COVERAGE_COUNTER_NAMES:
            self.assertIn(name, src, f"coverage SQL is missing counter '{name}'")

    def test_evaluate_consumes_counters(self) -> None:
        """evaluate_coverage_ok must read the counters verify_coverage emits."""
        import inspect

        src = inspect.getsource(evaluate_coverage_ok)
        # Every counter that drives a decision must be referenced by name.
        for name in (
            "workflows_total", "state_rows", "unrefreshed_workflows",
            "quarantined_docs", "duplicate_chunk_hashes_within_item",
            "python_distinct_items", "safe_available_python_rows",
            "chunk_count_mismatches", "safe_plus_quarantined_minus_unavailable_safe",
        ):
            self.assertIn(name, src, f"evaluate_coverage_ok does not read '{name}'")


# ---------------------------------------------------------------------------
# PG-gated test: isolated throwaway cluster (mirrors test_lexical_candidate_sql).
# Skipped entirely when PostgreSQL binaries are absent.
# ---------------------------------------------------------------------------

try:  # pragma: no cover - import guard
    from lexical_pg import LocalCluster, find_pgbins  # type: ignore
    _HAS_PG = find_pgbins() is not None
except Exception:  # noqa: BLE001
    _HAS_PG = False


@unittest.skipUnless(_HAS_PG, "PostgreSQL binaries (initdb/pg_ctl/psql) not found")
class TestCoverageSqlOnCluster(unittest.TestCase):  # pragma: no cover - PG-gated
    @classmethod
    def setUpClass(cls) -> None:
        from executors import lexical_documents as LD
        from lexical_pg import (  # type: ignore
            BOOTSTRAP_SQL, SCHEMA_003, q, q_array, q_jsonb,
        )
        cls.LD = LD
        cls.cluster = LocalCluster.start()
        try:
            cls.cluster.psql(BOOTSTRAP_SQL, capture=False)
            cls.cluster.psql_file(SCHEMA_003)
            cls._seed(cls)
        except Exception:
            cls.cluster.tear_down()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.cluster.tear_down()

    def _seed(self) -> None:
        from lexical_pg import q, q_array, q_jsonb  # type: ignore

        LD = self.LD
        # Two workflows: one safe+available (payload python => 1 doc), one
        # unavailable-safe (no python recoverable => zero docs, but a safe state
        # row). This is the miniature of the 221-vs-2535 production shape.
        sample_py = (
            "import torch\n"
            "class WanVideoSampler:\n"
            "    def __init__(self, lora_weight=0.8, num_frames=81):\n"
            "        self.lora_weight = lora_weight\n"
        )
        rows = [
            {"id": 1, "kind": "workflow", "title": "Payload Python",
             "body": "Description only.", "payload": {"python_source": sample_py},
             "metadata": {}},
            {"id": 2, "kind": "workflow", "title": "Unavailable No Python",
             "body": "Prose-only workflow, no code block.",
             "payload": {}, "metadata": {}},
        ]
        res_sql = ["begin;"]
        for r in rows:
            res_sql.append(
                "insert into external_resources "
                "(id, kind, source, title, body, metadata, payload) values "
                f"({r['id']}, {q(r['kind'])}, 'test', {q(r['title'])}, {q(r['body'])}, "
                f"{q_jsonb(r['metadata'])}, {q_jsonb(r['payload'])});"
            )
        res_sql.append("commit;")
        self.cluster.psql("\n".join(res_sql), capture=False)

        # Compute + insert state + docs via the frozen bridge (same as the refresh).
        doc_cols = LLR.DOC_COLS
        state_cols = LLR.STATE_COLS
        doc_rows: list[str] = []
        state_rows: list[str] = []
        for r in rows:
            state, docs = LD.compute_workflow_python_documents(dict(r))
            for d in docs:
                doc_rows.append(
                    f"({q(d.entity_type)},{q(d.item_id)},{q(d.representation_type)},"
                    f"{d.chunk_index},{q(d.chunk_text)},{q(d.matched_anchor)},"
                    f"{d.source_offset_start},{d.source_offset_end},"
                    f"{q(d.representation_hash)},{q(d.chunk_hash)},"
                    f"{q(d.quarantine_state)},{d.lexicalization_version},"
                    f"{d.canonicalization_version},{d.chunking_version},"
                    f"{d.secret_scan_version},{q(d.method)})"
                )
            state_rows.append(
                f"({state.resource_id},{q(state.kind)},{q(state.cohort)},"
                f"{q(state.public_state)},{q(state.available)},{q(state.body_duplicate)},"
                f"{q(state.delimiter)},{q(state.derivation)},{q(state.representation_hash)},"
                f"{q_array(state.secret_reason_codes)},{state.canonicalization_version},"
                f"{state.secret_scan_version},{state.chunking_version},{state.chunk_count})"
            )
        if doc_rows:
            self.cluster.psql(
                "insert into lexical_documents (" + ",".join(doc_cols) + ") values "
                + ",".join(doc_rows) + ";",
                capture=False,
            )
        self.cluster.psql(
            "insert into lexical_resource_python_state (" + ",".join(state_cols) + ") values "
            + ",".join(state_rows) + ";",
            capture=False,
        )

    def _run_coverage_sql(self) -> dict:
        """Run the SAME SQL verify_coverage runs, against the throwaway cluster."""
        import inspect
        import re

        # Extract the first SQL string literal from verify_coverage and run it.
        src = inspect.getsource(LLR.verify_coverage)
        m = re.search(r'r\s*=\s*psql_retry\(cred,\s*elevate\("""(.*?)"""\)', src, re.DOTALL)
        self.assertIsNotNone(m, "could not extract coverage SQL from verify_coverage")
        sql = m.group(1)
        rc, out = self.cluster.psql(sql)
        self.assertEqual(rc, 0, f"coverage SQL failed: {out}")
        cov = json.loads(out.strip())

        # The duplicate-hash query (also inlined in verify_coverage).
        rc2, out2 = self.cluster.psql(
            "select count(*)::text from (select item_id, chunk_hash, count(*) c "
            "from lexical_documents where representation_type='workflow_python' "
            "group by item_id, chunk_hash having count(*)>1) z;"
        )
        self.assertEqual(rc2, 0, f"dup-hash SQL failed: {out2}")
        cov["duplicate_chunk_hashes_within_item"] = (out2 or "").strip() or "?"
        return cov

    def test_coverage_ok_on_cluster(self) -> None:
        cov = self._run_coverage_sql()
        ok, reasons = evaluate_coverage_ok(cov)
        self.assertTrue(ok, f"expected ok on cluster; cov={cov} reasons={reasons}")
        # The unavailable-safe workflow (id 2) produces zero docs but is safe.
        self.assertEqual(cov["safe_available_python_rows"], 1)
        self.assertEqual(cov["python_distinct_items"], 1)
        self.assertEqual(cov["unavailable_safe_rows"], 1)
        self.assertEqual(cov["state_rows"], 2)
        self.assertEqual(cov["workflows_total"], 2)

    def test_coverage_detects_missing_state_row(self) -> None:
        # Delete one state row => unrefreshed + state_rows != workflows_total.
        self.cluster.psql(
            "delete from lexical_resource_python_state where resource_id=2;",
            capture=False,
        )
        try:
            cov = self._run_coverage_sql()
            ok, reasons = evaluate_coverage_ok(cov)
            self.assertFalse(ok)
            joined = " | ".join(reasons)
            self.assertIn("unrefreshed_workflows", joined)
            self.assertIn("state_rows", joined)
        finally:
            # Restore the row so the cluster is left consistent for siblings.
            from lexical_pg import q, q_array  # type: ignore
            # Recompute the state for row 2 from the bridge.
            row = {"id": 2, "kind": "workflow", "title": "Unavailable No Python",
                   "body": "Prose-only workflow, no code block.", "payload": {}, "metadata": {}}
            state, _docs = self.LD.compute_workflow_python_documents(dict(row))
            self.cluster.psql(
                "insert into lexical_resource_python_state (" + ",".join(LLR.STATE_COLS) + ") values "
                f"({state.resource_id},{q(state.kind)},{q(state.cohort)},"
                f"{q(state.public_state)},{q(state.available)},{q(state.body_duplicate)},"
                f"{q(state.delimiter)},{q(state.derivation)},{q(state.representation_hash)},"
                f"{q_array(state.secret_reason_codes)},{state.canonicalization_version},"
                f"{state.secret_scan_version},{state.chunking_version},{state.chunk_count});",
                capture=False,
            )


if __name__ == "__main__":
    unittest.main()
