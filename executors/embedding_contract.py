"""Hivemind embedding provider contract (plan tasks 2.1, 2.3, 2.5).

This module is the **single source of truth** for the embedding provider
interface, the deterministic offline embedder, the OpenAI embedder, vector
validation, query normalization, and content hashing. It is the shared
foundation that tasks 2.1–2.5 build on, so that the embedding *contract*,
identity boundary, representation hashing, and schema vocabulary are resolved
once and reused everywhere (plan AD-2, AD-8).

It ports Pumpernickel's small retrieval algorithms with attribution and fresh
Hivemind tests (plan AD-8); it creates **no runtime dependency** on Pumpernickel.
The ported logic lives in:

- ``app/services/embeddings.py`` — provider interface, deterministic fake
  embedder, OpenAI embedder, ``normalize_vector``/``validate_vectors``,
  ``content_hash``.
- ``app/services/retrieval.py`` — ``normalize_query_for_embedding`` and the
  best-effort query-embedding cache/timeout shape.

**One hash, one normalization, one set of version constants.** The frozen
canonical-text normalization, the SHA-256 representation/chunk hash, and the
``CANONICALIZATION_VERSION`` / ``CHUNKING_VERSION`` / ``SECRET_SCAN_VERSION``
constants live in :mod:`executors.workflow_representation` (the task-0.8 frozen
reference). This module re-uses them rather than redefining a second contract,
which is why every representation and chunk hash in Hivemind — lexical,
embedding, and snippet — derives from one algorithm.

**Pure stdlib.** The network boundary is injectable: :class:`OpenAIEmbedder`
takes an optional ``transport`` callable and reads its key from the constructor
or an environment variable, so every normal test runs fully offline and
deterministically. The optional ``openai`` package is never imported; Hivemind
stays stdlib-only (AGENTS.md).

**Secret boundary.** No credential, vector derived from private content, or
secret-like substring is ever printed. The approved provider key is read from
Hivemind's server-side secret store only at the one authorized smoke boundary
(plan task 2.1, §Credentials); if that key is absent the live smoke is recorded
as a gated deployment action, never worked around by adding another key.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import unicodedata
import urllib.error
import urllib.request
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable

# One source of truth: the frozen task-0.8 reference contract owns normalization,
# hashing, and every version constant. Re-export them so callers import from one
# place and so a contract bump in one module propagates everywhere.
from executors.workflow_representation import (  # noqa: F401  (re-exported API)
    CANONICALIZATION_VERSION,
    CHUNKING_VERSION,
    SECRET_SCAN_VERSION,
    DELIMITER_GRAMMAR_VERSION,
    SEMANTICS_PROJECTION_VERSION,
    REP_PROSE,
    REP_WORKFLOW_PYTHON,
    REPRESENTATION_TYPES,
    chunk_hash,
    normalize_for_hash,
    representation_hash,
)

__all__ = [
    "EmbeddingError",
    "Embedder",
    "DeterministicFakeEmbedder",
    "OpenAIEmbedder",
    "Transport",
    "DEFAULT_OPENAI_EMBEDDING_MODEL",
    "PILOT_DIMENSION_384",
    "PILOT_DIMENSION_1536",
    "DEFAULT_QUERY_EMBEDDING_TIMEOUT_S",
    "DEFAULT_QUERY_CACHE_TTL_S",
    "DEFAULT_QUERY_CACHE_MAX_ENTRIES",
    "normalize_vector",
    "validate_vectors",
    "normalize_query_for_embedding",
    "content_hash",
    "canonical_content_hash",
    "ContractSpec",
    "contract_identity_input",
    "contract_id",
    "EmbeddingContract",
    "CONTRACT_STATUS_DRAFT",
    "CONTRACT_STATUS_ACTIVE",
    "CONTRACT_STATUS_SUPERSEDED",
    "CONTRACT_STATUSES",
    # Re-exported frozen vocabulary / versions
    "CANONICALIZATION_VERSION",
    "CHUNKING_VERSION",
    "SECRET_SCAN_VERSION",
    "DELIMITER_GRAMMAR_VERSION",
    "SEMANTICS_PROJECTION_VERSION",
    "REP_PROSE",
    "REP_WORKFLOW_PYTHON",
    "REPRESENTATION_TYPES",
    "representation_hash",
    "chunk_hash",
    "normalize_for_hash",
]


# ---------------------------------------------------------------------------
# Provider vocabulary (frozen pilot candidates — plan AD-2, task 0.8 §9)
# ---------------------------------------------------------------------------

#: Hosted embedding model used by the one authorized provider path. The
#: ``dimensions`` request parameter selects the pilot dimension below.
DEFAULT_OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"

#: Capacity-preferred pilot dimension (task 0.7: full corpus 4.59 GB, PASS the
#: 12 GB gate). The production table is built at this dimension; the pilot
#: quality fallback and the production choice are plan task 2.14.
PILOT_DIMENSION_384 = 384

#: Quality-fallback pilot dimension (task 0.7: full corpus 16.4 GB, FAIL the
#: 12 GB gate at full scope; viable at pilot/cohort scope). A dimension
#: migration uses a sibling fixed-dimension table + HNSW index + atomic
#: active-contract switch (plan AD-2, task 0.8 §9) — never mixed into the 384
#: table.
PILOT_DIMENSION_1536 = 1536

#: Short query-embedding timeout matching Pumpernickel's ~400 ms policy so a
#: slow provider degrades to lexical retrieval (plan AD-7, task 0.8 §9).
DEFAULT_QUERY_EMBEDDING_TIMEOUT_S = 0.4

#: Best-effort per-instance query-embedding cache. Edge instances do not share
#: memory, so this is deliberately modest (plan AD-7, risk mitigation).
DEFAULT_QUERY_CACHE_TTL_S = 600
DEFAULT_QUERY_CACHE_MAX_ENTRIES = 1024

#: Embedding-contract lifecycle status (plan AD-2). At most one ``active``
#: contract per dimension; superseded contracts are retained for diagnosis.
CONTRACT_STATUS_DRAFT = "draft"
CONTRACT_STATUS_ACTIVE = "active"
CONTRACT_STATUS_SUPERSEDED = "superseded"
CONTRACT_STATUSES: tuple[str, ...] = (
    CONTRACT_STATUS_DRAFT,
    CONTRACT_STATUS_ACTIVE,
    CONTRACT_STATUS_SUPERSEDED,
)


# ---------------------------------------------------------------------------
# Content hashing — ONE algorithm, frozen in workflow_representation
# ---------------------------------------------------------------------------


def content_hash(text: str) -> str:
    """Return the canonical SHA-256 hash for already-canonical embedding text.

    This is the same algorithm as
    :func:`executors.workflow_representation.representation_hash` (frozen in the
    task-0.8 reference). Hivemind has exactly one content-hash algorithm; this
    thin alias exists so callers that think in terms of "the embedding content
    hash" resolve to the single source of truth rather than a second definition.
    """

    return representation_hash(text)


def canonical_content_hash(text: str) -> str:
    """Hash of :func:`content_hash` after the frozen normalization is applied.

    Equivalent to ``representation_hash``; kept as a named entry point for
    callers that build raw text and want normalization + hashing in one step.
    """

    return representation_hash(text)


# ---------------------------------------------------------------------------
# Vector validation + L2 normalization (ported from Pumpernickel embeddings.py)
# ---------------------------------------------------------------------------


class EmbeddingError(RuntimeError):
    """Raised when an embedder cannot satisfy the shared embedding contract."""


def normalize_vector(vector: Sequence[float], *, dimension: int) -> list[float]:
    """Validate dimension and finiteness, then return an L2-normalized vector.

    Ported from Pumpernickel's ``embeddings.normalize_vector``. A wrong
    dimension, a non-finite value, or an all-zero vector raises ``ValueError``;
    the embedding worker treats that as a fail-closed skip + count + retry
    (plan AD-7, task 0.8 §9), so the shared index never stores a malformed or
    wrong-dimension vector.
    """

    values = [float(value) for value in vector]
    if len(values) != dimension:
        raise ValueError(
            f"embedding dimension mismatch: expected {dimension}, got {len(values)}"
        )
    if any(not math.isfinite(value) for value in values):
        raise ValueError("embedding vector contains non-finite values")
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0.0:
        raise ValueError("embedding vector must not be all zeros")
    return [value / norm for value in values]


def validate_vectors(
    vectors: Sequence[Sequence[float]], *, dimension: int
) -> list[list[float]]:
    """Normalize and validate a batch of vectors (ported from Pumpernickel)."""

    return [normalize_vector(vector, dimension=dimension) for vector in vectors]


# ---------------------------------------------------------------------------
# Query normalization (ported from Pumpernickel retrieval.py, frozen to match
# the Hivemind corpus canonicalization)
# ---------------------------------------------------------------------------


def normalize_query_for_embedding(query: str) -> str:
    """Return the stable query text used for the provider call and cache key.

    Pumpernickel's ``normalize_query_for_embedding`` is NFC + whitespace
    collapse. The frozen Hivemind contract (task 0.8 §9) requires the query to
    use the **same** normalization as corpus canonicalization, so query and
    corpus text are embedded over identically-normalized bytes and cosine
    distance is meaningful. We therefore route the query through the frozen
    :func:`normalize_for_hash` (NFC + CRLF/CR→LF + per-line trailing-whitespace
    strip) and additionally collapse internal whitespace runs, which keeps cache
    keys stable against incidental spacing without changing semantic content.

    Returns the empty string for a blank/whitespace-only query so the caller can
    skip embedding and run lexical retrieval (a blank query is not "degraded").
    """

    if query is None:
        return ""
    normalized = normalize_for_hash(query)
    # Collapse runs of whitespace to single spaces for a stable cache key. The
    # frozen normalization already stripped trailing whitespace per line.
    collapsed = " ".join(normalized.split())
    return collapsed


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------

#: Injectable network transport: ``(url, headers, body_dict) -> parsed_json``.
#: Tests inject a deterministic fake; production uses the stdlib default.
Transport = Callable[[str, "Mapping[str, str]", "Mapping[str, Any]"], "Mapping[str, Any]"]


@runtime_checkable
class Embedder(Protocol):
    """Async embedding provider interface used by retrieval, worker, and backfill.

    Every embedder exposes a stable ``model_name`` and ``dimension`` and returns
    one L2-normalized vector per input text, preserving order. The interface is
    async so a worker/backfill can batch provider calls without blocking, but the
    deterministic fake and the tests never touch the network.
    """

    model_name: str
    dimension: int

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one L2-normalized vector per input text, preserving order."""


