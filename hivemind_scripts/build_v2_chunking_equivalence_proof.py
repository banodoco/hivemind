#!/usr/bin/env python3
"""Task-2.14/2.16 CHUNKING v2 — honest zero-call equivalence proof.

Propagates the bounded oversized workflow-Python fallback fix as CHUNKING
VERSION 2 and proves, with ZERO provider/network calls, that the v2 chunker is
*equivalent* to v1 over the accepted evaluation cohort:

  1. COHORT NO-OP: recompute the v2 chunk map for every candidate directly from
     the persisted raw reconstruction and prove it is byte-identical to the v1
     chunk map the immutable paid caches were built from (same multiset of
     (entity, representation, chunk_index, chunk_hash)). The v2 ``_line_window``
     bounding fix only changes pathological single over-long lines that no real
     cohort statement hits, so the real cohort is unchanged.
  2. ZERO CACHE MISS: every unique v2 chunk hash (and every golden query) is a
     key in the immutable paid cache for its dimension — i.e. every embedding
     the evaluation needs is already present, so the v2 evaluation makes zero
     provider calls.
  3. METRIC/WINNER EQUIVALENCE: recompute the four candidates' exact-cosine
     metrics from the cached vectors (cache-only replay, transport that raises
     on any call) and prove they reproduce the accepted eval3 metrics and the
     384-small winner.

HONEST LIMITATION (preserved verbatim from the v1 reconstruction): this proves a
deterministic cache-equivalent reconstruction. It cannot retroactively prove
equality to Task-2.14's *original* unpersisted per-entity mapping, because that
mapping was never persisted. What it DOES prove is that the v2 chunker produces
the same chunk maps the paid caches were built from, so the v2 evaluation is
zero-call and reproduces the accepted decision.

PRIVATE: raw/supporting evidence is written under
``.cache/hivemind-task-2.14-reconstruction/`` (parent 0700, files 0600). Only a
sanitized, credential/vector/raw-text-free summary is published under
``docs/hybrid-search/``. No provider call, no network read, no cache mutation.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
from typing import Any

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from eval.retrieval import semantic as sem  # noqa: E402
from eval.retrieval.loader import load_golden_set  # noqa: E402
from eval.retrieval.schema import CorpusItem  # noqa: E402
from executors import selected_contract as sc  # noqa: E402

PRIVATE_DIR = _REPO_ROOT / ".cache" / "hivemind-task-2.14-reconstruction"
RAW_SNAPSHOT = PRIVATE_DIR / "raw-reconstructed-snapshot.json"
V1_BINDING = PRIVATE_DIR / "reconstruction-binding.json"
V2_BINDING = PRIVATE_DIR / "v2-reconstruction-binding.json"
V2_PROOF = PRIVATE_DIR / "v2-cache-only-reconstruction-proof.json"

CACHE_DIR = _REPO_ROOT / ".cache" / "hivemind-semantic-eval"
CACHE_384 = CACHE_DIR / "cache.384.jsonl"
CACHE_1536 = CACHE_DIR / "cache.1536.jsonl"

DECISION_JSON = _REPO_ROOT / "docs" / "hybrid-search" / "task-2.14-embedding-decision.json"
GOLDEN_PATH = _REPO_ROOT / "eval" / "retrieval" / "golden" / "golden-v1.json"

TRACKED_SUMMARY = _REPO_ROOT / "docs" / "hybrid-search" / "task-2.14-v2-chunking-equivalence.json"

PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _sha256_json(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _ensure_private_dir(path: pathlib.Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777
    if mode != PRIVATE_DIR_MODE:
        os.chmod(path, PRIVATE_DIR_MODE)


def _atomic_write_private(path: pathlib.Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE_MODE)
    try:
        os.fchmod(fd, PRIVATE_FILE_MODE)
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    os.chmod(path, PRIVATE_FILE_MODE)


def _cache_facts(path: pathlib.Path, dim: int) -> dict[str, Any]:
    st = path.stat()
    return {
        "path": path.name,
        "dimension": dim,
        "sha256": _sha256_file(path),
        "byte_size": st.st_size,
        "record_count": sum(1 for line in open(path, encoding="utf-8") if line.strip()),
        "mode": oct(st.st_mode & 0o777),
        "mtime_ns": st.st_mtime_ns,
    }


def _cache_key_set(path: pathlib.Path) -> set[str]:
    keys: set[str] = set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                keys.add(json.loads(line)["key"])
            except (json.JSONDecodeError, KeyError):
                continue
    return keys


def _reconstruct_entities(snapshot: dict[str, Any]) -> list[sem.CohortEntity]:
    out: list[sem.CohortEntity] = []
    for e in snapshot["entities"]:
        ci = CorpusItem.from_dict(e["corpus_item"])
        out.append(
            sem.CohortEntity(
                entity_type=e["entity_type"],
                item_id=e["item_id"],
                corpus_item=ci,
                canon_row=e["canon_row"],
            )
        )
    return out


def _chunk_map_records(cohort: sem.ChunkedCohort) -> list[dict[str, Any]]:
    recs = []
    for ch in cohort.chunks:
        recs.append(
            {
                "entity_kind": ch.entity_kind,
                "item_id": ch.item_id,
                "representation_type": ch.representation_type,
                "chunk_index": ch.chunk_index,
                "chunk_hash": ch.chunk_hash,
                "representation_hash": ch.representation_hash,
            }
        )
    return recs


def _rounded_eq(a: Any, b: Any) -> bool:
    """Deep equality treating floats as equal when rounded to 6 dp (the sanitize
    precision the accepted decision was stored at, so a raw recompute matches)."""

    if isinstance(a, float) or isinstance(b, float):
        try:
            return round(float(a), 6) == round(float(b), 6)
        except (TypeError, ValueError):
            return False
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(_rounded_eq(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_rounded_eq(x, y) for x, y in zip(a, b))
    return a == b


def _main() -> int:
    _ensure_private_dir(PRIVATE_DIR)

    # ---- 0. paid-cache facts BEFORE (read-only; never mutated here) ---------
    cache_before = {
        "384": _cache_facts(CACHE_384, 384),
        "1536": _cache_facts(CACHE_1536, 1536),
    }

    raw_text = RAW_SNAPSHOT.read_text(encoding="utf-8")
    snapshot = json.loads(raw_text)
    raw_sha = _sha256_bytes(raw_text.encode("utf-8"))
    entities = _reconstruct_entities(snapshot)

    v1_binding = json.loads(V1_BINDING.read_text(encoding="utf-8"))
    decision = json.loads(DECISION_JSON.read_text(encoding="utf-8"))
    golden = load_golden_set(GOLDEN_PATH)

    cache_keys = {
        384: _cache_key_set(CACHE_384),
        1536: _cache_key_set(CACHE_1536),
    }

    per_candidate: dict[str, Any] = {}
    cohort_noop_all = True
    cache_miss_total = 0

    for cand in sem.CANDIDATES:
        name = cand.name
        dim = cand.dimension

        # ---- 1. recompute v2 cohort + chunk map directly from raw entities --
        cohort = sem.build_chunked_cohort(entities, cand)
        v2_records = _chunk_map_records(cohort)
        v2_unique = cohort.unique_texts  # chunk_hash -> text
        v2_multiset = sorted(
            tuple(sorted(r.items())) for r in v2_records
        )

        # ---- cohort no-op vs the v1 snapshot chunk map --------------------
        v1_pc = snapshot["per_candidate"][name]
        v1_records = v1_pc["chunk_map"]
        v1_unique = v1_pc["unique_texts"]            # chunk_hash -> normalized text
        v1_multiset = sorted(tuple(sorted(r.items())) for r in v1_records)

        records_identical = v2_multiset == v1_multiset
        # EXACT unique-text MAPPING equality (chunk_hash -> text), not merely
        # equal key sets: proves the v2 chunker reproduces the SAME normalized
        # text for every hash the v1 caches were built from. Key-set equality is
        # retained only as a weaker diagnostic; the binding/noop gate below keys
        # off the full mapping.
        unique_mapping_identical = v2_unique == v1_unique
        unique_keys_identical = set(v2_unique) == set(v1_unique)
        n_chunks_match = cohort.n_chunks == len(v1_records)
        n_unique_match = cohort.n_unique_embeddable_texts == len(v1_unique)
        # v2 chunk_map sha (serialized like the records the caches were built
        # from) and the unique-text MAPPING sha (chunk_hash -> text, NOT just
        # keys) must equal the v1 values computed from the persisted snapshot.
        v2_chunk_map_sha = _sha256_json(v2_records)
        v2_unique_texts_mapping_sha = _sha256_json(v2_unique)
        v2_unique_texts_keys_sha = _sha256_json(sorted(v2_unique.keys()))
        v1_unique_texts_mapping_sha = _sha256_json(v1_unique)
        noop_ok = (
            records_identical
            and unique_mapping_identical
            and n_chunks_match
            and n_unique_match
        )
        cohort_noop_all = cohort_noop_all and noop_ok

        # ---- 2. zero cache miss over v2 unique chunk hashes + golden queries
        prefix = f"{cand.provider}:{cand.model}:{dim}:"
        missing_chunks = [h for h in v2_unique if (prefix + h) not in cache_keys[dim]]
        norm_queries = []
        seen_q: set[str] = set()
        from executors import embedding_contract as ec  # local to avoid cost
        for case in golden.cases:
            nq = ec.normalize_query_for_embedding(case.query)
            if nq and nq not in seen_q:
                seen_q.add(nq)
                norm_queries.append(nq)
        missing_queries = [
            q for q in norm_queries if (prefix + ec.content_hash(q)) not in cache_keys[dim]
        ]
        cache_miss_total += len(missing_chunks) + len(missing_queries)

        per_candidate[name] = {
            "dimension": dim,
            "eval_contract_id": cand.eval_contract_id,
            "base_contract_id_dimension_only": cand.base_contract_id,
            "n_chunks": cohort.n_chunks,
            "n_unique_embeddable_texts": cohort.n_unique_embeddable_texts,
            "v2_chunk_map_sha256": v2_chunk_map_sha,
            "v2_unique_texts_mapping_sha256": v2_unique_texts_mapping_sha,
            "v1_unique_texts_mapping_sha256": v1_unique_texts_mapping_sha,
            "unique_texts_mapping_identical_to_v1": (
                v2_unique_texts_mapping_sha == v1_unique_texts_mapping_sha
            ),
            "v2_unique_texts_keys_sha256": v2_unique_texts_keys_sha,
            "v1_chunk_map_sha256": v1_binding["per_candidate_chunk_facts"][name]["chunk_map_sha256"],
            "v1_unique_texts_sha256": v1_binding["per_candidate_chunk_facts"][name]["unique_texts_sha256"],
            "v1_eval_contract_id": v1_binding["per_candidate_chunk_facts"][name]["eval_contract_id"],
            "cohort_chunk_map_identical_to_v1": records_identical,
            "unique_text_mapping_identical_to_v1": unique_mapping_identical,
            "unique_text_keys_identical_to_v1": unique_keys_identical,
            "n_chunks_match_v1": n_chunks_match,
            "n_unique_match_v1": n_unique_match,
            "cohort_noop": noop_ok,
            "cache_missing_chunk_hashes": len(missing_chunks),
            "cache_missing_query_hashes": len(missing_queries),
        }

    # ---- 3. cache-only metric recompute (zero provider calls) ---------------
    cache_by_dim = {
        384: sem.EmbeddingCache(CACHE_384, fail_closed=True),
        1536: sem.EmbeddingCache(CACHE_1536, fail_closed=True),
    }
    metrics_list: list[sem.CandidateMetrics] = []
    provider_transport_calls = 0
    for cand in sem.CANDIDATES:
        cohort = sem.build_chunked_cohort(entities, cand)
        client = sem.ProviderClient(
            candidate=cand,
            cache=cache_by_dim[cand.dimension],
            api_key=None,
            transport=sem._ReplayTransport(),
            replay_only=True,
        )
        m = sem.evaluate_candidate(cand, cohort, golden, client)
        provider_transport_calls += getattr(client, "requests", 0)
        metrics_list.append(m)

    winner = sem.select_winner(metrics_list)
    winner_name = winner.candidate.name if winner else None

    # ---- compare v2 metrics to the accepted eval3 decision metrics ---------
    dec_by_name = {c["candidate"]["name"]: c for c in decision["candidates"]}
    metric_equality: dict[str, Any] = {}
    metrics_all_equal = True
    for m in metrics_list:
        d = dec_by_name[m.candidate.name]
        fields = {
            "overall": _rounded_eq(m.overall, d["overall"]),
            "workflow_code_recall_at_10": _rounded_eq(m.workflow_code_recall_at_10, d["workflow_code_recall_at_10"]),
            "long_resource_chunk_recall_at_10": _rounded_eq(m.long_resource_chunk_recall_at_10, d["long_resource_chunk_recall_at_10"]),
            "by_entity_kind": _rounded_eq(m.by_entity_kind, d["by_entity_kind"]),
        }
        all_eq = all(fields.values())
        metrics_all_equal = metrics_all_equal and all_eq
        metric_equality[m.candidate.name] = {"all_equal": all_eq, "fields": fields}

    # ---- selected-contract bigints, derived DYNAMICALLY from the identity
    # helpers (never hardcoded): the CORRECT current literal plus the two it
    # must NOT be (the chunking-v1 historical literal, and the invalid
    # chunk_config v2 double-bump that this task corrects away).
    selected_contract_id_current = sc.SELECTED_CONTRACT_ID  # chunking v2 + cfg v1
    selected_contract_id_chunking_v1 = sc.selected_contract_id(
        sc.selected_contract_identity_input(chunking_version=1)
    )
    selected_contract_id_double_bump = sc.selected_contract_id(
        sc.selected_contract_identity_input(
            chunk_config_identity=sc.selected_chunk_config_identity(version=2)
        )
    )

    # ---- 4. paid-cache facts AFTER (must be unchanged) ---------------------
    cache_after = {
        "384": _cache_facts(CACHE_384, 384),
        "1536": _cache_facts(CACHE_1536, 1536),
    }
    caches_unchanged = cache_before == cache_after

    verdict = bool(
        cohort_noop_all
        and cache_miss_total == 0
        and metrics_all_equal
        and winner_name == "384-small"
        and provider_transport_calls == 0
        and caches_unchanged
    )

    v2_binding = {
        "kind": "task-2.14-cache-equivalent-reconstruction-v2",
        "version": 1,
        "chunking_version": sem.CHUNKING_VERSION,
        "chunk_config_identity_version": sem.CHUNK_CONFIG_IDENTITY_VERSION,
        "selected_contract_id": sc.SELECTED_CONTRACT_ID,
        "raw_frozen_snapshot": {
            "path": str(RAW_SNAPSHOT.relative_to(_REPO_ROOT)),
            "content_sha256": raw_sha,
            "n_entities": snapshot["n_entities"],
            "version": snapshot["version"],
        },
        "per_candidate": per_candidate,
        "cohort_noop_all": cohort_noop_all,
        "limitation": v1_binding["limitation"],
        "original_mapping_proven": False,
        "provider_calls": 0,
        "network_calls": 0,
        "source_mutations": 0,
    }

    v2_proof = {
        "kind": "task-2.14-cache-only-raw-reconstruction-proof-v2",
        "version": 1,
        "chunking_version": sem.CHUNKING_VERSION,
        "winner": winner_name,
        "winner_matches_eval3": winner_name == decision.get("winner", {}).get("name"),
        "metrics_all_equal_to_eval3": metrics_all_equal,
        "metric_equality": metric_equality,
        "cohort_noop_all": cohort_noop_all,
        "cache_misses": cache_miss_total,
        "provider_transport_calls": provider_transport_calls,
        "provider_calls": 0,
        "network_calls": 0,
        "paid_caches_unchanged": caches_unchanged,
        "paid_cache_sha256": {"384": cache_before["384"]["sha256"], "1536": cache_before["1536"]["sha256"]},
        "limitation": (
            "This proves exact cache-only metrics over the persisted cache-equivalent "
            "reconstruction under CHUNKING v2; it cannot retroactively prove equality to "
            "Task-2.14's original per-entity mapping because that original mapping was never "
            "persisted. Cohort no-op is proven directly: the v2 chunk map (recomputed from "
            "raw entities with the bounded _line_window fix) is byte-identical to the v1 "
            "chunk map the paid caches were built from, so the v2 evaluation is zero-call."
        ),
        "verdict": verdict,
    }

    _atomic_write_private(V2_BINDING, json.dumps(v2_binding, indent=2, sort_keys=True, ensure_ascii=False))
    _atomic_write_private(V2_PROOF, json.dumps(v2_proof, indent=2, sort_keys=True, ensure_ascii=False))

    # ---- sanitized tracked summary (no vectors/raw text/credentials) -------
    summary = {
        "kind": "task-2.14-v2-chunking-equivalence-summary",
        "chunking_version": sem.CHUNKING_VERSION,
        "chunk_config_identity_version": sem.CHUNK_CONFIG_IDENTITY_VERSION,
        "selected_contract_id": str(selected_contract_id_current),
        "selected_contract_identity_note": (
            "the CORRECT selected literal: chunking_version=2 (bounded oversized "
            "workflow-Python fallback) + chunk_config identity v1 (identity scheme "
            "unchanged). CHUNKING_VERSION=2; CHUNK_CONFIG_IDENTITY_VERSION=1."
        ),
        "historical_chunking_v1_selected_contract_id": str(selected_contract_id_chunking_v1),
        "invalid_double_bump_selected_contract_id": str(selected_contract_id_double_bump),
        "invalid_double_bump_note": (
            "the prior double-bump contract (chunk_config identity v2) is invalid "
            "and rejected by this correction; chunking behavior is v2 but the "
            "chunk-config identity scheme is v1."
        ),
        "cohort_noop_all": cohort_noop_all,
        "cohort_noop_detail": {
            n: {
                "cohort_chunk_map_identical_to_v1": per_candidate[n]["cohort_chunk_map_identical_to_v1"],
                "unique_text_mapping_identical_to_v1": per_candidate[n]["unique_text_mapping_identical_to_v1"],
                "unique_text_keys_identical_to_v1": per_candidate[n]["unique_text_keys_identical_to_v1"],
                "n_chunks": per_candidate[n]["n_chunks"],
                "n_unique_embeddable_texts": per_candidate[n]["n_unique_embeddable_texts"],
            }
            for n in per_candidate
        },
        "cache_misses": cache_miss_total,
        "provider_calls": 0,
        "network_calls": 0,
        "winner": winner_name,
        "winner_matches_eval3": winner_name == decision.get("winner", {}).get("name"),
        "metrics_all_equal_to_eval3": metrics_all_equal,
        "paid_caches_unchanged": caches_unchanged,
        "paid_cache_sha256": {"384": cache_before["384"]["sha256"], "1536": cache_before["1536"]["sha256"]},
        "limitation": v2_proof["limitation"],
        "verdict": verdict,
        "private_evidence": [
            str(V2_BINDING.relative_to(_REPO_ROOT)),
            str(V2_PROOF.relative_to(_REPO_ROOT)),
        ],
    }
    # hygiene: the tracked summary must carry no vectors/raw text/credentials.
    violations = sem.scan_envelope(summary)
    if violations:
        raise RuntimeError(f"tracked summary hygiene violations: {violations}")
    TRACKED_SUMMARY.write_text(
        json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    # ---- console verdict ---------------------------------------------------
    print("=== CHUNKING v2 zero-call equivalence proof ===")
    print(f"cohort_noop_all            : {cohort_noop_all}")
    for n, pc in per_candidate.items():
        print(
            f"  {n:11s}: chunks={pc['n_chunks']:6d} unique={pc['n_unique_embeddable_texts']:6d} "
            f"map_identical={pc['cohort_chunk_map_identical_to_v1']} "
            f"unique_identical={pc['unique_text_keys_identical_to_v1']} "
            f"miss_chunks={pc['cache_missing_chunk_hashes']} "
            f"miss_queries={pc['cache_missing_query_hashes']} "
            f"v1eval={pc['v1_eval_contract_id']} -> v2eval={pc['eval_contract_id']}"
        )
    print(f"cache_misses_total         : {cache_miss_total}")
    print(f"provider_transport_calls   : {provider_transport_calls}")
    print(f"metrics_all_equal_to_eval3 : {metrics_all_equal}")
    print(f"winner                     : {winner_name}")
    print(f"paid_caches_unchanged      : {caches_unchanged}")
    print(f"VERDICT                    : {'PASS' if verdict else 'FAIL'}")
    return 0 if verdict else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
