"""Small shared Hivemind authentication CLI.

Reads remain available through the existing executor commands.  This command
owns only the contributor-key login lifecycle; the key is written locally with
owner-only permissions and is never included in structured output.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import time
import urllib.error
import webbrowser
from typing import Any

try:
    from .executors._common import (
        auth_post,
        delete_local_contributor_key,
        resolve_contributor_key,
        write_contributor_key,
    )
except ImportError:  # pragma: no cover - direct checkout invocation
    from executors._common import (  # type: ignore[import-not-found]
        auth_post,
        delete_local_contributor_key,
        resolve_contributor_key,
        write_contributor_key,
    )


def _json(value: dict[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True))


def _error_body(exc: urllib.error.HTTPError) -> dict[str, Any]:
    try:
        value = json.loads(exc.read().decode("utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _login(args: argparse.Namespace) -> int:
    request_token = secrets.token_urlsafe(32)
    poll_secret = secrets.token_urlsafe(32)
    approval_code = secrets.token_hex(4).upper()
    try:
        created = auth_post({
            "action": "create",
            "request_token": request_token,
            "poll_secret": poll_secret,
            "approval_code": approval_code,
            "machine_label": args.machine or socket.gethostname(),
            "ttl_seconds": args.ttl,
        })
    except urllib.error.HTTPError as exc:
        _json({"error": "broker_create_failed", "status": exc.code, "detail": _error_body(exc).get("detail")})
        return 1

    url = created.get("approval_url")
    if not isinstance(url, str) or not url:
        _json({"error": "broker_create_failed", "detail": "broker did not return an approval URL"})
        return 1
    # The URL contains only the short-lived request capability and approval
    # code.  The CLI-held polling secret and contributor key never enter it.
    print(url)
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        try:
            polled = auth_post({
                "action": "poll", "request_token": request_token, "poll_secret": poll_secret,
            })
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                time.sleep(min(args.interval, max(0.0, deadline - time.monotonic())))
                continue
            _json({"error": "broker_poll_failed", "status": exc.code})
            return 1
        status = polled.get("status")
        if status == "approved":
            try:
                redeemed = auth_post({
                    "action": "redeem", "request_token": request_token, "poll_secret": poll_secret,
                })
                key = redeemed.get("key")
                if not isinstance(key, str):
                    raise ValueError("broker did not return a key")
                path = write_contributor_key(key)
            except (urllib.error.HTTPError, ValueError, OSError) as exc:
                _json({"error": "broker_redeem_failed", "detail": str(exc) if isinstance(exc, ValueError) else "redemption failed"})
                return 1
            _json({"ok": True, "status": "authenticated", "key_file": path})
            return 0
        if status in {"expired", "revoked", "consumed"}:
            _json({"error": "broker_request_unavailable", "status": status})
            return 1
        time.sleep(min(args.interval, max(0.0, deadline - time.monotonic())))
    _json({"error": "broker_timeout", "detail": "approval request expired or was not approved; restart login to try again"})
    return 1


def _status(_args: argparse.Namespace) -> int:
    key = resolve_contributor_key()
    if not key:
        _json({"status": "logged_out"})
        return 0
    source = "environment" if os.environ.get("HIVEMIND_CONTRIBUTOR_KEY", "").strip() else "file"
    try:
        status = auth_post({"action": "status", "key": key})
    except urllib.error.HTTPError as exc:
        _json({"status": "unknown", "source": source, "server_status": exc.code})
        return 1
    _json({"status": status.get("status", "unknown"), "source": source, "key_id": status.get("key_id")})
    return 0


def _logout(_args: argparse.Namespace) -> int:
    removed = delete_local_contributor_key()
    _json({"status": "logged_out", "local_key_removed": removed})
    return 0


def _revoke(_args: argparse.Namespace) -> int:
    key = resolve_contributor_key()
    if not key:
        _json({"error": "login_required", "detail": "no contributor key is configured"})
        return 1
    try:
        result = auth_post({"action": "revoke", "key": key})
    except urllib.error.HTTPError as exc:
        _json({"error": "revoke_failed", "status": exc.code})
        return 1
    _json({"status": "revoked" if result.get("revoked") else "not_found"})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hivemind", description="Hivemind public reads and contributor authentication.")
    commands = parser.add_subparsers(dest="command", required=True)
    auth = commands.add_parser("auth", help="Manage contributor authentication.")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    login = auth_commands.add_parser("login", help="Start browser approval and save a device key.")
    login.add_argument("--no-browser", action="store_true", help="Print the approval URL without opening a browser.")
    login.add_argument("--machine", help="Machine label shown during approval.")
    login.add_argument("--ttl", type=int, default=600, help="Request lifetime in seconds (60-1800).")
    login.add_argument("--timeout", type=float, default=600, help="Maximum local polling time in seconds.")
    login.add_argument("--interval", type=float, default=2.0, help="Polling interval in seconds.")
    auth_commands.add_parser("status", help="Show redacted local/server key status.")
    auth_commands.add_parser("logout", help="Delete only the local key file.")
    auth_commands.add_parser("revoke", help="Revoke the configured server-side device key.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "auth":
        return 2
    return {"login": _login, "status": _status, "logout": _logout, "revoke": _revoke}[args.auth_command](args)


if __name__ == "__main__":
    raise SystemExit(main())