# ---------------------------------------------------------------------------
# Deterministic fake embedder (offline, deterministic, no network)
# ---------------------------------------------------------------------------


class DeterministicFakeEmbedder:
    """Deterministic async embedder with no network and no model dependency.

    Ported from Pumpernickel's ``DeterministicFakeEmbedder``: a hashing
    bag-of-tokens projected into a fixed dimension and L2-normalized. Identical
    input text always yields an identical vector, so golden-set and contract
    tests are fully reproducible offline. The fake is the only embedder used by
    normal unit/contract tests (plan task 0.8 §9; AGENTS.md offline tests).
    """

    model_name = "deterministic-fake"

    def __init__(self, *, dimension: int = PILOT_DIMENSION_384) -> None:
        if dimension < 1:
            raise ValueError("dimension must be positive")
        self.dimension = dimension

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        canonical = normalize_for_hash(text).casefold()
        tokens = canonical.split() or [canonical]
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:8], "big") % self.dimension
            sign = 1.0 if digest[8] % 2 == 0 else -1.0
            vector[bucket] += sign
        return normalize_vector(vector, dimension=self.dimension)


# ---------------------------------------------------------------------------
# OpenAI embedder (stdlib urllib, injectable transport, lazy + secret-safe)
# ---------------------------------------------------------------------------

