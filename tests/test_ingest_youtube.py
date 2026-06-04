"""Tests for the ingest_youtube executor — mocked yt-dlp subprocess, no network/binary."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))

from executors.ingest_youtube.run import (  # noqa: E402
    _dedupe_rolling,
    _merge_overlap,
    build_envelope,
    build_parser,
    extract_video_id,
    main,
    parse_vtt,
)

FIXTURE_SAMPLE_CAPTIONS = _HERE / "fixtures" / "sample_captions.vtt"


def _read_captions() -> str:
    with open(FIXTURE_SAMPLE_CAPTIONS, "r", encoding="utf-8") as fh:
        return fh.read()


_META = {
    "id": "dQw4w9WgXcQ",
    "title": "Testing Wan Animate with VACE",
    "channel": "Banodoco Lab",
    "uploader": "Banodoco Lab",
    "duration": 600,
    "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
}


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class CLITests(unittest.TestCase):
    def test_url_required(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args([])

    def test_url_parsed(self):
        args = build_parser().parse_args(["--url", "https://youtu.be/abc"])
        self.assertEqual(args.url, "https://youtu.be/abc")

    def test_dry_run_flag(self):
        args = build_parser().parse_args(["--url", "https://youtu.be/abc", "--dry-run"])
        self.assertTrue(args.dry_run)

    def test_out_flag(self):
        args = build_parser().parse_args(["--url", "https://youtu.be/abc", "--out", "/tmp/o.json"])
        self.assertEqual(args.out, "/tmp/o.json")


# ---------------------------------------------------------------------------
# VTT parsing / dedup tests
# ---------------------------------------------------------------------------


class ParseVTTTests(unittest.TestCase):
    def test_strips_header_and_timing(self):
        text = parse_vtt(_read_captions())
        self.assertNotIn("WEBVTT", text)
        self.assertNotIn("-->", text)
        self.assertNotIn("00:00", text)
        self.assertNotIn("Kind:", text)
        self.assertNotIn("Language:", text)

    def test_strips_inline_tags(self):
        text = parse_vtt(_read_captions())
        self.assertNotIn("<c>", text)
        self.assertNotIn("00:00:08.500", text)

    def test_dedupes_rolling_repeats(self):
        text = parse_vtt(_read_captions())
        # "hey everyone" must appear exactly once despite the scrolling window.
        self.assertEqual(text.count("hey everyone"), 1)
        self.assertEqual(text.count("VACE nodes"), 1)

    def test_keeps_full_content(self):
        text = parse_vtt(_read_captions())
        self.assertIn("hey everyone welcome back to the channel", text)
        self.assertIn("testing Wan animate", text)
        self.assertIn("VACE nodes", text)
        self.assertIn("and it works really well", text)

    def test_empty_vtt_returns_empty(self):
        self.assertEqual(parse_vtt("WEBVTT\n\n"), "")


class DedupeRollingTests(unittest.TestCase):
    def test_prefix_growth_replaced(self):
        result = _dedupe_rolling(["a b", "a b c", "a b c d"])
        self.assertEqual(result, "a b c d")

    def test_exact_duplicate_dropped(self):
        result = _dedupe_rolling(["hello world", "hello world"])
        self.assertEqual(result, "hello world")

    def test_distinct_lines_kept(self):
        result = _dedupe_rolling(["one two", "three four"])
        self.assertEqual(result, "one two\nthree four")


class MergeOverlapTests(unittest.TestCase):
    def test_suffix_overlap_merges(self):
        merged = _merge_overlap("to the channel today we are", "today we are testing Wan")
        self.assertEqual(merged, "to the channel today we are testing Wan")

    def test_no_overlap_returns_none(self):
        self.assertIsNone(_merge_overlap("alpha beta", "gamma delta"))

    def test_single_word_overlap_ignored(self):
        # Only a 1-word overlap should not trigger a merge.
        self.assertIsNone(_merge_overlap("the cat", "the dog"))


# ---------------------------------------------------------------------------
# Metadata / envelope tests
# ---------------------------------------------------------------------------


class ExtractVideoIdTests(unittest.TestCase):
    def test_extracts_id(self):
        self.assertEqual(extract_video_id(_META), "dQw4w9WgXcQ")

    def test_missing_id_returns_none(self):
        self.assertIsNone(extract_video_id({}))


class BuildEnvelopeTests(unittest.TestCase):
    def test_envelope_shape(self):
        env = build_envelope("https://youtu.be/x", _META, "transcript text")
        self.assertEqual(env["action"], "add_resource")
        data = env["data"]
        self.assertEqual(data["kind"], "transcript")
        self.assertEqual(data["source"], "youtube")
        self.assertEqual(data["external_id"], "dQw4w9WgXcQ")
        self.assertEqual(data["title"], "Testing Wan Animate with VACE")
        self.assertEqual(data["author"], "Banodoco Lab")
        self.assertEqual(data["body"], "transcript text")
        self.assertEqual(data["metadata"]["video_id"], "dQw4w9WgXcQ")
        self.assertEqual(data["metadata"]["duration"], 600)


# ---------------------------------------------------------------------------
# main() — mocked subprocess
# ---------------------------------------------------------------------------


def _make_subprocess_mock(meta=_META, captions=None, captions_text=None):
    """Return a fake subprocess.run that emulates yt-dlp metadata + captions.

    *captions* selects whether download_captions finds a VTT: when truthy, the
    fake writes a VTT into the -o output dir; when falsy, it writes nothing.
    """
    cap_text = captions_text if captions_text is not None else _read_captions()

    def fake_run(cmd, capture_output=False, text=False, timeout=None):
        if "-j" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps(meta) + "\n", stderr=""
            )
        # caption download: find the -o template dir
        if captions and "-o" in cmd:
            tmpl = cmd[cmd.index("-o") + 1]
            out_dir = os.path.dirname(tmpl)
            vid = meta.get("id", "vid")
            with open(os.path.join(out_dir, f"{vid}.en.vtt"), "w", encoding="utf-8") as fh:
                fh.write(cap_text)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return fake_run


class MainDryRunTests(unittest.TestCase):
    def test_dry_run_with_captions(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            with unittest.mock.patch(
                "subprocess.run", side_effect=_make_subprocess_mock(captions=True)
            ):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                    ret = main(["--url", "https://youtu.be/dQw4w9WgXcQ", "--dry-run"])
                    self.assertEqual(ret, 0)
                    output = json.loads(out.getvalue())
                    self.assertIs(output["dry_run"], True)
                    data = output["envelope"]["data"]
                    self.assertEqual(data["kind"], "transcript")
                    self.assertEqual(data["source"], "youtube")
                    self.assertEqual(data["external_id"], "dQw4w9WgXcQ")
                    self.assertIn("welcome back to the channel", data["body"])

    def test_no_captions_exits_nonzero(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            with unittest.mock.patch(
                "subprocess.run", side_effect=_make_subprocess_mock(captions=False)
            ):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                    ret = main(["--url", "https://youtu.be/dQw4w9WgXcQ", "--dry-run"])
                    self.assertEqual(ret, 1)
                    output = json.loads(out.getvalue())
                    self.assertEqual(output["error"], "captions unavailable")
                    self.assertIn("transcribe", output["detail"].lower())
                    self.assertIn("hivemind.contribute", output["detail"])

    def test_empty_captions_exits_nonzero(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            with unittest.mock.patch(
                "subprocess.run",
                side_effect=_make_subprocess_mock(captions=True, captions_text="WEBVTT\n\n"),
            ):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                    ret = main(["--url", "https://youtu.be/x", "--dry-run"])
                    self.assertEqual(ret, 1)
                    self.assertEqual(json.loads(out.getvalue())["error"], "captions unavailable")

    def test_metadata_failure_exits_nonzero(self):
        def fake_run(cmd, capture_output=False, text=False, timeout=None):
            if "-j" in cmd:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            with unittest.mock.patch("subprocess.run", side_effect=fake_run):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                    ret = main(["--url", "https://youtu.be/x", "--dry-run"])
                    self.assertEqual(ret, 1)
                    self.assertIn("error", json.loads(out.getvalue()))

    def test_dry_run_with_out_file(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as tmp:
            out_path = tmp.name
        try:
            with unittest.mock.patch.dict(os.environ, {}, clear=True):
                with unittest.mock.patch(
                    "subprocess.run", side_effect=_make_subprocess_mock(captions=True)
                ):
                    ret = main([
                        "--url", "https://youtu.be/x", "--dry-run", "--out", out_path,
                    ])
                    self.assertEqual(ret, 0)
            with open(out_path, "r", encoding="utf-8") as fh:
                output = json.load(fh)
            self.assertIs(output["dry_run"], True)
        finally:
            os.unlink(out_path)


class MainRealSendTests(unittest.TestCase):
    def _patch_env(self):
        return unittest.mock.patch.dict(
            os.environ,
            {"HIVEMIND_CONTRIBUTOR_KEY": "hm_" + "a" * 64},
            clear=True,
        )

    def test_real_send_success(self):
        with self._patch_env():
            def mock_post(payload, contribute_url=None, contributor_key=None):
                return {"id": 99, "status": "ok"}

            with unittest.mock.patch(
                "subprocess.run", side_effect=_make_subprocess_mock(captions=True)
            ):
                with unittest.mock.patch(
                    "executors.ingest_youtube.run.edge_post", side_effect=mock_post
                ):
                    with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                        ret = main(["--url", "https://youtu.be/x"])
                        self.assertEqual(ret, 0)
                        self.assertEqual(json.loads(out.getvalue())["id"], 99)

    def test_missing_key_fails(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            with unittest.mock.patch(
                "subprocess.run", side_effect=_make_subprocess_mock(captions=True)
            ):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                    ret = main(["--url", "https://youtu.be/x"])
                    self.assertEqual(ret, 1)
                    self.assertIn("contributor key required", json.loads(out.getvalue())["error"])


if __name__ == "__main__":
    unittest.main()
