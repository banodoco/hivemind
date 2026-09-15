"""Task 1.2 lexical-document helper — resource / distillation / workflow Python.

This module is the **bridge** between a Hivemind source row and the chunk-aware
lexical storage created by ``schema/003_lexical_resource_documents.sql``. It
reuses the FROZEN reference contracts without reinterpretation:

- ``executors.workflow_representation`` (task 0.8): authoritative Python
  precedence, body-delimiter extraction, no-duplication prose stripping, the
  deterministic secret scanner + quarantine, and the AST-aware code chunker with
  its ``coverage_ok`` no-silent-truncation guard.
- ``executors.lexical_contract`` (task 1.1): the canonical ``'simple'`` config,
  the frozen weighted tsvector expressions, the chunk identity, and the frozen
  pilot chunk target/overlap.

It is **pure stdlib, offline, and dependency-free**. It does not import a
database driver, does not connect anywhere, does not call a provider, and does
not import VibeComfy. The storage adapter is injected (a :class:`LexicalDocumentStore`)
so the precedence / dedup / quarantine / chunking / refresh / drop logic is fully
unit-testable offline with an in-memory store.

Security invariants (frozen 0.8 §7), enforced here:

- **Never execute stored Python.** Only ``ast.parse`` is used (by the frozen
  chunker) — parsing is not execution. The authoritative bytes are treated as
  inert corpus text.
- **Never log / serialize / expose a matched secret.** A scan hit yields only
  non-secret ``reason_code`` strings; the matched value, its offset, and its
  length never leave :func:`scan_secrets`. Quarantine writes a state row with
  reason codes and **zero** lexical documents, so the secret never reaches the
  searchable index, a snippet, or ``get_item``.
- **Fail closed on truncation.** If a representation is not fully covered by its
  chunk set, the refresh raises rather than dropping the tail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, Sequence

# Reuse the frozen contracts — single source of truth.
from executors import lexical_contract as LC
from executors import workflow_representation as WR

# Re-export the frozen versions so callers import them from one place.
LEXICALIZATION_VERSION = LC.LEXICALIZATION_VERSION
CANONICALIZATION_VERSION = WR.CANONICALIZATION_VERSION
CHUNKING_VERSION = WR.CHUNKING_VERSION
SECRET_SCAN_VERSION = WR.SECRET_SCAN_VERSION


class CoverageError(RuntimeError):
    """Raised when a Python representation is not fully covered by its chunks.

    This is the fail-closed no-silent-truncation guard (frozen 0.8 §10): the
    refresh refuses to proceed rather than head/tail-truncate searchable code.
    """


# ---------------------------------------------------------------------------
# Result dataclasses (1:1 with the lexical_documents / state table columns)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PythonDocument:
    """One ``workflow_python`` (or over-long prose) lexical document / chunk."""

    entity_type: str
    item_id: str
    representation_type: str
    chunk_index: int
    chunk_text: str
    chunk_hash: str
    representation_hash: str
    source_offset_start: int
    source_offset_end: int
    method: str
    matched_anchor: str            # <=240-char secret-redacted snippet anchor
    quarantine_state: str
    lexicalization_version: int
    canonicalization_version: int
    chunking_version: int
    secret_scan_version: int


@dataclass(frozen=True)
class PythonRepresentationState:
    """Per-resource authoritative-Python state (mirrors the state table)."""

    resource_id: str
    kind: str
    cohort: str                    # payload_python | body_python | recoverable | unavailable
    public_state: str              # safe | quarantined
    available: bool
    body_duplicate: bool
    delimiter: str | None
    derivation: str
    representation_hash: str | None
    secret_reason_codes: tuple[str, ...]   # NON-SECRET codes only
    chunk_count: int
    canonicalization_version: int
    secret_scan_version: int
    chunking_version: int


@dataclass(frozen=True)
class RefreshOutcome:
    """What a refresh did for one resource (for reporting / tests)."""

    item_id: str
    action: str                    # skip | upsert | quarantine | unavailable | drop
    cohort: str
    public_state: str
    chunk_count: int
    secret_reason_codes: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Representation computation (precedence / dedup / scan / chunk / quarantine)
# ---------------------------------------------------------------------------


def _payload_python(row: dict[str, Any]) -> str:
    """Non-empty ``payload.python_source`` if present, else ''."""
    payload = row.get("payload")
    if isinstance(payload, dict):
        value = payload.get("python_source")
        return value if isinstance(value, str) else ""
    return ""


def _chunk_offsets(normalized_source: str, chunks: Sequence[WR.Chunk]) -> list[tuple[int, int]]:
    """Best-effort char offsets of each chunk in the normalized source.

    Overlap means these are anchors, not a strict partition (documented on the
    table column). Deterministic: a forward scan from a monotonic cursor finds
    each chunk's earliest position at/after the cursor.
    """
    out: list[tuple[int, int]] = []
    cursor = 0
    for c in chunks:
        text = c.text
        pos = normalized_source.find(text, cursor)
        if pos < 0:
            pos = normalized_source.find(text)
        if pos < 0:
            pos = cursor
        out.append((pos, pos + len(text)))
        cursor = max(cursor, pos + max(1, len(text)))
    return out


def compute_workflow_python_documents(
    row: dict[str, Any],
) -> tuple[PythonRepresentationState, tuple[PythonDocument, ...]]:
    """Resolve one workflow row into authoritative-Python state + chunk docs.

    Pipeline (frozen precedence / security / chunking):

    1. ``resolve_authoritative_python`` (0.8): non-empty ``payload.python_source``
       → recognized body-delimited block → recoverable → unavailable.
    2. ``scan_secrets`` (0.8 safe policy): a hit => ``quarantined``; the value is
       never stored, only non-secret reason codes.
    3. Only ``safe`` + ``available`` Python is chunked (0.8 code chunker, pilot
       512 tok / 50 overlap) and indexed; ``coverage_ok`` is enforced (no silent
       truncation). Quarantined/unavailable Python yields **zero** documents.

    The body block is stripped from the resource's *prose* by the SQL helper
    ``hivemind_workflow_prose`` (mirrors ``strip_python_blocks``), so identical
    code in ``payload`` and ``body`` is indexed exactly once (as workflow_python).
    """
    item_id = str(row["id"])
    kind = str(row.get("kind") or "")
    resolution = WR.resolve_authoritative_python(row)  # recoverer=None
    py = resolution.source  # "" when unavailable

    findings = WR.scan_secrets(py) if py else []
    reason_codes = tuple(sorted({f.reason_code for f in findings}))
    public_state = WR.PUBLIC_STATE_QUARANTINED if findings else WR.PUBLIC_STATE_SAFE
    rep_hash = WR.representation_hash(py) if py else None

    documents: list[PythonDocument] = []
    if public_state == WR.PUBLIC_STATE_SAFE and resolution.available and py:
        chunks = WR.chunk_python(
            py,
            target_tokens=LC.WORKFLOW_PYTHON_CHUNK_TARGET_TOKENS,
            overlap_tokens=LC.WORKFLOW_PYTHON_CHUNK_OVERLAP_TOKENS,
        )
        if not WR.coverage_ok(py, chunks):
            # Fail closed: never silently truncate searchable Python.
            raise CoverageError(
                f"workflow_python for resource {item_id} is not fully covered by "
                f"its chunk set (no silent truncation)"
            )
        offsets = _chunk_offsets(WR.normalize_for_hash(py), chunks)
        for chunk, (start, end) in zip(chunks, offsets):
            documents.append(
                PythonDocument(
                    entity_type=LC.ENTITY_RESOURCE,
                    item_id=item_id,
                    representation_type=LC.REP_WORKFLOW_PYTHON,
                    chunk_index=chunk.index,
                    chunk_text=chunk.text,
                    chunk_hash=chunk.hash,
                    representation_hash=rep_hash or "",
                    source_offset_start=start,
                    source_offset_end=end,
                    method=chunk.method,
                    matched_anchor=WR.redact_snippet(chunk.text, window=LC.SNIPPET_WINDOW_CHARS),
                    quarantine_state=WR.PUBLIC_STATE_SAFE,
                    lexicalization_version=LEXICALIZATION_VERSION,
                    canonicalization_version=CANONICALIZATION_VERSION,
                    chunking_version=CHUNKING_VERSION,
                    secret_scan_version=SECRET_SCAN_VERSION,
                )
            )

    state = PythonRepresentationState(
        resource_id=item_id,
        kind=kind,
        cohort=resolution.cohort,
        public_state=public_state,
        available=resolution.available,
        body_duplicate=resolution.body_duplicate,
        delimiter=resolution.delimiter,
        derivation=resolution.derivation,
        representation_hash=rep_hash,
        secret_reason_codes=reason_codes,
        chunk_count=len(documents),
        canonicalization_version=CANONICALIZATION_VERSION,
        secret_scan_version=SECRET_SCAN_VERSION,
        chunking_version=CHUNKING_VERSION,
    )
    return state, tuple(documents)


# ---------------------------------------------------------------------------
# Storage adapter (injected) + refresh / drop logic
# ---------------------------------------------------------------------------


class LexicalDocumentStore(Protocol):
    """Minimal storage surface refresh_workflow_python needs.

    Implementations must make ``replace_all`` and ``drop`` atomic (delete then
    insert within one transaction). The in-memory implementation is for tests;
    the psql-backed implementation lives in the local test runner.
    """

    def get_state(self, item_id: str) -> PythonRepresentationState | None: ...

    def list_documents(
        self, item_id: str, representation_type: str = LC.REP_WORKFLOW_PYTHON
    ) -> list[PythonDocument]: ...

    def replace_all(
        self,
        state: PythonRepresentationState,
        documents: Sequence[PythonDocument],
    ) -> None:
        """Atomically replace this item's workflow_python state + documents."""

    def drop(self, item_id: str) -> None:
        """Remove this item's workflow_python state + documents."""


