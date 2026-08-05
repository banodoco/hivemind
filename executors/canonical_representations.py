"""Canonical semantic representations + hashes for every entity/representation type.

Plan task 2.5. This module is the embedding-side source of truth for "what text
gets hashed and embedded" for messages, resources (incl. workflows), and
distillations. It enforces the frozen workflow Python precedence, secret-state
exclusion, no-duplication, and contract versioning (plan AD-4; task 0.8 freeze).

Canonical semantic text per (entity, representation) — frozen, mirrors the lexical
documents so lexical and semantic search run over the SAME content:

| Item / representation       | Canonical semantic text                                 |
|-----------------------------|---------------------------------------------------------|
| message / prose             | content                                                 |
| non-workflow resource / prose | title, then body, then stable textual tags            |
| workflow / prose            | title, body-with-python-blocks-removed, projected workflow_semantics |
| workflow / workflow_python  | authoritative Python (payload.python_source > body block > recoverable) |
| distillation / prose        | question, then conditions, then answer                  |

ONE normalization, ONE hash. All text is hashed with
:func:`executors.workflow_representation.representation_hash` (frozen task-0.8
algorithm: NFC + CRLF/CR→LF + per-line trailing-whitespace strip + strip, then
SHA-256). The ``representation_hash`` covers the full canonical representation;
the ``chunk_hash`` covers a single embedded chunk. They are the same algorithm
under different names (representation-level vs chunk-level freshness), kept
distinct so the two DB columns cannot be confused.

Workflow Python precedence (frozen, plan AD-4 — delegated to workflow_representation):
  1. non-empty payload.python_source is authoritative;
  2. else an exact recognized body-delimited block;
  3. else deterministic recovery (injected recoverer; VibeComfy in production);
  4. else unavailable (the row stays prose-searchable, no Python representation).

Secret-state exclusion (frozen task 0.8 §7): workflow Python is run through the
deterministic scanner. A quarantined representation is **excluded** from the
returned representations — it is never hashed, embedded, snippeted, or returned
by get_item. Its cohort + non-secret public-state are still reported for coverage
(without the text or hash). This is the "unsafe Python is excluded" gate.

No-duplication (frozen task 0.8 §4): when payload.python_source bytes also form
the body block, the block is stripped from prose and the authoritative Python is
emitted exactly ONCE as workflow_python. Duplicate Python is impossible here.

Versioning (plan AD-4 / task 0.8 §5): every representation carries
canonicalization/chunking/secret-scan versions mirrored in the embedding
contract; a version bump triggers a controlled re-embed even when the source row
did not change.

Pure stdlib, offline, dependency-free. Reuses the frozen
:mod:`executors.workflow_representation`; does not touch the DB, import VibeComfy,
execute stored Python, or read the corpus.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from executors import entity_identity as ei
from executors import workflow_representation as wr
from executors.workflow_representation import (  # version + vocab re-exports
    CANONICALIZATION_VERSION,
    CHUNKING_VERSION,
    SECRET_SCAN_VERSION,
    REP_PROSE,
    REP_WORKFLOW_PYTHON,
)

__all__ = [
    "CanonicalRepresentation",
    "WorkflowPythonStatus",
    "canonical_message_text",
    "canonical_resource_text",
    "canonical_distillation_text",
    "canonical_workflow_prose_text",
    "build_representations",
    "workflow_python_status",
]

#: Field separator for multi-field canonical text. Stable across Python/SQL so the
#: canonical TEXT (and therefore the hash on ASCII/UTF-8-NFC fixtures) is identical
#: in both languages (plan 2.5 cross-language parity).
_FIELD_SEP = "\n\n"


def _join_nonempty(parts: list[str | None]) -> str:
    return _FIELD_SEP.join(p.strip() for p in parts if p and p.strip())


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    # Lists/dicts of tags etc. are flattened to a single space-joined string so a
    # resource's stable textual tags participate without carrying structure.
    if isinstance(value, (list, tuple, set)):
        return " ".join(str(v).strip() for v in value if v is not None and str(v).strip())
    return str(value)


# ---------------------------------------------------------------------------
# Canonical text builders (frozen field order; mirrors SQL schema/024)
# ---------------------------------------------------------------------------


def canonical_message_text(content: Any) -> str:
    """message / prose = content (plan AD-4). Missing content -> empty string."""

    return _coerce_str(content)


def canonical_resource_text(
    title: Any,
    body: Any,
    tags: Any = None,
) -> str:
    """Non-workflow resource / prose = title, then body, then stable tags (AD-4).

    Tags come from ``metadata`` as a list/str of stable textual tags. Field order
    is frozen and mirrors the SQL canonicalizer so lexical and semantic text agree.
    """

    return _join_nonempty([_coerce_str(title), _coerce_str(body), _coerce_str(tags)])


def canonical_distillation_text(
    question: Any,
    conditions: Any,
    answer: Any,
) -> str:
    """distillation / prose = question, then conditions, then answer (AD-4).

    Status/confidence influence weighting but never enter the search text (AD-4),
    so they are deliberately absent here.
    """

    return _join_nonempty([_coerce_str(question), _coerce_str(conditions), _coerce_str(answer)])


def canonical_workflow_prose_text(row: dict[str, Any]) -> str:
    """workflow / prose — delegated to the frozen :func:`wr.build_workflow_prose`.

    Title, body-with-delimited-Python-blocks-removed (no-duplication), then the
    projected stable ``workflow_semantics``. The canonical Python lives in its own
    ``workflow_python`` representation and never appears in the prose hash.
    """

    return wr.build_workflow_prose(row)


# ---------------------------------------------------------------------------
# CanonicalRepresentation + WorkflowPythonStatus
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CanonicalRepresentation:
    """One canonical representation ready to hash + (later, in task 2.6) chunk+embed.

    ``text`` is the canonical semantic text; ``representation_hash`` is the
    frozen SHA-256 over it. For a single-chunk representation (the normal case
    for messages/distillations/short resources) the chunk hash equals the
    representation hash; multi-chunk is task 2.6. ``available=False`` only marks
    an explicit absence (e.g. an unavailable workflow_python is not returned at
    all, but a representation that exists with empty text is still hashable).
    """

    entity_type: str
    item_id: str
    representation_type: str
    text: str
    representation_hash: str
    available: bool
    public_state: str
    canonicalization_version: int = CANONICALIZATION_VERSION
    chunking_version: int = CHUNKING_VERSION
    secret_scan_version: int = SECRET_SCAN_VERSION


@dataclass(frozen=True)
class WorkflowPythonStatus:
    """Coverage/reporting view of a workflow's Python representation, secret-safe.

    Used by coverage (task 2.12) and the no-duplication/quarantine gates WITHOUT
    exposing quarantined text or its hash. Only the cohort, the cross-cutting
    public state, and non-secret reason codes are reported (task 0.8 §7 logging).
    """

    available: bool
    public_state: str
    cohort: str
    body_duplicate: bool
    secret_reason_codes: tuple[str, ...]
    has_representation: bool  # True iff available AND safe (i.e. emitted)


# ---------------------------------------------------------------------------
# The builder: resolve every applicable representation for an entity row
# ---------------------------------------------------------------------------


def build_representations(
    row: dict[str, Any],
    entity_type: str,
    *,
    item_id: Any = None,
    recoverer: Callable[[dict[str, Any]], "str | None"] | None = None,
) -> list[CanonicalRepresentation]:
    """Return every canonical representation for *row* under ``entity_type``.

    Rules (plan AD-4, frozen):
      * message / distillation / non-workflow resource -> one ``prose`` representation.
      * workflow -> a ``prose`` representation plus a ``workflow_python``
        representation ONLY when authoritative Python is available AND safe
        (quarantined Python is excluded — never returned, hashed, or embedded).
      * Duplicate Python is structurally impossible: prose has the body block
        stripped and Python is emitted once from the authoritative source.

    ``item_id`` is stringified via :func:`ei.stringify_item_id` (snowflake-safe).
    The ``recoverer`` is injected so workflow recovery is offline-testable; in
    production it wraps VibeComfy's exporters (task 2.12). No LLM, no execution.
    """

    if entity_type not in ei.ENTITY_TYPES:
        raise ValueError(f"entity_type must be one of {ei.ENTITY_TYPES}, got {entity_type!r}")
    row = row or {}
    string_id = ei.stringify_item_id(item_id) if item_id is not None else ""

    if entity_type == ei.ENTITY_MESSAGE:
        text = canonical_message_text(row.get("content"))
        return [_representation(ei.ENTITY_MESSAGE, string_id, REP_PROSE, text)]

    if entity_type == ei.ENTITY_DISTILLATION:
        text = canonical_distillation_text(
            row.get("question"), row.get("conditions"), row.get("answer")
        )
        return [_representation(ei.ENTITY_DISTILLATION, string_id, REP_PROSE, text)]

    # entity_type == resource
    kind = (row.get("kind") or "").strip()
    if kind != "workflow":
        text = canonical_resource_text(
            row.get("title"), row.get("body"), _resource_tags(row.get("metadata"))
        )
        return [_representation(ei.ENTITY_RESOURCE, string_id, REP_PROSE, text)]

    # Workflow: prose always; workflow_python only when available + safe.
    reps: list[CanonicalRepresentation] = []
    prose_text = canonical_workflow_prose_text(row)
    reps.append(_representation(ei.ENTITY_RESOURCE, string_id, REP_PROSE, prose_text))

    resolution = wr.resolve_authoritative_python(row, recoverer=recoverer)
    if resolution.available and resolution.source:
        if wr.public_state(resolution.source) == wr.PUBLIC_STATE_SAFE:
            reps.append(
                CanonicalRepresentation(
                    entity_type=ei.ENTITY_RESOURCE,
                    item_id=string_id,
                    representation_type=REP_WORKFLOW_PYTHON,
                    text=resolution.source,
                    representation_hash=wr.representation_hash(resolution.source),
                    available=True,
                    public_state=wr.PUBLIC_STATE_SAFE,
                )
            )
        # Quarantined: deliberately NOT appended. Excluded from embedding/snippet/
        # get_item (task 0.8 §7). Its hash is never computed for a client.
    return reps


def _resource_tags(metadata: Any) -> str:
    """Extract stable textual tags from a resource's metadata (AD-4)."""

    if not isinstance(metadata, dict):
        return ""
    tags = metadata.get("tags")
    if tags is None:
        return ""
    return _coerce_str(tags)


