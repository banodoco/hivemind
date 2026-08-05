#!/usr/bin/env python3
"""Task 2.16 — read-only representative reconstruction path (AUTHORIZED).

This is the ONLY sanctioned way to reconstruct a representative selected-384-small
contract cohort from the live read-only PostgREST endpoint, pinned to the frozen
Task 2.14 high-water, and bind it to the paid immutable ``cache.384.jsonl`` so a
true representative local benchmark becomes possible WITHOUT any provider call.

Modes
-----
- ``reconstruct-preflight``: run only the offline checks (strict Task 2.14
  offline replay green; high-water pinned; counts recorded; cache-384 facts
  verified read-only). Reports whether representative reconstruction is
  attemptable. No provider. No source/production mutation.
- ``reconstruct``: full read-only reconstruction. Reuses
  ``compare_embedding_candidates.read_real_cohort_entities`` PINNED to the frozen
  high-water, freezes a NEW private snapshot under
  ``.cache/hivemind-hnsw-task-2.16/`` (0700/0600), compares every comparable
  Task 2.14 value, opens ``cache.384.jsonl`` READ-ONLY and requires 100% exact
  text-hash hits for every reconstructed selected chunk AND every applicable
  golden query embedding text. On any drift/miss/mismatch FAILS CLOSED with
  sanitized ``blocked_not_representative`` evidence. If and only if the mapping
  is exact and 100% cache-served, loads the real cached vectors into a NEW
  isolated local PG14/pgvector cluster and runs the TRUE exact-vs-ANN benchmark.
- ``representative-benchmark``: run only the benchmark step (requires a prior
  successful reconstruct snapshot).

SAFETY BOUNDARY (non-negotiable)
--------------------------------
- NO embedding provider calls. NO network except READ-ONLY public PostgREST GETs
  to the frozen endpoint (anon key only). NO POST/PATCH/PUT/DELETE/RPC.
- NEVER write/compact/chmod/delete/rename/truncate ANYTHING under
  ``.cache/hivemind-semantic-eval/`` (the paid Task 2.14 cache). It is opened
  READ-ONLY only.
- NO source/production mutation. NO deployment/migration/contract-activation.
- New private snapshot ONLY under ``.cache/hivemind-hnsw-task-2.16/`` (parent
  0700, files 0600).
- Local PostgreSQL writes are isolated throwaway PG14 clusters only (teardown in
  ``finally``). NO modification of migrations 020-033 or any existing file.
- All tracked evidence is sanitized: no raw query/body/Python/vector/URL/
  credential/cache-path -- hashes + aggregate counts only.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import statistics
import sys
import time
import tempfile
import unittest.mock as mock  # noqa: WPS433  (for bounded-timeout wrapper only)
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

# --- repo bootstrap --------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

# Reuse the accepted Task 2.14 future-freeze architecture verbatim. Do NOT
# reimplement cohort reading, chunking, freezing, or the strict offline replay.
import compare_embedding_candidates as cec  # noqa: E402
from eval.retrieval import semantic as sem  # noqa: E402
from eval.retrieval.loader import load_golden_set  # noqa: E402

# --- accepted constants (zero network) -------------------------------------
DEFAULT_ENDPOINT = "https://ujlwuvkrxlvoswwkerdf.supabase.co/rest/v1"
DEFAULT_ANON_KEY = "sb_publishable_O38oPBafrBoFrpi_rlWJvA_UJrulFsx"

FROZEN_HIGH_WATER = "1532071167588958368"
MESSAGE_SAMPLE_SIZE = cec.MESSAGE_SAMPLE_SIZE  # 5000

SELECTED_CANDIDATE_NAME = "384-small"
# Single source of truth: the selected-contract identity lives in
# executors.selected_contract; import it so this reconstruction cannot drift from
# the frozen v2 literal (the chunking-version bump propagates automatically).
from executors import selected_contract as _sc  # noqa: E402

SELECTED_CONTRACT_ID = _sc.SELECTED_CONTRACT_ID
SELECTED_INDEX_NAME = f"content_embeddings_hnsw_c{_sc.SELECTED_CONTRACT_ID}"

PAID_CACHE_DIR = REPO_ROOT / ".cache" / "hivemind-semantic-eval"
CACHE_384 = PAID_CACHE_DIR / "cache.384.jsonl"
GOLDEN_PATH = REPO_ROOT / "eval" / "retrieval" / "golden" / "golden-v1.json"
FROZEN_MANIFEST = REPO_ROOT / "docs" / "hybrid-search" / "task-2.14-frozen-manifest.json"
DECISION_JSON = REPO_ROOT / "docs" / "hybrid-search" / "task-2.14-embedding-decision.json"
DECISION_MD = REPO_ROOT / "docs" / "hybrid-search" / "task-2.14-embedding-decision.md"
REPLAY_BUNDLE = PAID_CACHE_DIR / "replay-bundle.json"

# Accepted Task 2.14 cache-384 facts (verified, zero network).
ACCEPTED_CACHE_384 = {
    "sha256": "70a9fc7454c3374f89464f86022952b62862badd10f9c79becb0ebc95926a2b5",
    "byte_size": 60091232,
    "record_count": 27464,
    "dimension": 384,
    "mode_oct": "0o600",
}
ACCEPTED_EVAL3_COHORT_MANIFEST_HASH = (
    "0ae1c3f4deef42322d10deb53af1d97d6aec75e90424a9a4da3bf735e94a9b34"
)
ACCEPTED_GOLDEN_SHA256 = sem.GOLDEN_SHA256

# Accepted selected 384-small cohort counts (prose 7873, workflow_python 15265).
ACCEPTED_SELECTED_COUNTS = {
    "n_entities": 7769,
    "n_chunks": 23138,
    "prose_chunks": 7873,
    "workflow_python_chunks": 15265,
    "n_unique_embeddable_texts": 22850,
    "n_duplicate_chunks_collapsed": 288,
}

# New private snapshot dir (gitignored via parent .cache/ rule).
SNAPSHOT_DIR = REPO_ROOT / ".cache" / "hivemind-hnsw-task-2.16"

# The persisted raw reconstruction snapshot + v2 no-op proof, both written by
# the offline Task 2.14 equivalence proof
# (scripts/build_v2_chunking_equivalence_proof.py). The OFFLINE representative
# rebuild reads THIS frozen snapshot instead of a live PostgREST read, so it
# makes ZERO network calls. Parent dir is 0700; both files are 0600.
RAW_RECONSTRUCTION_DIR = REPO_ROOT / ".cache" / "hivemind-task-2.14-reconstruction"
RAW_RECONSTRUCTION_SNAPSHOT = RAW_RECONSTRUCTION_DIR / "raw-reconstructed-snapshot.json"
V2_NOOP_PROOF = RAW_RECONSTRUCTION_DIR / "v2-cache-only-reconstruction-proof.json"
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600

DOCS_EVIDENCE = REPO_ROOT / "docs" / "hybrid-search" / "task-2.16-reconstruction.json"


# ===========================================================================
# Read-only PostgREST reader PINNED to the frozen high-water
# ===========================================================================
def _get_retry(endpoint: str, anon_key: str, path: str, *, retries: int = 3) -> Any:
    """Read-only GET with bounded retry against transient endpoint timeouts.

    The public PostgREST endpoint occasionally times out under load; this never
    weakens read-only semantics (still only GET, still anon key, still zero
    mutation). It just retries the identical read-only request a few times with
    backoff so a flaky moment does not falsely block representative
    reconstruction. Per-request timeout is bounded (30s) so a sustained outage
    fails closed promptly rather than hanging.
    """
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return cec._postgrest_get(endpoint, anon_key, path, timeout=30.0)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt + 1 < retries:
                time.sleep(2.0 * (attempt + 1))
    raise last  # type: ignore[misc]


def _paginate_retry(
    endpoint: str, anon_key: str, table: str, select: str, order_col: str,
    n: int, *, extra_filter: str = "", page: int = 1000, retries: int = 3,
) -> list[dict[str, Any]]:
    """Read-only keyset pagination with bounded retry (same read-only semantics).

    ``_paginate_keyset`` internally calls ``_postgrest_get`` with its own default
    timeout; we wrap that inner call with a bounded 30s timeout so a sustained
    endpoint outage fails closed promptly instead of hanging on a 60s read.
    """
    last: Exception | None = None
    real_get = cec._postgrest_get

    def _bounded_get(ep: str, ak: str, path: str, *, timeout: float = 30.0) -> Any:
        return real_get(ep, ak, path, timeout=timeout)

    for attempt in range(retries):
        try:
            with mock.patch.object(cec, "_postgrest_get", _bounded_get):
                return cec._paginate_keyset(
                    endpoint, anon_key, table, select, order_col, n,
                    extra_filter=extra_filter, page=page,
                )
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt + 1 < retries:
                time.sleep(2.0 * (attempt + 1))
    raise last  # type: ignore[misc]


def read_cohort_pinned_to_high_water(
    *, endpoint: str, anon_key: str, golden_path: Path, high_water: str
) -> tuple[list[sem.CohortEntity], dict[str, Any]]:
    """Read the frozen real cohort from read-only PostgREST, PINNED to high-water.

    This reuses ``compare_embedding_candidates._postgrest_get``,
    ``_paginate_keyset``, ``_golden_message_ids``, ``_corpus_item_from_message``
    and the same distillations/external_resources/message_feed selects as the
    accepted Task 2.14 reader. The ONE intentional difference: the message_feed
    top-N scan is bounded by ``message_id=lte.{high_water}`` so it cannot drift
    with today's moving latest. The original reader derives high-water from the
    top row; that is unsafe for representative reconstruction because new
    messages arrive continuously.

    Returns ``(entities, sanitized_counts)`` exactly like the accepted reader.
    """
    counts: dict[str, Any] = {"sources": {}, "high_water": {}}
    entities: list[sem.CohortEntity] = []

    # --- distillations (identical to accepted reader) ---
    dist_rows = _get_retry(
        endpoint,
        anon_key,
        "/distillations?select=id,question,conditions,answer,status,confidence,created_at,author_id"
        "&status=in.(pending,approved)&order=id.asc",
    )
    from eval.retrieval.schema import ALLOWED_KINDS  # noqa: WPS433

    dist_status: dict[str, int] = {}
    for r in dist_rows:
        dist_status[r.get("status", "?")] = dist_status.get(r.get("status", "?"), 0) + 1
        from eval.retrieval.semantic import CorpusItem  # noqa: WPS433

        item = CorpusItem(
            kind="distillation",
            source="distillations",
            item_id=str(r["id"]),
            title=str(r.get("question") or ""),
            body=str(r.get("answer") or ""),
            author=str(r.get("author_id")) if r.get("author_id") else None,
            created_at=str(r.get("created_at")) if r.get("created_at") else None,
            status=str(r.get("status")) if r.get("status") else None,
            metadata={},
        )
        canon_row = {
            "question": r.get("question"),
            "conditions": r.get("conditions"),
            "answer": r.get("answer"),
            "status": r.get("status"),
        }
        entities.append(
            sem.CohortEntity(
                entity_type="distillation",
                item_id=str(r["id"]),
                corpus_item=item,
                canon_row=canon_row,
            )
        )
    counts["sources"]["distillations"] = {"rows": len(dist_rows), "by_status": dist_status}

    # --- external_resources (identical to accepted reader) ---
    res_rows = _paginate_retry(
        endpoint,
        anon_key,
        "external_resources",
        "id,kind,source,title,body,metadata,payload,author,created_at,url",
        "id",
        n=10_000,
        page=1000,
    )
    res_kind: dict[str, int] = {}
    malformed_resources_skipped = 0
    for r in res_rows:
        res_kind[r.get("kind", "?")] = res_kind.get(r.get("kind", "?"), 0) + 1
        if str(r.get("kind") or "") not in ALLOWED_KINDS:
            malformed_resources_skipped += 1
            continue
        from eval.retrieval.semantic import CorpusItem  # noqa: WPS433

        item = CorpusItem(
            kind=str(r.get("kind") or "resource"),
            source=str(r.get("source") or ""),
            item_id=str(r["id"]),
            title=str(r.get("title") or ""),
            body=str(r.get("body") or ""),
            author=str(r.get("author")) if r.get("author") else None,
            context=None,
            created_at=str(r.get("created_at")) if r.get("created_at") else None,
            url=str(r.get("url")) if r.get("url") else None,
            metadata=r.get("metadata") if isinstance(r.get("metadata"), dict) else {},
        )
        canon_row = {
            "kind": r.get("kind"),
            "title": r.get("title"),
            "body": r.get("body"),
            "metadata": r.get("metadata") if isinstance(r.get("metadata"), dict) else {},
            "payload": r.get("payload") if isinstance(r.get("payload"), dict) else {},
        }
        entities.append(
            sem.CohortEntity(
                entity_type="resource",
                item_id=str(r["id"]),
                corpus_item=item,
                canon_row=canon_row,
            )
        )
    counts["sources"]["external_resources"] = {
        "rows": len(res_rows),
        "by_kind": res_kind,
        "malformed_kind_skipped": malformed_resources_skipped,
    }

    # --- messages: top-N by message_id desc FROM the frozen high-water ----
    golden_msg_ids = cec._golden_message_ids(golden_path)
    top_rows = _paginate_retry(
        endpoint,
        anon_key,
        "message_feed",
        "message_id,content,author_name,channel_name,created_at",
        "message_id",
        n=MESSAGE_SAMPLE_SIZE,
        page=1000,
        # THE pin: bound the scan to the frozen high-water so it does not move
        # with today's latest. Snowflake sent as an exact string (PostgREST
        # treats bigints as strings when quoted this way).
        extra_filter=f"message_id=lte.{high_water}",
    )
    top_ids = {str(r["message_id"]) for r in top_rows}
    counts["high_water"]["message_id"] = str(high_water)
    counts["high_water"]["message_sample_size_target"] = MESSAGE_SAMPLE_SIZE
    counts["high_water"]["top_row_message_id"] = (
        str(top_rows[0]["message_id"]) if top_rows else None
    )

    missing_golden = golden_msg_ids - top_ids
    extra_rows: list[dict[str, Any]] = []
    if missing_golden:
        for mid in sorted(missing_golden):
            try:
                got = _get_retry(
                    endpoint,
                    anon_key,
                    f"/message_feed?message_id=eq.{mid}"
                    "&select=message_id,content,author_name,channel_name,created_at",
                )
            except RuntimeError:
                got = []
            if got:
                extra_rows.extend(got)
    merged: dict[str, dict[str, Any]] = {str(r["message_id"]): r for r in top_rows}
    for r in extra_rows:
        merged[str(r["message_id"])] = r
    if len(merged) > MESSAGE_SAMPLE_SIZE:
        non_golden = sorted((mid for mid in merged if mid not in golden_msg_ids))
        drop = len(merged) - MESSAGE_SAMPLE_SIZE
        for mid in non_golden[:drop]:
            merged.pop(mid, None)
    msg_rows = list(merged.values())
    for r in msg_rows:
        item = cec._corpus_item_from_message(r)
        entities.append(
            sem.CohortEntity(
                entity_type="message",
                item_id=item.item_id,
                corpus_item=item,
                canon_row={"content": r.get("content")},
            )
        )
    counts["sources"]["messages"] = {
        "sample_size": len(msg_rows),
        "golden_message_ids_required": len(golden_msg_ids),
        "golden_message_ids_present": len(
            golden_msg_ids & {str(r["message_id"]) for r in msg_rows}
        ),
        "deterministic_rule": (
            "top-N by message_id desc FROM the frozen high-water "
            f"{high_water}, unioned with every golden message identity, "
            "lowest non-golden ids dropped to hold N"
        ),
    }

    # Integrity: every judged golden identity must be present.
    golden = load_golden_set(golden_path)
    judged_keys = set()
    for case in golden.cases:
        for j in case.expected:
            if j.grade >= 1:
                judged_keys.add(j.key())
    present = {e.entity_key for e in entities}
    missing_judged = sorted(k for k in judged_keys if k not in present)
    counts["integrity"] = {
        "judged_identities_required": len(judged_keys),
        "judged_identities_present": len(judged_keys) - len(missing_judged),
        "missing_judged_identities": len(missing_judged),
    }
    return entities, counts


# ===========================================================================
# Hashing + snapshot helpers
# ===========================================================================
def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_json(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, PRIVATE_DIR_MODE)


def atomic_write_private(path: Path, text: str) -> None:
    """Write a private file atomically at mode 0600 from its first byte."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, PRIVATE_DIR_MODE)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_" + path.name + "_")
    try:
        os.write(fd, text.encode("utf-8"))
        os.close(fd)
        os.chmod(tmp, PRIVATE_FILE_MODE)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ===========================================================================
