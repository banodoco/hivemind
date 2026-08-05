"""Task 2.17 focused PURE tests — T1 (selected-contract artifact derivation) and
T2 (canonical manifest + chunker oversized-line / parity). Stdlib-only, offline,
no DB, no provider. The SQL/PG acceptance lives in test_phase2_acceptance_sql.py.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from executors import chunking as ch
from executors import canonical_representations as canon
from executors import entity_identity as ei
from executors import selected_contract as sc
from executors import workflow_representation as wr

SAFE_PY = "import torch\nMARKER_SAFE = 1\nclass C:\n    pass\n"
SECRET_PY = "api_key = 'sk-abcdefghijklmnopqrstuvwxyz0123456789ABCDEF'\n"


class SelectedContractArtifactTests(unittest.TestCase):
    """T1: the selected contract is DERIVED from the accepted 2.14 artifacts and
    fail-closes on every drift."""

    def test_resolves_exactly_one_selected_contract(self):
        c = sc.resolve_selected_contract_from_artifacts()
        self.assertEqual(c.candidate_name, "384-small")
        self.assertEqual(c.dimension, 384)
        self.assertEqual(c.selected_contract_id, 1360541028304258884)
        self.assertEqual(c.selected_contract_id, sc.SELECTED_CONTRACT_ID)
        self.assertNotEqual(c.selected_contract_id, c.historical_dimension_only_id)
        self.assertFalse(c.production_activated)
        self.assertEqual(c.chunk_config_identity, sc.SELECTED_CHUNK_CONFIG_IDENTITY)
        self.assertEqual(c.prose_target_tokens, 512)
        self.assertEqual(c.prose_overlap_tokens, 50)
        self.assertEqual(c.python_target_tokens, 512)
        self.assertEqual(c.python_overlap_tokens, 50)
        # manifest hashes are present (tamper anchors)
        for h in (c.golden_sha256, c.candidate_grid_hash, c.decision_policy_hash,
                  c.eval3_cohort_manifest_hash, c.decision_core_sha256):
            self.assertTrue(h and len(h) == 64)

    def test_all_config_surfaces_bind_the_same_values(self):
        c = sc.resolve_selected_contract_from_artifacts()
        # Python chunking defaults agree with the selected config.
        self.assertEqual(ch.DEFAULT_PROSE_CONFIG.target_tokens, c.prose_target_tokens)
        self.assertEqual(ch.DEFAULT_PROSE_CONFIG.overlap_tokens, c.prose_overlap_tokens)
        self.assertEqual(ch.DEFAULT_PYTHON_CONFIG.target_tokens, c.python_target_tokens)
        self.assertEqual(ch.DEFAULT_PYTHON_CONFIG.overlap_tokens, c.python_overlap_tokens)

    def _dec(self, **over):
        d = json.loads(Path(sc.TASK_2_14_DECISION_PATH).read_text())
        d.update(over)
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(d, tmp)
        tmp.close()
        return tmp.name

    def test_fail_closed_on_production_activated_true(self):
        import copy
        d = json.loads(Path(sc.TASK_2_14_DECISION_PATH).read_text())
        d["selection"]["production_activated"] = True
        dp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(d, dp); dp.close()
        mp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(json.loads(Path(sc.TASK_2_14_MANIFEST_PATH).read_text()), mp); mp.close()
        with self.assertRaises(sc.SelectedContractResolutionError):
            sc.resolve_selected_contract_from_artifacts(dp.name, mp.name)

    def test_fail_closed_on_1536_dimension(self):
        d = json.loads(Path(sc.TASK_2_14_DECISION_PATH).read_text())
        d["selection"]["selected_production_dimension"] = 1536
        dp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(d, dp); dp.close()
        mp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(json.loads(Path(sc.TASK_2_14_MANIFEST_PATH).read_text()), mp); mp.close()
        with self.assertRaises(sc.SelectedContractResolutionError):
            sc.resolve_selected_contract_from_artifacts(dp.name, mp.name)

    def test_fail_closed_on_tampered_manifest_hash(self):
        dp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(json.loads(Path(sc.TASK_2_14_DECISION_PATH).read_text()), dp); dp.close()
        m = json.loads(Path(sc.TASK_2_14_MANIFEST_PATH).read_text())
        m["candidate_grid_hash"] = "deadbeef"
        mp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(m, mp); mp.close()
        with self.assertRaises(sc.SelectedContractResolutionError):
            sc.resolve_selected_contract_from_artifacts(dp.name, mp.name)

    def test_fail_closed_on_ambiguous_legacy_bigint_as_selected(self):
        # The legacy dimension-only id must NEVER equal the selected full id.
        self.assertNotEqual(sc.HISTORICAL_DIMENSION_ONLY_ID, sc.SELECTED_CONTRACT_ID)
        d = json.loads(Path(sc.TASK_2_14_DECISION_PATH).read_text())
        d["selection"]["selected_base_contract_id_dimension_only"] = str(sc.SELECTED_CONTRACT_ID)
        dp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(d, dp); dp.close()
        mp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(json.loads(Path(sc.TASK_2_14_MANIFEST_PATH).read_text()), mp); mp.close()
        with self.assertRaises(sc.SelectedContractResolutionError):
            sc.resolve_selected_contract_from_artifacts(dp.name, mp.name)

    def test_fail_closed_on_multi_selected(self):
        d = json.loads(Path(sc.TASK_2_14_DECISION_PATH).read_text())
        for cand in d["candidates"]:
            cand["selection_state"]["selected"] = True
        dp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(d, dp); dp.close()
        mp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(json.loads(Path(sc.TASK_2_14_MANIFEST_PATH).read_text()), mp); mp.close()
        with self.assertRaises(sc.SelectedContractResolutionError):
            sc.resolve_selected_contract_from_artifacts(dp.name, mp.name)


class ChunkerOversizedLineTests(unittest.TestCase):
    """T2: a single oversized line is bounded into multi-chunks with full coverage
    (the task-2.17 oversized-line defect). Both AST and parser-fallback paths."""

    OVERSIZE_LINE = "x = '" + ("a" * 6000) + "'\n"

    def test_ast_path_oversized_block_is_bounded(self):
        src = "import torch\n" + self.OVERSIZE_LINE + "MARKER_HUGE_LATE = 1\n"
        chunks = wr.chunk_python(src, target_tokens=512, overlap_tokens=50)
        self.assertGreater(len(chunks), 1)
        bound = 512 * wr.CHARS_PER_TOKEN + 50 * wr.CHARS_PER_TOKEN  # target + overlap
        self.assertLessEqual(max(len(c.text) for c in chunks), bound + 1)
        self.assertTrue(wr.coverage_ok(src, chunks))
        # later marker is in a chunk with index > 0
        self.assertTrue(any("MARKER_HUGE_LATE" in c.text and c.index > 0 for c in chunks))

    def test_parser_fallback_oversized_line_is_bounded(self):
        # Unparseable Python -> _line_window fallback, with an oversized line.
        src = "def broken(:\n    y = " + ("z" * 6000) + "\nMARKER_FALLBACK_LATE = 1\n"
        chunks = wr.chunk_python(src, target_tokens=512, overlap_tokens=50)
        self.assertGreater(len(chunks), 1, "oversized line must split, not one unbounded chunk")
        bound = 512 * wr.CHARS_PER_TOKEN + 50 * wr.CHARS_PER_TOKEN
        self.assertLessEqual(max(len(c.text) for c in chunks), bound + 1, "each chunk within bound")
        self.assertEqual(sorted({c.method for c in chunks}), ["ast_fallback"])
        self.assertTrue(wr.coverage_ok(src, chunks), "full normalized-source coverage")
        self.assertTrue(any("MARKER_FALLBACK_LATE" in c.text and c.index > 0 for c in chunks))

    def test_negative_old_behavior_would_fail(self):
        # The OLD _line_window appended one oversized line whole. Assert the fix:
        # a single oversized line yields >1 chunk (the bug yielded exactly 1).
        norm = wr.normalize_for_hash("def broken(:\n    y = " + ("z" * 6000) + "\n")
        packs = wr._line_window(norm.split("\n"), 512 * wr.CHARS_PER_TOKEN, 50)
        self.assertGreater(len(packs), 1)

    def test_deterministic_rerun(self):
        src = "def broken(:\n    y = " + ("z" * 6000) + "\nMARKER_LATE = 1\n"
        a = wr.chunk_python(src, target_tokens=512, overlap_tokens=50)
        b = wr.chunk_python(src, target_tokens=512, overlap_tokens=50)
        self.assertEqual([c.text for c in a], [c.text for c in b])
        self.assertEqual([c.hash for c in a], [c.hash for c in b])


class ManifestBuilderTests(unittest.TestCase):
    """T2: the canonical manifest (worker payload source of truth) is correct for
    safe / quarantined / unavailable, and byte-identical to chunk_representations."""

    def _wf(self, payload_python, body="d"):
        return {"kind": "workflow", "title": "T", "body": body,
                "payload": {"python_source": payload_python}, "metadata": {}}

    def test_safe_workflow_manifest_has_prose_and_python_chunks(self):
        entries = ch.build_representation_manifest(self._wf(SAFE_PY), ei.ENTITY_RESOURCE, item_id=1)
        by = {e.representation_type: e for e in entries}
        self.assertIn("prose", by)
        self.assertIn("workflow_python", by)
        self.assertTrue(by["workflow_python"].source_available)
        self.assertEqual(by["workflow_python"].public_state, "safe")
        self.assertGreater(by["workflow_python"].chunk_count, 0)

    def test_quarantined_python_is_state_only_no_chunks(self):
        entries = ch.build_representation_manifest(self._wf(SECRET_PY), ei.ENTITY_RESOURCE, item_id=2)
        wp = next(e for e in entries if e.representation_type == "workflow_python")
        self.assertEqual(wp.public_state, "quarantined")
        self.assertEqual(wp.chunk_count, 0)
        self.assertEqual(wp.chunks, ())
        self.assertEqual(wp.unavailable_reason, "quarantined_python")

    def test_unavailable_python_is_state_only(self):
        entries = ch.build_representation_manifest(self._wf({}, body="no code"), ei.ENTITY_RESOURCE, item_id=3)
        wp = next(e for e in entries if e.representation_type == "workflow_python")
        self.assertFalse(wp.source_available)
        self.assertEqual(wp.chunk_count, 0)
        self.assertEqual(wp.unavailable_reason, "python_unavailable")

    def test_manifest_chunks_equal_chunk_representations(self):
        """The two producers (manifest vs chunk_representations) agree byte-for-byte."""
        row = self._wf(SAFE_PY)
        reps = canon.build_representations(row, ei.ENTITY_RESOURCE, item_id=7)
        via_chunks = ch.chunk_representations(reps)
        manifest = ch.build_representation_manifest(row, ei.ENTITY_RESOURCE, item_id=7)
        wp_manifest = next(e for e in manifest if e.representation_type == "workflow_python")
        via_wp = [c for c in via_chunks if c.representation_type == "workflow_python"]
        self.assertEqual(wp_manifest.chunk_count, len(via_wp))
        self.assertEqual([c["chunk_hash"] for c in wp_manifest.chunks], [c.chunk_hash for c in via_wp])
        self.assertEqual([c["chunk_text"] for c in wp_manifest.chunks], [c.text for c in via_wp])
        self.assertEqual([c["chunk_index"] for c in wp_manifest.chunks], [c.chunk_index for c in via_wp])

    def test_manifest_parity_rerun_equal(self):
        row = self._wf(SAFE_PY)
        a = ch.build_representation_manifest(row, ei.ENTITY_RESOURCE, item_id=9)
        b = ch.build_representation_manifest(row, ei.ENTITY_RESOURCE, item_id=9)
        self.assertTrue(ch.manifest_entries_equal(a, b))


if __name__ == "__main__":
    unittest.main()
