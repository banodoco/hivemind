#!/usr/bin/env python3
"""Fetch a web article, extract readable text, and submit as a resource."""

from __future__ import annotations

import argparse
import html.parser
import re
import sys
import urllib.error
import urllib.request
from typing import Any

# -- dual-import guard (T5 pattern) -------------------------------------------
try:
    from .._common import (
        build_add_resource_envelope,
        dry_run_output,
        edge_post,
        format_error,
        output_json,
        resolve_contribute_url,
        resolve_contributor_key,
    )
except ImportError:
    import os as _os

    _HERE = _os.path.dirname(_os.path.abspath(__file__))
    _EXECUTORS = _os.path.dirname(_HERE)
    sys.path.insert(0, _EXECUTORS)
    from _common import (  # type: ignore[import-not-found]
        build_add_resource_envelope,
        dry_run_output,
        edge_post,
        format_error,
        output_json,
        resolve_contribute_url,
        resolve_contributor_key,
    )


# ---------------------------------------------------------------------------
# HTML text extractor (stdlib only — html.parser)
# ---------------------------------------------------------------------------

# Tags whose inner content should be stripped entirely.
_STRIP_TAGS: frozenset[str] = frozenset({
    "script", "style", "nav", "header", "footer", "aside", "noscript",
})

# Tags that signal a block-level break (insert a newline before content).
_BLOCK_TAGS: frozenset[str] = frozenset({
    "div", "p", "br", "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "tr", "section", "article", "main",
})


class _ArticleExtractor(html.parser.HTMLParser):
    """Extract readable text and title from HTML.

    Strips ``script``, ``style``, ``nav``, ``header``, ``footer``,
    ``aside``, and ``noscript`` elements.  Collapses whitespace.
    """

    def __init__(self) -> None:
        super().__init__()
        self._text_chunks: list[str] = []
        self._strip_depth: int = 0
        self._title: str | None = None
        self._og_title: str | None = None
        self._in_title: bool = False

    # --- public results ------------------------------------------------------

    def get_text(self) -> str:
        """Return extracted text with whitespace collapsed."""
        raw = "".join(self._text_chunks)
        return _collapse_whitespace(raw)

    def get_title(self) -> str | None:
        """Return the ``<title>`` content, if found."""
        return self._title

    def get_og_title(self) -> str | None:
        """Return the ``og:title`` content, if found."""
        return self._og_title

    # --- parser hooks --------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()

        if tag_lower in _STRIP_TAGS:
            self._strip_depth += 1
            return

        if self._strip_depth > 0:
            return

        if tag_lower == "title":
            self._in_title = True

        # og:title from <meta property="og:title" content="...">
        if tag_lower == "meta":
            attrs_dict = {k.lower(): (v or "") for k, v in attrs}
            if attrs_dict.get("property") == "og:title":
                self._og_title = attrs_dict.get("content", "").strip() or None

        if tag_lower in _BLOCK_TAGS:
            # Insert newline before block-level content
            if self._text_chunks and not self._text_chunks[-1].endswith("\n"):
                self._text_chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()

        if tag_lower in _STRIP_TAGS:
            if self._strip_depth > 0:
                self._strip_depth -= 1
            return

        if tag_lower == "title":
            self._in_title = False

        if tag_lower in _BLOCK_TAGS and self._strip_depth == 0:
            # End of block — newline
            if self._text_chunks and not self._text_chunks[-1].endswith("\n"):
                self._text_chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._strip_depth > 0:
            return
        if self._in_title:
            self._title = (self._title or "") + data
            return
        self._text_chunks.append(data)


