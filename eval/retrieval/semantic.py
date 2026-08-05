"""Evaluation-only exact-cosine semantic retriever (plan task 2.14).

This module is the **evaluation-only** half of the task-2.14 embedding
dimension / chunk-configuration comparison. It is deliberately *not* the
production semantic path (that is the SQL/Edge work of tasks 2.15–2.17 /
3.1–3.10, intentionally not built here). It exists to answer one question,
frozen in advance:

    "Of the four candidate (dimension × chunk-configuration) contracts, which
    one should production adopt — compared against the fixed golden set with
    exact cosine, a frozen cohort, and a frozen decision policy, with no
    post-hoc threshold changes?"

It reuses the **frozen** representation / chunking / hashing / vector-validation
contract verbatim and adds *no* second canonicalization or chunking algorithm:

* canonical representations: :mod:`executors.canonical_representations`
* chunking + best-chunk collapse: :mod:`executors.chunking`
* normalization / hashing / vector validation / contract identity:
  :mod:`executors.embedding_contract` (which re-exports the frozen
  :mod:`executors.workflow_reference`)
* golden harness: :mod:`eval.retrieval.runner`, :mod:`eval.retrieval.metrics`,
  :mod:`eval.retrieval.adapters` (filter semantics), :mod:`eval.retrieval.schema`

What this module owns that is *new* and evaluation-only:

1. **Candidate / chunk-configuration identity.** The production
   :class:`~executors.embedding_contract.ContractSpec` identity is
   ``provider + model + dimension + canonicalization_version + chunking_version``.
   Its integer id is therefore *dimension-only* for the pilot pair (both share
   canonicalization/chunking version 1) and cannot distinguish a small-chunk
   from a large-chunk configuration. This module extends that identity with an
   explicit chunk-configuration identity/version so the four-candidate grid is
   unambiguous, **without mutating the production contract** (the historical
   dimension-only ids are recorded as the base, not as sufficient identities).
2. **A resumable, private embedding cache** keyed by
   ``(provider, model, dimension, content_hash)`` — never raw text — that
   validates cached vectors on reuse, checkpoints atomically, resumes without
   duplicate provider calls, and supports a strict zero-provider-call replay.
3. **An exact-cosine retriever** (numpy when available) that selects the
   highest-scoring chunk per immutable entity across prose/workflow_python
   representations before global ranking, with deterministic tie-breaks and
   golden-filter respect.
4. **The frozen decision policy** (disqualification + lexicographic selection)
   and **sanitized evidence** (the envelope carries only versions / hashes /
   counts / aggregate metrics / sanitized cost+capacity accounting — never raw
   queries, bodies, workflow Python, vectors, credentials, or private URLs).

Pure stdlib + optional numpy. No import-time network. Ordinary use is offline;
provider calls happen only behind an explicit evaluation entry point.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import struct
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from eval.retrieval import adapters
from eval.retrieval.adapters import lexical_passes_filters
from eval.retrieval.schema import CorpusItem, GoldenCase, GoldenSet, Query, Result
from executors import canonical_representations as canon
from executors import chunking
from executors import embedding_contract as ec
from executors import entity_identity as ei
from executors import workflow_representation as wr
from executors.workflow_representation import (
    CANONICALIZATION_VERSION,
    CHUNKING_VERSION,
    REP_PROSE,
    REP_WORKFLOW_PYTHON,
)

# numpy is optional: the real ~40k-chunk cohort uses it for exact-cosine
# scoring; the tiny offline fixture works without it (pure-Python fallback).
try:  # pragma: no cover - import guard
    import numpy as _np  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - environment-dependent
    _np = None  # type: ignore[assignment]

__all__ = [
    "ChunkConfigSpec",
    "ChunkConfigIdentity",
    "Candidate",
    "CANDIDATES",
    "CAPACITY_FACTS",
    "PRICE_PER_1M_TOKENS_USD",
    "SPEND_CAP_USD",
    "STORAGE_GATE_GB",
    "CohortEntity",
    "EntityChunk",
    "ChunkedCohort",
    "build_chunked_cohort",
    "EmbeddingCache",
    "AggregateUsageGuard",
    "ProviderClient",
    "SemanticRetriever",
    "CandidateMetrics",
    "evaluate_candidate",
    "extract_metrics",
    "select_winner",
    "scan_envelope",
    "sanitize_report",
    "DEFAULT_CACHE_DIR",
    "LATER_CHUNK_UNAVAILABLE_REASON",
    "later_chunk_hit_rate_from_provenance",
    "golden_query_token_estimate",
    "standalone_cost_for_candidate",
    "ReplayMismatch",
    "candidate_grid_hash",
    "decision_policy_hash",
    "eval3_cohort_manifest_hash",
    "decision_deterministic_core",
    "decision_deterministic_core_hash",
    "cache_file_facts",
    "golden_file_facts",
    "metrics_from_envelope",
    "build_evidence_accounting",
    "build_replay_bundle",
    "build_frozen_manifest",
    "strict_offline_replay",
    "EVAL3_CACHE_RECORD_COUNT",
    "EVAL3_DIMENSIONS",
    "REPLAY_BUNDLE_VERSION",
    "GOLDEN_SHA256",
    "PRIVATE_FILE_MODE",
    "PRIVATE_DIR_MODE",
    "assert_private_file",
    "assert_private_dir",
    "resolve_private_path",
    "atomic_write_text",
    "evidence_pair_id",
    "verify_evidence_pair",
]


# ---------------------------------------------------------------------------
# Frozen decision constants
# ---------------------------------------------------------------------------

#: Fixed provider spend cap for the bounded real comparison (plan §budget).
SPEND_CAP_USD: float = 25.0

#: Fixed full-corpus vector-table + HNSW storage gate (plan task 0.7).
STORAGE_GATE_GB: float = 12.0

#: text-embedding-3-small list price (plan task 0.7 capacity assumptions). Same
#: per-token price regardless of the ``dimensions`` shortening parameter, so a
#: 384-d and a 1536-d embedding of the same input cost the same.
PRICE_PER_1M_TOKENS_USD: float = 0.02

#: Total attempts the provider call makes for one batch = 1 initial + retries.
#: Bounded so a flapping endpoint cannot loop indefinitely. Usage is recorded
#: ONLY for a response the provider actually returns (see
#: ``AggregateUsageGuard``); failed attempts report no usage and are not counted.
PROVIDER_MAX_RETRIES: int = 3
PROVIDER_MAX_ATTEMPTS: int = PROVIDER_MAX_RETRIES + 1

#: Capacity verdicts frozen from task 0.7 (full-corpus vector table + HNSW).
#: ``verdict`` is the historical task-0.7 PASS/FAIL against STORAGE_GATE_GB.
CAPACITY_FACTS: dict[int, dict[str, Any]] = {
    384: {"dimension": 384, "full_corpus_storage_gb": 4.59, "verdict": "PASS"},
    1536: {"dimension": 1536, "full_corpus_storage_gb": 16.4, "verdict": "FAIL"},
}

#: Default private (gitignored) cache directory. Overridable via CLI.
DEFAULT_CACHE_DIR = Path(".cache/hivemind-semantic-eval")

#: Fixed SHA-256 of the immutable golden file, bound as a literal so that any
#: drift in the actual golden file, the bundle's golden facts, or the manifest's
#: golden facts fails closed. This is the exact hash of
#: ``eval/retrieval/golden/golden-v1.json``.
GOLDEN_SHA256 = "d29eb3e1867c5721d662faacda3d684444efd25b9cb8194737c8b8fde135a9be"

#: Required file/parent modes for private (gitignored) artifacts under the cache
#: tree. Paid caches are already 0600 and are never chmod'd here; the private
#: replay bundle, raw frozen snapshot, and the cache parent dir are tightened to
#: these modes and verified fail-closed on load.
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700


# ---------------------------------------------------------------------------
# Candidate + chunk-configuration identity (evaluation contract)
# ---------------------------------------------------------------------------

#: Separator inside the chunk-config identity preimage (unit separator keeps
#: fields unambiguous, mirroring :func:`ec.contract_identity_input`).
_CFG_SEP = "\x1f"

#: Version of the *evaluation* chunk-configuration IDENTITY SCHEME. This tags
#: the scheme's axes (representation_type, target_tokens, overlap_tokens), NOT
#: the chunker algorithm. Bumping the scheme (e.g. adding a new identity axis)
#: is an evaluation-contract change and bumps THIS version.
#:
#: IMPORTANT — two distinct version axes; do NOT conflate them:
#:   * ``workflow_representation.CHUNKING_VERSION`` (== 2) governs the chunker
#:     ALGORITHM. It was bumped 1 -> 2 for the bounded oversized workflow-Python
#:     fallback fix (a behavioral change to how text is chunked).
#:   * ``CHUNK_CONFIG_IDENTITY_VERSION`` (== 1) governs the identity SCHEME.
#:     The fallback fix did NOT add, remove, or rename an identity axis, so the
#:     identity scheme is unchanged and THIS version STAYS 1. The chunker
#:     behavior moved to v2; the chunk-config identity scheme did not.
#:
#: Kept in lockstep with ``executors.selected_contract.CHUNK_CONFIG_IDENTITY_VERSION``
#: so the eval identity is byte-identical to the accepted selected-contract identity:
#: the same full preimage
#: (``<base contract w/ chunking_version=2>\\x1fchunk_config\\x1fv1\\x1fprose#512/50\\x1fworkflow_python#512/50``)
#: hashes to the same eval-contract id / selected bigint. The earlier code that
#: bumped this to 2 was an invalid *double* version bump; it is reverted here.
CHUNK_CONFIG_IDENTITY_VERSION = 1


@dataclass(frozen=True)
class ChunkConfigSpec:
    """One representation's chunk configuration (target/overlap in tokens)."""

    representation_type: str
    target_tokens: int
    overlap_tokens: int

    def __post_init__(self) -> None:
        if self.representation_type not in (REP_PROSE, REP_WORKFLOW_PYTHON):
            raise ValueError(
                f"representation_type must be {REP_PROSE!r} or {REP_WORKFLOW_PYTHON!r}"
            )
        if self.target_tokens < 1:
            raise ValueError("target_tokens must be positive")
        if self.overlap_tokens < 0 or self.overlap_tokens >= self.target_tokens:
            raise ValueError("overlap_tokens must be in [0, target_tokens)")

    @property
    def identity(self) -> str:
        return f"{self.representation_type}#{self.target_tokens}/{self.overlap_tokens}"


@dataclass(frozen=True)
class ChunkConfigIdentity:
    """The full chunk configuration for a candidate (prose + python)."""

    prose: ChunkConfigSpec
    python: ChunkConfigSpec
    version: int = CHUNK_CONFIG_IDENTITY_VERSION

    @property
    def identity(self) -> str:
        return _CFG_SEP.join(
            ("chunk_config", f"v{self.version}", self.prose.identity, self.python.identity)
        )


@dataclass(frozen=True)
class Candidate:
    """One cell of the frozen four-candidate grid (dimension × chunk config)."""

    name: str
    dimension: int
    prose_target: int
    prose_overlap: int
    python_target: int
    python_overlap: int

    def __post_init__(self) -> None:
        if self.dimension not in CAPACITY_FACTS:
            raise ValueError(f"dimension must be one of {sorted(CAPACITY_FACTS)}")

    @property
    def provider(self) -> str:
        return "openai"

    @property
    def model(self) -> str:
        return ec.DEFAULT_OPENAI_EMBEDDING_MODEL

    @property
    def prose_config(self) -> ChunkConfigSpec:
        return ChunkConfigSpec(REP_PROSE, self.prose_target, self.prose_overlap)

    @property
    def python_config(self) -> ChunkConfigSpec:
        return ChunkConfigSpec(REP_WORKFLOW_PYTHON, self.python_target, self.python_overlap)

    @property
    def chunk_config(self) -> ChunkConfigIdentity:
        return ChunkConfigIdentity(prose=self.prose_config, python=self.python_config)

    @property
    def base_contract_spec(self) -> ec.ContractSpec:
        """The production :class:`ContractSpec` (dimension-only for the pilot)."""

        return ec.ContractSpec(
            provider=self.provider,
            model=self.model,
            dimension=self.dimension,
        )

    @property
    def base_contract_id(self) -> int:
        """The historical dimension-only contract id (NOT a sufficient identity)."""

        return self.base_contract_spec.id

    @property
    def eval_contract_identity_input(self) -> str:
        """Frozen preimage: base contract identity + explicit chunk config.

        Reproducible byte-for-byte; hashed into :attr:`eval_contract_id`. The
        base input is the *exact* production :func:`ec.contract_identity_input`
        so the evaluation identity extends (never replaces) the production one.
        """

        return _CFG_SEP.join(
            (ec.contract_identity_input(self.base_contract_spec), self.chunk_config.identity)
        )

    @property
    def eval_contract_id(self) -> str:
        """Stable hex id distinguishing all four candidates (not a DB bigint)."""

        return hashlib.sha256(self.eval_contract_identity_input.encode("utf-8")).hexdigest()[:16]

    def to_sanitized_dict(self) -> dict[str, Any]:
        """Credential/vector/raw-text-free identity summary for evidence."""

        return {
            "name": self.name,
            "provider": self.provider,
            "model": self.model,
            "dimension": self.dimension,
            "canonicalization_version": CANONICALIZATION_VERSION,
            "chunking_version": CHUNKING_VERSION,
            "chunk_config_version": CHUNK_CONFIG_IDENTITY_VERSION,
            "prose": {"target_tokens": self.prose_target, "overlap_tokens": self.prose_overlap},
            "python": {"target_tokens": self.python_target, "overlap_tokens": self.python_overlap},
            "chunk_config_identity": self.chunk_config.identity,
            "base_contract_id_dimension_only": str(self.base_contract_id),
            "base_contract_id_dimension_only_note": (
                "historical dimension-only ContractSpec id; NOT sufficient to "
                "distinguish small vs large chunk configurations"
            ),
            "eval_contract_id": self.eval_contract_id,
        }


