#!/usr/bin/env python3
"""Live (production) lexical workflow-Python refresh — task 1.2 backfill.

Populates the chunk-aware lexical storage created by schema/003
(``lexical_documents`` + ``lexical_resource_python_state``) from the authoritative
workflow representations in ``external_resources`` (kind='workflow'), reusing the
FROZEN reference contracts WITHOUT reinterpretation:

- :mod:`executors.workflow_representation` (task 0.8): payload > body-delimiter >
  unavailable precedence, body-block stripping (no duplication), the deterministic
  secret scanner + quarantine, and the AST-aware code chunker with its
  ``coverage_ok`` no-silent-truncation guard.
- :mod:`executors.lexical_documents` (task 1.2 bridge): ``compute_workflow_python_documents``
  turns one row into (state, chunk docs).

Security invariants (frozen 0.8 §7), enforced here:

- **Never execute** stored Python (only ``ast.parse`` for chunking).
- **Never log/serialize/expose** a matched secret or raw code. All stdout/logs are
  SANITIZED: counts, cohorts, public_state, non-secret reason codes, and hashes
  only. The ``chunk_text`` reaches the DB (the legitimate indexed write) but is
  never printed; ``psql -q`` suppresses query echoing, so even a failing statement
  emits only a generic ``ERROR:`` line (no code).
- **Quarantined** Python writes a state row (cohort, reason codes) and ZERO
  ``lexical_documents`` rows (structural exclusion). **Unavailable** Python writes a
  state row and zero docs. Only ``safe`` + ``available`` Python is chunked+indexed.

Operational properties:

- **Resumable + idempotent.** Workflows are read in ``id`` order with a high-water
  checkpoint; existing state is hash-skipped (representation_hash + cohort +
  public_state + versions), so an interrupted re-run does no churn.
- **Per-item atomic.** Each item's replace is a delete-then-insert in ONE
  transaction; a failure rolls back only that item and is retried.
- **Source-preserving.** Writes touch ONLY ``lexical_documents`` and
  ``lexical_resource_python_state``. Source rows are never mutated.

Same session-mode access path as every Phase-1 live driver (``supabase db dump
--dry-run`` -> short-lived CLI-login libpq env; credential in child env only).

Usage::

    python3 scripts/live_lexical_refresh.py --plan       # dry-run: no writes, full report
    python3 scripts/live_lexical_refresh.py --apply       # write lexical_documents + state
    python3 scripts/live_lexical_refresh.py --verify      # read-only coverage report
    python3 scripts/live_lexical_refresh.py --rollback    # delete derived rows (source untouched)
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile
import time
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from executors import lexical_documents as LD  # noqa: E402
from executors import workflow_representation as WR  # noqa: E402
from live_lexical_search import derive_pg_env, psql, elevate, redact  # noqa: E402
from lexical_pg import q, q_array  # noqa: E402  (safe SQL-literal renderers)

ELEVATE_ROLE = "postgres"
EVIDENCE_OUT = REPO / "docs" / "hybrid-search" / "phase1-lexical-refresh-live.json"

PAGE_SIZE = 50


# ---------------------------------------------------------------------------
# Short-lived CLI-login credential holder.
# ---------------------------------------------------------------------------
# The libpq env derived from ``supabase db dump --dry-run`` carries a TEMPORARY
# CLI-login password that rotates/expired mid-backfill on a long run. Hold it in a
# :class:`Cred` and re-derive on an authentication failure so a minutes-long
# refresh survives credential rotation without manual re-launch. The credential
# is held only in the child-process env; never printed.


class Cred:
    def __init__(self) -> None:
        self.env: dict = {}
        self.host = ""
        self.port = ""
        self.refresh()

    def refresh(self) -> None:
        self.env, self.host, self.port = derive_pg_env()


_AUTH_FAIL_MARKERS = ("password authentication failed", "authentication failed", "no pg_hba.conf entry")


def _is_auth_fail(stderr: str) -> bool:
    low = (stderr or "").lower()
    return any(m in low for m in _AUTH_FAIL_MARKERS)


def psql_retry(cred: "Cred", sql: str, *, timeout: float = 60.0, on_error_stop: bool = True):
    r = psql(cred.env, sql, timeout=timeout, on_error_stop=on_error_stop)
    if r.returncode != 0 and _is_auth_fail(r.stderr):
        cred.refresh()
        r = psql(cred.env, sql, timeout=timeout, on_error_stop=on_error_stop)
    return r
MAX_RETRIES = 3

DOC_COLS = (
    "entity_type", "item_id", "representation_type", "chunk_index", "chunk_text",
    "matched_anchor", "source_offset_start", "source_offset_end",
    "representation_hash", "chunk_hash", "quarantine_state",
    "lexicalization_version", "canonicalization_version",
    "chunking_version", "secret_scan_version", "method",
)
STATE_COLS = (
    "resource_id", "kind", "cohort", "public_state", "available", "body_duplicate",
    "delimiter", "derivation", "representation_hash", "secret_reason_codes",
    "canonicalization_version", "secret_scan_version", "chunking_version", "chunk_count",
)


# ---------------------------------------------------------------------------
# Reading workflow rows (sanitized transport: JSON page over psql)
# ---------------------------------------------------------------------------


def _read_page_sql(since_id: int, limit: int) -> str:
    # Payload/body travel as JSON (psql handles all escaping); never logged raw.
    return (
        "select coalesce(jsonb_agg(jsonb_build_object("
        "'id', id, 'kind', kind, 'body', body, 'payload', payload)), '[]') "
        "from (select id, kind, body, payload from public.external_resources "
        f"where kind='workflow' and id > {int(since_id)} order by id limit {int(limit)}) t;"
    )


def fetch_workflow_pages(cred: "Cred", *, since_id: int = 0, limit: int = PAGE_SIZE):
    """Yield lists of row-dicts, page by page, in id order from `since_id`."""
    cursor = since_id
    while True:
        r = psql_retry(cred, elevate(_read_page_sql(cursor, limit)), timeout=120, on_error_stop=False)
        if r.returncode != 0:
            raise RuntimeError("workflow page read failed: " + redact((r.stderr or "").strip()))
        page = json.loads(r.stdout.strip() or "[]")
        if not page:
            return
        for row in page:
            payload = row.get("payload")
            row["payload"] = payload if isinstance(payload, dict) else {}
            if not isinstance(row.get("body"), str):
                row["body"] = ""
            yield row
        last_id = int(page[-1]["id"])
        if len(page) < limit:
            return
        cursor = last_id


def fetch_existing_states(cred: "Cred") -> dict[str, dict[str, Any]]:
    """All existing python-state rows (small; loaded once for hash-skip)."""
    r = psql_retry(cred, elevate(
        "select coalesce(jsonb_agg(jsonb_build_object("
        "'resource_id', resource_id::text, 'cohort', cohort, 'public_state', public_state, "
        "'representation_hash', representation_hash, 'canonicalization_version', canonicalization_version, "
        "'secret_scan_version', secret_scan_version, 'chunking_version', chunking_version, "
        "'chunk_count', chunk_count)), '[]') from public.lexical_resource_python_state;"
    ), timeout=60, on_error_stop=False)
    if r.returncode != 0:
        raise RuntimeError("existing-state read failed: " + redact((r.stderr or "").strip()))
    rows = json.loads(r.stdout.strip() or "[]")
    return {str(x["resource_id"]): x for x in rows}


def _same_freshness(existing: dict[str, Any], cand: LD.PythonRepresentationState) -> bool:
    return (
        existing.get("cohort") == cand.cohort
        and existing.get("public_state") == cand.public_state
        and existing.get("representation_hash") == cand.representation_hash
        and int(existing.get("canonicalization_version") or 0) == cand.canonicalization_version
        and int(existing.get("secret_scan_version") or 0) == cand.secret_scan_version
        and int(existing.get("chunking_version") or 0) == cand.chunking_version
    )


# ---------------------------------------------------------------------------
# Writing one item (atomic, as postgres, via temp file — no arg-length limit)
# ---------------------------------------------------------------------------


def _state_insert_sql(state: LD.PythonRepresentationState) -> str:
    return (
        "insert into public.lexical_resource_python_state (" + ",".join(STATE_COLS) + ") values (" +
        ",".join([
            str(int(state.resource_id)),
            q(state.kind), q(state.cohort), q(state.public_state), q(state.available),
            q(state.body_duplicate), q(state.delimiter), q(state.derivation),
            q(state.representation_hash), q_array(list(state.secret_reason_codes)),
            str(state.canonicalization_version), str(state.secret_scan_version),
            str(state.chunking_version), str(state.chunk_count),
        ]) + ");"
    )


def _docs_insert_sql(docs: tuple) -> str:
    if not docs:
        return ""
    values = []
    for d in docs:
        values.append(
            "(" + ",".join([
                q(d.entity_type), q(d.item_id), q(d.representation_type), str(d.chunk_index),
                q(d.chunk_text), q(d.matched_anchor), str(d.source_offset_start), str(d.source_offset_end),
                q(d.representation_hash), q(d.chunk_hash), q(d.quarantine_state),
                str(d.lexicalization_version), str(d.canonicalization_version),
                str(d.chunking_version), str(d.secret_scan_version), q(d.method),
            ]) + ")"
        )
    return "insert into public.lexical_documents (" + ",".join(DOC_COLS) + ") values " + ",".join(values) + ";"


def _item_inner_sql(state: LD.PythonRepresentationState, docs: tuple) -> str:
    """The delete+insert statements for one item (no BEGIN/COMMIT)."""
    return (
        "delete from public.lexical_documents where entity_type='resource' "
        f"and item_id={q(state.resource_id)} and representation_type='workflow_python';\n"
        f"delete from public.lexical_resource_python_state where resource_id={int(state.resource_id)};\n"
        + _state_insert_sql(state) + "\n" + _docs_insert_sql(docs)
    )


def _render_item_sql(state: LD.PythonRepresentationState, docs: tuple) -> str:
    """One transaction: delete this item's workflow_python docs + state, then
    re-insert state + (possibly zero) docs. Idempotent."""
    return "begin;\n" + _item_inner_sql(state, docs) + "\ncommit;"


def _run_sql_file(cred: "Cred", sql: str, *, timeout: float) -> tuple[int, str]:
    import subprocess

    def _once(env: dict) -> tuple[int, str]:
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as tf:
            tf.write(sql)
            tmp = pathlib.Path(tf.name)
        try:
            proc = subprocess.run(
                ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", str(tmp)],
                env=env, capture_output=True, text=True, timeout=timeout,
            )
        finally:
            tmp.unlink(missing_ok=True)
        return proc.returncode, (proc.stderr or "")

    rc, stderr = _once(cred.env)
    if rc != 0 and _is_auth_fail(stderr):
        cred.refresh()
        rc, stderr = _once(cred.env)
    return rc, redact(stderr.strip().splitlines()[-1] if stderr.strip() else "")


_PRELUDE = f"SET ROLE {ELEVATE_ROLE};\nset lock_timeout='30s';\nset statement_timeout='600s';\n"


def write_item(cred: "Cred", state: LD.PythonRepresentationState, docs: tuple) -> dict[str, Any]:
    """Atomically replace one item's workflow_python state + docs as postgres."""
    sql = _PRELUDE + _render_item_sql(state, docs)
    last_err = ""
    for attempt in range(1, MAX_RETRIES + 1):
        rc, err = _run_sql_file(cred, sql, timeout=620)
        if rc == 0:
            return {"ok": True, "attempts": attempt}
        last_err = err or "unknown"
        time.sleep(0.4 * attempt)
    return {"ok": False, "attempts": MAX_RETRIES, "error": last_err}