def _collapse_whitespace(text: str) -> str:
    """Collapse runs of whitespace (including newlines) to single spaces.

    Preserves paragraph breaks (two or more newlines) as a single blank line.
    """
    # Normalise all whitespace to spaces, but keep paragraph breaks
    # (two+ newlines) as a double-newline marker.
    text = re.sub(r"\n{2,}", "\n\n", text)
    text = re.sub(r"[ \t\r]+", " ", text)
    # Collapse single newlines to space
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    # Collapse multiple spaces
    text = re.sub(r" {2,}", " ", text)
    # Collapse runs of \n\n... into exactly \n\n
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_from_html(html: str) -> tuple[str, str | None, str | None]:
    """Parse *html* and return ``(text, title, og_title)``.

    Returns
    -------
    text : str
        Extracted readable text with whitespace collapsed.
    title : str or None
        Content of ``<title>``, trimmed.
    og_title : str or None
        Content of ``og:title`` meta tag, trimmed.
    """
    extractor = _ArticleExtractor()
    extractor.feed(html)
    extractor.close()
    title = extractor.get_title()
    og_title = extractor.get_og_title()
    text = extractor.get_text()
    return text, (title.strip() if title else None), og_title


# ---------------------------------------------------------------------------
# Title resolution
# ---------------------------------------------------------------------------


def resolve_title(
    cli_title: str | None,
    og_title: str | None,
    html_title: str | None,
    fallback: str | None = None,
) -> str:
    """Resolve title in priority order: CLI flag > og:title > <title>.

    Empty/whitespace-only values are treated as absent (a blank ``<title>``
    or ``--title ''`` falls through to the next source).  When nothing usable
    remains, returns *fallback* (typically the source URL) or ``"Untitled"``.
    """
    for candidate in (cli_title, og_title, html_title):
        if candidate and candidate.strip():
            return candidate.strip()
    if fallback and fallback.strip():
        return fallback.strip()
    return "Untitled"


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def fetch_html(url: str) -> str:
    """Fetch *url* with a browser-like User-Agent, return decoded HTML.

    Raises ``urllib.error.URLError`` / ``HTTPError`` on failure.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; HivemindIngest/2.0; "
                "+https://github.com/banodoco/hivemind)"
            ),
            "Accept": "text/html,application/xhtml+xml,*/*",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        # Try to read with the encoding from Content-Type, fall back to utf-8
        raw = resp.read()
        content_type = resp.headers.get("Content-Type", "")
        encoding = "utf-8"
        if "charset=" in content_type:
            encoding = content_type.split("charset=")[-1].split(";")[0].strip()
        return raw.decode(encoding, errors="replace")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hivemind.ingest_article",
        description="Fetch a web article, extract text, and submit as a resource.",
    )
    parser.add_argument(
        "--url", required=True, help="URL of the article to ingest."
    )
    parser.add_argument(
        "--title", help="Override the extracted title."
    )
    parser.add_argument(
        "--kind", default="article", help="Resource kind (default: article)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract and print the envelope without submitting.",
    )
    parser.add_argument(
        "--out", help="Write JSON output to this file instead of stdout."
    )
    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # 1. Fetch and extract
    try:
        html = fetch_html(args.url)
    except urllib.error.HTTPError as exc:
        output_json(
            {"error": f"HTTP {exc.code} fetching {args.url}: {exc.reason}"},
            args.out,
        )
        return 1
    except urllib.error.URLError as exc:
        output_json(
            {"error": f"Failed to fetch {args.url}: {exc.reason}"},
            args.out,
        )
        return 1

    text, html_title, og_title = extract_from_html(html)

    # 2. Resolve title
    title = resolve_title(args.title, og_title, html_title, fallback=args.url)

    # 3. Build add_resource envelope
    data: dict[str, Any] = {
        "kind": args.kind,
        "source": "web",
        "title": title,
        "body": text,
        "url": args.url,
        "external_id": args.url,
    }
    envelope = build_add_resource_envelope(data)

    # 4. Dry-run path
    if args.dry_run:
        dry_run_output(envelope, args.out)
        return 0

    # 5. Real send — requires contributor key
    contributor_key = resolve_contributor_key()
    if not contributor_key:
        output_json(
            {
                "error": "contributor key required",
                "detail": "set HIVEMIND_CONTRIBUTOR_KEY or use --dry-run",
            },
            args.out,
        )
        return 1

    try:
        response = edge_post(envelope, contributor_key=contributor_key)
        output_json(response, args.out)
        return 0
    except urllib.error.HTTPError as exc:
        import json as _json

        body = {}
        try:
            body = _json.loads(exc.read().decode("utf-8"))
        except Exception:
            pass
        output_json({"error": format_error(exc.code, body)}, args.out)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