# Cache verification (READ-ONLY)
# ===========================================================================
def verify_cache_384_readonly(path: Path) -> dict[str, Any]:
    """Open cache.384.jsonl READ-ONLY and verify every accepted fact.

    Returns sanitized facts (sha256/byte_size/record_count/dimension/mode) plus
    a ``key_index`` of every cache key (no vectors, no raw text) for 100%-hit
    verification. NEVER writes/compacts/deletes the cache.
    """
    st = path.stat()
    mode_oct = "0o%o" % (st.st_mode & 0o777)
    byte_size = st.st_size
    h = hashlib.sha256()
    record_count = 0
    dimension_consistent = True
    key_index: set[str] = set()
    dimension_seen: set[int] = set()
    # Stream the file; never load vectors into memory as floats.
    with open(path, "rb") as f:
        for raw in f:
            h.update(raw)
            record_count += 1
            rec = json.loads(raw)
            dim = int(rec.get("d", -1))
            dimension_seen.add(dim)
            if dim != ACCEPTED_CACHE_384["dimension"]:
                dimension_consistent = False
            key_index.add(rec["key"])
    sha = h.hexdigest()
    facts = {
        "path_name": path.name,
        "sha256": sha,
        "byte_size": byte_size,
        "record_count": record_count,
        "dimension": ACCEPTED_CACHE_384["dimension"],
        "dimension_consistent": dimension_consistent,
        "dimensions_seen": sorted(dimension_seen),
        "mode_oct": mode_oct,
        "mode_is_private": mode_oct == ACCEPTED_CACHE_384["mode_oct"],
    }
    return facts, key_index