def write_batch(cred: "Cred", batch: list[tuple[LD.PythonRepresentationState, tuple]]) -> dict[str, Any]:
    """Write many items in ONE transaction (one pooled connection). On batch
    failure, fall back to per-item writes so a single anomaly is isolated and
    the rest of the batch still commits. Each per-item write is itself atomic."""
    if not batch:
        return {"ok": True, "per_item": 0, "failures": []}
    inner = "\n".join(_item_inner_sql(st, dc) for st, dc in batch)
    sql = _PRELUDE + "begin;\n" + inner + "\ncommit;"
    rc, err = _run_sql_file(cred, sql, timeout=620)
    if rc == 0:
        return {"ok": True, "per_item": 0, "failures": [], "items": len(batch)}
    # Batch failed: isolate by writing each item individually.
    failures: list[dict[str, Any]] = []
    written = 0
    for st, dc in batch:
        one = write_item(cred, st, dc)
        if one["ok"]:
            written += 1
        else:
            failures.append({"item_id": st.resource_id, "error": one.get("error"),
                             "attempts": one.get("attempts")})
    return {"ok": not failures, "per_item": written, "failures": failures,
            "batch_error": err, "items": len(batch)}


# ---------------------------------------------------------------------------
# Phase-2 (schema/012) per-item workflow_python search materialized view.
# ---------------------------------------------------------------------------
# The fragment arm of hivemind_lexical_candidates (schema/012) reads
# lexical_workflow_python_search instead of scanning every workflow_python
# chunk. The MV depends ONLY on lexical_documents, so it must be REFRESHed after
# every lexical_documents rebuild (this operator path) to stay consistent. The MV
# is created + initially populated by schema/012 itself; this function keeps it
# fresh on each refresh run.

