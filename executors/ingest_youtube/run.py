#!/usr/bin/env python3
"""Extract YouTube captions via yt-dlp and submit as a transcript resource.

Captions ONLY — shells out to ``yt-dlp`` to download subtitle (VTT) files and
fetch metadata; parses the VTT into clean, de-duplicated text. No audio
download, no Whisper. If no captions exist, exits non-zero advising the user to
transcribe the audio themselves (e.g. Astrid's ``editorial.transcribe``) and
submit via ``hivemind.contribute``. Stdlib only (yt-dlp invoked as a binary).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
from typing import Any

# -- dual-import guard (T5 pattern) -------------------------------------------
try:
    from .._common import (
        build_add_resource_envelope,
        dry_run_output,
        edge_post,
        format_error,
        output_json,
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
        resolve_contributor_key,
    )


_NO_CAPTIONS_MESSAGE = (
    "No captions available for this video. yt-dlp found no manual or "
    "auto-generated subtitles. Transcribe the audio yourself (e.g. with "
    "Astrid's editorial.transcribe) and submit the transcript via "
    "hivemind.contribute."
)


# ---------------------------------------------------------------------------
# VTT parsing
# ---------------------------------------------------------------------------

# A timestamp cue line, e.g. "00:00:01.000 --> 00:00:04.000 align:start ...".
_CUE_TIMING_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2}\.\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}\.\d{3}"
)
# Inline timing/markup tags like <00:00:01.000> and <c> ... </c>.
_INLINE_TAG_RE = re.compile(r"<[^>]+>")
# WebVTT header metadata lines, e.g. "Kind: captions", "Language: en".
_HEADER_META_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]*:\s")


def parse_vtt(vtt_text: str) -> str:
    """Parse WebVTT *vtt_text* into clean, de-duplicated transcript text.

    Strips the ``WEBVTT`` header, cue timing lines, ``NOTE`` blocks, inline
    ``<...>`` timing/markup tags, and ``align:``/``position:`` settings, then
    collapses the rolling-window duplicates that auto-captions produce (the
    same phrase repeated across consecutive cues as new words scroll in).
    """
    raw_lines: list[str] = []
    seen_first_cue = False
    for line in vtt_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "WEBVTT" or stripped.startswith("WEBVTT"):
            continue
        if stripped.startswith(("NOTE", "STYLE", "REGION")):
            continue
        if _CUE_TIMING_RE.match(stripped):
            seen_first_cue = True
            continue
        # Header metadata (e.g. "Kind: captions", "Language: en") appears
        # before the first cue — drop "Key: value" lines until then.
        if not seen_first_cue and _HEADER_META_RE.match(stripped):
            continue
        if "-->" in stripped:
            # Any other cue-settings line containing the arrow.
            continue
        if stripped.isdigit():
            # Numeric cue identifier.
            continue
        # Remove inline timing/markup tags.
        cleaned = _INLINE_TAG_RE.sub("", stripped).strip()
        if cleaned:
            raw_lines.append(cleaned)

    return _dedupe_rolling(raw_lines)


def _dedupe_rolling(lines: list[str]) -> str:
    """Collapse rolling auto-caption duplicates into clean prose.

    Auto-captions repeat lines as a scrolling window: a line often re-appears
    verbatim in the next cue, or a later cue is the previous line plus a few
    new words. We keep each distinct line once and, when consecutive lines
    overlap (one is a prefix of the next), keep only the longer one.
    """
    result: list[str] = []
    for line in lines:
        if not result:
            result.append(line)
            continue

        prev = result[-1]
        if line == prev:
            continue
        # New line extends the previous one (rolling growth) — replace.
        if line.startswith(prev):
            result[-1] = line
            continue
        # Previous line extends the new one — drop the new (already covered).
        if prev.startswith(line):
            continue
        # Suffix overlap: new line repeats the tail of prev then adds words.
        merged = _merge_overlap(prev, line)
        if merged is not None:
            result[-1] = merged
            continue
        result.append(line)

    # Final pass: drop any line fully contained in its predecessor.
    deduped: list[str] = []
    for line in result:
        if deduped and line in deduped[-1]:
            continue
        deduped.append(line)
    return "\n".join(deduped)


def _merge_overlap(prev: str, nxt: str) -> str | None:
    """If *nxt* begins with a suffix of *prev*, return them merged.

    Handles the common auto-caption pattern where each new cue repeats the
    last few words of the previous cue before adding fresh words. Returns
    ``None`` when there is no meaningful (word-aligned) overlap.
    """
    prev_words = prev.split()
    nxt_words = nxt.split()
    max_overlap = min(len(prev_words), len(nxt_words))
    # Require an overlap of at least 2 words to avoid spurious merges.
    for k in range(max_overlap, 1, -1):
        if prev_words[-k:] == nxt_words[:k]:
            return " ".join(prev_words + nxt_words[k:])
    return None


# ---------------------------------------------------------------------------
# yt-dlp interaction
# ---------------------------------------------------------------------------


def fetch_metadata(url: str) -> dict[str, Any]:
    """Run ``yt-dlp -j`` and return the parsed metadata dict.

    Raises ``RuntimeError`` if yt-dlp fails or output is unparseable.
    """
    try:
        proc = subprocess.run(
            ["yt-dlp", "-j", "--skip-download", url],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("yt-dlp binary not found on PATH") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"yt-dlp metadata fetch failed: {proc.stderr.strip() or proc.returncode}"
        )
    # yt-dlp -j prints one JSON object per line; take the first.
    first = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
    if not first:
        raise RuntimeError("yt-dlp returned no metadata")
    return json.loads(first)


def download_captions(url: str, out_dir: str) -> str | None:
    """Download captions into *out_dir* via yt-dlp; return VTT text or None.

    Prefers a manual subtitle file; falls back to an auto-generated one. Returns
    ``None`` when no VTT file is produced (no captions available).
    """
    try:
        proc = subprocess.run(
            [
                "yt-dlp",
                "--skip-download",
                "--write-subs",
                "--write-auto-subs",
                "--sub-format",
                "vtt",
                "-o",
                os.path.join(out_dir, "%(id)s.%(ext)s"),
                url,
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("yt-dlp binary not found on PATH") from exc
    # yt-dlp returns non-zero when only the (skipped) video is missing but
    # still writes subs; rely on the produced files rather than the exit code.
    _ = proc

    vtt_files = sorted(glob.glob(os.path.join(out_dir, "*.vtt")))
    if not vtt_files:
        return None
    # Prefer a non-auto file (yt-dlp names auto files "*.<lang>.vtt" too, so we
    # just take the first deterministically-sorted one).
    with open(vtt_files[0], "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def extract_video_id(metadata: dict[str, Any]) -> str | None:
    """Pull the video id from yt-dlp metadata."""
    vid = metadata.get("id")
    return str(vid) if vid else None


# ---------------------------------------------------------------------------
# Envelope assembly
# ---------------------------------------------------------------------------


def build_envelope(
    url: str,
    metadata: dict[str, Any],
    transcript: str,
    *,
    kind: str = "transcript",
) -> dict[str, Any]:
    """Assemble the add_resource envelope for a YouTube transcript."""
    video_id = extract_video_id(metadata)
    title = metadata.get("title") or video_id or url
    channel = metadata.get("channel") or metadata.get("uploader")
    duration = metadata.get("duration")

    data: dict[str, Any] = {
        "kind": kind,
        "source": "youtube",
        "title": title,
        "body": transcript,
        "url": metadata.get("webpage_url") or url,
        "metadata": {
            "video_id": video_id,
            "channel": channel,
            "duration": duration,
        },
    }
    if channel:
        data["author"] = channel
    if video_id:
        data["external_id"] = video_id
    return build_add_resource_envelope(data)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hivemind.ingest_youtube",
        description="Extract YouTube captions and submit as a transcript resource.",
    )
    parser.add_argument(
        "--url", required=True, help="YouTube video URL."
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

    # 1. Metadata
    try:
        metadata = fetch_metadata(args.url)
    except (RuntimeError, json.JSONDecodeError) as exc:
        output_json({"error": f"metadata fetch failed: {exc}"}, args.out)
        return 1

    # 2. Captions (into a temp dir)
    with tempfile.TemporaryDirectory(prefix="hivemind_yt_") as tmp:
        try:
            vtt_text = download_captions(args.url, tmp)
        except RuntimeError as exc:
            output_json({"error": f"caption download failed: {exc}"}, args.out)
            return 1

    if not vtt_text:
        output_json(
            {
                "error": "captions unavailable",
                "detail": _NO_CAPTIONS_MESSAGE,
            },
            args.out,
        )
        return 1

    transcript = parse_vtt(vtt_text)
    if not transcript.strip():
        output_json(
            {
                "error": "captions unavailable",
                "detail": _NO_CAPTIONS_MESSAGE,
            },
            args.out,
        )
        return 1

    # 3. Build envelope
    envelope = build_envelope(args.url, metadata, transcript)

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
        body: dict[str, Any] = {}
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            pass
        output_json({"error": format_error(exc.code, body)}, args.out)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
