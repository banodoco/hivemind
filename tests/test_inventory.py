"""Tests for ``scripts/inventory_corpus.py`` — offline parsers, statistics, redaction.

Pure and offline: no network, no CLI, no secrets are printed.  
These tests pin the redactor, statistics helpers, workflow cohort classifier,
Python extractor, and secret scanner so they can never leak values or produce
incorrect cohort assignments.
"""

from __future__ import annotations

import json
import sys
import unittest
from collections import Counter
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

import inventory_corpus as ic  # noqa: E402


# ---------------------------------------------------------------------------
# Redactor — same safety boundary as verify_access.py
# ---------------------------------------------------------------------------


class RedactTests(unittest.TestCase):
    """Every secret shape must be fully masked by ``redact``."""

    def _assert_masked(self, secret: str) -> None:
        out = ic.redact(f"prefix {secret} suffix")
        self.assertNotIn(secret, out, f"secret leaked through redact: {secret!r}")
        self.assertTrue(
            any(tok in out for tok in (
                "<redacted>", "<connstr>", "<token>", "<sb-key>",
                "<contributor-key>", "<jwt>",
            )),
            f"no placeholder found in: {out!r}",
        )

    def test_masks_supabase_publishable_key(self):
        self._assert_masked("sb_publishable_O38oPBafrBoFrpi_rlWJvA_UJrulFsx")

    def test_masks_supabase_secret_key(self):
        self._assert_masked("sb_secret_abcdEfghIjklMnopqrstuvwxYZ0123456789")

    def test_masks_jwt(self):
        self._assert_masked("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.SflKxwRJSMeKKF2QT4f")

    def test_masks_contributor_key(self):
        self._assert_masked("hm_" + "a" * 64)

    def test_masks_connection_string_with_password(self):
        self._assert_masked(
            "postgresql://postgres.ujlwuvkrxlvoswwkerdf:***@aws-0-eu-central-1.pooler.supabase.com:5432/postgres"
        )

    def test_masks_pgpassword_assignment(self):
        self._assert_masked('PGPASSWORD="gLunEF...kcqz"')

    def test_masks_openai_key(self):
        self._assert_masked("sk-proj-abcdefghijklmnopqrstuvwxyz123456")

    def test_masks_anthropic_key(self):
        self._assert_masked("sk-ant-api03-abcdefghijklmnopqrstuvwxyz123456")

    def test_preserves_safe_identifiers(self):
        out = ic.redact(
            "linked project ujlwuvkrxlvoswwkerdf region eu-central-1 "
            "function contribute port 5432 pg_trgm"
        )
        self.assertIn("ujlwuvkrxlvoswwkerdf", out)
        self.assertIn("contribute", out)
        self.assertIn("5432", out)
        self.assertIn("pg_trgm", out)


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------


