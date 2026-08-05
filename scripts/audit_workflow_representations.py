#!/usr/bin/env python3
"""Audit all 222 authoritative Python workflow representations in PRODUCTION (read-only).

Requirement F. Validates the 222 ``kind='workflow`` resources whose
``external_resources.payload->>'python_source'`` is non-empty (the authoritative
``payload_python`` cohort). For each one it re-derives, IN MEMORY ONLY, the
frozen classification (resolve / scan / chunk / coverage) and cross-checks it
against the derived ``lexical_resource_python_state`` row already in production.

SECURITY (load-bearing — read before editing):
  * The Python SOURCE is fetched into memory and NEVER printed, logged, written,
    serialized, or included in any tally. Only the opaque numeric resource id,
    non-secret reason codes, and COUNTS are emitted.
  * ``scan_secrets`` returns reason codes + positions only; the matched value is
    never exposed. We do not read ``SecretFinding.detail_kind`` into output
    beyond what is already a non-secret reason code vocabulary.
  * Output is a single sanitized JSON report; no source / snippet / secret value
    ever reaches stdout, stderr, or disk.

The script does NOT mutate production. It does NOT overwrite authoritative
``payload.python_source``, does NOT infer code with an LLM, and does NOT publish
any credential. Failures are reported with their opaque resource id + class only.

Usage::

    python3 scripts/audit_workflow_representations.py
"""

from __future__ import annotations

import argparse
import ast
import json
import pathlib
import sys
from collections import Counter
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO))  # so ``executors.*`` imports resolve

# Frozen reference contracts (single source of truth — production mirrors these).
from executors import workflow_representation as WR  # noqa: E402
from executors import lexical_documents as LD  # noqa: E402

# Live production access path (read-only session-mode libpq env).
from live_lexical_search import derive_pg_env, psql, elevate  # noqa: E402

REPORT_PATH = REPO / "docs" / "hybrid-search" / "phase1-workflow-representation-audit.json"


# ---------------------------------------------------------------------------
# SQL: fetch the 222 authoritative rows + their derived state (source in memory)
# ---------------------------------------------------------------------------
# ``python_source`` is selected ONLY so we can parse/scan/chunk it in memory. It
# is NEVER echoed. The COPY-style psql -t -A output is split on the first '|'
# per row so a source containing pipes cannot corrupt the column boundary — we
# use a sentinel-delimited protocol below, not naive split('|', N).

# Transport: a SINGLE JSON document for the whole result set. JSON guarantees
# unambiguous field boundaries regardless of pipes, quotes, newlines, or control
# chars inside ``python_source`` (sources can be hundreds of KB with arbitrary
# text). The document is parsed in memory; ``python_source`` lives only in the
# returned dict under ``_src`` and is never printed/logged/written.
FETCH_SQL = r"""
select coalesce(json_agg(t), '[]'::json)::text
from (
  select r.id::text as id,
         coalesce(s.cohort, '') as cohort,
         coalesce(s.public_state, '') as public_state,
         coalesce(s.available::text, '') as available,
         coalesce(s.chunk_count, 0) as chunk_count,
         coalesce(s.secret_reason_codes, '{}'::text[]) as secret_reason_codes,
         coalesce(s.representation_hash, '') as representation_hash,
         r.payload->>'python_source' as python_source
  from external_resources r
  left join lexical_resource_python_state s on s.resource_id = r.id
  where r.kind = 'workflow'
    and coalesce(r.payload->>'python_source', '') <> ''
  order by r.id
) t
"""


