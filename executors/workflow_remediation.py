"""Offline workflow-representation remediation/reconciliation (plan task 2.12a).

Pure, stdlib-only, and side-effect free except through the injected row store
and recovery adapter. It reuses the frozen representation contract in
:mod:`executors.workflow_representation` and the structured projection in
:mod:`executors.workflow_semantics`; it does **not** duplicate the deterministic
secret scanner, the representation rules, or a VibeComfy code generator.

Nothing here touches the network, the database, a provider, the filesystem, or
VibeComfy. ``corpus_path`` is authorized purely lexically and never opened.
"""
from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Any, Callable, Protocol

from executors import workflow_representation as wr
from executors import workflow_semantics as ws

# ---------------------------------------------------------------------------
# Frozen versions + nonsecret reason vocabulary
# ---------------------------------------------------------------------------

#: Bumped when the reconciled payload contract changes.
REMEDIATION_VERSION = 1

DEFAULT_BATCH_SIZE = 100

REASON_NO_SOURCE = "no_source_artifact"
REASON_QUARANTINED = "quarantined"
REASON_CORPUS_REJECTED = "corpus_path_unauthorized"
REASON_CONVERSION_ERROR = "conversion_error"


class CorpusPathRejected(Exception):
    """Raised when a local ``corpus_path`` cannot be lexically authorized.

    Carries only a nonsecret code; never the rejected path or caught text.
    """


#: Injected VibeComfy exporter/converter: ``recoverer(row) -> str | None``.
Recoverer = Callable[[dict[str, Any]], "str | None"]


class RowStore(Protocol):
    """Injected row store / write surface (production: Supabase; tests: fake)."""

    def fetch_page(
        self, *, after_id: int | None, high_water: int | None, limit: int
    ) -> list[dict[str, Any]]: ...

    def high_water(self) -> int | None: ...

    def eligible_total(self) -> int: ...

    def current(self, row_id: int) -> dict[str, Any] | None: ...

    def patch(self, row_id: int, update: dict[str, Any]) -> None: ...

    def refresh(self, row_id: int) -> None: ...

    def record_failure(self, row_id: int, reason: str) -> None: ...


# ---------------------------------------------------------------------------
# Reconciled value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconciledRepresentation:
    """The canonical, hashed, secret-scanned representation for one workflow row."""

    cohort: str
    available: bool
    body_duplicate: bool
    public_state: str
    secret_reason_codes: tuple[str, ...]
    python_source: str
    python_sha256: str
    representation_sha256: str
    reason: str | None
    provenance: dict[str, Any]
    payload: dict[str, Any]
    metadata: dict[str, Any]
    semantics: dict[str, Any]
    body: str


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    return row.get("payload") if isinstance(row.get("payload"), dict) else {}


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    return row.get("metadata") if isinstance(row.get("metadata"), dict) else {}


def _extract_corpus_path(row: dict[str, Any]) -> str | None:
    """Return the recognized local corpus_path evidence, if any (never opens it)."""
    payload = _payload(row)
    direct = payload.get("corpus_path")
    if isinstance(direct, str) and direct.strip():
        return direct
    metadata = _metadata(row)
    for path in (("corpus_path",), ("provenance", "corpus_path")):
        cursor: Any = metadata
        for key in path:
            cursor = cursor.get(key) if isinstance(cursor, dict) else None
        if isinstance(cursor, str) and cursor.strip():
            return cursor
    return None


def _has_recoverable_evidence(row: dict[str, Any], corpus_path: str | None) -> bool:
    payload = _payload(row)
    metadata = _metadata(row)
    if (
        isinstance(payload.get("workflow_json"), dict)
        or isinstance(payload.get("workflow"), dict)
        or isinstance(payload.get("compiled_api"), dict)
    ):
        return True
    if metadata.get("asset_kind") == "vibecomfy_ready_template":
        return True
    return corpus_path is not None


# ---------------------------------------------------------------------------
# corpus_path authorization (purely lexical, fail-closed, never reads)
# ---------------------------------------------------------------------------


def _lex_normalize(path: str) -> str:
    """Collapse ``.``/``..`` segments lexically without touching the filesystem."""
    absolute = path.startswith("/")
    out: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if out and out[-1] != "..":
                out.pop()
            elif not absolute:
                out.append("..")
            # absolute: a leading ``..`` cannot escape above root; drop it.
            else:
                continue
        else:
            out.append(part)
    body = "/".join(out)
    return ("/" + body) if absolute else body


