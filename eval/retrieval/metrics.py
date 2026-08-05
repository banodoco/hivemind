"""Retrieval evaluation metrics — deterministic, stdlib only.

Given the same inputs, every function here returns the same output. No
randomness, no external services, no floating point that varies by platform
(only plain arithmetic).

Ported from Pumpernickel ``eval/retrieval/metrics.py`` (Recall@k, MRR, set
precision/recall, group aggregation) and **extended** for Hivemind with graded
nDCG, latency percentiles, and zero-result / failure accounting that the
Pumpernickel harness did not carry.
"""

from __future__ import annotations

import math
from typing import Any, Sequence

# ---------------------------------------------------------------------------
# Ranked-list relevance metrics (binary relevant set)
# ---------------------------------------------------------------------------

# Ranked ids are sequences of match keys. A "key" is any hashable identity token
# (in practice the (entity_kind, item_id) tuples from schema.Result.key()).

Key = Any


def recall_at_k(ranked: Sequence[Key], expected: Sequence[Key], k: int) -> float:
    """Fraction of *expected* keys appearing in the first *k* results.

    Returns 0.0 when *expected* is empty (nothing to recall). ``k`` is 1-indexed.
    """
    if k <= 0:
        return 0.0
    if not expected:
        return 0.0
    top_k = set(ranked[:k])
    hits = sum(1 for e in expected if e in top_k)
    return hits / len(expected)


def reciprocal_rank(ranked: Sequence[Key], expected: Sequence[Key]) -> float:
    """Reciprocal rank of the first expected hit (1/index, 1-indexed).

    Returns 0.0 when *expected* is empty or no expected key is retrieved.
    """
    if not expected:
        return 0.0
    expected_set = set(expected)
    for i, rid in enumerate(ranked):
        if rid in expected_set:
            return 1.0 / (i + 1)
    return 0.0


def average_precision(ranked: Sequence[Key], expected: Sequence[Key]) -> float:
    """Average precision for one query (used by MAP). 0.0 if expected is empty."""
    if not expected:
        return 0.0
    expected_set = set(expected)
    hits = 0
    total = 0.0
    for i, rid in enumerate(ranked):
        if rid in expected_set:
            hits += 1
            total += hits / (i + 1)
    return total / len(expected)


# ---------------------------------------------------------------------------
# Graded relevance: nDCG
# ---------------------------------------------------------------------------


def dcg_at_k(grades_at_rank: Sequence[int]) -> float:
    """Discounted cumulative gain for a list of grades in rank order.

    Uses the standard ``sum (2^g - 1) / log2(i+2)`` (i is 0-indexed rank), which
    emphasises highly-graded results and smoothly discounts lower ranks.
    """
    total = 0.0
    for i, g in enumerate(grades_at_rank):
        if g <= 0:
            continue
        total += (2.0 ** g - 1.0) / math.log2(i + 2)
    return total


def ndcg_at_k(
    ranked: Sequence[Key],
    grade_map: dict[Key, int],
    k: int,
) -> float:
    """Normalised DCG@k for one ranked list against a graded judgment map.

    ``grade_map`` maps item key -> relevance grade (items not in the map, or
    mapped to 0, are non-relevant). The ideal DCG is computed by sorting all
    judged-positive grades descending and truncating to k. Returns 0.0 when
    there are no positive grades (IDCG == 0).
    """
    if k <= 0:
        return 0.0
    gains = [max(0, grade_map.get(rid, 0)) for rid in ranked[:k]]
    dcg = dcg_at_k(gains)
    ideal = sorted((g for g in grade_map.values() if g > 0), reverse=True)[:k]
    idcg = dcg_at_k(ideal)
    if idcg <= 0.0:
        return 0.0
    return dcg / idcg


# ---------------------------------------------------------------------------
# Set precision / recall (per-query, for diagnostics)
# ---------------------------------------------------------------------------


def set_precision(returned: Sequence[Key], expected: Sequence[Key]) -> float:
    """|returned ∩ expected| / |returned|. 1.0 when nothing is returned."""
    if not returned:
        return 1.0
    ret_set = set(returned)
    exp_set = set(expected)
    return len(ret_set & exp_set) / len(ret_set)