#: Default hosted embeddings endpoint. Overridable for tests / private deployments.
DEFAULT_OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"


def _stdlib_transport(
    url: str, headers: Mapping[str, str], body: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Default stdlib HTTP transport (urllib) for the OpenAI embeddings API.

    Raises ``EmbeddingError`` wrapping the underlying HTTP/transport failure so
    callers see a single error type. The body is serialized with sorted keys for
    a stable wire shape; the Authorization header is set by the caller and never
    logged here.
    """

    payload = json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers=dict(headers),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (provider endpoint)
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # Surface only the status and a generic reason — never the body, which
        # could echo the request/key context.
        raise EmbeddingError(f"OpenAI embeddings HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise EmbeddingError(f"OpenAI embeddings transport error: {type(exc).__name__}") from exc
    try:
        return json.loads(raw)  # type: ignore[no-any-return]
    except json.JSONDecodeError as exc:
        raise EmbeddingError("OpenAI embeddings returned non-JSON") from exc


class OpenAIEmbedder:
    """Hosted OpenAI embedder for ``text-embedding-3-small`` (stdlib, injectable).

    The credential is read from ``api_key`` (preferred) or the ``OPENAI_API_KEY``
    environment variable and is **never** stored beyond the instance, printed,
    or included in errors/metadata. The network call goes through an injectable
    :data:`Transport` so tests never touch the network and never need a key.

    No ``openai`` package is imported: Hivemind is stdlib-only. The default
    transport is :func:`_stdlib_transport`. Only the one authorized smoke path
    (plan task 2.1) supplies a real key through the server-side secret boundary;
    if that key is absent, the smoke is recorded as a gated deployment action.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model_name: str = DEFAULT_OPENAI_EMBEDDING_MODEL,
        dimension: int = PILOT_DIMENSION_384,
        timeout_s: float | None = None,
        url: str = DEFAULT_OPENAI_EMBEDDINGS_URL,
        transport: Transport | None = None,
    ) -> None:
        import os

        self.model_name = model_name
        self.dimension = dimension
        self._api_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY")
        self._timeout_s = timeout_s
        self._url = url
        self._transport: Transport = transport or _stdlib_transport

    def has_credential(self) -> bool:
        """Return whether a provider key is configured (never reveals the value).

        The live smoke gate checks this; the absence of a key is the recorded
        blocker for the one authorized provider call, not a reason to add one.
        """

        return bool(self._api_key)

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.has_credential():
            # Fail closed with a single, secret-free reason. Callers (query path)
            # catch this and degrade to lexical retrieval (plan AD-7).
            raise EmbeddingError("OpenAI embeddings requested without a configured key")
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        body: dict[str, Any] = {
            "model": self.model_name,
            "input": list(texts),
            "dimensions": self.dimension,
        }
        # The transport may raise EmbeddingError; let it propagate.
        response = self._transport(self._url, headers, body)
        data = response.get("data") if isinstance(response, Mapping) else None
        if not isinstance(data, list):
            raise EmbeddingError("OpenAI embeddings response missing 'data' array")
        ordered = sorted(data, key=lambda item: item.get("index", 0))
        vectors = [item.get("embedding") for item in ordered]
        if any(v is None for v in vectors) or len(vectors) != len(texts):
            raise EmbeddingError(
                f"OpenAI returned {len(vectors)} vectors for {len(texts)} inputs"
            )
        return validate_vectors(vectors, dimension=self.dimension)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Best-effort query-embedding cache (ported shape from Pumpernickel retrieval.py)