@dataclass
class InMemoryLexicalStore:
    """Deterministic in-memory store for offline unit tests."""

    states: dict[str, PythonRepresentationState] = field(default_factory=dict)
    documents: dict[tuple[str, str], list[PythonDocument]] = field(default_factory=dict)

    def get_state(self, item_id: str) -> PythonRepresentationState | None:
        return self.states.get(item_id)

    def list_documents(
        self, item_id: str, representation_type: str = LC.REP_WORKFLOW_PYTHON
    ) -> list[PythonDocument]:
        return list(self.documents.get((item_id, representation_type), []))

    def replace_all(
        self,
        state: PythonRepresentationState,
        documents: Sequence[PythonDocument],
    ) -> None:
        rep = LC.REP_WORKFLOW_PYTHON
        # Atomic delete-then-insert.
        self.states[state.resource_id] = state
        self.documents[(state.resource_id, rep)] = [
            d for d in documents if d.representation_type == rep
        ]

    def drop(self, item_id: str) -> None:
        self.states.pop(item_id, None)
        self.documents.pop((item_id, LC.REP_WORKFLOW_PYTHON), None)


def _same_freshness(
    existing: PythonRepresentationState, candidate: PythonRepresentationState
) -> bool:
    """Hash-skip test: representation hash + state + versions all unchanged."""
    return (
        existing.cohort == candidate.cohort
        and existing.public_state == candidate.public_state
        and existing.representation_hash == candidate.representation_hash
        and existing.canonicalization_version == candidate.canonicalization_version
        and existing.secret_scan_version == candidate.secret_scan_version
        and existing.chunking_version == candidate.chunking_version
    )


