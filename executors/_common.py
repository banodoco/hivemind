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
import hashlib
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# ---------------------------------------------------------------------------
# Defaults (baked in — overridable via environment variables)
# ---------------------------------------------------------------------------

_DEFAULT_API_URL = "https://ujlwuvkrxlvoswwkerdf.supabase.co/rest/v1"
_DEFAULT_ANON_KEY = "sb_publishable_O38oPBafrBoFrpi_rlWJvA_UJrulFsx"
_DEFAULT_CONTRIBUTE_URL = "https://ujlwuvkrxlvoswwkerdf.supabase.co/functions/v1/contribute"
_DEFAULT_REFRESH_MEDIA_URL = "https://ujlwuvkrxlvoswwkerdf.supabase.co/functions/v1/refresh-media-urls"
_DEFAULT_AUTH_BROKER_URL = "https://ujlwuvkrxlvoswwkerdf.supabase.co/functions/v1/contributor-auth"
_BODY_TRUNCATION_LIMIT = 700
_CONTRIBUTOR_KEY_RE = re.compile(r"^hm_[0-9a-f]{64}$")

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
    """Return the contributor key from the environment or standard key file."""
    env_key = os.environ.get("HIVEMIND_CONTRIBUTOR_KEY")
    if env_key and env_key.strip():
        return env_key.strip()

    key_path = contributor_key_path()
    try:
        with open(key_path, encoding="utf-8") as handle:
            file_key = handle.read().strip()
    except (FileNotFoundError, OSError):
        return None
    return file_key or None


def hash_contributor_key(key: str) -> str:
    """Return the storage digest for a strict contributor key."""
    if not _CONTRIBUTOR_KEY_RE.fullmatch(key):
        raise ValueError("contributor key must be hm_<64 lowercase hex chars>")
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def contributor_key_path(home: str | None = None) -> str:
    """Return the local owner-only key path without reading its contents."""
    root = home or os.environ.get("HOME")
    if not root:
        root = os.path.expanduser("~")
    return os.path.join(root, ".hivemind", "key")


def write_contributor_key(key: str, *, home: str | None = None) -> str:
    """Atomically replace the local key with mode 0600 and return its path."""
    if not _CONTRIBUTOR_KEY_RE.fullmatch(key):
        raise ValueError("broker returned an invalid contributor key")
    path = contributor_key_path(home)
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=".key.", dir=directory, text=True)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(key + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return path


def delete_local_contributor_key(*, home: str | None = None) -> bool:
    """Delete only the local key file; server revocation is a separate action."""
    try:
        os.unlink(contributor_key_path(home))
        return True
    except FileNotFoundError:
        return False


def resolve_auth_broker_url() -> str:
    """Return the contributor-auth broker edge-function URL."""
    return os.environ.get("HIVEMIND_AUTH_BROKER_URL", _DEFAULT_AUTH_BROKER_URL).rstrip("/")


def resolve_contribute_url() -> str:
    """Return the contribute edge-function URL."""
    return os.environ.get("HIVEMIND_CONTRIBUTE_URL", _DEFAULT_CONTRIBUTE_URL).rstrip("/")


def resolve_refresh_media_url() -> str:
    """Return the Discord media refresh edge-function URL."""
    return os.environ.get("HIVEMIND_REFRESH_MEDIA_URL", _DEFAULT_REFRESH_MEDIA_URL).rstrip("/")


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only — urllib)
# ---------------------------------------------------------------------------


def _http_get(url: str, headers: dict[str, str], timeout: float = 30.0) -> dict[str, Any]:
    """Perform a GET and return parsed JSON (dict).

    Raises *urllib.error.HTTPError* on non-2xx status.
    """
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
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
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Issue a GET against the PostgREST API.

    Parameters
    ----------
    path:
        Relative path, e.g. ``"message_feed"``.
    params:
        Query-string parameters (e.g. ``{"select": "*", "limit": "20"}``).
    endpoint:
        Base URL override (defaults to :func:`resolve_endpoint`).
    anon_key:
        API key override (defaults to :func:`resolve_anon_key`).
    timeout:
        Per-request timeout in seconds (default 30).

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
    return _http_get(url, headers, timeout=timeout)


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
    anon_key = resolve_anon_key()
    headers = {
        "Authorization": f"Bearer {anon_key}",
        "apikey": anon_key,
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


def auth_post(
    payload: dict[str, Any],
    *,
    broker_url: str | None = None,
    anon_key: str | None = None,
) -> dict[str, Any]:
    """Call the contributor-auth broker without ever putting secrets in URLs."""
    key = anon_key or resolve_anon_key()
    headers = {
        "Authorization": f"Bearer {key}",
        "apikey": key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    return _http_post(
        (broker_url or resolve_auth_broker_url()).rstrip("/"),
        headers,
        json.dumps(payload).encode("utf-8"),
    )


# ---------------------------------------------------------------------------
# Envelope builders
# ---------------------------------------------------------------------------


def build_knowledge_model_envelope(action: str, data: dict[str, Any]) -> dict[str, Any]:
    """Build a T2-T6 contribution envelope with caller-scoped retry data."""
    if not action or not isinstance(data, dict):
        raise ValueError("knowledge-model action and object data are required")
    return {"action": action, "data": data}


def build_submit_resource_envelope(data: dict[str, Any], *, idempotency_token: str | None = None) -> dict[str, Any]:
    """Build the sole initial-resource write envelope used by ingestors.

    The token is deterministic for source-owned identities, making a retry
    safe without creating a second contribution endpoint.
    """
    if not isinstance(data, dict):
        raise ValueError("resource data must be an object")
    if idempotency_token:
        token = idempotency_token
    else:
        # The source identity makes retries for the same representation stable;
        # the content digest makes a changed re-import a new proposal instead of
        # colliding with the original submit_resource idempotency row.  Keep the
        # token bounded because the database contract caps it at 200 characters.
        source = str(data.get("origin_source") or "unknown")[:32]
        identity = str(data.get("origin_external_id") or data.get("title") or "untitled")
        identity_digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        request = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        content_digest = hashlib.sha256(request.encode("utf-8")).hexdigest()[:32]
        token = f"ingest:{source}:{identity_digest}:{content_digest}"
    return build_knowledge_model_envelope("submit_resource", {"idempotency_token": token, **data})


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
