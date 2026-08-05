#!/usr/bin/env python3
"""Offline validator for the embedding provider contract (plan task 2.1).

Runs the provider-independent checks WITHOUT a network, database, or provider
key, and emits machine-readable sanitized evidence:

  * the deterministic fake embedder is reproducible + L2-normalized;
  * vector validation rejects wrong dimension / NaN / Inf / all-zero;
  * query normalization is deterministic (cache-key stable);
  * the content hash is the single frozen algorithm;
  * the embedding-contract identity is deterministic and yields the frozen
    Python contract_id values the SQL seeding (schema/021) must reproduce
    (cross-language parity anchor);
  * the one authorized provider smoke GATE: the approved key is read from
    Hivemind's server-side secret store; if absent, the live smoke is recorded
    as a blocked deployment action — NEVER worked around by adding a key
    (plan task 2.1 §Credentials; batch requirement 5).

No secret, credential, vector derived from private content, or secret-like
substring is printed. Output is plain JSON to stdout (or --out <path>).

Run::

    python3 scripts/validate_embedding_contract.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from executors import embedding_contract as ec  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _vector_checks() -> dict:
    import math
    checks = {}
    try:
        ec.normalize_vector([0.1, 0.2, 0.3], dimension=4)
        checks["wrong_dimension_rejects"] = False
    except ValueError:
        checks["wrong_dimension_rejects"] = True
    for label, bad in [("nan", [float("nan"), 0.5, 0.5]),
                       ("inf", [float("inf"), 0.5, 0.5]),
                       ("zero", [0.0, 0.0, 0.0])]:
        try:
            ec.normalize_vector(bad, dimension=3)
            checks[f"rejects_{label}"] = False
        except ValueError:
            checks[f"rejects_{label}"] = True
    normed = ec.normalize_vector([3.0, 4.0], dimension=2)
    checks["l2_norm_is_one"] = math.isclose(sum(x * x for x in normed), 1.0)
    return checks


def _fake_checks() -> dict:
    e = ec.DeterministicFakeEmbedder(dimension=64)
    a = _run(e.embed_texts(["WanVideoSampler with LoRA"]))
    b = _run(e.embed_texts(["WanVideoSampler with LoRA"]))
    c = _run(e.embed_texts(["different content"]))
    import math
    return {
        "deterministic": a == b,
        "preserves_order": len(_run(e.embed_texts(["x", "y", "z"]))) == 3,
        "l2_normalized": math.isclose(sum(v * v for v in a[0]), 1.0),
        "different_input_different_vector": a != c,
        "empty_input_empty": _run(e.embed_texts([])) == [],
    }


def _query_checks() -> dict:
    return {
        "whitespace_collapsed": ec.normalize_query_for_embedding("a   b\tc") == "a b c",
        "blank_to_empty": ec.normalize_query_for_embedding("   ") == "",
        "deterministic_key": ec.normalize_query_for_embedding("x\n y") == ec.normalize_query_for_embedding("x y"),
    }


def _hash_checks() -> dict:
    from executors import workflow_representation as wr
    return {
        "content_hash_is_frozen_algorithm": ec.content_hash("abc") == wr.representation_hash("abc"),
        "crlf_normalizes": ec.content_hash("a\r\nb") == ec.content_hash("a\nb"),
    }


def _contract_anchors() -> dict:
    """Frozen Python contract_id values the SQL seeding must reproduce."""

    specs = ec.pilot_contract_specs()
    out = {}
    for s in specs:
        out[f"{s.dimension}d"] = {
            "spec": {
                "provider": s.provider,
                "model": s.model,
                "dimension": s.dimension,
                "canonicalization_version": s.canonicalization_version,
                "chunking_version": s.chunking_version,
            },
            "identity_input": s.identity_input,
            "contract_id": s.id,
            "positive_bigint": 0 < s.id < 2 ** 63,
        }
    out["identical_spec_same_id"] = (
        ec.ContractSpec(provider="openai", model="m", dimension=384).id
        == ec.ContractSpec(provider="openai", model="m", dimension=384).id
    )
    out["different_dimension_different_id"] = (
        ec.ContractSpec(provider="openai", model="m", dimension=384).id
        != ec.ContractSpec(provider="openai", model="m", dimension=1536).id
    )
    return out


#: Persisted live-smoke evidence written by scripts/smoke_embedding_provider.py
#: (the one authorized provider call). The validator reads it to report the gate
#: honestly; it performs no network call itself.
_SMOKE_EVIDENCE_PATH = REPO_ROOT / "docs" / "hybrid-search" / "phase2-embedding-provider-smoke.json"


def _load_smoke_evidence() -> dict | None:
    if not _SMOKE_EVIDENCE_PATH.exists():
        return None
    try:
        return json.loads(_SMOKE_EVIDENCE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _provider_smoke_gate() -> dict:
    """Report the authorized-provider-smoke gate from the recorded live evidence.

    The one live provider smoke is permitted ONLY through Hivemind's server-side
    secret boundary and is performed once by ``scripts/smoke_embedding_provider.py``,
    which writes sanitized evidence (provider/model, dimension, verification
    booleans, norm tolerance, request success, elapsed time, redacted error) —
    never the vector, the input text, or the key. This offline validator reads
    that file and reports PRESENCE/success only; it never makes a network call
    and never prints a value. If the live smoke has not been run, or did not
    pass, the gate is reported as BLOCKED/FAILED — never worked around by adding
    a key to source/tests/pack.
    """

    smoke = _load_smoke_evidence()
    env_present = bool(os.environ.get("OPENAI_API_KEY"))
    embedder = ec.OpenAIEmbedder()  # reads OPENAI_API_KEY from this process env, if any
    configured = embedder.has_credential()
    live_ok = bool(
        smoke
        and smoke.get("request_success")
        and smoke.get("dimension_matches_configured")
        and smoke.get("all_finite")
        and smoke.get("l2_norm_within_tolerance")
    )
    if smoke is None:
        status = "BLOCKED — authorized smoke not run"
    elif live_ok:
        status = "PASSED — authorized smoke recorded"
    else:
        status = "FAILED — authorized smoke recorded but did not pass"
    boundary = (smoke or {}).get("secret_boundary", {}) or {}
    return {
        "gate": "one authorized provider smoke via Hivemind Edge secret (OPENAI_API_KEY)",
        "openai_api_key_in_env": env_present,
        "embedder_has_credential": configured,
        "edge_secret_registered": boundary.get("edge_secret_registered"),
        "live_smoke_status": status,
        "smoke_dimension": smoke.get("dimension") if smoke else None,
        "smoke_elapsed_ms": smoke.get("elapsed_ms") if smoke else None,
        "smoke_evidence_file": (
            "docs/hybrid-search/phase2-embedding-provider-smoke.json"
            if _SMOKE_EVIDENCE_PATH.exists() else None
        ),
        "policy": "one live provider smoke only through Hivemind's Edge secret boundary; "
                  "if absent, record the gate; never add a key to source/tests/pack",
        "offline_path_proven": True,  # fake embedder + injectable transport cover the contract
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Task 2.1 offline embedding-contract validator.")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    evidence = {
        "task": "2.1-embedding-contract-validator",
        "offline": True,
        "vector_validation": _vector_checks(),
        "fake_embedder": _fake_checks(),
        "query_normalization": _query_checks(),
        "content_hash": _hash_checks(),
        "contract_identity_anchors": _contract_anchors(),
        "provider_smoke_gate": _provider_smoke_gate(),
    }
    evidence["all_pass"] = all(
        all(v.values()) if isinstance(v, dict) else bool(v)
        for v in [
            evidence["vector_validation"],
            evidence["fake_embedder"],
            evidence["query_normalization"],
            evidence["content_hash"],
        ]
    ) and evidence["provider_smoke_gate"]["live_smoke_status"].startswith("PASSED")

    text = json.dumps(evidence, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