def _representation(
    entity_type: str, item_id: str, representation_type: str, text: str
) -> CanonicalRepresentation:
    return CanonicalRepresentation(
        entity_type=entity_type,
        item_id=item_id,
        representation_type=representation_type,
        text=text,
        representation_hash=wr.representation_hash(text),
        available=True,
        public_state=wr.PUBLIC_STATE_SAFE,
    )


def workflow_python_status(
    row: dict[str, Any],
    *,
    recoverer: Callable[[dict[str, Any]], "str | None"] | None = None,
) -> WorkflowPythonStatus:
    """Return a secret-safe status view of a workflow's Python representation.

    For coverage/reporting only. Exposes the cohort, the cross-cutting public
    state, the no-duplication flag, and NON-SECRET scanner reason codes — never
    the matched value, the source text, or the hash (task 0.8 §7).
    """

    row = row or {}
    resolution = wr.resolve_authoritative_python(row, recoverer=recoverer)
    reason_codes: tuple[str, ...] = ()
    public_state = wr.PUBLIC_STATE_SAFE
    if resolution.available and resolution.source:
        findings = wr.scan_secrets(resolution.source)
        public_state = wr.public_state(resolution.source)
        reason_codes = tuple(sorted({f.reason_code for f in findings}))
    has_representation = bool(
        resolution.available and resolution.source and public_state == wr.PUBLIC_STATE_SAFE
    )
    return WorkflowPythonStatus(
        available=resolution.available,
        public_state=public_state,
        cohort=resolution.cohort,
        body_duplicate=resolution.body_duplicate,
        secret_reason_codes=reason_codes,
        has_representation=has_representation,
    )
