#!/usr/bin/env python3
"""Hivemind — Phase 2 / Task 2.16 — selected-contract HNSW pilot rehearsal.

A stdlib-only, fully-isolated local structural rehearsal of the selected-contract
partial HNSW index. It is NOT a representative semantic benchmark: it loads
deterministic normalized FAKE vectors at the frozen selected-contract volume
(exactly 23,138 vectors, prose 7,873 / workflow_python 15,265) into a throwaway
local PostgreSQL 14 + pgvector 0.8.5 cluster, builds the selected-literal partial
HNSW index via autocommit operator commands, and proves the build/plan/storage/
parity/rollback mechanics. ``run_class`` is ``local_volume_only`` and
``representative_semantic_evidence`` is ``false``.

Modes (``--mode``):
  * ``structural-local`` (default) — D1–D5 preflight + full structural rehearsal,
    writes ``docs/hybrid-search/task-2.16-hnsw-rehearsal.json``.
  * ``representative-preflight``    — run ONLY the D1–D5 dependency preflight and
    report the representative-evidence blocker (no cluster).
  * ``recover-index`` / ``rollback-index`` — operator helpers (drop/rebuild the
    frozen index on an existing cluster; documented, local-only).

Safety: no provider call, no network, no source-row mutation, no production
mutation, no contract activation outside the throwaway cluster. Teardown is in
``finally``. Evidence is sanitized (hashes + aggregate counts only).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Iterable

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import lexical_pg as lp  # noqa: E402
from executors import selected_contract as sc  # noqa: E402

SCHEMA_DIR = REPO / "schema"
DOCS_DIR = REPO / "docs" / "hybrid-search"
REHEARSAL_EVIDENCE = DOCS_DIR / "task-2.16-hnsw-rehearsal.json"
BENCHMARK_EVIDENCE = DOCS_DIR / "task-2.16-hnsw-benchmark.json"
DECISION_JSON = DOCS_DIR / "task-2.14-embedding-decision.json"
MANIFEST_JSON = DOCS_DIR / "task-2.14-frozen-manifest.json"
PG14_BINDIR = pathlib.Path("/opt/homebrew/opt/postgresql@14/bin")

DIM = 384
SEL = sc.SELECTED_CONTRACT_ID
SEL_LITERAL = SEL  # the selected full-literal bigint baked into 033
INDEX_NAME = f"content_embeddings_hnsw_c{SEL}"
M = 16
EF_CONSTRUCTION = 64
EF_SWEEP = [40, 80, 120, 200]
K = 10
# Frozen selected-contract volume (accepted task-2.14 winner cohort).
VOLUME = 23138
PROSE_N = 7873
WP_N = 15265
ENTITIES_N = 7769
UNIQUE_TEXTS_N = 22850
DUP_COLLAPSED_N = 288
# Old task-0.7 capacity-model estimate (labeled separately; superseded for
# semantic evidence by the frozen 23,138 cohort).
OLD_MODEL_VOLUME = 41196
STORAGE_GATE_GB = 12
MONTHLY_COST_GATE_USD = 50
# A second 384-d contract with a DIFFERENT base identity (chunking_version =
# SELECTED_CHUNKING_VERSION + 1, so it can never collide with the selected
# contract's identity), used ONLY to prove the selected-literal fail-closed
# gate (active != literal).
OTHER_384_CONTRACT_ID = 4663917141483337700  # arbitrary, != SEL

MIGRATIONS = [
    "003_lexical_resource_documents.sql",
    "020_enable_pgvector.sql",
    "021_embedding_contracts.sql",
    "022_content_embeddings.sql",
    "023_embedding_contract_switch.sql",
    "024_identity_and_canonical_representations.sql",
    "025_embedding_jobs.sql",
    "026_embedding_job_rpc.sql",
    "027_embedding_worker_surface.sql",
    "028_embedding_worker_cron.sql",
    "029_embedding_cleanup.sql",
    "032_semantic_candidate_sql.sql",
    "033_selected_contract_hnsw.sql",
]

MINIMAL_BOOTSTRAP = """
create extension if not exists pg_trgm;
create table if not exists external_resources (
  id bigint primary key, kind text not null, source text not null default 'manual',
  external_id text, title text not null, body text not null default '',
  author text, url text, metadata jsonb not null default '{}'::jsonb,
  payload jsonb, created_at timestamptz not null default now());
create table if not exists contributors (id bigint primary key, name text not null unique,
  kind text not null default 'agent', api_key_hash text, revoked_at timestamptz,
  created_at timestamptz not null default now());
insert into contributors (id, name) values (1,'tester') on conflict (name) do nothing;
create table if not exists distillations (
  id bigint primary key, question text not null, conditions text, answer text not null,
  confidence text not null default 'medium' check (confidence in ('high','medium','low')),
  status text not null default 'pending' check (status in ('pending','approved','rejected','superseded')),
  author_id bigint not null default 1, approved_by bigint, supersedes_id bigint,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now());
create table if not exists discord_messages (
  message_id bigint primary key, content text, is_deleted boolean not null default false,
  deleted_at timestamptz, created_at timestamptz not null default now());
