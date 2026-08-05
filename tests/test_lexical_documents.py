"""Offline unit tests for the task-1.2 lexical-document helper.

Covers the frozen workflow-Python pipeline end to end on in-memory rows, using
the frozen reference contracts (``executors.workflow_representation`` /
``executors.lexical_contract``) and an in-memory store:

  - payload-only / body-only / duplicated-both (precedence + no-duplication)
  - changed Python (hash-skip vs re-upsert)
  - huge code (multi-chunk, no silent truncation, AST + literal fallback)
  - parser fallback (unparseable Python => ast_fallback)
  - unavailable (no Python recoverable; prose stays searchable)
  - quarantined credential (scanner hit => zero documents, secret never leaks)
  - prose-only / non-workflow resource
  - distillation pending/approved/rejected eligibility

No database, no network, no provider. Pure stdlib.
"""

from __future__ import annotations

import unittest

from executors import lexical_contract as LC
from executors import lexical_documents as LD
from executors import workflow_representation as WR

# A small, valid VibeComfy-shaped Python module used across fixtures.
SAMPLE_PY = (
    "import torch\n"
    "class WanVideoSampler:\n"
    "    def __init__(self, lora_weight=0.8, num_frames=81):\n"
    "        self.lora_weight = lora_weight\n"
    "        self.num_frames = num_frames\n"
)

READY_DELIM = "Python ready-template source:"
SCRATCH_DELIM = "Python scratchpad source:"


def _row(rid, kind="workflow", body="", python=None, metadata=None):
    payload = {"python_source": python} if python is not None else {}
    return {
        "id": rid,
        "kind": kind,
        "title": f"title-{rid}",
        "body": body,
        "metadata": metadata or {},
        "payload": payload,
    }


class TestPrecedenceAndCohorts(unittest.TestCase):
    def test_payload_python_is_authoritative(self):
        row = _row(1, python=SAMPLE_PY)
        state, docs = LD.compute_workflow_python_documents(row)
        self.assertEqual(state.cohort, WR.COHORT_PAYLOAD_PYTHON)
        self.assertTrue(state.available)
        self.assertTrue(docs)
        self.assertEqual({d.representation_type for d in docs}, {"workflow_python"})
        self.assertTrue(all(d.quarantine_state == "safe" for d in docs))

    def test_body_only_when_payload_empty(self):
        body = f"description\n\n{READY_DELIM}\n{SAMPLE_PY}"
        row = _row(2, body=body, python=None)
        state, docs = LD.compute_workflow_python_documents(row)
        self.assertEqual(state.cohort, WR.COHORT_BODY_PYTHON)
        self.assertEqual(state.delimiter, READY_DELIM)
        self.assertTrue(docs)
        self.assertIn("wanvideosampler", docs[0].chunk_text.lower())

    def test_payload_precedence_over_different_body_block(self):
        # payload and body both carry Python but DIFFERENT bytes -> payload wins,
        # and the body block is not a duplicate.
        body = f"{SCRATCH_DELIM}\nLEGACY_CODE = 1\n"
        row = _row(3, body=body, python=SAMPLE_PY)
        state, docs = LD.compute_workflow_python_documents(row)
        self.assertEqual(state.cohort, WR.COHORT_PAYLOAD_PYTHON)
        self.assertFalse(state.body_duplicate)
        # Authoritative bytes come from payload, not the body block.
        self.assertIn("wanvideosampler", docs[0].chunk_text.lower())
        self.assertNotIn("LEGACY_CODE", "".join(d.chunk_text for d in docs))


class TestNoDuplication(unittest.TestCase):
    def _both_row(self):
        # The "both" cohort: identical Python in payload AND under a body delimiter.
        body = f"Workflow description.\n\n{READY_DELIM}\n{SAMPLE_PY}"
        return _row(10, body=body, python=SAMPLE_PY)

    def test_both_flagged_body_duplicate(self):
        state, _ = LD.compute_workflow_python_documents(self._both_row())
        self.assertEqual(state.cohort, WR.COHORT_PAYLOAD_PYTHON)
        self.assertTrue(state.body_duplicate)

    def test_python_indexed_exactly_once(self):
        row = self._both_row()
        state, docs = LD.compute_workflow_python_documents(row)
        # One set of chunks from the authoritative payload, not two.
        expected = WR.chunk_python(SAMPLE_PY)
        self.assertEqual(state.chunk_count, len(expected))
        self.assertEqual(len(docs), len(expected))
        self.assertEqual(sorted(d.chunk_index for d in docs), list(range(len(expected))))

    def test_prose_has_python_stripped(self):
        row = self._both_row()
        prose = LD.build_workflow_prose(row)
        # The class symbol lives only in code; prose must not carry it.
        self.assertNotIn("WanVideoSampler", prose)
        self.assertNotIn("lora_weight", prose)
        # Prose still keeps the description.
        self.assertIn("Workflow description", prose)