MV_NAME = "public.lexical_workflow_python_search"


def _mv_present(cred: "Cred") -> bool:
    """True iff the MV exists (schema/012 applied). Read-only, no error raise."""
    r = psql_retry(cred, elevate(
        "select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace "
        "where c.relname='lexical_workflow_python_search' and n.nspname='public' "
        "and c.relkind='m';"
    ), timeout=30, on_error_stop=False)
    return (r.returncode == 0 and (r.stdout or "").strip() not in ("", "0"))


# The bare relation name the schema/012 fragment arm reads. It appears in the
# installed ``hivemind_lexical_candidates`` body ONLY when schema/012 is the
# active read path (schema/008 and schema/010 bodies never reference it), so its
# presence in the function body is the precise "schema/012 is live" signal.
_MV_RELNAME = "lexical_workflow_python_search"


def candidate_fn_uses_search_mv(prosrc: Any) -> bool:
    """Pure (no DB, no I/O): True iff a candidate-function BODY string references
    the schema/012 per-item search MV ``lexical_workflow_python_search``.

    Sanitized + unit-testable: ``prosrc`` is already-fetched catalog text (never
    a secret or user payload), and the relation name is a fixed schema object, so
    a plain substring test is exact — no normalization. This predicate is what
    makes "MV absent" mean two opposite things:

      * False -> the pre-012 (schema/008 or 010) per-chunk read path is live; the
        MV is NOT on the read path, so an absent MV is a COMPATIBLE no-op
        (detected + warned, never a silent pass, and not a failure).
      * True  -> the schema/012 read path is live; the fragment arm reads the MV,
        so an absent MV is a HARD failure — the search depends on a missing
        object and must not be reported as success.
    """
    return _MV_RELNAME in (prosrc or "")