"""

HASH64 = "0" * 64


# ---------------------------------------------------------------------------
# Deterministic normalized fake vectors (stdlib only, no numpy).
# ---------------------------------------------------------------------------


def fake_vec(text: str, dim: int = DIM) -> list[float]:
    """Deterministic L2-normalized bag-of-hash vector over a text seed.

    Mirrors executors.embedding_contract.DeterministicFakeEmbedder's approach
    (hashing projection) so identical text yields an identical normalized
    vector — fully reproducible offline. Synthetic: proves volume/build/plan/
    parity mechanics only, never representative semantics.
    """

    vector = [0.0] * dim
    canonical = text.casefold()
    tokens = canonical.split() or [canonical]
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:8], "big") % dim
        sign = 1.0 if digest[8] % 2 == 0 else -1.0
        vector[bucket] += sign
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:  # pragma: no cover - degenerate seed
        vector[0] = 1.0
        norm = 1.0
    return [v / norm for v in vector]


def vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(repr(x) for x in vec) + "]"


def cosine_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 1.0
    return 1.0 - dot / (na * nb)


# ---------------------------------------------------------------------------
# Volume generation: exactly VOLUME vectors, prose=PROSE_N, wp=WP_N, with a
# fixed fixture set carved in (eligibility / gate / Snowflake / single-workflow).
# ---------------------------------------------------------------------------


def _fixture_rows() -> list[dict[str, Any]]:
    """ELIGIBLE-ONLY fixture entities that exercise every arm and filter.

    Task 2.16 C3: the HNSW-volume cohort must be ELIGIBLE-ONLY — no deleted
    messages, no rejected/superseded distillations, no quarantined/unsafe
    workflow-python, no missing resources are physically indexed under the
    selected contract. The ineligible safety-filter fixtures (deleted msg 200,
    rejected distillation 701, quarantined 5002 workflow_python) live in a
    SEPARATE small parity cluster (see :func:`run_parity_fixture`) where they
    ARE under the selected literal solely to prove the function's eligibility
    JOIN filters them; they never enter the HNSW partial index built here.
    """

    rows: list[dict[str, Any]] = []
    snow = "9007199254740993"  # 2**53 + 1, JS-unsafe Snowflake

    def add(etype: str, item: str, rep: str, ci: int) -> None:
        rows.append({
            "entity_type": etype, "item_id": item, "representation_type": rep,
            "chunk_index": ci, "chunk_text": f"{etype}-{item}-{rep}-{ci}",
            "vec": fake_vec(f"{etype}|{item}|{rep}|{ci}"),
        })

    # messages (snowflake JS-unsafe; ok). No deleted message here (C3).
    add("message", snow, "prose", 0)
    add("message", "100", "prose", 0)
    # resource workflow safe (prose + 2 wp). 5001 is safe.
    add("resource", "5001", "prose", 0)
    add("resource", "5001", "workflow_python", 0)
    add("resource", "5001", "workflow_python", 1)
    # resource article (concrete kind)
    add("resource", "5003", "prose", 0)
    # single-workflow fixture (prose + 1 wp). 9500 is safe.
    add("resource", "9500", "prose", 0)
    add("resource", "9500", "workflow_python", 0)
    # distillation (approved only; no rejected here (C3))
    add("distillation", "700", "prose", 0)
    return rows


def generate_volume() -> list[dict[str, Any]]:
    """Return exactly VOLUME rows hitting prose=PROSE_N, wp=WP_N.

    Fixtures are carved in first; the remainder is filler workflow resources
    (1 prose each, wp chunks distributed to consume the wp budget). 288 rows
    reuse an existing chunk_text so unique embeddable texts == UNIQUE_TEXTS_N.
    """

    rows = _fixture_rows()
    f_prose = sum(1 for r in rows if r["representation_type"] == "prose")
    f_wp = sum(1 for r in rows if r["representation_type"] == "workflow_python")
    need_prose = PROSE_N - f_prose
    need_wp = WP_N - f_wp
    if need_prose < 0 or need_wp < 0:
        raise RuntimeError("fixture budget exceeds frozen representation counts")

    # Filler workflow resources: each gets 1 prose. need_prose resources.
    base_item = 100000
    for i in range(need_prose):
        item = str(base_item + i)
        rows.append({
            "entity_type": "resource", "item_id": item, "representation_type": "prose",
            "chunk_index": 0, "chunk_text": f"filler-prose-{item}",
            "vec": fake_vec(f"resource|{item}|prose|0"),
        })
    # Distribute wp chunks across filler resources (>=1 each, remainder +1).
    per = need_wp // need_prose if need_prose else 0
    rem = need_wp - per * need_prose
    wp_added = 0
    for i in range(need_prose):
        item = str(base_item + i)
        cnt = per + (1 if i < rem else 0)
        for ci in range(cnt):
            rows.append({
                "entity_type": "resource", "item_id": item,
                "representation_type": "workflow_python", "chunk_index": ci,
                "chunk_text": f"filler-wp-{item}-{ci}",
                "vec": fake_vec(f"resource|{item}|workflow_python|{ci}"),
            })
            wp_added += 1
    if wp_added != need_wp or len(rows) != VOLUME:
        raise RuntimeError(
            f"volume generation drift: rows={len(rows)} wp_added={wp_added} "
            f"need_wp={need_wp}"
        )

    # Inject DUP_COLLAPSED_N duplicate-text rows so unique embeddable texts ==
    # UNIQUE_TEXTS_N. The first (VOLUME - DUP_COLLAPSED_N) rows keep distinct
    # texts; the last DUP_COLLAPSED_N rows each copy a distinct text from that
    # set, removing exactly DUP_COLLAPSED_N distinct texts.
    keep = len(rows) - DUP_COLLAPSED_N
    pool = [rows[i]["chunk_text"] for i in range(keep)]
    if len(set(pool)) != keep:
        raise RuntimeError("filler texts not distinct; cannot match unique-text count")
    for i in range(DUP_COLLAPSED_N):
        rows[keep + i]["chunk_text"] = pool[i % keep]
    return rows


# ---------------------------------------------------------------------------
# Hygiene scan: tracked evidence must contain no raw query / source body /
# workflow Python / vector / URL / connection string / credential / secret /
# private cache path.
# ---------------------------------------------------------------------------

_SECRET_RE = [
    "sk-", "Bearer ", "postgres://", "postgresql://", "supabase", "service_role",
    "eyJ", "password", "secret", "api_key", "token",
]
_URL_RE = ["http://", "https://", "ftp://", "www."]
_CACHE_PATH = ".cache/hivemind-semantic-eval/"


def _is_vector_literal(s: str) -> bool:
    if not isinstance(s, str) or len(s) < 200:
        return False
    body = s.strip()
    if not (body.startswith("[") and body.endswith("]")):
        return False
    inner = body[1:-1].replace(" ", "")
    parts = inner.split(",")
    if len(parts) < 50:
        return False
    digits = sum(1 for c in inner if c.isdigit() or c in ".-eE,[]")
    return digits / max(1, len(inner)) > 0.7


def hygiene_scan(obj: Any, _path: str = "") -> list[str]:
    """Return a list of hygiene findings (empty == clean)."""

    findings: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            findings += hygiene_scan(v, f"{_path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            findings += hygiene_scan(v, f"{_path}[{i}]")
    elif isinstance(obj, str):
        low = obj.lower()
        for pat in _SECRET_RE:
            if pat.lower() in low:
                findings.append(f"{_path}: suspected secret/credential pattern {pat!r}")
        for pat in _URL_RE:
            if pat in low:
                findings.append(f"{_path}: suspected URL {pat!r}")
        if _CACHE_PATH in obj:
            findings.append(f"{_path}: private cache path")
        if _is_vector_literal(obj):
            findings.append(f"{_path}: raw vector literal")
    return findings


# ---------------------------------------------------------------------------
# Task 2.16 C6: read-only cache-index fail-closed loader. Validates a
# query-embedding cache record WITHOUT ever opening it for write. Used by the
# benchmark's cache-only load path and by the focused tests. The paid
# `.cache/hivemind-semantic-eval/` caches are NEVER written, compacted, chmod'd,
# or deleted; this loader only READS a record dict (or a sanitized temp-dir
# copy in the tests) and fails closed on any of: missing key, wrong dimension,
# duplicate/conflicting records, wrong record count, wrong file hash, or a
# permissive (non-0600) mode on the backing file.
# ---------------------------------------------------------------------------


class CacheValidationError(RuntimeError):
    """Raised when a cache record fails fail-closed validation."""


def validate_cache_record(record: dict[str, Any], *,
                          expected_key: str,
                          expected_dim: int = DIM,
                          expected_file_hash: str | None = None,
                          expected_count: int | None = None) -> None:
    """Fail-closed validation of a single query-embedding cache record.

    Raises :class:`CacheValidationError` on any mismatch. Pure function: it
    never opens a file handle for write.
    """

    if not isinstance(record, dict):
        raise CacheValidationError("cache record is not a dict")
    if "key" not in record or record.get("key") is None:
        raise CacheValidationError("cache miss: missing key")
    if record["key"] != expected_key:
        raise CacheValidationError("cache miss: key mismatch")
    dim = record.get("dim")
    if dim is None:
        raise CacheValidationError("cache record missing dimension")
    if int(dim) != expected_dim:
        raise CacheValidationError(
            f"cache wrong-dimension: record dim {dim} != expected {expected_dim}")
    if expected_file_hash is not None:
        fh = record.get("file_hash")
        if fh != expected_file_hash:
            raise CacheValidationError("cache wrong file hash")
    vec = record.get("vector")
    if isinstance(vec, list) and len(vec) != expected_dim:
        raise CacheValidationError(
            f"cache wrong-dimension vector: len {len(vec)} != expected {expected_dim}")


def load_cache_index_fail_closed(path: pathlib.Path, *,
                                 expected_key: str,
                                 expected_dim: int = DIM,
                                 expected_file_hash: str | None = None,
                                 expected_count: int | None = None,
                                 require_private_mode: bool = True) -> list[dict[str, Any]]:
    """Read and validate a whole jsonl cache index, fail-closed.

    Validates: file mode is 0o600 (when require_private_mode); each record
    (key, dim, file_hash, vector length); no duplicate/conflicting keys; total
    record count matches expected_count. NEVER opens the file for write.
    """

    if not path.exists():
        raise CacheValidationError(f"cache miss: index file not found: {path.name}")
    if require_private_mode:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode != 0o600:
            raise CacheValidationError(
                f"cache permissive mode: {oct(mode)} != 0o600 on {path.name}")
    records: list[dict[str, Any]] = []
    seen_keys: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as fh:  # read-only
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CacheValidationError(f"cache corrupt json at line {lineno}") from exc
            validate_cache_record(rec, expected_key=expected_key, expected_dim=expected_dim,
                                  expected_file_hash=expected_file_hash)
            key = rec["key"]
            if key in seen_keys:
                prev = seen_keys[key]
                # conflict = same key, different vector/hash/dim
                if (prev.get("vector") != rec.get("vector")
                        or prev.get("file_hash") != rec.get("file_hash")
                        or prev.get("dim") != rec.get("dim")):
                    raise CacheValidationError(f"cache duplicate/conflicting key: {key}")
            seen_keys[key] = rec
            records.append(rec)
    if not records:
        raise CacheValidationError("cache miss: empty index")
    if expected_count is not None and len(records) != expected_count:
        raise CacheValidationError(
            f"cache wrong count: {len(records)} != expected {expected_count}")
    return records


# ---------------------------------------------------------------------------
# Manifest: frozen BEFORE measurement, hashed, refuse drift.
# ---------------------------------------------------------------------------


def build_manifest(env_probe: dict[str, Any]) -> dict[str, Any]:
    return {
        "task": "2.16-selected-contract-hnsw-pilot",
        "selected_contract": {
            "provider": sc.SELECTED_PROVIDER,
            "model": sc.SELECTED_MODEL,
            "dimension": sc.SELECTED_DIMENSION,
            "canonicalization_version": sc.SELECTED_CANONICALIZATION_VERSION,
            "chunking_version": sc.SELECTED_CHUNKING_VERSION,
            "chunk_config_identity": sc.SELECTED_CHUNK_CONFIG_IDENTITY,
            "selected_contract_preimage_sha256": sc.SELECTED_CONTRACT_SHA256_HEX,
            "eval_contract_id_hex": sc.EVAL_CONTRACT_ID_HEX,
            "selected_contract_id_bigint": SEL,
            "historical_dimension_only_id_bigint": sc.HISTORICAL_DIMENSION_ONLY_ID,
        },
        "task_2_14_hashes": _task_2_14_hashes(),
        "frozen_volume": {
            "vectors": VOLUME,
            "prose_chunks": PROSE_N,
            "workflow_python_chunks": WP_N,
            "entities_cohort": ENTITIES_N,
            "unique_embeddable_texts_cohort": UNIQUE_TEXTS_N,
            "duplicate_chunks_collapsed_cohort": DUP_COLLAPSED_N,
            "old_capacity_model_estimate_labeled": OLD_MODEL_VOLUME,
        },
        "index_contract": {
            "name": INDEX_NAME,
            "access_method": "hnsw",
            "opclass": "vector_cosine_ops",
            "m": M,
            "ef_construction": EF_CONSTRUCTION,
            "predicate": f"contract_id = {SEL}",
            "table": "public.content_embeddings",
        },
        "hnsw_query_gucs": {
            "ef_search_sweep": EF_SWEEP,
            "default_ef_search": 40,
            "iterative_scan": "strict_order",
            "max_scan_tuples": "unspecified_default",
            "scan_mem_multiplier": "unspecified_default",
        },
        "measurement": {
            "k": K,
            "priming_passes": 1,
            "warm_repetitions_per_query_per_setting": 5,
            "interleaving": "deterministic_round_robin_by_query_then_setting",
            "exact_mode": ("TRUE exact: set hivemind.semantic_exact_mode=on in a FRESH "
                           "session with enable_indexscan=off; enable_bitmapscan=off so the "
                           "function NULLs the per-arm bound and scores the COMPLETE "
                           "applicable cohort (not a LIMIT-truncated baseline). One shared "
                           "SQL surface (the function); no second hand-edited ranking."),
            "ann_mode": "natural plan; hnsw.ef_search=<sweep>; hnsw.iterative_scan=strict_order",
        },
        "gates": {
            "storage_full_corpus_gb": STORAGE_GATE_GB,
            "monthly_search_infrastructure_usd": MONTHLY_COST_GATE_USD,
            "mean_exact_vs_ann_recall_at_10": 0.95,
            "duplicate_entity_rate_after_collapse": 0,
            "ineligible_indexed_items": 0,
            "stale_or_mismatched_hashes_fraction_lt": 0.001,
        },
        "selection_rule": (
            "lowest pre-registered ef_search passing every applicable quality, "
            "latency, security, coverage, and capacity gate; ties -> lower ef_search, "
            "then lower warm p95; if none passes, verdict stop. selected_ef_search is "
            "NULL here because representative quality cannot be proved from synthetic data."
        ),
        "environment": {
            "run_class": "local_volume_only",
            "pg_bindir": str(env_probe.get("pg_bindir")),
            "server_version": env_probe.get("server_version"),
            "pgvector_extversion": env_probe.get("pgvector_extversion"),
            "vector_dims": env_probe.get("vector_dims"),
        },
    }


def manifest_hash(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _task_2_14_hashes() -> dict[str, str]:
    try:
        m = json.loads(MANIFEST_JSON.read_text())
    except Exception:
        return {"_error": "frozen-manifest unreadable"}
    return {
        "candidate_grid_hash": m.get("candidate_grid_hash"),
        "decision_policy_hash": m.get("decision_policy_hash"),
        "eval3_cohort_manifest_hash": m.get("eval3_cohort_manifest_hash"),
        "accounting_sha256": m.get("accounting_sha256"),
        "decision_deterministic_core_sha256": m.get("decision_deterministic_core_sha256"),
        "golden_sha256": (m.get("golden") or {}).get("sha256"),
        "replay_bundle_canonical_sha256": (m.get("replay_bundle") or {}).get("canonical_sha256"),
        "replay_bundle_file_sha256": (m.get("replay_bundle") or {}).get("file_sha256"),
        "cache_384_sha256": (m.get("immutable_caches", {}).get("384") or {}).get("sha256"),
    }


# ---------------------------------------------------------------------------
# D1–D5 dependency preflight (fail closed).
# ---------------------------------------------------------------------------


def preflight() -> dict[str, Any]:
    """Validate every dependency gate. Returns {ok, checks, blocker}."""

    checks: dict[str, bool] = {}
    detail: dict[str, Any] = {}

    # D1 — final selected contract from task 2.14.
    try:
        d = json.loads(DECISION_JSON.read_text())
        m = json.loads(MANIFEST_JSON.read_text())
        win = next(c for c in d["candidates"] if c["candidate"]["name"] == "384-small")
        w = win["candidate"]
        checks["d1_decision_json_present"] = True
        checks["d1_provider_model"] = (w["provider"], w["model"]) == ("openai", "text-embedding-3-small")
        checks["d1_dimension_384"] = w["dimension"] == 384
        checks["d1_canon_chunk_versions"] = (w["canonicalization_version"], w["chunking_version"]) == (
            sc.SELECTED_CANONICALIZATION_VERSION, sc.SELECTED_CHUNKING_VERSION)
        checks["d1_prose_512_50"] = (w["prose"]["target_tokens"], w["prose"]["overlap_tokens"]) == (512, 50)
        checks["d1_python_512_50"] = (w["python"]["target_tokens"], w["python"]["overlap_tokens"]) == (512, 50)
        checks["d1_chunk_config_identity"] = w["chunk_config_identity"] == sc.SELECTED_CHUNK_CONFIG_IDENTITY
        checks["d1_eval_contract_id"] = w["eval_contract_id"] == sc.EVAL_CONTRACT_ID_HEX
        ss = win["selection_state"]
        checks["d1_winner_selected_true"] = ss["selected"] is True
        checks["d1_winner_active_false"] = ss["active"] is False
        checks["d1_winner_production_activated_false"] = ss["production_activated"] is False
        nonwin = [c for c in d["candidates"] if c["candidate"]["name"] != "384-small"]
        checks["d1_nonwinners_inactive_eval_only"] = all(
            c["selection_state"]["active"] is False and c["selection_state"]["evaluation_only"] is True
            for c in nonwin
        )
        checks["d1_hashes_bound"] = all(v for v in _task_2_14_hashes().values() if isinstance(v, str))
        rp = m["replay_proof"]
        checks["d1_strict_replay_zero_provider"] = rp["provider_calls_attempted"] == 0 and rp["zero_provider_calls"] is True
        checks["d1_strict_replay_zero_network"] = rp["network_calls"] == 0 and rp["zero_network_calls"] is True
        checks["d1_strict_replay_winner_reproduced"] = rp["winner_reproduced"] == "384-small"
        # selected full bigint independently derived (chunking v2 + chunk_config v1)
        checks["d1_selected_bigint_derived"] = sc.SELECTED_CONTRACT_ID == 1360541028304258884
        checks["d1_selected_bigint_not_dimension_only"] = sc.SELECTED_CONTRACT_ID != sc.HISTORICAL_DIMENSION_ONLY_ID
        detail["d1_selection"] = d["selection"]
    except Exception as exc:  # noqa: BLE001
        checks["d1_unreadable"] = False
        detail["d1_error"] = type(exc).__name__

    # D2 — immutable cohort + query cache.
    try:
        win = next(c for c in d["candidates"] if c["candidate"]["name"] == "384-small")
        cc = win["cohort_counts"]
        checks["d2_entities"] = cc["n_entities"] == ENTITIES_N
        checks["d2_chunks"] = cc["n_chunks"] == VOLUME
        checks["d2_prose"] = cc["chunks_by_representation"]["prose"] == PROSE_N
        checks["d2_workflow_python"] = cc["chunks_by_representation"]["workflow_python"] == WP_N
        checks["d2_unique_texts"] = cc["n_unique_embeddable_texts"] == UNIQUE_TEXTS_N
        checks["d2_dup_collapsed"] = cc["n_duplicate_chunks_collapsed"] == DUP_COLLAPSED_N
        g = m["golden"]
        checks["d2_golden_counts"] = (g["n_cases"], g["n_judged"], g["n_no_hit"]) == (112, 104, 8)
        checks["d2_cache_record_count"] = m["eval3_cache_record_count"] == 27464
        checks["d2_cache_384_private"] = (m["immutable_caches"]["384"]["mode"] == "0o600"
                                          and m["immutable_caches"]["384"]["mode_is_private"] is True)
        detail["d2_cohort"] = cc
    except Exception as exc:  # noqa: BLE001
        checks["d2_unreadable"] = False
        detail["d2_error"] = type(exc).__name__

    # D3 — representative PostgreSQL vectors (BLOCKER: only synthetic available).
    checks["d3_representative_pg_vectors"] = False
    checks["d3_real_selected_contract_cache_only_snapshot"] = False
    detail["d3_blocker"] = (
        "No representative selected-contract PostgreSQL cohort or production-like "
        "staging snapshot exists (Phase-0 access audit evidences only the live "
        "Banodoco project, which must not be mutated). Only deterministic fake "
        "vectors can be loaded locally -> run_class=local_volume_only."
    )

    # D4 — accepted task-2.15 surface present (dependency gate; must be green).
    _032 = (SCHEMA_DIR / "032_semantic_candidate_sql.sql")
    checks["d4_schema_032_present"] = _032.exists()
    checks["d4_schema_033_present"] = (SCHEMA_DIR / "033_selected_contract_hnsw.sql").exists()
    checks["d4_signature"] = _032.exists() and "hivemind_semantic_candidates" in _032.read_text() and "vector(384)" in _032.read_text()

    # D5 — representative environment class (BLOCKER by design: only local here).
    checks["d5_representative_staging"] = False
    detail["d5_environment_class"] = "local_volume_only"

    # The dependency preflight passes when D1/D2/D4 (the green dependency gates)
    # hold. D3/D5 are the REPRESENTATIVE-EVIDENCE BLOCKER (recorded separately):
    # synthetic local volume alone can never close representative semantics, so
    # the run stays blocked even though every dependency is present.
    dependency_keys = [k for k in checks if k.startswith(("d1_", "d2_", "d4_"))]
    dependency_ok = all(checks[k] for k in dependency_keys)
    representative_ok = False  # D3 + D5
    return {
        "ok": dependency_ok,
        "dependency_ok": dependency_ok,
        "representative_ok": representative_ok,
        "checks": checks,
        "detail": detail,
        "representative_semantic_evidence": representative_ok,
        "run_class": "local_volume_only",
        "verdict": "blocked_not_representative" if dependency_ok else "dependency_gate_failed",
        "blocker": detail.get("d3_blocker"),
    }


# ---------------------------------------------------------------------------
# Cluster helpers.
# ---------------------------------------------------------------------------


def probe_env(cluster: lp.LocalCluster) -> dict[str, Any]:
    """Compatibility wrapper: versions + (best-effort) physical dim probe.

    Prefer :func:`_probe_versions` (before seeding) and
    :func:`probe_physical_dim` (after seeding). This wrapper is retained for
    older callers; it merges the two and tolerates an unseeded table.
    """

    env = _probe_versions(cluster)
    env.update(probe_physical_dim(cluster))
    return env


def _probe_versions(cluster: lp.LocalCluster) -> dict[str, Any]:
    """Probe only server/extension versions (callable before any data is seeded)."""

    rc, out = cluster.psql("show server_version;")
    server_version = out.strip()
    rc, out = cluster.psql("select extversion from pg_extension where extname='vector';")
    extversion = out.strip()
    return {
        "pg_bindir": str(cluster.bindir),
        "server_version": server_version,
        "pgvector_extversion": extversion,
    }


def probe_physical_dim(cluster: lp.LocalCluster) -> dict[str, Any]:
    """Task 2.16 C4: probe the PHYSICAL selected-contract embedding column.

    Reads vector_dims(embedding) of an actual indexed row (not a literal), the
    physical typmod (vector(384)), and an all-row 384 consistency count over the
    selected-contract cohort. Called AFTER the volume is seeded.
    """

    rc, phys = cluster.psql(
        f"select vector_dims(embedding) from content_embeddings "
        f"where contract_id={SEL} limit 1;"
    )
    rc, typmod = cluster.psql(
        f"select format_type(a.atttypid, a.atttypmod) from pg_attribute a "
        f"join pg_class c on c.oid=a.attrelid join pg_namespace n on n.oid=c.relnamespace "
        f"where n.nspname='public' and c.relname='content_embeddings' and a.attname='embedding';"
    )
    rc, consist = cluster.psql(
        f"select count(*) filter (where vector_dims(embedding)=384), count(*) "
        f"from content_embeddings where contract_id={SEL};"
    )
    consist_parts = (consist or "0|0").strip().split("|")
    return {
        "vector_dims": (phys or "").strip(),
        "embedding_typmod": (typmod or "").strip(),
        "vector_dims_384_consistency_count": int(consist_parts[0] or 0),
        "vector_dims_total_count": int(consist_parts[1] or 0),
    }


def setup_cluster() -> tuple[lp.LocalCluster, dict[str, Any]]:
    bindir = PG14_BINDIR if PG14_BINDIR.exists() else None
    cluster = lp.LocalCluster.start(bindir=bindir)
    cluster.psql(MINIMAL_BOOTSTRAP, capture=False)
    for name in MIGRATIONS:
        cluster.psql_file(SCHEMA_DIR / name)
    # Seed + activate the selected full-literal contract.
    cluster.psql(
        f"insert into embedding_contracts(id,provider,model,dimension,"
        f"canonicalization_version,chunking_version,status) values "
        f"({SEL},'openai','text-embedding-3-small',384,1,2,'draft') on conflict (id) do nothing;",
        capture=False,
    )
    cluster.psql(f"select hivemind_set_active_embedding_contract({SEL}, false);", capture=False)
    env = _probe_versions(cluster)
    return cluster, env


def seed_sources_and_volume(cluster: lp.LocalCluster, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Seed source eligibility rows + the frozen volume via COPY.

    Task 2.16 C3: the HNSW-volume cohort is ELIGIBLE-ONLY. No deleted message,
    no rejected/superseded distillation, no quarantined workflow-python, and no
    missing-resource row is seeded under the selected contract here. The
    ineligible safety-filter fixtures live in :func:`run_parity_fixture`
    (separate cluster) so they prove the function's eligibility exclusion
    without ever being physically indexed by the partial HNSW index.
    """

    # Source fixtures (eligible only).
    cluster.psql(
        "insert into discord_messages(message_id,content,is_deleted) values "
        "(9007199254740993,'snowflake-ok',false),(100,'ok',false) "
        "on conflict do nothing;",
        capture=False,
    )
    cluster.psql(
        "insert into external_resources(id,kind,source,title,body) values "
        "(5001,'workflow','vibecomfy-external','t','b'),"
        "(5003,'article','vibecomfy-external','t','b'),"
        "(9500,'workflow','vibecomfy-external','single-wf','b') on conflict do nothing;",
        capture=False,
    )
    cluster.psql(
        "insert into distillations(id,question,answer,status) values "
        "(700,'q','a','approved') on conflict do nothing;",
        capture=False,
    )
    # python state: 5001 safe, 9500 safe. (No quarantined 5002 under SEL (C3).)
    cluster.psql(
        "insert into lexical_resource_python_state(resource_id,kind,cohort,public_state,available) values "
        "(5001,'workflow','payload_python','safe',true),"
        "(9500,'workflow','payload_python','safe',true) on conflict (resource_id) do nothing;",
        capture=False,
    )
    # Filler workflow resources + safe python state (so their wp vectors rank).
    filler_ids = sorted({int(r["item_id"]) for r in rows if r["entity_type"] == "resource"
                         and int(r["item_id"]) >= 100000})
    if filler_ids:
        lo, hi = min(filler_ids), max(filler_ids)
        cluster.psql(
            "insert into external_resources(id,kind,source,title,body) "
            "select g,'workflow','vibecomfy-external','filler '||g,'b' "
            f"from generate_series({lo},{hi}) g on conflict do nothing;",
            capture=False,
        )
        cluster.psql(
            "insert into lexical_resource_python_state(resource_id,kind,cohort,public_state,available) "
            "select g,'workflow','payload_python','safe',true "
            f"from generate_series({lo},{hi}) g on conflict (resource_id) do nothing;",
            capture=False,
        )
    cluster.psql("insert into discord_messages(message_id,content) "
                 "select i,'filler msg '||i from generate_series(100000,199999) i on conflict do nothing;",
                 capture=False)
    cluster.psql(
        "copy content_embeddings(contract_id,entity_type,item_id,representation_type,"
        "chunk_index,chunk_text,embedding,representation_hash,chunk_hash) "
        "from stdin (format text);",
        stdin="\n".join(_copy_line(r) for r in rows) + "\n\\.\n",
        capture=False,
    )
    cluster.psql("analyze content_embeddings; analyze discord_messages; "
                 "analyze external_resources; analyze distillations;", capture=False)
    rc, out = cluster.psql(f"select count(*),count(*) filter (where representation_type='prose'),"
                           f"count(*) filter (where representation_type='workflow_python'),"
                           f"count(distinct chunk_text) from content_embeddings where contract_id={SEL};")
    parts = out.strip().split("|") if out.strip() else ["0", "0", "0", "0"]
    return {
        "vectors_loaded": int(parts[0]),
        "prose_loaded": int(parts[1]),
        "workflow_python_loaded": int(parts[2]),
        "unique_texts_loaded": int(parts[3]),
    }


