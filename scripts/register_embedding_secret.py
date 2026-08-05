#!/usr/bin/env python3
"""Register ONLY ``OPENAI_API_KEY`` in Hivemind's Edge secret store (plan 2.1).

Plan task 0.1 named ``OPENAI_API_KEY`` in Hivemind's Supabase Edge secret store
as the single "remaining setup action" before the one live embedding smoke. This
script performs that registration using a **secret-safe mechanism**:

  * It uploads ONLY ``OPENAI_API_KEY`` — never every variable from a source
    ``.env`` (a narrow upload, not a broad secrets upload).
  * The value never appears in process args / titles: it is carried through a
    0600 temp file consumed by ``supabase secrets set --env-file`` and unlinked
    immediately afterward. The CLI arg is a throwaway path, not the value.
  * The value is never printed, logged, or serialized into evidence. CLI output
    is defensively redacted before display.
  * It targets the linked Hivemind project only, and confirms registration by a
    names-only ``secrets list`` (the CLI never returns secret values).

No new credential is created; the approved organizational key is read from an
authorized local source. Run (operator)::

    python3 scripts/register_embedding_secret.py --source-env <approved .env>

Exit codes: 0 registered (or already present), 1 CLI/upload error, 2 no key.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence

#: Linked Hivemind Supabase project (plan task 0.1; "Banodoco"). Public ref.
DEFAULT_PROJECT_REF = "ujlwuvkrxlvoswwkerdf"

#: Patterns scrubbed from any CLI output before display (defense in depth).
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{8,}"),
    re.compile(r"[A-Za-z0-9_-]{32,}"),
]


def _redact(text: str) -> str:
    red = text or ""
    for pat in _SECRET_PATTERNS:
        red = pat.sub("<redacted>", red)
    return red


def extract_openai_key(env_text: str) -> str | None:
    """Return ONLY the ``OPENAI_API_KEY`` value from ``env_text`` (testable).

    Handles optional surrounding quotes. Returns None if absent. Pure parser —
    no I/O, no real secret.
    """

    for raw in env_text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        if line.startswith("OPENAI_API_KEY="):
            value = line[len("OPENAI_API_KEY="):].strip()
            if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
                value = value[1:-1]
            return value or None
    return None


def _list_secret_names(project_ref: str) -> set[str] | None:
    """Names-only ``secrets list`` (the CLI returns no values). None on failure."""

    try:
        proc = subprocess.run(
            ["supabase", "secrets", "list", "-o", "json", "--project-ref", project_ref],
            capture_output=True, text=True, timeout=40,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    items = data if isinstance(data, list) else list(data.values())
    names: set[str] = set()
    for entry in items:
        name = entry.get("name") if isinstance(entry, dict) else entry
        if isinstance(name, str):
            names.add(name)
    return names


def register(*, source_env: str | None, project_ref: str = DEFAULT_PROJECT_REF,
             force: bool = False) -> int:
    names = _list_secret_names(project_ref)
    if names is not None and "OPENAI_API_KEY" in names and not force:
        print(json.dumps({
            "action": "already_registered",
            "edge_secret_name": "OPENAI_API_KEY",
            "edge_secret_registered": True,
            "project_ref": project_ref,
            "value_exposed": False,
        }, indent=2))
        return 0

    # Load ONLY OPENAI_API_KEY from the approved source; never print it.
    raw_key = os.environ.get("OPENAI_API_KEY")
    source_label = "env:OPENAI_API_KEY"
    if not raw_key and source_env:
        raw_key = extract_openai_key(Path(source_env).read_text(encoding="utf-8"))
        source_label = "file:%s" % source_env
    if not raw_key:
        print(json.dumps({
            "action": "no_key", "edge_secret_registered": False,
            "reason": "OPENAI_API_KEY not present in approved source",
            "value_exposed": False,
        }, indent=2))
        return 2

    # Carry the value only through a private 0600 temp file consumed by --env-file.
    fd, tmp_path = tempfile.mkstemp(prefix="hm_openai_key_", suffix=".env")
    os.close(fd)
    os.chmod(tmp_path, 0o600)
    rc = 1
    redacted_stdout = ""
    redacted_stderr = ""
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write("OPENAI_API_KEY=" + raw_key + "\n")  # ONLY this one var
        proc = subprocess.run(
            ["supabase", "secrets", "set", "--env-file", tmp_path,
             "--project-ref", project_ref],
            capture_output=True, text=True,
        )
        rc = proc.returncode
        redacted_stdout = _redact(proc.stdout)
        redacted_stderr = _redact(proc.stderr)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    confirmed = None
    if rc == 0:
        after = _list_secret_names(project_ref)
        confirmed = bool(after is not None and "OPENAI_API_KEY" in after)

    print(json.dumps({
        "action": "registered" if rc == 0 else "upload_failed",
        "edge_secret_name": "OPENAI_API_KEY",
        "edge_secret_registered": confirmed if confirmed is not None else False,
        "project_ref": project_ref,
        "source_label_kind": source_label.split(":", 1)[0],
        "uploaded_vars": ["OPENAI_API_KEY"],
        "cli_returncode": rc,
        "cli_stdout_tail": redacted_stdout[-200:],
        "cli_stderr_tail": redacted_stderr[-200:],
        "value_exposed": False,
        "temp_file_unlinked": True,
    }, indent=2))
    return 0 if (rc == 0 and confirmed) else 1


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Register ONLY OPENAI_API_KEY in Hivemind's Edge secret store.")
    ap.add_argument("--source-env", type=str, default=None,
                    help="Approved .env to read ONLY OPENAI_API_KEY from.")
    ap.add_argument("--project-ref", type=str, default=DEFAULT_PROJECT_REF)
    ap.add_argument("--force", action="store_true",
                    help="Re-register even if already present.")
    args = ap.parse_args(argv)
    if not args.source_env and not os.environ.get("OPENAI_API_KEY"):
        ap.error("--source-env PATH (or an OPENAI_API_KEY env var) is required")
    return register(source_env=args.source_env, project_ref=args.project_ref, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
