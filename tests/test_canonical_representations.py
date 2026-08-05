"""Offline tests for canonical semantic representations + hashes (plan task 2.5).

Pure and offline. Pins the frozen canonical text for every entity/representation
type, the workflow Python precedence, secret-state EXCLUSION (quarantined Python
is never returned/hashed), no-duplication (one workflow_python per workflow),
contract versioning, and that representation_hash is the single frozen algorithm.
Cross-language (Python vs SQL) hash parity is proven separately by the isolated
rehearsal (scripts/rehearse_embedding_schema.py) and the SQL test
(test_embedding_schema_sql.py).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))

from executors import canonical_representations as cr  # noqa: E402
from executors import workflow_representation as wr  # noqa: E402
from executors import embedding_contract as ec  # noqa: E402


# ---------------------------------------------------------------------------
# Non-workflow canonical text + hash
# ---------------------------------------------------------------------------


class MessageRepresentationTests(unittest.TestCase):
    def test_prose_is_content(self):
        reps = cr.build_representations({"content": "lower the motion amplitude"}, "message", item_id="999")
        self.assertEqual(len(reps), 1)
        r = reps[0]
        self.assertEqual(r.entity_type, "message")
        self.assertEqual(r.representation_type, "prose")
        self.assertEqual(r.text, "lower the motion amplitude")
        self.assertEqual(r.representation_hash, wr.representation_hash("lower the motion amplitude"))

    def test_missing_content_is_empty_but_present(self):
        reps = cr.build_representations({}, "message", item_id="1")
        self.assertEqual(len(reps), 1)
        self.assertEqual(reps[0].text, "")
        # Empty text still hashes deterministically.
        self.assertEqual(reps[0].representation_hash, wr.representation_hash(""))


class DistillationRepresentationTests(unittest.TestCase):
    def test_prose_field_order(self):
        reps = cr.build_representations(
            {"question": "best upscaler", "conditions": "for anime video", "answer": "RealESRGAN x2"},
            "distillation", item_id="5",
        )
        self.assertEqual(len(reps), 1)
        self.assertEqual(reps[0].text, "best upscaler\n\nfor anime video\n\nRealESRGAN x2")

    def test_empty_conditions_skipped(self):
        reps = cr.build_representations(
            {"question": "q", "conditions": "", "answer": "a"}, "distillation", item_id="5",
        )
        self.assertEqual(reps[0].text, "q\n\na")

    def test_status_and_confidence_excluded(self):
        reps = cr.build_representations(
            {"question": "q", "conditions": None, "answer": "a", "status": "approved", "confidence": "high"},
            "distillation", item_id="5",
        )
        self.assertNotIn("approved", reps[0].text)
        self.assertNotIn("high", reps[0].text)


class ResourceRepresentationTests(unittest.TestCase):
    def test_non_workflow_prose(self):
        reps = cr.build_representations(
            {"kind": "article", "title": "WanVideo guide", "body": "use WanVideoSampler",
             "metadata": {"tags": ["wan", "video"]}},
            "resource", item_id="17",
        )
        self.assertEqual(len(reps), 1)
        self.assertEqual(reps[0].representation_type, "prose")
        self.assertEqual(reps[0].text, "WanVideo guide\n\nuse WanVideoSampler\n\nwan video")

    def test_unknown_resource_kind_is_prose_only(self):
        reps = cr.build_representations(
            {"kind": "transcript", "title": "T", "body": "B"}, "resource", item_id="1",
        )
        self.assertEqual([r.representation_type for r in reps], ["prose"])


# ---------------------------------------------------------------------------
# Workflow Python precedence (frozen AD-4)
# ---------------------------------------------------------------------------


class WorkflowPrecedenceTests(unittest.TestCase):
    def _wf(self, **kw):
        row = {"kind": "workflow", "title": "WanVideo I2V", "body": "A description of the workflow."}
        row.update(kw)
        return row

    def test_payload_python_is_authoritative_and_emitted_once(self):
        row = self._wf(body="desc\n\nPython scratchpad source:\nx = 1\n",
                       payload={"python_source": "x = 1\n"})
        reps = cr.build_representations(row, "resource", item_id="2580")
        types = sorted(r.representation_type for r in reps)
        self.assertEqual(types, ["prose", "workflow_python"])
        py = [r for r in reps if r.representation_type == "workflow_python"]
        self.assertEqual(len(py), 1)  # emitted exactly once
        self.assertEqual(py[0].text, "x = 1\n")
        self.assertEqual(py[0].representation_hash, wr.representation_hash("x = 1\n"))

    def test_both_cohort_no_duplication(self):
        # Same Python in payload AND under a body delimiter -> ONE python rep,
        # and the prose has the block stripped (no duplicate code in prose).
        py = "import comfy\nsampler = WanVideoSampler()\n"
        row = self._wf(body="A workflow.\n\nPython scratchpad source:\n" + py,
                       payload={"python_source": py})
        reps = cr.build_representations(row, "resource", item_id="2580")
        python_reps = [r for r in reps if r.representation_type == "workflow_python"]
        self.assertEqual(len(python_reps), 1)
        prose = next(r for r in reps if r.representation_type == "prose")
        # The code is NOT in the prose representation (no-duplication).
        self.assertNotIn("WanVideoSampler", prose.text)
        self.assertIn("A workflow", prose.text)

    def test_body_only_python_cohort(self):
        row = self._wf(body="desc\n\nPython ready-template source:\ny = 2\n")
        reps = cr.build_representations(row, "resource", item_id="1")
        python_reps = [r for r in reps if r.representation_type == "workflow_python"]
        self.assertEqual(len(python_reps), 1)
        # The frozen extractor strips the delimited body block (no trailing newline).
        self.assertEqual(python_reps[0].text, "y = 2")

    def test_unavailable_no_python_representation(self):
        # No payload python, no body block, no recoverer -> only prose.
        row = self._wf(body="just a description, no code")
        reps = cr.build_representations(row, "resource", item_id="1")
        self.assertEqual([r.representation_type for r in reps], ["prose"])

    def test_recoverable_python_via_injected_recoverer(self):
        recovered = "z = compute()\n"
        row = self._wf(body="no code here")
        reps = cr.build_representations(
            row, "resource", item_id="1", recoverer=lambda r: recovered,
        )
        python_reps = [r for r in reps if r.representation_type == "workflow_python"]
        self.assertEqual(len(python_reps), 1)
        self.assertEqual(python_reps[0].text, recovered)


# ---------------------------------------------------------------------------
# Secret-state EXCLUSION (the task 0.8 quarantine freeze)
# ---------------------------------------------------------------------------


_SECRET_PY = "API_TOKEN = 'sk-" + "A" * 40 + "'\n"  # matches provider_token_prefix


class SecretExclusionTests(unittest.TestCase):
    def test_quarantined_python_not_returned(self):
        row = {"kind": "workflow", "title": "T", "body": "desc",
               "payload": {"python_source": _SECRET_PY}}
        reps = cr.build_representations(row, "resource", item_id="1")
        # Only prose is returned; the quarantined python representation is excluded.
        self.assertEqual([r.representation_type for r in reps], ["prose"])
        # And the prose does not carry the secret.
        for r in reps:
            self.assertNotIn("sk-", r.text)
            self.assertNotIn("AAAA", r.text)

    def test_quarantined_python_never_hashed(self):
        row = {"kind": "workflow", "title": "T", "body": "desc",
               "payload": {"python_source": _SECRET_PY}}
        reps = cr.build_representations(row, "resource", item_id="1")
        secret_hash = wr.representation_hash(_SECRET_PY)
        for r in reps:
            self.assertNotEqual(r.representation_hash, secret_hash)

    def test_status_reports_quarantine_without_leaking_value(self):
        row = {"kind": "workflow", "title": "T", "body": "desc",
               "payload": {"python_source": _SECRET_PY}}
        status = cr.workflow_python_status(row)
        self.assertFalse(status.has_representation)
        self.assertEqual(status.public_state, "quarantined")
        self.assertIn("provider_token_prefix", status.secret_reason_codes)
        # The status dataclass exposes NO text and NO hash fields.
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(status)}
        self.assertNotIn("text", field_names)
        self.assertNotIn("hash", field_names)
        self.assertNotIn("source", field_names)

    def test_status_secret_value_not_in_repr(self):
        row = {"kind": "workflow", "title": "T", "body": "desc",
               "payload": {"python_source": _SECRET_PY}}
        status = cr.workflow_python_status(row)
        self.assertNotIn("sk-", repr(status))
        self.assertNotIn("A" * 40, repr(status))


# ---------------------------------------------------------------------------
# Versioning + hashing invariants
# ---------------------------------------------------------------------------


class VersioningTests(unittest.TestCase):
    def test_versions_present_and_frozen(self):
        reps = cr.build_representations({"content": "x"}, "message", item_id="1")
        r = reps[0]
        self.assertEqual(r.canonicalization_version, wr.CANONICALIZATION_VERSION)
        self.assertEqual(r.chunking_version, wr.CHUNKING_VERSION)
        self.assertEqual(r.secret_scan_version, wr.SECRET_SCAN_VERSION)
        # And they match the embedding-contract vocabulary.
        self.assertEqual(r.canonicalization_version, ec.CANONICALIZATION_VERSION)

    def test_item_id_stringified(self):
        reps = cr.build_representations({"content": "x"}, "message", item_id=1234567890123456789)
        self.assertEqual(reps[0].item_id, "1234567890123456789")

    def test_bad_entity_type_raises(self):
        with self.assertRaises(ValueError):
            cr.build_representations({}, "workflow", item_id="1")  # not an entity_type


class HashSingleSourceTests(unittest.TestCase):
    def test_representation_hash_uses_frozen_algorithm(self):
        reps = cr.build_representations(
            {"question": "q", "conditions": "c", "answer": "a"}, "distillation", item_id="1",
        )
        self.assertEqual(reps[0].representation_hash, wr.representation_hash(reps[0].text))

    def test_crlf_normalizes_same_hash(self):
        a = cr.canonical_resource_text("T", "line1\r\nline2", None)
        b = cr.canonical_resource_text("T", "line1\nline2", None)
        self.assertEqual(wr.representation_hash(a), wr.representation_hash(b))


if __name__ == "__main__":
    unittest.main()
