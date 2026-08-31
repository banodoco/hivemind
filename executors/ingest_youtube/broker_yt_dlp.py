#!/usr/bin/env python3
"""Validated native yt-dlp launch for Hivemind YouTube ingest."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from urllib.parse import urlsplit


_ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com"}


def _url_arg(argv: list[str]) -> str:
    for value in reversed(argv):
        if value.startswith(("http://", "https://")):
            return value
    raise SystemExit("broker yt-dlp wrapper requires an explicit YouTube URL")


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    url = _url_arg(args)
    parsed = urlsplit(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower().rstrip(".") not in _ALLOWED_HOSTS:
        raise SystemExit("broker yt-dlp wrapper allows only HTTPS YouTube URLs")
    proxy = os.environ.get("ASTRID_BROKER_PROXY", "").strip()
    if not proxy:
        raise SystemExit("broker yt-dlp wrapper requires the host-issued ASTRID_BROKER_PROXY")
    proxy_parts = urlsplit(proxy)
    if proxy_parts.scheme not in {"http", "https"} or not proxy_parts.hostname or proxy_parts.port is None:
        raise SystemExit("broker yt-dlp wrapper received an invalid explicit proxy")
    binary = shutil.which("yt-dlp")
    if not binary:
        raise SystemExit("broker yt-dlp wrapper could not locate the real yt-dlp binary")
    # Ignore any caller-supplied proxy and make the broker-issued endpoint
    # authoritative. Ambient proxy variables are not consulted by this code.
    filtered: list[str] = []
    skip = False
    for value in args:
        if skip:
            skip = False
            continue
        if value == "--proxy":
            skip = True
            continue
        filtered.append(value)
    return subprocess.run([binary, "--proxy", proxy, *filtered], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