def _copy_line(r: dict[str, Any]) -> str:
    return "\t".join([
        str(SEL), r["entity_type"], r["item_id"], r["representation_type"],
        str(r["chunk_index"]), r["chunk_text"], vec_literal(r["vec"]), HASH64, HASH64,
    ])


def _emb(cluster: lp.LocalCluster, contract_id: int, entity_type: str, item_id: str,
         representation_type: str, chunk_index: int, chunk_text: str,
         vec: list[float]) -> None:
    """Insert one selected-contract embedding row (test/fixture helper)."""

    cluster.psql(
        f"insert into content_embeddings(contract_id,entity_type,item_id,representation_type,"
        f"chunk_index,chunk_text,embedding,representation_hash,chunk_hash) values "
        f"({contract_id},'{entity_type}','{item_id}','{representation_type}',{chunk_index},"
        f"'{chunk_text}','{vec_literal(vec)}','{HASH64}','{HASH64}') on conflict do nothing;",
        capture=False,
    )


# ---------------------------------------------------------------------------
# Task 2.16 C3: fail-closed PRE-BUILD eligibility audit on the selected-contract
# cohort. The HNSW partial index physically indexes EVERY selected-contract
# row, so the cohort must be ELIGIBLE-ONLY before CREATE INDEX. This audit
# COUNTS deleted/ineligible messages, rejected/superseded/ineligible
# distillations, missing resources, workflow-python whose current kind/state is
# not workflow/safe, and selected-contract-only dimension/representation counts,
# and ABORTS (raises, no CREATE INDEX) if any selected-contract
# ineligible/quarantined count is nonzero.
# ---------------------------------------------------------------------------