def _authorize_corpus_path(corpus_path: str, vibecomfy_root: Any) -> None:
    if vibecomfy_root is None:
        raise CorpusPathRejected(REASON_CORPUS_REJECTED)
    root_norm = _lex_normalize(str(vibecomfy_root))
    if PurePosixPath(corpus_path).is_absolute():
        candidate = _lex_normalize(corpus_path)
    else:
        candidate = _lex_normalize(root_norm + "/" + corpus_path)
    if candidate == root_norm or candidate.startswith(root_norm + "/"):
        return
    raise CorpusPathRejected(REASON_CORPUS_REJECTED)


# ---------------------------------------------------------------------------
# Per-row reconciliation
# ---------------------------------------------------------------------------


def reconcile_row(
    row: dict[str, Any],
    *,
    recoverer: Recoverer | None = None,
    vibecomfy_root: Any = None,
) -> ReconciledRepresentation:
    """Reconcile one ``kind=workflow`` row into its canonical representation.

    Source-cohort precedence (frozen in :mod:`workflow_representation`):
    authoritative payload Python, then a recognized body-delimited block, then
    deterministic recovery via the injected adapter, else unavailable. Public
    state (safe/quarantined) is independent of cohort and is decided by the
    deterministic secret scanner.
    """
    title = row.get("title") if isinstance(row.get("title"), str) else ""
    title = title.strip()
    body = row.get("body") if isinstance(row.get("body"), str) else ""
    payload = _payload(row)
    metadata = _metadata(row)

    # Reuse the frozen precedence rules for payload/body; recovery is finalized
    # here so recoverer failures surface to the batch layer (not swallowed).
    resolution = wr.resolve_authoritative_python(row, recoverer=None)

    if resolution.cohort == wr.COHORT_UNAVAILABLE:
        corpus_path = _extract_corpus_path(row)
        if corpus_path is not None:
            _authorize_corpus_path(corpus_path, vibecomfy_root)
        if recoverer is not None and _has_recoverable_evidence(row, corpus_path):
            recovered = recoverer(row)  # propagate exceptions to the orchestrator
            if isinstance(recovered, str) and recovered.strip():
                resolution = wr.PythonResolution(
                    cohort=wr.COHORT_RECOVERABLE,
                    source=recovered,
                    available=True,
                    body_duplicate=False,
                    delimiter=None,
                    derivation=wr._infer_recovery_derivation(recovered),
                    body_blocks=(),
                    unavailable_reason=None,
                )

    python_source = resolution.source

    # Deterministic secret scan runs before any public materialization.
    findings = wr.scan_secrets(python_source)
    secret_codes = tuple(sorted({f.reason_code for f in findings}))
    public_state = (
        wr.PUBLIC_STATE_QUARANTINED if secret_codes else wr.PUBLIC_STATE_SAFE
    )
    python_sha256 = wr.representation_hash(python_source)

    provenance: dict[str, Any] = {
        "cohort": resolution.cohort,
        "derivation": resolution.derivation,
        "delimiter": resolution.delimiter,
        "canonicalization_version": wr.CANONICALIZATION_VERSION,
        "delimiter_grammar_version": wr.DELIMITER_GRAMMAR_VERSION,
        "secret_scan_version": wr.SECRET_SCAN_VERSION,
    }
    if resolution.body_duplicate:
        provenance["body_duplicate"] = True

    if not resolution.available:
        reason: str | None = REASON_NO_SOURCE
    elif secret_codes:
        reason = REASON_QUARANTINED
    else:
        reason = None

    # Recompute workflow semantics from the best available structured evidence,
    # replacing any stale projection already present in metadata. Generated
    # semantics and delimited Python are excluded so a repaired row cannot feed
    # its own derived text back into the next pass.
    semantic_body = wr.strip_projected_semantics(
        wr.strip_python_blocks(body), wr.project_semantics(metadata)
    )
    semantic_body = wr.strip_canonical_workflow_prefix(semantic_body, title)
    semantics = ws.build_workflow_semantics(
        metadata=metadata, payload=payload, title=title, body=semantic_body
    )
    new_metadata = copy.deepcopy(metadata)
    new_metadata["workflow_semantics_version"] = ws.WORKFLOW_SEMANTICS_VERSION
    new_metadata["workflow_semantics"] = semantics

    # Canonical searchable body: title + prose (Python blocks stripped, so the
    # code lives exactly once, in the payload) + projected semantics.
    canonical_body = wr.build_workflow_prose(
        {"title": title, "body": body, "metadata": new_metadata}
    )
    representation_sha256 = wr.representation_hash(
        canonical_body + "\n\n# python representation\n\n" + python_source
    )

    # Preserve every native artifact and identity field; patch only the
    # canonical python/body/metadata state.
    new_payload = copy.deepcopy(payload)
    new_payload["python_source"] = python_source
    new_payload["python_representation_version"] = REMEDIATION_VERSION
    new_payload["python_representation_sha256"] = representation_sha256
    new_payload["python_sha256"] = python_sha256
    new_payload["python_provenance"] = provenance
    new_payload["python_public_state"] = public_state
    new_payload["python_reason"] = reason

    # Remediation stores recovered/body Python in payload.python_source so the
    # embedding pass can consume the repaired row. On a later pass that field
    # would otherwise look like newly authoritative payload Python. Preserve
    # the original cohort only while the materialized Python is unchanged.
    prior_provenance = payload.get("python_provenance")
    prior_python_sha = payload.get("python_sha256")
    if (
        resolution.cohort == wr.COHORT_PAYLOAD_PYTHON
        and isinstance(prior_provenance, dict)
        and isinstance(prior_python_sha, str)
        and prior_python_sha == python_sha256
        and prior_provenance.get("cohort") in wr.SOURCE_COHORTS
    ):
        durable_cohort = str(prior_provenance["cohort"])
        durable_duplicate = bool(prior_provenance.get("body_duplicate"))
        provenance = dict(provenance)
        provenance["cohort"] = durable_cohort
        if durable_duplicate:
            provenance["body_duplicate"] = True
        else:
            provenance.pop("body_duplicate", None)
        new_payload["python_provenance"] = provenance
        resolution = replace(
            resolution, cohort=durable_cohort, body_duplicate=durable_duplicate
        )

    return ReconciledRepresentation(
        cohort=resolution.cohort,
        available=resolution.available,
        body_duplicate=resolution.body_duplicate,
        public_state=public_state,
        secret_reason_codes=secret_codes,
        python_source=python_source,
        python_sha256=python_sha256,
        representation_sha256=representation_sha256,
        reason=reason,
        provenance=provenance,
        payload=new_payload,
        metadata=new_metadata,
        semantics=semantics,
        body=canonical_body,
    )


