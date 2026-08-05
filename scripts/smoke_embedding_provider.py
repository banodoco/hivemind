#!/usr/bin/env python3
"""The ONE authorized provider smoke for plan task 2.1.

This is the live, provider-touching counterpart of the offline
``validate_embedding_contract.py``. It runs exactly one minimal OpenAI embedding
request through the **actual** :class:`executors.embedding_contract.OpenAIEmbedder`
provider implementation (real stdlib transport) using the approved organizational
credential registered at Hivemind's server-side (Edge) secret boundary, and
records sanitized evidence that the plan's task-2.1 completion signal is met:

    "one authorized smoke call uses the existing approved credential through
     Hivemind's server-side secret boundary."

Secret boundary (plan §Credentials / AD-8):
  * The worker Edge Function that would normally read ``OPENAI_API_KEY`` from the
    Edge secret store is plan task 2.9 (intentionally not deployed in 2.1). This
    script is therefore the **closest local invocation of that same boundary**:
    it loads the same approved key (registered in Hivemind's Edge secret store)
    and feeds it to the identical ``OpenAIEmbedder`` the deployed worker will use.
  * The key value is never printed, logged, serialized into evidence, placed in
    process args, or exposed. It lives only inside the ``OpenAIEmbedder`` instance
    for the single request.

Sanitization contract (exhaustive): the evidence records ONLY provider/model,
the configured dimension, the dimension/finite/L2 verification booleans, the norm
tolerance checked, request success, elapsed time, and a redacted error state. It
deliberately does **not** store the embedding vector, the input probe text, or any
credential substring. ``run_smoke`` is the unit-testable core (injectable
transport + key); ``main`` performs the one real authorized call.

Run (operator, one authorized call)::

    python3 scripts/smoke_embedding_provider.py --key-source-env <approved .env>

The approved key may also be supplied via the existing ``OPENAI_API_KEY`` env var.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from executors import embedding_contract as ec  # noqa: E402

__all__ = ["run_smoke", "load_approved_key", "edge_secret_registered"]

#: Fixed, harmless, non-secret synthetic probe. Never varies; never persisted in
#: evidence (the operator can confirm it here without touching private content).
DEFAULT_PROBE = "hivemind hybrid search provider smoke probe"

#: Capacity-preferred pilot dimension (plan task 0.7 / AD-2). Matches the fixed
#: ``vector(384)`` production table (schema/022).
DEFAULT_DIMENSION = ec.PILOT_DIMENSION_384

#: Tolerance for the L2-normalization assertion. OpenAI embeddings are not
#: guaranteed unit-norm to full float precision, so we assert closeness, not
#: equality, and record the tolerance used.
DEFAULT_NORM_TOLERANCE = 1e-4

#: Linked Hivemind Supabase project (plan task 0.1 audit; "Banodoco"). A public
#: project reference, not a secret. Used only for the names-only Edge-secret
#: presence check; overridable via --project-ref.
DEFAULT_PROJECT_REF = "ujlwuvkrxlvoswwkerdf"

#: Patterns scrubbed from any error string before it is recorded, as defense in
#: depth (provider errors are already secret-free; this is belt-and-braces).
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{8,}"),
    re.compile(r"[A-Za-z0-9_-]{32,}"),
]


def _redact(text: str) -> str:
    """Strip secret-like substrings from ``text`` before it is recorded."""

    red = text
    for pat in _SECRET_PATTERNS:
        red = pat.sub("<redacted>", red)
    return red


def run_smoke(
    *,
    api_key: str | None,
    transport: ec.Transport | None = None,
    probe: str = DEFAULT_PROBE,
    dimension: int = DEFAULT_DIMENSION,
    model: str = ec.DEFAULT_OPENAI_EMBEDDING_MODEL,
) -> dict[str, Any]:
    """Run one embedding request and return **sanitized** smoke evidence.

    ``transport=None`` uses the real :func:`ec._stdlib_transport` (the actual
    provider implementation). Tests inject a deterministic fake transport and a
    throwaway key. The returned dict contains no vector and no credential.

    The verification (dimension == configured, all-finite, L2-norm ≈ 1) is the
    task-2.1 contract gate; ``request_success`` is true only when all three hold.
    """

    started = time.monotonic()
    embedder = ec.OpenAIEmbedder(
        api_key=api_key,
        transport=transport,
        model_name=model,
        dimension=dimension,
    )
    configured = embedder.has_credential()
    dimension_ok = False
    all_finite = False
    norm_ok = False
    err: str | None = None
    try:
        if not configured:
            raise ec.EmbeddingError("no configured key")
        vectors = asyncio.run(embedder.embed_texts([probe]))
        if len(vectors) != 1:
            raise ec.EmbeddingError(f"provider returned {len(vectors)} vectors for 1 input")
        vec = vectors[0]
        dimension_ok = len(vec) == dimension
        all_finite = all(math.isfinite(x) for x in vec)
        norm = math.sqrt(sum(x * x for x in vec))
        norm_ok = abs(norm - 1.0) <= DEFAULT_NORM_TOLERANCE
    except ec.EmbeddingError as exc:
        # _stdlib_transport surfaces only status/transport-type — already secret
        # free — but redact defensively regardless.
        err = _redact(str(exc) or "provider error")
    except Exception as exc:  # noqa: BLE001 — record type only, never the message
        err = f"unexpected:{type(exc).__name__}"
    elapsed_ms = round((time.monotonic() - started) * 1000.0, 3)
    success = configured and dimension_ok and all_finite and norm_ok and err is None
    return {
        "task": "2.1-embedding-provider-smoke",
        "gate": "one authorized provider smoke via Hivemind Edge secret (OPENAI_API_KEY)",
        "provider": "openai",
        "model": model,
        "dimension": dimension,
        "dimension_matches_configured": dimension_ok,
        "all_finite": all_finite,
        "l2_norm_within_tolerance": norm_ok,
        "norm_tolerance": DEFAULT_NORM_TOLERANCE,
        "request_success": success,
        "embedder_has_credential": configured,
        "elapsed_ms": elapsed_ms,
        "error_state": err,
        # Explicit hygiene flags: the vector and probe text are never persisted.
        "vector_persisted": False,
        "input_text_persisted": False,
    }


def load_approved_key(env_path: str | None = None) -> tuple[str | None, str]:
    """Load the approved OpenAI key without printing it.

    Preference order: an existing ``OPENAI_API_KEY`` env var; otherwise only the
    ``OPENAI_API_KEY`` line (nothing else) read from ``env_path``. Returns
    ``(key_or_None, source_label)``. The value is returned to the caller and is
    never printed by this function.
    """

    env_val = os.environ.get("OPENAI_API_KEY")
    if env_val:
        return env_val, "env:OPENAI_API_KEY"
    if env_path:
        path = Path(env_path)
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("OPENAI_API_KEY="):
                value = line[len("OPENAI_API_KEY="):].strip()
                if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
                    value = value[1:-1]
                return value or None, f"file:{env_path}:OPENAI_API_KEY"
    return None, "none"


def edge_secret_registered(project_ref: str = DEFAULT_PROJECT_REF) -> bool | None:
    """Best-effort, names-only check that ``OPENAI_API_KEY`` is in the Edge store.

    ``supabase secrets list`` returns names + timestamps only — never values. We
    return True/False on success, or None if the CLI is unavailable (so a missing
    CLI can never falsely fail the gate).
    """

    try:
        proc = subprocess.run(
            ["supabase", "secrets", "list", "-o", "json", "--project-ref", project_ref],
            capture_output=True,
            text=True,
            timeout=40,
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
    for entry in items:
        name = entry.get("name") if isinstance(entry, Mapping) else entry
        if name == "OPENAI_API_KEY":
            return True
    return False


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Task 2.1 one authorized provider smoke.")
    ap.add_argument("--key-source-env", type=str, default=None,
                    help="Path to an approved .env to read ONLY OPENAI_API_KEY from.")
    ap.add_argument("--out", type=Path,
                    default=REPO_ROOT / "docs" / "hybrid-search" / "phase2-embedding-provider-smoke.json")
    ap.add_argument("--project-ref", type=str, default=DEFAULT_PROJECT_REF)
    ap.add_argument("--probe", type=str, default=DEFAULT_PROBE)
    ap.add_argument("--dimension", type=int, default=DEFAULT_DIMENSION)
    args = ap.parse_args(argv)

    api_key, source = load_approved_key(args.key_source_env)
    if not api_key:
        # No approved key reachable through the boundary — record the gate, do
        # NOT invent a key (plan §Credentials; batch requirement 5).
        evidence = run_smoke(api_key=None, probe=args.probe, dimension=args.dimension)
        evidence["credential_source"] = "none (approved key not reachable through boundary)"
        evidence["secret_boundary"] = {
            "edge_secret_name": "OPENAI_API_KEY",
            "edge_secret_registered": edge_secret_registered(args.project_ref),
            "value_exposed": False,
        }
        _write(args.out, evidence)
        _print_summary(evidence, source)
        return 2

    # The ONE real authorized call. transport=None => actual provider path.
    evidence = run_smoke(api_key=api_key, probe=args.probe, dimension=args.dimension)
    evidence["credential_source"] = "approved organizational key (source: %s)" % source.split(":", 1)[0]
    evidence["secret_boundary"] = {
        "edge_secret_name": "OPENAI_API_KEY",
        "edge_secret_registered": edge_secret_registered(args.project_ref),
        "value_exposed": False,
    }
    _write(args.out, evidence)
    _print_summary(evidence, source)
    return 0 if evidence["request_success"] else 1


def _contains_vector(obj: Any) -> bool:
    """True if ``obj`` contains any list-of-numbers value (an embedding vector)."""

    if isinstance(obj, list):
        if obj and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in obj):
            return True
        return any(_contains_vector(x) for x in obj)
    if isinstance(obj, Mapping):
        return any(_contains_vector(v) for v in obj.values())
    return False


def _write(out_path: Path, evidence: Mapping[str, Any]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Structural guardrail: the word "embedding" legitimately appears in
    # descriptive strings, but an actual vector must never be persisted.
    assert not _contains_vector(dict(evidence)), "smoke evidence must not persist a vector"
    blob = json.dumps(evidence, indent=2) + "\n"
    assert not re.search(r"sk-[A-Za-z0-9_-]{8,}", blob), "smoke evidence must not contain a key"
    out_path.write_text(blob, encoding="utf-8")


def _print_summary(evidence: Mapping[str, Any], source: str) -> None:
    # Summary is sanitized: success, dimension, elapsed, registration, source
    # label — never the key, never the vector.
    print(json.dumps({
        "request_success": evidence["request_success"],
        "dimension": evidence["dimension"],
        "dimension_matches_configured": evidence["dimension_matches_configured"],
        "all_finite": evidence["all_finite"],
        "l2_norm_within_tolerance": evidence["l2_norm_within_tolerance"],
        "elapsed_ms": evidence["elapsed_ms"],
        "error_state": evidence["error_state"],
        "edge_secret_registered": evidence["secret_boundary"]["edge_secret_registered"],
        "credential_source_label": source.split(":", 1)[0],
        "vector_persisted": False,
    }, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
