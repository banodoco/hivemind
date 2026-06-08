"""Tests for the refresh_media executor — mocked HTTP, no network."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import unittest.mock
import urllib.error
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))

from executors.refresh_media.run import (  # noqa: E402
    _validate_message_id,
    build_parser,
    main,
    refresh_media,
)


class CLITests(unittest.TestCase):
    def test_message_id_required(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])

    def test_message_id_parsed_as_string(self):
        parser = build_parser()
        args = parser.parse_args(["--message-id", "1512127379039060118"])
        self.assertEqual(args.message_id, "1512127379039060118")

    def test_out_parsed(self):
        parser = build_parser()
        args = parser.parse_args(["--message-id", "1", "--out", "/tmp/result.json"])
        self.assertEqual(args.out, "/tmp/result.json")


class ValidationTests(unittest.TestCase):
    def test_accepts_digit_string(self):
        self.assertEqual(_validate_message_id("1512127379039060118"), "1512127379039060118")

    def test_strips_whitespace(self):
        self.assertEqual(_validate_message_id(" 1512127379039060118 "), "1512127379039060118")

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            _validate_message_id(" ")

    def test_rejects_non_digits(self):
        with self.assertRaises(ValueError):
            _validate_message_id("abc123")


class RefreshMediaTests(unittest.TestCase):
    def test_calls_public_edge_with_string_message_id(self):
        captured = {}

        def mock_post(payload, *, url=None, anon_key=None):
            captured["payload"] = payload
            captured["url"] = url
            captured["anon_key"] = anon_key
            return {
                "success": True,
                "message_id": payload["message_id"],
                "attachments": [{"url": "https://cdn.discordapp.com/file.mp4"}],
                "urls_updated": 1,
            }

        with unittest.mock.patch("executors.refresh_media.run.public_edge_post", side_effect=mock_post):
            result = refresh_media(
                "1512127379039060118",
                refresh_url="https://example.test/functions/v1/refresh-media-urls",
                anon_key="anon",
            )

        self.assertTrue(result["success"])
        self.assertEqual(captured["payload"], {"message_id": "1512127379039060118"})
        self.assertEqual(captured["url"], "https://example.test/functions/v1/refresh-media-urls")
        self.assertEqual(captured["anon_key"], "anon")


class MainTests(unittest.TestCase):
    def test_main_success_stdout(self):
        def mock_refresh(message_id, *, refresh_url, anon_key):
            return {"success": True, "message_id": message_id, "attachments": [], "urls_updated": 0}

        buf = io.StringIO()
        with unittest.mock.patch("executors.refresh_media.run.refresh_media", side_effect=mock_refresh):
            with unittest.mock.patch("sys.stdout", buf):
                code = main(["--message-id", "1512127379039060118"])

        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["success"])
        self.assertEqual(payload["message_id"], "1512127379039060118")

    def test_main_validation_error(self):
        buf = io.StringIO()
        with unittest.mock.patch("sys.stdout", buf):
            code = main(["--message-id", "not-a-snowflake"])

        self.assertEqual(code, 2)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["error"], "validation_error")

    def test_main_http_error(self):
        def mock_refresh(message_id, *, refresh_url, anon_key):
            raise urllib.error.HTTPError(
                url="https://example.test",
                code=404,
                msg="Not Found",
                hdrs={},
                fp=io.BytesIO(b'{"success":false}'),
            )

        buf = io.StringIO()
        with unittest.mock.patch("executors.refresh_media.run.refresh_media", side_effect=mock_refresh):
            with unittest.mock.patch("sys.stdout", buf):
                code = main(["--message-id", "1512127379039060118"])

        self.assertEqual(code, 2)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["error"], "edge_function_error")
        self.assertEqual(payload["status"], 404)

    def test_main_writes_out_file(self):
        def mock_refresh(message_id, *, refresh_url, anon_key):
            return {"success": True, "message_id": message_id, "attachments": [], "urls_updated": 0}

        with tempfile.NamedTemporaryFile() as tmp:
            with unittest.mock.patch("executors.refresh_media.run.refresh_media", side_effect=mock_refresh):
                code = main(["--message-id", "1512127379039060118", "--out", tmp.name])
            self.assertEqual(code, 0)
            tmp.seek(0)
            payload = json.loads(tmp.read().decode("utf-8"))
        self.assertTrue(payload["success"])