#: The frozen four-candidate grid. Order is the candidate *order*, which the
#: deterministic selection policy respects only as a final tie-break fallback.
CANDIDATES: tuple[Candidate, ...] = (
    Candidate("384-small", 384, 512, 50, 512, 50),
    Candidate("384-large", 384, 1024, 100, 2048, 100),
    Candidate("1536-small", 1536, 512, 50, 512, 50),
    Candidate("1536-large", 1536, 1024, 100, 2048, 100),
)


# ---------------------------------------------------------------------------
# Cohort entities + chunked cohort
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CohortEntity:
    """One immutable corpus entity: its CorpusItem (filters/identity) + raw row.

    ``canon_row`` is the row dict :func:`canon.build_representations` reads
    (title/body/payload/metadata/content/...). It is held in memory only to
    build representations and is never serialized into tracked evidence.
    """

    entity_type: str  # message | resource | distillation
    item_id: str
    corpus_item: CorpusItem
    canon_row: dict[str, Any]

    @property
    def entity_key(self) -> tuple[str, str]:
        return self.corpus_item.id.key()


@dataclass(frozen=True)
class EntityChunk:
    """One embedded chunk with its immutable identity + filterable parent.

    ``normalized_text`` is the frozen chunker output (already normalized); it is
    used only to look up / produce the embedding and is NEVER serialized into
    tracked evidence (only its ``chunk_hash`` is).
    """

    entity_kind: str
    item_id: str
    representation_type: str
    chunk_index: int
    chunk_hash: str
    representation_hash: str
    normalized_text: str
    parent: CorpusItem

    @property
    def entity_key(self) -> tuple[str, str]:
        return (self.entity_kind, self.item_id)


@dataclass
class ChunkedCohort:
    """A candidate's chunked cohort + sanitized counts for evidence."""

    candidate: Candidate
    chunks: list[EntityChunk] = field(default_factory=list)
    #: chunk_hash -> normalized_text (the unique texts that get embedded).
    unique_texts: dict[str, str] = field(default_factory=dict)
    #: entity_key -> set of representation types present (for the one-python rule).
    entity_reps: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    n_entities: int = 0
    n_duplicate_chunks_collapsed: int = 0

    @property
    def n_chunks(self) -> int:
        return len(self.chunks)

    @property
    def n_unique_embeddable_texts(self) -> int:
        return len(self.unique_texts)

    def sanitized_counts(self) -> dict[str, Any]:
        """Counts only — no text, no vectors, no hashes-of-private-content."""

        by_rep: dict[str, int] = {}
        max_chunk_index = 0
        multi_chunk_entities = 0
        for ch in self.chunks:
            by_rep[ch.representation_type] = by_rep.get(ch.representation_type, 0) + 1
            max_chunk_index = max(max_chunk_index, ch.chunk_index)
        # entities that have >1 chunk across any representation stream
        per_entity_chunk_count: dict[tuple[str, str], int] = {}
        for ch in self.chunks:
            per_entity_chunk_count[ch.entity_key] = per_entity_chunk_count.get(ch.entity_key, 0) + 1
        multi_chunk_entities = sum(1 for c in per_entity_chunk_count.values() if c > 1)
        return {
            "n_entities": self.n_entities,
            "n_chunks": self.n_chunks,
            "n_unique_embeddable_texts": self.n_unique_embeddable_texts,
            "n_duplicate_chunks_collapsed": self.n_duplicate_chunks_collapsed,
            "chunks_by_representation": by_rep,
            "max_chunk_index": max_chunk_index,
            "multi_chunk_entities": multi_chunk_entities,
        }


def build_chunked_cohort(entities: Iterable[CohortEntity], candidate: Candidate) -> ChunkedCohort:
    """Chunk every canonical representation of every entity under *candidate*.

    Reuses :func:`canon.build_representations` (frozen precedence / no-duplication
    / secret-quarantine) and :func:`chunking.chunk_representation` (frozen
    chunker). Identical chunk *texts* are collapsed to one embeddable text
    (keyed by ``chunk_hash``) so the same text is never embedded twice, while
    every (entity, representation, chunk_index) remains a distinct retrievable
    unit. Exactly one safe ``workflow_python`` stream per workflow at most is
    guaranteed structurally by the builder.
    """

    cohort = ChunkedCohort(candidate=candidate)
    seen_entity_keys: set[tuple[str, str]] = set()
    prose_cfg = candidate.prose_config
    python_cfg = candidate.python_config

    for ent in entities:
        reps = canon.build_representations(ent.canon_row, ent.entity_type, item_id=ent.item_id)
        if not reps:
            continue
        seen_entity_keys.add(ent.entity_key)
        rep_types: set[str] = set()
        for rep in reps:
            rep_types.add(rep.representation_type)
            cfg = prose_cfg if rep.representation_type == REP_PROSE else python_cfg
            cks = chunking.chunk_representation(
                rep, target_tokens=cfg.target_tokens, overlap_tokens=cfg.overlap_tokens
            )
            for ck in cks:
                cohort.chunks.append(
                    EntityChunk(
                        entity_kind=ent.corpus_item.entity_kind(),
                        item_id=ent.item_id,
                        representation_type=rep.representation_type,
                        chunk_index=ck.chunk_index,
                        chunk_hash=ck.chunk_hash,
                        representation_hash=ck.representation_hash,
                        normalized_text=ck.text,
                        parent=ent.corpus_item,
                    )
                )
        cohort.entity_reps[ent.entity_key] = rep_types

    cohort.n_entities = len(seen_entity_keys)
    # Collapse duplicate chunk texts (same normalized bytes) to one embeddable.
    unique: dict[str, str] = {}
    for ch in cohort.chunks:
        if ch.chunk_hash not in unique:
            unique[ch.chunk_hash] = ch.normalized_text
    cohort.unique_texts = unique
    cohort.n_duplicate_chunks_collapsed = cohort.n_chunks - cohort.n_unique_embeddable_texts
    return cohort


# ---------------------------------------------------------------------------
# Resumable private embedding cache
# ---------------------------------------------------------------------------


def _cache_key(provider: str, model: str, dimension: int, text_hash: str) -> str:
    return f"{provider}:{model}:{dimension}:{text_hash}"


def _pack_vector(vec: Sequence[float]) -> str:
    """Compact base64 of little-endian floats (halves the JSON size vs float text)."""

    return base64.b64encode(struct.pack(f"<{len(vec)}f", *vec)).decode("ascii")


def _unpack_vector(blob: str) -> list[float]:
    raw = base64.b64decode(blob)
    n = len(raw) // 4
    return list(struct.unpack(f"<{n}f", raw))


