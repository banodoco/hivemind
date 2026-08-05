"""Tests for the task-2.1 authorized-provider smoke driver and secret boundary.

Pure and offline: no network, no real provider call, no real key. These pin the
sanitization contract of ``scripts/smoke_embedding_provider.run_smoke`` (the
unit-testable core of the one authorized live smoke) and the secret-safe
``scripts.register_embedding_secret`` parser:

  * a successful smoke records exactly the allowed, sanitized fields;
  * the embedding vector and the credential are NEVER present in the evidence,
    even when a recognizable vector / a throwaway ``sk-`` key are used;
  * the verification booleans (dimension / finite / L2) drive ``request_success``
    honestly for success, wrong-dimension, non-finite, and no-credential cases;
  * the ``_write`` guard refuses to persist any numeric-list (vector) value;
  * the ``.env`` parser extracts ONLY ``OPENAI_API_KEY`` and never prints it.

A skip-if-present local-evidence check documents the sanitization expectation for
the real recorded artifact (the live smoke file lives outside source control).
"""

from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))

import scripts.smoke_embedding_provider as smoke_drv  # noqa: E402
import scripts.register_embedding_secret as reg  # noqa: E402
from executors import embedding_contract as ec  # noqa: E402

#: A throwaway key used only to PROVE the evidence never contains it.
_FAKE_KEY = "sk-test-DO-NOT-USE-AAAAAAAAAAAAAAAAAA"
#: A distinctive vector value used only to PROVE the evidence never persists it.
_MARKER = "0.123456789012345"


def _unit_vector(dim: int, marker_first: bool = True) -> list[float]:
    v = [0.0] * dim
    v[0] = 0.123456789012345 if marker_first else 0.5
    v[1] = math.sqrt(max(0.0, 1.0 - v[0] * v[0]))
    return v


def _fake_transport(dim: int, *, marker: bool = True, finite: bool = True,
                    wrong_dim: bool = False):
    """Deterministic transport: returns one vector, mimicking the provider shape."""

    def transport(url, headers, body):  # noqa: ANN001 — matches ec.Transport
        d = dim if not wrong_dim else dim + 100
        vec = _unit_vector(d, marker_first=marker)
        if not finite:
            vec = list(vec)
            vec[2] = float("nan")
        return {"data": [{"index": 0, "embedding": vec}]}
    return transport


class RunSmokeSanitizationTests(unittest.TestCase):
    def test_success_records_allowed_fields_only(self):
        ev = smoke_drv.run_smoke(
            api_key=_FAKE_KEY, transport=_fake_transport(384), dimension=384)
        self.assertTrue(ev["request_success"])
        self.assertTrue(ev["dimension_matches_configured"])
        self.assertTrue(ev["all_finite"])
        self.assertTrue(ev["l2_norm_within_tolerance"])
        self.assertEqual(ev["dimension"], 384)
        self.assertEqual(ev["provider"], "openai")
        self.assertEqual(ev["model"], ec.DEFAULT_OPENAI_EMBEDDING_MODEL)
        self.assertIsNone(ev["error_state"])
        self.assertFalse(ev["vector_persisted"])
        self.assertFalse(ev["input_text_persisted"])

    def test_vector_value_never_persisted(self):
        ev = smoke_drv.run_smoke(
            api_key=_FAKE_KEY, transport=_fake_transport(384), dimension=384)
        blob = json.dumps(ev)
        self.assertNotIn(_MARKER, blob, "embedding vector value leaked into evidence")

    def test_credential_never_persisted(self):
        ev = smoke_drv.run_smoke(
            api_key=_FAKE_KEY, transport=_fake_transport(384), dimension=384)
        blob = json.dumps(ev)
        self.assertNotIn(_FAKE_KEY, blob, "api key leaked into evidence")
        self.assertNotIn("sk-test", blob, "api key prefix leaked into evidence")

    def test_evidence_has_no_vector_typed_value(self):
        ev = smoke_drv.run_smoke(
            api_key=_FAKE_KEY, transport=_fake_transport(384), dimension=384)
        self.assertFalse(smoke_drv._contains_vector(ev),
                         "evidence structurally contains a numeric-list vector")


