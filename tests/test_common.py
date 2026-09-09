"""Offline tests for the active executor boundary helpers."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from executors._common import (  # noqa: E402
    build_knowledge_model_envelope,
    build_submit_resource_envelope,
    dry_run_output,
    format_error,
    output_json,
    postgrest_get,
    read_body_file,
    resolve_anon_key,
    resolve_contribute_url,
    resolve_contributor_key,
    resolve_endpoint,
    truncate_body,
)


class EnvelopeTests(unittest.TestCase):
    def test_submit_resource_adds_deterministic_retry_token_without_mutating_input(self):
        data = {
            "kind": "article",
            "origin_source": "web",
            "origin_external_id": "https://example.test/a",
            "title": "Title",
            "body": "Body",
        }
        original = dict(data)
        envelope = build_submit_resource_envelope(data)
        self.assertEqual(envelope["action"], "submit_resource")
        self.assertRegex(
            envelope["data"]["idempotency_token"],
            r"^ingest:web:[0-9a-f]{16}:[0-9a-f]{32}$",
        )
        self.assertEqual(envelope["data"]["kind"], "article")
        self.assertEqual(data, original)

    def test_submit_resource_honors_explicit_retry_token(self):
        envelope = build_submit_resource_envelope({"kind": "guide"}, idempotency_token="retry-7")
        self.assertEqual(envelope, {"action": "submit_resource", "data": {
            "idempotency_token": "retry-7", "kind": "guide",
        }})

    def test_generic_knowledge_envelope_is_exact(self):
        self.assertEqual(
            build_knowledge_model_envelope("propose_revision", {"idempotency_token": "r"}),
            {"action": "propose_revision", "data": {"idempotency_token": "r"}},
        )

    def test_envelope_rejects_bad_inputs(self):
        with self.assertRaises(ValueError):
            build_knowledge_model_envelope("", {})
        with self.assertRaises(ValueError):
            build_submit_resource_envelope([])  # type: ignore[arg-type]


class UtilityTests(unittest.TestCase):
    def test_truncate_body(self):
        self.assertEqual(truncate_body("hello"), {"body": "hello", "truncated": False})
        self.assertEqual(truncate_body("x" * 8, max_chars=5), {"body": "xxxxx", "truncated": True})

    def test_format_error(self):
        self.assertIn("400 validation error", format_error(400, {"detail": "bad"}))
        self.assertIn("401 unauthorized", format_error(401, {}))
        self.assertIn("409 duplicate", format_error(409, {"existing_id": "7"}))
        self.assertIn("500 internal", format_error(500, {}))

    def test_output_and_dry_run_output(self):
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            output_json({"ok": True})
        self.assertEqual(json.loads(out.getvalue()), {"ok": True})
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            dry_run_output({"action": "submit_resource", "data": {}})
        self.assertEqual(json.loads(out.getvalue())["dry_run"], True)

    def test_read_body_file(self):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as f:
            f.write("payload")
            path = f.name
        try:
            self.assertEqual(read_body_file(path), "payload")
        finally:
            os.unlink(path)


class ResolutionTests(unittest.TestCase):
    def test_environment_resolution(self):
        with unittest.mock.patch.dict(os.environ, {
            "HIVEMIND_API_URL": "https://api.test/",
            "HIVEMIND_CONTRIBUTE_URL": "https://edge.test/",
            "HIVEMIND_ANON_KEY": "anon",
        }, clear=True):
            self.assertEqual(resolve_endpoint(), "https://api.test")
            self.assertEqual(resolve_contribute_url(), "https://edge.test")
            self.assertEqual(resolve_anon_key(), "anon")

    def test_contributor_key_environment_wins(self):
        with unittest.mock.patch.dict(os.environ, {"HIVEMIND_CONTRIBUTOR_KEY": "hm_test"}, clear=True):
            self.assertEqual(resolve_contributor_key(), "hm_test")


class PostgrestTests(unittest.TestCase):
    def test_postgrest_get_constructs_safe_request(self):
        captured = {}

        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return b"[{\"id\":\"7\"}]"

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.headers)
            return Response()

        with unittest.mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = postgrest_get("resources", {"id": "eq.7", "limit": "1"}, endpoint="https://api.test", anon_key="anon")
        self.assertEqual(result, [{"id": "7"}])
        self.assertIn("resources?", captured["url"])
        self.assertIn("eq.7", captured["url"])
        self.assertEqual(captured["headers"]["Apikey"], "anon")


if __name__ == "__main__":
    unittest.main()
