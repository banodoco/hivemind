"""Deterministic, representation-aware chunking for the embedding lifecycle.

Plan task 2.6 (completion signal: "Repeated runs yield identical chunks; long
prose and generated-Python golden cases can hit later chunks"). This module is
the embedding-side bridge between the canonical representations (task 2.5,
:mod:`executors.canonical_representations`) and the shared
``content_embeddings`` index (schema/022). It turns one canonical representation
into a deterministic list of chunks that are ready to hash, embed, and store.

Design rules (frozen, plan AD-5 + the S1 handoff):

* **One chunker, one hash.** Chunk *texts* come from the frozen task-0.8
  chunkers (:func:`wr.chunk_prose` / :func:`wr.chunk_python`); chunk *hashes*
  come from the frozen :func:`wr.chunk_hash`. Repeated runs over the same
  representation + config are byte-identical, so a chunk's identity
  ``(representation_hash, chunk_hash, chunk_index)`` is stable across runs and
  contract versions (the version fields live on the contract, task 2.3).
* **Offsets are recorded, never silently truncated.** Every chunk carries
  best-effort character + line offsets into the normalized source so a matched
  chunk can be explained/snippeted. Overlap means offsets are *anchors*, not a
  strict partition (mirrors the lexical lane's ``lexical_documents`` contract);
  :func:`wr.coverage_ok` is the hard no-silent-truncation guard.
* **Identity is preserved.** Each chunk carries the immutable
  ``(entity_type, item_id)`` and its ``representation_type`` (prose |
  workflow_python) plus the representation-level ``representation_hash``. Chunk
  collapse and deletion key on ``(entity_type, item_id)`` (plan AD-2).
* **Best chunk per item wins.** :func:`select_best_chunk` /
  :func:`collapse_by_item` deterministically pick one best chunk per item across
  representation types and chunk indexes, proving a later prose/code chunk can be
  the best hit (the 2.6 signal). The semantic candidate SQL (task 2.15) performs
  the same collapse at query time; this is the tested selection primitive.
* **No double embedding, no quarantined Python.** This module receives only
  ``safe`` representations from :func:`build_representations`; quarantined
  workflow Python never reaches it (task 0.8 §7). A workflow emits prose and
  workflow_python as SEPARATE representation streams, each with its own
  ``chunk_index`` sequence starting at 0 — the code is never duplicated.

Pure stdlib, offline, dependency-free. No DB, no provider, no stored-Python
execution. The frozen reference chunkers are reused verbatim so this layer adds
*offsets + identity + selection*, not a second chunking algorithm.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from executors import canonical_representations as canon
from executors import entity_identity as ei
from executors import workflow_representation as wr
from executors.workflow_representation import (  # version + vocab re-exports
    CHARS_PER_TOKEN,
    CHUNKING_VERSION,
    CHUNK_CONFIGS,
    REP_PROSE,
    REP_WORKFLOW_PYTHON,
)

__all__ = [
    "EmbeddingChunk",
    "ChunkingConfig",
    "DEFAULT_PROSE_CONFIG",
    "DEFAULT_PYTHON_CONFIG",
    "CHUNK_CONFIGS",
    "RepresentationManifestEntry",
    "chunk_representation",
    "chunk_representations",
    "chunk_manifest",
    "build_representation_manifest",
    "manifest_entries_equal",
    "select_best_chunk",
    "collapse_by_item",
    "coverage_ok",
    "config_for_representation",
]


# ---------------------------------------------------------------------------
# Configs (pilot candidates; production choice is task 2.14)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkingConfig:
    """A frozen chunking configuration (target/overlap in the chars/4 token estimate).

    Mirrors the pilot ``CHUNK_CONFIGS`` entries. The exact size is an evaluation
    parameter (plan AD-5); the config is part of the embedding contract's
    ``chunking_version`` axis, so changing the alignment/budget/overlap rule is a
    contract bump, not a silent edit.
    """

    representation_type: str
    target_tokens: int
    overlap_tokens: int

    def __post_init__(self) -> None:
        if self.representation_type not in (REP_PROSE, REP_WORKFLOW_PYTHON):
            raise ValueError(
                f"representation_type must be {REP_PROSE!r} or {REP_WORKFLOW_PYTHON!r}, "
                f"got {self.representation_type!r}"
            )
        if self.target_tokens < 1:
            raise ValueError("target_tokens must be positive")
        if self.overlap_tokens < 0:
            raise ValueError("overlap_tokens must be non-negative")
        if self.overlap_tokens >= self.target_tokens:
            # Overlap >= target would stall progress; reject rather than silently loop.
            raise ValueError("overlap_tokens must be < target_tokens")

    @property
    def overlap_chars(self) -> int:
        return self.overlap_tokens * CHARS_PER_TOKEN


#: Default prose config (pilot). Paragraph-aware packing with modest overlap.
DEFAULT_PROSE_CONFIG = ChunkingConfig(REP_PROSE, target_tokens=512, overlap_tokens=50)

#: Default workflow-Python config (pilot). AST-aligned with a bounded fallback.
DEFAULT_PYTHON_CONFIG = ChunkingConfig(
    REP_WORKFLOW_PYTHON, target_tokens=512, overlap_tokens=50
)


def config_for_representation(representation_type: str) -> ChunkingConfig:
    """Return the default pilot :class:`ChunkingConfig` for a representation type."""

    if representation_type == REP_WORKFLOW_PYTHON:
        return DEFAULT_PYTHON_CONFIG
    return DEFAULT_PROSE_CONFIG


# ---------------------------------------------------------------------------
# EmbeddingChunk — a chunk ready to store in content_embeddings (schema/022)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmbeddingChunk:
    """One deterministic chunk of a canonical representation.

    Maps one-to-one onto a ``content_embeddings`` row (schema/022): the identity
    fields ``(entity_type, item_id, representation_type, chunk_index)`` plus the
    two frozen hashes (``representation_hash`` for representation-level freshness,
    ``chunk_hash`` for chunk-level reuse). ``char_start``/``char_end``/
    ``start_line``/``end_line`` are best-effort anchors into the normalized
    source; ``-1`` means the anchor could not be located (observable, never a
    silent drop). ``method`` is the frozen chunker's method tag.
    """

    entity_type: str
    item_id: str
    representation_type: str
    chunk_index: int
    text: str
    chunk_hash: str
    representation_hash: str
    method: str
    token_estimate: int
    char_start: int
    char_end: int
    start_line: int
    end_line: int


# ---------------------------------------------------------------------------
# Offset location (best-effort anchors; overlap-safe, monotonic)
# ---------------------------------------------------------------------------


def _line_start_indices(normalized: str) -> list[int]:
    """Char index of the start of each line (0-based line numbers)."""

    starts = [0]
    for i, ch in enumerate(normalized):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _line_at(line_starts: list[int], char_index: int) -> int:
    """1-based line number containing ``char_index`` (bisect, no import needed)."""

    if char_index < 0:
        return 0
    line = 1
    for start in line_starts:
        if start <= char_index:
            line += 1
        else:
            break
    return line


def _compute_base_offsets(
    normalized: str, base_texts: list[str]
) -> list[tuple[int, int, int, int]]:
    """Return ``(char_start, char_end, start_line, end_line)`` per BASE pack.

    Offsets are anchored on the NON-overlapped partition (overlap=0), whose packs
    are contiguous substrings of the normalized source (paragraph packs are
    ``"\\n\\n"``-joined consecutive paragraphs; code packs are consecutive
    top-level blocks / windows). Contiguous substrings locate reliably with an
    advancing cursor, so anchors are monotonic and exact for the base pack.

    A stored chunk with overlap prepends a tail from the previous pack, so the
    chunk's *text* is ``tail + basepack`` and the base pack is its suffix; the
    anchor therefore points at the chunk's primary (new) content, which is what a
    snippet/explanation wants. A pack that cannot be located (pathological
    repeated content) yields ``(-1, -1, 0, 0)`` — observable, never a silent
    truncation. The hard coverage check is :func:`coverage_ok`.
    """

    if not normalized:
        return [(-1, -1, 0, 0)] * len(base_texts)
    line_starts = _line_start_indices(normalized)
    offsets: list[tuple[int, int, int, int]] = []
    cursor = 0
    for text in base_texts:
        if not text:
            offsets.append((-1, -1, 0, 0))
            continue
        idx = normalized.find(text, cursor)
        if idx < 0:
            idx = normalized.find(text)  # full-scan fallback preserves determinism
        if idx < 0:
            offsets.append((-1, -1, 0, 0))
            continue
        start = idx
        end = idx + len(text)
        offsets.append(
            (start, end, _line_at(line_starts, start), _line_at(line_starts, max(start, end - 1)))
        )
        cursor = end
    return offsets


# ---------------------------------------------------------------------------
# Chunking a representation / a whole item
# ---------------------------------------------------------------------------


def chunk_representation(
    rep: canon.CanonicalRepresentation,
    *,
    target_tokens: int | None = None,
    overlap_tokens: int | None = None,
) -> list[EmbeddingChunk]:
    """Chunk one canonical representation deterministically.

    Dispatches to the frozen prose/code chunker by ``representation_type``,
    records offsets into the normalized source, and stamps each chunk with the
    representation's immutable identity + frozen hashes. ``chunk_index`` is
    0-based and scoped per representation stream (prose and workflow_python each
    start at 0 for the same item). An empty/unavailable representation yields no
    chunks (nothing to embed).
    """

    if not rep.text or not rep.text.strip():
        return []
    cfg = config_for_representation(rep.representation_type)
    target = cfg.target_tokens if target_tokens is None else target_tokens
    overlap = cfg.overlap_tokens if overlap_tokens is None else overlap_tokens
    if overlap >= target:
        overlap = max(0, target - 1)

    if rep.representation_type == REP_WORKFLOW_PYTHON:
        frozen = wr.chunk_python(rep.text, target_tokens=target, overlap_tokens=overlap)
        base = wr.chunk_python(rep.text, target_tokens=target, overlap_tokens=0)
    else:
        frozen = wr.chunk_prose(rep.text, target_tokens=target, overlap_tokens=overlap)
        base = wr.chunk_prose(rep.text, target_tokens=target, overlap_tokens=0)
    if not frozen:
        return []

    normalized = wr.normalize_for_hash(rep.text)
    # Offsets anchor on the non-overlapped partition (contiguous substrings); the
    # frozen chunker's overlap prepends a tail, keeping base packs 1:1 with the
    # final chunks, so we map base offsets onto final chunks by index.
    base_offsets = _compute_base_offsets(normalized, [c.text for c in base])

    out: list[EmbeddingChunk] = []
    for idx, fchunk in enumerate(frozen):
        cstart, cend, sline, eline = (
            base_offsets[idx] if idx < len(base_offsets) else (-1, -1, 0, 0)
        )
        out.append(
            EmbeddingChunk(
                entity_type=rep.entity_type,
                item_id=rep.item_id,
                representation_type=rep.representation_type,
                chunk_index=idx,
                text=fchunk.text,
                chunk_hash=fchunk.hash,
                representation_hash=rep.representation_hash,
                method=fchunk.method,
                token_estimate=fchunk.token_estimate,
                char_start=cstart,
                char_end=cend,
                start_line=sline,
                end_line=eline,
            )
        )
    return out


def chunk_representations(
    reps: Iterable[canon.CanonicalRepresentation],
    *,
    target_tokens: int | None = None,
    overlap_tokens: int | None = None,
) -> list[EmbeddingChunk]:
    """Chunk every representation of an item; each stream keeps its own chunk_index.

    For a workflow this yields prose chunks (chunk_index 0..n) and workflow_python
    chunks (chunk_index 0..m) for the same ``(entity_type, item_id)`` — two
    representation streams, one item, code never duplicated. Ordering is stable:
    representations in the order :func:`build_representations` emits them (prose
    before workflow_python), chunks in source order within each stream.
    """

    chunks: list[EmbeddingChunk] = []
    for rep in reps:
        chunks.extend(
            chunk_representation(rep, target_tokens=target_tokens, overlap_tokens=overlap_tokens)
        )
    return chunks


def coverage_ok(text: str, chunks: Iterable[EmbeddingChunk]) -> bool:
    """Re-export the frozen no-silent-truncation guard over :class:`EmbeddingChunk`."""

    return wr.coverage_ok(text, [wr.Chunk(0, c.text, c.chunk_hash, 0, 0, c.method, 0) for c in chunks])


# ---------------------------------------------------------------------------
# Best-chunk selection (deterministic collapse) — the 2.6 signal
# ---------------------------------------------------------------------------


def _collapse_key(chunk: EmbeddingChunk) -> tuple[str, str]:
    return (chunk.entity_type, chunk.item_id)


def select_best_chunk(
    scored: Iterable[tuple[EmbeddingChunk, float]],
) -> EmbeddingChunk | None:
    """Return the single best chunk across a scored set (one item assumed).

    Highest score wins; ties break by ``representation_type`` (prose before
    workflow_python) then ``chunk_index`` ascending — a deterministic, stable
    choice. This is the primitive the semantic candidate SQL (task 2.15) mirrors
    when it collapses to one best chunk per item across representation types.
    Returns ``None`` for an empty input.
    """

    best_chunk: EmbeddingChunk | None = None
    best_score: float | None = None
    for chunk, score in scored:
        key = (-score, 0 if chunk.representation_type == REP_PROSE else 1, chunk.chunk_index)
        if best_score is None or key < best_score:
            best_score = key
            best_chunk = chunk
    return best_chunk


def collapse_by_item(
    scored: Iterable[tuple[EmbeddingChunk, float]],
) -> dict[tuple[str, str], EmbeddingChunk]:
    """Collapse scored chunks to one best chunk per ``(entity_type, item_id)``.

    Across representation types AND chunk indexes, the best-scoring chunk per
    item wins (same deterministic tie-break as :func:`select_best_chunk`). This
    is the "one best chunk per item before RRF" rule (plan AD-2) made testable:
    a long resource or a workflow with prose + code cannot gain an unfair
    advantage merely by having more chunks.
    """

    best: dict[tuple[str, str], tuple[tuple[float, int, int], EmbeddingChunk]] = {}
    for chunk, score in scored:
        k = _collapse_key(chunk)
        rank = (-score, 0 if chunk.representation_type == REP_PROSE else 1, chunk.chunk_index)
        if k not in best or rank < best[k][0]:
            best[k] = (rank, chunk)
    return {k: v[1] for k, v in best.items()}


# ---------------------------------------------------------------------------
# content_embeddings row manifest (the shape the worker/backfill writes)
# ---------------------------------------------------------------------------


def chunk_manifest(
    chunks: Iterable[EmbeddingChunk],
    contract_id: int,
    *,
    chunk_text_bound: int = 1024,
    text_truncator: Callable[[str, int], str] | None = None,
) -> list[dict[str, Any]]:
    """Render chunks as ``content_embeddings``-shaped row dicts (schema/022).

    The stored ``chunk_text`` is bounded (defensive; the chunker already targets
    a token budget) and the vector is left to the embedder. ``chunk_hash`` is the
    authoritative frozen hash of the *full* chunk text (not the bounded copy), so
    chunk identity/reuse is unaffected by the storage bound. ``contract_id`` is
    the active contract the rows are filed under (task 2.3).
    """

    truncate = text_truncator or (lambda t, n: t if len(t) <= n else t[:n])
    rows: list[dict[str, Any]] = []
    for c in chunks:
        rows.append(
            {
                "contract_id": contract_id,
                "entity_type": c.entity_type,
                "item_id": c.item_id,
                "representation_type": c.representation_type,
                "chunk_index": c.chunk_index,
                "chunk_text": truncate(c.text, chunk_text_bound),
                "embedding": None,  # filled by the embedder; vector(384)
                "representation_hash": c.representation_hash,
                "chunk_hash": c.chunk_hash,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Canonical representation manifest (task 2.17 — schema/034 source of truth)
# ---------------------------------------------------------------------------
#
# ONE producer of chunk identity for both the historical backfill and the
# incremental worker. The worker payload (schema/034 hivemind_embedding_payload)
# reads ``content_representation_manifest`` rows built here, so backfill and
# worker embed byte-identical chunks by construction (no second chunker to
# diverge). Quarantined/unavailable workflow Python is a STATE-ONLY row here —
# its source never crosses the provider/vector boundary, but its state is
# recorded so finalize/cleanup can DROP stale vectors deterministically.


def _selected_chunk_config_identity() -> str:
    """The frozen selected chunk-config identity (imported lazily to keep this
    module's import graph unchanged for callers that do not need the contract)."""

    from executors.selected_contract import SELECTED_CHUNK_CONFIG_IDENTITY

    return SELECTED_CHUNK_CONFIG_IDENTITY


@dataclass(frozen=True)
class RepresentationManifestEntry:
    """One canonical representation manifest row (schema/034).

    ``chunks`` carries the full chunk texts + frozen hashes (the bytes the worker
    embeds); the stored ``content_embeddings.chunk_text`` is bounded separately.
    A quarantined/unavailable workflow-Python row has ``chunks=()`` and records
    only its state + a bounded reason code (never the source or matched value).
    """

    entity_type: str
    item_id: str
    representation_type: str
    representation_hash: str
    source_available: bool
    public_state: str
    unavailable_reason: str | None
    chunking_version: int
    chunk_config_identity: str
    chunk_method: str | None
    chunks: tuple[dict[str, Any], ...]

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "item_id": self.item_id,
            "representation_type": self.representation_type,
            "representation_hash": self.representation_hash,
            "source_available": self.source_available,
            "public_state": self.public_state,
            "unavailable_reason": self.unavailable_reason,
            "chunking_version": self.chunking_version,
            "chunk_config_identity": self.chunk_config_identity,
            "chunk_method": self.chunk_method,
            "chunks": [dict(c) for c in self.chunks],
            "chunk_count": self.chunk_count,
        }