# ---------------------------------------------------------------------------

# Per-instance LRU with TTL. Edge instances do not share memory; this is a
# best-effort latency optimization only (plan AD-7 risk mitigation).
_QUERY_EMBEDDING_CACHE: "OrderedDict[tuple[str, str], tuple[float, list[float]]]" = (
    OrderedDict()
)


def _cache_lookup(
    model_name: str, normalized_query: str
) -> list[float] | None:
    key = (model_name, normalized_query)
    cached = _QUERY_EMBEDDING_CACHE.get(key)
    if cached is None:
        return None
    expires_at, vector = cached
    if expires_at < time.monotonic():
        _QUERY_EMBEDDING_CACHE.pop(key, None)
        return None
    _QUERY_EMBEDDING_CACHE.move_to_end(key)
    return list(vector)


def _cache_store(
    model_name: str,
    normalized_query: str,
    vector: list[float],
    *,
    ttl_s: int = DEFAULT_QUERY_CACHE_TTL_S,
    max_entries: int = DEFAULT_QUERY_CACHE_MAX_ENTRIES,
) -> None:
    if ttl_s <= 0 or max_entries <= 0:
        return
    key = (model_name, normalized_query)
    _QUERY_EMBEDDING_CACHE[key] = (time.monotonic() + ttl_s, list(vector))
    _QUERY_EMBEDDING_CACHE.move_to_end(key)
    while len(_QUERY_EMBEDDING_CACHE) > max_entries:
        _QUERY_EMBEDDING_CACHE.popitem(last=False)


def _cache_clear() -> None:
    """Test hook: clear the best-effort query-embedding cache."""

    _QUERY_EMBEDDING_CACHE.clear()


# ---------------------------------------------------------------------------
# Embedding contract identity (plan AD-2, task 0.8 §9)
# ---------------------------------------------------------------------------

