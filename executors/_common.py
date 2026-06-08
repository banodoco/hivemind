#!/usr/bin/env python3
"""Shared helpers for all Hivemind executors — stdlib only.

These functions centralise environment resolution, HTTP calls, envelope
construction, cite parsing, truncation, error formatting, and JSON output.
Every executor imports from this module and uses the same dual-import guard:

    try:
        from .._common import (
            resolve_endpoint, resolve_anon_key, ...
        )
    except ImportError:
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from _common import (
            resolve_endpoint, resolve_anon_key, ...
        )
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

# ---------------------------------------------------------------------------
# Defaults (baked in — overridable via environment variables)
# ---------------------------------------------------------------------------

_DEFAULT_API_URL = "https://ujlwuvkrxlvoswwkerdf.supabase.co/rest/v1"
_DEFAULT_ANON_KEY = "sb_publishable_O38oPBafrBoFrpi_rlWJvA_UJrulFsx"
_DEFAULT_CONTRIBUTE_URL = "https://ujlwuvkrxlvoswwkerdf.supabase.co/functions/v1/contribute"
_DEFAULT_REFRESH_MEDIA_URL = "https://ujlwuvkrxlvoswwkerdf.supabase.co/functions/v1/refresh-media-urls"
_BODY_TRUNCATION_LIMIT = 700

# ---------------------------------------------------------------------------
# Environment resolution
# ---------------------------------------------------------------------------


def resolve_endpoint() -> str:
    """Return the PostgREST base URL (no trailing slash)."""
    return os.environ.get("HIVEMIND_API_URL", _DEFAULT_API_URL).rstrip("/")


def resolve_anon_key() -> str:
    """Return the anon (publishable) key for public read queries."""
    return os.environ.get("HIVEMIND_ANON_KEY", _DEFAULT_ANON_KEY)


def resolve_contributor_key() -> str | None:
    """Return the contributor key for writes, or *None* if not set."""
    return os.environ.get("HIVEMIND_CONTRIBUTOR_KEY")


def resolve_contribute_url() -> str:
    """Return the contribute edge-function URL."""
    return os.environ.get("HIVEMIND_CONTRIBUTE_URL", _DEFAULT_CONTRIBUTE_URL).rstrip("/")


def resolve_refresh_media_url() -> str:
    """Return the Discord media refresh edge-function URL."""
    return os.environ.get("HIVEMIND_REFRESH_MEDIA_URL", _DEFAULT_REFRESH_MEDIA_URL).rstrip("/")


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only — urllib)
# ---------------------------------------------------------------------------


def _http_get(url: str, headers: dict[str, str]) -> dict[str, Any]:
    """Perform a GET and return parsed JSON (dict).

    Raises *urllib.error.HTTPError* on non-2xx status.
    """
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)  # type: ignore[no-any-return]


def _http_post(url: str, headers: dict[str, str], body: bytes) -> dict[str, Any]:
    """Perform a POST and return parsed JSON (dict).

    Raises *urllib.error.HTTPError* on non-2xx status.
    """
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)  # type: ignore[no-any-return]


def postgrest_get(
    path: str,
    params: dict[str, str] | None = None,
    *,
    endpoint: str | None = None,
    anon_key: str | None = None,
) -> dict[str, Any]:
    """Issue a GET against the PostgREST API.

    Parameters
    ----------
    path:
        Relative path, e.g. ``"unified_feed"``.
    params:
        Query-string parameters (e.g. ``{"select": "*", "limit": "20"}``).
    endpoint:
        Base URL override (defaults to :func:`resolve_endpoint`).
    anon_key:
        API key override (defaults to :func:`resolve_anon_key`).

    Returns the parsed JSON response body (always a dict — PostgREST
    returns either an object or an array, both are valid JSON).
    """
    base = (endpoint or resolve_endpoint()).rstrip("/")
    url = f"{base}/{path.lstrip('/')}"
    if params:
        qs = urllib.parse.urlencode(params)
        url = f"{url}?{qs}"
    headers = {
        "apikey": anon_key or resolve_anon_key(),
        "Accept": "application/json",
    }
    return _http_get(url, headers)


def edge_post(
    payload: dict[str, Any],
    *,
    contribute_url: str | None = None,
    contributor_key: str | None = None,
) -> dict[str, Any]:
    """POST to the contribute edge function.

    Parameters
    ----------
    payload:
        The ``{action, data}`` envelope.
    contribute_url:
        Edge-function URL override.
    contributor_key:
        ``hm_<64 hex>`` key override.  Must be provided either via this
        parameter or the ``HIVEMIND_CONTRIBUTOR_KEY`` environment variable.

    Returns the parsed JSON response from the edge function.

    Raises
    ------
    ValueError:
        If no contributor key is available.
    urllib.error.HTTPError:
        On HTTP error responses (400/401/409/500 etc.).
    """
    key = contributor_key or resolve_contributor_key()
    if not key:
        raise ValueError(
            "contributor key required — set HIVEMIND_CONTRIBUTOR_KEY or pass contributor_key="
        )
    url = (contribute_url or resolve_contribute_url()).rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "X-Contributor-Key": key,
        "Accept": "application/json",
    }
    return _http_post(url, headers, json.dumps(payload).encode("utf-8"))


def public_edge_post(
    payload: dict[str, Any],
    *,
    url: str,
    anon_key: str | None = None,
) -> dict[str, Any]:
    """POST to a public Supabase edge function using the anon key.

    This is for read/refresh surfaces such as ``refresh-media-urls`` that
    authenticate with the publishable key rather than ``X-Contributor-Key``.
    """
    key = anon_key or resolve_anon_key()
    headers = {
        "Authorization": f"Bearer {key}",
        "apikey": key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    return _http_post(url.rstrip("/"), headers, json.dumps(payload).encode("utf-8"))


# ---------------------------------------------------------------------------
# Envelope builders
# ---------------------------------------------------------------------------


def build_add_resource_envelope(data: dict[str, Any]) -> dict[str, Any]:
    """Construct a complete ``add_resource`` request envelope.

    Parameters
    ----------
    data:
        Must contain ``kind``, ``source``, ``title``, ``body``.
        Optional: ``external_id``, ``author``, ``url``, ``metadata``,
        ``payload``.
    """
    return {
        "action": "add_resource",
        "data": data,
    }


def build_submit_distillation_envelope(data: dict[str, Any]) -> dict[str, Any]:
    """Construct a complete ``submit_distillation`` request envelope.

    Parameters
    ----------
    data:
        Must contain ``question``, ``answer``, ``confidence``, ``cites``.
        Optional: ``conditions``, ``supersedes_id``.
    """
    return {
        "action": "submit_distillation",
        "data": data,
    }


# ---------------------------------------------------------------------------
# Cite parsing
# ---------------------------------------------------------------------------

# Valid cite item kinds (must match the DB constraint and protocol.ts).
_VALID_CITE_ITEM_KINDS = frozenset({"message", "resource", "distillation"})


def parse_cites(cites_str: str) -> list[dict[str, object]]:
    """Parse a ``--cites`` CLI string into a list of cite objects.

    Expected format: ``"message:88123,resource:17"``.

    Returns a list of ``{"item_kind": "<kind>", "item_id": <int>}`` dicts.

    Raises *ValueError* if any element is malformed.
    """
    if not cites_str.strip():
        raise ValueError("cites string must not be empty")

    results: list[dict[str, object]] = []
    for chunk in cites_str.split(","):
        chunk = chunk.strip()
        if ":" not in chunk:
            raise ValueError(
                f"invalid cite '{chunk}': expected format 'kind:id' (e.g. message:88123)"
            )
        kind, _, id_str = chunk.partition(":")
        kind = kind.strip()
        id_str = id_str.strip()

        if kind not in _VALID_CITE_ITEM_KINDS:
            raise ValueError(
                f"invalid cite kind '{kind}': must be one of "
                f"{sorted(_VALID_CITE_ITEM_KINDS)!r}"
            )
        if not id_str.isdigit():
            raise ValueError(
                f"invalid cite id '{id_str}': must be a positive integer"
            )
        item_id = int(id_str)
        if item_id < 1:
            raise ValueError(
                f"invalid cite id {item_id}: must be >= 1"
            )
        # item_id travels as a STRING: Discord snowflake ids exceed the
        # float64-safe integer range, so a JSON number would be silently
        # rounded by the edge function's JSON.parse.
        results.append({"item_kind": kind, "item_id": str(item_id)})
    return results


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------


def read_body_file(path: str) -> str:
    """Read and return the contents of *path* (UTF-8).

    Raises *FileNotFoundError* or *OSError* on failure.
    """
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


def truncate_body(
    body: str,
    max_chars: int = _BODY_TRUNCATION_LIMIT,
) -> dict[str, object]:
    """Return a dict with ``body`` (possibly truncated) and a ``truncated`` flag.

    Parameters
    ----------
    body:
        The full text.
    max_chars:
        Character limit (default 700).

    Returns
    -------
    dict with keys ``"body"`` (str) and ``"truncated"`` (bool).
    """
    if len(body) <= max_chars:
        return {"body": body, "truncated": False}
    return {"body": body[:max_chars], "truncated": True}


# ---------------------------------------------------------------------------
# Error formatting
# ---------------------------------------------------------------------------


def format_error(status: int, body: dict[str, Any]) -> str:
    """Map a contribute API error response to a human-readable message.

    Parameters
    ----------
    status:
        HTTP status code.
    body:
        Parsed JSON response body (may be empty or malformed).

    Returns a single-line message string.
    """
    if status == 400:
        detail = body.get("detail", "bad request")
        return f"400 validation error: {detail}"
    if status == 401:
        return "401 unauthorized — contributor key is missing, invalid, or revoked"
    if status == 409:
        existing_id = body.get("existing_id", "?")
        detail = body.get("detail", "duplicate")
        return f"409 duplicate (existing_id={existing_id}): {detail}"
    if status == 500:
        return "500 internal server error — try again later"
    return f"{status} error: {body}"


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


def output_json(data: object, out_path: str | None = None) -> None:
    """Write *data* as pretty-printed JSON to *out_path* or stdout.

    Parameters
    ----------
    data:
        JSON-serialisable object.
    out_path:
        Output file path.  Prints to stdout when ``None``.
    """
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if out_path:
        from pathlib import Path

        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


# ---------------------------------------------------------------------------
# Dry-run helper
# ---------------------------------------------------------------------------


def dry_run_output(envelope: dict[str, Any], out_path: str | None = None) -> None:
    """Print (or write) an annotated dry-run envelope.

    Wraps the envelope in ``{"dry_run": true, "envelope": <envelope>}``
    so callers can distinguish dry-run output from real API responses.
    """
    payload: dict[str, object] = {"dry_run": True, "envelope": envelope}
    output_json(payload, out_path)