def _fetch_rows(env: dict) -> list[dict[str, Any]]:
    """Fetch the 222 authoritative rows into memory as one JSON document.

    The python_source lives ONLY in the returned dict under ``_src`` and is
    never printed. The dict is consumed by :func:`_classify` and discarded.
    """
    proc = psql(env, elevate(FETCH_SQL), timeout=180.0, on_error_stop=False)
    if proc.returncode != 0:
        # Do NOT echo the query or full stderr (may carry env/connection detail).
        tail = (proc.stderr or "").strip().splitlines()
        raise RuntimeError(f"fetch failed rc={proc.returncode}; stderr_tail={tail[-2:] if tail else []}")
    # psql -t -A emits the JSON value on one line; join in case of wrapping.
    text = (proc.stdout or "").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"fetch JSON parse failed: {exc.__class__.__name__}") from exc
    if not isinstance(data, list):
        raise RuntimeError(f"fetch returned non-list JSON: {type(data).__name__}")
    rows: list[dict[str, Any]] = []
    for d in data:
        avail = d.get("available")
        rows.append(
            {
                "id": d.get("id"),
                "state_cohort": d.get("cohort", ""),
                "state_public_state": d.get("public_state", ""),
                "state_available": (avail == "t") if isinstance(avail, str) else bool(avail),
                "state_chunk_count": int(d.get("chunk_count") or 0),
                "state_reason_codes": tuple(d.get("secret_reason_codes") or []),
                "state_representation_hash": d.get("representation_hash", ""),
                "_src": d.get("python_source", "") or "",
            }
        )
    return rows


# ---------------------------------------------------------------------------
# In-memory classification (mirrors the frozen executors exactly)
# ---------------------------------------------------------------------------


def _classify(row: dict[str, Any]) -> dict[str, Any]:
    """Classify one authoritative row IN MEMORY. Never emits source.

    Classification precedence (mutually exclusive outcome ``klass``):
      * secret_quarantined  — fresh ``scan_secrets`` finds a match (regardless
                              of parse success). Mirrors production: a hit
                              quarantines before chunking.
      * syntactically_valid — ``ast.parse`` succeeds AND no secret.
      * parser_fallback     — ``ast.parse`` raises SyntaxError AND the frozen
                              ``chunk_python`` fallback path still produces
                              full coverage (a recoverable huge-literal /
                              unparseable block). Mirrors executors/chunking.py.
      * coverage_failed     — parses (or fallbacks) but ``coverage_ok`` is False
                              (the fail-closed no-silent-truncation guard).
      * invalid_source      — cannot be parsed AND no recoverable fallback.
    """
    src = row.get("_src", "")
    # Fresh scan (the frozen scanner; reason codes only, never the value).
    findings = WR.scan_secrets(src) if src else []
    fresh_reason_codes = tuple(sorted({f.reason_code for f in findings}))

    if findings:
        klass = "secret_quarantined"
        # Still report parse/coverage status for diagnostics, but the klass is
        # dominated by the quarantine decision (matches production).
        parse_ok = _try_parse(src)
        return {
            "klass": klass,
            "fresh_reason_codes": fresh_reason_codes,
            "parses": parse_ok,
            "coverage_ok": None,  # not evaluated when quarantined (no chunks built)
        }

    # No secret: evaluate parse / chunk / coverage exactly as production does.
    parse_ok, syntax_error_kind = _try_parse_detail(src)
    try:
        chunks = WR.chunk_python(
            src,
            target_tokens=LD.LC.WORKFLOW_PYTHON_CHUNK_TARGET_TOKENS,
            overlap_tokens=LD.LC.WORKFLOW_PYTHON_CHUNK_OVERLAP_TOKENS,
        )
        cov = WR.coverage_ok(src, chunks) if chunks else False
    except Exception:
        chunks = []
        cov = False

    if parse_ok:
        if cov:
            klass = "syntactically_valid"
        else:
            klass = "coverage_failed"
    else:
        # Source has a syntax error: is it a recoverable fallback (full coverage
        # via the ast_fallback path) or genuinely broken?
        if chunks and cov:
            klass = "parser_fallback"
        else:
            klass = "invalid_source"

    return {
        "klass": klass,
        "fresh_reason_codes": fresh_reason_codes,
        "parses": parse_ok,
        "syntax_error_kind": syntax_error_kind,
        "coverage_ok": cov,
        "fresh_chunk_count": len(chunks),
    }