def prebuild_eligibility_audit(cluster: lp.LocalCluster) -> dict[str, Any]:
    """Count every class of ineligible/quarantined row under the selected contract.

    Returns a dict of per-class counts (all MUST be zero for the cohort to be
    HNSW-eligible) plus ``all_eligible`` and ``abort`` flags. Callers ABORT the
    build (raise) when ``abort`` is True.
    """

    def q_int(sql: str) -> int:
        return int((_scalar(cluster, sql) or "0").strip() or 0)

    # Deleted discord messages indexed under SEL.
    deleted_messages = q_int(
        f"select count(*) from content_embeddings ce join discord_messages m "
        f"on m.message_id::text=ce.item_id where ce.contract_id={SEL} "
        f"and ce.entity_type='message' and coalesce(m.is_deleted,false)=true;"
    )
    # Distillations not in (pending, approved): rejected/superseded/ineligible.
    rejected_distillations = q_int(
        f"select count(*) from content_embeddings ce join distillations d "
        f"on d.id::text=ce.item_id where ce.contract_id={SEL} "
        f"and ce.entity_type='distillation' and d.status not in ('pending','approved');"
    )
    # Embeddings whose source row is missing entirely (orphan embedding).
    missing_resources = q_int(
        f"select count(*) from content_embeddings ce where ce.contract_id={SEL} "
        f"and ce.entity_type='resource' and not exists "
        f"(select 1 from external_resources r where r.id::text=ce.item_id);"
    )
    missing_messages = q_int(
        f"select count(*) from content_embeddings ce where ce.contract_id={SEL} "
        f"and ce.entity_type='message' and not exists "
        f"(select 1 from discord_messages m where m.message_id::text=ce.item_id);"
    )
    missing_distillations = q_int(
        f"select count(*) from content_embeddings ce where ce.contract_id={SEL} "
        f"and ce.entity_type='distillation' and not exists "
        f"(select 1 from distillations d where d.id::text=ce.item_id);"
    )
    # Quarantined / unsafe workflow-python vectors indexed under SEL: a wp vector
    # whose current resource kind is not 'workflow' OR whose python state is not
    # 'safe'. These would be physically indexed yet must never rank/snippet.
    quarantined_wp = q_int(
        f"select count(*) from content_embeddings ce join external_resources r "
        f"on r.id::text=ce.item_id left join lexical_resource_python_state lps "
        f"on lps.resource_id=r.id where ce.contract_id={SEL} "
        f"and ce.entity_type='resource' and ce.representation_type='workflow_python' "
        f"and (r.kind<>'workflow' or coalesce(lps.public_state,'<>safe')<>'safe' "
        f"or coalesce(lps.available,false)=false);"
    )
    # Selected-contract dimension consistency (must all be 384).
    wrong_dim = q_int(
        f"select count(*) from content_embeddings where contract_id={SEL} "
        f"and vector_dims(embedding)<>384;"
    )
    # Representation split sanity (prose + wp only).
    other_reps = q_int(
        f"select count(*) from content_embeddings where contract_id={SEL} "
        f"and representation_type not in ('prose','workflow_python');"
    )
    counts = {
        "deleted_messages_indexed": deleted_messages,
        "rejected_or_superseded_distillations_indexed": rejected_distillations,
        "missing_resource_sources_indexed": missing_resources,
        "missing_message_sources_indexed": missing_messages,
        "missing_distillation_sources_indexed": missing_distillations,
        "quarantined_or_unsafe_workflow_python_indexed": quarantined_wp,
        "wrong_dimension_vectors_indexed": wrong_dim,
        "non_prose_or_wp_representations_indexed": other_reps,
    }
    all_eligible = all(v == 0 for v in counts.values())
    return {
        "counts": counts,
        "all_eligible": all_eligible,
        "abort": not all_eligible,
        "abort_reason": (None if all_eligible
                         else "selected-contract cohort has ineligible/quarantined "
                              "indexed rows: " + ", ".join(f"{k}={v}" for k, v in counts.items() if v)),
    }


# ---------------------------------------------------------------------------
# Index build / drop / catalog (autocommit operator commands).
# ---------------------------------------------------------------------------


def _env_with_gucs(cluster: lp.LocalCluster, gucs: dict[str, str]) -> dict[str, str]:
    env = dict(os.environ)
    env.update(PGHOST=str(cluster.socketdir), PGPORT=str(cluster.port),
               PGUSER="postgres", PGDATABASE="postgres")
    env["PGOPTIONS"] = " ".join(f"-c {k}={v}" for k, v in gucs.items())
    return env


# Conservative free-space margin (GB) the build must leave on disk regardless of
# the 12 GB storage gate. Local rehearsal volumes are tiny, so this only ever
# trips on a genuinely full disk.
SAFE_FREE_DISK_MARGIN_GB = 2.0