def _classify_action(state: PythonRepresentationState, docs: Sequence[PythonDocument]) -> str:
    if state.public_state == WR.PUBLIC_STATE_QUARANTINED:
        return "quarantine"
    if not state.available:
        return "unavailable"
    if not docs:
        return "drop"
    return "upsert"


def refresh_workflow_python(
    row: dict[str, Any], store: LexicalDocumentStore
) -> RefreshOutcome:
    """Refresh one resource's workflow_python lexical state (idempotent).

    - **Hash-skip** when the representation hash, public state, and versions are
      all unchanged since the last refresh (no work, no churn).
    - **Quarantine** writes a state row (safe→quarantined) and zero documents.
    - **Unavailable** writes a state row (no Python recoverable) and zero docs;
      the resource's prose/semantics remain searchable.
    - **Upsert** atomically replaces the item's workflow_python documents.
    """
    state, docs = compute_workflow_python_documents(row)
    existing = store.get_state(state.resource_id)
    if existing is not None and _same_freshness(existing, state):
        return RefreshOutcome(
            item_id=state.resource_id,
            action="skip",
            cohort=state.cohort,
            public_state=state.public_state,
            chunk_count=existing.chunk_count,
            secret_reason_codes=existing.secret_reason_codes,
        )
    store.replace_all(state, docs)
    return RefreshOutcome(
        item_id=state.resource_id,
        action=_classify_action(state, docs),
        cohort=state.cohort,
        public_state=state.public_state,
        chunk_count=len(docs),
        secret_reason_codes=state.secret_reason_codes,
    )


def drop_workflow_python(item_id: str, store: LexicalDocumentStore) -> None:
    """Drop a resource's workflow_python lexical state + documents (e.g. delete)."""
    store.drop(str(item_id))


# ---------------------------------------------------------------------------
# Candidate-query shape helpers (frozen eligibility encoded explicitly)
# ---------------------------------------------------------------------------
# These string fragments are the EXACT predicates frozen in lexical_contract
# (service-role RLS bypass => eligibility must be explicit in candidate SQL).
# They are surfaced here so task 1.7/1.9 build candidate SQL from one place.

DISTILLATION_STATUS_PREDICATE = LC.DISTILLATION_ELIGIBLE  # status IN ('pending','approved')
WORKFLOW_PYTHON_ELIGIBLE_PREDICATE = LC.WORKFLOW_PYTHON_ELIGIBLE


def project_semantics_text(metadata: dict[str, Any] | None) -> str:
    """Convenience passthrough to the frozen semantics projector (parity help)."""
    return WR.project_semantics(metadata)


def build_workflow_prose(row: dict[str, Any]) -> str:
    """Convenience passthrough to the frozen canonical-prose builder."""
    return WR.build_workflow_prose(row)


__all__ = [
    "LEXICALIZATION_VERSION",
    "CANONICALIZATION_VERSION",
    "CHUNKING_VERSION",
    "SECRET_SCAN_VERSION",
    "CoverageError",
    "PythonDocument",
    "PythonRepresentationState",
    "RefreshOutcome",
    "LexicalDocumentStore",
    "InMemoryLexicalStore",
    "compute_workflow_python_documents",
    "refresh_workflow_python",
    "drop_workflow_python",
    "DISTILLATION_STATUS_PREDICATE",
    "WORKFLOW_PYTHON_ELIGIBLE_PREDICATE",
    "project_semantics_text",
    "build_workflow_prose",
]