def _try_parse(src: str) -> bool:
    try:
        ast.parse(src)
        return True
    except SyntaxError:
        return False


def _try_parse_detail(src: str) -> tuple[bool, str | None]:
    try:
        ast.parse(src)
        return True, None
    except SyntaxError as exc:
        # Sanitized: only the exception class name + frozen message category.
        # NEVER include the offending line text (exc.text may carry source).
        return False, type(exc).__name__


# ---------------------------------------------------------------------------
# Cross-check: live derived state vs fresh in-memory classification
# ---------------------------------------------------------------------------


def _disagreements(row: dict[str, Any], fresh: dict[str, Any]) -> list[str]:
    """Return a list of human-readable disagreement codes (no source).

    A disagreement is a real defect or staleness signal:
      * state_secret_stale   — state says safe but fresh scan finds a secret
                               (a secret was introduced AFTER the last refresh,
                                or the scanner version bumped — real defect).
      * state_quarantine_stale — state says quarantined but fresh scan is clean
                                 (source was edited to remove the secret, or a
                                  scanner version bump retired a pattern).
      * state_chunk_drift    — state chunk_count differs from fresh chunk count
                               for a safe+available row (scanner/chunker drift).
      * state_missing        — no state row exists for an authoritative source.
      * state_hash_drift     — state representation_hash differs from fresh hash.
    """
    codes: list[str] = []
    state_state = row.get("state_public_state", "")
    fresh_has_secret = fresh["klass"] == "secret_quarantined"

    if not state_state:
        codes.append("state_missing")
        return codes

    if state_state == WR.PUBLIC_STATE_SAFE and fresh_has_secret:
        codes.append("state_secret_stale")
    if state_state == WR.PUBLIC_STATE_QUARANTINED and not fresh_has_secret:
        codes.append("state_quarantine_stale")

    # Chunk drift only meaningful for safe+available rows that are not
    # quarantined and not coverage_failed/invalid.
    if (
        state_state == WR.PUBLIC_STATE_SAFE
        and row.get("state_available")
        and fresh["klass"] in ("syntactically_valid", "parser_fallback")
        and row.get("state_chunk_count") != fresh.get("fresh_chunk_count")
    ):
        codes.append("state_chunk_drift")

    # Hash drift for safe rows (cheap staleness signal; quarantine rows are not chunked).
    src = row.get("_src", "")
    if state_state == WR.PUBLIC_STATE_SAFE and src:
        fresh_hash = WR.representation_hash(src)
        if row.get("state_representation_hash") and row["state_representation_hash"] != fresh_hash:
            codes.append("state_hash_drift")

    return codes


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run() -> dict[str, Any]:
    env, _host, _port = derive_pg_env()
    rows = _fetch_rows(env)

    total = len(rows)

    # Classify every row in memory; build tallies. Source is discarded after use.
    per_row: list[dict[str, Any]] = []
    class_counts: Counter = Counter()
    cohort_counts: Counter = Counter()
    reason_counts: Counter = Counter()
    fresh_reason_counts: Counter = Counter()
    disagreement_counter: Counter = Counter()
    ids_by_class: dict[str, list[str]] = {
        "secret_quarantined": [],
        "syntactically_valid": [],
        "parser_fallback": [],
        "invalid_source": [],
        "coverage_failed": [],
    }
    disagreement_ids: dict[str, list[str]] = {}

    for r in rows:
        fresh = _classify(r)
        klass = fresh["klass"]
        class_counts[klass] += 1
        ids_by_class.setdefault(klass, []).append(r["id"])
        # Cohort tally uses the LIVE state cohort (all 222 are payload_python).
        cohort_counts[r.get("state_cohort") or "<missing>"] += 1
        for code in r.get("state_reason_codes", ()):
            reason_counts[code] += 1
        for code in fresh.get("fresh_reason_codes", ()):
            fresh_reason_counts[code] += 1

        disagrees = _disagreements(r, fresh)
        for d in disagrees:
            disagreement_counter[d] += 1
            disagreement_ids.setdefault(d, []).append(r["id"])

        per_row.append(
            {
                "id": r["id"],
                "klass": klass,
                "parses": fresh["parses"],
                "coverage_ok": fresh["coverage_ok"],
                "syntax_error_kind": fresh.get("syntax_error_kind"),
                "fresh_reason_codes": list(fresh.get("fresh_reason_codes", [])),
                "fresh_chunk_count": fresh.get("fresh_chunk_count"),
                "state_public_state": r.get("state_public_state"),
                "state_chunk_count": r.get("state_chunk_count"),
                "disagreements": disagrees,
            }
        )

    # Sanitized summary. No source, no secret values, no snippets.
    report = {
        "requirement": "F",
        "read_only": True,
        "mutated_production": False,
        "population": {
            "total_workflow_resources": None,  # filled below
            "authoritative_payload_python_rows": total,
            "parse_errors_at_fetch": 0,  # JSON transport cannot produce a parse error
        },
        "class_counts": {
            "syntactically_valid": class_counts.get("syntactically_valid", 0),
            "parser_fallback": class_counts.get("parser_fallback", 0),
            "invalid_source": class_counts.get("invalid_source", 0),
            "secret_quarantined": class_counts.get("secret_quarantined", 0),
            "coverage_failed": class_counts.get("coverage_failed", 0),
        },
        "cohort_counts": dict(cohort_counts),
        "secret_reason_counts": {
            "from_live_state": dict(reason_counts),
            "from_fresh_scan": dict(fresh_reason_counts),
        },
        "disagreements": {
            "total_rows_with_any_disagreement": sum(1 for p in per_row if p.get("disagreements")),
            "by_code": dict(disagreement_counter),
            "ids_by_code": disagreement_ids,
        },
        "ids_by_failure_class": {
            k: v for k, v in ids_by_class.items() if k in ("invalid_source", "coverage_failed", "parser_fallback", "secret_quarantined")
        },
        "notes": [
            "secret_quarantined counts include any row whose fresh scan_secrets hits, regardless of parse status.",
            "parser_fallback = ast.parse raises SyntaxError but the frozen chunk_python ast_fallback path still achieves full coverage (recoverable huge literal / unparseable block).",
            "coverage_failed = parses (or fallbacks) but coverage_ok is False (fail-closed; would raise CoverageError in the refresh path; zero chunks indexed).",
            "invalid_source = cannot be parsed AND no recoverable fallback (genuinely broken authoritative source).",
            "ids are opaque numeric resource ids; no source/snippet/secret value is emitted by this audit.",
        ],
    }

    # Fill total workflow resources for context.
    proc = psql(
        env,
        elevate("select count(*)::text from external_resources where kind='workflow'"),
        timeout=60.0,
        on_error_stop=False,
    )
    if proc.returncode == 0:
        line = (proc.stdout or "").strip().splitlines()
        if line and line[0].isdigit():
            report["population"]["total_workflow_resources"] = int(line[0])

    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(REPORT_PATH), help="sanitized JSON report path")
    ap.add_argument("--stdout", action="store_true", help="also print the sanitized report")
    args = ap.parse_args()

    report = run()
    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

    # Print ONLY the sanitized summary to stdout (counts + disagreement codes).
    summary = {
        "population": report["population"],
        "class_counts": report["class_counts"],
        "cohort_counts": report["cohort_counts"],
        "secret_reason_counts": report["secret_reason_counts"],
        "disagreements": report["disagreements"],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"\nSanitized report written to: {out_path}")
    if args.stdout:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