class TestQuarantine(unittest.TestCase):
    SECRET_SK = "sk-abcdefghijklmnopqrstuvwxyz0123456789ABCDEF"
    SECRET_PEM = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIKBgQdummydummydummydummydummydummydummydummydummy=\n"
        "-----END RSA PRIVATE KEY-----\n"
    )

    def _secret_row(self):
        py = f"import torch\napi_key = '{self.SECRET_SK}'\nprint(api_key)\n"
        return _row(20, python=py)

    def test_secret_quarantines_and_produces_zero_documents(self):
        row = self._secret_row()
        state, docs = LD.compute_workflow_python_documents(row)
        self.assertEqual(state.public_state, "quarantined")
        self.assertFalse(state.available is False and state.public_state == "safe")
        self.assertEqual(docs, ())
        self.assertEqual(state.chunk_count, 0)
        self.assertTrue(state.secret_reason_codes)

    def test_matched_secret_value_never_leaks(self):
        row = self._secret_row()
        state, docs = LD.compute_workflow_python_documents(row)
        # The secret value must not appear anywhere in state or documents.
        for obj in (state, *docs):
            self.assertNotIn(self.SECRET_SK, repr(obj))
        # Reason codes are codes only, never the value.
        for code in state.secret_reason_codes:
            self.assertNotIn(self.SECRET_SK, code)

    def test_pem_block_is_quarantined(self):
        row = _row(21, python=f"x = 1\n{self.SECRET_PEM}\n")
        state, docs = LD.compute_workflow_python_documents(row)
        self.assertEqual(state.public_state, "quarantined")
        self.assertEqual(docs, ())
        self.assertIn("private_key_block", state.secret_reason_codes)

    def test_quarantine_refresh_writes_state_but_no_docs(self):
        store = LD.InMemoryLexicalStore()
        outcome = LD.refresh_workflow_python(self._secret_row(), store)
        self.assertEqual(outcome.action, "quarantine")
        self.assertEqual(store.list_documents("20"), [])
        self.assertIsNotNone(store.get_state("20"))
        self.assertEqual(store.get_state("20").public_state, "quarantined")


class TestUnavailableAndProseOnly(unittest.TestCase):
    def test_unavailable_when_no_python(self):
        row = _row(30, body="Just a description, no code.", python=None)
        state, docs = LD.compute_workflow_python_documents(row)
        self.assertEqual(state.cohort, WR.COHORT_UNAVAILABLE)
        self.assertFalse(state.available)
        self.assertEqual(state.public_state, "safe")
        self.assertEqual(docs, ())

    def test_unavailable_remains_prose_searchable(self):
        row = _row(
            31,
            body="Image-to-video pipeline using WanVideoSampler.",
            metadata={"workflow_semantics": {"media_type": "video", "task_type": "image_to_video"}},
            python=None,
        )
        state, docs = LD.compute_workflow_python_documents(row)
        self.assertEqual(state.cohort, WR.COHORT_UNAVAILABLE)
        self.assertEqual(docs, ())
        # Prose + semantics remain searchable.
        prose = LD.build_workflow_prose(row)
        self.assertIn("WanVideoSampler", prose)
        self.assertIn("video", LD.project_semantics_text(row["metadata"]))

    def test_non_workflow_resource_has_no_python_doc(self):
        # An article carries no Python; the helper reports unavailable/safe.
        row = _row(40, kind="article", body="An article body about upscaling.", python=None)
        state, docs = LD.compute_workflow_python_documents(row)
        self.assertEqual(state.cohort, WR.COHORT_UNAVAILABLE)
        self.assertEqual(docs, ())


class TestChunkingAndCoverage(unittest.TestCase):
    def test_small_payload_is_single_chunk(self):
        row = _row(50, python=SAMPLE_PY)
        state, docs = LD.compute_workflow_python_documents(row)
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].method, "ast")
        self.assertEqual(docs[0].chunk_index, 0)

    def test_huge_code_multi_chunk_no_silent_truncation(self):
        # ~600 top-level statements -> multiple AST chunks, full coverage.
        lines = [f"v{i} = WanVideoSampler(model='m{i}.safetensors', num_frames={i})"
                 for i in range(600)]
        py = "import os\n" + "\n".join(lines)
        row = _row(51, python=py)
        state, docs = LD.compute_workflow_python_documents(row)
        self.assertGreater(len(docs), 1)
        # No-silent-truncation: chunks cover the full normalized source.
        self.assertTrue(WR.coverage_ok(py, [WR.Chunk(
            index=d.chunk_index, text=d.chunk_text, hash=d.chunk_hash,
            start_line=0, end_line=0, method=d.method, token_estimate=0)
            for d in docs]))

    def test_huge_literal_uses_ast_fallback(self):
        # A single generated literal dwarfing the budget => ast_fallback split.
        py = "big = " + repr("a" * 60000) + "\n"
        row = _row(52, python=py)
        state, docs = LD.compute_workflow_python_documents(row)
        self.assertGreater(len(docs), 1)
        self.assertTrue(all(d.method == "ast_fallback" for d in docs))
        chunks = [WR.Chunk(index=d.chunk_index, text=d.chunk_text, hash=d.chunk_hash,
                           start_line=0, end_line=0, method=d.method, token_estimate=0)
                  for d in docs]
        self.assertTrue(WR.coverage_ok(py, chunks))

    def test_parser_fallback_on_unparseable_python(self):
        py = "def broken(:\n    this is not valid python !!!\n"
        row = _row(53, python=py)
        state, docs = LD.compute_workflow_python_documents(row)
        self.assertTrue(docs)
        self.assertTrue(all(d.method == "ast_fallback" for d in docs))

    def test_offsets_are_within_bounds(self):
        row = _row(54, python="import torch\n" + "x = 1\n" * 900)
        _, docs = LD.compute_workflow_python_documents(row)
        norm_len = len(WR.normalize_for_hash(row["payload"]["python_source"]))
        for d in docs:
            self.assertGreaterEqual(d.source_offset_start, 0)
            self.assertGreaterEqual(d.source_offset_end, d.source_offset_start)
            self.assertLessEqual(d.source_offset_end, norm_len + 1)