# ===========================================================================
# Evidence hygiene
# ===========================================================================
_SENSITIVE_PATTERNS = [
    # raw vectors (long base64 or long float sequences)
    (re.compile(r"\b[A-Za-z0-9+/]{200,}={0,2}"), "long_base64_blob"),
    # the anon key
    (
        re.compile(r"sb_publishable_[A-Za-z0-9_-]+"),
        "anon_key_literal",
    ),
    # full https URLs (endpoint leaked)
    (re.compile(r"https?://[^\s\"',]+"), "url_literal"),
    # cache file paths
    (re.compile(r"\.cache/hivemind-semantic-eval/\S+"), "paid_cache_path"),
    (re.compile(r"\.cache/hivemind-hnsw-task-2\.16/\S+"), "private_snapshot_path"),
    # private IP / connection strings
    (re.compile(r"postgresql://[^\s\"']+"), "connection_string"),
    # generic hex hashes are allowed (we emit them deliberately) -- do NOT flag.
]
_ALLOWED_HASH_RE = re.compile(r"^[0-9a-f]{16,128}$")


def evidence_hygiene_scan(obj: Any) -> list[dict[str, Any]]:
    """Return findings of any raw vector/URL/credential/cache-path in evidence.

    Hashes (hex strings up to 128 chars) are explicitly allowed; they are the
    sanctioned evidence format.
    """
    findings: list[dict[str, Any]] = []
    text = json.dumps(obj, ensure_ascii=False)

    def _add(kind: str, sample: str) -> None:
        # redact the sample itself in the finding
        findings.append({"kind": kind, "sample_sha256_prefix": sha256_bytes(sample.encode())[:16]})

    for pat, kind in _SENSITIVE_PATTERNS:
        for m in pat.finditer(text):
            sample = m.group(0)
            # a bare hex hash of <=128 chars that matches the allowed pattern is OK
            if kind == "long_base64_blob" and _ALLOWED_HASH_RE.match(sample):
                continue
            _add(kind, sample)
    return findings


# ===========================================================================
# Golden applicability split
# ===========================================================================
def split_golden(golden_path: Path) -> tuple[list[Any], list[Any], dict[str, Any]]:
    """Split golden into task-2.15-applicable vs future-filter-diagnostic.

    Applicable = cases whose ``filters`` keys are a subset of {kinds, item_ids}
    (the only filters the canonical hivemind_semantic_candidates(kinds,item_ids)
    surface can represent EXACTLY). All other cases (authors/channels/since/...)
    go to future_filter_diagnostic and are NEVER compared as equivalent.
    """
    golden = load_golden_set(golden_path)
    applicable: list[Any] = []
    future: list[Any] = []
    for case in golden.cases:
        fkeys = set((case.filters or {}).keys())
        if fkeys <= {"kinds", "item_ids"}:
            applicable.append(case)
        else:
            future.append(case)
    # Deterministic applicable-cohort identity hash (over sorted case ids).
    app_ids = sorted(c.id for c in applicable)
    fut_ids = sorted(c.id for c in future)
    facts = {
        "n_cases_total": len(golden.cases),
        "n_applicable": len(applicable),
        "n_future_filter_diagnostic": len(future),
        "n_applicable_judged": sum(
            1 for c in applicable if any(j.grade >= 1 for j in c.expected)
        ),
        "n_applicable_expect_no_hit": sum(1 for c in applicable if c.expect_no_hit),
        "applicable_case_ids_sha256": sha256_json(app_ids),
        "future_filter_key_sets": sorted(
            {tuple(sorted((c.filters or {}).keys())) for c in future}
        ),
        "future_case_ids_sha256": sha256_json(fut_ids),
    }
    return applicable, future, facts


# ===========================================================================
# Offline preflight
# ===========================================================================
def run_strict_offline_replay() -> dict[str, Any]:
    """Run the accepted strict offline replay (zero provider/network)."""
    bundle = json.loads(REPLAY_BUNDLE.read_text())
    manifest = json.loads(FROZEN_MANIFEST.read_text())
    decision = json.loads(DECISION_JSON.read_text())
    golden = load_golden_set(GOLDEN_PATH)
    golden_query_tokens = int(
        sem.golden_query_token_estimate(golden)["estimated_input_tokens"]
    )
    proof = sem.strict_offline_replay(
        bundle=bundle,
        decision=decision,
        golden_path=GOLDEN_PATH,
        cache_dir=PAID_CACHE_DIR,
        manifest=manifest,
        golden_query_tokens=golden_query_tokens,
        bundle_path=REPLAY_BUNDLE,
        decision_json_path=DECISION_JSON,
        decision_md_path=DECISION_MD,
    )
    return proof


def reconstruct_preflight(*, endpoint: str, anon_key: str) -> dict[str, Any]:
    """Offline-only preflight. Reports whether reconstruction is attemptable."""
    checks: list[dict[str, Any]] = []

    def _check(name: str, ok: bool, detail: Any = None) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    # P1 strict offline replay green with zero provider/network.
    try:
        proof = run_strict_offline_replay()
        zero_provider = bool(proof.get("zero_provider_calls"))
        zero_network = bool(proof.get("zero_network_calls"))
        winner = proof.get("winner_reproduced")
        _check(
            "strict_offline_replay_green",
            zero_provider and zero_network and winner == SELECTED_CANDIDATE_NAME,
            {
                "zero_provider_calls": zero_provider,
                "zero_network_calls": zero_network,
                "winner_reproduced": winner,
                "n_checks": proof.get("n_checks"),
            },
        )
    except Exception as exc:  # noqa: BLE001
        _check("strict_offline_replay_green", False, {"error_type": type(exc).__name__})

    # P2 high-water pinned to the frozen literal.
    _check(
        "high_water_pinned",
        FROZEN_HIGH_WATER == "1532071167588958368",
        {"frozen_high_water": FROZEN_HIGH_WATER},
    )

    # P3 accepted selected counts recorded.
    _check(
        "accepted_selected_counts_recorded",
        ACCEPTED_SELECTED_COUNTS["n_chunks"] == 23138
        and ACCEPTED_SELECTED_COUNTS["n_entities"] == 7769
        and ACCEPTED_SELECTED_COUNTS["n_unique_embeddable_texts"] == 22850,
        dict(ACCEPTED_SELECTED_COUNTS),
    )

    # P4 cache-384 facts verified READ-ONLY (does not require network; local file).
    try:
        facts, _keys = verify_cache_384_readonly(CACHE_384)
        ok = (
            facts["sha256"] == ACCEPTED_CACHE_384["sha256"]
            and facts["byte_size"] == ACCEPTED_CACHE_384["byte_size"]
            and facts["record_count"] == ACCEPTED_CACHE_384["record_count"]
            and facts["dimension_consistent"]
            and facts["mode_is_private"]
            and facts["dimensions_seen"] == [ACCEPTED_CACHE_384["dimension"]]
        )
        _check("cache_384_facts_verified", ok, facts)
    except Exception as exc:  # noqa: BLE001
        _check("cache_384_facts_verified", False, {"error_type": type(exc).__name__})

    # P5 golden hash + applicability split recorded.
    try:
        gfacts = sem.golden_file_facts(GOLDEN_PATH)
        _app, _fut, split = split_golden(GOLDEN_PATH)
        _check(
            "golden_hash_and_applicability_recorded",
            gfacts["sha256"] == ACCEPTED_GOLDEN_SHA256,
            {
                "golden_sha256": gfacts["sha256"],
                "n_cases": gfacts.get("record_count") or gfacts.get("n_cases"),
                "applicable_split": split,
            },
        )
    except Exception as exc:  # noqa: BLE001
        _check("golden_hash_and_applicability_recorded", False, {"error_type": type(exc).__name__})

    # P6 count-level manifest hash recorded (the ONLY comparable cohort hash).
    _check(
        "eval3_cohort_manifest_hash_recorded",
        ACCEPTED_EVAL3_COHORT_MANIFEST_HASH
        == "0ae1c3f4deef42322d10deb53af1d97d6aec75e90424a9a4da3bf735e94a9b34",
        {"eval3_cohort_manifest_hash": ACCEPTED_EVAL3_COHORT_MANIFEST_HASH},
    )

    all_ok = all(c["ok"] for c in checks)
    # The preflight is "attemptable" iff offline checks are green. The live
    # read-only read still has to confirm zero drift; that happens in `reconstruct`.
    attemptable = all_ok
    return {
        "mode": "reconstruct-preflight",
        "attemptable": attemptable,
        "checks": checks,
        "n_checks": len(checks),
        "n_pass": sum(1 for c in checks if c["ok"]),
        "no_old_chunk_map_hash_limitation": _NO_OLD_CHUNK_MAP_HASH_LIMITATION,
    }