def _candidate_fn_prosrc(cred: "Cred") -> str | None:
    """Read-only: the installed ``public.hivemind_lexical_candidates`` body, or
    None if the function is not installed / unreadable. Pinned by
    ``pronargs=10`` (the single overload mirrors schema/008 + schema/012) so the
    exact function is matched regardless of search_path; bodies are aggregated so
    a stray overload cannot hide the reference. Read via ``psql -t -A`` like every
    other coverage counter (no payload reaches stdout)."""
    r = psql_retry(cred, elevate(
        "select coalesce(string_agg(p.prosrc, ''), '') from pg_proc p "
        "join pg_namespace n on n.oid=p.pronamespace "
        "where n.nspname='public' and p.proname='hivemind_lexical_candidates' "
        "and p.pronargs = 10;"
    ), timeout=30, on_error_stop=False)
    if r.returncode != 0:
        return None
    body = (r.stdout or "").strip()
    return body or None


def search_mv_read_path_active(cred: "Cred") -> bool:
    """Read-only: True iff the installed candidate function body references the
    schema/012 search MV — i.e. the schema/012 read path is the live path. When
    True, an absent or stale MV is a HARD correctness requirement (the fragment
    arm reads it). When False, the live path is still schema/010 (per-chunk) and
    the MV is not yet required. If the candidate function is not installed at
    all, returns False (no read path to break)."""
    body = _candidate_fn_prosrc(cred)
    if not body:
        return False
    return candidate_fn_uses_search_mv(body)


def refresh_search_mv(cred: "Cred") -> dict[str, Any]:
    """Refresh the per-item workflow_python search MV after a lexical_documents
    rebuild so the schema/012 fragment arm stays consistent.

    Strategy:
      * If the MV does not exist, the outcome depends on which read path is live
        (``search_mv_read_path_active``):
          - pre-012 function active (schema/008 or 010 body, no MV reference):
            report ``absent`` and succeed — there is nothing to refresh, and the
            live read path is still the schema/010 per-chunk path.
          - schema/012 function active (body references the MV): HARD FAILURE
            (``ok=False``). The fragment arm reads the MV, so a missing MV means
            the search depends on a dropped object — reporting success here would
            mask a broken read path. Re-apply schema/012 to recreate it.
      * Otherwise REFRESH ... CONCURRENTLY first (non-blocking for readers; it
        requires the unique index ``lexical_workflow_python_search_item_uidx``
        AND an already-populated MV — both true post-012).
      * On ANY concurrent-refresh failure, fall back to a plain (blocking)
        REFRESH. This is the deterministic first-population path too: CONCURRENTLY
        cannot populate an empty MV, so the very first operator refresh after
        apply (if the migration's initial REFRESH were ever skipped) lands here.

    CONCURRENTLY cannot run inside a transaction; ``_run_sql_file`` runs each
    statement autocommitted via ``psql -f`` (no BEGIN/COMMIT), so this is safe.
    """
    if not _mv_present(cred):
        if search_mv_read_path_active(cred):
            return {"ok": False, "absent": True, "concurrently": None,
                    "error": "schema/012 read path active "
                             "(hivemind_lexical_candidates references "
                             "lexical_workflow_python_search) but the MV is "
                             "ABSENT — the fragment arm depends on the missing "
                             "MV; re-apply schema/012 to recreate it"}
        return {"ok": True, "absent": True, "concurrently": None,
                "note": "MV absent and pre-012 read path active (schema/012 not "
                        "applied or MV dropped); compatible no-op"}

    conc = (f"SET ROLE {ELEVATE_ROLE};\n"
            "refresh materialized view concurrently public.lexical_workflow_python_search;")
    rc, err = _run_sql_file(cred, conc, timeout=600)
    if rc == 0:
        return {"ok": True, "absent": False, "concurrently": True}

    # Deterministic fallback: plain (blocking) refresh. Handles first population
    # (MV never refreshed) and any transient concurrent-refresh failure.
    plain = (f"SET ROLE {ELEVATE_ROLE};\n"
             "refresh materialized view public.lexical_workflow_python_search;")
    rc2, err2 = _run_sql_file(cred, plain, timeout=900)
    return {"ok": rc2 == 0, "absent": False, "concurrently": False,
            "error": redact(err2) if rc2 else "",
            "concurrent_error": redact(err)}


# ---------------------------------------------------------------------------
# Coverage verification (read-only)
# ---------------------------------------------------------------------------


