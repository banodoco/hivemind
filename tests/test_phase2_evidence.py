"""Adversarial tests for the Task 2.17 evidence boundary."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.rehearse_phase2_acceptance import scan_evidence  # noqa: E402


class EvidenceBoundaryTests(unittest.TestCase):
    def test_normal_aggregate_envelope_is_allowed(self):
        self.assertEqual(scan_evidence({"checks": [{"name": "x", "ok": True, "detail": "count=1"}]}), [])

    def test_numeric_vector_array_is_rejected(self):
        self.assertTrue(scan_evidence({"checks": [{"name": "x", "ok": True, "detail": "ok"}], "vectors": [0.0, 1.0, 2.0]}))

    def test_forbidden_keys_are_rejected_regardless_of_value_type(self):
        for key, value in {
            "embedding": [0, 1, 2], "snippet": 7, "query": 3, "url": "x", "source": False,
            "exception": {"message": "x"}, "password": 123,
        }.items():
            with self.subTest(key=key):
                self.assertTrue(scan_evidence({key: value}))

    def test_unknown_keys_and_bad_types_are_rejected(self):
        self.assertTrue(scan_evidence({"not_in_schema": 1}))
        self.assertTrue(scan_evidence({"ok": "true"}))
        self.assertTrue(scan_evidence({"dimension": 384.0}))

    def test_paths_and_exception_text_are_rejected_recursively(self):
        self.assertTrue(scan_evidence({"checks": [{"name": "x", "ok": True, "detail": "/private/tmp/a"}]}))
        self.assertTrue(scan_evidence({"checks": [{"name": "x", "ok": True, "detail": "Traceback: hidden"}]}))


if __name__ == "__main__":
    unittest.main()