class TestRefreshUpsertDropHashSkip(unittest.TestCase):
    def test_idempotent_refresh_hash_skips(self):
        store = LD.InMemoryLexicalStore()
        row = _row(60, python=SAMPLE_PY)
        first = LD.refresh_workflow_python(row, store)
        self.assertEqual(first.action, "upsert")
        second = LD.refresh_workflow_python(row, store)
        self.assertEqual(second.action, "skip")
        # State untouched on skip.
        self.assertEqual(store.get_state("60").chunk_count, first.chunk_count)

    def test_changed_python_re_upserts(self):
        store = LD.InMemoryLexicalStore()
        row = _row(61, python=SAMPLE_PY)
        LD.refresh_workflow_python(row, store)
        old_hash = store.get_state("61").representation_hash
        # Change the authoritative Python.
        row2 = _row(61, python=SAMPLE_PY + "\nextra_call = True\n")
        outcome = LD.refresh_workflow_python(row2, store)
        self.assertEqual(outcome.action, "upsert")
        new_hash = store.get_state("61").representation_hash
        self.assertNotEqual(old_hash, new_hash)

    def test_drop_removes_state_and_documents(self):
        store = LD.InMemoryLexicalStore()
        LD.refresh_workflow_python(_row(62, python=SAMPLE_PY), store)
        self.assertTrue(store.list_documents("62"))
        LD.drop_workflow_python("62", store)
        self.assertEqual(store.list_documents("62"), [])
        self.assertIsNone(store.get_state("62"))

    def test_quarantine_then_fix_re_upserts(self):
        store = LD.InMemoryLexicalStore()
        secret = f"api_key = '{TestQuarantine.SECRET_SK}'\n"
        LD.refresh_workflow_python(_row(63, python=secret), store)
        self.assertEqual(store.get_state("63").public_state, "quarantined")
        self.assertEqual(store.list_documents("63"), [])
        # Operator removes the secret from the source row.
        LD.refresh_workflow_python(_row(63, python=SAMPLE_PY), store)
        self.assertEqual(store.get_state("63").public_state, "safe")
        self.assertTrue(store.list_documents("63"))


class TestDistillationEligibility(unittest.TestCase):
    def test_predicate_is_pending_or_approved(self):
        pred = LD.DISTILLATION_STATUS_PREDICATE
        self.assertIn("status IN ('pending', 'approved')", pred)

    def test_status_filter_logic(self):
        ok = {"pending", "approved"}
        for status in ("pending", "approved"):
            self.assertIn(status, ok)
        for status in ("rejected", "superseded"):
            self.assertNotIn(status, ok)

    def test_workflow_python_eligibility_gate(self):
        self.assertIn("hivemind_workflow_python_state", LD.WORKFLOW_PYTHON_ELIGIBLE_PREDICATE)
        self.assertIn("'safe'", LD.WORKFLOW_PYTHON_ELIGIBLE_PREDICATE)


class TestIdentityAndInvariants(unittest.TestCase):
    def test_documents_carry_frozen_identity(self):
        row = _row(70, python="import torch\n" + "x = 1\n" * 700)
        _, docs = LD.compute_workflow_python_documents(row)
        for d in docs:
            self.assertEqual(d.entity_type, LC.ENTITY_RESOURCE)
            self.assertEqual(d.representation_type, LC.REP_WORKFLOW_PYTHON)
            self.assertEqual(d.item_id, "70")
            self.assertEqual(d.lexicalization_version, LC.LEXICALIZATION_VERSION)
            self.assertEqual(d.canonicalization_version, WR.CANONICALIZATION_VERSION)
            self.assertEqual(d.chunking_version, WR.CHUNKING_VERSION)
            self.assertEqual(d.secret_scan_version, WR.SECRET_SCAN_VERSION)

    def test_matched_anchor_is_bounded_and_redacted(self):
        secret = f"# note\napi_key = '{TestQuarantine.SECRET_SK}'\n"
        masked = WR.redact_snippet(secret, window=LC.SNIPPET_WINDOW_CHARS)
        self.assertLessEqual(len(masked), LC.SNIPPET_WINDOW_CHARS)
        self.assertNotIn(TestQuarantine.SECRET_SK, masked)
        self.assertIn("[REDACTED", masked)


if __name__ == "__main__":
    unittest.main()