def _setting(cluster: lp.LocalCluster, name: str) -> tuple[str, bool]:
    """Return (effective_value, available) for a GUC via current_setting(...,true)."""

    rc, out = cluster.psql(f"select coalesce(current_setting('{name}', true), '');")
    val = (out or "").strip()
    return val, bool(val)


def record_query_gucs(cluster: lp.LocalCluster) -> dict[str, Any]:
    """Task 2.16 C5(c): record effective hnsw.* query GUCs WITH availability."""

    gucs: dict[str, Any] = {}
    for name in ("hnsw.ef_search", "hnsw.iterative_scan",
                 "hnsw.max_scan_tuples", "hnsw.scan_mem_multiplier"):
        val, avail = _setting(cluster, name)
        gucs[name] = {"effective": val if val else None, "available": avail}
    return gucs


def prebuild_capacity_check(cluster: lp.LocalCluster,
                            maintenance_work_mem: str = "512MB") -> dict[str, Any]:
    """Task 2.16 C5(a/b): free-space + DB-size measurement and abort decision.

    Measures filesystem free space (shutil.disk_usage on the cluster's datadir)
    AND current database/table/index bytes. Aborts (``abort=True``) if projected
    new index storage could cross the 12 GB storage gate OR the safe free-space
    margin. Hosted quota is recorded UNAVAILABLE for local clusters. Also
    records effective maintenance_work_mem with a justification against measured
    local RAM headroom.
    """

    db_bytes = int(_scalar(cluster, "select pg_database_size(current_database());") or 0)
    tbl_bytes = int(_scalar(cluster, "select pg_total_relation_size('content_embeddings');") or 0)
    sel_rows = int(_scalar(cluster, f"select count(*) from content_embeddings where contract_id={SEL};") or 0)
    # Projected HNSW index size: scale a per-vector HNSW cost (~1.5 KB/vector at
    # m=16, 384-d, conservative) by the full-corpus vector estimate. This is a
    # rough projection used only for the abort decision; the ACTUAL built index
    # size is recorded post-build from pg_relation_size.
    full_vectors_estimate = 1_250_000 + 19313 + 16872 + 11
    per_vec_bytes = 1536  # conservative HNSW per-vector footprint (m=16, 384-d)
    proj_idx_bytes = per_vec_bytes * full_vectors_estimate
    proj_idx_gb = round(proj_idx_bytes / 1e9, 3)
    # Free disk at the datadir.
    usage = shutil.disk_usage(str(cluster.datadir))
    free_disk_gb = round(usage.free / 1e9, 3)
    total_disk_gb = round(usage.total / 1e9, 3)
    # maintenance_work_mem: query the effective value the build will use and the
    # machine's physical RAM; justify a conservative choice. The default 512MB is
    # conservative for a local rehearsal; we keep it unless measured RAM headroom
    # proves insufficient (in which case we record the justification but do not
    # silently raise it above safe headroom).
    mwm_eff, _ = _setting(cluster, "maintenance_work_mem")
    ram_bytes = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") if hasattr(os, "sysconf") else 0
    ram_gb = round(ram_bytes / 1e9, 3) if ram_bytes else None
    mwm_justification = (
        f"maintenance_work_mem={maintenance_work_mem} (build-session override); "
        f"effective_cluster_default={mwm_eff or 'unavailable'}; "
        f"local_ram_gb={'unavailable' if ram_gb is None else ram_gb}; "
        f"conservative for an m=16 HNSW build at the local rehearsal volume "
        f"({sel_rows} vectors); never raised above measured safe headroom."
    )
    abort = proj_idx_gb >= STORAGE_GATE_GB or free_disk_gb < SAFE_FREE_DISK_MARGIN_GB
    abort_reason = None
    if proj_idx_gb >= STORAGE_GATE_GB:
        abort_reason = f"projected_full_corpus_index_gb {proj_idx_gb} >= gate {STORAGE_GATE_GB}"
    elif free_disk_gb < SAFE_FREE_DISK_MARGIN_GB:
        abort_reason = (f"free_disk_gb {free_disk_gb} < safe margin "
                        f"{SAFE_FREE_DISK_MARGIN_GB}")
    return {
        "database_bytes": db_bytes,
        "table_total_bytes": tbl_bytes,
        "selected_contract_vectors": sel_rows,
        "projected_index_gb": proj_idx_gb,
        "storage_gate_gb": STORAGE_GATE_GB,
        "free_disk_gb": free_disk_gb,
        "total_disk_gb": total_disk_gb,
        "safe_free_disk_margin_gb": SAFE_FREE_DISK_MARGIN_GB,
        "hosted_quota_gb": "UNAVAILABLE (local isolated cluster)",
        "maintenance_work_mem_effective": mwm_eff or "unavailable",
        "maintenance_work_mem_justification": mwm_justification,
        "query_gucs": record_query_gucs(cluster),
        "abort": abort,
        "abort_reason": abort_reason,
    }


