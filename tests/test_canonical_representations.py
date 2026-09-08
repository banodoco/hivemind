"""Offline tests for message/resource canonical representations."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from executors import canonical_representations as cr  # noqa: E402
from executors import entity_identity as ei  # noqa: E402
from executors import embedding_contract as ec  # noqa: E402
from executors import workflow_representation as wr  # noqa: E402


class MessageTests(unittest.TestCase):
    def test_content_is_the_prose_representation(self):
        reps = cr.build_representations({"content": "lower motion amplitude"}, "message", item_id="999")
        self.assertEqual(len(reps), 1)
        self.assertEqual(reps[0].text, "lower motion amplitude")
        self.assertEqual(reps[0].representation_hash, wr.representation_hash(reps[0].text))


class ResourceTests(unittest.TestCase):
    def test_non_workflow_resource_uses_frozen_field_order(self):
        reps = cr.build_representations({
            "kind": "article", "title": "Wan guide", "body": "Use sampler",
            "metadata": {"tags": ["wan", "video"]},
        }, "resource", item_id="17")
        self.assertEqual(reps[0].text, "Wan guide\n\nUse sampler\n\nwan video")

    def test_workflow_emits_prose_and_safe_python_once(self):
        source = "x = 1\n"
        reps = cr.build_representations({
            "kind": "workflow", "title": "WF", "body": "Description\n\nPython ready-template source:\n" + source,
            "payload": {"python_source": source},
        }, "resource", item_id=17)
        self.assertEqual([r.representation_type for r in reps], ["prose", "workflow_python"])
        self.assertNotIn("x = 1", reps[0].text)
        self.assertEqual(reps[1].text, source)

    def test_quarantined_python_is_not_returned_or_hashed(self):
        secret = "API_TOKEN = 'sk-" + "A" * 40 + "'\n"
        reps = cr.build_representations({"kind": "workflow", "title": "WF", "body": "desc", "payload": {"python_source": secret}}, "resource", item_id=1)
        self.assertEqual([r.representation_type for r in reps], ["prose"])
        self.assertNotIn("sk-", repr(reps))


class VersionTests(unittest.TestCase):
    def test_representation_versions_match_contract(self):
        rep = cr.build_representations({"content": "x"}, "message", item_id=1)[0]
        self.assertEqual(rep.canonicalization_version, ec.CANONICALIZATION_VERSION)
        self.assertEqual(rep.chunking_version, ec.CHUNKING_VERSION)
        self.assertEqual(rep.secret_scan_version, ec.SECRET_SCAN_VERSION)

    def test_ids_are_exact_strings_and_bad_types_fail(self):
        self.assertEqual(cr.build_representations({"content": "x"}, "message", item_id=1234567890123456789)[0].item_id, "1234567890123456789")
        with self.assertRaises(ValueError):
            cr.build_representations({}, "workflow", item_id="1")

    def test_hash_is_single_frozen_algorithm(self):
        text = cr.canonical_resource_text("T", "line1\r\nline2", None)
        self.assertEqual(wr.representation_hash(text), wr.representation_hash(text.replace("\r\n", "\n")))
        self.assertEqual(ei.entity_type_for_result_kind("guide"), "resource")


if __name__ == "__main__":
    unittest.main()