# NOTE: every counter name emitted by ``verify_coverage``'s SQL must also appear
# as a literal in verify_coverage's source. The unit test
# ``test_coverage_sql_contains_counters`` enforces this pairing stays in sync.
COVERAGE_COUNTER_NAMES = (
    "workflows_total", "state_rows", "by_cohort", "by_public_state",
    "python_chunks", "python_distinct_items", "quarantined_docs",
    "body_duplicate_state_rows", "unrefreshed_workflows",
    "safe_available_python_rows", "unavailable_safe_rows", "quarantined_rows",
    "chunk_count_mismatches", "safe_plus_quarantined_minus_unavailable_safe",
    # Phase-2 (schema/012) MV coverage counters (emitted as literal keys in
    # verify_coverage; the lexical_documents vs MV distinct-item comparison is
    # the stale-MV detector — see evaluate_mv_coverage_ok).
    "mv_present", "mv_distinct_items", "mv_expected_distinct_items",
)


def evaluate_coverage_ok(cov: dict[str, Any]) -> tuple[bool, list[str]]:
    """Pure (no DB, no I/O) coverage decision.

    Returns ``(ok, reasons)`` where ``reasons`` lists every failing check with
    actual vs expected. ``ok`` is True only when ALL of the following hold:

    - ``state_rows == workflows_total``           (one state row per workflow)
    - ``unrefreshed_workflows == 0``              (no workflow missing a state row)
    - ``quarantined_docs == 0``                   (no quarantined doc leaked into the index)
    - ``duplicate_chunk_hashes_within_item == 0`` (no duplicated chunk within an item)
    - ``python_distinct_items == safe_available_python_rows``
        (THE FIX: only safe+available python is chunked. The old code compared
        distinct_items against ``by_public_state.safe`` which counts every safe
        state row, including intentionally-UNAVAILABLE workflows that correctly
        produce ZERO docs.)
    - ``chunk_count_mismatches == 0``
        (the stored ``state.chunk_count`` agrees with the actual
        ``lexical_documents`` doc count for each safe+available item)
    - ``safe_plus_quarantined_minus_unavailable_safe == state_rows``
        (the public_state partition sums correctly: safe + quarantined covers
        every state row, and unavailable-safe rows are the safe-but-zero-doc
        subset — the partition identity is ``safe + quarantined == state_rows``
        with ``unavailable_safe_rows`` a subset of safe.)

    All numeric comparisons are normalized: SQL may return counters as strings
    (e.g. the ``::text`` duplicate-hash count) so they are coerced via ``int``.
    """
    reasons: list[str] = []

    def _int(key: str) -> int | None:
        v = cov.get(key)
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    workflows_total = _int("workflows_total")
    state_rows = _int("state_rows")
    unrefreshed = _int("unrefreshed_workflows")
    quarantined_docs = _int("quarantined_docs")
    dup_hash = _int("duplicate_chunk_hashes_within_item")
    distinct_items = _int("python_distinct_items")
    safe_available = _int("safe_available_python_rows")
    chunk_mismatches = _int("chunk_count_mismatches")
    partition = _int("safe_plus_quarantined_minus_unavailable_safe")

    if state_rows is None or workflows_total is None or state_rows != workflows_total:
        reasons.append(
            f"state_rows={state_rows} != workflows_total={workflows_total} "
            f"(expected one state row per workflow)"
        )

    if unrefreshed is None or unrefreshed != 0:
        reasons.append(
            f"unrefreshed_workflows={unrefreshed} != 0 "
            f"(expected every workflow to have a state row)"
        )

    if quarantined_docs is None or quarantined_docs != 0:
        reasons.append(
            f"quarantined_docs={quarantined_docs} != 0 "
            f"(expected no quarantined doc row in lexical_documents)"
        )

    if dup_hash is None or dup_hash != 0:
        reasons.append(
            f"duplicate_chunk_hashes_within_item={dup_hash} != 0 "
            f"(expected no duplicated chunk_hash within any item)"
        )

    # THE FIX: distinct indexed items must equal the safe+available python rows,
    # NOT ``by_public_state.safe`` (which also counts unavailable-safe rows).
    if distinct_items is None or safe_available is None or distinct_items != safe_available:
        reasons.append(
            f"python_distinct_items={distinct_items} != "
            f"safe_available_python_rows={safe_available} "
            f"(drift: expected one indexed item per safe+available python workflow)"
        )

    if chunk_mismatches is None or chunk_mismatches != 0:
        reasons.append(
            f"chunk_count_mismatches={chunk_mismatches} != 0 "
            f"(expected stored state.chunk_count to equal actual doc count per item)"
        )

    if (partition is None or state_rows is None) or partition != state_rows:
        reasons.append(
            f"safe_plus_quarantined_minus_unavailable_safe={partition} != "
            f"state_rows={state_rows} "
            f"(expected public_state partition to sum to state_rows)"
        )

    ok = not reasons
    return ok, reasons


