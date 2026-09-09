"""Tests for the active knowledge-model contribution CLI."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from executors.contribute.run import (  # noqa: E402
    _build_knowledge_model_envelope,
    build_parser,
    main,
)


class ParserTests(unittest.TestCase):
    def test_active_types_are_exposed(self):
        action = build_parser()._option_string_actions["--type"]
        self.assertEqual(set(action.choices), {
            "submit-resource", "propose-revision", "decide-revision",
            "mark-canonical", "capture-message", "evidence",
        })

    def test_legacy_distillation_type_is_rejected(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["--type", "distillation", "--idempotency-token", "x"])


class EnvelopeTests(unittest.TestCase):
    def _args(self, *args: str):
        return build_parser().parse_args(["--type", *args])

    def test_submit_resource_reads_body_and_references(self):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as f:
            f.write("See [resource:42@7]")
            path = f.name
        try:
            args = build_parser().parse_args([
                "--type", "submit-resource", "--idempotency-token", "retry-1",
                "--kind", "guide", "--title", "Guide", "--body-file", path,
                "--source", "web", "--external-id", "guide-1",
            ])
            envelope = _build_knowledge_model_envelope(args)
        finally:
            Path(path).unlink()
        self.assertEqual(envelope["action"], "submit_resource")
        self.assertEqual(envelope["data"]["idempotency_token"], "retry-1")
        self.assertEqual(envelope["data"]["origin_source"], "web")
        self.assertEqual(envelope["data"]["origin_external_id"], "guide-1")
        self.assertEqual(envelope["data"]["references"][0]["target_kind"], "resource")
        self.assertEqual(envelope["data"]["references"][0]["target_version_id"], "7")

    def test_propose_revision_has_explicit_base(self):
        args = build_parser().parse_args([
            "--type", "propose-revision", "--idempotency-token", "retry-2",
            "--resource-id", "42", "--base-revision-id", "7",
            "--kind", "workflow", "--title", "Candidate",
        ])
        data = _build_knowledge_model_envelope(args)["data"]
        self.assertEqual(data["resource_id"], "42")
        self.assertEqual(data["base_revision_id"], "7")

    def test_evidence_and_decision_actions(self):
        evidence = _build_knowledge_model_envelope(build_parser().parse_args([
            "--type", "evidence", "--idempotency-token", "e-1",
            "--claim", "claim", "--reported-result", "result", "--basis", "reported",
        ]))
        decision = _build_knowledge_model_envelope(build_parser().parse_args([
            "--type", "decide-revision", "--idempotency-token", "d-1",
            "--revision-id", "8", "--decision", "accepted",
        ]))
        self.assertEqual(evidence["action"], "submit_evidence")
        self.assertEqual(decision["action"], "decide_revision")


class MainTests(unittest.TestCase):
    def test_dry_run_requires_no_key(self):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as f:
            f.write("Guide body")
            body = f.name
        try:
            with unittest.mock.patch.dict("os.environ", {}, clear=True):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                    rc = main([
                        "--type", "submit-resource", "--idempotency-token", "retry-1",
                        "--kind", "guide", "--title", "Guide", "--body-file", body,
                        "--dry-run",
                    ])
            self.assertEqual(rc, 0)
            payload = json.loads(out.getvalue())
            self.assertTrue(payload["dry_run"])
            self.assertEqual(payload["envelope"]["action"], "submit_resource")
        finally:
            Path(body).unlink()

    def test_missing_idempotency_token_is_reported(self):
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = main(["--type", "mark-canonical", "--resource-id", "42", "--dry-run"])
        self.assertEqual(rc, 1)
        self.assertIn("idempotency-token", json.loads(out.getvalue())["error"])


if __name__ == "__main__":
    unittest.main()