class RunSmokeVerificationTests(unittest.TestCase):
    def test_wrong_dimension_fails(self):
        ev = smoke_drv.run_smoke(
            api_key=_FAKE_KEY, transport=_fake_transport(384, wrong_dim=True),
            dimension=384)
        self.assertFalse(ev["dimension_matches_configured"])
        self.assertFalse(ev["request_success"])

    def test_non_finite_fails(self):
        ev = smoke_drv.run_smoke(
            api_key=_FAKE_KEY, transport=_fake_transport(384, finite=False),
            dimension=384)
        self.assertFalse(ev["all_finite"])
        self.assertFalse(ev["request_success"])

    def test_no_credential_fails_closed_with_redacted_error(self):
        ev = smoke_drv.run_smoke(api_key=None, dimension=384)
        self.assertFalse(ev["embedder_has_credential"])
        self.assertFalse(ev["request_success"])
        self.assertIsNotNone(ev["error_state"])
        # Error must never echo the key or an sk- prefix.
        self.assertNotIn("sk-", ev["error_state"] or "")


class WriteGuardTests(unittest.TestCase):
    def test_write_refuses_vector_payload(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ev.json"
            poisoned = {"task": "x", "leak": [0.1, 0.2, 0.3]}
            with self.assertRaises(AssertionError):
                smoke_drv._write(path, poisoned)
            self.assertFalse(path.exists(), "vector payload was written despite the guard")

    def test_write_refuses_key_payload(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ev.json"
            poisoned = {"task": "x", "note": "sk-test-AAAAAAAAAAAAAAAA"}
            with self.assertRaises(AssertionError):
                smoke_drv._write(path, poisoned)

    def test_write_accepts_clean_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ev.json"
            ev = smoke_drv.run_smoke(
                api_key=_FAKE_KEY, transport=_fake_transport(384), dimension=384)
            smoke_drv._write(path, ev)
            self.assertTrue(path.exists())
            on_disk = path.read_text()
            self.assertNotIn(_MARKER, on_disk)
            self.assertNotIn("sk-test", on_disk)


class RedactorTests(unittest.TestCase):
    def test_redact_strips_secret_tokens(self):
        sk_token = "sk-abcdEFgh1234567890"
        long_token = "z" * 40  # high-entropy, >= 32 chars -> redacted
        out = smoke_drv._redact("error: Bearer %s tail %s" % (sk_token, long_token))
        # The secret VALUES must be gone; the literal scheme word is harmless.
        self.assertNotIn(sk_token, out)
        self.assertNotIn(long_token, out)
        self.assertIn("<redacted>", out)


class RegisterParserTests(unittest.TestCase):
    def test_extracts_plain_key(self):
        self.assertEqual(reg.extract_openai_key("OPENAI_API_KEY=sk-plain-1234567890"),
                         "sk-plain-1234567890")

    def test_extracts_quoted_key(self):
        self.assertEqual(reg.extract_openai_key('OPENAI_API_KEY="sk-quoted-1234567890"'),
                         "sk-quoted-1234567890")

    def test_ignores_comments_and_other_vars(self):
        text = (
            "# comment OPENAI_API_KEY=sk-nope-1234567890\n"
            "OTHER=sk-other-1234567890\n"
            "OPENAI_API_KEY=sk-real-1234567890\n"
        )
        self.assertEqual(reg.extract_openai_key(text), "sk-real-1234567890")

    def test_absent_returns_none(self):
        self.assertIsNone(reg.extract_openai_key("OTHER=value\n"))


class LiveEvidenceArtifactTests(unittest.TestCase):
    """Sanitization expectation for the real recorded live-smoke artifact.

    The artifact lives outside source control; skip if it is not present.
    """

    def test_local_smoke_evidence_is_sanitized_and_successful(self):
        path = _REPO / "docs" / "hybrid-search" / "phase2-embedding-provider-smoke.json"
        if not path.exists():
            self.skipTest("live smoke artifact not present on this machine")
        ev = json.loads(path.read_text())
        self.assertTrue(ev["request_success"], "recorded live smoke did not succeed")
        self.assertTrue(ev["dimension_matches_configured"])
        self.assertTrue(ev["all_finite"])
        self.assertTrue(ev["l2_norm_within_tolerance"])
        blob = json.dumps(ev)
        self.assertNotIn("sk-", blob)
        self.assertFalse(smoke_drv._contains_vector(ev),
                         "recorded live evidence contains a vector")


if __name__ == "__main__":
    unittest.main()