def evaluate_mv_coverage_ok(cov: dict[str, Any]) -> tuple[bool, list[str]]:
    """Pure (no DB, no I/O) decision for the schema/012 search-MV dimension.

    The MV ``lexical_workflow_python_search`` is the surface the fragment arm
    reads; it must mirror the safe, in-range workflow_python items in
    ``lexical_documents``. Returns ``(ok, reasons)``:

      * MV ABSENT (``mv_present`` false): the verdict depends on
        ``mv_read_path_active`` (set by ``verify_coverage`` from
        ``search_mv_read_path_active``):
          - pre-012 read path (``mv_read_path_active`` false): ``ok=True``. The
            MV is not on the live read path (schema/008 or 010 per-chunk), so MV
            coverage is not yet a correctness requirement. DETECTED + SURFACED
            (``cov['mv_present']=False`` + a warning in ``verify_coverage``),
            never a silent pass, but not a failure.
          - schema/012 read path (``mv_read_path_active`` true): ``ok=False`` —
            HARD FAILURE. The fragment arm reads the MV, so an absent MV means
            the search depends on a missing object; reporting success would mask
            a broken read path. ``mv_read_path_active`` defaults to False when
            absent from the dict (callers that omit it get the pre-012 reading,
            matching the historical no-MV behavior).
      * MV PRESENT but counts unreadable: ``ok=False`` (cannot prove coverage).
      * MV PRESENT and ``mv_distinct_items != mv_expected_distinct_items``:
        ``ok=False`` — STALE. The MV must be refreshed (refresh_search_mv) after
        lexical_documents changed.
      * MV PRESENT and counts match: ``ok=True``.

    All numeric comparisons normalize strings from SQL via ``int``.
    """
    reasons: list[str] = []

    def _int(v: Any) -> int | None:
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    present = cov.get("mv_present")
    if not present:
        if cov.get("mv_read_path_active"):
            reasons.append(
                "schema/012 read path active (hivemind_lexical_candidates "
                "references lexical_workflow_python_search) but the MV is ABSENT "
                "— the fragment arm depends on the missing MV; re-apply "
                "schema/012 to recreate + populate it"
            )
            return False, reasons
        return True, []  # pre-012 read path: absent MV is compatible (warned)

    di = _int(cov.get("mv_distinct_items"))
    exp = _int(cov.get("mv_expected_distinct_items"))
    if di is None or exp is None:
        reasons.append(
            f"mv_present=true but counts unreadable: mv_distinct_items="
            f"{cov.get('mv_distinct_items')!r} mv_expected_distinct_items="
            f"{cov.get('mv_expected_distinct_items')!r}"
        )
        return False, reasons
    if di != exp:
        reasons.append(
            f"mv stale: lexical_workflow_python_search mv_distinct_items={di} != "
            f"mv_expected_distinct_items={exp} (run the lexical refresh to REFRESH "
            f"the MV after lexical_documents changes)"
        )
        return False, reasons
    return True, []


def _mv_coverage(cred: "Cred") -> dict[str, Any]:
    """Read-only MV coverage: present + distinct-item count vs the expected count
    derived from lexical_documents. The MV may be absent (pre-012); the lookup is
    a separate psql call so an absent MV is reported, not raised.
    """
    if not _mv_present(cred):
        return {"present": False, "distinct_items": None, "expected_distinct_items": None}
    r = psql_retry(cred, elevate(
        "select (select count(*) from public.lexical_workflow_python_search)::text "
        "|| '|' || (select count(distinct item_id) from public.lexical_documents "
        "where representation_type='workflow_python' and quarantine_state='safe' "
        "and char_length(chunk_text) between 1 and 8000)::text;"
    ), timeout=60, on_error_stop=False)
    if r.returncode != 0:
        return {"present": True, "distinct_items": None, "expected_distinct_items": None,
                "error": redact((r.stderr or "").strip())}
    parts = (r.stdout or "").strip().split("|")
    di = parts[0] if len(parts) > 0 else None
    exp = parts[1] if len(parts) > 1 else None
    return {"present": True, "distinct_items": di, "expected_distinct_items": exp}