def _manifest_entry_from_rep(
    rep: canon.CanonicalRepresentation,
    *,
    target_tokens: int,
    overlap_tokens: int,
) -> RepresentationManifestEntry:
    chunks = chunk_representation(
        rep, target_tokens=target_tokens, overlap_tokens=overlap_tokens
    )
    chunk_dicts = tuple(
        {
            "chunk_index": c.chunk_index,
            "chunk_text": c.text,  # FULL chunk text (worker embeds this)
            "chunk_hash": c.chunk_hash,
            "method": c.method,
        }
        for c in chunks
    )
    methods = sorted({c.method for c in chunks}) if chunks else []
    return RepresentationManifestEntry(
        entity_type=rep.entity_type,
        item_id=rep.item_id,
        representation_type=rep.representation_type,
        representation_hash=rep.representation_hash,
        source_available=bool((rep.text or "").strip()),
        public_state=rep.public_state,
        unavailable_reason=None,
        chunking_version=wr.CHUNKING_VERSION,
        chunk_config_identity=_selected_chunk_config_identity(),
        chunk_method="+".join(methods) if methods else None,
        chunks=chunk_dicts,
    )


def build_representation_manifest(
    row: dict[str, Any],
    entity_type: str,
    *,
    item_id: Any = None,
    recoverer: Callable[[dict[str, Any]], "str | None"] | None = None,
    prose_target_tokens: int = DEFAULT_PROSE_CONFIG.target_tokens,
    prose_overlap_tokens: int = DEFAULT_PROSE_CONFIG.overlap_tokens,
    python_target_tokens: int = DEFAULT_PYTHON_CONFIG.target_tokens,
    python_overlap_tokens: int = DEFAULT_PYTHON_CONFIG.overlap_tokens,
) -> list[RepresentationManifestEntry]:
    """Build the canonical representation manifest entries for one source row.

    Yields one entry per representation: prose for messages/distillations/non-
    workflow resources; prose + workflow_python for workflows. Safe workflow
    Python carries its chunks; quarantined/unavailable Python is a STATE-ONLY
    entry (no chunks, no source) so the worker/finalize/cleanup can DROP stale
    vectors without ever embedding or persisting the unsafe source.

    ``item_id`` is snowflake-stringified. The selected (512/50) config is the
    default; a non-selected config raises (the selected contract is the only one
    the 034 payload will serve).
    """

    if entity_type not in ei.ENTITY_TYPES:
        raise ValueError(f"entity_type must be one of {ei.ENTITY_TYPES}, got {entity_type!r}")
    for label, target, overlap in (
        ("prose", prose_target_tokens, prose_overlap_tokens),
        ("python", python_target_tokens, python_overlap_tokens),
    ):
        if overlap >= target:
            raise ValueError(f"{label} overlap_tokens must be < target_tokens")

    row = row or {}
    string_id = ei.stringify_item_id(item_id) if item_id is not None else str(row.get("item_id") or "")
    reps = canon.build_representations(row, entity_type, item_id=item_id, recoverer=recoverer)
    by_rep = {r.representation_type: r for r in reps}

    entries: list[RepresentationManifestEntry] = []
    prose_rep = by_rep.get(REP_PROSE)
    if prose_rep is not None:
        entries.append(
            _manifest_entry_from_rep(
                prose_rep, target_tokens=prose_target_tokens, overlap_tokens=prose_overlap_tokens
            )
        )

    kind = (row.get("kind") or "").strip()
    if entity_type == ei.ENTITY_RESOURCE and kind == "workflow":
        if REP_WORKFLOW_PYTHON in by_rep:
            entries.append(
                _manifest_entry_from_rep(
                    by_rep[REP_WORKFLOW_PYTHON],
                    target_tokens=python_target_tokens,
                    overlap_tokens=python_overlap_tokens,
                )
            )
        else:
            # Quarantined or unavailable Python: state-only entry (no chunks,
            # no source/hash). The reason is a bounded code, never the value.
            status = canon.workflow_python_status(row, recoverer=recoverer)
            if not status.available:
                reason = "python_unavailable"
                state = wr.PUBLIC_STATE_SAFE
                available = False
            else:
                # available but not has_representation -> quarantined
                reason = "quarantined_python"
                state = status.public_state
                available = True
            entries.append(
                RepresentationManifestEntry(
                    entity_type=ei.ENTITY_RESOURCE,
                    item_id=string_id,
                    representation_type=REP_WORKFLOW_PYTHON,
                    representation_hash="",
                    source_available=available,
                    public_state=state,
                    unavailable_reason=reason,
                    chunking_version=wr.CHUNKING_VERSION,
                    chunk_config_identity=_selected_chunk_config_identity(),
                    chunk_method=None,
                    chunks=(),
                )
            )
    return entries


def manifest_entries_equal(
    a: Iterable[RepresentationManifestEntry],
    b: Iterable[RepresentationManifestEntry],
) -> bool:
    """Byte-identity of two manifest entry lists (the task-2.17 parity check).

    Two manifests are equal iff they have the same entries (by representation)
    with identical representation_hash, source_available, public_state,
    chunk_count, chunk indexes/texts/hashes/method. Used to prove the historical
    backfill and the incremental worker emit byte-identical chunks.
    """

    def key(e: RepresentationManifestEntry) -> tuple[str, str]:
        return (e.representation_type, e.item_id or "")

    am = {key(e): e for e in a}
    bm = {key(e): e for e in b}
    if set(am) != set(bm):
        return False
    for k in am:
        x, y = am[k], bm[k]
        if (x.representation_hash != y.representation_hash
                or x.source_available != y.source_available
                or x.public_state != y.public_state
                or x.chunk_count != y.chunk_count
                or x.chunk_method != y.chunk_method):
            return False
        if x.chunks != y.chunks:
            return False
    return True
