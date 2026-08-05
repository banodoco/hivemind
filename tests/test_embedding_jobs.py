"""Offline tests for executors.embedding_jobs (plan task 2.7 decision matrix).

Pins the insert/change/delete/soft-delete/status/python/semantic reclassification
rules that the SQL triggers in schema/025 enforce, so Python and SQL agree on
WHICH jobs a source transition enqueues. (The SQL-trigger behavior itself is
proven on an isolated cluster by tests.test_embedding_lifecycle_sql.)
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))

from executors import embedding_jobs as ej  # noqa: E402


class MessageDecisionTests(unittest.TestCase):
    def test_insert_enqueues_embed(self):
        intents = ej.decide_jobs(None, {"message_id": 123, "content": "hi"}, "message", op="insert")
        self.assertEqual([i.job_kind for i in intents], ["embed"])
        self.assertEqual(intents[0].representation_type, "prose")
        self.assertEqual(intents[0].source_op, ej.SOURCE_INSERT)

    def test_insert_of_soft_deleted_message_enqueues_nothing(self):
        intents = ej.decide_jobs(None, {"message_id": 1, "content": "x", "is_deleted": True}, "message", op="insert")
        self.assertEqual(intents, [])

    def test_content_change_enqueues_reembed(self):
        old = {"message_id": 9, "content": "a"}
        new = {"message_id": 9, "content": "b"}
        intents = ej.decide_jobs(old, new, "message", op="update")
        self.assertEqual([i.job_kind for i in intents], ["reembed"])

    def test_metadata_only_update_enqueues_nothing(self):
        old = {"message_id": 9, "content": "a", "reactions": []}
        new = {"message_id": 9, "content": "a", "reactions": [{"x": 1}]}
        self.assertEqual(ej.decide_jobs(old, new, "message", op="update"), [])

    def test_soft_delete_enqueues_drop(self):
        old = {"message_id": 9, "content": "a", "is_deleted": False}
        new = {"message_id": 9, "content": "a", "is_deleted": True}
        intents = ej.decide_jobs(old, new, "message", op="update")
        self.assertEqual([i.job_kind for i in intents], ["drop"])
        self.assertEqual(intents[0].source_op, ej.SOURCE_SOFT_DELETE)

    def test_hard_delete_enqueues_drop(self):
        intents = ej.decide_jobs({"message_id": 9, "content": "a"}, None, "message", op="delete")
        self.assertEqual([i.job_kind for i in intents], ["drop"])
        self.assertEqual(intents[0].source_op, ej.SOURCE_DELETE)


class DistillationDecisionTests(unittest.TestCase):
    def test_insert_approved_enqueues_embed(self):
        intents = ej.decide_jobs(None, {"id": 1, "status": "approved", "question": "q", "answer": "a"}, "distillation", op="insert")
        self.assertEqual([i.job_kind for i in intents], ["embed"])

    def test_insert_rejected_enqueues_nothing(self):
        intents = ej.decide_jobs(None, {"id": 1, "status": "rejected", "question": "q", "answer": "a"}, "distillation", op="insert")
        self.assertEqual(intents, [])

    def test_status_to_rejected_enqueues_drop(self):
        old = {"id": 1, "status": "approved", "question": "q", "answer": "a"}
        new = {"id": 1, "status": "rejected", "question": "q", "answer": "a"}
        intents = ej.decide_jobs(old, new, "distillation", op="update")
        self.assertEqual([i.job_kind for i in intents], ["drop"])
        self.assertEqual(intents[0].source_op, ej.SOURCE_STATUS_CHANGE)

    def test_answer_change_enqueues_reembed(self):
        old = {"id": 1, "status": "approved", "question": "q", "answer": "a"}
        new = {"id": 1, "status": "approved", "question": "q", "answer": "b"}
        intents = ej.decide_jobs(old, new, "distillation", op="update")
        self.assertEqual([i.job_kind for i in intents], ["reembed"])

    def test_rejected_to_approved_enqueues_reembed(self):
        old = {"id": 1, "status": "rejected", "question": "q", "answer": "a"}
        new = {"id": 1, "status": "approved", "question": "q", "answer": "a"}
        intents = ej.decide_jobs(old, new, "distillation", op="update")
        self.assertEqual([i.job_kind for i in intents], ["reembed"])


class ResourceDecisionTests(unittest.TestCase):
    def test_insert_workflow_enqueues_prose_and_python(self):
        row = {"id": 100, "kind": "workflow", "title": "t", "body": "b", "payload": {"python_source": "x=1"}}
        intents = ej.decide_jobs(None, row, "resource", op="insert")
        kinds = sorted(i.job_kind for i in intents)
        reps = sorted(i.representation_type for i in intents)
        self.assertEqual(kinds, ["embed", "embed"])
        self.assertEqual(reps, ["prose", "workflow_python"])

    def test_insert_article_enqueues_prose_only(self):
        row = {"id": 100, "kind": "article", "title": "t", "body": "b"}
        intents = ej.decide_jobs(None, row, "resource", op="insert")
        self.assertEqual([i.representation_type for i in intents], ["prose"])

    def test_payload_python_change_enqueues_python_reembed_not_prose(self):
        old = {"id": 100, "kind": "workflow", "title": "t", "body": "b", "payload": {"python_source": "x=1"}}
        new = {"id": 100, "kind": "workflow", "title": "t", "body": "b", "payload": {"python_source": "x=2"}}
        intents = ej.decide_jobs(old, new, "resource", op="update")
        # Python changed; prose (title/body/metadata/kind) did not -> only python reembed.
        self.assertEqual([(i.representation_type, i.job_kind) for i in intents],
                         [("workflow_python", "reembed")])
        self.assertEqual(intents[0].source_op, ej.SOURCE_PYTHON_CHANGE)

    def test_prose_only_change_does_not_duplicate_code_chunk(self):
        old = {"id": 100, "kind": "workflow", "title": "t", "body": "b", "payload": {"python_source": "x=1"}}
        new = {"id": 100, "kind": "workflow", "title": "t2", "body": "b", "payload": {"python_source": "x=1"}}
        intents = ej.decide_jobs(old, new, "resource", op="update")
        self.assertEqual([(i.representation_type, i.job_kind) for i in intents],
                         [("prose", "reembed")])

    def test_reclassify_away_from_workflow_drops_python(self):
        old = {"id": 100, "kind": "workflow", "title": "t", "body": "b", "payload": {"python_source": "x=1"}}
        new = {"id": 100, "kind": "article", "title": "t", "body": "b", "payload": {"python_source": "x=1"}}
        intents = ej.decide_jobs(old, new, "resource", op="update")
        kinds = {(i.representation_type, i.job_kind) for i in intents}
        self.assertIn(("workflow_python", "drop"), kinds)
        self.assertIn(("prose", "reembed"), kinds)  # kind changed -> prose reembed

    def test_delete_workflow_drops_both_reps(self):
        old = {"id": 100, "kind": "workflow", "title": "t", "body": "b", "payload": {"python_source": "x=1"}}
        intents = ej.decide_jobs(old, None, "resource", op="delete")
        reps = sorted(i.representation_type for i in intents)
        self.assertEqual(reps, ["prose", "workflow_python"])
        self.assertTrue(all(i.job_kind == "drop" for i in intents))


class ApplicableRepsTests(unittest.TestCase):
    def test_workflow_has_both_reps(self):
        self.assertEqual(
            ej.applicable_representations({"kind": "workflow"}, "resource"),
            ["prose", "workflow_python"],
        )

    def test_message_is_prose_only(self):
        self.assertEqual(ej.applicable_representations({}, "message"), ["prose"])


class ValidationTests(unittest.TestCase):
    def test_bad_entity_raises(self):
        with self.assertRaises(ValueError):
            ej.decide_jobs(None, {"id": 1}, "bogus", op="insert")

    def test_bad_op_raises(self):
        with self.assertRaises(ValueError):
            ej.decide_jobs(None, {"id": 1}, "message", op="truncate")

    def test_snowflake_item_id_stringified(self):
        intents = ej.decide_jobs(None, {"message_id": 1234567890123456789, "content": "x"}, "message", op="insert")
        self.assertEqual(intents[0].item_id, "1234567890123456789")


if __name__ == "__main__":
    unittest.main()