#: Unit separator keeps provider/model/version fields unambiguous in the hash
#: preimage so two contracts cannot collide by string concatenation.
_CONTRACT_FIELD_SEP = "\x1f"


@dataclass(frozen=True)
class ContractSpec:
    """An embedding contract identity (plan AD-2).

    ``contract_id = provider + model + dimension + canonicalization_version +
    chunking_version``. Two specs with identical fields are the same contract and
    must share one ``embedding_contracts`` row and one integer id; this is what
    makes the backfill, worker, and SQL seeding agree without coordination.
    """

    provider: str
    model: str
    dimension: int
    canonicalization_version: int = CANONICALIZATION_VERSION
    chunking_version: int = CHUNKING_VERSION

    def __post_init__(self) -> None:
        if not self.provider or not self.model:
            raise ValueError("ContractSpec provider and model are required")
        if self.dimension < 1:
            raise ValueError("ContractSpec dimension must be positive")
        if self.canonicalization_version < 1 or self.chunking_version < 1:
            raise ValueError("ContractSpec versions must be positive")

    @property
    def identity_input(self) -> str:
        return contract_identity_input(self)

    @property
    def id(self) -> int:
        return contract_id(self)


def contract_identity_input(spec: ContractSpec | Mapping[str, Any]) -> str:
    """Return the exact, frozen preimage string hashed into a contract id.

    Field order and separators are part of the contract; changing them is a
    contract-identity change. Reproduced byte-for-byte by the SQL seeding
    (schema/021) via ``pgcrypto`` so Python and SQL derive the same bigint.
    """

    if isinstance(spec, ContractSpec):
        provider = spec.provider
        model = spec.model
        dimension = spec.dimension
        canon = spec.canonicalization_version
        chunk = spec.chunking_version
    else:
        provider = str(spec["provider"])
        model = str(spec["model"])
        dimension = int(spec["dimension"])
        canon = int(spec.get("canonicalization_version", CANONICALIZATION_VERSION))
        chunk = int(spec.get("chunking_version", CHUNKING_VERSION))
    return _CONTRACT_FIELD_SEP.join(
        (provider, model, str(dimension), str(canon), str(chunk))
    )


def contract_id(spec: ContractSpec | Mapping[str, Any]) -> int:
    """Return the deterministic bigint id for a contract spec.

    SHA-256 of :func:`contract_identity_input`, truncated to 63 bits so the id is
    a positive PostgreSQL ``bigint`` and is identical across Python and SQL. The
    value is a stable identifier, not security-sensitive.
    """

    preimage = contract_identity_input(spec).encode("utf-8")
    digest = hashlib.sha256(preimage).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF


@dataclass(frozen=True)
class EmbeddingContract:
    """A materialized embedding-contract row (mirrors ``embedding_contracts``)."""

    id: int
    provider: str
    model: str
    dimension: int
    canonicalization_version: int
    chunking_version: int
    status: str = CONTRACT_STATUS_DRAFT

    @classmethod
    def from_spec(cls, spec: ContractSpec, *, status: str = CONTRACT_STATUS_DRAFT) -> "EmbeddingContract":
        return cls(
            id=spec.id,
            provider=spec.provider,
            model=spec.model,
            dimension=spec.dimension,
            canonicalization_version=spec.canonicalization_version,
            chunking_version=spec.chunking_version,
            status=status,
        )

    def to_spec(self) -> ContractSpec:
        return ContractSpec(
            provider=self.provider,
            model=self.model,
            dimension=self.dimension,
            canonicalization_version=self.canonicalization_version,
            chunking_version=self.chunking_version,
        )


def pilot_contract_specs() -> list[ContractSpec]:
    """Return the frozen pilot contract candidates (task 0.8 §9).

    Both are ``text-embedding-3-small``; they differ only by dimension. The 384-d
    spec is capacity-preferred (PASS the 12 GB gate); the 1536-d spec is the
    quality fallback. Selecting the production dimension is plan task 2.14; this
    function only enumerates the candidates the schema/SQL may seed.
    """

    return [
        ContractSpec(
            provider="openai",
            model=DEFAULT_OPENAI_EMBEDDING_MODEL,
            dimension=PILOT_DIMENSION_384,
        ),
        ContractSpec(
            provider="openai",
            model=DEFAULT_OPENAI_EMBEDDING_MODEL,
            dimension=PILOT_DIMENSION_1536,
        ),
    ]