class EmbeddingCache:
    """Private, resumable embedding cache keyed by provider/model/dimension/hash.

    Storage is an append-only JSONL file under a private (gitignored) location.
    Each line is ``{"key":..., "d":dimension, "v":<base64 packed floats>}``.
    The file is created with mode 0600. Writes append one line per vector and
    are flushed+fsync'd so an interruption leaves at most a partial final line
    (skipped on load). A compacting rewrite can be triggered on close.

    Contract:

    * keys NEVER include raw text — only the content hash;
    * cached vectors are re-validated (dimension + finiteness) on load and on
      store (via :func:`ec.normalize_vector`), so a corrupted/wrong-dimension
      cached vector is never reused;
    * resume is automatic: missing keys are the cache misses a caller must fill;
    * strict replay (``fail_closed=True``) turns any miss into an error and the
      caller proves zero provider calls.
    """

    def __init__(self, path: str | Path, *, fail_closed: bool = False) -> None:
        self.path = Path(path)
        self.fail_closed = fail_closed
        self._store: dict[str, dict[str, Any]] = {}
        self._load()

    # -- load / persist ----------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    # Partial final line from an interrupted write: skip silently.
                    continue
                if not isinstance(rec, dict) or "key" not in rec:
                    continue
                self._store[rec["key"]] = rec

    def _append(self, rec: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(rec, separators=(",", ":"), ensure_ascii=False)
        # Append atomically: open for append, write, flush, fsync.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        # Tighten mode if the file pre-existed with a permissive mode.
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def compact(self) -> None:
        """Rewrite the cache as a single clean JSONL (dedup, last-wins)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            os.chmod(tmp, 0o600)
            for rec in self._store.values():
                fh.write(json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n")
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # -- access ------------------------------------------------------------

    def has(self, provider: str, model: str, dimension: int, text_hash: str) -> bool:
        return _cache_key(provider, model, dimension, text_hash) in self._store

    def get(
        self, provider: str, model: str, dimension: int, text_hash: str
    ) -> list[float] | None:
        """Return a re-validated cached vector, or ``None`` on a clean miss.

        A cached vector that fails dimension/finiteness validation is dropped
        (treated as a miss) rather than reused, so a corrupt entry can never
        poison an evaluation.
        """

        rec = self._store.get(_cache_key(provider, model, dimension, text_hash))
        if rec is None:
            return None
        try:
            vec = _unpack_vector(rec["v"])
            return ec.normalize_vector(vec, dimension=dimension)
        except (KeyError, ValueError, struct.error):
            # Corrupt or wrong-dimension: drop and miss.
            self._store.pop(_cache_key(provider, model, dimension, text_hash), None)
            return None

    def store(
        self,
        provider: str,
        model: str,
        dimension: int,
        text_hash: str,
        vector: Sequence[float],
    ) -> None:
        """Validate then atomically checkpoint a vector under its content key."""

        normalized = ec.normalize_vector(vector, dimension=dimension)
        rec = {
            "key": _cache_key(provider, model, dimension, text_hash),
            "d": dimension,
            "v": _pack_vector(normalized),
            "ts": int(time.time()),
        }
        self._store[rec["key"]] = rec
        self._append(rec)

    def stats(self) -> dict[str, int]:
        return {"entries": len(self._store)}


# ---------------------------------------------------------------------------
# Provider client (counting transport, batching, real cost accounting)
# ---------------------------------------------------------------------------


class _CountingTransport:
    """Wraps the real :func:`ec._stdlib_transport` to capture usage metadata.

    The credential is held only inside the :class:`ec.OpenAIEmbedder` and is
    never inspected here. We record only ``prompt_tokens`` + ``total_tokens`` +
    ``model`` + the input-item count — never the request body, key, or vectors.
    """

    def __init__(self, inner: ec.Transport) -> None:
        self._inner = inner
        self.calls = 0
        self.usage: list[dict[str, Any]] = []

    def __call__(self, url: str, headers: Mapping[str, str], body: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls += 1
        resp = self._inner(url, headers, body)
        if isinstance(resp, Mapping):
            u = resp.get("usage") or {}
            self.usage.append(
                {
                    "prompt_tokens": int(u.get("prompt_tokens", 0) or 0),
                    "total_tokens": int(u.get("total_tokens", 0) or 0),
                    "input_items": len(body.get("input", [])) if isinstance(body.get("input"), list) else 0,
                    "model": body.get("model"),
                }
            )
        return resp


class _ReplayTransport:
    """A transport that must never be called in strict replay mode."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, url: str, headers: Mapping[str, str], body: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls += 1
        raise ec.EmbeddingError("provider call attempted in cache-only replay mode")


class AggregateUsageGuard:
    """Shared cross-candidate actual-usage guard (the hard aggregate spend cap).

    The per-client ``cost_cap_usd`` preflight resets per :class:`ProviderClient`
    (one client per candidate), so once an earlier candidate's client has
    consumed budget a later candidate's client starts fresh and can overspend.
    This guard is created ONCE by ``_run_all_candidates`` and shared by every
    real-path client, so the cap never resets between candidates or dimensions.

    Before every provider request the client calls :meth:`gate` with the next
    conservative *estimated* batch token count; the guard projects
    ``actual_tokens_already_reported + next_estimated_batch`` and raises if that
    exceeds the cap — BEFORE the transport is invoked. After each successful
    batch the client records the newly reported actual usage :meth:`record_exactly_once`
    exactly once (via a monotonic per-client offset over the persistent usage
    counter, which accumulates ONLY successful-response prompt_tokens), so no
    usage *record* is counted twice. HONESTLY: usage is recorded ONLY for a
    response the provider actually returned; a failed attempt (transport error /
    HTTP 429 / 5xx) raises before reporting usage, so any tokens billed for a
    failed attempt are UNAVAILABLE and NOT counted. The recorded aggregate is
    therefore the sum of successful-response usage only and is NOT proof against
    billed-but-unreported failed attempts. Offline/fake embedders never touch
    this guard and are never blocked (zero cost).
    """

    def __init__(self, cap_usd: float = SPEND_CAP_USD) -> None:
        self.cap_usd = float(cap_usd)
        #: Aggregate actual prompt tokens reported across ALL clients/candidates.
        self.actual_tokens: int = 0

    def gate(self, next_estimated_batch_tokens: int) -> None:
        """Raise if aggregate actual usage + next estimated batch exceeds cap.

        This is the DISTINCT runtime gate from the preflight *estimate* gate:
        preflight checks a projected aggregate estimate once before any call;
        this guard checks (actual usage already reported across prior + current
        clients) + (the next conservative estimated batch) before every request.
        """

        projected = self.actual_tokens + max(0, int(next_estimated_batch_tokens))
        projected_cost = projected / 1_000_000.0 * PRICE_PER_1M_TOKENS_USD
        if projected_cost > self.cap_usd:
            raise ec.EmbeddingError(
                f"aggregate actual+estimated spend ${projected_cost:.4f} exceeds "
                f"hard cap ${self.cap_usd:.2f} (actual_tokens={self.actual_tokens}, "
                f"next_estimated_batch={next_estimated_batch_tokens})"
            )

    def record_exactly_once(self, delta_tokens: int) -> None:
        """Record a batch's newly reported actual usage exactly once.

        Callers pass a monotonic per-client delta (current reported usage minus
        the last-reported offset), so no usage record is double-counted. The
        delta is over the persistent usage counter, which accumulates ONLY
        successful-response prompt_tokens: a failed attempt reports no usage and
        is NOT counted (any tokens a provider bills for a failed attempt are
        unavailable), so the recorded aggregate is the sum of successful-response
        usage only.
        """

        self.actual_tokens += max(0, int(delta_tokens))


@dataclass
class ProviderClient:
    """Drives batching + caching around the real :class:`ec.OpenAIEmbedder`.

    ``embed_texts`` returns vectors in input order, hitting the cache first and
    only calling the provider for the missing keys (batched by item count and a
    token budget). ``replay_only=True`` fails closed on any miss and uses a
    transport that raises if ever invoked, so zero provider calls are provable.
    """

    candidate: Candidate
    cache: EmbeddingCache
    api_key: str | None
    transport: ec.Transport | None = None
    replay_only: bool = False
    max_items_per_request: int = 2048
    max_tokens_per_request: int = 100000
    max_retries: int = PROVIDER_MAX_RETRIES
    cost_cap_usd: float = SPEND_CAP_USD
    requests: int = 0
    embedded_inputs: int = 0
    input_tokens: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    #: Shared cross-candidate aggregate actual-usage guard (gap 4). When bound
    #: (set once by ``_run_all_candidates`` for every real-path client) the hard
    #: cap does NOT reset between candidates/dimensions. ``None`` for offline
    #: fake-embedder clients (zero cost, never gated) and for standalone use
    #: (the per-client ``cost_cap_usd`` preflight then applies).
    aggregate_guard: Any = None
    #: Monotonic per-client offset of usage already reported to the aggregate
    #: guard, so each SUCCESSFUL batch records its delta exactly once (no usage
    #: record is double-counted). Failed attempts report no usage and are not
    #: counted; only successful-response usage is recorded.
    _last_reported_tokens: int = 0
    #: Persistent usage counter for the real path. Created once per client (one
    #: client per candidate) and reused across every ``embed_texts`` call so the
    #: reported ``input_tokens`` / ``api_requests`` reflect the TRUE total for
    #: that candidate, not just the last batch.
    _counter: Any = None
    #: Injectable offline embedder (e.g. DeterministicFakeEmbedder). When set,
    #: misses are served by it with NO network, NO provider, and zero cost — the
    #: offline fixture path. When None, the real OpenAI embedder is used.
    embed_fn: Callable[[Sequence[str]], "list[list[float]]"] | None = None

    def _text_hash(self, text: str) -> str:
        return ec.content_hash(text)

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one validated vector per text, in order (cache-first)."""
        if not texts:
            return []
        hashes = [self._text_hash(t) for t in texts]
        results: list[list[float] | None] = [None] * len(texts)
        # Cache pass.
        miss_indices: list[int] = []
        for i, h in enumerate(hashes):
            cached = self.cache.get(self.candidate.provider, self.candidate.model, self.candidate.dimension, h)
            if cached is not None:
                results[i] = cached
                self.cache_hits += 1
            else:
                self.cache_misses += 1
                miss_indices.append(i)

        if miss_indices:
            self._embed_misses(texts, hashes, results, miss_indices)
        # All slots filled (or raised).
        return [v for v in results if v is not None]  # type: ignore[list-item]

    def _embed_misses(
        self,
        texts: Sequence[str],
        hashes: list[str],
        results: list[list[float] | None],
        miss_indices: list[int],
    ) -> None:
        if self.replay_only:
            raise ec.EmbeddingError(
                f"cache-only replay miss for {len(miss_indices)} text(s); "
                "replay requires a complete cache"
            )
        # Batch misses by item count + estimated token budget.
        batch: list[int] = []
        batch_tokens = 0
        flushed: list[list[int]] = []

        def flush(batch_idx: list[int]) -> None:
            if batch_idx:
                flushed.append(batch_idx[:])

        for idx in miss_indices:
            est_tok = max(1, wr.estimate_tokens(texts[idx]))
            would_items = len(batch) + 1
            would_tokens = batch_tokens + est_tok
            if batch and (
                would_items > self.max_items_per_request or would_tokens > self.max_tokens_per_request
            ):
                flush(batch)
                batch = []
                batch_tokens = 0
            batch.append(idx)
            batch_tokens += est_tok
        flush(batch)

        embedder = self._make_embedder() if self.embed_fn is None else None
        import asyncio

        for batch_idx in flushed:
            batch_texts = [texts[i] for i in batch_idx]
            batch_hashes = [hashes[i] for i in batch_idx]
            # Spend gate (skipped for offline embed_fn — zero cost, never blocked).
            # Two distinct gates, kept separate in the evidence:
            #  * the shared aggregate actual-usage guard (gap 4): does NOT reset
            #    between candidates/dimensions; gates BEFORE the transport on
            #    (aggregate actual usage already reported) + (next est. batch);
            #  * the per-client preflight (standalone fallback when no shared
            #    guard is bound): gates on this client's accumulated actual usage
            #    + the next estimated batch against ``cost_cap_usd``.
            if self.embed_fn is None:
                est_batch_tokens = sum(max(1, wr.estimate_tokens(t)) for t in batch_texts)
                if self.aggregate_guard is not None:
                    # Cross-candidate hard cap; raises BEFORE the transport call.
                    self.aggregate_guard.gate(est_batch_tokens)
                else:
                    # Standalone per-client preflight (resets per client).
                    projected_tokens = self._usage_tokens() + est_batch_tokens
                    projected_cost = projected_tokens / 1_000_000.0 * PRICE_PER_1M_TOKENS_USD
                    if projected_cost > self.cost_cap_usd:
                        raise ec.EmbeddingError(
                            f"projected spend ${projected_cost:.4f} exceeds cap ${self.cost_cap_usd:.2f}"
                        )
            if self.embed_fn is not None:
                vectors = self.embed_fn(batch_texts)
            else:
                vectors = self._embed_with_retries(embedder, batch_texts)
            if len(vectors) != len(batch_texts):
                raise ec.EmbeddingError(
                    f"provider returned {len(vectors)} vectors for {len(batch_texts)} inputs"
                )
            for h, vec in zip(batch_hashes, vectors):
                self.cache.store(
                    self.candidate.provider, self.candidate.model, self.candidate.dimension, h, vec
                )
            for offset, i in enumerate(batch_idx):
                results[i] = vectors[offset]
            self.requests += 1
            self.embedded_inputs += len(batch_texts)
            # Record this batch's newly reported actual usage to the shared
            # aggregate guard exactly once (monotonic per-client delta over the
            # persistent usage counter, which holds ONLY successful-response
            # prompt_tokens). Failed attempts within ``_embed_with_retries``
            # report no usage and are NOT counted; no usage record is double-
            # counted.
            if self.embed_fn is None and self.aggregate_guard is not None:
                now = self._usage_tokens()
                self.aggregate_guard.record_exactly_once(now - self._last_reported_tokens)
                self._last_reported_tokens = now

    def _embed_with_retries(self, embedder: ec.OpenAIEmbedder, batch_texts: list[str]) -> list[list[float]]:
        """Call the provider with bounded retry on transient transport / rate errors.

        Retries only on transport errors (URLError/Timeout/OSError) and HTTP
        429/5xx; a 4xx (bad request) surfaces immediately. The cache makes a
        batch idempotent, so a retry after a partial/failed response is safe.
        """

        import asyncio

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return asyncio.run(embedder.embed_texts(batch_texts))
            except ec.EmbeddingError as exc:
                last_exc = exc
                msg = str(exc)
                retryable = (
                    "transport error" in msg
                    or "HTTP 429" in msg
                    or "HTTP 500" in msg
                    or "HTTP 502" in msg
                    or "HTTP 503" in msg
                    or "HTTP 504" in msg
                )
                if not retryable or attempt == self.max_retries:
                    raise
                time.sleep(2 ** attempt)  # exponential backoff: 1s, 2s, 4s ...
        assert last_exc is not None
        raise last_exc

    def _make_embedder(self) -> ec.OpenAIEmbedder:
        if self.transport is not None:
            inner = self.transport
        else:
            inner = ec._stdlib_transport
        # Reuse one persistent counter per client so usage accumulates across
        # every embed_texts call (chunks + queries) for an honest true total.
        if self._counter is None:
            self._counter = _CountingTransport(inner)
        else:
            self._counter._inner = inner
        return ec.OpenAIEmbedder(
            api_key=self.api_key,
            model_name=self.candidate.model,
            dimension=self.candidate.dimension,
            transport=self._counter,
        )

    def cost_usd(self) -> float:
        """Cost from real provider ``usage.prompt_tokens`` (true total per client)."""

        return self._usage_tokens() / 1_000_000.0 * PRICE_PER_1M_TOKENS_USD

    def _usage_tokens(self) -> int:
        if self._counter is None:
            return 0
        return sum(u["prompt_tokens"] for u in self._counter.usage)

    def account(self) -> dict[str, Any]:
        api_requests = self._counter.calls if self._counter is not None else 0
        usage_tokens = self._usage_tokens()
        return {
            "provider": self.candidate.provider,
            "model": self.candidate.model,
            "dimension": self.candidate.dimension,
            "api_requests": api_requests,
            "embedded_inputs": self.embedded_inputs,
            "input_tokens_from_usage": usage_tokens,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "price_per_1m_tokens_usd": PRICE_PER_1M_TOKENS_USD,
            "cost_usd": round(self.cost_usd(), 6),
        }


# Patch: capture usage into the account. We attach the counter after embed runs.
def _finalize_account(client: ProviderClient) -> None:
    counter = getattr(client, "_counter", None)
    if counter is None:
        return
    client.requests = max(client.requests, counter.calls)
    client.input_tokens = sum(u["prompt_tokens"] for u in counter.usage)


# ---------------------------------------------------------------------------
# Exact-cosine retriever (best chunk per entity, then global rank)
# ---------------------------------------------------------------------------


class SemanticRetriever:
    """Evaluation-only exact-cosine retriever for one candidate.

    Scores every chunk whose parent passes the golden filters, selects the
    single best chunk per immutable ``(entity_kind, item_id)`` across
    prose/workflow_python representations with the frozen tie-break, then ranks
    globally by score desc + stable entity-key asc. Query vectors are looked up
    from a precomputed map (the caller embeds queries through the same cache).
    """

    name = "semantic"

    def __init__(
        self,
        cohort: ChunkedCohort,
        vectors_by_hash: Mapping[str, Sequence[float]],
        query_vectors_by_normtext: Mapping[str, Sequence[float]],
        *,
        dimension: int,
    ) -> None:
        self.candidate = cohort.candidate
        self.dimension = dimension
        self._query_vectors = dict(query_vectors_by_normtext)
        # Build the chunk matrix (numpy if available) + parallel metadata.
        self._chunks = list(cohort.chunks)
        self._vectors: list[list[float]] = [list(vectors_by_hash[ch.chunk_hash]) for ch in self._chunks]
        self._matrix = None
        if _np is not None and self._vectors:
            self._matrix = _np.asarray(self._vectors, dtype=_np.float32)

    # -- scoring helpers ---------------------------------------------------

    def _scores_for(self, qvec: Sequence[float]) -> list[float]:
        if self._matrix is not None:
            q = _np.asarray(qvec, dtype=_np.float32)
            return (self._matrix @ q).tolist()
        qv = list(qvec)
        return [sum(a * b for a, b in zip(qv, v)) for v in self._vectors]

    @staticmethod
    def _collapse_ranked(
        chunks: list[EntityChunk], scores: list[float], limit: int
    ) -> list[Result]:
        """Best chunk per entity (frozen tie-break) then global deterministic rank."""

        best: dict[tuple[str, str], tuple[tuple[float, int, int], EntityChunk, float]] = {}
        for ch, score in zip(chunks, scores):
            # Frozen tie-break: higher score wins; prose before python; lower chunk_index.
            rank_key = (-score, 0 if ch.representation_type == REP_PROSE else 1, ch.chunk_index)
            cur = best.get(ch.entity_key)
            if cur is None or rank_key < cur[0]:
                best[ch.entity_key] = (rank_key, ch, score)
        # Global rank: score desc, then (entity_kind, item_id) asc.
        ordered = sorted(
            best.values(), key=lambda t: (-t[2], t[1].entity_kind, t[1].item_id)
        )
        out: list[Result] = []
        for _rank, ch, score in ordered[:limit]:
            out.append(
                Result(
                    kind=ch.entity_kind,
                    item_id=ch.item_id,
                    score=float(score),
                    matched_representation=ch.representation_type,
                    matched_chunk_index=ch.chunk_index,
                )
            )
        return out

    # -- Retriever protocol ------------------------------------------------

    def retrieve(self, query: Query) -> list[Result]:
        norm_q = ec.normalize_query_for_embedding(query.query)
        qvec = self._query_vectors.get(norm_q)
        if qvec is None:
            # A query with no precomputed vector yields no semantic hits (the
            # caller is responsible for embedding every golden query up front).
            return []
        scores = self._scores_for(qvec)
        # Filter + collect, preserving index alignment.
        kept_chunks: list[EntityChunk] = []
        kept_scores: list[float] = []
        for ch, score in zip(self._chunks, scores):
            if not lexical_passes_filters(ch.parent, query.filters or {}):
                continue
            kept_chunks.append(ch)
            kept_scores.append(score)
        if not kept_chunks:
            return []
        return self._collapse_ranked(kept_chunks, kept_scores, query.limit)


# ---------------------------------------------------------------------------
# Metrics extraction + frozen decision policy
# ---------------------------------------------------------------------------


@dataclass
class CandidateMetrics:
    """Sanitized metrics for one candidate against the frozen golden set."""

    candidate: Candidate
    overall: dict[str, Any]
    by_category: dict[str, dict[str, Any]]
    by_entity_kind: dict[str, dict[str, Any]]
    workflow_code_recall_at_10: float
    long_resource_chunk_recall_at_10: float
    #: Later-chunk hit rate; ``None`` when per-chunk provenance was not persisted
    #: (see :attr:`later_chunk_diagnostic`). Never fabricated.
    later_chunk_hit_rate: float | None
    #: Structured later-chunk provenance diagnostic (available or unavailable).
    later_chunk_diagnostic: dict[str, Any]
    counts: dict[str, Any]
    cohort_counts: dict[str, Any]
    account: dict[str, Any]
    latency_ms: dict[str, float]
    #: Deterministic order-independent standalone cost used as the cost axis of
    #: the frozen tiebreak. ``None`` falls back to ``account.cost_usd`` (the
    #: eval3 incremental cost) so older call sites keep working.
    selection_cost_usd: float | None = None
    status: str = "evaluated"
    disqualify_reasons: list[str] = field(default_factory=list)
    capacity_fail: bool = False
    eligible_for_selection: bool = True
    # The full per_case is kept out of the sanitized envelope by default; it is
    # available for the report builder but carries only ids/keys/metrics.

    def selection_key(self) -> tuple[float, float, float, float, float, float]:
        """Lexicographic key for the frozen deterministic selection policy.

        Higher is better for recall/mrr/ndcg/workflow_code/long_resource_chunk;
        lower **standalone** cost wins ties (never the order-dependent
        incremental cache-miss cost). Returns a tuple sorted ascending by
        Python's ``sorted``.
        """

        if self.selection_cost_usd is not None:
            cost = float(self.selection_cost_usd)
        else:
            cost = float(self.account.get("cost_usd", 0.0))
        return (
            -float(self.overall.get("recall@10", 0.0)),
            -float(self.overall.get("mrr", 0.0)),
            -float(self.overall.get("ndcg@10", 0.0)),
            -float(self.workflow_code_recall_at_10),
            -float(self.long_resource_chunk_recall_at_10),
            cost,
        )


def _recall_at_10_for_cases(per_case: list[dict[str, Any]], predicate: Callable[[dict[str, Any]], bool]) -> tuple[float, int]:
    rows = [c for c in per_case if c.get("is_judged") and predicate(c)]
    if not rows:
        return 0.0, 0
    vals = [c.get("recall_at_10", 0.0) for c in rows]
    return sum(vals) / len(vals), len(rows)


#: Reason recorded when later-chunk hit rate cannot be computed because the
#: frozen eval3 per-case rankings persist only entity keys, not chunk provenance.
LATER_CHUNK_UNAVAILABLE_REASON = "raw_eval3_ranking_provenance_not_persisted"


def later_chunk_hit_rate_from_provenance(top_chunk_indices: Sequence[int | None]) -> tuple[float, int]:
    """Honest later-chunk hit rate from persisted top-1 chunk indices.

    ``top_chunk_indices`` is the top-1 ``matched_chunk_index`` for each judged
    case that produced at least one hit (``None`` entries are ignored). Returns
    ``(rate, n)`` where *rate* is the fraction whose winning chunk was a
    *later* chunk (``chunk_index > 0``) and *n* is the number of cases scored.
    Used by future raw-snapshot evaluations that persist chunk provenance.
    """

    vals = [i for i in top_chunk_indices if i is not None]
    if not vals:
        return 0.0, 0
    hits = sum(1 for i in vals if i > 0)
    return hits / len(vals), len(vals)


def extract_metrics(report: Any, cohort: ChunkedCohort, retriever: SemanticRetriever) -> tuple[dict[str, Any], dict[str, Any]]:
    """Derive workflow_code / long_resource_chunk metrics + a later-chunk diagnostic.

    ``report`` is a :class:`eval.retrieval.runner.SystemReport` (or its dict).
    Returns ``(extra_metrics, later_chunk_diagnostic)``.

    The later-chunk hit rate is computed **only** when the per-case rankings
    carry persisted chunk provenance (``ranked_chunk_index``). The frozen eval3
    harness persisted only entity keys, so eval3 yields an explicit *unavailable*
    diagnostic — it is never fabricated as 0.0. ``long_resource_chunk_recall_at_10``
    (the actual frozen selection signal) is always computed and unaffected.
    """

    per_case = report.per_case if hasattr(report, "per_case") else report["per_case"]
    wf_r10, wf_n = _recall_at_10_for_cases(
        per_case, lambda c: "workflow_code" in (c.get("categories") or [])
    )
    lrc_r10, lrc_n = _recall_at_10_for_cases(
        per_case, lambda c: "long_resource_chunk" in (c.get("categories") or [])
    )

    # Later-chunk provenance: only computable when rankings carry a REAL
    # (non-None) top-1 chunk index. A list of all-None (an adapter that did not
    # set matched_chunk_index, or a fabricated report) is treated as no
    # provenance, never as a 0.0 hit rate.
    top_indices: list[int | None] = []
    saw_provenance = False
    for c in per_case:
        if not c.get("is_judged"):
            continue
        ranked_chunks = c.get("ranked_chunk_index")
        if ranked_chunks and ranked_chunks[0] is not None:
            saw_provenance = True
            top_indices.append(ranked_chunks[0])
    if saw_provenance:
        rate_val, n_with_hit = later_chunk_hit_rate_from_provenance(top_indices)
        later_diag: dict[str, Any] = {
            "available": True,
            "later_chunk_hit_rate": round(rate_val, 6),
            "n_judged_with_hit": n_with_hit,
            "definition": (
                "fraction of judged cases with >=1 hit whose top-1 result is a "
                "non-zero chunk_index (a later chunk won best-chunk collapse)"
            ),
        }
    else:
        later_diag = {
            "available": False,
            "reason": LATER_CHUNK_UNAVAILABLE_REASON,
            "later_chunk_hit_rate": None,
            "note": (
                "eval3 per-case rankings persist only entity keys "
                "(entity_kind, item_id), not matched_chunk_index, so the "
                "later-chunk hit rate cannot be recomputed. "
                "long_resource_chunk_recall_at_10 remains the frozen selection "
                "signal and is unaffected."
            ),
        }

    extra: dict[str, Any] = {
        "workflow_code_recall_at_10": round(wf_r10, 6),
        "workflow_code_n": wf_n,
        "long_resource_chunk_recall_at_10": round(lrc_r10, 6),
        "long_resource_chunk_n": lrc_n,
        "later_chunk_diagnostic": later_diag,
    }
    return extra, later_diag


def select_winner(metrics: list[CandidateMetrics]) -> CandidateMetrics | None:
    """Apply the frozen deterministic selection policy.

    Eligibility: no disqualifying reason AND not capacity-disqualified. The
    winner is the eligible candidate with the lexicographically smallest
    :meth:`CandidateMetrics.selection_key` (recall@10 → mrr → nDCG@10 →
    workflow_code recall@10 → later-chunk recall@10 → lower cost). Returns
    ``None`` if no candidate is eligible.
    """

    eligible = [m for m in metrics if m.eligible_for_selection and not m.disqualify_reasons]
    if not eligible:
        return None
    return sorted(eligible, key=CandidateMetrics.selection_key)[0]


def classify_candidate(
    m: CandidateMetrics,
    *,
    capacity_fail: bool,
    missing_judged_identities: int,
    duplicate_entities_after_collapse: int,
    vector_validation_failures: int,
    provider_failures: int,
) -> None:
    """Mutate *m* in place to attach the frozen disqualification verdicts."""

    reasons: list[str] = []
    if vector_validation_failures > 0:
        reasons.append("malformed_or_wrong_dimension_vector")
    if provider_failures > 0:
        reasons.append("provider_failure_prevented_complete_evaluation")
    if missing_judged_identities > 0:
        reasons.append("missing_judged_identity")
    if duplicate_entities_after_collapse > 0:
        reasons.append("duplicate_entity_after_best_chunk_collapse")
    if m.account.get("cost_usd", 0.0) > SPEND_CAP_USD:
        reasons.append("projected_spend_above_cap")
    full_corpus_gb = CAPACITY_FACTS[m.candidate.dimension]["full_corpus_storage_gb"]
    if full_corpus_gb > STORAGE_GATE_GB:
        reasons.append("projected_full_corpus_storage_above_gate")
    m.disqualify_reasons = reasons
    m.capacity_fail = capacity_fail or (full_corpus_gb > STORAGE_GATE_GB)
    # A capacity FAIL is reported + evaluated but not selectable as the winner.
    m.eligible_for_selection = not reasons


# ---------------------------------------------------------------------------
# Deterministic standalone cost (order-independent; the cost axis of the tiebreak)
# ---------------------------------------------------------------------------


def golden_query_token_estimate(golden: GoldenSet) -> dict[str, Any]:
    """Deterministic, order-independent golden-query token estimate (offline).

    Returns the count of unique normalized golden queries and the summed
    ``estimate_tokens`` over them. Identical for every candidate (the golden set
    is fixed), so it never depends on which candidate ran first.
    """

    seen: set[str] = set()
    ordered: list[str] = []
    for case in golden.cases:
        nq = ec.normalize_query_for_embedding(case.query)
        if nq and nq not in seen:
            seen.add(nq)
            ordered.append(nq)
    tokens = sum(max(1, wr.estimate_tokens(q)) for q in ordered)
    return {
        "unique_normalized_queries": len(ordered),
        "estimated_input_tokens": tokens,
    }


def standalone_cost_for_candidate(
    candidate: Candidate, cohort: ChunkedCohort, golden: GoldenSet
) -> float:
    """Deterministic order-independent USD cost for one candidate's own inputs.

    = (tokens over the candidate's own unique chunk texts + golden-query tokens)
      * price. Independent of run order and of any sibling warming the shared
    dimension cache. This — not the order-dependent incremental cache-miss cost
    — is the cost axis of the frozen selection tiebreak.
    """

    corpus_tokens = sum(max(1, wr.estimate_tokens(t)) for t in cohort.unique_texts.values())
    golden_tokens = golden_query_token_estimate(golden)["estimated_input_tokens"]
    return (corpus_tokens + golden_tokens) / 1_000_000.0 * PRICE_PER_1M_TOKENS_USD


# ---------------------------------------------------------------------------
# Evaluate one candidate end-to-end
# ---------------------------------------------------------------------------


def evaluate_candidate(
    candidate: Candidate,
    cohort: ChunkedCohort,
    golden: GoldenSet,
    client: ProviderClient,
    *,
    ks: tuple[int, ...] = (1, 5, 10),
) -> CandidateMetrics:
    """Embed cohort + queries (cache-first), retrieve, and extract metrics.

    The golden queries are embedded through the same cache as the cohort so a
    query and a chunk over identical normalized bytes share one vector. Exact
    cosine only; latency is local exact-cosine latency, honestly labelled.
    """

    import time as _time

    from eval.retrieval.runner import run_eval

    # 1. Embed every unique chunk text (cache-first / provider).
    chunk_hashes = list(cohort.unique_texts.keys())
    chunk_texts = [cohort.unique_texts[h] for h in chunk_hashes]
    chunk_vecs = client.embed_texts(chunk_texts)
    vectors_by_hash = {h: v for h, v in zip(chunk_hashes, chunk_vecs)}

    # 2. Embed every golden query (cache-first), keyed by normalized query text.
    norm_queries: list[str] = []
    seen_q: set[str] = set()
    for case in golden.cases:
        nq = ec.normalize_query_for_embedding(case.query)
        if nq and nq not in seen_q:
            seen_q.add(nq)
            norm_queries.append(nq)
    qvecs = client.embed_texts(norm_queries)
    query_vectors = {nq: v for nq, v in zip(norm_queries, qvecs)}

    # 3. Build the exact-cosine retriever + run the frozen harness.
    retriever = SemanticRetriever(
        cohort, vectors_by_hash, query_vectors, dimension=candidate.dimension
    )
    t0 = _time.perf_counter()
    report = run_eval(retriever, golden, ks=ks, timeout_s=60.0)
    eval_latency_ms = (_time.perf_counter() - t0) * 1000.0

    # 4. Extract metrics (overall/by_category come from the harness).
    extra, later_diag = extract_metrics(report, cohort, retriever)
    latency = report.latency
    latency_label = {
        "p50_ms": round(latency.get("p50_ms", 0.0), 3),
        "p95_ms": round(latency.get("p95_ms", 0.0), 3),
        "mean_ms": round(latency.get("mean_ms", 0.0), 3),
        "n": int(latency.get("n", 0)),
        "method": "local exact cosine (numpy); NOT HNSW/SQL/Edge latency",
        "full_eval_wall_ms": round(eval_latency_ms, 3),
    }

    m = CandidateMetrics(
        candidate=candidate,
        overall=dict(report.overall),
        by_category={k: dict(v) for k, v in report.by_category.items()},
        by_entity_kind={k: dict(v) for k, v in report.by_entity_kind.items()},
        workflow_code_recall_at_10=extra["workflow_code_recall_at_10"],
        long_resource_chunk_recall_at_10=extra["long_resource_chunk_recall_at_10"],
        later_chunk_hit_rate=later_diag.get("later_chunk_hit_rate"),
        later_chunk_diagnostic=later_diag,
        counts=dict(report.counts),
        cohort_counts=cohort.sanitized_counts(),
        account=client.account(),
        selection_cost_usd=standalone_cost_for_candidate(candidate, cohort, golden),
        latency_ms=latency_label,
    )

    # 5. Integrity verdicts required by the frozen decision policy.
    # Every judged identity must be present in the cohort.
    judged_keys = set()
    for case in golden.cases:
        for j in case.expected:
            if j.grade >= 1:
                judged_keys.add(j.key())
    cohort_keys = {ch.entity_key for ch in cohort.chunks}
    missing = len([k for k in judged_keys if k not in cohort_keys])
    # Duplicate entity after collapse: the retriever collapses by entity, so the
    # ranked result must contain unique entity keys. Check the per-case rankings.
    duplicate_entities = 0
    for c in report.per_case:
        ranked = c.get("ranked") or []
        keys = [tuple(k) for k in ranked]
        if len(keys) != len(set(keys)):
            duplicate_entities += 1
    classify_candidate(
        m,
        capacity_fail=CAPACITY_FACTS[candidate.dimension]["verdict"] == "FAIL",
        missing_judged_identities=missing,
        duplicate_entities_after_collapse=duplicate_entities,
        vector_validation_failures=0,  # validate_vectors raises -> provider failure path
        provider_failures=0,
    )
    return m


# ---------------------------------------------------------------------------
# Sanitized evidence envelope
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = [
    ("openai_key", "sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    ("contributor_key", "hm_[0-9a-f]{64}"),
    ("supabase_project_url", "ujlwuvkrxlvoswwkerdf\\.supabase\\.co"),
    ("postgres_url", "postgres(?:ql)?://[^\\s\"']+"),
    ("generic_private_url", "https?://[0-9A-Za-z._-]+\\.(co|com|io|net)/functions/v1/[^\\s\"']+"),
]
_PY_MARKERS = ("Python ready-template source:", "Python scratchpad source:")

#: Structural denylist of field names that represent raw sensitive content —
#: raw query text, raw body/content, raw Python/source, raw URLs, raw payloads,
#: or vectors. These are rejected by *structure* (key name) even when the value
#: carries no secret marker, so a benign ``{"query": "...", "body": "..."}``
#: container fails closed. Exact-key match (not substring) so legitimate keys
#: like ``python`` (chunk config), ``sources`` (cohort counts), ``embedded_inputs``
#: and ``n_unique_embeddable_texts`` are NOT tripped. None of these names appear
#: in any tracked sanitized artifact (verified at build time).
_RAW_FIELD_DENYLIST = frozenset({
    "query", "query_text", "raw_query",
    "body", "content", "raw_body", "raw_content", "text", "raw_text", "normalized_text",
    "python_source", "source_code", "raw_source", "code",
    "url", "raw_url", "payload",
    "snippet", "matched_snippet",
    "vectors", "vector", "embedding", "embeddings", "embedding_vector",
})


def _iter_strings(obj: Any) -> Iterable[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, Mapping):
        for v in obj.values():
            yield from _iter_strings(v)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            yield from _iter_strings(v)


def _contains_vector(obj: Any) -> bool:
    """True if *obj* contains a list of many numbers (an embedding vector)."""
    if isinstance(obj, list):
        if len(obj) >= 16 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in obj):
            return True
        return any(_contains_vector(x) for x in obj)
    if isinstance(obj, Mapping):
        return any(_contains_vector(v) for v in obj.values())
    return False


def _raw_field_violations(obj: Any) -> list[str]:
    """Structural reject: any mapping key naming a raw-content/vector field.

    Walks containers by key name (not value), so a benign raw-query/body
    container fails closed even with no secret marker in its values.
    """

    found: list[str] = []

    def walk(x: Any, path: str) -> None:
        if isinstance(x, Mapping):
            for k, v in x.items():
                if isinstance(k, str) and k in _RAW_FIELD_DENYLIST:
                    found.append(f"raw_field:{path}/{k}" if path else f"raw_field:{k}")
                walk(v, f"{path}/{k}" if path else str(k))
        elif isinstance(x, (list, tuple)):
            for i, v in enumerate(x):
                walk(v, f"{path}[{i}]")

    walk(obj, "")
    return found


def scan_envelope(obj: Any) -> list[str]:
    """Return a list of evidence-hygiene violations in *obj* (empty = clean).

    Checks: no embedding vectors, no provider/contributor keys, no Supabase /
    postgres / functions URLs, no raw workflow-Python delimiter markers, and no
    structurally raw-content/vector field names (raw query/body/content/python/
    source/url/payload/vector keys are rejected even with no secret marker).
    """

    violations: list[str] = []
    if _contains_vector(obj):
        violations.append("contains_embedding_vector")
    violations.extend(_raw_field_violations(obj))
    blob = json.dumps(obj, ensure_ascii=False) if not isinstance(obj, str) else obj
    import re

    for label, pat in _SECRET_PATTERNS:
        if re.search(pat, blob):
            violations.append(f"forbidden_pattern:{label}")
    for marker in _PY_MARKERS:
        if marker in blob:
            violations.append(f"raw_workflow_python_marker")
            break
    return violations


def sanitize_report(metrics_list: list[CandidateMetrics], winner: CandidateMetrics | None) -> dict[str, Any]:
    """Build the credential/vector/raw-text-free decision envelope."""

    candidates = []
    for m in metrics_list:
        candidates.append(
            {
                "candidate": m.candidate.to_sanitized_dict(),
                "capacity": dict(CAPACITY_FACTS[m.candidate.dimension]),
                "status": m.status,
                "disqualify_reasons": list(m.disqualify_reasons),
                "capacity_fail": m.capacity_fail,
                "eligible_for_selection": m.eligible_for_selection,
                "overall": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in m.overall.items()},
                "by_category": {
                    cat: {k: (round(val, 6) if isinstance(val, float) else val) for k, val in bucket.items()}
                    for cat, bucket in m.by_category.items()
                },
                "by_entity_kind": {
                    ek: {k: (round(val, 6) if isinstance(val, float) else val) for k, val in bucket.items()}
                    for ek, bucket in m.by_entity_kind.items()
                },
                "workflow_code_recall_at_10": m.workflow_code_recall_at_10,
                "long_resource_chunk_recall_at_10": m.long_resource_chunk_recall_at_10,
                "later_chunk_diagnostic": m.later_chunk_diagnostic,
                "selection_cost_usd": (
                    round(m.selection_cost_usd, 6)
                    if m.selection_cost_usd is not None
                    else None
                ),
                "counts": m.counts,
                "cohort_counts": m.cohort_counts,
                "provider_account": m.account,
                "latency": m.latency_ms,
            }
        )
    envelope = {
        "decision_policy": {
            "disqualify_on": [
                "malformed_or_wrong_dimension_vector",
                "provider_failure_prevented_complete_evaluation",
                "missing_judged_identity",
                "duplicate_entity_after_best_chunk_collapse",
                "projected_spend_above_cap",
                "projected_full_corpus_storage_above_gate",
            ],
            "spend_cap_usd": SPEND_CAP_USD,
            "storage_gate_gb": STORAGE_GATE_GB,
            "selection_tiebreak": [
                "overall_recall_at_10",
                "mrr",
                "ndcg_at_10",
                "workflow_code_recall_at_10",
                "long_resource_chunk_recall_at_10",
                "lower_standalone_candidate_cost",
            ],
            "selection_tiebreak_note": (
                "the cost axis is the deterministic order-independent standalone "
                "candidate cost, NOT the order-dependent incremental cache-miss cost"
            ),
            "note": "capacity-FAIL dimensions are evaluated + reported but not selectable",
        },
        "candidates": candidates,
        "winner": winner.candidate.to_sanitized_dict() if winner else None,
        "winner_rationale": _winner_rationale(metrics_list, winner),
    }
    return envelope


# The frozen selection semantics: recall/quality axes, then the deterministic,
# order-independent *standalone* candidate cost. This is the single source of
# truth for `winner_rationale.selection_key_used`. It must NEVER read as the
# order-dependent incremental cache-miss cost ("lower actual cost").
SELECTION_KEY_USED = (
    "recall@10, mrr, ndcg@10, workflow_code_recall@10, "
    "long_resource_chunk_recall@10, lower standalone candidate cost"
)


def _winner_rationale(metrics_list: list[CandidateMetrics], winner: CandidateMetrics | None) -> dict[str, Any]:
    if winner is None:
        return {"eligible": False, "reason": "no candidate was eligible for selection"}
    ranked = sorted(
        [m for m in metrics_list if m.eligible_for_selection and not m.disqualify_reasons],
        key=CandidateMetrics.selection_key,
    )
    return {
        "eligible": True,
        "selected": winner.candidate.name,
        "eligible_ranking": [m.candidate.name for m in ranked],
        "selection_key_used": SELECTION_KEY_USED,
        "capacity_note": (
            "1536-d candidates are evaluated for quality but capacity-disqualified "
            "(full-corpus storage 16.4 GB > 12 GB gate) and cannot be the winner"
        ),
    }


# ---------------------------------------------------------------------------
# Immutable offline evidence replay (task 2.14 final correction)
# ---------------------------------------------------------------------------
#
# The first implementation failed to persist a raw eval3 cohort snapshot
# (raw texts + identity-to-chunk mapping) before the provider calls. That raw
# snapshot is unrecoverable, so exact-cosine retrieval cannot be recomputed
# from raw text offline. The strongest *honest* immutable evidence available is
# the two immutable cache files + the eval3 decision JSON + the fixed golden
# file + the frozen candidate grid and decision policy. The functions below
# build a sanitized replay bundle + tracked evidence manifest and a strict
# cache/evidence replay that fails closed on any mismatch and provably makes
# zero provider/network calls. This is a cryptographic evidence/cache replay,
# NOT a recomputation of exact-cosine retrieval from raw text (limitation is
# recorded explicitly in every artifact).


#: Both immutable eval3 caches contain exactly this many records.
EVAL3_CACHE_RECORD_COUNT = 27464
#: The two paid eval3 embedding dimensions.
EVAL3_DIMENSIONS = (384, 1536)
#: Eval3 was the third evaluation attempt; the first two were interrupted /
#: destructively reset. Exact spend for those earlier attempts is unrecoverable.
EVAL3_EVALUATION_ATTEMPTS = 3
EVAL3_DESTRUCTIVE_CACHE_RESETS = 2
#: Version stamp for the private replay bundle schema.
REPLAY_BUNDLE_VERSION = 1


class ReplayMismatch(Exception):
    """Raised by strict offline replay on any cache/bundle/golden/grid/policy/
    cohort/decision hash, record-count, dimension, or mode mismatch."""


def _sha256_json(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _repo_root() -> Path:
    """Repo root derived from this module's location (``eval/retrieval/``)."""

    return Path(__file__).resolve().parents[2]


def resolve_private_path(path: str | Path, *, anchor: Path | None = None) -> Path:
    """Resolve a private artifact path.

    Absolute paths are returned as-is. Repo-relative paths (e.g.
    ``.cache/hivemind-semantic-eval/replay-bundle.json``) are anchored at the
    repo root — NEVER at ``cache_dir.parent`` (which would double the cache dir
    into ``.cache/.cache/...``).
    """

    p = Path(path)
    if p.is_absolute():
        return p
    return (anchor or _repo_root()) / p


def assert_private_file(path: str | Path, *, mode: int = PRIVATE_FILE_MODE) -> None:
    """Fail closed if a private artifact file is absent or its mode is permissive."""

    p = Path(path)
    if not p.exists():
        raise ReplayMismatch(f"private artifact missing: {p}")
    actual = p.stat().st_mode & 0o777
    if actual != mode:
        raise ReplayMismatch(
            f"private artifact {p} mode {oct(actual)} != required private {oct(mode)}"
        )


def assert_private_dir(path: str | Path, *, mode: int = PRIVATE_DIR_MODE) -> None:
    """Fail closed if a private directory is absent or its mode is permissive."""

    p = Path(path)
    if not p.is_dir():
        raise ReplayMismatch(f"private dir missing: {p}")
    actual = p.stat().st_mode & 0o777
    if actual != mode:
        raise ReplayMismatch(
            f"private dir {p} mode {oct(actual)} != required private {oct(mode)}"
        )


def atomic_write_text(path: str | Path, text: str, *, mode: int = 0o644) -> None:
    """Stage, fsync, and atomically replace *path* (mode-safe from first byte).

    Opens a sibling temp file with the requested *mode* at creation time, then
    ``fchmod``s the open fd to defeat the process umask BEFORE writing any bytes,
    so the file is private from its first byte (the temp lives under the already
    0700 parent). Writes, fsyncs, closes, then ``os.replace``s into place. An
    interruption leaves the original file untouched.

    Fails closed (raises :class:`ReplayMismatch`) rather than silently swallowing
    a chmod/permission error: a private artifact that cannot be made private is
    never written. Used for the tracked JSON/Markdown evidence pair (mode 0644)
    and the private replay bundle / raw snapshot (mode 0600).
    """

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        # Defeat the umask: set the exact mode on the open fd before any byte.
        os.fchmod(fd, mode)
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, p)
    # The replaced inode already carries the exact mode (set via fd above); this
    # final chmod only guards a pathological pre-existing permissive inode at the
    # final path. A failure here is a real privacy failure -> fail closed.
    try:
        os.chmod(p, mode)
    except OSError as exc:
        raise ReplayMismatch(f"could not set mode {oct(mode)} on {p}: {exc}") from exc


def evidence_pair_id(decision: Mapping[str, Any]) -> str:
    """Deterministic shared evidence-pair ID for the JSON + Markdown pair.

    Derived from the decision deterministic-core hash (NOT from either file's
    bytes), so the same pair ID can be embedded in both files and then bound by
    the last-published manifest without any recursive self-hashing.
    """

    return "task-2.14-evidence-" + decision_deterministic_core_hash(decision)[:16]


def candidate_grid_hash() -> str:
    """Deterministic SHA-256 of the frozen four-candidate grid (sanitized)."""

    grid = [c.to_sanitized_dict() for c in CANDIDATES]
    return _sha256_json(grid)


def decision_policy_hash(policy: Mapping[str, Any]) -> str:
    """Deterministic SHA-256 of the frozen decision policy object."""

    return _sha256_json(dict(policy))


def eval3_cohort_manifest_hash(decision: Mapping[str, Any]) -> str:
    """Deterministic SHA-256 over eval3's persisted cohort counts.

    Derived only from durable eval3 cohort facts: the top-level cohort_counts
    (sources / high_water / integrity) and each candidate's persisted chunk
    counts. No raw texts, no identity-to-chunk mapping (those were not
    persisted), no vectors.
    """

    cm = {
        "cohort_counts": decision["cohort_counts"],
        "per_candidate_cohort_counts": {
            c["candidate"]["name"]: c["cohort_counts"] for c in decision["candidates"]
        },
    }
    return _sha256_json(cm)


def accounting_canonical_hash(accounting: Mapping[str, Any]) -> str:
    """Deterministic SHA-256 over the COMPLETE canonical accounting object.

    Covers all three blocks (exact eval3 incremental, standalone per-candidate,
    and historical reconstructed estimate/disclosures) plus the runtime
    spend-gate disclosures. Bound identically in the bundle + tracked manifest
    and compared to ``decision.accounting`` by strict replay, so tampering ANY
    accounting field (especially a top-level total cost) fails closed.
    """

    return _sha256_json(dict(accounting))


def decision_deterministic_core(decision: Mapping[str, Any]) -> dict[str, Any]:
    """The decision-relevant subset that must be byte-identical across replays.

    Strips timestamps and volatile envelope metadata; keeps the winner, each
    candidate's selection-relevant metrics + eval3-recorded input tokens, the
    eligible/disqualify state, and the frozen tiebreak order.
    """

    return {
        "winner": decision.get("winner", {}).get("name") if decision.get("winner") else None,
        "selection_tiebreak": decision["decision_policy"]["selection_tiebreak"],
        "candidates": [
            {
                "name": c["candidate"]["name"],
                "dimension": c["candidate"]["dimension"],
                "eval_contract_id": c["candidate"]["eval_contract_id"],
                "overall": c["overall"],
                "workflow_code_recall_at_10": c["workflow_code_recall_at_10"],
                "long_resource_chunk_recall_at_10": c["long_resource_chunk_recall_at_10"],
                "eligible_for_selection": c["eligible_for_selection"],
                "disqualify_reasons": c["disqualify_reasons"],
                "eval3_input_tokens_from_usage": c["provider_account"].get("input_tokens_from_usage"),
            }
            for c in decision["candidates"]
        ],
    }


def decision_deterministic_core_hash(decision: Mapping[str, Any]) -> str:
    return _sha256_json(decision_deterministic_core(decision))


def cache_file_facts(path: Path, expected_dimension: int) -> dict[str, Any]:
    """Read-only facts about one immutable cache file (no mutation, no vector load).

    Verifies every record carries the expected dimension and counts records.
    """

    path = Path(path)
    st = path.stat()
    records = 0
    dim_ok = True
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                # A partial trailing line would itself be a mismatch vs the
                # frozen record count; let the count check catch it.
                continue
            records += 1
            if rec.get("d") != expected_dimension:
                dim_ok = False
    return {
        "path": path.name,
        "dimension": expected_dimension,
        "sha256": _sha256_file(path),
        "byte_size": st.st_size,
        "record_count": records,
        "dimension_consistent": dim_ok,
        "mode": oct(st.st_mode & 0o777),
        "mode_is_private": oct(st.st_mode & 0o777) == "0o600",
    }


def golden_file_facts(path: Path) -> dict[str, Any]:
    """Read-only facts about the fixed golden file."""

    path = Path(path)
    import json as _json

    data = _json.loads(path.read_text(encoding="utf-8"))
    cases = data.get("cases", [])
    n_judged = sum(
        1 for c in cases if any(j.get("grade", 0) >= 1 for j in c.get("expected", []))
    )
    n_no_hit = sum(1 for c in cases if c.get("expect_no_hit"))
    return {
        "path": str(path.name),
        "sha256": _sha256_file(path),
        "version": data.get("version"),
        "n_cases": len(cases),
        "n_judged": n_judged,
        "n_no_hit": n_no_hit,
    }


def metrics_from_envelope(
    decision: Mapping[str, Any], golden_query_tokens: int
) -> list[CandidateMetrics]:
    """Reconstruct :class:`CandidateMetrics` from a tracked decision envelope.

    Used by strict replay to re-derive the winner from the recorded metrics via
    the frozen selection policy. ``selection_cost_usd`` is the deterministic
    standalone cost (preflight corpus tokens + shared golden-query tokens), so
    the cost axis never depends on evaluation order.
    """

    preflight = decision.get("preflight", {}).get("per_candidate", {})
    by_name = {c.name: c for c in CANDIDATES}
    out: list[CandidateMetrics] = []
    for c in decision["candidates"]:
        name = c["candidate"]["name"]
        cand = by_name[name]
        corpus_tokens = preflight.get(name, {}).get("projected_input_tokens", 0)
        standalone = (corpus_tokens + golden_query_tokens) / 1_000_000.0 * PRICE_PER_1M_TOKENS_USD
        m = CandidateMetrics(
            candidate=cand,
            overall=dict(c["overall"]),
            by_category={},
            by_entity_kind={},
            workflow_code_recall_at_10=c["workflow_code_recall_at_10"],
            long_resource_chunk_recall_at_10=c["long_resource_chunk_recall_at_10"],
            later_chunk_hit_rate=c.get("later_chunk_diagnostic", {}).get("later_chunk_hit_rate"),
            later_chunk_diagnostic=dict(c.get("later_chunk_diagnostic", {})),
            counts=dict(c.get("counts", {})),
            cohort_counts=dict(c.get("cohort_counts", {})),
            account=dict(c["provider_account"]),
            selection_cost_usd=standalone,
            latency_ms={},
        )
        m.disqualify_reasons = list(c.get("disqualify_reasons", []))
        m.capacity_fail = bool(c.get("capacity_fail", False))
        m.eligible_for_selection = bool(c.get("eligible_for_selection", True))
        out.append(m)
    return out


def build_evidence_accounting(
    decision: Mapping[str, Any], golden_query_estimate: Mapping[str, Any]
) -> dict[str, Any]:
    """The three clearly-named accounting blocks (exact eval3 / standalone /
    historical), derived deterministically from durable inputs.

    * ``eval3_actual_incremental_bakeoff`` — exact provider usage captured by
      eval3, including shared dimension-cache reuse.
    * ``standalone_candidate_accounting`` — deterministic order-independent
      per-candidate token/cost (each candidate's own unique corpus texts + the
      shared golden queries).
    * ``historical_duplicate_attempts`` — transparent disclosure that exact
      earlier spend is unavailable, the two destructive resets, and a
      conservative reconstructed estimate that is NOT a guaranteed upper bound
      (true spend could exceed it; see ``estimate_label``).
    """

    preflight = decision["preflight"]["per_candidate"]
    gq_tokens = int(golden_query_estimate["estimated_input_tokens"])
    gq_count = int(golden_query_estimate["unique_normalized_queries"])

    eval3_per: dict[str, Any] = {}
    eval3_total_tokens = 0
    for c in decision["candidates"]:
        name = c["candidate"]["name"]
        acc = c["provider_account"]
        toks = int(acc.get("input_tokens_from_usage", 0))
        eval3_total_tokens += toks
        eval3_per[name] = {
            "dimension": c["candidate"]["dimension"],
            "api_requests": int(acc.get("api_requests", 0)),
            "embedded_inputs": int(acc.get("embedded_inputs", 0)),
            "input_tokens_from_usage": toks,
            "cache_hits": int(acc.get("cache_hits", 0)),
            "cache_misses": int(acc.get("cache_misses", 0)),
            "cost_usd": round(toks / 1_000_000.0 * PRICE_PER_1M_TOKENS_USD, 6),
        }

    standalone_per: dict[str, Any] = {}
    for c in decision["candidates"]:
        name = c["candidate"]["name"]
        corpus_tokens = int(preflight[name]["projected_input_tokens"])
        total = corpus_tokens + gq_tokens
        standalone_per[name] = {
            "dimension": c["candidate"]["dimension"],
            "corpus_unique_texts": int(preflight[name]["n_unique_embeddable_texts"]),
            "corpus_estimated_input_tokens": corpus_tokens,
            "golden_query_count": gq_count,
            "golden_query_estimated_tokens": gq_tokens,
            "total_estimated_input_tokens": total,
            "estimated_cost_usd": round(total / 1_000_000.0 * PRICE_PER_1M_TOKENS_USD, 6),
            "basis": (
                "deterministic wr.estimate_tokens over the candidate's own unique "
                "corpus chunk texts + the shared fixed golden queries; order-"
                "independent; excludes any sibling warming the shared dimension cache"
            ),
        }

    per_bakeoff = float(decision["preflight"]["aggregate"]["projected_cost_usd_all_candidates"])
    bound = EVAL3_EVALUATION_ATTEMPTS * per_bakeoff

    return {
        "price_per_1m_tokens_usd": PRICE_PER_1M_TOKENS_USD,
        "spend_cap_usd": SPEND_CAP_USD,
        "runtime_spend_gates": {
            "preflight_estimate": {
                "scope": (
                    "estimated aggregate projected cost over all four candidates, "
                    "checked ONCE before any provider call; an estimate, not actual"
                ),
                "field": "preflight.aggregate.projected_cost_usd_all_candidates",
                "separate_from_runtime_actual_gate": True,
            },
            "aggregate_actual_usage_guard": {
                "scope": (
                    "shared cross-candidate guard created once and bound to every "
                    "real-path ProviderClient; before every provider request it gates "
                    "(aggregate actual usage reported across prior + current clients) "
                    "+ (the next conservative estimated batch) and raises BEFORE the "
                    "transport is invoked"
                ),
                "hard_cap_resets_between_candidates_or_dimensions": False,
                "records_actual_usage_exactly_once_per_successful_batch": True,
                "failed_attempt_billing_unavailable_and_not_counted": True,
                "failed_attempt_billing_note": (
                    "the transport records usage ONLY for a response the provider "
                    "returned (a Mapping); a failed attempt (transport error / HTTP "
                    "429 / 5xx) raises before any usage is reported and supplies zero "
                    "usage, so any tokens a provider may bill for a failed attempt are "
                    "UNAVAILABLE and are NOT counted in the recorded aggregate. The "
                    "recorded token total is the sum of successful-response usage only."
                ),
                "retries_bounded": True,
                "retries_bounded_max_attempts": PROVIDER_MAX_ATTEMPTS,
                "recorded_aggregate_actual_cap_not_proof_against_unreported_billed_failures": True,
                "offline_fake_embedder_zero_cost_unblocked": True,
                "separate_from_preflight_estimate": True,
            },
        },
        "eval3_actual_incremental_bakeoff": {
            "scope": "exact provider usage captured by eval3 (the final uninterrupted run) only",
            "shared_dimension_cache_reuse": (
                "the two candidates sharing a dimension share one cache keyed by "
                "content hash, so a later same-dimension candidate reuses identical "
                "chunk texts as cache hits; each candidate's input_tokens reflect "
                "only its own cache misses actually sent to the provider"
            ),
            "per_candidate": eval3_per,
            "total_input_tokens": eval3_total_tokens,
            "total_cost_usd": round(eval3_total_tokens / 1_000_000.0 * PRICE_PER_1M_TOKENS_USD, 6),
        },
        "standalone_candidate_accounting": {
            "scope": (
                "deterministic order-independent per-candidate cost used as the cost "
                "axis of the frozen tiebreak; does NOT depend on evaluation order or "
                "which sibling warmed the dimension cache"
            ),
            "per_candidate": standalone_per,
        },
        "historical_duplicate_attempts": {
            "exact_spend": "unavailable",
            "exact_spend_reason": (
                "the first implementation did not persist per-attempt provider usage; "
                "two interrupted/destructive resets make exact earlier spend "
                "unrecoverable"
            ),
            "destructive_cache_resets_disclosed": EVAL3_DESTRUCTIVE_CACHE_RESETS,
            "duplicate_provider_work_disclosed": (
                "the earlier partial/duplicate attempts re-embedded texts already "
                "embedded in the same run before being reset"
            ),
            "evaluation_attempts": EVAL3_EVALUATION_ATTEMPTS,
            "per_full_bakeoff_projected_cost_usd": round(per_bakeoff, 6),
            "conservative_reconstructed_estimate_usd": round(bound, 6),
            "estimate_label": (
                "CONSERVATIVE RECONSTRUCTED ESTIMATE, not a guaranteed upper bound "
                "and not exact: it scales the frozen preflight projection by the "
                f"{EVAL3_EVALUATION_ATTEMPTS} attempts. The true spend is NOT "
                "guaranteed to be lower — responses can be billed before cache "
                "persistence/retry, and the frozen preflight OMITTED golden-query "
                "tokens, so actual spend could exceed this figure. No bounded "
                "in-flight/retry + golden-token margin has been added or justified."
            ),
            "not_a_guaranteed_upper_bound": True,
            "within_spend_cap": bound <= SPEND_CAP_USD,
            "within_spend_cap_note": (
                "the ESTIMATE is under the cap; this is NOT proof that true spend was "
                "under the cap, because the estimate omits in-flight/retry billing "
                "and golden-query tokens"
            ),
        },
    }


def build_replay_bundle(
    decision: Mapping[str, Any],
    *,
    golden_path: Path,
    cache_dir: Path,
    golden_query_estimate: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the private (gitignored) sanitized replay bundle.

    Contains only versions / hashes / counts / aggregate metrics / sanitized
    accounting — never raw queries, raw bodies, raw Python, credentials, URLs,
    or vectors. Cache file *facts* (hash/size/count/dim/mode) are recorded as
    immutable evidence about the files; the vector payloads are never included.
    """

    golden = golden_file_facts(golden_path)
    # Bind the fixed golden SHA-256 literal at build time: the actual golden
    # file must equal GOLDEN_SHA256, or the bundle (and downstream the manifest
    # + strict replay) fails closed. Strict replay re-asserts the same literal
    # against the actual / bundle / manifest golden facts.
    if golden["sha256"] != GOLDEN_SHA256:
        raise ReplayMismatch(
            f"golden sha256 {golden['sha256']} != fixed literal {GOLDEN_SHA256}"
        )
    caches = {
        str(dim): cache_file_facts(cache_dir / f"cache.{dim}.jsonl", dim)
        for dim in EVAL3_DIMENSIONS
    }
    # The observed per-dimension record count (both dimensions are equal for a
    # valid eval3 run). Recorded so strict replay can re-assert immutability.
    observed_counts = {c["record_count"] for c in caches.values()}
    if len(observed_counts) != 1:
        raise ReplayMismatch(
            f"eval3 caches disagree on record count: {sorted(observed_counts)}"
        )
    eval3_record_count = observed_counts.pop()
    accounting = build_evidence_accounting(decision, golden_query_estimate)
    bundle = {
        "bundle_version": REPLAY_BUNDLE_VERSION,
        "kind": "task-2.14-evidence-cache-replay",
        "decision_deterministic_core": decision_deterministic_core(decision),
        "decision_deterministic_core_sha256": decision_deterministic_core_hash(decision),
        "candidate_grid_hash": candidate_grid_hash(),
        "decision_policy_hash": decision_policy_hash(decision["decision_policy"]),
        "eval3_cohort_manifest_hash": eval3_cohort_manifest_hash(decision),
        "accounting_sha256": accounting_canonical_hash(accounting),
        "golden": golden,
        "immutable_caches": caches,
        "eval3_cache_record_count": eval3_record_count,
        "accounting": accounting,
        "winner": decision["winner"]["name"] if decision.get("winner") else None,
        "limitation": (
            " cryptographic evidence/cache replay, NOT a recomputation of "
            "exact-cosine retrieval from raw text: the first implementation did "
            "not persist the raw eval3 texts or identity-to-chunk mapping. The "
            "winner/metrics are re-derived from the recorded decision via the "
            "frozen policy; the immutable caches are verified by hash/count/dim/"
            "mode. No raw queries, bodies, Python, credentials, URLs, or vectors."
        ),
    }
    violations = scan_envelope(bundle)
    if violations:
        raise RuntimeError(f"replay bundle hygiene violations: {violations}")
    return bundle


def build_frozen_manifest(
    decision: Mapping[str, Any],
    *,
    golden_path: Path,
    cache_dir: Path,
    bundle: Mapping[str, Any],
    replay_proof: Mapping[str, Any],
    evidence_pair: Mapping[str, Any] | None = None,
    bundle_path: str | Path | None = None,
    bundle_file_sha256: str | None = None,
) -> dict[str, Any]:
    """The tracked (committed) frozen/evidence manifest with every required hash.

    Binds **exact file bytes** (not only canonical objects): the replay-bundle
    file SHA-256 (``file_sha256``) plus the exact published decision-JSON and
    decision-Markdown byte hashes (via *evidence_pair*). The canonical object
    hash of the bundle (``canonical_sha256``) is retained but labeled distinctly
    so it is never confused with the byte hash.

    When *evidence_pair* is supplied (the last-published commit marker), it binds
    the exact published decision-JSON and decision-Markdown SHA-256 hashes plus
    the shared canonical pair ID. The manifest is published LAST so its recorded
    file hashes are the authoritative binding of the pair (no recursive
    self-hashing: the pair ID is derived from the decision deterministic core,
    not from either file's bytes).
    """

    replay_bundle: dict[str, Any] = {
        "version": bundle["bundle_version"],
        "path": str(bundle_path) if bundle_path is not None
        else ".cache/hivemind-semantic-eval/replay-bundle.json",
        # Canonical object hash (sort_keys JSON) — distinct from the file bytes.
        "canonical_sha256": _sha256_json(bundle),
        "canonical_sha256_label": (
            "SHA-256 over canonical sort_keys JSON of the bundle OBJECT, NOT the "
            "written file bytes; used for object integrity, labeled distinctly"
        ),
    }
    if bundle_file_sha256 is not None:
        # Exact bytes of the written replay-bundle file — the authoritative hash.
        replay_bundle["file_sha256"] = bundle_file_sha256
        replay_bundle["file_sha256_label"] = (
            "SHA-256 over the EXACT bytes of the written replay-bundle.json file"
        )
    manifest = {
        "task": "2.14-embedding-decision-frozen-evidence",
        "purpose": (
            "immutable offline evidence manifest binding the eval3 decision to the "
            "two immutable cache files, the fixed golden file, the frozen candidate "
            "grid, and the frozen decision policy"
        ),
        "golden": bundle["golden"],
        "candidate_grid_hash": bundle["candidate_grid_hash"],
        "decision_policy_hash": bundle["decision_policy_hash"],
        "eval3_cohort_manifest_hash": bundle["eval3_cohort_manifest_hash"],
        "accounting_sha256": bundle["accounting_sha256"],
        "decision_deterministic_core_sha256": bundle["decision_deterministic_core_sha256"],
        "immutable_caches": bundle["immutable_caches"],
        "eval3_cache_record_count": bundle["eval3_cache_record_count"],
        "replay_bundle": replay_bundle,
        "replay_proof": dict(replay_proof),
        "limitation": bundle["limitation"],
    }
    if evidence_pair is not None:
        manifest["evidence_pair"] = dict(evidence_pair)
    violations = scan_envelope(manifest)
    if violations:
        raise RuntimeError(f"manifest hygiene violations: {violations}")
    return manifest


def verify_evidence_pair(
    manifest: Mapping[str, Any],
    *,
    decision_json_path: str | Path,
    decision_md_path: str | Path,
) -> dict[str, Any]:
    """Fail closed if the tracked JSON/Markdown pair diverges from the manifest.

    Recomputes both file SHA-256 hashes and asserts they equal the manifest's
    ``evidence_pair`` bindings, and that the shared pair ID embedded in each file
    matches the manifest's pair ID. Raises :class:`ReplayMismatch` on any tamper.

    Returns the parsed *bound* decision object loaded from the hash-verified JSON
    file so callers can require it to be the exact authoritative object used for
    replay (full-object deep equality / hash equality with the supplied decision).
    The bound file is the single source of truth; a caller-supplied in-memory
    decision that diverges from it in ANY field (selection, handoff, evidence
    metadata, accounting, flags, timestamps, future additions) is rejected.
    """

    pair = manifest.get("evidence_pair")
    if pair is None:
        return {}  # older manifest without the pair binding; no object to bind
    pair_id = pair.get("id")
    if decision_json_path is None:
        raise ReplayMismatch(
            "manifest binds evidence_pair but decision_json_path is required "
            "(the evidence pair is NOT optional)"
        )
    if decision_md_path is None:
        raise ReplayMismatch(
            "manifest binds evidence_pair but decision_md_path is required "
            "(the evidence pair is NOT optional)"
        )
    jp = Path(decision_json_path)
    mp = Path(decision_md_path)
    if not jp.exists():
        raise ReplayMismatch(f"decision json missing: {jp}")
    if not mp.exists():
        raise ReplayMismatch(f"decision md missing: {mp}")
    if _sha256_file(jp) != pair["decision_json"]["sha256"]:
        raise ReplayMismatch("decision json sha256 != manifest evidence_pair")
    if _sha256_file(mp) != pair["decision_md"]["sha256"]:
        raise ReplayMismatch("decision md sha256 != manifest evidence_pair")
    decision = json.loads(jp.read_text(encoding="utf-8"))
    if decision.get("evidence_pair_id") != pair_id:
        raise ReplayMismatch("decision json evidence_pair_id != manifest pair id")
    if pair_id not in mp.read_text(encoding="utf-8"):
        raise ReplayMismatch("decision md does not carry the evidence pair id")
    return decision


def strict_offline_replay(
    *,
    bundle: Mapping[str, Any],
    decision: Mapping[str, Any],
    golden_path: Path,
    cache_dir: Path,
    manifest: Mapping[str, Any],
    golden_query_tokens: int,
    bundle_path: str | Path | None = None,
    decision_json_path: str | Path | None = None,
    decision_md_path: str | Path | None = None,
) -> dict[str, Any]:
    """Strict cache/evidence replay. Fails closed on any mismatch; zero calls.

    Loads only the bundle + cache files + tracked decision/golden/manifest.
    Never calls the live cohort reader, never instantiates a provider transport,
    never compacts or writes either cache. Proves ``provider_calls_attempted ==
    0`` and ``network_calls == 0`` and reproduces the recorded winner from the
    frozen policy.

    Full integrity (fail closed on):

    * the fixed golden SHA-256 literal — actual file, bundle facts, and manifest
      facts must all equal :data:`GOLDEN_SHA256`;
    * bundle canonical hash ``_sha256_json(bundle) == manifest.replay_bundle.sha256``;
    * every bundle/manifest fact (golden / grid / policy / cohort / decision-core
      / caches / record-count);
    * private file/parent modes for the cache dir + paid caches (+ the bundle
      file when *bundle_path* is given);
    * the tracked JSON/Markdown evidence pair hashes + shared pair ID (when the
      manifest carries ``evidence_pair`` and the file paths are supplied).
    """

    checks: list[str] = []

    def _check(cond: bool, label: str) -> None:
        if not cond:
            raise ReplayMismatch(label)
        checks.append(label)

    # 1. Decision deterministic core + hashes (vs bundle + manifest).
    _check(
        decision_deterministic_core_hash(decision) == bundle["decision_deterministic_core_sha256"],
        "decision_deterministic_core_hash_matches_bundle",
    )
    _check(
        bundle["decision_deterministic_core_sha256"] == manifest["decision_deterministic_core_sha256"],
        "decision_core_hash_matches_manifest",
    )
    _check(bundle["candidate_grid_hash"] == manifest["candidate_grid_hash"], "grid_hash_matches_manifest")
    _check(candidate_grid_hash() == manifest["candidate_grid_hash"], "candidate_grid_hash_recomputed_matches")
    _check(decision_policy_hash(decision["decision_policy"]) == manifest["decision_policy_hash"], "policy_hash_recomputed_matches")
    _check(eval3_cohort_manifest_hash(decision) == manifest["eval3_cohort_manifest_hash"], "cohort_manifest_hash_recomputed_matches")
    _check(bundle["decision_deterministic_core"]["winner"] == decision.get("winner", {}).get("name"), "bundle_winner_matches_decision")

    # 1ab. COMPLETE accounting binding (gap 2). ``decision.accounting`` is outside
    # the deterministic core, so it is bound explicitly: the COMPLETE canonical
    # accounting object (all three blocks + runtime spend gates) must be
    # byte-identical between the tracked decision and the bundle, and its
    # recomputed canonical hash must equal both the bundle's and the manifest's
    # recorded ``accounting_sha256``. Tampering ANY accounting field — especially
    # a top-level total cost — fails closed here.
    decision_accounting = decision.get("accounting")
    if decision_accounting is None:
        raise ReplayMismatch("decision is missing the bound accounting object")
    _check(bundle.get("accounting") is not None, "bundle_carries_accounting")
    _check(
        decision_accounting == bundle["accounting"],
        "decision_accounting_object_matches_bundle_accounting",
    )
    _check(
        manifest.get("accounting_sha256") is not None,
        "manifest_carries_accounting_sha256",
    )
    recomputed_accounting_hash = accounting_canonical_hash(decision_accounting)
    _check(
        recomputed_accounting_hash == bundle["accounting_sha256"],
        "decision_accounting_hash_matches_bundle",
    )
    _check(
        recomputed_accounting_hash == manifest["accounting_sha256"],
        "decision_accounting_hash_matches_manifest",
    )
    _check(
        bundle["accounting_sha256"] == manifest["accounting_sha256"],
        "bundle_accounting_hash_matches_manifest",
    )

    # 1b. Bundle/manifest fact cross-checks (golden/grid/policy/cohort/caches).
    _check(bundle["golden"] == manifest["golden"], "bundle_golden_facts_match_manifest")
    _check(bundle["decision_policy_hash"] == manifest["decision_policy_hash"], "bundle_policy_hash_matches_manifest")
    _check(bundle["eval3_cohort_manifest_hash"] == manifest["eval3_cohort_manifest_hash"], "bundle_cohort_hash_matches_manifest")
    _check(bundle["candidate_grid_hash"] == manifest["candidate_grid_hash"], "bundle_grid_hash_matches_manifest")
    _check(bundle["immutable_caches"] == manifest["immutable_caches"], "bundle_caches_match_manifest")
    _check(bundle["eval3_cache_record_count"] == manifest["eval3_cache_record_count"], "bundle_record_count_matches_manifest")

    # 1c. Bundle canonical-object integrity (tamper of the bundle object itself).
    rb = manifest.get("replay_bundle", {})
    canonical = rb.get("canonical_sha256", rb.get("sha256"))
    if canonical is not None:
        _check(_sha256_json(bundle) == canonical, "bundle_canonical_hash_matches_manifest")
    _check(bundle["bundle_version"] == rb.get("version"), "bundle_version_matches_manifest")

    # 2. Golden file (re-read read-only; never mutated).
    g_actual = golden_file_facts(golden_path)
    _check(g_actual["sha256"] == manifest["golden"]["sha256"], "golden_sha256_matches")
    _check(g_actual["version"] == manifest["golden"]["version"], "golden_version_matches")
    _check(g_actual["n_cases"] == manifest["golden"]["n_cases"], "golden_case_count_matches")
    # Fixed golden literal: actual / bundle / manifest must all equal GOLDEN_SHA256.
    _check(g_actual["sha256"] == GOLDEN_SHA256, "golden_sha256_matches_fixed_literal")
    _check(bundle["golden"]["sha256"] == GOLDEN_SHA256, "bundle_golden_matches_fixed_literal")
    _check(manifest["golden"]["sha256"] == GOLDEN_SHA256, "manifest_golden_matches_fixed_literal")

    # 3. Immutable caches (re-read read-only; never mutated/written/compacted).
    for dim in EVAL3_DIMENSIONS:
        key = str(dim)
        actual = cache_file_facts(cache_dir / f"cache.{dim}.jsonl", dim)
        recorded = manifest["immutable_caches"][key]
        _check(actual["sha256"] == recorded["sha256"], f"cache.{dim}_sha256_unchanged")
        _check(actual["byte_size"] == recorded["byte_size"], f"cache.{dim}_byte_size_unchanged")
        _check(actual["record_count"] == recorded["record_count"], f"cache.{dim}_record_count_unchanged")
        if "eval3_cache_record_count" in manifest:
            _check(
                actual["record_count"] == manifest["eval3_cache_record_count"],
                f"cache.{dim}_record_count_is_eval3_frozen",
            )
        _check(actual["dimension_consistent"], f"cache.{dim}_dimension_consistent")
        _check(actual["mode_is_private"], f"cache.{dim}_mode_is_0600")

    # 3b. Private parent dir of the caches/snapshot/bundle must be 0700.
    _check(
        cache_dir.is_dir() and (cache_dir.stat().st_mode & 0o777) == PRIVATE_DIR_MODE,
        "cache_dir_mode_is_0700",
    )

    # 3c. Private bundle file mode + exact file-byte hash (when path supplied).
    if bundle_path is not None:
        bp = resolve_private_path(bundle_path)
        assert_private_file(bp, mode=PRIVATE_FILE_MODE)
        checks.append("bundle_file_mode_is_0600")
        # Exact FILE bytes (item 12): distinct from the canonical object hash.
        file_hash = rb.get("file_sha256")
        if file_hash is not None:
            _check(_sha256_file(bp) == file_hash, "bundle_file_sha256_matches_manifest")
        elif "sha256" in rb:
            # Backward compat: older manifests recorded only a single sha256.
            _check(_sha256_file(bp) == rb["sha256"], "bundle_file_sha256_matches_manifest")

    # 4. Re-derive the winner from the recorded metrics via the frozen policy.
    metrics = metrics_from_envelope(decision, golden_query_tokens)
    derived = select_winner(metrics)
    derived_name = derived.candidate.name if derived else None
    recorded_name = decision.get("winner", {}).get("name")
    _check(derived_name == recorded_name, "winner_reproduced_from_frozen_policy")

    # 5. Re-derive the eligible ordering (must match the recorded rationale).
    recorded_ranking = decision.get("winner_rationale", {}).get("eligible_ranking", [])
    ranked = [m.candidate.name for m in sorted(
        [m for m in metrics if m.eligible_for_selection and not m.disqualify_reasons],
        key=CandidateMetrics.selection_key,
    )]
    _check(ranked == recorded_ranking, "eligible_ranking_reproduced")

    # 6. Tracked JSON/Markdown evidence pair (hashes + shared pair ID).
    # When the manifest binds an evidence_pair, BOTH the decision JSON and the
    # decision Markdown paths are MANDATORY (gap 1): a tracked decision replay
    # may not weaken or skip this binding by omitting/None-ing a path. Missing /
    # None / unreadable / mismatched paths fail closed.
    #
    # verify_evidence_pair returns the parsed decision from the EXACT hash-
    # verified bound JSON file (the single source of truth) so step 6b can require
    # the supplied in-memory ``decision`` argument to be canonical deep-equal to
    # it across EVERY field — not just the deterministic core.
    if manifest.get("evidence_pair") is not None:
        if decision_json_path is None:
            raise ReplayMismatch(
                "manifest binds evidence_pair but decision_json_path is required "
                "(the evidence pair is NOT optional for a tracked decision replay)"
            )
        if decision_md_path is None:
            raise ReplayMismatch(
                "manifest binds evidence_pair but decision_md_path is required "
                "(the evidence pair is NOT optional for a tracked decision replay)"
            )
        bound_decision = verify_evidence_pair(
            manifest,
            decision_json_path=decision_json_path,
            decision_md_path=decision_md_path,
        )
        checks.append("evidence_pair_json_md_hashes_and_id_match")

        # 6b. Bound-object deep equality (gap: supplied decision == bound JSON
        # file). The supplied in-memory ``decision`` MUST be canonical deep-equal
        # to the bound file object — covering EVERY field (selection,
        # task_2_17_handoff, evidence metadata, accounting, limitations, flags,
        # timestamps, future additions), not just the deterministic core. This
        # blocks the direct attack of mutating the in-memory decision (e.g.
        # selection.production_activated = True) while passing the ORIGINAL bound
        # JSON/Markdown paths. Two divergent representations are never trusted.
        _check(
            bound_decision == decision
            and _sha256_json(bound_decision) == _sha256_json(decision),
            "decision_object_matches_bound_json_full_deep_equality",
        )

    return {
        "provider_calls_attempted": 0,
        "network_calls": 0,
        "zero_provider_calls": True,
        "zero_network_calls": True,
        "fail_closed_on_miss": True,
        "winner_reproduced": derived_name,
        "eligible_ranking_reproduced": ranked,
        "checks_passed": checks,
        "n_checks": len(checks),
        "note": (
            "pure offline evidence/cache replay; no transport was instantiated and "
            "neither cache file was opened for write or compacted"
        ),
    }


# ---------------------------------------------------------------------------
# Future authorized freeze architecture (not executed in this offline task)
# ---------------------------------------------------------------------------


def raw_frozen_snapshot_identity_input(cohort_counts: Mapping[str, Any]) -> str:
    """Deterministic preimage for the raw private frozen-snapshot content hash.

    A future authorized ``freeze`` run writes the raw private frozen inputs
    (the exact cohort rows + per-candidate chunk texts + identity-to-chunk map)
    to a private (gitignored) location BEFORE any provider call, and records
    this content hash. ``evaluate`` and ``replay`` then load that exact
    snapshot and fail closed on a hash mismatch. This function is the contract
    for that binding; the network freeze path is NOT executed in this offline
    correction.
    """

    return _sha256_json(dict(cohort_counts))