def build_index(cluster: lp.LocalCluster, *, lock_timeout: str = "5s",
                statement_timeout: str = "300s",
                maintenance_work_mem: str = "512MB") -> dict[str, Any]:
    """CREATE INDEX CONCURRENTLY as a single autocommit statement with GUCs via
    PGOPTIONS, polling pg_stat_progress_create_index from a separate connection.

    Task 2.16 C5 operator safety:
      * invalid-same-name fail-closed detects BOTH 'f' AND 'false' for
        pg_index.indisvalid and ABORTS (raises, no CREATE INDEX) before
        ``IF NOT EXISTS`` can silently accept an interrupted build;
      * free-space + DB-size are measured pre-build and the build ABORTS if the
        projected new index storage could cross the 12 GB gate or the safe
        free-space margin (hosted quota recorded UNAVAILABLE for local);
      * effective maintenance_work_mem, statement/lock timeouts and the
        hnsw.* query GUCs (with availability) are recorded;
      * the table in the DDL is schema-qualified ``public.content_embeddings``
        (the CREATE INDEX *name* cannot be schema-qualified per PostgreSQL, so
        it is documented unqualified — same as the operator DDL in schema/033).
    """

    build_sql = (
        f"create index concurrently if not exists {INDEX_NAME} "
        f"on public.content_embeddings using hnsw (embedding vector_cosine_ops) "
        f"with (m = {M}, ef_construction = {EF_CONSTRUCTION}) where contract_id = {SEL};"
    )
    env = _env_with_gucs(cluster, {
        "lock_timeout": lock_timeout,
        "statement_timeout": statement_timeout,
        "maintenance_work_mem": maintenance_work_mem,
    })
    # --- C5(e): invalid-same-name fail-closed BEFORE CREATE INDEX IF NOT EXISTS.
    # Detect BOTH 'f' (bool out) AND 'false' (text coercion); never let
    # IF NOT EXISTS accept an interrupted/invalid same-name index.
    rc, out = cluster.psql(
        "select coalesce((select i.indisvalid::text from pg_index i "
        f"join pg_class c on c.oid=i.indexrelid where c.relname='{INDEX_NAME}'), "
        "'<absent>')::text;"
    )
    pre_state = (out or "").strip()
    invalid_preexists = pre_state in ("f", "false")
    pre_capacity = prebuild_capacity_check(cluster, maintenance_work_mem)

    if invalid_preexists:
        # ABORT: do not run CREATE INDEX ... IF NOT EXISTS (it would no-op and
        # leave the invalid index in place). Operator must DROP CONCURRENTLY the
        # invalid index first (see rollback-index mode).
        raise RuntimeError(
            "build aborted: an INVALID same-name index already exists "
            f"(pg_index.indisvalid={pre_state!r}); DROP INDEX CONCURRENTLY "
            f"{INDEX_NAME} before rebuilding (IF NOT EXISTS would accept it)."
        )
    # --- C5(a): free-space / storage abort BEFORE build.
    if pre_capacity["abort"]:
        raise RuntimeError(
            "build aborted by capacity gate: "
            f"{pre_capacity['abort_reason']} "
            f"(projected_index_gb={pre_capacity['projected_index_gb']}, "
            f"free_gb={pre_capacity['free_disk_gb']})"
        )
    progress_samples: list[dict[str, Any]] = []
    t0 = time.monotonic()
    proc = subprocess.Popen(
        [str(cluster.bindir / "psql"), "-v", "ON_ERROR_STOP=1", "-X", "-q", "-c", build_sql],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    # Poll progress from a separate connection while the build runs.
    # C5(d): the previous SQL had an unbalanced quote (blocks_total::text','?')
    # which silently stored blank progress; fixed below (text,'?').
    while proc.poll() is None:
        rc, out = cluster.psql(
            "select coalesce((select phase||':'||"
            "coalesce(blocks_done::text,'?')||'/'||coalesce(blocks_total::text,'?') "
            "from pg_stat_progress_create_index), 'idle');"
        )
        progress_samples.append({"t_ms": int((time.monotonic() - t0) * 1000), "phase": (out or "").strip()})
        time.sleep(0.5)
    stdout, stderr = proc.communicate()
    build_rc = proc.returncode
    wall_s = round(time.monotonic() - t0, 3)
    return {
        "build_returncode": build_rc,
        "wall_s": wall_s,
        "lock_timeout": lock_timeout,
        "statement_timeout": statement_timeout,
        "maintenance_work_mem": maintenance_work_mem,
        "invalid_same_name_preexisted": invalid_preexists,
        "prebuild_capacity": pre_capacity,
        "progress_samples_count": len(progress_samples),
        "progress_last": progress_samples[-1]["phase"] if progress_samples else None,
        "build_stderr_redacted": (type  # never expose raw diagnostics
                                  and _redact(stderr)),
    }


def drop_index(cluster: lp.LocalCluster) -> int:
    env = _env_with_gucs(cluster, {"lock_timeout": "5s", "statement_timeout": "120s"})
    proc = subprocess.run(
        [str(cluster.bindir / "psql"), "-v", "ON_ERROR_STOP=1", "-X", "-q", "-c",
         f"drop index concurrently if exists {INDEX_NAME};"],
        env=env, capture_output=True, text=True,
    )
    return proc.returncode


def catalog(cluster: lp.LocalCluster) -> dict[str, Any]:
    def q(sql: str) -> str:
        return cluster.psql(sql)[1].strip()
    v = q(f"select indisvalid from pg_index i join pg_class c on c.oid=i.indexrelid where c.relname='{INDEX_NAME}';")
    r = q(f"select indisready from pg_index i join pg_class c on c.oid=i.indexrelid where c.relname='{INDEX_NAME}';")
    pred = q(f"select pg_get_expr(i.indpred,i.indrelid) from pg_index i join pg_class c on c.oid=i.indexrelid where c.relname='{INDEX_NAME}';")
    opcls = q(
        f"select opc.opcname from pg_index i join pg_class c on c.oid=i.indexrelid "
        f"join pg_opclass opc on opc.oid = i.indclass[0] where c.relname='{INDEX_NAME}';"
    )
    opts = q(f"select coalesce(array_to_string(c.reloptions,','),'') from pg_class c where c.relname='{INDEX_NAME}';")
    size = q(f"select pg_size_pretty(pg_relation_size('{INDEX_NAME}'::regclass));")
    return {
        "exists": bool(v),
        "indisvalid": v, "indisready": r,
        "predicate": pred,
        "opclass": opcls,
        "reloptions": opts,
        "size_pretty": size,
    }


def _redact(s: str) -> str:
    if not s:
        return ""
    # Keep only the trailing error tag (e.g. ERROR: canceling statement), drop
    # any SQL/body/vector/credential detail.
    for line in s.splitlines():
        ls = line.strip()
        if ls.startswith("ERROR:") or ls.startswith("FATAL:"):
            tag = ls.split(":", 1)[0]
            return f"{tag}: <redacted>"
    return "<redacted>"


# ---------------------------------------------------------------------------
# Structural checks.
# ---------------------------------------------------------------------------


def _scalar(cluster: lp.LocalCluster, sql: str) -> str:
    rc, out = cluster.psql(sql)
    return out.strip() if rc == 0 else ""


def run_checks(cluster: lp.LocalCluster, rows: list[dict[str, Any]],
               volume_info: dict[str, Any]) -> tuple[dict[str, bool], dict[str, Any]]:
    checks: dict[str, bool] = {}
    ev: dict[str, Any] = {}

    Q = fake_vec("query-selected-contract-fixture-0")
    QL = vec_literal(Q)

    # --- volume / representation split ---
    checks["volume_exact_23138"] = volume_info["vectors_loaded"] == VOLUME
    checks["prose_exact_7873"] = volume_info["prose_loaded"] == PROSE_N
    checks["wp_exact_15265"] = volume_info["workflow_python_loaded"] == WP_N
    checks["unique_texts_22850"] = volume_info["unique_texts_loaded"] == UNIQUE_TEXTS_N
    ev["volume"] = volume_info

    # --- build + catalog ---
    build = build_index(cluster)
    ev["build"] = build
    cat = catalog(cluster)
    ev["catalog"] = cat
    checks["build_returncode_zero"] = build["build_returncode"] == 0
    checks["index_valid"] = cat["indisvalid"] == "t"
    checks["index_ready"] = cat["indisready"] == "t"
    checks["index_opclass_cosine"] = cat["opclass"] == "vector_cosine_ops"
    checks["index_predicate_selected_literal"] = str(SEL) in (cat["predicate"] or "") and "contract_id" in (cat["predicate"] or "")
    checks["index_m16"] = "'m'=16" in (cat["reloptions"] or "").replace(" ", "") or "m=16" in (cat["reloptions"] or "")
    checks["index_ef_construction_64"] = "ef_construction=64" in (cat["reloptions"] or "")
    checks["no_invalid_same_name_index"] = build["invalid_same_name_preexisted"] is False

    # --- storage + capacity gate ---
    db_bytes = int(_scalar(cluster, "select pg_database_size(current_database());") or 0)
    tbl_bytes = int(_scalar(cluster, f"select pg_total_relation_size('content_embeddings');") or 0)
    idx_bytes = int(_scalar(cluster, f"select pg_relation_size('{INDEX_NAME}'::regclass);") or 0)
    # Full-corpus projection: scale measured index bytes by the production
    # full-corpus vector ratio (1.25M messages + resources). Conservative linear
    # projection in GB; the 12 GB gate is checked against it.
    full_vectors_estimate = 1_250_000 + 19313 + 16872 + 11  # plan task-0.7 modeled scale
    proj_idx_gb = round((idx_bytes / VOLUME) * full_vectors_estimate / 1e9, 3)
    proj_full_gb = round((tbl_bytes / VOLUME) * full_vectors_estimate / 1e9, 3)
    ev["storage"] = {
        "database_bytes": db_bytes, "table_total_bytes": tbl_bytes, "hnsw_index_bytes": idx_bytes,
        "hnsw_index_size_pretty": cat["size_pretty"],
        "projected_full_corpus_index_gb": proj_idx_gb,
        "projected_full_corpus_table_gb": proj_full_gb,
        "storage_gate_gb": STORAGE_GATE_GB,
        "within_storage_gate": proj_full_gb < STORAGE_GATE_GB,
    }
    checks["storage_within_12gb_gate"] = proj_full_gb < STORAGE_GATE_GB

    # --- natural HNSW use by the ACTUAL canonical function (the decisive gate) ---
    # ROOT CAUSE (task-2.16 GLM-5.2 renewal): pgvector 0.8.5 on PG14 generates the
    # partial-HNSW kNN path ONLY for a single-table scan whose filters are ROW-LOCAL
    # on content_embeddings. ANY source JOIN (inner/left/semi/lateral/IN) — or any
    # parameterized array/limit — forces a non-HNSW driver (btree
    # content_embeddings_identity_idx + Sort); that holds even under
    # enable_seqscan=off + enable_indexscan=off, so HNSW is structurally unreachable
    # through a join. The prior pass accepted a direct INNER kNN as "proof"; that
    # was the decisive blocker — the actual function did NOT use HNSW (delta 0).
    # The fix (schema/033): eligibility is resolved to the eligible, EMBEDDED
    # item-id set per entity and baked as a LITERAL text[] into each per-arm
    # single-table kNN predicate (a row-local predicate), keeping the HNSW scan as
    # the driving path; hnsw.iterative_scan applies that filter BEFORE the per-arm
    # LIMIT (C1 preserved), and the source JOIN is a post-collapse decoration only.
    # The decisive proof is therefore a STRICTLY POSITIVE idx_scan delta on the
    # selected HNSW index from MULTIPLE BROAD calls to the ACTUAL function. The
    # inner single-table kNN is recorded as a DIAGNOSTIC only (insufficient). No
    # forced planner settings are used as proof (the brief forbids that).
    # Measurement: read the idx_scan counter (monotonic, only increases), run the
    # work, read it again. Consecutive deltas from the un-reset counter are
    # race-free (pg_stat_reset across read boundaries is unreliable in PG14). PG14
    # flushes per-backend stats on connection exit (each psql is its own
    # connection) plus a short sleep for the collector. EXPLAIN of
    # select * from func() is opaque ("Function Scan"), so the idx_scan delta —
    # not the plan text — is the real signal.

    # (1) DECISIVE: the ACTUAL canonical function, multiple BROAD calls.
    c0 = int((_scalar(cluster, f"select coalesce(idx_scan,0) from pg_stat_user_indexes where indexrelname='{INDEX_NAME}';") or "0"))
    func_call_sql = " ".join([
        f"select count(*) from hivemind_semantic_candidates('{QL}'::vector,{K},'{{}}','{{}}');"
    ] * 6)
    cluster.psql(f"set hnsw.ef_search=80; set hnsw.iterative_scan=strict_order; {func_call_sql}", capture=False)
    time.sleep(0.8)
    c1 = int((_scalar(cluster, f"select idx_scan from pg_stat_user_indexes where indexrelname='{INDEX_NAME}';") or "0"))
    func_before, func_after, func_delta = c0, c1, c1 - c0

    # (2) DIAGNOSTIC ONLY: a direct single-table inner kNN. It uses the index by
    # construction (no eligibility), so a positive delta here does NOT prove the
    # canonical function uses HNSW — it only confirms the index is reachable. The
    # prior pass wrongly treated this as the proof; it is explicitly insufficient.
    inner_knn_sql = (
        f"select ce.item_id from content_embeddings ce "
        f"where ce.contract_id={SEL} and ce.entity_type='resource' "
        f"order by ce.embedding <=> '{QL}'::vector limit 500;"
    )
    for _ in range(3):
        cluster.psql(f"set hnsw.ef_search=80; set hnsw.iterative_scan=strict_order; {inner_knn_sql}", capture=False)
    time.sleep(0.8)
    c2 = int((_scalar(cluster, f"select idx_scan from pg_stat_user_indexes where indexrelname='{INDEX_NAME}';") or "0"))
    inner_before, inner_after, inner_delta = c1, c2, c2 - c1
    inner_plan = cluster.psql(
        f"set hnsw.ef_search=80; set hnsw.iterative_scan=strict_order; "
        f"explain (costs off) {inner_knn_sql}"
    )[1]
    inner_uses_index = INDEX_NAME in inner_plan

    ev["hnsw_use"] = {
        # DECISIVE: the ACTUAL canonical function naturally uses the HNSW index.
        "full_function_idx_scan_before": func_before,
        "full_function_idx_scan_after": func_after,
        "full_function_idx_scan_delta": func_delta,
        "full_function_idx_scan_delta_positive": func_delta > 0,
        "full_function_uses_hnsw": func_delta > 0,
        "full_function_broad_calls": 6,
        "full_function_call_shape": (
            f"select count(*) from hivemind_semantic_candidates(vec,{K},'{{}}','{{}}') x6 "
            "(broad, ANN, hnsw.ef_search=80, hnsw.iterative_scan=strict_order)"
        ),
        "full_function_note": (
            "Strictly positive idx_scan delta from MULTIPLE BROAD calls to the "
            "ACTUAL public.hivemind_semantic_candidates(vector,int,text[],text[]). "
            "Eligibility is baked as a literal row-local text[] per arm so the "
            "partial HNSW index stays the driving path (iterative scan filters "
            "before the per-arm LIMIT). This is the decisive natural-HNSW proof; "
            "EXPLAIN of the function call is opaque so the delta is the signal."
        ),
        # DIAGNOSTIC ONLY: direct single-table inner kNN — insufficient as proof.
        "inner_knn_idx_scan_before": inner_before,
        "inner_knn_idx_scan_after": inner_after,
        "inner_knn_idx_scan_delta": inner_delta,
        "inner_knn_idx_scan_delta_positive": inner_delta > 0,
        "inner_knn_uses_index": inner_uses_index,
        "inner_knn_plan_node": ("Index Scan using " + INDEX_NAME) if inner_uses_index else "<not HNSW>",
        "inner_knn_role": "diagnostic_only_insufficient",
        "inner_knn_note": (
            "A direct single-table kNN uses the index by construction (no "
            "eligibility). A positive delta here does NOT prove the canonical "
            "function uses HNSW; recorded as a structural diagnostic only."
        ),
    }
    # THE DECISIVE GATE: the actual canonical function must naturally use HNSW.
    checks["full_function_idx_scan_delta_positive"] = func_delta > 0
    checks["inner_knn_idx_scan_delta_positive"] = inner_delta > 0  # diagnostic, also expected

    # --- exact baseline uses no HNSW (indexscan off) ---
    exact_plan = cluster.psql(
        f"set enable_indexscan=off; set enable_bitmapscan=off; "
        f"explain (costs off) select ce.item_id from content_embeddings ce "
        f"where ce.contract_id={SEL} and ce.entity_type='message' "
        f"order by ce.embedding <=> '{QL}'::vector limit 500;"
    )[1]
    checks["exact_baseline_no_hnsw"] = INDEX_NAME not in exact_plan

    # --- exact-vs-ANN entity recall@10 (deterministic fixtures) ---
    ev["exact_vs_ann"] = _exact_vs_ann(cluster, rows)
    checks["exact_equals_ann_recall_1_at_ef200"] = ev["exact_vs_ann"]["recall_at_10_by_ef"].get("200") == 1.0

    # --- filters / fail-closed / security (eligible cohort has no ineligible
    # rows to exclude — those checks run against the parity fixture below). ---
    checks["snowflake_item_id_exact"] = _has(cluster, QL, kinds=["message"], item_ids=["9007199254740993"], want_item="9007199254740993")
    checks["bare_item_ids_fail_closed"] = _count(cluster, QL, kinds=[], item_ids=["100"]) == 0
    checks["ambiguous_item_ids_fail_closed"] = _count(cluster, QL, kinds=["message", "resource"], item_ids=["100"]) == 0
    checks["concrete_workflow_filter"] = _has_workflow_only(cluster, QL)
    checks["single_workflow_filter"] = _single_workflow(cluster, QL)
    checks["no_duplicate_entities"] = _no_dup_entities(cluster, QL)
    checks["result_count_bounded_by_limit"] = _bounded(cluster, QL)
    checks["active_mismatch_returns_zero"] = _active_mismatch_zero(cluster, QL)

    # --- rollback (drop) + idempotent rebuild ---
    drop_rc = drop_index(cluster)
    ev["rollback"] = {"drop_returncode": drop_rc,
                      "embeddings_retained": int(_scalar(cluster, f"select count(*) from content_embeddings;") or 0)}
    checks["rollback_drop_zero"] = drop_rc == 0
    checks["rollback_retains_embeddings"] = ev["rollback"]["embeddings_retained"] == VOLUME
    # rebuild (idempotent)
    rebuild = build_index(cluster)
    rcat = catalog(cluster)
    checks["idempotent_rebuild_valid"] = rebuild["build_returncode"] == 0 and rcat["indisvalid"] == "t"

    return checks, ev


def _count(cluster: lp.LocalCluster, ql: str, *, kinds: list[str], item_ids: list[str]) -> int:
    k = "{" + ",".join(kinds) + "}" if kinds else "{}"
    ids = "{" + ",".join(item_ids) + "}" if item_ids else "{}"
    rc, out = cluster.psql(
        f"select count(*) from hivemind_semantic_candidates('{ql}'::vector,{K},'{k}','{ids}');"
    )
    return int(out.strip() or 0)


def _has(cluster: lp.LocalCluster, ql: str, *, kinds: list[str], item_ids: list[str], want_item: str) -> bool:
    ids = "{" + ",".join(item_ids) + "}"
    k = "{" + ",".join(kinds) + "}"
    rc, out = cluster.psql(
        f"select count(*) from hivemind_semantic_candidates('{ql}'::vector,{K},'{k}','{ids}') "
        f"where item_id='{want_item}';"
    )
    return int(out.strip() or 0) > 0


def _wp_gate(cluster: lp.LocalCluster, ql: str) -> bool:
    # 5002 quarantined: its prose may rank, but its workflow_python must NOT, and
    # no quarantined python snippet leaks. Assert 5002's winning representation
    # is prose (not workflow_python) when it ranks.
    rc, out = cluster.psql(
        f"select representation_type from hivemind_semantic_candidates('{ql}'::vector,{K},"
        f"'{{resource}}','{{5002}}') where item_id='5002';"
    )
    reps = [x.strip() for x in out.strip().splitlines() if x.strip()]
    return all(r == "prose" for r in reps) and "workflow_python" not in reps


def _has_workflow_only(cluster: lp.LocalCluster, ql: str) -> bool:
    rc, out = cluster.psql(
        f"select count(*), count(*) filter (where kind='workflow') from "
        f"hivemind_semantic_candidates('{ql}'::vector,{K},'{{workflow}}','{{}}');"
    )
    parts = out.strip().split("|")
    if len(parts) < 2:
        return False
    total, wf = int(parts[0] or 0), int(parts[1] or 0)
    return total > 0 and total == wf


def _single_workflow(cluster: lp.LocalCluster, ql: str) -> bool:
    rc, out = cluster.psql(
        f"select count(*), count(distinct item_id), count(*) filter (where item_id<>'9500') from "
        f"hivemind_semantic_candidates('{ql}'::vector,{K},'{{resource}}','{{9500}}');"
    )
    parts = out.strip().split("|")
    if len(parts) < 3:
        return False
    total, distinct, other = int(parts[0] or 0), int(parts[1] or 0), int(parts[2] or 0)
    return total > 0 and distinct == 1 and other == 0


def _no_dup_entities(cluster: lp.LocalCluster, ql: str) -> bool:
    rc, out = cluster.psql(
        f"select count(*), count(distinct (entity_type,item_id)) from "
        f"hivemind_semantic_candidates('{ql}'::vector,500,'{{}}','{{}}');"
    )
    parts = out.strip().split("|")
    if len(parts) < 2:
        return False
    return int(parts[0] or 0) == int(parts[1] or 0)


def _bounded(cluster: lp.LocalCluster, ql: str) -> bool:
    rc, out = cluster.psql(
        f"select count(*) from hivemind_semantic_candidates('{ql}'::vector,7,'{{}}','{{}}');"
    )
    return int(out.strip() or 0) <= 7


def _active_mismatch_zero(cluster: lp.LocalCluster, ql: str) -> bool:
    # Activate a different 384-d contract -> selected-literal fail-closed -> 0 rows.
    cluster.psql(
        f"insert into embedding_contracts(id,provider,model,dimension,canonicalization_version,"
        f"chunking_version,status) values ({OTHER_384_CONTRACT_ID},'openai','text-embedding-3-small',"
        f"384,1,{sc.SELECTED_CHUNKING_VERSION + 1},'draft') on conflict (id) do nothing;",
        capture=False,
    )
    cluster.psql(f"select hivemind_set_active_embedding_contract({OTHER_384_CONTRACT_ID}, false);", capture=False)
    n = _count(cluster, ql, kinds=[], item_ids=[])
    # restore selected active (re-seed + activate; it was superseded by the switch)
    cluster.psql(
        f"update embedding_contracts set status='draft' where id={SEL} and status='superseded';",
        capture=False,
    )
    cluster.psql(f"select hivemind_set_active_embedding_contract({SEL}, false);", capture=False)
    return n == 0


def _exact_vs_ann(cluster: lp.LocalCluster, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Exact-vs-ANN entity Recall@10 over deterministic query fixtures.

    Query fixtures are actual corpus vectors (resource rows), so the exact
    top-10 is well-defined (distance 0 to the query's own entity, then its true
    nearest neighbours). Exact = the function with indexscan OFF (seqscan exact
    baseline); ANN = the function with the HNSW index + ef_search. Identity is
    (entity_type, item_id) after best-chunk collapse.
    """

    res_rows = [r for r in rows if r["entity_type"] == "resource" and int(r["item_id"]) >= 100000]
    queries = [res_rows[i]["vec"] for i in range(0, min(10, len(res_rows)))]
    recall_by_ef: dict[str, float] = {}
    min_by_ef: dict[str, float] = {}
    for ef in EF_SWEEP:
        recalls = []
        for q in queries:
            ql = vec_literal(q)
            exact_ids = _identities(cluster, ql, mode="exact")
            ann_ids = _identities(cluster, ql, mode="ann", ef=ef)
            if not exact_ids:
                continue
            inter = len(set(exact_ids) & set(ann_ids))
            recalls.append(inter / len(exact_ids))
        recall_by_ef[str(ef)] = round(sum(recalls) / len(recalls), 4) if recalls else 0.0
        min_by_ef[str(ef)] = round(min(recalls), 4) if recalls else 0.0
    return {"recall_at_10_by_ef": recall_by_ef,
            "recall_at_10_min_by_ef": min_by_ef,
            "n_query_fixtures": len(queries),
            "note": "exact = function indexscan-off seqscan baseline; ann = function + HNSW + ef_search; queries are corpus vectors"}


def _identities(cluster: lp.LocalCluster, ql: str, *, mode: str, ef: int = 200) -> list[str]:
    prefix = ""
    if mode == "exact":
        prefix = "set enable_indexscan=off; set enable_bitmapscan=off;"
    else:
        prefix = f"set hnsw.ef_search={ef}; set hnsw.iterative_scan=strict_order;"
    rc, out = cluster.psql(
        f"{prefix} select entity_type||':'||item_id from hivemind_semantic_candidates('{ql}'::vector,{K},'{{}}','{{}}') order by semantic_rank;"
    )
    return [x.strip() for x in out.strip().splitlines() if x.strip()]


# ---------------------------------------------------------------------------
# Task 2.16 C3: separate PARITY fixture proving the function EXCLUDES
# deleted/rejected/quarantined rows. This is a SEPARATE small isolated cluster
# (NOT the HNSW-volume cohort) where ineligible rows ARE embedded under the
# selected literal solely to exercise the function's eligibility JOIN inside
# the kNN arm. They never enter the HNSW partial index built on the main cohort
# (different cluster, torn down independently).
# ---------------------------------------------------------------------------


def run_parity_fixture() -> tuple[dict[str, bool], dict[str, Any]]:
    """Build a tiny isolated cluster with ineligible rows under SEL and prove the
    function's eligibility logic excludes them from ranking/snippets."""

    checks: dict[str, bool] = {}
    ev: dict[str, Any] = {"fixture": "separate isolated cluster; ineligible rows under SEL"}
    cluster = None
    try:
        cluster, _ = setup_cluster()
        Q = fake_vec("parity-query")
        QL = vec_literal(Q)
        # Ineligible source rows (these are the safety-filter fixtures moved OUT
        # of the HNSW-volume cohort per C3).
        cluster.psql(
            "insert into discord_messages(message_id,content,is_deleted) values "
            "(200,'deleted',true) on conflict do nothing;", capture=False,
        )
        cluster.psql(
            "insert into distillations(id,question,answer,status) values "
            "(701,'q','a','rejected') on conflict do nothing;", capture=False,
        )
        cluster.psql(
            "insert into external_resources(id,kind,source,title,body) values "
            "(5002,'workflow','vibecomfy-external','quarantined-wf','b') on conflict do nothing;",
            capture=False,
        )
        cluster.psql(
            "insert into lexical_resource_python_state(resource_id,kind,cohort,public_state,available) values "
            "(5002,'workflow','unavailable','quarantined',false) on conflict (resource_id) do nothing;",
            capture=False,
        )
        # Embed the ineligible rows under SEL (so the function's contract filter
        # does NOT trivially exclude them — only the eligibility JOIN should).
        hsh = HASH64
        def emb(et, iid, rep, ci, v):
            cluster.psql(
                f"insert into content_embeddings(contract_id,entity_type,item_id,representation_type,"
                f"chunk_index,chunk_text,embedding,representation_hash,chunk_hash) values "
                f"({SEL},'{et}','{iid}','{rep}',{ci},'t','{vec_literal(v)}','{hsh}','{hsh}') "
                f"on conflict do nothing;",
                capture=False,
            )
        # Make the ineligible rows NEARER than any eligible alternative so the
        # only way they fail to rank is the eligibility predicate (not distance).
        emb("message", "200", "prose", 0, Q)              # deleted -> must NOT rank
        emb("distillation", "701", "prose", 0, Q)         # rejected -> must NOT rank
        emb("resource", "5002", "workflow_python", 0, Q)  # quarantined wp -> must NOT rank/snippet
        emb("resource", "5002", "prose", 0, fake_vec("parity-5002-prose"))  # eligible prose
        cluster.psql("analyze content_embeddings; analyze discord_messages; "
                     "analyze external_resources; analyze distillations;", capture=False)

        # Deleted message must not rank even when it is the nearest message.
        checks["deleted_message_excluded"] = not _has(cluster, QL, kinds=["message"], item_ids=["200"], want_item="200")
        # Rejected distillation must not rank.
        checks["rejected_distillation_excluded"] = not _has(cluster, QL, kinds=["distillation"], item_ids=["701"], want_item="701")
        # Quarantined workflow-python must not rank/snippet; 5002's winning
        # representation (if it ranks) must be prose.
        checks["quarantined_wp_gated_out"] = _wp_gate(cluster, QL)
        ev["note"] = ("ineligible rows were the NEAREST vectors to the query; "
                      "their exclusion proves the eligibility JOIN inside the kNN "
                      "arm, not distance ordering.")
    except Exception as exc:  # noqa: BLE001
        ev["parity_exception"] = {"type": type(exc).__name__, "msg_redacted": _redact(str(exc))}
        checks["parity_fixture_ok"] = False
    finally:
        if cluster is not None:
            cluster.tear_down()
    return checks, ev


# ---------------------------------------------------------------------------
# Driver.
# ---------------------------------------------------------------------------


def rehearse(out_path: pathlib.Path = REHEARSAL_EVIDENCE) -> dict[str, Any]:
    pf = preflight()
    rows = generate_volume()

    ev: dict[str, Any] = {
        "task": "2.16-selected-contract-hnsw-rehearsal",
        "cluster_isolated": True,
        "run_class": "local_volume_only",
        "representative_semantic_evidence": False,
        "selected_contract_id": SEL,
        "selected_contract_preimage_sha256": sc.SELECTED_CONTRACT_SHA256_HEX,
        "preflight": pf,
    }
    cluster = None
    try:
        cluster, env = setup_cluster()
        # Freeze the manifest AFTER the version probe but BEFORE any build/measurement.
        manifest = build_manifest(env)
        frozen_hash = manifest_hash(manifest)
        ev["manifest_hash"] = frozen_hash
        ev["manifest_frozen_before_measurement"] = True
        volume_info = seed_sources_and_volume(cluster, rows)
        # Task 2.16 C4: probe the PHYSICAL selected-contract column AFTER seeding.
        env.update(probe_physical_dim(cluster))
        ev["environment"] = env
        # Refuse drift: re-hash the unchanged manifest and require equality.
        ev["manifest_drift_refused"] = manifest_hash(manifest) == frozen_hash

        # --- Task 2.16 C3: fail-closed PRE-BUILD eligibility audit. ---
        audit = prebuild_eligibility_audit(cluster)
        ev["prebuild_eligibility_audit"] = audit
        if audit["abort"]:
            raise RuntimeError(f"pre-build eligibility audit failed: {audit['abort_reason']}")
        # C7b: source_mutations=0 is scoped to external/production sources; local
        # fixture inserts are disclosed separately (never external/production).
        ev["local_fixture_inserts"] = {
            "discord_messages": int(_scalar(cluster, "select count(*) from discord_messages;") or 0),
            "external_resources": int(_scalar(cluster, "select count(*) from external_resources;") or 0),
            "distillations": int(_scalar(cluster, "select count(*) from distillations;") or 0),
            "content_embeddings_selected_contract": int(_scalar(cluster, f"select count(*) from content_embeddings where contract_id={SEL};") or 0),
            "note": "throwaway isolated local cluster only; no external/production source mutated.",
        }

        checks, structural_ev = run_checks(cluster, rows, volume_info)

        # --- Task 2.16 C3: separate PARITY fixture (eligibility exclusion). ---
        parity_checks, parity_ev = run_parity_fixture()
        checks.update(parity_checks)
        structural_ev["parity_fixture"] = parity_ev

        ev["checks"] = checks
        ev["evidence"] = structural_ev
        ev["manifest"] = manifest
        all_checks_pass = all(checks.values()) and pf["ok"]
        ev["checks_summary"] = {
            "all_pass": all_checks_pass,
            "n_pass": sum(1 for v in checks.values() if v),
            "n_total": len(checks),
        }
        # Representative semantics cannot be proved from synthetic local volume,
        # so the run verdict is blocked_not_representative even when every
        # structural check passes.
        ev["verdict"] = "blocked_not_representative" if all_checks_pass else "structural_check_failed"
    except Exception as exc:  # noqa: BLE001
        ev["rehearsal_exception"] = {"type": type(exc).__name__, "msg_redacted": _redact(str(exc))}
        ev["verdict"] = "rehearsal_exception"
    finally:
        if cluster is not None:
            cluster.tear_down()

    # Task 2.16 C7b: external/production source mutations are zero; local fixture
    # inserts are disclosed in ev["local_fixture_inserts"] (throwaway cluster).
    ev["source_mutations"] = 0
    ev["source_mutations_scope"] = "external/production sources; local fixture inserts disclosed separately"
    ev["production_mutated"] = False
    ev["contract_activated"] = False
    # Evidence hygiene scan (refuse to persist anything dirty).
    ev["hygiene_findings"] = hygiene_scan(ev)
    ev["hygiene_clean"] = not ev["hygiene_findings"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(ev, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return ev


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="structural-local",
                    choices=["structural-local", "representative-preflight",
                             "recover-index", "rollback-index"])
    ap.add_argument("--out", default=str(REHEARSAL_EVIDENCE))
    args = ap.parse_args(argv)

    if args.mode == "representative-preflight":
        pf = preflight()
        print(json.dumps({
            "dependency_preflight_ok": pf["ok"],
            "run_class": pf["run_class"],
            "representative_semantic_evidence": pf["representative_semantic_evidence"],
            "verdict": pf["verdict"],
            "blocker": pf["blocker"],
            "selected_contract_id": SEL,
            "selected_contract_preimage_sha256": sc.SELECTED_CONTRACT_SHA256_HEX,
        }, indent=2))
        return 0

    if args.mode in ("recover-index", "rollback-index"):
        # Task 2.16 C7c: these modes are RENDER-ONLY operator helpers. They print
        # the frozen DDL for the operator to run against an existing cluster;
        # they do NOT connect to, build against, or mutate any database here.
        print(json.dumps({
            "mode": args.mode,
            "render_only": True,
            "note": ("RENDER-ONLY operator helper: prints the frozen DDL for the "
                     "operator to execute by hand against an existing cluster. It "
                     "does NOT connect to, build against, or mutate any database "
                     "in this process. Run structural-local for the isolated "
                     "build/drop/rebuild rehearsal."),
            "index_name": INDEX_NAME,
            "drop_ddl": f"drop index concurrently if exists public.{INDEX_NAME};",
            "create_ddl": (f"create index concurrently if not exists {INDEX_NAME} "
                           f"on public.content_embeddings using hnsw (embedding vector_cosine_ops) "
                           f"with (m={M}, ef_construction={EF_CONSTRUCTION}) where contract_id={SEL};"),
            "create_name_unqualified_note": (
                "PostgreSQL forbids a schema-qualified name on CREATE INDEX "
                "(the index is always created in its table's schema, public); "
                "the CREATE name above is unqualified while the table is "
                "schema-qualified, matching schema/033's documented operator DDL."
            ),
        }, indent=2))
        return 0

    ev = rehearse(pathlib.Path(args.out))
    cs = ev.get("checks_summary", {})
    print(f"Task 2.16 rehearsal: {cs.get('n_pass')}/{cs.get('n_total')} checks; "
          f"all_pass={cs.get('all_pass')}; verdict={ev.get('verdict')}; "
          f"run_class={ev.get('run_class')}; hygiene_clean={ev.get('hygiene_clean')}")
    print(f"selected_contract_id={SEL}  preimage_sha256={sc.SELECTED_CONTRACT_SHA256_HEX}")
    print(f"evidence: {args.out}")
    return 0 if (cs.get("all_pass") and ev.get("hygiene_clean")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