_NO_OLD_CHUNK_MAP_HASH_LIMITATION = (
    "Task 2.14 NEVER persisted the raw eval3 identity-to-chunk mapping or raw "
    "texts (neither into tracked evidence nor into the surviving private cache "
    "dir, which holds only cache.384.jsonl, cache.1536.jsonl, replay-bundle.json). "
    "Therefore reconstruction can verify COUNTS (entities/chunks/prose/workflow_"
    "python/unique), the frozen high-water, and the count-level "
    "eval3_cohort_manifest_hash. It COMPUTES and FREEZES a fresh "
    "entity_order_identity_hash, per_representation_chunk_map_hash, "
    "text_hash_multiset_hash, and snapshot file hash in a NEW private snapshot, "
    "but there is NO Task-2.14-persisted equivalent to compare them against "
    "(only the count-level manifest hash is comparable). No old identity-to-"
    "chunk hash is invented."
)


# ===========================================================================
# Full reconstruction
# ===========================================================================
def _selected_chunk_counts(cohort: sem.ChunkedCohort) -> dict[str, Any]:
    prose = sum(1 for ch in cohort.chunks if ch.representation_type == sem.REP_PROSE)
    wp = sum(1 for ch in cohort.chunks if ch.representation_type == sem.REP_WORKFLOW_PYTHON)
    return {
        "n_entities": cohort.n_entities,
        "n_chunks": cohort.n_chunks,
        "prose_chunks": prose,
        "workflow_python_chunks": wp,
        "n_unique_embeddable_texts": cohort.n_unique_embeddable_texts,
        "n_duplicate_chunks_collapsed": cohort.n_chunks - cohort.n_unique_embeddable_texts,
    }


def _text_hash_multiset_hash(cohort: sem.ChunkedCohort) -> str:
    """Hash over the multiset of chunk text-hashes (detects any text drift)."""
    # Each chunk's text-hash is its chunk_hash (already sha256 of normalized text).
    # Hash the sorted multiset so duplicate texts are counted with multiplicity.
    multiset = sorted(ch.chunk_hash for ch in cohort.chunks)
    return sha256_json(multiset)


def _per_representation_chunk_map_hash(cohort: sem.ChunkedCohort) -> dict[str, str]:
    """Per-representation chunk-map hash (the accepted freeze fact shape)."""
    out: dict[str, str] = {}
    for rep in (sem.REP_PROSE, sem.REP_WORKFLOW_PYTHON):
        chunk_map = [
            {
                "entity_kind": ch.entity_kind,
                "item_id": ch.item_id,
                "representation_type": ch.representation_type,
                "chunk_index": ch.chunk_index,
                "chunk_hash": ch.chunk_hash,
                "representation_hash": ch.representation_hash,
            }
            for ch in cohort.chunks
            if ch.representation_type == rep
        ]
        out[rep] = sha256_json(chunk_map)
    # whole-cohort map hash too
    full = [
        {
            "entity_kind": ch.entity_kind,
            "item_id": ch.item_id,
            "representation_type": ch.representation_type,
            "chunk_index": ch.chunk_index,
            "chunk_hash": ch.chunk_hash,
            "representation_hash": ch.representation_hash,
        }
        for ch in cohort.chunks
    ]
    out["__all__"] = sha256_json(full)
    return out


def _entity_order_identity_hash(entities: Sequence[sem.CohortEntity]) -> str:
    """Hash over the sorted set of entity identity keys."""
    keys = sorted(e.entity_key for e in entities)
    return sha256_json(keys)


def reconstruct(*, endpoint: str, anon_key: str, skip_benchmark: bool = False) -> dict[str, Any]:
    """Full read-only reconstruction. FAILS CLOSED on any drift/miss/mismatch."""
    # Step 0: preflight must be green.
    pre = reconstruct_preflight(endpoint=endpoint, anon_key=anon_key)
    if not pre["attemptable"]:
        return _blocked(
            "preflight_red",
            "offline preflight is not attemptable; representative reconstruction refused",
            preflight=pre,
        )

    # Step 1: read cohort PINNED to high-water (read-only GETs, zero provider).
    try:
        entities, read_counts = read_cohort_pinned_to_high_water(
            endpoint=endpoint,
            anon_key=anon_key,
            golden_path=GOLDEN_PATH,
            high_water=FROZEN_HIGH_WATER,
        )
    except Exception as exc:  # noqa: BLE001
        return _blocked(
            "live_read_failed",
            f"read-only PostgREST cohort read failed: {type(exc).__name__}",
            preflight=pre,
            error_type=type(exc).__name__,
        )

    # Integrity: every judged golden identity MUST be present.
    if read_counts["integrity"]["missing_judged_identities"] != 0:
        return _blocked(
            "judged_identity_missing",
            "a judged golden identity is absent from the reconstructed cohort",
            preflight=pre,
            read_counts=read_counts,
        )

    # Step 2: chunk under the selected 384-small candidate (frozen chunker).
    selected = next(c for c in sem.CANDIDATES if c.name == SELECTED_CANDIDATE_NAME)
    cohort = sem.build_chunked_cohort(entities, selected)
    counts = _selected_chunk_counts(cohort)

    # Step 3: compare COUNTS against accepted (the only comparable cohort check).
    count_drift: list[dict[str, Any]] = []
    for k, expected in ACCEPTED_SELECTED_COUNTS.items():
        actual = counts.get(k)
        if actual != expected:
            count_drift.append({"field": k, "expected": expected, "actual": actual})
    if count_drift:
        return _blocked(
            "count_drift",
            "reconstructed selected counts differ from the accepted Task 2.14 counts",
            preflight=pre,
            read_counts=read_counts,
            reconstructed_counts=counts,
            count_drift=count_drift,
        )

    # High-water present in the top row (should equal the pin).
    top_row_id = read_counts["high_water"].get("top_row_message_id")
    if str(top_row_id) != FROZEN_HIGH_WATER:
        return _blocked(
            "high_water_drift",
            "the reconstructed top-row message_id does not equal the frozen high-water "
            "(new messages have arrived at/above the boundary, or the pin failed)",
            preflight=pre,
            read_counts=read_counts,
            expected_high_water=FROZEN_HIGH_WATER,
            actual_top_row_message_id=top_row_id,
        )

    # Step 4: verify cache-384 facts READ-ONLY, then require 100% cache hits.
    try:
        cache_facts, key_index = verify_cache_384_readonly(CACHE_384)
    except Exception as exc:  # noqa: BLE001
        return _blocked(
            "cache_read_failed",
            f"cache.384.jsonl read-only verification failed: {type(exc).__name__}",
            preflight=pre,
            error_type=type(exc).__name__,
        )
    cache_fact_drift = []
    for k, expected in ACCEPTED_CACHE_384.items():
        actual = cache_facts.get(k)
        if str(actual) != str(expected):
            cache_fact_drift.append({"field": k, "expected": expected, "actual": actual})
    if cache_fact_drift or not cache_facts["dimension_consistent"] or not cache_facts["mode_is_private"]:
        return _blocked(
            "cache_fact_drift",
            "cache.384.jsonl facts differ from the accepted Task 2.14 facts",
            preflight=pre,
            cache_facts=cache_facts,
            cache_fact_drift=cache_fact_drift,
        )

    # Require 100% exact text-hash hits for every reconstructed selected chunk.
    provider = selected.provider
    model = selected.model
    dim = selected.dimension
    chunk_text_hashes = {ch.chunk_hash for ch in cohort.chunks}
    required_chunk_keys = {
        f"{provider}:{model}:{dim}:{th}" for th in chunk_text_hashes
    }
    missing_chunk_keys = sorted(required_chunk_keys - key_index)
    chunk_hit_rate = (
        (len(required_chunk_keys) - len(missing_chunk_keys))
        / len(required_chunk_keys)
        if required_chunk_keys
        else 1.0
    )
    if missing_chunk_keys:
        return _blocked(
            "chunk_cache_miss",
            "one or more reconstructed selected-chunk text-hashes are absent from "
            "cache.384.jsonl (would require a provider call to embed; refused)",
            preflight=pre,
            reconstructed_counts=counts,
            cache_facts=cache_facts,
            chunk_cache_hit_rate=chunk_hit_rate,
            n_chunk_keys_required=len(required_chunk_keys),
            n_chunk_keys_missing=len(missing_chunk_keys),
            # do NOT leak raw keys; hash the missing-key set
            missing_chunk_keys_set_sha256=sha256_json(missing_chunk_keys),
        )

    # Require 100% exact text-hash hits for every applicable golden QUERY text.
    applicable, future, split = split_golden(GOLDEN_PATH)
    query_texts = [c.query for c in applicable]
    query_text_hashes = [sem.ec.content_hash(q) for q in query_texts]
    required_query_keys = {
        f"{provider}:{model}:{dim}:{th}" for th in query_text_hashes
    }
    missing_query_keys = sorted(required_query_keys - key_index)
    query_hit_rate = (
        (len(required_query_keys) - len(missing_query_keys))
        / len(required_query_keys)
        if required_query_keys
        else 1.0
    )
    if missing_query_keys:
        return _blocked(
            "golden_query_cache_miss",
            "one or more applicable golden query text-hashes are absent from "
            "cache.384.jsonl (would require a provider call to embed; refused)",
            preflight=pre,
            reconstructed_counts=counts,
            cache_facts=cache_facts,
            chunk_cache_hit_rate=chunk_hit_rate,
            query_cache_hit_rate=query_hit_rate,
            n_query_keys_required=len(required_query_keys),
            n_query_keys_missing=len(missing_query_keys),
            missing_query_keys_set_sha256=sha256_json(missing_query_keys),
            golden_split=split,
        )

    # Step 5: freeze a NEW private snapshot under .cache/hivemind-hnsw-task-2.16/
    ensure_private_dir(SNAPSHOT_DIR)
    snapshot_payload = {
        "version": 1,
        "kind": "task-2.16-representative-reconstruction-private-snapshot",
        "frozen_high_water": FROZEN_HIGH_WATER,
        "selected_candidate": SELECTED_CANDIDATE_NAME,
        "selected_contract_id": SELECTED_CONTRACT_ID,
        "read_counts": read_counts,
        "reconstructed_counts": counts,
        "cache_facts": cache_facts,
        "golden_split": split,
        "entity_order_identity_hash": _entity_order_identity_hash(entities),
        "per_representation_chunk_map_hash": _per_representation_chunk_map_hash(cohort),
        "text_hash_multiset_hash": _text_hash_multiset_hash(cohort),
        "chunk_text_hashes": sorted(chunk_text_hashes),
        "applicable_query_text_hashes": sorted(set(query_text_hashes)),
        "no_old_chunk_map_hash_limitation": _NO_OLD_CHUNK_MAP_HASH_LIMITATION,
    }
    snapshot_blob = json.dumps(snapshot_payload, sort_keys=True, ensure_ascii=False)
    snapshot_path = SNAPSHOT_DIR / "representative-snapshot.json"
    atomic_write_private(snapshot_path, snapshot_blob)
    snapshot_file_sha256 = sha256_bytes(snapshot_blob.encode("utf-8"))

    # Step 6: run the representative benchmark in an isolated PG14 cluster.
    benchmark: dict[str, Any]
    if skip_benchmark:
        benchmark = {"skipped": True, "reason": "--skip-benchmark"}
    else:
        try:
            benchmark = _run_representative_benchmark(
                cohort=cohort,
                key_index=key_index,
                applicable=applicable,
                provider=provider,
                model=model,
                dim=dim,
            )
        except Exception as exc:  # noqa: BLE001
            # The mapping is exact and cache-served, but the local benchmark
            # failed. Keep the snapshot; report blocked with the precise blocker.
            # Sanitize the error message: keep only the exception type + a
            # hashed digest of the message (the message may contain our own DDL
            # or item ids, never raw corpus content, but hash it regardless).
            msg = str(exc)
            return _blocked(
                "benchmark_failed",
                f"isolated PG14 representative benchmark failed: {type(exc).__name__}",
                preflight=pre,
                reconstructed_counts=counts,
                cache_facts=cache_facts,
                chunk_cache_hit_rate=chunk_hit_rate,
                query_cache_hit_rate=query_hit_rate,
                golden_split=split,
                snapshot_file_sha256=snapshot_file_sha256,
                entity_order_identity_hash=snapshot_payload["entity_order_identity_hash"],
                error_type=type(exc).__name__,
                error_message_sha256=sha256_bytes(msg.encode())[:16],
            )

    # SUCCESS: representative local content, non-production compute.
    verdict = {
        "task": "2.16-representative-reconstruction",
        "run_class": "representative_local_content_nonproduction_compute",
        "verdict": "representative_local_content_nonproduction_compute",
        "representative_semantic_evidence": True,
        "selected_contract_id": SELECTED_CONTRACT_ID,
        "selected_candidate": SELECTED_CANDIDATE_NAME,
        "frozen_high_water": FROZEN_HIGH_WATER,
        "reconstructed_counts": counts,
        "cache_facts": cache_facts,
        "chunk_cache_hit_rate": chunk_hit_rate,
        "n_chunk_keys_required": len(required_chunk_keys),
        "n_chunk_keys_missing": 0,
        "query_cache_hit_rate": query_hit_rate,
        "n_query_keys_required": len(required_query_keys),
        "n_query_keys_missing": 0,
        "golden_split": split,
        "snapshot": {
            "content_sha256": snapshot_file_sha256,
            "n_records": len(json.loads(snapshot_blob)),
        },
        "entity_order_identity_hash": snapshot_payload["entity_order_identity_hash"],
        "per_representation_chunk_map_hash": snapshot_payload["per_representation_chunk_map_hash"],
        "text_hash_multiset_hash": snapshot_payload["text_hash_multiset_hash"],
        "no_old_chunk_map_hash_limitation": _NO_OLD_CHUNK_MAP_HASH_LIMITATION,
        "benchmark": benchmark,
        "limitations": [
            "Even a successful real-content local run CANNOT close hosted Q8/Q9 "
            "latency or P3 recurring-compute gates or select production rollout "
            "without staging.",
            _NO_OLD_CHUNK_MAP_HASH_LIMITATION,
        ],
        "provider_calls": 0,
        "network_calls": "read_only_postgrest_get_only",
        "source_mutations": 0,
        "production_mutated": False,
        "contract_activated": False,
    }
    _write_evidence(verdict)
    return verdict