def patch_update(rep: ReconciledRepresentation) -> dict[str, Any]:
    """Build the row PATCH: only payload/body/metadata, never identity columns."""
    return {"payload": rep.payload, "body": rep.body, "metadata": rep.metadata}


def _current_representation_hash(current: dict[str, Any] | None) -> str | None:
    """Read the durable reconciliation hash from either supported row shape.

    The pure stores used by the unit tests historically returned a flattened
    ``representation_sha256`` field.  The PostgreSQL adapter returns the real
    source-row shape, where the same hash is inside ``payload``.  Treating only
    the former as current made every production rerun patch the row again and
    obscured the embedding freshness proof.
    """
    if not isinstance(current, dict):
        return None
    direct = current.get("representation_sha256") or current.get(
        "python_representation_sha256"
    )
    if isinstance(direct, str) and direct:
        return direct
    payload = current.get("payload")
    if isinstance(payload, dict):
        nested = payload.get("python_representation_sha256")
        if isinstance(nested, str) and nested:
            return nested
    return None


# ---------------------------------------------------------------------------
# Bounded, resumable, idempotent orchestration
# ---------------------------------------------------------------------------


def reconcile(
    store: RowStore,
    *,
    recoverer: Recoverer | None = None,
    vibecomfy_root: Any = None,
    apply: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    sample: int | None = None,
    cursor: int | None = None,
    high_water: int | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Reconcile every eligible workflow row with bounded, stable paging.

    Defaults to a read-only dry-run. In apply mode each row is patched before
    its lexical refresh is requested, hash-identical rows are skipped, and a
    failed row stays retryable: the cursor advances only on success, so a resume
    revisits the failed row rather than silently advancing past it. The first
    retryable row failure terminates this invocation (after recording it once)
    with the cursor still before the failed row, so a resume revisits it. The
    report carries only counts/codes/cursors — never rows, bodies, paths, or
    secrets.
    """
    # Durable lifecycle is optional for the pure reference stores.  The real
    # PostgreSQL store supplies it in apply mode; dry-runs remain read-only.
    lifecycle = all(callable(getattr(store, name, None)) for name in (
        "create_remediation_run", "load_remediation_run", "checkpoint_remediation", "complete_remediation_run"))
    mode = "apply" if apply else "dry_run"
    effective_high_water = (
        high_water if high_water is not None else store.high_water()
    )
    lifecycle_version: int | None = None
    durable_counts: dict[str, int] = {}
    if apply and lifecycle:
        if run_id is not None:
            state = store.load_remediation_run(int(run_id))
            run_id = str(state["run_id"])
            lifecycle_version = int(state["version"])
            current_cursor = state.get("cursor")
            effective_high_water = state.get("high_water", effective_high_water)
            durable_counts = dict(state.get("counters") or {})
        else:
            state = store.create_remediation_run(effective_high_water)
            run_id = str(state["run_id"])
            lifecycle_version = int(state["version"])
            current_cursor = state.get("cursor")
    else:
        run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
        current_cursor = cursor

    cohorts = {c: 0 for c in wr.SOURCE_COHORTS}
    public_states = {s: 0 for s in wr.PUBLIC_STATES}
    materialized = int(durable_counts.get("materialized", 0))
    unavailable = int(durable_counts.get("unavailable", 0))
    stale = 0
    duplicates = 0
    skipped = int(durable_counts.get("skipped", 0))
    failed = int(durable_counts.get("failed", 0))
    processed = int(durable_counts.get("processed", 0))
    interrupted = False

    def checkpoint(*, next_cursor: int | None, processed_delta: int = 0,
                   materialized_delta: int = 0, skipped_delta: int = 0,
                   unavailable_delta: int = 0, failed_delta: int = 0,
                   reason: str | None = None, failed_resource_id: int | None = None) -> bool:
        nonlocal lifecycle_version
        if not (apply and lifecycle):
            return True
        assert lifecycle_version is not None
        response = store.checkpoint_remediation(
            int(run_id), lifecycle_version, next_cursor, effective_high_water,
            processed_delta, materialized_delta, skipped_delta, unavailable_delta,
            failed_delta, reason, failed_resource_id,
        )
        if not response.get("ok"):
            return False
        lifecycle_version = int(response.get("version", lifecycle_version))
        return True

    while True:
        if sample is not None and processed >= sample:
            break
        limit = batch_size
        if sample is not None:
            limit = min(batch_size, sample - processed)
        try:
            page = store.fetch_page(
                after_id=current_cursor,
                high_water=effective_high_water,
                limit=limit,
            )
        except KeyboardInterrupt:
            interrupted = True
            break
        if not page:
            break

        sample_reached = False
        stop_on_failure = False
        for row in page:
            if sample is not None and processed >= sample:
                sample_reached = True
                break
            rid = row["id"]
            try:
                rep = reconcile_row(
                    row, recoverer=recoverer, vibecomfy_root=vibecomfy_root
                )
            except CorpusPathRejected:
                failed += 1
                store.record_failure(rid, REASON_CORPUS_REJECTED)
                checkpoint(next_cursor=current_cursor, failed_delta=1,
                           reason=REASON_CORPUS_REJECTED, failed_resource_id=rid)
                stop_on_failure = True
                break
            except Exception:
                failed += 1
                store.record_failure(rid, REASON_CONVERSION_ERROR)
                checkpoint(next_cursor=current_cursor, failed_delta=1,
                           reason=REASON_CONVERSION_ERROR, failed_resource_id=rid)
                stop_on_failure = True
                break

            # Success: the cursor advances only for handled rows, so a failed
            # row remains retryable on resume.
            current_cursor = rid
            processed += 1
            cohorts[rep.cohort] = cohorts.get(rep.cohort, 0) + 1
            public_states[rep.public_state] = public_states.get(rep.public_state, 0) + 1
            if rep.cohort == wr.COHORT_UNAVAILABLE:
                unavailable += 1
            if rep.body_duplicate:
                duplicates += 1

            if apply:
                current = store.current(rid)
                existing_hash = _current_representation_hash(current)
                if existing_hash and existing_hash == rep.representation_sha256:
                    skipped += 1
                    materialized_delta = 0
                    skipped_delta = 1
                else:
                    stale += 1
                    store.patch(rid, patch_update(rep))
                    store.refresh(rid)
                    if rep.available and rep.public_state == wr.PUBLIC_STATE_SAFE:
                        materialized += 1
                        materialized_delta = 1
                    else:
                        materialized_delta = 0
                    skipped_delta = 0
                if not checkpoint(
                    next_cursor=current_cursor, processed_delta=1,
                    materialized_delta=materialized_delta, skipped_delta=skipped_delta,
                    unavailable_delta=1 if rep.cohort == wr.COHORT_UNAVAILABLE else 0,
                ):
                    failed += 1
                    stop_on_failure = True
                    break
            else:
                stale += 1

        if sample_reached:
            break
        if stop_on_failure:
            break

    if apply and lifecycle and not interrupted and not failed:
        assert lifecycle_version is not None
        completed = store.complete_remediation_run(int(run_id), lifecycle_version)
        if not completed.get("ok"):
            failed += 1

    return {
        "run_id": run_id,
        "mode": mode,
        "eligible_total": store.eligible_total(),
        "cohorts": cohorts,
        "public_state": public_states,
        "materialized": materialized,
        "processed": processed,
        "unavailable": unavailable,
        "stale_or_mismatched_hashes": stale,
        "duplicate_python_bodies": duplicates,
        "skipped": skipped,
        "failed": failed,
        "cursor": current_cursor,
        "high_water": effective_high_water,
        "interrupted": interrupted,
    }
