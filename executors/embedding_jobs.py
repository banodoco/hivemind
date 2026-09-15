"""Embedding job intent — the offline mirror of the source-table triggers (2.7).

The SQL triggers in ``schema/025_embedding_jobs.sql`` enqueue one idempotent job
per affected ``(entity_type, item_id, representation_type)``. This module is the
pure-Python statement of that SAME decision matrix so the lifecycle rules are
unit-testable offline and provably consistent with SQL:

* insert -> ``embed`` for every applicable representation of the new row.
* content change -> ``reembed`` for the affected representation(s).
* soft-delete / hard-delete / status->ineligible -> ``drop``.
* workflow Python change (payload / body block) -> ``reembed`` workflow_python;
  prose-only changes never duplicate a code chunk (no-duplication, plan AD-4).
* reclassification away from ``workflow`` -> ``drop`` workflow_python.

The authoritative enqueue is the SQL trigger; this module only computes the
*intent* (which jobs SHOULD be enqueued for a given transition). The worker
(task 2.9) consumes jobs and always recomputes canonical representations from
the CURRENT source, so a job is a "this identity needs (re)processing" signal,
not a snapshot — that is what makes completion source-hash safe.

Pure stdlib, offline, dependency-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from executors import entity_identity as ei
from executors import workflow_representation as wr

__all__ = [
    "JOB_EMBED",
    "JOB_REEMBED",
    "JOB_DROP",
    "JOB_KINDS",
    "OP_INSERT",
    "OP_UPDATE",
    "OP_DELETE",
    "SOURCE_INSERT",
    "SOURCE_UPDATE",
    "SOURCE_DELETE",
    "SOURCE_SOFT_DELETE",
    "SOURCE_STATUS_CHANGE",
    "SOURCE_PYTHON_CHANGE",
    "SOURCE_SEMANTIC_CHANGE",
    "SOURCE_OPT_OUT",
    "JobIntent",
    "decide_jobs",
    "applicable_representations",
]

# ---------------------------------------------------------------------------
# Vocabulary (mirrors schema/025 CHECK constraints)
# ---------------------------------------------------------------------------

JOB_EMBED = "embed"
JOB_REEMBED = "reembed"
JOB_DROP = "drop"
JOB_KINDS = (JOB_EMBED, JOB_REEMBED, JOB_DROP)

OP_INSERT = "insert"
OP_UPDATE = "update"
OP_DELETE = "delete"

SOURCE_INSERT = "insert"
SOURCE_UPDATE = "update"
SOURCE_DELETE = "delete"
SOURCE_SOFT_DELETE = "soft_delete"
SOURCE_STATUS_CHANGE = "status_change"
SOURCE_PYTHON_CHANGE = "python_change"
SOURCE_SEMANTIC_CHANGE = "semantic_change"
SOURCE_OPT_OUT = "opt_out"

REP_PROSE = wr.REP_PROSE
REP_WORKFLOW_PYTHON = wr.REP_WORKFLOW_PYTHON


# ---------------------------------------------------------------------------
# JobIntent
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobIntent:
    """One job the trigger SHOULD enqueue for a source transition.

    ``entity_type`` / ``item_id`` / ``representation_type`` identify the
    embedding stream; ``job_kind`` is embed/reembed/drop; ``source_op`` is the
    frozen reason code stored on the job row (mirrors schema/025 source_op).
    """

    entity_type: str
    item_id: str
    representation_type: str
    job_kind: str
    source_op: str

    def __post_init__(self) -> None:
        if self.entity_type not in ei.ENTITY_TYPES:
            raise ValueError(f"entity_type must be one of {ei.ENTITY_TYPES}")
        if self.job_kind not in JOB_KINDS:
            raise ValueError(f"job_kind must be one of {JOB_KINDS}")
        if self.representation_type not in (REP_PROSE, REP_WORKFLOW_PYTHON):
            raise ValueError(f"representation_type must be prose|workflow_python")


def _stringify(item_id: Any) -> str:
    return ei.stringify_item_id(item_id)


def _is_workflow(row: dict[str, Any] | None) -> bool:
    return bool(row) and (row.get("kind") or "").strip() == "workflow"


def applicable_representations(row: dict[str, Any] | None, entity_type: str) -> list[str]:
    """Return the representation streams an entity row carries.

    Messages/distillations/non-workflow resources carry ``prose`` only; a
    workflow carries ``prose`` and ``workflow_python`` (the worker decides
    whether python is actually available/safe — task 0.8 §7).
    """

    if entity_type in (ei.ENTITY_MESSAGE, ei.ENTITY_DISTILLATION):
        return [REP_PROSE]
    if _is_workflow(row):
        return [REP_PROSE, REP_WORKFLOW_PYTHON]
    return [REP_PROSE]


def _prose_changed(old: dict[str, Any], new: dict[str, Any]) -> bool:
    return any(
        old.get(k) != new.get(k) for k in ("title", "body", "metadata", "kind")
    )


def _python_changed(old: dict[str, Any], new: dict[str, Any]) -> bool:
    # payload (python_source), the legacy body block, and semantics all affect
    # the authoritative Python representation.
    return any(
        old.get(k) != new.get(k) for k in ("payload", "body", "metadata")
    )


# ---------------------------------------------------------------------------
# decide_jobs — the per-entity decision matrix (mirrors the SQL triggers)
# ---------------------------------------------------------------------------


def decide_jobs(
    old: dict[str, Any] | None,
    new: dict[str, Any] | None,
    entity_type: str,
    *,
    op: str,
    item_id: Any = None,
) -> list[JobIntent]:
    """Return the jobs a source transition should enqueue (mirrors schema/025).

    ``op`` is insert|update|delete. ``item_id`` is taken from the surviving row
    (``new`` for insert/update, ``old`` for delete) unless explicitly passed.
    Raises ``ValueError`` for an unknown entity_type/op.
    """

    if entity_type not in ei.ENTITY_TYPES:
        raise ValueError(f"entity_type must be one of {ei.ENTITY_TYPES}, got {entity_type!r}")
    if op not in (OP_INSERT, OP_UPDATE, OP_DELETE):
        raise ValueError(f"op must be insert|update|delete, got {op!r}")

    old = old or {}
    new = new or {}
    if op in (OP_INSERT, OP_UPDATE):
        source = new
        sid = item_id if item_id is not None else _id_from_row(new, entity_type)
    else:
        source = old
        sid = item_id if item_id is not None else _id_from_row(old, entity_type)
    sid = _stringify(sid)

    if entity_type == ei.ENTITY_MESSAGE:
        return _decide_message(old, new, op, sid)
    if entity_type == ei.ENTITY_DISTILLATION:
        return _decide_distillation(old, new, op, sid)
    return _decide_resource(old, new, op, sid)


def _id_from_row(row: dict[str, Any], entity_type: str) -> Any:
    if entity_type == ei.ENTITY_MESSAGE:
        return row.get("message_id")
    if entity_type == ei.ENTITY_DISTILLATION:
        return row.get("id")
    return row.get("id")


def _decide_message(old, new, op, sid) -> list[JobIntent]:
    if op == OP_INSERT:
        if not _is_deleted(new):
            return [JobIntent(ei.ENTITY_MESSAGE, sid, REP_PROSE, JOB_EMBED, SOURCE_INSERT)]
        return []
    if op == OP_UPDATE:
        if _is_deleted(new) and not _is_deleted(old):
            return [JobIntent(ei.ENTITY_MESSAGE, sid, REP_PROSE, JOB_DROP, SOURCE_SOFT_DELETE)]
        if new.get("content") != old.get("content"):
            return [JobIntent(ei.ENTITY_MESSAGE, sid, REP_PROSE, JOB_REEMBED, SOURCE_UPDATE)]
        return []
    return [JobIntent(ei.ENTITY_MESSAGE, sid, REP_PROSE, JOB_DROP, SOURCE_DELETE)]


def _decide_distillation(old, new, op, sid) -> list[JobIntent]:
    if op == OP_INSERT:
        if _distillation_eligible(new):
            return [JobIntent(ei.ENTITY_DISTILLATION, sid, REP_PROSE, JOB_EMBED, SOURCE_INSERT)]
        return []
    if op == OP_UPDATE:
        eligible_old = _distillation_eligible(old)
        eligible_new = _distillation_eligible(new)
        if not eligible_new and eligible_old:
            return [JobIntent(ei.ENTITY_DISTILLATION, sid, REP_PROSE, JOB_DROP, SOURCE_STATUS_CHANGE)]
        if eligible_new and (
            new.get("question") != old.get("question")
            or new.get("conditions") != old.get("conditions")
            or new.get("answer") != old.get("answer")
            or not eligible_old
        ):
            return [JobIntent(ei.ENTITY_DISTILLATION, sid, REP_PROSE, JOB_REEMBED, SOURCE_UPDATE)]
        return []
    return [JobIntent(ei.ENTITY_DISTILLATION, sid, REP_PROSE, JOB_DROP, SOURCE_DELETE)]


def _decide_resource(old, new, op, sid) -> list[JobIntent]:
    if op == OP_INSERT:
        intents = [JobIntent(ei.ENTITY_RESOURCE, sid, REP_PROSE, JOB_EMBED, SOURCE_INSERT)]
        if _is_workflow(new):
            intents.append(
                JobIntent(ei.ENTITY_RESOURCE, sid, REP_WORKFLOW_PYTHON, JOB_EMBED, SOURCE_INSERT)
            )
        return intents
    if op == OP_UPDATE:
        intents: list[JobIntent] = []
        if _prose_changed(old, new):
            intents.append(JobIntent(ei.ENTITY_RESOURCE, sid, REP_PROSE, JOB_REEMBED, SOURCE_UPDATE))
        if _is_workflow(new) and _python_changed(old, new):
            intents.append(
                JobIntent(ei.ENTITY_RESOURCE, sid, REP_WORKFLOW_PYTHON, JOB_REEMBED, SOURCE_PYTHON_CHANGE)
            )
        if _is_workflow(old) and not _is_workflow(new):
            intents.append(
                JobIntent(ei.ENTITY_RESOURCE, sid, REP_WORKFLOW_PYTHON, JOB_DROP, SOURCE_SEMANTIC_CHANGE)
            )
        return intents
    intents = [JobIntent(ei.ENTITY_RESOURCE, sid, REP_PROSE, JOB_DROP, SOURCE_DELETE)]
    if _is_workflow(old):
        intents.append(
            JobIntent(ei.ENTITY_RESOURCE, sid, REP_WORKFLOW_PYTHON, JOB_DROP, SOURCE_DELETE)
        )
    return intents


def _is_deleted(row: dict[str, Any]) -> bool:
    return bool(row.get("is_deleted"))


def _distillation_eligible(row: dict[str, Any]) -> bool:
    return (row.get("status") or "") in ("pending", "approved")