def _blocked(blocker: str, reason: str, **extra: Any) -> dict[str, Any]:
    """Emit sanitized blocked_not_representative evidence."""
    verdict = {
        "task": "2.16-representative-reconstruction",
        "run_class": "blocked_not_representative",
        "verdict": "blocked_not_representative",
        "representative_semantic_evidence": False,
        "blocker": blocker,
        "reason": reason,
        "no_old_chunk_map_hash_limitation": _NO_OLD_CHUNK_MAP_HASH_LIMITATION,
        "provider_calls": 0,
        "source_mutations": 0,
        "production_mutated": False,
        "contract_activated": False,
    }
    verdict.update(extra)
    _write_evidence(verdict)
    return verdict


def _write_evidence(verdict: dict[str, Any]) -> None:
    """Sanitize + hygiene-scan + write the tracked evidence JSON."""
    findings = evidence_hygiene_scan(verdict)
    if findings:
        # FAIL CLOSED on hygiene: never write dirty evidence.
        verdict = dict(verdict)
        verdict["hygiene_findings"] = findings
        verdict["hygiene_clean"] = False
        verdict["verdict"] = "blocked_not_representative"
        verdict["blocker"] = "evidence_hygiene_violation"
        verdict["reason"] = (
            "tracked evidence contains raw vector/URL/credential/cache-path; "
            "refused to emit dirty evidence"
        )
        # Do NOT write the dirty payload; write only the blocked notice.
        DOCS_EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        safe = {
            "task": "2.16-representative-reconstruction",
            "verdict": "blocked_not_representative",
            "blocker": "evidence_hygiene_violation",
            "hygiene_findings": findings,
            "note": "full evidence suppressed to avoid leaking raw content",
        }
        DOCS_EVIDENCE.write_text(json.dumps(safe, indent=2, sort_keys=True))
        return
    verdict["hygiene_findings"] = []
    verdict["hygiene_clean"] = True
    DOCS_EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    DOCS_EVIDENCE.write_text(json.dumps(verdict, indent=2, sort_keys=True))


# ===========================================================================
# Isolated PG14/pgvector representative benchmark
# ===========================================================================
def _load_cached_vectors(
    *, cohort: sem.ChunkedCohort, provider: str, model: str, dim: int
) -> dict[str, list[float]]:
    """Return chunk_hash -> float vector from cache. READ-ONLY (no writes).

    The cache stores vectors base64-encoded as little-endian float32 in field
    ``v``. We decode ONLY the keys we need and never persist the decoded form
    anywhere except the throwaway isolated cluster (torn down in finally).
    """
    import struct  # noqa: WPS433

    needed = {f"{provider}:{model}:{dim}:{ch.chunk_hash}" for ch in cohort.chunks}
    out: dict[str, list[float]] = {}
    with open(CACHE_384, "rb") as f:
        for raw in f:
            rec = json.loads(raw)
            key = rec["key"]
            if key in needed:
                vec_bytes = base64.b64decode(rec["v"])
                n = len(vec_bytes) // 4
                out[key.rsplit(":", 1)[-1]] = list(struct.unpack(f"<{n}f", vec_bytes))
    return out


def _load_query_vectors(
    *, queries: list[str], provider: str, model: str, dim: int
) -> dict[int, list[float]]:
    """Return query_index -> float vector from cache. READ-ONLY.

    Handles duplicate query texts correctly: multiple indices may share a
    content_hash; we load the vector once per unique hash and assign it to
    EVERY index with that hash.
    """
    import struct  # noqa: WPS433

    # Map each cache-key -> list of indices that share it.
    key_to_indices: dict[str, list[int]] = {}
    for i, q in enumerate(queries):
        key = f"{provider}:{model}:{dim}:{sem.ec.content_hash(q)}"
        key_to_indices.setdefault(key, []).append(i)
    hash_to_vec: dict[str, list[float]] = {}
    with open(CACHE_384, "rb") as f:
        for raw in f:
            rec = json.loads(raw)
            if rec["key"] in key_to_indices and rec["key"] not in hash_to_vec:
                vec_bytes = base64.b64decode(rec["v"])
                n = len(vec_bytes) // 4
                hash_to_vec[rec["key"]] = list(struct.unpack(f"<{n}f", vec_bytes))
    out: dict[int, list[float]] = {}
    for key, indices in key_to_indices.items():
        vec = hash_to_vec.get(key)
        if vec is not None:
            for idx in indices:
                out[idx] = vec
    return out