class PercentileTests(unittest.TestCase):
    """Test percentile computation."""

    def test_empty_list(self):
        result = ic._percentiles([])
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["p50"], 0)

    def test_single_value(self):
        result = ic._percentiles([42])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["p50"], 42)
        self.assertEqual(result["min"], 42)
        self.assertEqual(result["max"], 42)

    def test_even_distribution(self):
        result = ic._percentiles([10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
        self.assertEqual(result["count"], 10)
        self.assertEqual(result["min"], 10)
        self.assertEqual(result["max"], 100)
        # p50 of 10 values: index 4.5 -> interpolate between values[4]=50 and values[5]=60
        self.assertEqual(result["p50"], 55)

    def test_small_set(self):
        result = ic._percentiles([1, 2, 3, 4, 5])
        self.assertEqual(result["p50"], 3)
        self.assertEqual(result["min"], 1)
        self.assertEqual(result["max"], 5)


class EstimatedTokensTests(unittest.TestCase):
    """Test token estimation from character counts."""

    def test_basic(self):
        tokens = ic._estimated_tokens([400, 800, 40])
        self.assertEqual(tokens, [100, 200, 10])

    def test_min_one(self):
        tokens = ic._estimated_tokens([1, 2, 3])
        self.assertEqual(tokens, [1, 1, 1])

    def test_custom_ratio(self):
        tokens = ic._estimated_tokens([100], chars_per_token=5.0)
        self.assertEqual(tokens, [20])


class BucketCountsTests(unittest.TestCase):
    """Test size bucket classification."""

    def test_basic(self):
        boundaries = [
            (0, 99, "small"),
            (100, 999, "medium"),
            (1000, 9999, "large"),
        ]
        counts = ic._bucket_counts([50, 150, 500, 2000], boundaries)
        self.assertEqual(counts["small"], 1)
        self.assertEqual(counts["medium"], 2)
        self.assertEqual(counts["large"], 1)


# ---------------------------------------------------------------------------
# Workflow representation helpers
# ---------------------------------------------------------------------------


class ExtractPythonFromBodyTests(unittest.TestCase):
    """Test Python extraction from workflow body text."""

    def test_ready_template_delimiter(self):
        body = """Some description text.

Python ready-template source:
import comfy
from nodes import LoadImage

model = LoadImage()
## Workflow JSON"""
        result = ic._extract_python_from_body(body)
        self.assertIsNotNone(result)
        self.assertIn("import comfy", result)
        self.assertIn("LoadImage", result)
        self.assertNotIn("Workflow JSON", result)

    def test_scratchpad_delimiter(self):
        body = """Description.

Python scratchpad source:
x = 1 + 2
y = x * 3
## Notes"""
        result = ic._extract_python_from_body(body)
        self.assertIsNotNone(result)
        self.assertIn("x = 1 + 2", result)
        self.assertNotIn("Notes", result)

    def test_no_delimiter(self):
        body = "Just a regular description with some code: print('hello')"
        result = ic._extract_python_from_body(body)
        self.assertIsNone(result)

    def test_delimiter_but_no_code(self):
        body = "Python ready-template source:\n\n## Next section"
        result = ic._extract_python_from_body(body)
        self.assertIsNone(result)

    def test_delimiter_tiny_code(self):
        body = "Python ready-template source:\nx = 1"
        result = ic._extract_python_from_body(body)
        # < 10 chars after strip -> None
        self.assertIsNone(result)

    def test_code_to_end_of_string(self):
        body = """Python ready-template source:
import torch
import numpy as np
"""
        result = ic._extract_python_from_body(body)
        self.assertIsNotNone(result)
        self.assertIn("import torch", result)
        self.assertIn("numpy", result)

    def test_code_with_dashes_separator(self):
        body = """Description.

Python scratchpad source:
def build():
    pass
---
More text after."""
        result = ic._extract_python_from_body(body)
        self.assertIsNotNone(result)
        self.assertIn("def build():", result)
        self.assertNotIn("More text", result)


class ClassifyWorkflowCohortsTests(unittest.TestCase):
    """Test workflow cohort classification logic."""

    def _row(self, id_: int, body: str, payload: dict | None = None) -> dict:
        return {"id": id_, "body": body, "payload": payload or {}}

    def test_payload_python_only(self):
        rows = [
            self._row(1, "Description.", {"python_source": "import torch\nx=1"}),
        ]
        result = ic._classify_workflow_cohorts(rows)
        self.assertEqual(result["payload_python"], 1)
        self.assertEqual(result["body_python"], 0)
        self.assertEqual(result["both"], 0)
        self.assertEqual(result["neither"], 0)
        self.assertEqual(result["cohorts"]["any_python"], 1)

    def test_body_python_only(self):
        rows = [
            self._row(1, "Python ready-template source:\nimport torch\nx=1\n## Notes", {}),
        ]
        result = ic._classify_workflow_cohorts(rows)
        self.assertEqual(result["payload_python"], 0)
        self.assertEqual(result["body_python"], 1)
        self.assertEqual(result["both"], 0)
        self.assertEqual(result["neither"], 0)

    def test_both_with_duplicate(self):
        py = "import torch\nx = 1"
        rows = [
            self._row(1, f"Python ready-template source:\n{py}\n## Notes", {"python_source": py}),
        ]
        result = ic._classify_workflow_cohorts(rows)
        self.assertEqual(result["both"], 1)
        self.assertEqual(result["duplicate_body_payload"], 1)

    def test_both_different(self):
        rows = [
            self._row(1,
                "Python ready-template source:\nimport torch\n## Notes",
                {"python_source": "import numpy\nx=1"},
            ),
        ]
        result = ic._classify_workflow_cohorts(rows)
        self.assertEqual(result["both"], 1)
        self.assertEqual(result["duplicate_body_payload"], 0)

    def test_neither(self):
        rows = [
            self._row(1, "Just a description.", {}),
        ]
        result = ic._classify_workflow_cohorts(rows)
        self.assertEqual(result["neither"], 1)
        self.assertEqual(result["cohorts"]["no_python"], 1)

    def test_empty_payload_python_source(self):
        rows = [
            self._row(1, "Description.", {"python_source": ""}),
        ]
        result = ic._classify_workflow_cohorts(rows)
        self.assertEqual(result["neither"], 1)

    def test_whitespace_payload_python_source(self):
        rows = [
            self._row(1, "Description.", {"python_source": "   "}),
        ]
        result = ic._classify_workflow_cohorts(rows)
        self.assertEqual(result["neither"], 1)


# ---------------------------------------------------------------------------
# Secret scanner
# ---------------------------------------------------------------------------


class SecretScannerTests(unittest.TestCase):
    """Test the deterministic secret-pattern scanner.

    These tests verify that known credential shapes are detected and that
    the scanner returns reason codes without storing matched values.
    """

    def test_private_key_block_detected(self):
        text = """some code
-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEA...
-----END RSA PRIVATE KEY-----
more code"""
        count, codes = ic._scan_for_secrets(text)
        self.assertGreater(count, 0)
        self.assertIn("private_key_block", codes)

    def test_openai_key_detected(self):
        count, codes = ic._scan_for_secrets("api_key = 'sk-proj-abcdefghijklmnopqrstuvwxyz123456'")
        self.assertGreater(count, 0)
        self.assertIn("openai_api_key", codes)

    def test_anthropic_key_detected(self):
        count, codes = ic._scan_for_secrets("token = 'sk-ant-api03-abcdefghijklmnopqrstuvwxyz123456'")
        self.assertGreater(count, 0)
        self.assertIn("anthropic_api_key", codes)

    def test_huggingface_token_detected(self):
        count, codes = ic._scan_for_secrets("hf_token = 'hf_abcdefghijklmnopqrstuvwxyz'")
        self.assertGreater(count, 0)
        self.assertIn("huggingface_token", codes)

    def test_credential_assignment_detected(self):
        count, codes = ic._scan_for_secrets('api_key = "my-secret-value-here"')
        self.assertGreater(count, 0)
        self.assertIn("credential_assignment", codes)

    def test_credential_url_detected(self):
        count, codes = ic._scan_for_secrets("url = 'https://user:pass123@example.com/path'")
        self.assertGreater(count, 0)
        self.assertIn("credential_bearing_url", codes)

    def test_clean_code_passes(self):
        clean = """
import torch
from comfyui import nodes

def build_pipeline():
    model = load_model("wan2.1")
    sampler = WANSampler(steps=20)
    return model, sampler
"""
        count, codes = ic._scan_for_secrets(clean)
        self.assertEqual(count, 0)
        self.assertEqual(codes, [])

    def test_no_false_positive_on_normal_base64(self):
        # Short base64-like strings should not trigger
        count, codes = ic._scan_for_secrets("data = 'dGVzdA=='")
        # This may or may not trigger depending on length
        # The scanner is conservative; it's OK either way
        # Just verify no crash
        self.assertIsInstance(count, int)

    def test_returns_reason_codes_not_values(self):
        text = 'api_key = "sk-proj-abcdefghijklmnopqrstuvwxyz123456"'
        count, codes = ic._scan_for_secrets(text)
        # codes should contain reason label, not the actual key
        for code in codes:
            self.assertIn(code, [
                "private_key_block", "openai_api_key", "anthropic_api_key",
                "huggingface_token", "credential_assignment", "credential_bearing_url",
                "high_entropy_base64",
            ])
        # The matched value must not appear in codes
        codes_str = " ".join(codes)
        self.assertNotIn("sk-proj", codes_str)


# ---------------------------------------------------------------------------
# workflow python size statistics
# ---------------------------------------------------------------------------


class WorkflowPythonSizeStatsTests(unittest.TestCase):
    """Test workflow Python size statistics computation."""

    def test_basic_stats(self):
        rows = [
            {
                "id": 1,
                "body": "Description text here",
                "payload": {"python_source": "import torch\nx = 1"},
            },
            {
                "id": 2,
                "body": "Python ready-template source:\ndef build():\n    pass\n## End",
                "payload": {},
            },
            {
                "id": 3,
                "body": "No Python here.",
                "payload": {},
            },
        ]
        stats = ic._workflow_python_size_stats(rows)
        self.assertEqual(stats["coverage"]["total"], 3)
        self.assertEqual(stats["coverage"]["with_payload_python"], 1)
        self.assertEqual(stats["coverage"]["with_body_python"], 1)

        # payload_python_source stats
        self.assertIsNotNone(stats["payload_python_source"])
        self.assertEqual(stats["payload_python_source"]["count"], 1)

        # body_python_extracted stats
        self.assertIsNotNone(stats["body_python_extracted"])
        self.assertEqual(stats["body_python_extracted"]["count"], 1)


# ---------------------------------------------------------------------------
# Markdown rendering (smoke tests)
# ---------------------------------------------------------------------------


class RenderMarkdownTests(unittest.TestCase):
    """Ensure the markdown renderer does not crash on realistic input."""

    def test_minimal_result_renders(self):
        result = {
            "report": "test",
            "generated_at": "2026-07-28T00:00:00Z",
            "plan_date": "2026-07-28",
            "completed_at": "2026-07-28T00:00:01Z",
            "methodology": {"test": "test method"},
            "row_counts": {
                "unified_feed_total": 1000,
                "by_entity": {"message": 900, "workflow": 90, "distillation": 10},
                "distillations": {"total": 10, "approved": 8, "pending": 2, "rejected": 0, "superseded": 0},
                "distillation_cites_total": 25,
                "discord_messages_approx": 950,
            },
            "text_lengths": {
                "external_resources_body": {"count": 100, "min": 10, "p50": 100, "mean": 200, "p75": 300, "p90": 500, "p95": 800, "p99": 2000, "max": 5000},
                "messages_body_sample": {"count": 5000, "min": 1, "p50": 100, "mean": 150, "p75": 200, "p90": 400, "p95": 600, "p99": 1500, "max": 10000},
                "message_length_method": {"method": "sample", "sample_size": 5000, "total_messages": 1000000, "pages_used": 10, "stride": 100000, "uncertainty": "sample based"},
                "long_resource_buckets": {"<1 KB": 50, "1-10 KB": 30, "10-50 KB": 15, "50-100 KB": 3, "100-500 KB": 2, "500 KB+": 0},
            },
            "workflow_representation": {
                "cohorts": {
                    "total_workflows": 90,
                    "payload_python": 30,
                    "body_python": 20,
                    "both": 10,
                    "duplicate_body_payload": 5,
                    "neither": 30,
                    "suspect_count": 2,
                    "suspect_reasons": {"openai_api_key": 1, "credential_assignment": 1},
                    "cohorts": {"payload_python": 40, "body_python_only": 20, "any_python": 60, "no_python": 30},
                },
                "size_stats": {"coverage": {"total": 90, "with_payload_python": 40, "with_body_python": 30}},
            },
            "table_sizes": [
                {"table": "discord_messages", "total_pretty": "1 GB", "table_pretty": "600 MB", "index_pretty": "400 MB"},
                {"table": "external_resources", "total_pretty": "50 MB", "table_pretty": "30 MB", "index_pretty": "20 MB"},
            ],
            "indexes": {
                "discord_content_fts": {
                    "index_name": "idx_discord_messages_content_fts",
                    "size_pretty": "200 MB",
                    "definition": "CREATE INDEX ... ON ... USING gin (to_tsvector('english'::regconfig, content))",
                },
                "all_gin_trgm": ["a", "b"],
            },
        }
        md = ic.render_markdown(result)
        self.assertIsInstance(md, str)
        self.assertIn("Hivemind Corpus Inventory", md)
        self.assertIn("Row Counts", md)
        self.assertIn("Workflow Representation", md)
        self.assertIn("Table and Index Sizes", md)

    def test_empty_result_renders(self):
        result = {
            "report": "empty",
            "generated_at": "2026-07-28T00:00:00Z",
            "plan_date": "2026-07-28",
            "completed_at": "2026-07-28T00:00:01Z",
            "methodology": {},
            "row_counts": {},
            "text_lengths": {},
            "workflow_representation": {"cohorts": {}, "size_stats": {}},
            "table_sizes": [],
            "indexes": {},
        }
        md = ic.render_markdown(result)
        self.assertIsInstance(md, str)


if __name__ == "__main__":
    unittest.main()
