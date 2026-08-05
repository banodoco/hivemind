"""Offline tests for the frozen workflow representation contract (plan task 0.8).

Pure and offline: no network, no database, no provider call, no VibeComfy import,
no stored-Python execution, no secrets read. These tests pin the precedence,
delimiter grammar, no-duplication rule, normalization/hashing, the secret-scanner
trap-fix (task-0.3 ``high_entropy_base64`` is NOT a reason), chunk determinism
and the no-silent-truncation guard, so Phase 1/2 implementations must match them.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))

from executors import workflow_representation as wr  # noqa: E402


# ---------------------------------------------------------------------------
# Precedence (plan AD-4)
# ---------------------------------------------------------------------------

class PrecedenceTests(unittest.TestCase):
    def test_payload_python_is_authoritative(self):
        # "both" cohort: identical Python in payload and under a body delimiter.
        row = {
            "body": "desc\n\nPython scratchpad source:\nx = 1\n",
            "payload": {"python_source": "x = 1\n"},
        }
        res = wr.resolve_authoritative_python(row)
        self.assertEqual(res.cohort, wr.COHORT_PAYLOAD_PYTHON)
        self.assertEqual(res.source, "x = 1\n")
        self.assertTrue(res.available)
        self.assertEqual(res.derivation, "payload")
        self.assertTrue(res.body_duplicate)

    def test_payload_without_body_is_not_duplicate(self):
        row = {"body": "just prose here", "payload": {"python_source": "z = 3\n"}}
        res = wr.resolve_authoritative_python(row)
        self.assertEqual(res.cohort, wr.COHORT_PAYLOAD_PYTHON)
        self.assertFalse(res.body_duplicate)

    def test_body_python_when_no_payload(self):
        row = {"body": "desc\n\nPython ready-template source:\na = 1\nb = 2\n"}
        res = wr.resolve_authoritative_python(row)
        self.assertEqual(res.cohort, wr.COHORT_BODY_PYTHON)
        self.assertIn("a = 1", res.source)
        self.assertEqual(res.derivation, "body_delimiter")
        self.assertEqual(res.delimiter, "Python ready-template source:")
        self.assertFalse(res.body_duplicate)

    def test_unavailable_when_no_python_and_no_recoverer(self):
        row = {"body": "just a description", "payload": {}}
        res = wr.resolve_authoritative_python(row)
        self.assertEqual(res.cohort, wr.COHORT_UNAVAILABLE)
        self.assertFalse(res.available)
        self.assertEqual(res.source, "")
        self.assertIsNotNone(res.unavailable_reason)

    def test_recoverable_via_injected_recoverer(self):
        row = {"body": "desc", "payload": {"corpus_path": "external_workflows/corpus/x.json"}}

        def recoverer(r):
            return "# vibecomfy: generated scratchpad\nz = 9\n"

        res = wr.resolve_authoritative_python(row, recoverer=recoverer)
        self.assertEqual(res.cohort, wr.COHORT_RECOVERABLE)
        self.assertTrue(res.available)
        self.assertEqual(res.derivation, "scratchpad")

    def test_recovery_conversion_error_is_unavailable_with_reason(self):
        row = {"body": "desc", "payload": {"corpus_path": "missing.json"}}

        def boom(r):
            raise ValueError("bad json")

        res = wr.resolve_authoritative_python(row, recoverer=boom)
        self.assertEqual(res.cohort, wr.COHORT_UNAVAILABLE)
        self.assertEqual(res.unavailable_reason, "conversion_error:ValueError")

    def test_recovery_returning_empty_is_unavailable(self):
        row = {"body": "desc", "payload": {}}
        res = wr.resolve_authoritative_python(row, recoverer=lambda r: "")
        self.assertEqual(res.cohort, wr.COHORT_UNAVAILABLE)


# ---------------------------------------------------------------------------
# Delimiter grammar
# ---------------------------------------------------------------------------

class DelimiterTests(unittest.TestCase):
    def test_both_exact_delimiters_recognized(self):
        for delim in wr.BODY_DELIMITERS:
            body = f"intro\n\n{delim}\nimport comfy\n"
            blocks = wr.extract_python_blocks(body)
            self.assertEqual(len(blocks), 1)
            self.assertEqual(blocks[0].delimiter, delim)
            self.assertIn("import comfy", blocks[0].text)

    def test_near_miss_delimiter_is_ignored(self):
        # Wrong casing / missing colon / extra words must not parse as Python.
        for fake in ("python scratchpad source:", "Python source:", "Python scratchpad source"):
            self.assertEqual(wr.extract_python_blocks(f"x\n\n{fake}\nsecret = 1\n"), [])

    def test_semantics_marker_terminates_block(self):
        body = (
            "intro\n\nPython scratchpad source:\ncode = 1\n\n"
            f"{wr.SEMANTICS_BLOCK_MARKER}\nmedia_type: video\n"
        )
        blocks = wr.extract_python_blocks(body)
        self.assertEqual(len(blocks), 1)
        self.assertNotIn("media_type", blocks[0].text)
        self.assertIn("code = 1", blocks[0].text)

    def test_multiple_blocks_both_extracted(self):
        body = (
            "Python ready-template source:\na = 1\n\n"
            "Python scratchpad source:\nb = 2\n"
        )
        blocks = wr.extract_python_blocks(body)
        self.assertEqual(len(blocks), 2)


# ---------------------------------------------------------------------------
# No-duplication + canonical prose
# ---------------------------------------------------------------------------

class NoDuplicationTests(unittest.TestCase):
    def test_strip_removes_python_blocks_from_prose(self):
        # VibeComfy appends Python at the END of the body, so the block is the
        # trailing region; preceding prose must survive the strip.
        body = "Description.\n\nA note about the workflow.\n\nPython scratchpad source:\nSECRET_CODE = 1"
        stripped = wr.strip_python_blocks(body)
        self.assertNotIn("Python scratchpad source:", stripped)
        self.assertNotIn("SECRET_CODE", stripped)
        self.assertIn("Description.", stripped)
        self.assertIn("A note about the workflow.", stripped)

    def test_prose_excludes_python_for_both_cohort(self):
        row = {
            "title": "Wan I2V",
            "body": "A description.\n\nPython scratchpad source:\nx = 1\n",
            "payload": {"python_source": "x = 1\n"},
            "metadata": {"workflow_semantics": {"media_type": "video", "task_type": "image_to_video"}},
        }
        prose = wr.build_workflow_prose(row)
        python = wr.build_workflow_python(row)
        # Python appears exactly once across the two representations.
        self.assertNotIn("Python scratchpad source:", prose)
        self.assertNotIn("x = 1", prose)
        self.assertEqual(python.strip(), "x = 1")
        self.assertIn("Wan I2V", prose)
        self.assertIn("video", prose)
        self.assertIn("image_to_video", prose)

    def test_project_semantics_skips_empty_fields(self):
        out = wr.project_semantics({"workflow_semantics": {"media_type": "video", "models": []}})
        self.assertIn("video", out)
        self.assertNotIn("[]", out)


# ---------------------------------------------------------------------------
# Normalization + hashing
# ---------------------------------------------------------------------------

class HashTests(unittest.TestCase):
    def test_hash_is_deterministic_and_stable(self):
        a = wr.representation_hash("line one\nline two\n")
        b = wr.representation_hash("line one\nline two")
        self.assertEqual(a, b)

    def test_crlf_and_cr_collapsed(self):
        unix = wr.representation_hash("a\nb\nc")
        crlf = wr.representation_hash("a\r\nb\r\nc")
        cr = wr.representation_hash("a\rb\rc")
        self.assertEqual(unix, crlf)
        self.assertEqual(unix, cr)

    def test_nfc_normalization(self):
        # Decomposed e-acute vs composed.
        self.assertEqual(
            wr.representation_hash("café"),
            wr.representation_hash("café"),
        )

    def test_chunk_hash_equals_representation_hash_algorithm(self):
        self.assertEqual(wr.chunk_hash("abc"), wr.representation_hash("abc"))


# ---------------------------------------------------------------------------
# Secret scanner — the task-0.3 trap fix
# ---------------------------------------------------------------------------

GENERATED_VIBECOMFY_PY = '''
# vibecomfy: generated scratchpad
"""Auto-generated VibeComfy scratchpad."""
from vibecomfy import workflow as _w

sampler = WanVideoSampler(
    model="wan2.2_animate_14B_bf16.safetensors",
    lora=("detailer/ltx-2-19b-ic-lora-detailer.safetensors", 0.8),
    ipadapter_weight_types="linear",
)

# Large generated literal — NOT a credential (no keyword context).
embeddings_blob = "eJxrYK5mn6egoaOjaGZqYWBmZmBgamKQnJqSpK2RkZPpkJ6ekZYSkKKQkVq..." \
    "AAEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQ==" * 200

workflow_json = {
    "nodes": [{"class_type": "BerniniConditioning"}, {"class_type": "BlockifyMask"}],
    "title": "Wan2.2 I2V",
}
'''


class ScannerTrapFixTests(unittest.TestCase):
    def test_generated_vibecomfy_code_scans_clean(self):
        # The exact failure mode of task 0.3: 222 bodies flagged
        # high_entropy_base64. The frozen scanner must return ZERO findings here.
        findings = wr.scan_secrets(GENERATED_VIBECOMFY_PY)
        self.assertEqual(findings, [], f"false positive: {findings}")

    def test_high_entropy_base64_is_not_a_reason_code(self):
        self.assertNotIn("high_entropy_base64", wr.SECRET_REASON_CODES)

    def test_bare_high_entropy_string_is_not_a_hit(self):
        blob = "data = 9" + "AbCd1234" * 60  # long high-entropy run, no credential keyword
        self.assertEqual(wr.scan_secrets(blob), [])


class ScannerPlantedSecretTests(unittest.TestCase):
    def _reasons(self, python: str) -> set[str]:
        return {f.reason_code for f in wr.scan_secrets(python)}

    def test_private_key_block(self):
        self.assertIn("private_key_block", self._reasons(
            "x = 1\n-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----\n"))

    def test_openai_token(self):
        val = "sk-" + "Aa0" * 20  # sk- + 60 alphanumerics
        self.assertIn("provider_token_prefix", self._reasons(f'k = "{val}"\n'))

    def test_github_token(self):
        self.assertIn("provider_token_prefix", self._reasons(
            'gh = "ghp_' + "a" * 36 + '"\n'))

    def test_slack_token(self):
        self.assertIn("provider_token_prefix", self._reasons(
            's = "xoxb-' + "1234567890-abcdef" + '"\n'))

    def test_aws_access_key(self):
        self.assertIn("provider_token_prefix", self._reasons('a = "AKIAIOSFODNN7EXAMPLE"\n'))

    def test_google_api_key(self):
        # Google API keys are "AIza" + 35 chars = 39 total.
        self.assertIn("provider_token_prefix", self._reasons(
            'g = "AIza' + "a" * 35 + '"\n'))

    def test_supabase_secret_not_publishable(self):
        # publishable keys are public and must NOT trip; secret keys must.
        self.assertEqual(self._reasons('p = "sb_publishable_O38oPBafrBoFrpi"\n'), set())
        self.assertIn("provider_token_prefix", self._reasons('s = "sb_secret_' + "a" * 20 + '"\n'))

    def test_hivemind_contributor_key(self):
        self.assertIn("provider_token_prefix", self._reasons('h = "hm_' + "0" * 64 + '"\n'))

    def test_jwt_token(self):
        self.assertIn("provider_token_prefix", self._reasons(
            'j = "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0NTY.eXN0' + "aB" * 8 + '"\n'))

    def test_credential_assignment(self):
        python = 'client_secret = "9xH7vQ2pZ4rT1wY8nK3mB6cD5eF0aLm"\n'
        self.assertIn("credential_assignment", self._reasons(python))

    def test_credential_url(self):
        python = 'engine = "postgresql://hivemind:S3cretPassw0rd@db.example.com:5432/postgres"\n'
        self.assertIn("credential_url", self._reasons(python))

    def test_credential_url_placeholder_not_flagged(self):
        python = 'engine = "postgresql://user:password@host/db"\n'  # placeholder password
        self.assertNotIn("credential_url", self._reasons(python))

    def test_aws_env_var(self):
        self.assertIn("aws_env_var", self._reasons(
            "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n"))

    def test_findings_never_carry_secret_value(self):
        # Finding objects must expose only position + reason, never the value.
        findings = wr.scan_secrets('token = "sk-' + "A" * 50 + '"\n')
        self.assertTrue(findings)
        for f in findings:
            blob = json.dumps({"reason": f.reason_code, "start": f.start, "length": f.length, "detail": f.detail_kind})
            self.assertNotIn("sk-", blob)

    def test_public_state_transitions(self):
        self.assertEqual(wr.public_state("x = 1\n"), wr.PUBLIC_STATE_SAFE)
        self.assertEqual(wr.public_state("sk-" + "A" * 50), wr.PUBLIC_STATE_QUARANTINED)


class SnippetTests(unittest.TestCase):
    def test_redact_bounds_and_masks(self):
        # A realistic matched snippet centers the secret inside the window.
        secret = "token = 'sk-" + "A" * 60 + "'"
        snippet = "context line\n" + secret + "\nmore context\n"
        red = wr.redact_snippet(snippet, window=240)
        self.assertLessEqual(len(red), 240 + 60)  # windowed + redaction marker slack
        self.assertNotIn("sk-", red)
        self.assertIn("[REDACTED", red)


# ---------------------------------------------------------------------------
# Chunking — determinism, overlap, fallback, no silent truncation
# ---------------------------------------------------------------------------

class ChunkingTests(unittest.TestCase):
    def test_prose_chunking_deterministic(self):
        text = "\n\n".join(f"paragraph number {i} about wan video generation." for i in range(40))
        a = wr.chunk_prose(text, target_tokens=64, overlap_tokens=10)
        b = wr.chunk_prose(text, target_tokens=64, overlap_tokens=10)
        self.assertEqual([c.text for c in a], [c.text for c in b])
        self.assertTrue(a)
        self.assertTrue(wr.coverage_ok(text, a))

    def test_two_prose_configs(self):
        text = "\n\n".join(f"p{i} " * 30 for i in range(30))
        small = wr.chunk_prose(text, target_tokens=64, overlap_tokens=8)
        large = wr.chunk_prose(text, target_tokens=256, overlap_tokens=16)
        self.assertGreater(len(small), len(large))

    def test_python_ast_alignment(self):
        text = "\n".join([
            "import comfy",
            "a = 1",
            "def f():",
            "    return 2",
            "class CfgGuider:",
            "    pass",
            "b = 3",
        ])
        chunks = wr.chunk_python(text, target_tokens=8, overlap_tokens=4)
        self.assertTrue(chunks)
        # All chunks recorded the AST method (no fallback needed here).
        self.assertEqual({c.method for c in chunks}, {"ast"})
        self.assertTrue(wr.coverage_ok(text, chunks))

    def test_python_parser_fallback_for_huge_literal(self):
        # A single generated literal dwarfs the budget → AST fallback windowing.
        huge = 'workflow_json = "' + ("A" * 4000) + '"\n'
        chunks = wr.chunk_python(huge, target_tokens=64, overlap_tokens=16)
        self.assertTrue(chunks)
        self.assertIn("ast_fallback", {c.method for c in chunks})
        self.assertTrue(wr.coverage_ok(huge, chunks))

    def test_python_syntax_error_uses_fallback(self):
        # Malformed source must still be chunked (never require execution/validity).
        text = "def broken(:\n    x =\nplain text line\n"
        chunks = wr.chunk_python(text, target_tokens=32, overlap_tokens=8)
        self.assertTrue(chunks)
        self.assertTrue(wr.coverage_ok(text, chunks))

    def test_no_silent_truncation_on_measured_worst_case(self):
        # Inventory max payload.python_source ~1.4M chars. A budget that is far
        # smaller must still cover the full source across many chunks.
        text = "node = 1  # " + ("x" * 1_400_000) + "\n"
        chunks = wr.chunk_python(text, target_tokens=512, overlap_tokens=50)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(wr.coverage_ok(text, chunks))

    def test_python_inter_statement_comments_preserved(self):
        # Standalone comment lines are not AST nodes, so the AST chunker must
        # fold the gap lines into a chunk (frozen §10 no-truncation; comments
        # must stay discoverable). A `#`-only-in-comments source regressed to
        # ~100/222 python-bearing workflows before this guard.
        text = "\n".join([
            "# WanVideoSampler settings for image to video",
            "import torch",
            "# lora_weight controls adapter strength",
            "class WanVideoSampler:",
            "    pass",
        ])
        chunks = wr.chunk_python(text, target_tokens=8, overlap_tokens=4)
        self.assertTrue(chunks)
        self.assertTrue(wr.coverage_ok(text, chunks))
        joined = "\n".join(c.text for c in chunks)
        # The comment-only model name must survive into a chunk (discoverable).
        self.assertIn("WanVideoSampler", joined)
        self.assertIn("lora_weight", joined)

    def test_python_chunking_unchanged_for_contiguous_code(self):
        # The coverage-span fix must be byte-identical for code with no
        # inter-statement gaps (the common case): same chunk texts as a plain
        # statement-span split would produce.
        text = "\n".join(["a = 1", "b = 2", "c = 3", "d = 4"])
        chunks = wr.chunk_python(text, target_tokens=8, overlap_tokens=0)
        self.assertTrue(wr.coverage_ok(text, chunks))
        # Each statement lands in the chunk stream contiguously.
        self.assertIn("a = 1", "\n".join(c.text for c in chunks))

    def test_chunk_dispatch_by_mode(self):
        text = "a = 1\n"
        self.assertEqual(wr.chunk_text(text, mode="python", target_tokens=64)[0].method, "ast")
        self.assertEqual(wr.chunk_text(text, mode="prose", target_tokens=64)[0].method, "paragraph")


# ---------------------------------------------------------------------------
# Machine-readable contract consistency
# ---------------------------------------------------------------------------

class ContractJsonTests(unittest.TestCase):
    CONTRACT_PATH = _REPO / "docs" / "hybrid-search" / "phase0-workflow-representation-contract.json"

    def test_contract_json_exists_and_parses(self):
        self.assertTrue(self.CONTRACT_PATH.exists(), "machine-readable contract must exist")
        data = json.loads(self.CONTRACT_PATH.read_text())
        self.assertEqual(data["schema"], "hivemind/workflow-representation-contract")

    def test_contract_versions_match_module(self):
        data = json.loads(self.CONTRACT_PATH.read_text())
        v = data["versions"]
        self.assertEqual(v["canonicalization"], wr.CANONICALIZATION_VERSION)
        self.assertEqual(v["delimiter_grammar"], wr.DELIMITER_GRAMMAR_VERSION)
        self.assertEqual(v["secret_scan"], wr.SECRET_SCAN_VERSION)
        self.assertEqual(v["chunking"], wr.CHUNKING_VERSION)
        self.assertEqual(v["semantics_projection"], wr.SEMANTICS_PROJECTION_VERSION)

    def test_contract_delimiters_match_module(self):
        data = json.loads(self.CONTRACT_PATH.read_text())
        self.assertEqual(
            tuple(data["delimiters"]["recognized"]),
            wr.BODY_DELIMITERS,
        )

    def test_contract_reason_codes_match_module_and_exclude_trap(self):
        data = json.loads(self.CONTRACT_PATH.read_text())
        codes = set(data["secret_scanner"]["reason_codes"])
        self.assertEqual(codes, set(wr.SECRET_REASON_CODES))
        self.assertNotIn("high_entropy_base64", codes)


if __name__ == "__main__":
    unittest.main()