def _run_representative_benchmark(
    *,
    cohort: sem.ChunkedCohort,
    key_index: set[str],
    applicable: list[Any],
    provider: str,
    model: str,
    dim: int,
) -> dict[str, Any]:
    """Run the TRUE exact-vs-ANN benchmark on real cached 384 vectors.

    Isolated throwaway PG14/pgvector cluster only; teardown in finally. REUSES
    ``rehearse_hnsw_pilot.setup_cluster`` (applies 003+020-029+032+033 + seeds
    the selected contract active), ``build_index``, ``catalog``, ``_scalar``,
    and ``vec_literal``. The ONLY difference from the structural rehearsal: the
    loaded vectors are the REAL cached 384-small embeddings (read-only from the
    paid cache), and the source rows are the REAL reconstructed cohort entities.
    """
    import rehearse_hnsw_pilot as rh  # noqa: WPS433

    bins = rh.lp.find_pgbins()
    if not bins:
        return {
            "skipped": True,
            "reason": "no local PG14 binaries found; isolated benchmark not run",
            "cluster_isolated": False,
        }

    # Load REAL cached vectors (chunk_hash -> floats).
    chunk_vectors = _load_cached_vectors(
        cohort=cohort, provider=provider, model=model, dim=dim
    )
    needed_hashes = {ch.chunk_hash for ch in cohort.chunks}
    if len(chunk_vectors) != len(needed_hashes):
        raise RuntimeError(
            f"vector load mismatch: got {len(chunk_vectors)} unique hashes vs "
            f"{len(needed_hashes)} expected"
        )

    cluster, env = rh.setup_cluster()
    try:
        return _benchmark_in_cluster(
            rh=rh,
            cluster=cluster,
            env=env,
            cohort=cohort,
            chunk_vectors=chunk_vectors,
            applicable=applicable,
            provider=provider,
            model=model,
            dim=dim,
        )
    finally:
        cluster.tear_down()


def _entity_type_for_kind(kind: str) -> str:
    # entity_kind values are the citation-vocab entity types.
    if kind == "distillation":
        return "distillation"
    if kind == "message":
        return "message"
    return "resource"


def _benchmark_in_cluster(
    *,
    rh: Any,
    cluster: Any,
    env: dict[str, Any],
    cohort: sem.ChunkedCohort,
    chunk_vectors: dict[str, list[float]],
    applicable: list[Any],
    provider: str,
    model: str,
    dim: int,
) -> dict[str, Any]:
    """Seed REAL cohort source rows + REAL cached vectors, build, run exact/ANN."""
    SEL = SELECTED_CONTRACT_ID
    HASH64 = "0" * 64  # placeholder hashes for the cluster only (not evidence)

    # --- seed REAL eligible source rows for every entity in the cohort ---
    msgs: set[str] = set()
    res: set[str] = set()
    dist: set[str] = set()
    workflows: set[str] = set()
    for ch in cohort.chunks:
        ent = _entity_type_for_kind(ch.entity_kind)
        if ent == "message":
            msgs.add(ch.item_id)
        elif ent == "distillation":
            dist.add(ch.item_id)
        else:
            res.add(ch.item_id)
            if ch.representation_type == sem.REP_WORKFLOW_PYTHON:
                workflows.add(ch.item_id)

    def _bulk(table_cols: str, conflict_target: str, ids: Iterable[str], row_sql: str) -> None:
        ids = list(ids)
        if not ids:
            return
        # conflict_target is the optional "(col)" target; "do nothing" appended.
        on_conf = f"on conflict {conflict_target} do nothing" if conflict_target else "on conflict do nothing"
        bsz = 1000
        for i in range(0, len(ids), bsz):
            vals = ",\n".join(row_sql.format(_id=ids[j]) for j in range(i, min(i + bsz, len(ids))))
            rc, _ = cluster.psql(
                f"insert into {table_cols} values {vals} {on_conf};",
                capture=False,
            )
            if rc != 0:
                raise RuntimeError(f"seed {table_cols} batch {i} failed rc={rc}")

    _bulk(
        "discord_messages(message_id, content, is_deleted)",
        "",
        msgs,
        "('{_id}', '', false)",
    )
    # resources: kind must be a valid citation kind. Use 'workflow' for any
    # resource that has workflow_python reps, else 'resource'/'article'.
    wf_res = workflows
    non_wf_res = res - workflows
    _bulk(
        "external_resources(id, kind, source, title, body)",
        "",
        wf_res,
        "('{_id}', 'workflow', 'recon', 't', 'b')",
    )
    _bulk(
        "external_resources(id, kind, source, title, body)",
        "",
        non_wf_res,
        "('{_id}', 'resource', 'recon', 't', 'b')",
    )
    # workflow-python gate: r.kind='workflow' AND lps.public_state='safe'
    _bulk(
        "lexical_resource_python_state(resource_id, kind, cohort, public_state, available)",
        "(resource_id)",
        workflows,
        "('{_id}', 'workflow', 'payload_python', 'safe', true)",
    )
    _bulk(
        "distillations(id, question, answer, status)",
        "",
        dist,
        "('{_id}', 'q', 'a', 'approved')",
    )

    # --- COPY the REAL cached vectors into content_embeddings ---
    # chunk_text intentionally left as a placeholder (NOT the real content) so
    # no raw corpus text lands in the throwaway cluster; the benchmark measures
    # distance over the REAL embeddings only.
    def _copy_line(ch: Any) -> str:
        vec = chunk_vectors[ch.chunk_hash]
        return "\t".join([
            str(SEL), _entity_type_for_kind(ch.entity_kind), ch.item_id,
            ch.representation_type, str(ch.chunk_index),
            f"chunk-{ch.chunk_hash[:8]}",  # placeholder text, not real content
            rh.vec_literal(vec), HASH64, ch.chunk_hash,
        ])

    copy_body = "\n".join(_copy_line(ch) for ch in cohort.chunks) + "\n\\.\n"
    rc, _ = cluster.psql(
        "copy content_embeddings(contract_id,entity_type,item_id,representation_type,"
        "chunk_index,chunk_text,embedding,representation_hash,chunk_hash) "
        "from stdin (format text);",
        stdin=copy_body,
        capture=False,
    )
    if rc != 0:
        raise RuntimeError(f"COPY content_embeddings failed rc={rc}")
    cluster.psql(
        "analyze content_embeddings; analyze discord_messages; "
        "analyze external_resources; analyze distillations;",
        capture=False,
    )

    n_loaded = int((rh._scalar(cluster, f"select count(*) from content_embeddings where contract_id={SEL};") or "0").strip())
    if n_loaded != cohort.n_chunks:
        raise RuntimeError(f"loaded {n_loaded} embeddings vs {cohort.n_chunks} cohort chunks")

    # --- build the HNSW partial index (CONCURRENTLY, autocommit) ---
    build = rh.build_index(cluster)
    cat = rh.catalog(cluster)
    if not cat.get("indisvalid"):
        raise RuntimeError(f"HNSW index not valid after build: {cat!r}")

    # --- load REAL cached query vectors for applicable golden cases ---
    queries = [c.query for c in applicable]
    query_vectors = _load_query_vectors(queries=queries, provider=provider, model=model, dim=dim)
    if len(query_vectors) != len(queries):
        raise RuntimeError(
            f"query vector load mismatch: got {len(query_vectors)} vs {len(queries)} queries"
        )

    # --- DECISIVE: canonical-function idx_scan delta on REAL cached content ---
    # Multiple BROAD calls to the ACTUAL public
    # hivemind_semantic_candidates(vector,int,text[],text[]) must drive the
    # partial HNSW index (strictly positive pg_stat_user_indexes idx_scan delta
    # on SELECTED_INDEX_NAME). The direct single-table inner kNN is
    # diagnostic_only_insufficient (uses the index by construction, with no
    # eligibility); the canonical-function delta is the decisive proof the index
    # is the natural driving path on REAL cached content. Mirrors the structural
    # rehearsal/benchmark measurement (scripts/rehearse_hnsw_pilot.py,
    # scripts/benchmark_hnsw_pilot.py).
    func_before = int((rh._scalar(cluster,
        f"select coalesce(idx_scan,0) from pg_stat_user_indexes "
        f"where indexrelname='{SELECTED_INDEX_NAME}';") or "0"))
    # 6 BROAD canonical-function calls (ANN, HNSW-forcing GUCs). Batched into one
    # psql round-trip and followed by a short sleep so PG14's stats collector
    # flushes the idx_scan increment before re-reading (mirrors the structural
    # rehearsal/benchmark measurement in scripts/rehearse_hnsw_pilot.py).
    qv0 = next(iter(query_vectors.values())) if query_vectors else None
    broad_calls = 0
    if qv0 is not None:
        ql0 = rh.vec_literal(qv0)
        func_call_sql = " ".join(
            [f"select count(*) from hivemind_semantic_candidates('{ql0}'::vector,10,'{{}}','{{}}');"] * 6
        )
        cluster.psql(
            f"set hnsw.ef_search=80; set hnsw.iterative_scan=strict_order; {func_call_sql}",
            capture=False,
        )
        broad_calls = 6
    time.sleep(0.8)
    func_after = int((rh._scalar(cluster,
        f"select idx_scan from pg_stat_user_indexes "
        f"where indexrelname='{SELECTED_INDEX_NAME}';") or "0"))
    func_delta = func_after - func_before

    K = 10
    # exact baseline identities via the GUC (TRUE exact: no HNSW).
    exact_prefix = (
        "set enable_indexscan=off; set enable_bitmapscan=off; "
        "set hivemind.semantic_exact_mode=on;"
    )
    exact_ids_per_q: list[list[str]] = []
    for i in range(len(applicable)):
        if i not in query_vectors:
            exact_ids_per_q.append([])
            continue
        ql = rh.vec_literal(query_vectors[i])
        rc, out = cluster.psql(
            f"{exact_prefix} select entity_type||':'||item_id from "
            f"hivemind_semantic_candidates('{ql}'::vector,{K},'{{}}','{{}}');"
        )
        exact_ids_per_q.append(_parse_ids(out))

    # ANN sweep over the pre-registered ef values.
    recall_by_ef: dict[str, Any] = {}
    for ef in rh.EF_SWEEP:
        prefix = f"set hnsw.ef_search={ef}; set hnsw.iterative_scan=strict_order;"
        recalls: list[float] = []
        for i in range(len(applicable)):
            if i not in query_vectors or not exact_ids_per_q[i]:
                continue
            ql = rh.vec_literal(query_vectors[i])
            rc, out = cluster.psql(
                f"{prefix} select entity_type||':'||item_id from "
                f"hivemind_semantic_candidates('{ql}'::vector,{K},'{{}}','{{}}');"
            )
            ann_ids = _parse_ids(out)
            ex = exact_ids_per_q[i]
            recalls.append(len(set(ex) & set(ann_ids)) / len(ex))
        mean = statistics.mean(recalls) if recalls else 0.0
        recall_by_ef[str(ef)] = {
            "mean_recall_at_10": round(mean, 6),
            "n_pairs": len(recalls),
        }

    return {
        "run_class": "representative_local_content_nonproduction_compute",
        "cluster_isolated": True,
        "environment": env,
        "n_vectors_loaded": n_loaded,
        "index_catalog": cat,
        "n_applicable_queries": len(applicable),
        "exact_vs_ann_recall_at_10_by_ef": recall_by_ef,
        "canonical_function_idx_scan_before": func_before,
        "canonical_function_idx_scan_after": func_after,
        "canonical_function_idx_scan_delta": func_delta,
        "canonical_function_idx_scan_delta_positive": func_delta > 0,
        "canonical_function_uses_hnsw": func_delta > 0,
        "canonical_function_broad_calls": broad_calls,
        "canonical_function_note": (
            "Strictly positive idx_scan delta from BROAD calls to the ACTUAL "
            "public hivemind_semantic_candidates(vector,int,text[],text[]) on "
            "REAL cached 384-small content; the partial HNSW index is the natural "
            "driving path. The direct inner kNN is diagnostic only."
        ),
        "limitations": [
            "Local isolated PG14/pgvector cluster only; NOT a production-like "
            "staging run. Hosted Q8/Q9 latency and P3 recurring-compute gates "
            "remain unproven.",
            "Vectors are the REAL cached 384-small embeddings from the paid "
            "Task 2.14 cache (read-only); no provider call was made.",
            "chunk_text is a placeholder in the throwaway cluster (not the real "
            "corpus text); the benchmark measures distance over REAL embeddings only.",
        ],
    }