def verify_coverage(cred: "Cred") -> dict[str, Any]:
    """Every workflow is safe+indexed, quarantined, or unavailable with one count.

    Chunks are written ONLY for ``public_state='safe' AND available=true AND
    cohort IN ('payload_python','body_python','recoverable')``. So the healthy
    invariant is ``python_distinct_items == safe_available_python_rows`` — NOT
    ``by_public_state.safe`` (which counts every safe state row, including the
    intentionally-UNAVAILABLE workflows that correctly produce ZERO docs).
    """
    r = psql_retry(cred, elevate("""
      select jsonb_build_object(
        'workflows_total', (select count(*) from public.external_resources where kind='workflow'),
        'state_rows', (select count(*) from public.lexical_resource_python_state),
        'by_cohort', (select jsonb_object_agg(cohort, c) from
          (select cohort, count(*) c from public.lexical_resource_python_state group by cohort) x),
        'by_public_state', (select jsonb_object_agg(public_state, c) from
          (select public_state, count(*) c from public.lexical_resource_python_state group by public_state) y),
        'python_chunks', (select count(*) from public.lexical_documents where representation_type='workflow_python'),
        'python_distinct_items', (select count(distinct item_id) from public.lexical_documents where representation_type='workflow_python'),
        'quarantined_docs', (select count(*) from public.lexical_documents where quarantine_state='quarantined'),
        'body_duplicate_state_rows', (select count(*) from public.lexical_resource_python_state where body_duplicate=true),
        'unrefreshed_workflows', (select count(*) from public.external_resources r where r.kind='workflow'
           and not exists (select 1 from public.lexical_resource_python_state s where s.resource_id=r.id)),
        'safe_available_python_rows', (select count(*) from public.lexical_resource_python_state
           where public_state='safe' and available=true
             and cohort in ('payload_python','body_python','recoverable')),
        'unavailable_safe_rows', (select count(*) from public.lexical_resource_python_state
           where public_state='safe' and available=false),
        'quarantined_rows', (select count(*) from public.lexical_resource_python_state
           where public_state='quarantined'),
        'chunk_count_mismatches', (select count(*) from (
           select s.resource_id
             from public.lexical_resource_python_state s
             left join (
               select item_id, count(*) c
                 from public.lexical_documents
                where representation_type='workflow_python'
                group by item_id
             ) d on d.item_id = s.resource_id::text
            where s.public_state='safe' and s.available=true
              and coalesce(s.chunk_count, 0) <> coalesce(d.c, 0)
           ) m),
        'safe_plus_quarantined_minus_unavailable_safe',
           (select (select count(*) from public.lexical_resource_python_state where public_state='safe')
                 + (select count(*) from public.lexical_resource_python_state where public_state='quarantined'))
      )::text;
    """), timeout=60, on_error_stop=False)
    if r.returncode != 0:
        return {"ok": False, "error": redact((r.stderr or "").strip())}
    cov = json.loads(r.stdout.strip())

    # No duplicated chunk_hash within any item (kept as a separate ::text query
    # for plan readability; normalized via int() in evaluate_coverage_ok).
    dup = psql_retry(cred, elevate(
        "select count(*)::text from (select item_id, chunk_hash, count(*) c from public.lexical_documents "
        "where representation_type='workflow_python' group by item_id, chunk_hash having count(*)>1) z;"
    ), timeout=60, on_error_stop=False)
    cov["duplicate_chunk_hashes_within_item"] = (dup.stdout or "").strip() or "?"

    ok, reasons = evaluate_coverage_ok(cov)

    # Phase-2 (schema/012) MV coverage. The literal keys below MUST stay in sync
    # with COVERAGE_COUNTER_NAMES (test_coverage_sql_contains_counters enforces it).
    mv = _mv_coverage(cred)
    cov["mv"] = mv
    cov["mv_present"] = mv.get("present")
    cov["mv_distinct_items"] = mv.get("distinct_items")
    cov["mv_expected_distinct_items"] = mv.get("expected_distinct_items")
    # Which read path is live drives whether an absent MV is merely a warning
    # (pre-012) or a hard failure (schema/012). Fed to evaluate_mv_coverage_ok.
    read_path_active = search_mv_read_path_active(cred)
    cov["mv_read_path_active"] = read_path_active
    mv_ok, mv_reasons = evaluate_mv_coverage_ok(cov)
    warnings: list[str] = []
    # An absent MV is only a benign warning under the PRE-012 read path. When the
    # schema/012 read path is active and the MV is absent, evaluate_mv_coverage_ok
    # already failed (mv_ok=False) with an explicit reason — do NOT also emit a
    # benign "not validated" warning that would contradict the hard failure.
    if not mv.get("present") and not read_path_active:
        warnings.append(
            "lexical_workflow_python_search MV absent and the pre-012 per-chunk "
            "read path is active (schema/012 not applied or MV dropped) — the MV "
            "is not yet on the live path, so MV coverage is not validated. Apply "
            "schema/012 + run the refresh to enable it."
        )
    cov["warnings"] = warnings

    cov["ok"] = bool(ok and mv_ok)
    cov["reasons"] = reasons + mv_reasons
    return cov


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def _tally(action: str, state: LD.PythonRepresentationState) -> None:
    """Sanitized progress accumulator hook (no code/secrets)."""
    # Intentionally a no-op placeholder; aggregation happens in the driver.
    return None


BATCH_SIZE = 64
BATCH_BYTES = 3_000_000  # flush early if a batch carries a huge-workflow payload


