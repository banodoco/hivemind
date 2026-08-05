#!/usr/bin/env python3
"""Hivemind hybrid-search capacity & cost model (plan task 0.7).

A **pure, deterministic, offline** model — no database, network, secret, or
provider call. It turns the measured corpus facts from tasks 0.1–0.3 into a
storage / memory / spend / compute projection for the 384- and 1536-dimensional
embedding candidates across the pilot and full eligible corpus, and issues
explicit pass / fail / conditional verdicts for the plan's three fixed gates:

  * `$25` initial embedding API spend.
  * `12 GB` projected vector-table plus HNSW storage.
  * `$50/month` recurring search-related incremental cost.

Design rules (matching the repo's task-0.x tooling):

  * **Measured facts are separated from priced assumptions and from heuristics.**
    Every number traces to a citation; heuristic parameters carry an explicit
    range so callers can sweep them.
  * **Deterministic.** No randomness and no wall-clock dependence in the math;
    `generated_at` is the only time-derived field and does not affect any result.
  * **Reproducible.** Re-running yields byte-identical numbers; outputs are
    machine-readable JSON plus a human summary.

Primary sources (fetched 2026-07-28, recorded in :data:`SOURCES`):

  * OpenAI pricing (``developers.openai.com``): ``text-embedding-3-small``
    $0.02 / 1M input tokens (native 1536-d); ``text-embedding-3-large``
    $0.13 / 1M input tokens (native 3072-d). Both support a ``dimensions``
    parameter that shortens the vector.
  * Supabase pricing (``supabase.com/pricing``): Pro $25/mo, 8 GB disk included
    then $0.125/GB, compute add-ons Micro..16XL, Edge Functions 2M included on
    Pro then $2 / 1M invocations.
  * pgvector README (``github.com/pgvector/pgvector``): a ``vector(D)`` value
    occupies ``4*D + 8`` bytes on disk; HNSW defaults ``m=16``,
    ``ef_construction=64``, ``ef_search=40``; builds are faster when the graph
    fits in ``maintenance_work_mem`` but the index need not be RAM-resident.

Run::

    python3 scripts/capacity_model.py                  # human summary to stdout
    python3 scripts/capacity_model.py --results PATH   # write results JSON
    python3 scripts/capacity_model.py --assumptions PATH  # write assumptions JSON
    python3 scripts/capacity_model.py --results docs/hybrid-search/phase0-capacity-results.json
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Citations — every price/technical fact and its source + retrieval date.
# ---------------------------------------------------------------------------

SOURCES: dict[str, dict[str, str]] = {
    "openai_embedding_3_small_price": {
        "value": "$0.02 per 1,000,000 input tokens",
        "url": "https://developers.openai.com/api/docs/pricing",
        "retrieved": "2026-07-28",
        "note": "List price for text-embedding-3-small. Same per-token price "
                "regardless of the `dimensions` shortening parameter.",
    },
    "openai_embedding_3_large_price": {
        "value": "$0.13 per 1,000,000 input tokens",
        "url": "https://developers.openai.com/api/docs/pricing",
        "retrieved": "2026-07-28",
        "note": "Alternative 'quality' model for a 1536-d path "
                "(3-large truncated to 1536). ~6.5x the 3-small price.",
    },
    "openai_dimensions_param": {
        "value": "text-embedding-3-small native 1536-d; text-embedding-3-large "
                 "native 3072-d; `dimensions` param shortens output (256 shown).",
        "url": "https://developers.openai.com/api/docs/guides/embeddings",
        "retrieved": "2026-07-28",
        "note": "So 384-d = 3-small(dimensions=384); 1536-d = 3-small native.",
    },
    "supabase_pro_plan": {
        "value": "$25/month base; 8 GB disk included then $0.125/GB (General "
                 "Purpose); 250 GB egress included.",
        "url": "https://supabase.com/pricing",
        "retrieved": "2026-07-28",
    },
    "supabase_compute_addons": {
        "value": "Pro includes $10 compute credit = 1 Micro instance (1 GB RAM). "
                 "Add-ons: Small $15/2GB, Medium $60/4GB, Large $110/8GB, "
                 "XL $210/16GB, 2XL $410/32GB, 4XL $960/64GB, 8XL $1870/128GB.",
        "url": "https://supabase.com/pricing",
        "retrieved": "2026-07-28",
        "note": "All ARM, billed hourly; incremental cost below subtracts the "
                "$10 Pro credit that already covers a Micro instance.",
    },
    "supabase_edge_functions": {
        "value": "2,000,000 invocations included on Pro, then $2 per 1,000,000.",
        "url": "https://supabase.com/pricing",
        "retrieved": "2026-07-28",
    },
    "pgvector_vector_size": {
        "value": "A vector(D) value occupies 4*D + 8 bytes.",
        "url": "https://github.com/pgvector/pgvector",
        "retrieved": "2026-07-28",
    },
    "pgvector_hnsw_defaults": {
        "value": "HNSW defaults m=16, ef_construction=64, ef_search=40; builds "
                 "are faster when the graph fits maintenance_work_mem but the "
                 "index need not be RAM-resident for correct queries.",
        "url": "https://github.com/pgvector/pgvector",
        "retrieved": "2026-07-28",
    },
}

# ---------------------------------------------------------------------------
# MEASURED FACTS — live read-only corpus metadata, 2026-07-28.
# Source files: docs/hybrid-search/phase0-inventory.json (task 0.3) and
# docs/hybrid-search/phase0-schema-eligibility-map.md (task 0.2).
# These are facts, not assumptions; they are not swept.
# ---------------------------------------------------------------------------

MEASURED: dict[str, Any] = {
    "_source_inventory": "docs/hybrid-search/phase0-inventory.json (task 0.3)",
    "_source_schema": "docs/hybrid-search/phase0-schema-eligibility-map.md (task 0.2)",
    "_as_of": "2026-07-28",
    # Discord messages. Eligible = is_deleted=false (schema map sec.5: 1,245,006).
    # reltuples estimate (inventory) = 1,248,240; total incl. deleted = 1,251,991.
    "messages_eligible": 1_245_006,
    "messages_reluples": 1_248_240,
    "messages_total_incl_deleted": 1_251_991,
    # External resources (inventory, count=exact).
    "resources_total": 2_759,
    "workflows_total": 2_757,
    "workflows_with_python": 222,            # payload.python_source non-empty (AD-4)
    # Distillations (inventory): 11, all pending, 0 approved.
    "distillations": 11,
    # Token estimates (chars/4 heuristic from task 0.3).
    "message_est_tokens_mean": 19,           # 5,000-msg stratified sample (mean 77 chars)
    "resource_body_est_tokens_mean": 3_189,  # all 2,759 resources (mean 12,759 chars)
    "workflow_python_est_tokens_mean": 34_746,  # 222 workflows, mean 138,983 chars / 4
    # Current corpus-table footprint (bytes), measured in task 0.3. This is the
    # corpus tables only; the full Supabase project DB (537 relations including
    # the larger Banodoco app) is larger and was NOT measured — see report.
    "corpus_table_bytes": {
        "discord_messages": 1_210_236_928,
        "external_resources": 70_295_552,
        "distillations": 188_416,
        "vibecomfy_ratings": 65_536,
        "contributors": 49_152,
        "distillation_cites": 32_768,
    },
}

# ---------------------------------------------------------------------------
# PRICED ASSUMPTIONS — vendor list prices (SOURCES above). Sweepable where
# the plan itself flags them as dated planning estimates.
# ---------------------------------------------------------------------------

PRICING: dict[str, Any] = {
    "embedding_3small_per_m_tokens": 0.02,   # $/1M input tokens (OpenAI)
    "embedding_3large_per_m_tokens": 0.13,   # $/1M input tokens (OpenAI, alt)
    "supabase_pro_base_per_month": 25.0,     # $/mo (already paid today)
    "supabase_disk_included_gb": 8.0,        # GB included on Pro
    "supabase_disk_per_gb_month": 0.125,     # $/GB/mo beyond included (General Purpose)
    "edge_included_invocations": 2_000_000,  # on Pro
    "edge_per_m_invocations": 2.0,           # $/1M beyond included
    # Compute add-ons: (name, $/month, RAM GB). Micro is the included baseline.
    "compute_addons": [
        ("Micro", 10, 1),
        ("Small", 15, 2),
        ("Medium", 60, 4),
        ("Large", 110, 8),
        ("XL", 210, 16),
        ("2XL", 410, 32),
        ("4XL", 960, 64),
        ("8XL", 1870, 128),
    ],
    "pro_compute_credit": 10.0,              # $/mo credit already covering a Micro
}

# ---------------------------------------------------------------------------
# MODEL HEURISTICS — not vendor-published; carry explicit ranges for sweeps.
# ---------------------------------------------------------------------------

HEURISTICS: dict[str, Any] = {
    # pgvector HNSW index = a stored copy of every vector PLUS the graph.
    # Vector copy is documented (4D+8). Graph overhead per node is NOT published;
    # derived from m=16: ~2*16 base-layer links + upper layers + page headers.
    "hnsw_graph_bytes_per_node_central": 150,
    "hnsw_graph_bytes_per_node_low": 100,
    "hnsw_graph_bytes_per_node_high": 250,
    # Heap (content_embeddings) non-vector bytes per row: bigint contract_id,
    # entity_type/item_id/representation_type text, ints, two sha-256 hex hashes,
    # timestamptz, tuple header + alignment.
    "row_overhead_bytes_central": 200,
    "row_overhead_bytes_low": 180,
    "row_overhead_bytes_high": 240,
    # Stored chunk_text for non-message cohorts (snippet/source offsets). Messages
    # carry chunk_index=0 and no stored chunk_text (the row itself is the source).
    "chunk_text_bytes_central": 1_000,
    # Secondary indexes on content_embeddings (composite PK, representation_hash,
    # chunk_hash, active-contract lookup) — small, mostly independent of dim.
    "secondary_indexes_gb_central": 0.15,
    # Chunking (tokens). Prose: paragraph-aware; workflow Python: code-aware.
    "prose_chunk_tokens": 512,
    "python_chunk_tokens": 512,
    "chunk_overlap_frac": 0.10,
    # RAM safety factor: HNSW index shares RAM with shared_buffers + OS + other
    # work. Treat the index as RAM-resident when a tier has >= this multiple.
    "hnsw_ram_safety_factor": 1.25,
    # Operating-traffic assumptions for the steady-state (sweepable).
    "searches_per_month": 250_000,           # mid estimate of agent search load
    "query_est_tokens": 20,                  # ~plan's query-embedding assumption
    "embedding_worker_cron_invocations_per_month": 8_640,  # ~every 5 min
}

# Fixed gates from the plan ("Cost and capacity" + stop conditions).
GATES: dict[str, float] = {
    "initial_spend_usd": 25.0,
    "new_storage_gb": 12.0,
    "monthly_incremental_usd": 50.0,
}

# ---------------------------------------------------------------------------
# Pure helpers — every one is unit-tested in tests/test_capacity_model.py.
# ---------------------------------------------------------------------------

BYTES_PER_GIB = 1024 ** 3
BYTES_PER_GB = 1_000_000_000


def vector_bytes_per_node(dim: int) -> int:
    """On-disk bytes for one pgvector ``vector(dim)`` value: ``4*dim + 8``."""
    if dim <= 0:
        raise ValueError("dimension must be positive")
    return 4 * dim + 8


def raw_vector_bytes(dim: int, n_vectors: int) -> int:
    """Raw vector payload bytes (the 4*dim float4 values, no header)."""
    return 4 * dim * n_vectors


def chunks_for(total_units: int, chunk_units: int, overlap_units: int) -> int:
    """Deterministic chunk count to cover ``total_units`` with overlap.

    >>> chunks_for(0, 512, 51)
    0
    >>> chunks_for(400, 512, 51)
    1
    >>> chunks_for(1200, 512, 51)
    3
    """
    if total_units <= 0:
        return 0
    if total_units <= chunk_units:
        return 1
    step = chunk_units - overlap_units
    if step <= 0:
        step = 1
    return 1 + math.ceil((total_units - chunk_units) / step)


def tokens_with_overlap(total_tokens: int, overlap_frac: float) -> float:
    """Embedded token count after chunk overlap re-processing.

    Each chunk of size ``c`` with fractional overlap re-embeds the overlap once,
    so the multiplicative overhead is ``1 + overlap_frac``. Items that are a
    single chunk (e.g. messages) incur no overlap.
    """
    if total_tokens <= 0:
        return 0.0
    return total_tokens * (1.0 + overlap_frac)


def embedding_cost_usd(total_tokens: float, price_per_m: float) -> float:
    """Provider embedding cost: ``tokens / 1e6 * price_per_m``."""
    if total_tokens < 0 or price_per_m < 0:
        raise ValueError("tokens and price must be non-negative")
    return total_tokens / 1_000_000.0 * price_per_m


def bytes_to_gib(b: int) -> float:
    return b / BYTES_PER_GIB


def bytes_to_gb(b: int) -> float:
    return b / BYTES_PER_GB


def gb_to_gib(gb: float) -> float:
    """Supabase bills in decimal GB; storage math is cleaner in GiB. Convert."""
    return gb * BYTES_PER_GB / BYTES_PER_GIB


# ---------------------------------------------------------------------------
# Scenario definition.
# ---------------------------------------------------------------------------

def default_pilot_message_sample() -> int:
    """A small, representative Discord sample for the pilot (plan 2.13)."""
    return 5_000


def build_scenarios() -> list[dict[str, Any]]:
    """Return the four headline scenarios (pilot/full x 384/1536)."""
    pilot_msg = default_pilot_message_sample()
    cohort_counts = {
        "pilot": {
            "messages": pilot_msg,
            "resources_prose": MEASURED["resources_total"],
            "workflow_python_workflows": MEASURED["workflows_with_python"],
            "distillations": MEASURED["distillations"],
            "_label": "Pilot (distillations + all resources + workflow Python + "
                      f"{pilot_msg:,}-msg sample)",
        },
        "full": {
            "messages": MEASURED["messages_eligible"],
            "resources_prose": MEASURED["resources_total"],
            "workflow_python_workflows": MEASURED["workflows_with_python"],
            "distillations": MEASURED["distillations"],
            "_label": "Full eligible corpus "
                      f"(messages={MEASURED['messages_eligible']:,} + resources "
                      "+ workflow Python + distillations)",
        },
    }
    scenarios: list[dict[str, Any]] = []
    for dim in (384, 1536):
        for scale_key in ("pilot", "full"):
            cc = cohort_counts[scale_key]
            scenarios.append({
                "id": f"{scale_key}_{dim}",
                "dimension": dim,
                "scale": scale_key,
                "label": cc["_label"] + f" @ {dim}-d",
                "cohort_counts": {k: v for k, v in cc.items() if not k.startswith("_")},
            })
    return scenarios


def _resource_prose_vectors(resources_total: int, chunk_tokens: int,
                            overlap_frac: float) -> tuple[int, float]:
    """Vector count and embedded tokens for resource PROSE representations.

    Most resources are workflows whose AD-4 canonical prose is compact (title +
    description + semantics; full Python is a separate representation), so the
    median resource is ~1 chunk. We approximate total prose tokens from the
    measured resource-body mean (an upper bound, since it predates Python
    removal) and chunk it; long descriptions produce a few extra chunks.
    """
    mean_tokens = MEASURED["resource_body_est_tokens_mean"]
    total_tokens = mean_tokens * resources_total
    overlap_tokens = int(chunk_tokens * overlap_frac)
    # One base vector per resource (chunk 0) plus extra chunks for the long tail.
    # Approximate extra chunks via the mean-driven chunk function over the average.
    avg_chunks = chunks_for(int(mean_tokens), chunk_tokens, overlap_tokens)
    vectors = resources_total * max(1, avg_chunks)
    tokens = tokens_with_overlap(total_tokens, overlap_frac)
    return vectors, tokens


def _workflow_python_vectors(workflows: int, chunk_tokens: int,
                             overlap_frac: float) -> tuple[int, float]:
    """Vector count and embedded tokens for workflow PYTHON representations."""
    mean_tokens = MEASURED["workflow_python_est_tokens_mean"]
    total_tokens = mean_tokens * workflows
    overlap_tokens = int(chunk_tokens * overlap_frac)
    avg_chunks = chunks_for(int(mean_tokens), chunk_tokens, overlap_tokens)
    vectors = workflows * avg_chunks
    tokens = tokens_with_overlap(total_tokens, overlap_frac)
    return vectors, tokens


def evaluate_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    """Project storage / spend / compute for one scenario at its dimension."""
    dim = scenario["dimension"]
    cc = scenario["cohort_counts"]
    H = HEURISTICS
    P = PRICING

    prose_chunk = H["prose_chunk_tokens"]
    py_chunk = H["python_chunk_tokens"]
    overlap = H["chunk_overlap_frac"]

    # --- vector counts + backfill tokens by cohort ---------------------------
    msg_vectors = cc["messages"]
    msg_tokens = float(msg_vectors) * MEASURED["message_est_tokens_mean"]  # 1 chunk, no overlap

    prose_vectors, prose_tokens = _resource_prose_vectors(
        cc["resources_prose"], prose_chunk, overlap)
    py_vectors, py_tokens = _workflow_python_vectors(
        cc["workflow_python_workflows"], py_chunk, overlap)

    dist_vectors = cc["distillations"]
    dist_tokens = float(dist_vectors) * (  # question+conditions+answer, ~1 chunk
        MEASURED["resource_body_est_tokens_mean"] * 0 + 150)  # ~150 tok avg distillation

    total_vectors = msg_vectors + prose_vectors + py_vectors + dist_vectors
    total_backfill_tokens = msg_tokens + prose_tokens + py_tokens + dist_tokens

    # --- storage -------------------------------------------------------------
    vbytes = vector_bytes_per_node(dim)
    raw_payload_bytes = raw_vector_bytes(dim, total_vectors)

    row_over = H["row_overhead_bytes_central"]
    # Heap: every row stores the vector (4D+8) + row overhead. Non-message rows
    # also store chunk_text (snippet/source). Messages carry chunk_index=0 only.
    chunk_text_bytes_non_msg = (
        (prose_vectors + py_vectors + dist_vectors) * H["chunk_text_bytes_central"]
    )
    heap_bytes = total_vectors * (vbytes + row_over) + chunk_text_bytes_non_msg

    # HNSW index: stored copy of every vector (4D+8) + graph overhead per node.
    idx_low = total_vectors * (vbytes + H["hnsw_graph_bytes_per_node_low"])
    idx_central = total_vectors * (vbytes + H["hnsw_graph_bytes_per_node_central"])
    idx_high = total_vectors * (vbytes + H["hnsw_graph_bytes_per_node_high"])

    secondary_bytes = int(H["secondary_indexes_gb_central"] * BYTES_PER_GB)

    new_storage_low = heap_bytes + idx_low + secondary_bytes
    new_storage_central = heap_bytes + idx_central + secondary_bytes
    new_storage_high = heap_bytes + idx_high + secondary_bytes

    # --- current DB + headroom ----------------------------------------------
    corpus_db_bytes = sum(MEASURED["corpus_table_bytes"].values())
    total_db_after_central = corpus_db_bytes + new_storage_central

    # --- spend ---------------------------------------------------------------
    backfill_cost_3small = embedding_cost_usd(
        total_backfill_tokens, P["embedding_3small_per_m_tokens"])
    backfill_cost_3large = embedding_cost_usd(
        total_backfill_tokens, P["embedding_3large_per_m_tokens"])

    per_query_cost = embedding_cost_usd(H["query_est_tokens"],
                                        P["embedding_3small_per_m_tokens"])
    monthly_query_cost = per_query_cost * H["searches_per_month"]

    # --- Edge invocations ----------------------------------------------------
    monthly_invocations = (H["searches_per_month"]
                           + H["embedding_worker_cron_invocations_per_month"])
    overage = max(0, monthly_invocations - P["edge_included_invocations"])
    edge_cost = overage / 1_000_000.0 * P["edge_per_m_invocations"]

    # --- compute add-on for RAM-resident HNSW -------------------------------
    # pgvector HNSW need not be RAM-resident for *correct* queries; it is
    # served disk-cached from fast NVMe. So two operating points exist:
    #   baseline      = included Micro compute (disk-cached index, $0 add-on);
    #                   meets the capacity gate but warm p95 is unproven.
    #   ram_resident  = smallest tier that holds the index in RAM (latency-safe);
    #                   the realistic production floor.
    idx_gib = bytes_to_gib(idx_central)
    needed_ram = idx_gib * H["hnsw_ram_safety_factor"]
    recommended_tier = _smallest_tier(needed_ram)          # RAM-resident (safe)
    minimal_ram_tier = _smallest_tier(idx_gib)             # smallest that fits in RAM
    baseline_tier = PRICING["compute_addons"][0]           # Micro (included)
    rec_incremental = max(0.0, recommended_tier[1] - P["pro_compute_credit"])
    baseline_incremental = 0.0                             # Micro covered by credit

    # --- monthly steady-state (incremental over today's Pro baseline) --------
    extra_storage_gb = max(0.0, bytes_to_gb(total_db_after_central)
                           - P["supabase_disk_included_gb"])
    extra_storage_cost = extra_storage_gb * P["supabase_disk_per_gb_month"]
    non_compute = extra_storage_cost + edge_cost + monthly_query_cost
    monthly_baseline = non_compute + baseline_incremental
    monthly_ram_resident = non_compute + rec_incremental

    # --- gate verdicts -------------------------------------------------------
    gates = _gate_verdicts(
        backfill_cost_3small, bytes_to_gb(new_storage_central),
        bytes_to_gb(new_storage_high), monthly_baseline, monthly_ram_resident)

    return {
        "id": scenario["id"],
        "dimension": dim,
        "scale": scenario["scale"],
        "label": scenario["label"],
        "vectors": {
            "messages": msg_vectors,
            "resource_prose": prose_vectors,
            "workflow_python": py_vectors,
            "distillations": dist_vectors,
            "total": total_vectors,
        },
        "backfill_tokens": {
            "messages": msg_tokens,
            "resource_prose": prose_tokens,
            "workflow_python": py_tokens,
            "distillations": dist_tokens,
            "total": total_backfill_tokens,
        },
        "storage": {
            "vector_bytes_per_node": vbytes,
            "raw_vector_payload_gib": round(bytes_to_gib(raw_payload_bytes), 4),
            "heap_gib": round(bytes_to_gib(heap_bytes), 4),
            "hnsw_index_gib": {
                "low": round(bytes_to_gib(idx_low), 4),
                "central": round(bytes_to_gib(idx_central), 4),
                "high": round(bytes_to_gib(idx_high), 4),
            },
            "secondary_indexes_gib": round(bytes_to_gb(secondary_bytes), 4),
            "new_storage_gb": {
                "low": round(bytes_to_gb(new_storage_low), 3),
                "central": round(bytes_to_gb(new_storage_central), 3),
                "high": round(bytes_to_gb(new_storage_high), 3),
            },
            "hnsw_build_maintenance_work_mem_note": idx_gib,  # GiB to fit graph
        },
        "current_db": {
            "corpus_table_bytes": corpus_db_bytes,
            "corpus_table_gib": round(bytes_to_gib(corpus_db_bytes), 4),
            "full_project_db_note": "Unmeasured (537 relations incl. larger "
                                    "Banodoco app); true total is larger. Measure "
                                    "before backfill.",
            "total_db_after_central_gib": round(bytes_to_gib(total_db_after_central), 4),
            "extra_storage_gb": round(extra_storage_gb, 4),
        },
        "spend": {
            "backfill_cost_3small_usd": round(backfill_cost_3small, 4),
            "backfill_cost_3large_usd": round(backfill_cost_3large, 4),
            "per_query_embedding_cost_usd": per_query_cost,
            "monthly_query_cost_usd": round(monthly_query_cost, 4),
        },
        "edge": {
            "monthly_invocations": monthly_invocations,
            "included_invocations": P["edge_included_invocations"],
            "monthly_cost_usd": round(edge_cost, 4),
        },
        "compute": {
            "hnsw_index_central_gib": round(idx_gib, 4),
            "needed_ram_gib": round(needed_ram, 4),
            "baseline_tier": {
                "name": baseline_tier[0], "price": baseline_tier[1],
                "ram_gb": baseline_tier[2],
                "note": "Included Micro (covered by the $10 Pro credit). Index "
                        "served disk-cached; meets capacity, warm p95 unproven.",
                "incremental_over_pro_credit_usd": 0.0,
            },
            "recommended_tier": {
                "name": recommended_tier[0], "price": recommended_tier[1],
                "ram_gb": recommended_tier[2],
                "note": "Smallest tier holding the HNSW index in RAM (latency-safe).",
                "incremental_over_pro_credit_usd": round(rec_incremental, 2),
            },
            "minimal_ram_tier": {
                "name": minimal_ram_tier[0], "price": minimal_ram_tier[1],
                "ram_gb": minimal_ram_tier[2],
            },
        },
        "monthly_steady_state_usd": {
            "baseline_disk_cached": round(monthly_baseline, 2),
            "ram_resident": round(monthly_ram_resident, 2),
            "components": {
                "extra_storage": round(extra_storage_cost, 2),
                "compute_incremental_ram_resident": round(rec_incremental, 2),
                "edge": round(edge_cost, 2),
                "query_embeddings": round(monthly_query_cost, 2),
            },
        },
        "gate_verdicts": gates,
    }


def _smallest_tier(needed_ram_gb: float) -> tuple[str, int, int]:
    """Smallest compute add-on whose RAM >= ``needed_ram_gb``."""
    for name, price, ram in PRICING["compute_addons"]:
        if ram >= needed_ram_gb:
            return (name, price, ram)
    # Fall back to the largest listed tier.
    *_, last = PRICING["compute_addons"]
    return last


def _verdict(value: float, limit: float, lower_is_better: bool = True) -> str:
    if lower_is_better:
        return "PASS" if value <= limit else "FAIL"
    return "PASS" if value >= limit else "FAIL"


def _gate_verdicts(backfill_cost: float, new_storage_gb: float,
                   new_storage_gb_high: float, monthly_baseline: float,
                   monthly_ram_resident: float) -> dict[str, Any]:
    """Issue explicit pass/fail/conditional verdicts for the three fixed gates."""
    # $25 initial spend.
    spend_v = _verdict(backfill_cost, GATES["initial_spend_usd"])

    # 12 GB new storage (plan stop condition: vector-table + HNSW).
    # Robustness check: even the high-overhead estimate must clear the gate.
    storage_v = _verdict(new_storage_gb, GATES["new_storage_gb"])
    storage_v_high = _verdict(new_storage_gb_high, GATES["new_storage_gb"])
    storage_verdict = storage_v
    if storage_v == "PASS" and storage_v_high == "FAIL":
        storage_verdict = "CONDITIONAL"  # passes central, fails high-overhead sweep

    # $50/month recurring incremental.
    #   baseline       = included Micro compute, index served disk-cached
    #                    (meets capacity; warm p95 is a deferred latency question).
    #   ram_resident   = smallest tier holding the index in RAM (latency-safe floor).
    # PASS if the latency-safe operating point is within budget; CONDITIONAL if a
    # sub-budget operating point exists but the latency-safe one is not; FAIL only
    # if even the cheapest operating point exceeds the budget.
    if monthly_ram_resident <= GATES["monthly_incremental_usd"]:
        monthly_verdict = "PASS"
    elif monthly_baseline <= GATES["monthly_incremental_usd"] < monthly_ram_resident:
        monthly_verdict = "CONDITIONAL"  # cheap to run; latency tier may push over
    else:
        monthly_verdict = "FAIL"

    return {
        "initial_spend_25usd": {
            "verdict": spend_v,
            "value_usd": round(backfill_cost, 4),
            "limit_usd": GATES["initial_spend_usd"],
        },
        "new_storage_12gb": {
            "verdict": storage_verdict,
            "value_gb_central": round(new_storage_gb, 3),
            "value_gb_high_overhead": round(new_storage_gb_high, 3),
            "limit_gb": GATES["new_storage_gb"],
            "central_verdict": storage_v,
            "high_overhead_verdict": storage_v_high,
        },
        "monthly_incremental_50usd": {
            "verdict": monthly_verdict,
            "value_baseline_disk_cached_usd": round(monthly_baseline, 2),
            "value_ram_resident_usd": round(monthly_ram_resident, 2),
            "limit_usd": GATES["monthly_incremental_usd"],
        },
    }


# ---------------------------------------------------------------------------
# Output / CLI
# ---------------------------------------------------------------------------

def build_results() -> dict[str, Any]:
    scenarios = build_scenarios()
    evaluated = [evaluate_scenario(s) for s in scenarios]
    return {
        "report": "Hivemind hybrid-search capacity & cost model (task 0.7)",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "plan_date": "2026-07-28",
        "measured_as_of": MEASURED["_as_of"],
        "gates": GATES,
        "scenarios": evaluated,
    }


def build_assumptions() -> dict[str, Any]:
    return {
        "report": "Assumptions for the task-0.7 capacity model",
        "plan_date": "2026-07-28",
        "sources": SOURCES,
        "measured_facts": MEASURED,
        "priced_assumptions": PRICING,
        "model_heuristics": HEURISTICS,
    }


def _fmt_summary(results: dict[str, Any]) -> str:
    lines = []
    lines.append("Hivemind hybrid-search capacity & cost model (task 0.7)")
    lines.append(f"Generated: {results['generated_at']}  (facts as of "
                 f"{results['measured_as_of']})\n")
    for sc in results["scenarios"]:
        lines.append("=" * 78)
        lines.append(f"{sc['label']}")
        lines.append("-" * 78)
        v = sc["vectors"]
        lines.append(f"  vectors total: {v['total']:,}  "
                     f"(msg {v['messages']:,}, prose {v['resource_prose']:,}, "
                     f"py {v['workflow_python']:,}, dist {v['distillations']})")
        st = sc["storage"]
        lines.append(f"  raw vector payload: {st['raw_vector_payload_gib']} GiB")
        lines.append(f"  heap (table):       {st['heap_gib']} GiB")
        lines.append(f"  HNSW index:         {st['hnsw_index_gib']['central']} GiB "
                     f"(low {st['hnsw_index_gib']['low']} / high "
                     f"{st['hnsw_index_gib']['high']})")
        lines.append(f"  NEW storage (vec-table+HNSW+2nd idx): "
                     f"{st['new_storage_gb']['central']} GB central "
                     f"(low {st['new_storage_gb']['low']} / high "
                     f"{st['new_storage_gb']['high']})")
        cd = sc["current_db"]
        lines.append(f"  total DB after (corpus only): "
                     f"{cd['total_db_after_central_gib']} GiB  "
                     f"(extra paid storage {cd['extra_storage_gb']} GB)")
        sp = sc["spend"]
        lines.append(f"  backfill cost: ${sp['backfill_cost_3small_usd']} "
                     f"(3-small)  /  ${sp['backfill_cost_3large_usd']} (3-large alt)")
        co = sc["compute"]
        lines.append(f"  HNSW index {co['hnsw_index_central_gib']} GiB -> "
                     f"baseline {co['baseline_tier']['name']} (+$0/mo, disk-cached) / "
                     f"RAM-resident {co['recommended_tier']['name']} "
                     f"(+${co['recommended_tier']['incremental_over_pro_credit_usd']}/mo)")
        ms = sc["monthly_steady_state_usd"]
        lines.append(f"  monthly steady-state: ${ms['baseline_disk_cached']} (disk-cached) / "
                     f"${ms['ram_resident']} (RAM-resident)")
        gv = sc["gate_verdicts"]
        lines.append("  GATES:")
        lines.append(f"    $25 initial spend : {gv['initial_spend_25usd']['verdict']}  "
                     f"(value ${gv['initial_spend_25usd']['value_usd']})")
        lines.append(f"    12 GB new storage : {gv['new_storage_12gb']['verdict']}  "
                     f"(central {gv['new_storage_12gb']['value_gb_central']} GB, "
                     f"high {gv['new_storage_12gb']['value_gb_high_overhead']} GB)")
        lines.append(f"    $50/mo incremental: {gv['monthly_incremental_50usd']['verdict']}  "
                     f"(disk-cached ${gv['monthly_incremental_50usd']['value_baseline_disk_cached_usd']}, "
                     f"RAM-resident ${gv['monthly_incremental_50usd']['value_ram_resident_usd']})")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--results", metavar="PATH", help="write results JSON to PATH")
    p.add_argument("--assumptions", metavar="PATH",
                   help="write assumptions JSON to PATH")
    args = p.parse_args(argv)

    results = build_results()
    print(_fmt_summary(results))

    if args.results:
        out = Path(args.results)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(results, indent=2) + "\n")
        print(f"\nwrote results -> {out}")
    if args.assumptions:
        out = Path(args.assumptions)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(build_assumptions(), indent=2) + "\n")
        print(f"wrote assumptions -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