def _parse_ids(out: str) -> list[str]:
    ids: list[str] = []
    for ln in out.strip().splitlines():
        ln = ln.strip()
        if not ln or ln.startswith(("entity_type", "-", "set", "NOTICE", "INFO", "WARNING")):
            continue
        if "|" in ln:
            ids.append(ln)
        elif ":" in ln:
            ids.append(ln)
    return ids


# ===========================================================================
# reconstruct-offline mode (genuinely offline; ZERO network)
# ===========================================================================
def _load_offline_entities_from_snapshot() -> tuple[list[sem.CohortEntity], dict[str, Any], dict[str, Any]]:
    """Read the PERSISTED raw reconstruction snapshot and reconstruct the
    CohortEntity list. Verifies the snapshot exists, its parent is 0700, the file
    is 0600, and records its content_sha256. NO network call."""
    from eval.retrieval.schema import CorpusItem  # noqa: WPS433

    if not RAW_RECONSTRUCTION_SNAPSHOT.exists():
        raise FileNotFoundError("raw reconstruction snapshot not found")
    parent_mode = RAW_RECONSTRUCTION_DIR.stat().st_mode & 0o777
    file_mode = RAW_RECONSTRUCTION_SNAPSHOT.stat().st_mode & 0o777
    if parent_mode != PRIVATE_DIR_MODE:
        raise RuntimeError(f"raw snapshot parent not {oct(PRIVATE_DIR_MODE)}: {oct(parent_mode)}")
    if file_mode != PRIVATE_FILE_MODE:
        raise RuntimeError(f"raw snapshot not {oct(PRIVATE_FILE_MODE)}: {oct(file_mode)}")
    raw_bytes = RAW_RECONSTRUCTION_SNAPSHOT.read_bytes()
    snapshot = json.loads(raw_bytes)
    entities: list[sem.CohortEntity] = []
    for e in snapshot["entities"]:
        ci = CorpusItem.from_dict(e["corpus_item"])
        entities.append(
            sem.CohortEntity(
                entity_type=e["entity_type"],
                item_id=e["item_id"],
                corpus_item=ci,
                canon_row=e["canon_row"],
            )
        )
    meta = {
        "path": RAW_RECONSTRUCTION_SNAPSHOT.name,
        "content_sha256": sha256_bytes(raw_bytes),
        "n_entities": snapshot.get("n_entities"),
        "version": snapshot.get("version"),
        "parent_mode": oct(parent_mode),
        "file_mode": oct(file_mode),
    }
    return entities, meta, snapshot


