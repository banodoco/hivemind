"""Tests for the shared ``executors/_common.py`` helper module.

All tests are pure — no network, no environment mutations that persist
across tests, no live HTTP calls.  The URL-fetching wrappers are tested
via the stdlib ``unittest.mock``.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
import urllib.error
from pathlib import Path


# -- Ensure the executors package is importable --------------------------------
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))

from executors._common import (  # noqa: E402
    build_add_resource_envelope,
    build_submit_distillation_envelope,
    dry_run_output,
    edge_post,
    format_error,
    output_json,
    parse_cites,
    postgrest_get,
    read_body_file,
    resolve_anon_key,
    resolve_contribute_url,
    resolve_contributor_key,
    resolve_endpoint,
    truncate_body,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class EnvelopeTests(unittest.TestCase):
    """Envelope builder output shape tests."""

    def test_add_resource_envelope_minimal(self):
        data = {
            "kind": "article",
            "source": "web",
            "title": "Test Title",
            "body": "Test body content.",
        }
        envelope = build_add_resource_envelope(data)
        self.assertEqual(envelope["action"], "add_resource")
        self.assertEqual(envelope["data"], data)

    def test_add_resource_envelope_full(self):
        data = {
            "kind": "transcript",
            "source": "youtube",
            "title": "Video Title",
            "body": "Full transcript text here...",
            "external_id": "abc123",
            "author": "Channel Name",
            "url": "https://youtube.com/watch?v=abc123",
            "metadata": {"duration": 3600, "channel": "TestChannel"},
            "payload": {"raw": {"key": "value"}},
        }
        envelope = build_add_resource_envelope(data)
        self.assertEqual(envelope["action"], "add_resource")
        self.assertDictEqual(envelope["data"], data)

    def test_submit_distillation_envelope_minimal(self):
        data = {
            "question": "What is the best upscale model?",
            "answer": "4x-UltraSharp for anime.",
            "confidence": "high",
            "cites": [{"item_kind": "message", "item_id": "12345"}],
        }
        envelope = build_submit_distillation_envelope(data)
        self.assertEqual(envelope["action"], "submit_distillation")
        self.assertEqual(envelope["data"], data)

    def test_submit_distillation_envelope_full(self):
        data = {
            "question": "What is the best upscale model?",
            "answer": "4x-UltraSharp for anime style, but ESRGAN for photos.",
            "confidence": "high",
            "conditions": "For anime-style video upscaling from 1080p to 4K.",
            "supersedes_id": 5,
            "cites": [
                {"item_kind": "message", "item_id": "88123"},
                {"item_kind": "resource", "item_id": "17"},
                {"item_kind": "distillation", "item_id": "3"},
            ],
        }
        envelope = build_submit_distillation_envelope(data)
        self.assertEqual(envelope["action"], "submit_distillation")
        self.assertDictEqual(envelope["data"], data)

    def test_add_resource_envelope_does_not_mutate_input(self):
        data = {"kind": "note", "source": "cli", "title": "T", "body": "B"}
        original = dict(data)
        build_add_resource_envelope(data)
        self.assertDictEqual(data, original)

    def test_submit_distillation_envelope_does_not_mutate_input(self):
        data = {
            "question": "Q?",
            "answer": "A.",
            "confidence": "low",
            "cites": [{"item_kind": "resource", "item_id": "1"}],
        }
        original = dict(data)
        build_submit_distillation_envelope(data)
        self.assertDictEqual(data, original)


# ---------------------------------------------------------------------------


class CiteParsingTests(unittest.TestCase):
    """parse_cites tests."""

    # -- Happy paths -----------------------------------------------------------

    def test_single_message_cite(self):
        result = parse_cites("message:88123")
        self.assertEqual(result, [{"item_kind": "message", "item_id": "88123"}])

    def test_single_resource_cite(self):
        result = parse_cites("resource:17")
        self.assertEqual(result, [{"item_kind": "resource", "item_id": "17"}])

    def test_single_distillation_cite(self):
        result = parse_cites("distillation:5")
        self.assertEqual(result, [{"item_kind": "distillation", "item_id": "5"}])

    def test_multiple_cites(self):
        result = parse_cites("message:88123,resource:17")
        self.assertEqual(
            result,
            [
                {"item_kind": "message", "item_id": "88123"},
                {"item_kind": "resource", "item_id": "17"},
            ],
        )

    def test_three_cites(self):
        result = parse_cites("message:1,resource:2,distillation:3")
        self.assertEqual(
            result,
            [
                {"item_kind": "message", "item_id": "1"},
                {"item_kind": "resource", "item_id": "2"},
                {"item_kind": "distillation", "item_id": "3"},
            ],
        )

    def test_whitespace_tolerance(self):
        result = parse_cites("  message:88123 , resource:17 ")
        self.assertEqual(
            result,
            [
                {"item_kind": "message", "item_id": "88123"},
                {"item_kind": "resource", "item_id": "17"},
            ],
        )

    def test_large_ids(self):
        result = parse_cites("message:999999999999")
        self.assertEqual(result, [{"item_kind": "message", "item_id": "999999999999"}])

    # -- Error paths -----------------------------------------------------------

    def test_empty_string_raises(self):
        with self.assertRaises(ValueError):
            parse_cites("")

    def test_whitespace_only_raises(self):
        with self.assertRaises(ValueError):
            parse_cites("   ")

    def test_missing_colon_raises(self):
        with self.assertRaises(ValueError) as ctx:
            parse_cites("message88123")
        self.assertIn("invalid cite", str(ctx.exception))

    def test_invalid_kind_raises(self):
        with self.assertRaises(ValueError) as ctx:
            parse_cites("video:88123")
        self.assertIn("invalid cite kind", str(ctx.exception))
        self.assertIn("video", str(ctx.exception))

    def test_non_integer_id_raises(self):
        with self.assertRaises(ValueError) as ctx:
            parse_cites("message:abc")
        self.assertIn("invalid cite id", str(ctx.exception))

    def test_negative_id_raises(self):
        with self.assertRaises(ValueError) as ctx:
            parse_cites("message:-1")
        self.assertIn("invalid cite id", str(ctx.exception))

    def test_zero_id_raises(self):
        with self.assertRaises(ValueError) as ctx:
            parse_cites("message:0")
        self.assertIn("invalid cite id", str(ctx.exception))

    def test_float_id_raises(self):
        with self.assertRaises(ValueError) as ctx:
            parse_cites("message:1.5")
        self.assertIn("invalid cite id", str(ctx.exception))

    def test_mixed_valid_and_invalid_raises(self):
        with self.assertRaises(ValueError):
            parse_cites("message:88123,bad")


# ---------------------------------------------------------------------------


class TruncationTests(unittest.TestCase):
    """truncate_body tests."""

    def test_short_body_not_truncated(self):
        body = "Short text."
        result = truncate_body(body, max_chars=700)
        self.assertEqual(result["body"], body)
        self.assertIs(result["truncated"], False)

    def test_exactly_at_limit_not_truncated(self):
        body = "x" * 700
        result = truncate_body(body, max_chars=700)
        self.assertEqual(result["body"], body)
        self.assertIs(result["truncated"], False)

    def test_over_limit_truncated(self):
        body = "x" * 701
        result = truncate_body(body, max_chars=700)
        self.assertEqual(result["body"], "x" * 700)
        self.assertIs(result["truncated"], True)

    def test_much_longer_body_truncated(self):
        body = "abcdefghij" * 100  # 1000 chars
        result = truncate_body(body, max_chars=700)
        self.assertEqual(len(result["body"]), 700)
        self.assertEqual(result["body"], body[:700])
        self.assertIs(result["truncated"], True)

    def test_empty_body_not_truncated(self):
        result = truncate_body("", max_chars=700)
        self.assertEqual(result["body"], "")
        self.assertIs(result["truncated"], False)

    def test_custom_max_chars(self):
        body = "Hello World! This is a test."
        result = truncate_body(body, max_chars=10)
        self.assertEqual(result["body"], "Hello Worl")
        self.assertIs(result["truncated"], True)

    def test_default_max_chars_is_700(self):
        """truncate_body defaults to 700 chars."""
        body = "x" * 701
        result = truncate_body(body)
        self.assertEqual(len(result["body"]), 700)
        self.assertIs(result["truncated"], True)

    def test_returns_dict_with_expected_keys(self):
        result = truncate_body("hello")
        self.assertIn("body", result)
        self.assertIn("truncated", result)
        self.assertEqual(len(result), 2)

    def test_unicode_body(self):
        body = "🔥" * 800  # emoji, multiple bytes per char
        result = truncate_body(body, max_chars=700)
        self.assertEqual(len(result["body"]), 700)
        self.assertIs(result["truncated"], True)

    def test_unicode_at_limit(self):
        body = "🔥" * 100  # 100 chars
        result = truncate_body(body, max_chars=100)
        self.assertEqual(result["body"], body)
        self.assertIs(result["truncated"], False)


# ---------------------------------------------------------------------------


class ErrorFormattingTests(unittest.TestCase):
    """format_error tests."""

    def test_400_with_detail(self):
        msg = format_error(400, {"error": "validation", "detail": "field 'title' required"})
        self.assertIn("400", msg)
        self.assertIn("field 'title' required", msg)

    def test_400_without_detail(self):
        msg = format_error(400, {})
        self.assertIn("400", msg)
        self.assertIn("bad request", msg)

    def test_401(self):
        msg = format_error(401, {"error": "unauthorized"})
        self.assertIn("401", msg)
        self.assertIn("unauthorized", msg.lower())

    def test_409_with_existing_id(self):
        msg = format_error(
            409,
            {
                "error": "duplicate",
                "existing_id": 42,
                "detail": "similar question exists — extend or supersede it",
            },
        )
        self.assertIn("409", msg)
        self.assertIn("existing_id=42", msg)
        self.assertIn("extend or supersede", msg)

    def test_409_without_existing_id(self):
        msg = format_error(409, {"error": "duplicate"})
        self.assertIn("409", msg)
        self.assertIn("existing_id=?", msg)

    def test_500(self):
        msg = format_error(500, {})
        self.assertIn("500", msg)
        self.assertIn("internal server error", msg.lower())

    def test_unknown_status(self):
        msg = format_error(418, {"teapot": True})
        self.assertIn("418", msg)


# ---------------------------------------------------------------------------


class JSONOutputTests(unittest.TestCase):
    """output_json tests."""

    def test_writes_to_stdout(self):
        data = {"key": "value", "nested": [1, 2, 3]}
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            output_json(data)
        output = mock_stdout.getvalue()
        parsed = json.loads(output)
        self.assertEqual(parsed, data)

    def test_writes_to_file(self):
        data = {"a": 1, "b": 2}
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            output_json(data, out_path=tmp_path)
            with open(tmp_path, "r", encoding="utf-8") as fh:
                parsed = json.load(fh)
            self.assertEqual(parsed, data)
        finally:
            os.unlink(tmp_path)

    def test_creates_parent_directories(self):
        data = {"hello": "world"}
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "sub", "nested", "out.json")
            output_json(data, out_path=out_path)
            self.assertTrue(os.path.isfile(out_path))
            with open(out_path, "r", encoding="utf-8") as fh:
                parsed = json.load(fh)
            self.assertEqual(parsed, data)

    def test_unicode_preserved(self):
        data = {"text": "café 🎉"}
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            output_json(data)
        output = mock_stdout.getvalue()
        self.assertIn("café", output)
        self.assertIn("🎉", output)

    def test_trailing_newline(self):
        data = {"x": 1}
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            output_json(data)
        self.assertTrue(mock_stdout.getvalue().endswith("\n"))


# ---------------------------------------------------------------------------


class DryRunOutputTests(unittest.TestCase):
    """dry_run_output tests."""

    def test_wraps_envelope(self):
        envelope = {"action": "add_resource", "data": {"kind": "article"}}
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            dry_run_output(envelope)
        output = mock_stdout.getvalue()
        parsed = json.loads(output)
        self.assertIs(parsed["dry_run"], True)
        self.assertEqual(parsed["envelope"], envelope)

    def test_writes_to_file(self):
        envelope = {"action": "submit_distillation", "data": {"question": "Q?"}}
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            dry_run_output(envelope, out_path=tmp_path)
            with open(tmp_path, "r", encoding="utf-8") as fh:
                parsed = json.load(fh)
            self.assertIs(parsed["dry_run"], True)
            self.assertEqual(parsed["envelope"], envelope)
        finally:
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------


class EnvironmentResolutionTests(unittest.TestCase):
    """Environment variable resolution tests."""

    def test_resolve_endpoint_default(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            ep = resolve_endpoint()
            self.assertIn("supabase.co", ep)
            self.assertIn("rest/v1", ep)

    def test_resolve_endpoint_env_override(self):
        with unittest.mock.patch.dict(os.environ, {"HIVEMIND_API_URL": "http://localhost:54321/rest/v1"}, clear=True):
            ep = resolve_endpoint()
            self.assertEqual(ep, "http://localhost:54321/rest/v1")

    def test_resolve_endpoint_strips_trailing_slash(self):
        with unittest.mock.patch.dict(os.environ, {"HIVEMIND_API_URL": "http://example.com/"}, clear=True):
            ep = resolve_endpoint()
            self.assertEqual(ep, "http://example.com")

    def test_resolve_anon_key_default(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            key = resolve_anon_key()
            self.assertTrue(key.startswith("sb_publishable_"))

    def test_resolve_anon_key_env_override(self):
        with unittest.mock.patch.dict(os.environ, {"HIVEMIND_ANON_KEY": "custom-key"}, clear=True):
            key = resolve_anon_key()
            self.assertEqual(key, "custom-key")

    def test_resolve_contributor_key_default(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            key = resolve_contributor_key()
            self.assertIsNone(key)

    def test_resolve_contributor_key_env_override(self):
        with unittest.mock.patch.dict(
            os.environ,
            {"HIVEMIND_CONTRIBUTOR_KEY": "hm_" + "a" * 64},
            clear=True,
        ):
            key = resolve_contributor_key()
            self.assertEqual(key, "hm_" + "a" * 64)

    def test_resolve_contribute_url_default(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            url = resolve_contribute_url()
            self.assertIn("functions/v1/contribute", url)

    def test_resolve_contribute_url_env_override(self):
        with unittest.mock.patch.dict(os.environ, {"HIVEMIND_CONTRIBUTE_URL": "http://localhost:54321/functions/v1/contribute"}, clear=True):
            url = resolve_contribute_url()
            self.assertEqual(url, "http://localhost:54321/functions/v1/contribute")


# ---------------------------------------------------------------------------


class ReadBodyFileTests(unittest.TestCase):
    """read_body_file tests."""

    def test_reads_file_content(self):
        content = "Hello, world!\nLine two."
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        try:
            result = read_body_file(tmp_path)
            self.assertEqual(result, content)
        finally:
            os.unlink(tmp_path)

    def test_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            read_body_file("/nonexistent/path/to/file.txt")

    def test_reads_empty_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as tmp:
            tmp_path = tmp.name
        try:
            result = read_body_file(tmp_path)
            self.assertEqual(result, "")
        finally:
            os.unlink(tmp_path)

    def test_unicode_file(self):
        content = "café résumé 🎉"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        try:
            result = read_body_file(tmp_path)
            self.assertEqual(result, content)
        finally:
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------


class PostgrestGetTests(unittest.TestCase):
    """postgrest_get tests with mocked HTTP."""

    def setUp(self):
        self.endpoint = "http://fake-api.example.com/rest/v1"
        self.anon_key = "fake-anon-key"

    def test_builds_correct_url_no_params(self):
        with unittest.mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = unittest.mock.MagicMock()
            mock_resp.__enter__.return_value.read.return_value = b'{"result": "ok"}'
            mock_urlopen.return_value = mock_resp

            result = postgrest_get("unified_feed", endpoint=self.endpoint, anon_key=self.anon_key)
            self.assertEqual(result, {"result": "ok"})

            call_args = mock_urlopen.call_args[0][0]
            self.assertIn("/unified_feed", call_args.full_url)
            self.assertEqual(call_args.get_header("Apikey"), self.anon_key)

    def test_builds_correct_url_with_params(self):
        with unittest.mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = unittest.mock.MagicMock()
            mock_resp.__enter__.return_value.read.return_value = b'[]'
            mock_urlopen.return_value = mock_resp

            params = {"select": "*", "limit": "20", "or": "(title.ilike.*test*,body.ilike.*test*)"}
            postgrest_get("unified_feed", params=params, endpoint=self.endpoint, anon_key=self.anon_key)

            call_args = mock_urlopen.call_args[0][0]
            url = call_args.full_url
            self.assertIn("select=%2A", url)
            self.assertIn("limit=20", url)

    def test_strips_leading_slash_in_path(self):
        with unittest.mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = unittest.mock.MagicMock()
            mock_resp.__enter__.return_value.read.return_value = b'{"ok": true}'
            mock_urlopen.return_value = mock_resp

            postgrest_get("/unified_feed", endpoint=self.endpoint, anon_key=self.anon_key)
            call_args = mock_urlopen.call_args[0][0]
            self.assertEqual(call_args.full_url, "http://fake-api.example.com/rest/v1/unified_feed")

    def test_accept_header_is_json(self):
        with unittest.mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = unittest.mock.MagicMock()
            mock_resp.__enter__.return_value.read.return_value = b'[]'
            mock_urlopen.return_value = mock_resp

            postgrest_get("unified_feed", endpoint=self.endpoint, anon_key=self.anon_key)
            call_args = mock_urlopen.call_args[0][0]
            self.assertEqual(call_args.get_header("Accept"), "application/json")


# ---------------------------------------------------------------------------


class EdgePostTests(unittest.TestCase):
    """edge_post tests with mocked HTTP."""

    def setUp(self):
        self.contribute_url = "http://fake-api.example.com/functions/v1/contribute"
        self.contributor_key = "hm_" + "a" * 64

    def test_sends_correct_headers(self):
        with unittest.mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = unittest.mock.MagicMock()
            mock_resp.__enter__.return_value.read.return_value = b'{"id": 1, "status": "ok"}'
            mock_urlopen.return_value = mock_resp

            payload = {
                "action": "add_resource",
                "data": {"kind": "test", "source": "test", "title": "T", "body": "B"},
            }
            result = edge_post(
                payload,
                contribute_url=self.contribute_url,
                contributor_key=self.contributor_key,
            )
            self.assertEqual(result, {"id": 1, "status": "ok"})

            call_args = mock_urlopen.call_args[0][0]
            self.assertEqual(call_args.get_header("Content-type"), "application/json")
            self.assertEqual(call_args.get_header("X-contributor-key"), self.contributor_key)
            self.assertEqual(call_args.get_header("Accept"), "application/json")

    def test_raises_when_no_key(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                edge_post({}, contribute_url=self.contribute_url)
            self.assertIn("contributor key required", str(ctx.exception))

    def test_uses_env_key_when_no_param(self):
        key = "hm_" + "b" * 64
        with unittest.mock.patch.dict(os.environ, {"HIVEMIND_CONTRIBUTOR_KEY": key}, clear=True):
            with unittest.mock.patch("urllib.request.urlopen") as mock_urlopen:
                mock_resp = unittest.mock.MagicMock()
                mock_resp.__enter__.return_value.read.return_value = b'{"ok": true}'
                mock_urlopen.return_value = mock_resp

                edge_post({"action": "add_resource", "data": {"kind": "k", "source": "s", "title": "t", "body": "b"}},
                          contribute_url=self.contribute_url)

                call_args = mock_urlopen.call_args[0][0]
                self.assertEqual(call_args.get_header("X-contributor-key"), key)

    def test_sends_json_body(self):
        with unittest.mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = unittest.mock.MagicMock()
            mock_resp.__enter__.return_value.read.return_value = b'{"ok": true}'
            mock_urlopen.return_value = mock_resp

            payload = {"action": "submit_distillation", "data": {"question": "Q?"}}
            edge_post(payload, contribute_url=self.contribute_url, contributor_key=self.contributor_key)

            call_args = mock_urlopen.call_args[0][0]
            # urllib puts the body in .data
            sent_body = json.loads(call_args.data)
            self.assertEqual(sent_body, payload)


# ===========================================================================
# Discovery
# ===========================================================================

if __name__ == "__main__":
    unittest.main()