def run(cred: "Cred", *, apply: bool, since_id: int = 0) -> dict[str, Any]:
    existing = fetch_existing_states(cred) if apply else {}
    tally = {"upsert": 0, "skip": 0, "quarantine": 0, "unavailable": 0, "drop": 0, "fail": 0}
    anomalies: list[dict[str, Any]] = []
    high_water = since_id
    quarantined_reasons: dict[str, int] = {}
    sample_payload_hashes: list[str] = []  # hashes only (safe)
    batch: list[tuple[LD.PythonRepresentationState, tuple]] = []
    batch_bytes = 0
    n_batches = 0

    def _est_bytes(st: LD.PythonRepresentationState, docs: tuple) -> int:
        return sum(len(d.chunk_text) for d in docs) + 256

    def _flush() -> None:
        nonlocal batch, batch_bytes, n_batches
        if not batch:
            return
        res = write_batch(cred, batch)
        n_batches += 1
        if res.get("failures"):
            for f in res["failures"]:
                tally["fail"] += 1
                anomalies.append({"item_id": f["item_id"], "action": "fail", "error": f.get("error"),
                                  "attempts": f.get("attempts")})
        batch, batch_bytes = [], 0

    for row in fetch_workflow_pages(cred, since_id=since_id):
        item_id = str(row["id"])
        high_water = max(high_water, int(row["id"]))
        try:
            state, docs = LD.compute_workflow_python_documents(row)
        except LD.CoverageError:
            tally["fail"] += 1
            anomalies.append({"item_id": item_id, "action": "fail", "reason": "coverage_error"})
            continue

        prev = existing.get(item_id)
        if apply and prev is not None and _same_freshness(prev, state):
            tally["skip"] += 1
            continue

        if state.public_state == WR.PUBLIC_STATE_QUARANTINED:
            action = "quarantine"
            for code in state.secret_reason_codes:
                quarantined_reasons[code] = quarantined_reasons.get(code, 0) + 1
        elif not state.available:
            action = "unavailable"
        elif not docs:
            action = "drop"
        else:
            action = "upsert"

        if apply:
            eb = _est_bytes(state, docs)
            # Flush before adding if this item would overflow the batch (huge
            # workflow payloads get their own small/sole transaction).
            if len(batch) >= BATCH_SIZE or batch_bytes + eb > BATCH_BYTES:
                _flush()
            batch.append((state, docs))
            batch_bytes += eb
        tally[action] = tally.get(action, 0) + 1
        if action == "upsert" and len(sample_payload_hashes) < 12:
            sample_payload_hashes.append(state.representation_hash or "")

    if apply:
        _flush()
        # Phase-2 (schema/012): keep the per-item workflow_python search MV in
        # sync with the freshly rebuilt lexical_documents. The fragment arm reads
        # the MV; a stale/empty MV would silently drop workflow_python recall.
        # Absent MV succeeds only under the pre-012 read path; under schema/012
        # an absent MV fails closed (the read path depends on it).
        mv_refresh = refresh_search_mv(cred)

    report = {
        "mode": "apply" if apply else "plan",
        "since_id": since_id,
        "high_water_id": high_water,
        "tally": tally,
        "quarantined_reason_codes": quarantined_reasons,
        "sample_representation_hashes": sample_payload_hashes,
        "anomalies": anomalies,
        "n_write_batches": n_batches,
    }
    if apply:
        report["mv_refresh"] = mv_refresh
        report["coverage"] = verify_coverage(cred)
    return report


def rollback(cred: "Cred") -> dict[str, Any]:
    """Delete ALL derived workflow_python rows + state. Source untouched. Idempotent."""
    r = psql_retry(cred, elevate(
        f"SET ROLE {ELEVATE_ROLE};\n"
        "delete from public.lexical_documents where representation_type='workflow_python';\n"
        "delete from public.lexical_resource_python_state;\n"
        "select (select count(*) from public.lexical_documents where representation_type='workflow_python')||'|'"
        "||(select count(*) from public.lexical_resource_python_state);"
    ), timeout=120, on_error_stop=False)
    leftover = (r.stdout or "").strip()
    ok = (r.returncode == 0)
    # Keep the phase-2 search MV consistent with the now-emptied lexical_documents.
    # Absent MV succeeds only under the pre-012 read path; under schema/012 it
    # fails closed. Otherwise REFRESH empties the MV to match the cleared docs.
    mv_refresh = refresh_search_mv(cred) if ok else {"ok": False, "skipped": True}
    return {"ok": ok, "leftover_docs_state": leftover or "ERR",
            "mv_refresh": mv_refresh,
            "error": redact((r.stderr or "").strip()) if r.returncode else ""}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Live lexical workflow-Python refresh (task 1.2 backfill).")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true", help="dry-run: compute only, no writes")
    mode.add_argument("--apply", action="store_true", help="write lexical_documents + state")
    mode.add_argument("--verify", action="store_true", help="read-only coverage report")
    mode.add_argument("--rollback", action="store_true", help="delete derived rows")
    p.add_argument("--since-id", type=int, default=0, help="resume high-water (workflow id)")
    args = p.parse_args(argv)

    cred = Cred()

    if args.verify:
        rep = verify_coverage(cred)
        print(json.dumps(rep, indent=2, default=str))
        return 0 if rep.get("ok") else 1

    if args.rollback:
        rep = rollback(cred)
        print(json.dumps(rep, indent=2, default=str))
        return 0 if rep.get("ok") else 1

    rep = run(cred, apply=args.apply, since_id=args.since_id)
    EVIDENCE_OUT.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_OUT.write_text(json.dumps(rep, indent=2, default=str) + "\n")
    print(json.dumps(rep, indent=2, default=str))
    return 0 if (rep["tally"].get("fail", 0) == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
