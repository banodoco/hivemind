"""Evaluation runner — measure one system against a golden set.

Wires adapters, metrics, and latency/failure accounting into a single
:class:`SystemReport`. :func:`run_eval` is the library entry point; the
``compare`` CLI (see :mod:`eval.retrieval.compare`) calls it once per system.

Ported shape from Pumpernickel ``eval/retrieval/runner.py`` (per-case Recall/MRR
+ grouped aggregates) and extended for Hivemind with graded nDCG, latency
percentiles, zero-result / no-hit satisfaction, and explicit failure/timeout
accounting.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable

from eval.retrieval.adapters import (
    RetrievalTimeout,
    Retriever,
    RetrieverConfigError,
)
from eval.retrieval.metrics import (
    aggregate,
    aggregate_by_category,
    aggregate_by_group,
    average_precision,
    latency_stats,
    ndcg_at_k,
    rate,
    recall_at_k,
    reciprocal_rank,
)
from eval.retrieval.schema import GoldenCase, GoldenSet, Query, Result

#: Default per-call latency budget (seconds). Offline adapters never approach it;
#: it exists so a runaway remote adapter is recorded as a timeout rather than
#: hanging the run. Configurable per call to :func:`run_eval`.
DEFAULT_TIMEOUT_S: float = 5.0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SystemReport:
    """Everything measured about one system on one golden set."""

    system: str
    corpus_path: str = ""
    golden_set_path: str = ""
    ks: tuple[int, ...] = (1, 5, 10)
    generated_at: str = ""
    overall: dict[str, Any] = field(default_factory=dict)
    by_category: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_entity_kind: dict[str, dict[str, Any]] = field(default_factory=dict)
    latency: dict[str, Any] = field(default_factory=dict)
    counts: dict[str, Any] = field(default_factory=dict)
    per_case: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "system": self.system,
            "corpus_path": self.corpus_path,
            "golden_set_path": self.golden_set_path,
            "ks": list(self.ks),
            "generated_at": self.generated_at,
            "overall": self.overall,
            "by_category": self.by_category,
            "by_entity_kind": self.by_entity_kind,
            "latency": self.latency,
            "counts": self.counts,
            "per_case": self.per_case,
        }


def _entity_kind_bucket(case: GoldenCase) -> str | None:
    """Bucket a case by the entity kind(s) it expects to retrieve."""
    if case.expect_no_hit:
        return None
    kinds = {j.key()[0] for j in case.expected}
    if not kinds:
        return None
    if len(kinds) == 1:
        return next(iter(kinds))
    return "mixed"


def _run_one(
    system: Retriever,
    case: GoldenCase,
    ks: tuple[int, ...],
    timeout_s: float,
) -> dict[str, Any]:
    """Run one case through *system* and collect diagnostics."""
    query = Query(
        query=case.query,
        limit=case.limit,
        filters=case.filters,
        case_id=case.id,
        expected=case.expected,
    )

    outcome = "ok"
    error_msg: str | None = None
    results: list[Result] = []
    t0 = time.perf_counter()
    try:
        results = list(system.retrieve(query))
    except RetrievalTimeout as exc:
        outcome = "timeout"
        error_msg = str(exc)
    except RetrieverConfigError:
        # The whole system is unusable (e.g. remote adapter with no URL / no
        # network opt-in). Abort rather than masking it as per-case errors.
        raise
    except Exception as exc:  # noqa: BLE001 - any adapter failure is a data point
        outcome = "error"
        error_msg = f"{type(exc).__name__}: {exc}"
    elapsed_s = time.perf_counter() - t0
    if outcome == "ok" and elapsed_s > timeout_s:
        outcome = "timeout"

    ranked_keys = [r.key() for r in results]
    zero_result = len(results) == 0

    # Persist safe per-result matched-chunk provenance from the ACTUAL results
    # (not fabricated): the chunk index + representation that won best-chunk
    # collapse for each ranked entity. ``None`` when the adapter did not set it
    # (e.g. a non-semantic adapter), so downstream metrics can distinguish "no
    # provenance" from "chunk 0 won". Never carries raw text or vectors.
    ranked_chunk_index = [getattr(r, "matched_chunk_index", None) for r in results]
    ranked_representation = [getattr(r, "matched_representation", None) for r in results]

    diag: dict[str, Any] = {
        "case_id": case.id,
        "query": case.query,
        "categories": list(case.categories),
        "entity_kind": _entity_kind_bucket(case),
        "expect_no_hit": case.expect_no_hit,
        "is_judged": case.is_judged,
        "outcome": outcome,
        "error": error_msg,
        "latency_ms": round(elapsed_s * 1000.0, 3),
        "expected_count": len(case.expected),
        "retrieved_count": len(results),
        "zero_result": zero_result,
        "ranked": [list(k) for k in ranked_keys],
        "ranked_chunk_index": ranked_chunk_index,
        "ranked_representation": ranked_representation,
    }

    if case.is_judged:
        relevant = case.relevant_keys()
        grade_map = case.grade_map()
        for k in ks:
            diag[f"recall_at_{k}"] = recall_at_k(ranked_keys, relevant, k)
        diag["reciprocal_rank"] = reciprocal_rank(ranked_keys, relevant)
        diag["ndcg_at_10"] = ndcg_at_k(ranked_keys, grade_map, 10)
        diag["average_precision"] = average_precision(ranked_keys, relevant)

    if case.expect_no_hit:
        diag["no_hit_satisfied"] = zero_result

    return diag


# Re-exported alias so adapters can raise a config failure mid-run without an
# import cycle; defined here to keep the exception near the runner that uses it.


def run_eval(
    system: Retriever,
    golden: GoldenSet,
    *,
    ks: tuple[int, ...] = (1, 5, 10),
    timeout_s: float = DEFAULT_TIMEOUT_S,
    now: Callable[[], str] | None = None,
) -> SystemReport:
    """Evaluate *system* against *golden* and return a :class:`SystemReport`.

    Recall/MRR/nDCG are aggregated over **judged** cases only (cases with at
    least one relevant item). No-hit cases contribute to ``no_hit_satisfied_rate``
    instead. Failures (errors + timeouts) contribute 0 to recall aggregates and
    are also reported as their own rates so a crashing system is distinguishable
    from a merely weak one.
    """
    limit_needed = max(ks) if ks else 1
    ks = tuple(ks) or (1, 5, 10)

    per_case: list[dict[str, Any]] = []
    for case in golden.cases:
        # Ensure the case limit can express the largest k we score.
        effective_case = case
        if case.limit < limit_needed:
            # Score at the case's own limit but ensure the adapter returns enough
            # depth to evaluate the largest k; we widen only the retrieve limit.
            effective_case = replace(case, limit=max(case.limit, limit_needed))
        per_case.append(_run_one(system, effective_case, ks, timeout_s))

    judged = [d for d in per_case if d["is_judged"] and d["outcome"] == "ok"]
    judged_any = [d for d in per_case if d["is_judged"]]

    # Recall/MRR/nDCG: failures count as 0 so a broken system is not hidden.
    overall = aggregate(judged_any) if judged_any else _zero_overall()

    by_category = aggregate_by_category(judged_any)
    by_entity_kind = aggregate_by_group(
        [d for d in judged_any if d["entity_kind"] is not None], "entity_kind"
    )

    # Latency over successful calls only (failures have no useful latency).
    ok_latencies = [d["latency_ms"] for d in per_case if d["outcome"] == "ok"]
    lat = latency_stats(ok_latencies)

    n_total = len(per_case)
    n_judged = len(judged_any)
    n_no_hit = sum(1 for d in per_case if d["expect_no_hit"])
    n_zero = sum(1 for d in per_case if d["zero_result"])
    n_timeout = sum(1 for d in per_case if d["outcome"] == "timeout")
    n_error = sum(1 for d in per_case if d["outcome"] == "error")
    n_no_hit_ok = sum(
        1 for d in per_case if d["expect_no_hit"] and d.get("no_hit_satisfied")
    )

    counts = {
        "n_total": n_total,
        "n_judged": n_judged,
        "n_no_hit": n_no_hit,
        "zero_result": n_zero,
        "timeouts": n_timeout,
        "errors": n_error,
        "failures": n_timeout + n_error,
        "zero_result_rate": rate(n_zero, n_total),
        "no_hit_satisfied_rate": rate(n_no_hit_ok, n_no_hit),
        "timeout_rate": rate(n_timeout, n_total),
        "error_rate": rate(n_error, n_total),
        "failure_rate": rate(n_timeout + n_error, n_total),
    }

    return SystemReport(
        system=getattr(system, "name", system.__class__.__name__),
        ks=ks,
        generated_at=(now or _utc_now_iso)(),
        overall=overall,
        by_category=by_category,
        by_entity_kind=by_entity_kind,
        latency=lat,
        counts=counts,
        per_case=per_case,
    )


def _zero_overall() -> dict[str, Any]:
    return {
        "recall@1": 0.0,
        "recall@5": 0.0,
        "recall@10": 0.0,
        "mrr": 0.0,
        "ndcg@10": 0.0,
        "map": 0.0,
        "n": 0,
    }