def set_recall(returned: Sequence[Key], expected: Sequence[Key]) -> float:
    """|returned ∩ expected| / |expected|. 0.0 when expected is empty."""
    if not expected:
        return 0.0
    return len(set(returned) & set(expected)) / len(expected)


# ---------------------------------------------------------------------------
# Latency percentiles
# ---------------------------------------------------------------------------


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolation percentile (numpy 'linear' / R type 7).

    Deterministic. ``q`` is in [0, 100]. Returns 0.0 for an empty sample.
    Guarantees p50 <= p95 <= p99 for the same non-empty sample.
    """
    if not values:
        return 0.0
    if not 0.0 <= q <= 100.0:
        raise ValueError(f"percentile q must be in [0, 100], got {q}")
    xs = sorted(float(v) for v in values)
    if len(xs) == 1:
        return xs[0]
    rank = (q / 100.0) * (len(xs) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return xs[int(rank)]
    frac = rank - lo
    return xs[lo] + (xs[hi] - xs[lo]) * frac


def latency_stats(values: Sequence[float]) -> dict[str, float]:
    """p50/p95/p99 plus mean and count for a latency sample (milliseconds)."""
    xs = [float(v) for v in values]
    n = len(xs)
    if n == 0:
        return {"p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0, "mean_ms": 0.0, "n": 0}
    return {
        "p50_ms": percentile(xs, 50),
        "p95_ms": percentile(xs, 95),
        "p99_ms": percentile(xs, 99),
        "mean_ms": sum(xs) / n,
        "n": n,
    }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def aggregate(per_case: list[dict[str, Any]]) -> dict[str, float | int]:
    """Macro-average Recall@1/5/10, MRR, nDCG@10 over per-case dicts.

    Each per-case dict should contain ``recall_at_1/5/10``, ``reciprocal_rank``,
    and ``ndcg_at_10``. Returns ``n`` = number of cases averaged.
    """
    n = len(per_case)
    if n == 0:
        return _zero_overall(0)
    return {
        "recall@1": _mean(per_case, "recall_at_1"),
        "recall@5": _mean(per_case, "recall_at_5"),
        "recall@10": _mean(per_case, "recall_at_10"),
        "mrr": _mean(per_case, "reciprocal_rank"),
        "ndcg@10": _mean(per_case, "ndcg_at_10"),
        "map": _mean(per_case, "average_precision"),
        "n": n,
    }


def _zero_overall(n: int) -> dict[str, float | int]:
    return {
        "recall@1": 0.0,
        "recall@5": 0.0,
        "recall@10": 0.0,
        "mrr": 0.0,
        "ndcg@10": 0.0,
        "map": 0.0,
        "n": n,
    }


def _mean(rows: list[dict[str, Any]], key: str) -> float:
    vals = [r[key] for r in rows if key in r]
    if not vals:
        return 0.0
    return sum(vals) / len(vals)


def aggregate_by_group(per_case: list[dict[str, Any]], group_key: str) -> dict[str, dict]:
    """Partition per-case dicts by ``group_key`` and aggregate each partition.

    Each per-case dict must carry ``group_key`` plus the metric keys used by
    :func:`aggregate`. Returns ``{group_value: aggregate-dict}``.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in per_case:
        gv = r.get(group_key)
        if gv is None:
            continue
        groups.setdefault(str(gv), []).append(r)
    return {gv: aggregate(group) for gv, group in groups.items()}


def aggregate_by_category(per_case: list[dict[str, Any]]) -> dict[str, dict]:
    """Aggregate by golden category.

    Unlike :func:`aggregate_by_group`, each case carries a *list* of categories
    (``categories``); a case contributes to every category it belongs to, so
    category buckets are a covering, not a partition.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in per_case:
        for cat in r.get("categories", []) or []:
            groups.setdefault(str(cat), []).append(r)
    return {cat: aggregate(group) for cat, group in groups.items()}


def rate(num: int, denom: int) -> float:
    """Safe ratio; 0.0 when denom is 0."""
    return num / denom if denom else 0.0