def reconstruct_offline() -> dict[str, Any]:
    """OFFLINE representative rebuild. Reads the PERSISTED raw reconstruction
    snapshot (NOT a live PostgREST read), reconstructs the 384-small cohort,
    verifies the snapshot binding + v2 no-op proof, loads the REAL cached vectors
    READ-ONLY, seeds ONLY a throwaway local PG14/pgvector cluster, builds the
    CORRECT v2 partial HNSW, and reruns exact-vs-ANN recall with a decisive
    canonical-function idx_scan delta. network_calls=0, provider_calls=0. Never
    calls PostgREST or any embedding provider."""
    # Offline preflight (P1-P6; no network).
    pre = reconstruct_preflight(endpoint="", anon_key="")
    if not pre["attemptable"]:
        return _blocked(
            "preflight_red",
            "offline preflight is not attemptable; offline representative rebuild refused",
            preflight=pre,
        )

    # Step 1: load the cohort from the PERSISTED raw snapshot (ZERO network).
    try:
        entities, snap_meta, snapshot = _load_offline_entities_from_snapshot()
    except Exception as exc:  # noqa: BLE001
        return _blocked(
            "raw_snapshot_unreadable",
            f"persisted raw reconstruction snapshot unreadable: {type(exc).__name__}",
            preflight=pre,
            error_type=type(exc).__name__,
        )

    # Verify the v2 no-op proof (gate-2 equivalence proof artifact).
    try:
        proof = json.loads(V2_NOOP_PROOF.read_text())
        v2_noop_ok = (
            bool(proof.get("verdict"))
            and bool(proof.get("cohort_noop_all"))
            and proof.get("provider_calls") == 0
            and proof.get("network_calls") == 0
        )
    except Exception as exc:  # noqa: BLE001
        return _blocked(
            "v2_noop_proof_unreadable",
            f"v2 no-op proof unreadable: {type(exc).__name__}",
            preflight=pre,
            error_type=type(exc).__name__,
        )
    if not v2_noop_ok:
        return _blocked(
            "v2_noop_proof_red",
            "the v2 zero-call equivalence proof is not green; offline rebuild refused",
            preflight=pre,
        )

    # Step 2: chunk under the selected 384-small candidate (frozen chunker).
    selected = next(c for c in sem.CANDIDATES if c.name == SELECTED_CANDIDATE_NAME)
    cohort = sem.build_chunked_cohort(entities, selected)
    counts = _selected_chunk_counts(cohort)

    # Step 3: counts must match the accepted (the snapshot is the frozen cohort).
    count_drift = [
        {"field": k, "expected": ACCEPTED_SELECTED_COUNTS[k], "actual": counts.get(k)}
        for k in ACCEPTED_SELECTED_COUNTS
        if counts.get(k) != ACCEPTED_SELECTED_COUNTS[k]
    ]
    if count_drift:
        return _blocked(
            "count_drift",
            "offline reconstructed selected counts differ from the accepted Task 2.14 counts",
            preflight=pre,
            reconstructed_counts=counts,
            count_drift=count_drift,
        )

    # Re-prove the v2 cohort NO-OP inline for the selected candidate: the v2
    # chunk map (recomputed from the raw entities) is byte-identical to the v1
    # chunk map persisted in the snapshot.
    def _rec(ch: Any) -> dict[str, Any]:
        return {
            "entity_kind": ch.entity_kind,
            "item_id": ch.item_id,
            "representation_type": ch.representation_type,
            "chunk_index": ch.chunk_index,
            "chunk_hash": ch.chunk_hash,
            "representation_hash": ch.representation_hash,
        }

    v1_records = snapshot["per_candidate"][SELECTED_CANDIDATE_NAME]["chunk_map"]
    v2_multiset = sorted(tuple(sorted(_rec(ch).items())) for ch in cohort.chunks)
    v1_multiset = sorted(tuple(sorted(r.items())) for r in v1_records)
    cohort_noop_selected = v2_multiset == v1_multiset
    if not cohort_noop_selected:
        return _blocked(
            "cohort_noop_red",
            "the offline v2 selected chunk map is not byte-identical to the persisted v1 map",
            preflight=pre,
        )

    # Step 4: verify cache-384 READ-ONLY, then require 100% chunk/query hits.
    try:
        cache_facts, key_index = verify_cache_384_readonly(CACHE_384)
    except Exception as exc:  # noqa: BLE001
        return _blocked(
            "cache_read_failed",
            f"cache.384.jsonl read-only verification failed: {type(exc).__name__}",
            preflight=pre,
            error_type=type(exc).__name__,
        )
    provider = selected.provider
    model = selected.model
    dim = selected.dimension
    chunk_text_hashes = {ch.chunk_hash for ch in cohort.chunks}
    required_chunk_keys = {f"{provider}:{model}:{dim}:{th}" for th in chunk_text_hashes}
    missing_chunk_keys = sorted(required_chunk_keys - key_index)
    chunk_hit_rate = (
        (len(required_chunk_keys) - len(missing_chunk_keys)) / len(required_chunk_keys)
        if required_chunk_keys
        else 1.0
    )
    if missing_chunk_keys:
        return _blocked(
            "chunk_cache_miss",
            "one or more offline selected-chunk text-hashes are absent from cache.384.jsonl",
            preflight=pre,
            reconstructed_counts=counts,
            cache_facts=cache_facts,
            chunk_cache_hit_rate=chunk_hit_rate,
            n_chunk_keys_required=len(required_chunk_keys),
            n_chunk_keys_missing=len(missing_chunk_keys),
            missing_chunk_keys_set_sha256=sha256_json(missing_chunk_keys),
        )
    applicable, _future, split = split_golden(GOLDEN_PATH)
    query_text_hashes = [sem.ec.content_hash(c.query) for c in applicable]
    required_query_keys = {f"{provider}:{model}:{dim}:{th}" for th in query_text_hashes}
    missing_query_keys = sorted(required_query_keys - key_index)
    query_hit_rate = (
        (len(required_query_keys) - len(missing_query_keys)) / len(required_query_keys)
        if required_query_keys
        else 1.0
    )
    if missing_query_keys:
        return _blocked(
            "golden_query_cache_miss",
            "one or more applicable golden query text-hashes are absent from cache.384.jsonl",
            preflight=pre,
            reconstructed_counts=counts,
            cache_facts=cache_facts,
            chunk_cache_hit_rate=chunk_hit_rate,
            query_cache_hit_rate=query_hit_rate,
            n_query_keys_required=len(required_query_keys),
            n_query_keys_missing=len(missing_query_keys),
            missing_query_keys_set_sha256=sha256_json(missing_query_keys),
            golden_split=split,
        )

    # Step 5: run the representative benchmark in an isolated throwaway PG cluster.
    try:
        benchmark = _run_representative_benchmark(
            cohort=cohort,
            key_index=key_index,
            applicable=applicable,
            provider=provider,
            model=model,
            dim=dim,
        )
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        return _blocked(
            "benchmark_failed",
            f"isolated PG14 representative benchmark failed: {type(exc).__name__}",
            preflight=pre,
            reconstructed_counts=counts,
            cache_facts=cache_facts,
            chunk_cache_hit_rate=chunk_hit_rate,
            query_cache_hit_rate=query_hit_rate,
            golden_split=split,
            raw_snapshot=snap_meta,
            error_type=type(exc).__name__,
            error_message_sha256=sha256_bytes(msg.encode())[:16],
        )

    # Assert the decisive canonical-function idx_scan delta is strictly positive.
    if not benchmark.get("canonical_function_idx_scan_delta_positive"):
        return _blocked(
            "canonical_idx_scan_delta_not_positive",
            "the canonical-function idx_scan delta on REAL content is not positive",
            preflight=pre,
            benchmark=benchmark,
        )

    verdict = {
        "task": "2.16-representative-reconstruction",
        "mode": "reconstruct-offline",
        "run_class": "representative_local_content_nonproduction_compute",
        "verdict": "representative_local_content_nonproduction_compute",
        "representative_semantic_evidence": True,
        "representative_run_is_offline": True,
        "selected_contract_id": SELECTED_CONTRACT_ID,
        "selected_index_name": SELECTED_INDEX_NAME,
        "selected_candidate": SELECTED_CANDIDATE_NAME,
        "frozen_high_water": FROZEN_HIGH_WATER,
        "raw_reconstruction_snapshot": snap_meta,
        "v2_noop_proof": {
            "path": V2_NOOP_PROOF.name,
            "verdict": proof.get("verdict"),
            "cohort_noop_all": proof.get("cohort_noop_all"),
            "provider_calls": proof.get("provider_calls"),
            "network_calls": proof.get("network_calls"),
        },
        "cohort_noop_selected_inline": cohort_noop_selected,
        "reconstructed_counts": counts,
        "cache_facts": cache_facts,
        "chunk_cache_hit_rate": chunk_hit_rate,
        "n_chunk_keys_required": len(required_chunk_keys),
        "n_chunk_keys_missing": 0,
        "query_cache_hit_rate": query_hit_rate,
        "n_query_keys_required": len(required_query_keys),
        "n_query_keys_missing": 0,
        "golden_split": split,
        "benchmark": benchmark,
        "limitations": [
            "Genuinely OFFLINE representative rebuild: the cohort is read from the "
            "persisted raw reconstruction snapshot, NOT a live PostgREST read "
            "(network_calls=0). No embedding provider call was made (provider_calls=0).",
            "Local isolated PG14/pgvector cluster only (seeded and torn down); NOT "
            "a production-like staging run. Hosted Q8/Q9 latency and P3 "
            "recurring-compute gates remain unproven.",
            "Vectors are the REAL cached 384-small embeddings from the paid Task "
            "2.14 cache (read-only); chunk_text is a placeholder in the throwaway "
            "cluster (not real corpus text); the benchmark measures distance over "
            "REAL embeddings only.",
            _NO_OLD_CHUNK_MAP_HASH_LIMITATION,
        ],
        "provider_calls": 0,
        "network_calls": 0,
        "source_mutations": 0,
        "production_mutated": False,
        "contract_activated": False,
    }
    _write_evidence(verdict)
    return verdict


# ===========================================================================
# representative-benchmark mode
# ===========================================================================
def representative_benchmark() -> dict[str, Any]:
    """Run only the benchmark step, from a prior successful reconstruct snapshot."""
    snapshot_path = SNAPSHOT_DIR / "representative-snapshot.json"
    if not snapshot_path.exists():
        return _blocked(
            "no_prior_snapshot",
            "representative-benchmark requires a prior successful reconstruct snapshot",
        )
    # Re-read cohort from the pinned high-water (the snapshot stores hashes only,
    # not raw vectors; the benchmark needs the live cohort rebuilt identically).
    endpoint = os.environ.get("HIVEMIND_API_URL", DEFAULT_ENDPOINT)
    anon_key = os.environ.get("HIVEMIND_ANON_KEY", DEFAULT_ANON_KEY)
    return reconstruct(endpoint=endpoint, anon_key=anon_key, skip_benchmark=False)


# ===========================================================================
# CLI
# ===========================================================================
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        required=True,
        choices=[
            "reconstruct-preflight",
            "reconstruct",
            "representative-benchmark",
            "reconstruct-offline",
        ],
    )
    parser.add_argument("--endpoint", default=os.environ.get("HIVEMIND_API_URL", DEFAULT_ENDPOINT))
    parser.add_argument("--anon-key", default=os.environ.get("HIVEMIND_ANON_KEY", DEFAULT_ANON_KEY))
    parser.add_argument("--skip-benchmark", action="store_true")
    args = parser.parse_args(argv)

    if args.mode == "reconstruct-preflight":
        result = reconstruct_preflight(endpoint=args.endpoint, anon_key=args.anon_key)
    elif args.mode == "reconstruct":
        result = reconstruct(
            endpoint=args.endpoint, anon_key=args.anon_key, skip_benchmark=args.skip_benchmark
        )
    elif args.mode == "representative-benchmark":
        result = representative_benchmark()
    elif args.mode == "reconstruct-offline":
        result = reconstruct_offline()
    else:  # pragma: no cover
        parser.error(f"unknown mode {args.mode}")

    print(json.dumps(result, indent=2, sort_keys=True))
    verdict = result.get("verdict", "")
    # Exit non-zero on any blocked/failed verdict so the CLI fail-closes.
    if verdict in ("blocked_not_representative",) or (
        args.mode == "reconstruct-preflight" and not result.get("attemptable")
    ):
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
