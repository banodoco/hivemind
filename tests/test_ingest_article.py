"""Tests for the ingest_article executor — mocked HTTP, no network."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))

from executors.ingest_article.run import (  # noqa: E402
    _ArticleExtractor,
    _collapse_whitespace,
    build_parser,
    extract_from_html,
    fetch_html,
    main,
    resolve_title,
)

# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------

FIXTURE_SAMPLE_ARTICLE = _HERE / "fixtures" / "sample_article.html"


def _read_fixture(path: Path) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class CLITests(unittest.TestCase):
    """Argument parsing tests."""

    def test_url_required(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])

    def test_url_parsed(self):
        parser = build_parser()
        args = parser.parse_args(["--url", "https://example.com/article"])
        self.assertEqual(args.url, "https://example.com/article")

    def test_title_optional_default_none(self):
        parser = build_parser()
        args = parser.parse_args(["--url", "https://example.com"])
        self.assertIsNone(args.title)

    def test_title_override(self):
        parser = build_parser()
        args = parser.parse_args(["--url", "https://example.com", "--title", "Custom"])
        self.assertEqual(args.title, "Custom")

    def test_kind_default_article(self):
        parser = build_parser()
        args = parser.parse_args(["--url", "https://example.com"])
        self.assertEqual(args.kind, "article")

    def test_kind_override(self):
        parser = build_parser()
        args = parser.parse_args(["--url", "https://example.com", "--kind", "tutorial"])
        self.assertEqual(args.kind, "tutorial")

    def test_dry_run_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--url", "https://example.com", "--dry-run"])
        self.assertTrue(args.dry_run)

    def test_dry_run_default_false(self):
        parser = build_parser()
        args = parser.parse_args(["--url", "https://example.com"])
        self.assertFalse(args.dry_run)

    def test_out_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--url", "https://example.com", "--out", "/tmp/o.json"])
        self.assertEqual(args.out, "/tmp/o.json")


# ---------------------------------------------------------------------------
# Whitespace collapsing tests
# ---------------------------------------------------------------------------


class CollapseWhitespaceTests(unittest.TestCase):
    """_collapse_whitespace tests."""

    def test_single_line_unchanged(self):
        result = _collapse_whitespace("Hello world")
        self.assertEqual(result, "Hello world")

    def test_multiple_spaces_collapsed(self):
        result = _collapse_whitespace("Hello    world")
        self.assertEqual(result, "Hello world")

    def test_tabs_collapsed(self):
        result = _collapse_whitespace("Hello\t\tworld")
        self.assertEqual(result, "Hello world")

    def test_newlines_become_spaces(self):
        result = _collapse_whitespace("Hello\nworld")
        self.assertEqual(result, "Hello world")

    def test_paragraph_breaks_preserved(self):
        result = _collapse_whitespace("Para 1.\n\nPara 2.")
        self.assertIn("Para 1.", result)
        self.assertIn("Para 2.", result)
        self.assertIn("\n\n", result)

    def test_triple_newline_collapsed_to_double(self):
        result = _collapse_whitespace("A\n\n\nB")
        self.assertEqual(result, "A\n\nB")

    def test_trailing_whitespace_stripped(self):
        result = _collapse_whitespace("  hello  ")
        self.assertEqual(result, "hello")

    def test_empty_string(self):
        result = _collapse_whitespace("")
        self.assertEqual(result, "")

    def test_only_whitespace(self):
        result = _collapse_whitespace("   \n  \t  ")
        self.assertEqual(result, "")


# ---------------------------------------------------------------------------
# HTML extraction tests (pure, no network)
# ---------------------------------------------------------------------------


class ArticleExtractorTests(unittest.TestCase):
    """_ArticleExtractor tests."""

    def test_extracts_plain_text(self):
        extractor = _ArticleExtractor()
        extractor.feed("<html><body><p>Hello world</p></body></html>")
        extractor.close()
        text = extractor.get_text()
        self.assertIn("Hello world", text)

    def test_strips_script_tag(self):
        extractor = _ArticleExtractor()
        extractor.feed("<html><script>alert('xss')</script><p>Safe</p></html>")
        extractor.close()
        text = extractor.get_text()
        self.assertNotIn("alert", text)
        self.assertIn("Safe", text)

    def test_strips_style_tag(self):
        extractor = _ArticleExtractor()
        extractor.feed("<html><style>.x{color:red}</style><p>Visible</p></html>")
        extractor.close()
        text = extractor.get_text()
        self.assertNotIn("color", text)
        self.assertIn("Visible", text)

    def test_strips_nav_tag(self):
        extractor = _ArticleExtractor()
        extractor.feed("<html><nav><a href='/'>Home</a></nav><p>Content</p></html>")
        extractor.close()
        text = extractor.get_text()
        self.assertNotIn("Home", text)
        self.assertIn("Content", text)

    def test_strips_header_tag(self):
        extractor = _ArticleExtractor()
        extractor.feed("<html><header>Site title</header><p>Body</p></html>")
        extractor.close()
        text = extractor.get_text()
        self.assertNotIn("Site title", text)
        self.assertIn("Body", text)

    def test_strips_footer_tag(self):
        extractor = _ArticleExtractor()
        extractor.feed("<html><p>Body</p><footer>Copyright</footer></html>")
        extractor.close()
        text = extractor.get_text()
        self.assertNotIn("Copyright", text)
        self.assertIn("Body", text)

    def test_strips_aside_tag(self):
        extractor = _ArticleExtractor()
        extractor.feed("<html><aside>Sidebar</aside><p>Main</p></html>")
        extractor.close()
        text = extractor.get_text()
        self.assertNotIn("Sidebar", text)
        self.assertIn("Main", text)

    def test_strips_noscript_tag(self):
        extractor = _ArticleExtractor()
        extractor.feed("<html><noscript>No JS</noscript><p>Content</p></html>")
        extractor.close()
        text = extractor.get_text()
        self.assertNotIn("No JS", text)
        self.assertIn("Content", text)

    def test_extracts_title(self):
        extractor = _ArticleExtractor()
        extractor.feed("<html><head><title>My Article</title></head><body><p>Body</p></body></html>")
        extractor.close()
        self.assertEqual(extractor.get_title(), "My Article")

    def test_extracts_og_title(self):
        extractor = _ArticleExtractor()
        extractor.feed(
            '<html><head><meta property="og:title" content="OG Title"></head>'
            '<body><p>Body</p></body></html>'
        )
        extractor.close()
        self.assertEqual(extractor.get_og_title(), "OG Title")

    def test_extracts_both_titles(self):
        extractor = _ArticleExtractor()
        extractor.feed(
            '<html><head>'
            '<meta property="og:title" content="OG Title">'
            '<title>HTML Title</title>'
            '</head><body><p>Body</p></body></html>'
        )
        extractor.close()
        self.assertEqual(extractor.get_title(), "HTML Title")
        self.assertEqual(extractor.get_og_title(), "OG Title")

    def test_no_title_returns_none(self):
        extractor = _ArticleExtractor()
        extractor.feed("<html><body><p>No title here</p></body></html>")
        extractor.close()
        self.assertIsNone(extractor.get_title())
        self.assertIsNone(extractor.get_og_title())

    def test_whitespace_collapsed_in_extracted_text(self):
        extractor = _ArticleExtractor()
        extractor.feed("<html><body><p>Line   one</p><p>Line   two</p></body></html>")
        extractor.close()
        text = extractor.get_text()
        self.assertNotIn("   ", text)  # multiple spaces collapsed

    def test_block_tags_produce_separation(self):
        extractor = _ArticleExtractor()
        extractor.feed("<html><body><p>First paragraph.</p><p>Second paragraph.</p></body></html>")
        extractor.close()
        text = extractor.get_text()
        self.assertIn("First paragraph", text)
        self.assertIn("Second paragraph", text)

    def test_nested_tags_handled(self):
        extractor = _ArticleExtractor()
        extractor.feed(
            "<html><body><div><p>Nested <strong>bold</strong> text</p></div></body></html>"
        )
        extractor.close()
        text = extractor.get_text()
        self.assertIn("Nested", text)
        self.assertIn("bold", text)
        self.assertIn("text", text)


class ExtractFromHTMLFunctionTests(unittest.TestCase):
    """extract_from_html() convenience function tests."""

    def test_returns_tuple_of_text_title_ogtitle(self):
        html = (
            '<html><head>'
            '<meta property="og:title" content="OG Title">'
            '<title>HTML Title</title>'
            '</head><body><p>Body text</p></body></html>'
        )
        text, html_title, og_title = extract_from_html(html)
        self.assertIsInstance(text, str)
        self.assertIn("Body text", text)
        self.assertEqual(html_title, "HTML Title")
        self.assertEqual(og_title, "OG Title")

    def test_script_stripped_in_function_output(self):
        html = "<html><script>var x=1;</script><p>Visible</p></html>"
        text, _, _ = extract_from_html(html)
        self.assertNotIn("var x", text)
        self.assertIn("Visible", text)


class ExtractFromFixtureTests(unittest.TestCase):
    """Tests against the sample_article.html fixture."""

    def setUp(self):
        self.html = _read_fixture(FIXTURE_SAMPLE_ARTICLE)

    def test_fixture_loads(self):
        self.assertIn("<!DOCTYPE html>", self.html)
        self.assertIn("<title>", self.html)

    def test_extracts_title_from_fixture(self):
        text, html_title, og_title = extract_from_html(self.html)
        self.assertEqual(html_title, "HTML Title: The Future of AI Video Generation")
        self.assertEqual(og_title, "OG: The Future of AI Video Generation")

    def test_strips_script_from_fixture(self):
        text, _, _ = extract_from_html(self.html)
        self.assertNotIn("console.log", text)
        self.assertNotIn("tracking", text)

    def test_strips_style_from_fixture(self):
        text, _, _ = extract_from_html(self.html)
        self.assertNotIn("font-family", text)

    def test_strips_nav_from_fixture(self):
        text, _, _ = extract_from_html(self.html)
        self.assertNotIn("Home", text)
        self.assertNotIn("About", text)

    def test_strips_header_from_fixture(self):
        text, _, _ = extract_from_html(self.html)
        self.assertNotIn("Site Header", text)

    def test_strips_footer_from_fixture(self):
        text, _, _ = extract_from_html(self.html)
        self.assertNotIn("AI News Daily", text)

    def test_strips_aside_from_fixture(self):
        text, _, _ = extract_from_html(self.html)
        self.assertNotIn("Related links", text)
        self.assertNotIn("Sidebar", text)

    def test_strips_noscript_from_fixture(self):
        text, _, _ = extract_from_html(self.html)
        self.assertNotIn("Your browser does not support JavaScript", text)

    def test_extracts_article_body(self):
        text, _, _ = extract_from_html(self.html)
        self.assertIn("Artificial intelligence", text)
        self.assertIn("diffusion transformers", text)
        self.assertIn("ComfyUI workflows", text)


# ---------------------------------------------------------------------------
# Title resolution tests
# ---------------------------------------------------------------------------


class ResolveTitleTests(unittest.TestCase):
    """resolve_title() tests."""

    def test_cli_title_wins(self):
        result = resolve_title("CLI Title", "OG Title", "HTML Title")
        self.assertEqual(result, "CLI Title")

    def test_og_title_wins_when_no_cli(self):
        result = resolve_title(None, "OG Title", "HTML Title")
        self.assertEqual(result, "OG Title")

    def test_html_title_wins_when_no_cli_or_og(self):
        result = resolve_title(None, None, "HTML Title")
        self.assertEqual(result, "HTML Title")

    def test_falls_back_to_untitled(self):
        result = resolve_title(None, None, None)
        self.assertEqual(result, "Untitled")

    def test_cli_title_empty_string_falls_through(self):
        """--title '' (blank) is treated as absent and falls through to og:title."""
        result = resolve_title("", "OG Title", "HTML Title")
        self.assertEqual(result, "OG Title")

    def test_falls_back_to_url_when_all_empty(self):
        """When title/og/html are all empty, fall back to the URL."""
        result = resolve_title("", "", "", fallback="https://example.com/x")
        self.assertEqual(result, "https://example.com/x")

    def test_falls_back_to_untitled_when_no_fallback(self):
        result = resolve_title(None, None, None)
        self.assertEqual(result, "Untitled")

    def test_og_title_none_skipped(self):
        result = resolve_title(None, None, "HTML Title")
        self.assertEqual(result, "HTML Title")

    def test_all_none(self):
        result = resolve_title(None, None, None)
        self.assertEqual(result, "Untitled")


# ---------------------------------------------------------------------------
# fetch_html tests (mocked)
# ---------------------------------------------------------------------------


class FetchHTMLTests(unittest.TestCase):
    """fetch_html() tests with mocked urllib."""

    def test_fetch_returns_html_string(self):
        mock_html = "<html><body><p>Test</p></body></html>"

        class MockResponse:
            @staticmethod
            def read():
                return mock_html.encode("utf-8")

            @staticmethod
            def getheader(name, default=None):
                return {}

            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def mock_urlopen(req, timeout=30):
            return MockResponse()

        with unittest.mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
            result = fetch_html("http://example.com")
            self.assertEqual(result, mock_html)

    def test_fetch_http_error(self):
        import urllib.error

        def mock_urlopen(req, timeout=30):
            raise urllib.error.HTTPError(
                "http://example.com", 404, "Not Found", {}, io.BytesIO(b"")
            )

        with unittest.mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
            with self.assertRaises(urllib.error.HTTPError):
                fetch_html("http://example.com/404")


# ---------------------------------------------------------------------------
# Main integration tests — dry-run (no key needed, mocked fetch)
# ---------------------------------------------------------------------------


class MainDryRunTests(unittest.TestCase):
    """main() tests in dry-run mode — no contributor key needed."""

    def setUp(self):
        self.contribute_url = "http://fake.example.com/functions/v1/contribute"

    def _patch_env(self):
        return unittest.mock.patch.dict(
            os.environ,
            {"HIVEMIND_CONTRIBUTE_URL": self.contribute_url},
            clear=True,
        )

    def test_dry_run_outputs_envelope(self):
        mock_html = (
            '<html><head><title>Test Article</title></head>'
            '<body><p>Body text here.</p></body></html>'
        )

        class MockResponse:
            @staticmethod
            def read():
                return mock_html.encode("utf-8")

            headers = {"Content-Type": "text/html; charset=utf-8"}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def mock_urlopen(req, timeout=30):
            return MockResponse()

        with self._patch_env():
            with unittest.mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = main([
                        "--url", "https://example.com/article",
                        "--dry-run",
                    ])
                    self.assertEqual(ret, 0)
                    output = json.loads(mock_stdout.getvalue())
                    self.assertIs(output["dry_run"], True)
                    self.assertIn("envelope", output)
                    self.assertEqual(output["envelope"]["action"], "add_resource")
                    self.assertEqual(output["envelope"]["data"]["kind"], "article")
                    self.assertEqual(output["envelope"]["data"]["source"], "web")
                    self.assertEqual(output["envelope"]["data"]["title"], "Test Article")
                    self.assertIn("Body text here", output["envelope"]["data"]["body"])
                    self.assertEqual(
                        output["envelope"]["data"]["url"],
                        "https://example.com/article",
                    )
                    self.assertEqual(
                        output["envelope"]["data"]["external_id"],
                        "https://example.com/article",
                    )

    def test_dry_run_does_not_require_contributor_key(self):
        """Dry-run should NOT require a contributor key."""
        mock_html = "<html><body><p>Test</p></body></html>"

        class MockResponse:
            @staticmethod
            def read():
                return mock_html.encode("utf-8")

            headers = {"Content-Type": "text/html"}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def mock_urlopen(req, timeout=30):
            return MockResponse()

        with self._patch_env():
            with unittest.mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = main([
                        "--url", "https://example.com",
                        "--dry-run",
                    ])
                    self.assertEqual(ret, 0)
                    output = json.loads(mock_stdout.getvalue())
                    self.assertIn("dry_run", output)
                    self.assertIn("envelope", output)

    def test_dry_run_respects_title_override(self):
        mock_html = (
            '<html><head><title>Original</title></head>'
            '<body><p>Body</p></body></html>'
        )

        class MockResponse:
            @staticmethod
            def read():
                return mock_html.encode("utf-8")

            headers = {"Content-Type": "text/html"}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def mock_urlopen(req, timeout=30):
            return MockResponse()

        with self._patch_env():
            with unittest.mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = main([
                        "--url", "https://example.com",
                        "--title", "Overridden Title",
                        "--dry-run",
                    ])
                    self.assertEqual(ret, 0)
                    output = json.loads(mock_stdout.getvalue())
                    self.assertEqual(
                        output["envelope"]["data"]["title"],
                        "Overridden Title",
                    )

    def test_dry_run_prefers_og_title_over_html_title(self):
        mock_html = (
            '<html><head>'
            '<meta property="og:title" content="OG Title">'
            '<title>HTML Title</title>'
            '</head><body><p>Body</p></body></html>'
        )

        class MockResponse:
            @staticmethod
            def read():
                return mock_html.encode("utf-8")

            headers = {"Content-Type": "text/html"}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def mock_urlopen(req, timeout=30):
            return MockResponse()

        with self._patch_env():
            with unittest.mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = main([
                        "--url", "https://example.com",
                        "--dry-run",
                    ])
                    self.assertEqual(ret, 0)
                    output = json.loads(mock_stdout.getvalue())
                    self.assertEqual(
                        output["envelope"]["data"]["title"],
                        "OG Title",
                    )

    def test_dry_run_respects_kind_override(self):
        mock_html = "<html><body><p>Body</p></body></html>"

        class MockResponse:
            @staticmethod
            def read():
                return mock_html.encode("utf-8")

            headers = {"Content-Type": "text/html"}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def mock_urlopen(req, timeout=30):
            return MockResponse()

        with self._patch_env():
            with unittest.mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = main([
                        "--url", "https://example.com",
                        "--kind", "tutorial",
                        "--dry-run",
                    ])
                    self.assertEqual(ret, 0)
                    output = json.loads(mock_stdout.getvalue())
                    self.assertEqual(output["envelope"]["data"]["kind"], "tutorial")

    def test_dry_run_with_out_file(self):
        mock_html = "<html><body><p>Body</p></body></html>"

        class MockResponse:
            @staticmethod
            def read():
                return mock_html.encode("utf-8")

            headers = {"Content-Type": "text/html"}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def mock_urlopen(req, timeout=30):
            return MockResponse()

        with self._patch_env():
            with unittest.mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".json", delete=False, encoding="utf-8"
                ) as tmp:
                    tmp_path = tmp.name
                try:
                    ret = main([
                        "--url", "https://example.com",
                        "--dry-run",
                        "--out", tmp_path,
                    ])
                    self.assertEqual(ret, 0)
                    with open(tmp_path, "r", encoding="utf-8") as fh:
                        output = json.load(fh)
                    self.assertIs(output["dry_run"], True)
                    self.assertIn("envelope", output)
                finally:
                    os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Main integration tests — real send (requires key, mocked fetch + edge_post)
# ---------------------------------------------------------------------------


class MainRealSendTests(unittest.TestCase):
    """main() tests for real sends with mocked fetch and edge_post."""

    def setUp(self):
        self.contribute_url = "http://fake.example.com/functions/v1/contribute"
        self.contributor_key = "hm_" + "a" * 64
        self.mock_html = "<html><head><title>Test</title></head><body><p>Body</p></body></html>"

        class MockResponse:
            @staticmethod
            def read():
                return self.mock_html.encode("utf-8")

            headers = {"Content-Type": "text/html"}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        self.MockResponse = MockResponse

    def _patch_env(self):
        return unittest.mock.patch.dict(
            os.environ,
            {
                "HIVEMIND_CONTRIBUTE_URL": self.contribute_url,
                "HIVEMIND_CONTRIBUTOR_KEY": self.contributor_key,
            },
            clear=True,
        )

    def test_real_send_success(self):
        with self._patch_env():
            def mock_urlopen(req, timeout=30):
                return self.MockResponse()

            def mock_post(payload, contribute_url=None, contributor_key=None):
                return {"id": 42, "status": "ok"}

            with unittest.mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
                with unittest.mock.patch(
                    "executors.ingest_article.run.edge_post", side_effect=mock_post
                ):
                    with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                        ret = main(["--url", "https://example.com"])
                        self.assertEqual(ret, 0)
                        output = json.loads(mock_stdout.getvalue())
                        self.assertEqual(output["id"], 42)
                        self.assertEqual(output["status"], "ok")

    def test_missing_contributor_key_fails(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            def mock_urlopen(req, timeout=30):
                return self.MockResponse()

            with unittest.mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = main(["--url", "https://example.com"])
                    self.assertEqual(ret, 1)
                    output = json.loads(mock_stdout.getvalue())
                    self.assertIn("error", output)
                    self.assertIn("contributor key required", output["error"])

    def test_fetch_http_error(self):
        import urllib.error

        with self._patch_env():
            def mock_urlopen(req, timeout=30):
                raise urllib.error.HTTPError(
                    "http://example.com", 404, "Not Found", {}, io.BytesIO(b"")
                )

            with unittest.mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                    ret = main(["--url", "https://example.com/404"])
                    self.assertEqual(ret, 1)
                    output = json.loads(mock_stdout.getvalue())
                    self.assertIn("error", output)
                    self.assertIn("404", output["error"])

    def test_409_duplicate_surfaces_existing_id(self):
        import urllib.error

        with self._patch_env():
            def mock_urlopen(req, timeout=30):
                return self.MockResponse()

            def mock_post(payload, contribute_url=None, contributor_key=None):
                raise urllib.error.HTTPError(
                    "http://fake/", 409, "Conflict", {},
                    io.BytesIO(b'{"error":"duplicate","existing_id":42,"detail":"similar"}'),
                )

            with unittest.mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
                with unittest.mock.patch(
                    "executors.ingest_article.run.edge_post", side_effect=mock_post
                ):
                    with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                        ret = main(["--url", "https://example.com"])
                        self.assertEqual(ret, 1)
                        output = json.loads(mock_stdout.getvalue())
                        self.assertIn("error", output)
                        self.assertIn("409", output["error"])
                        self.assertIn("existing_id=42", output["error"])

    def test_400_validation_error(self):
        import urllib.error

        with self._patch_env():
            def mock_urlopen(req, timeout=30):
                return self.MockResponse()

            def mock_post(payload, contribute_url=None, contributor_key=None):
                raise urllib.error.HTTPError(
                    "http://fake/", 400, "Bad Request", {},
                    io.BytesIO(b'{"error":"validation","detail":"field title required"}'),
                )

            with unittest.mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
                with unittest.mock.patch(
                    "executors.ingest_article.run.edge_post", side_effect=mock_post
                ):
                    with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                        ret = main(["--url", "https://example.com"])
                        self.assertEqual(ret, 1)
                        output = json.loads(mock_stdout.getvalue())
                        self.assertIn("400", output["error"])
                        self.assertIn("field title required", output["error"])

    def test_401_unauthorized(self):
        import urllib.error

        with self._patch_env():
            def mock_urlopen(req, timeout=30):
                return self.MockResponse()

            def mock_post(payload, contribute_url=None, contributor_key=None):
                raise urllib.error.HTTPError(
                    "http://fake/", 401, "Unauthorized", {},
                    io.BytesIO(b'{"error":"unauthorized"}'),
                )

            with unittest.mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
                with unittest.mock.patch(
                    "executors.ingest_article.run.edge_post", side_effect=mock_post
                ):
                    with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                        ret = main(["--url", "https://example.com"])
                        self.assertEqual(ret, 1)
                        output = json.loads(mock_stdout.getvalue())
                        self.assertIn("401", output["error"])

    def test_500_internal_error(self):
        import urllib.error

        with self._patch_env():
            def mock_urlopen(req, timeout=30):
                return self.MockResponse()

            def mock_post(payload, contribute_url=None, contributor_key=None):
                raise urllib.error.HTTPError(
                    "http://fake/", 500, "Internal Server Error", {},
                    io.BytesIO(b"{}"),
                )

            with unittest.mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
                with unittest.mock.patch(
                    "executors.ingest_article.run.edge_post", side_effect=mock_post
                ):
                    with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                        ret = main(["--url", "https://example.com"])
                        self.assertEqual(ret, 1)
                        output = json.loads(mock_stdout.getvalue())
                        self.assertIn("500", output["error"])

    def test_sends_x_contributor_key_header(self):
        with self._patch_env():
            captured_key = None

            def mock_urlopen(req, timeout=30):
                return self.MockResponse()

            def mock_post(payload, contribute_url=None, contributor_key=None):
                nonlocal captured_key
                captured_key = contributor_key
                return {"id": 1, "status": "ok"}

            with unittest.mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
                with unittest.mock.patch(
                    "executors.ingest_article.run.edge_post", side_effect=mock_post
                ):
                    with unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
                        main(["--url", "https://example.com"])
            self.assertEqual(captured_key, self.contributor_key)

    def test_external_id_is_url(self):
        """Verify external_id is set to the article URL in the envelope."""
        with self._patch_env():
            captured_payload = None

            def mock_urlopen(req, timeout=30):
                return self.MockResponse()

            def mock_post(payload, contribute_url=None, contributor_key=None):
                nonlocal captured_payload
                captured_payload = payload
                return {"id": 1, "status": "ok"}

            with unittest.mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
                with unittest.mock.patch(
                    "executors.ingest_article.run.edge_post", side_effect=mock_post
                ):
                    with unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
                        main(["--url", "https://example.com/article"])
            self.assertEqual(
                captured_payload["data"]["external_id"],
                "https://example.com/article",
            )
            self.assertEqual(
                captured_payload["data"]["url"],
                "https://example.com/article",
            )

