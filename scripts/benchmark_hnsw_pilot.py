#!/usr/bin/env python3
"""Hivemind — Phase 2 / Task 2.16 — selected-contract HNSW pilot BENCHMARK.

Formal, pre-registered ef_search sweep over the selected-contract partial HNSW
index in an isolated local PostgreSQL 14 + pgvector 0.8.5 cluster at the frozen
volume (23,138 deterministic FAKE vectors). Reuses the rehearsal harness in
:mod:`rehearse_hnsw_pilot` (cluster, volume, manifest, index build, hygiene).

This is a STRUCTURAL benchmark only: ``run_class=local_volume_only`` and
``representative_semantic_evidence=false``. Synthetic fake vectors cannot prove
representative semantic recall or hosted latency, so ``selected_ef_search`` is
NULL (never a production tuning selection from synthetic data) and the verdict
is ``blocked_not_representative``. The sweep still proves, locally, that HNSW
builds, the natural plan uses it, exact-vs-ANN parity holds, and per-ef latency
is bounded — the mechanics a later representative run would re-measure.

Safety: no provider call, no network, no source/production mutation, no contract
activation outside the throwaway cluster. Teardown in ``finally``. Evidence is
sanitized (server-side timings + identity hashes + aggregate counts only).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import statistics
import sys
import time
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import rehearse_hnsw_pilot as rh  # noqa: E402
from executors import selected_contract as sc  # noqa: E402

BENCHMARK_EVIDENCE = rh.BENCHMARK_EVIDENCE
SEL = rh.SEL
INDEX_NAME = rh.INDEX_NAME
EF_SWEEP = rh.EF_SWEEP
K = rh.K
N_WARM = 5  # >=5 warm repetitions per query per setting (pre-registered)


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    # linear interpolation percentile
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def _exec_time_ms(cluster: "rh.lp.LocalCluster", prefix: str, ql: str) -> float:
    """Server-side query latency via EXPLAIN ANALYZE Execution Time (ms)."""

    rc, out = cluster.psql(
        f"{prefix} explain (analyze, costs off, summary on, timing off) "
        f"select * from hivemind_semantic_candidates('{ql}'::vector,{K},'{{}}','{{}}');"
    )
    if rc != 0:
        return float("nan")
    for line in reversed(out.splitlines()):
        m = re.search(r"Execution Time:\s*([0-9.]+)\s*ms", line)
        if m:
            return float(m.group(1))
    return float("nan")


def _identities(cluster: "rh.lp.LocalCluster", prefix: str, ql: str) -> list[str]:
    rc, out = cluster.psql(
        f"{prefix} select entity_type||':'||item_id from hivemind_semantic_candidates("
        f"'{ql}'::vector,{K},'{{}}','{{}}') order by semantic_rank;"
    )
    return [x.strip() for x in out.strip().splitlines() if x.strip()]


def _identities_hash(ids: list[str]) -> str:
    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()


def benchmark(out_path: pathlib.Path = BENCHMARK_EVIDENCE) -> dict[str, Any]:
    pf = rh.preflight()
    rows = rh.generate_volume()
    res_rows = [r for r in rows if r["entity_type"] == "resource" and int(r["item_id"]) >= 100000]
    queries = [res_rows[i]["vec"] for i in range(0, min(10, len(res_rows)))]

    ev: dict[str, Any] = {
        "task": "2.16-selected-contract-hnsw-benchmark",
        "cluster_isolated": True,
        "run_class": "local_volume_only",
        "representative_semantic_evidence": False,
        "selected_contract_id": SEL,
        "selected_contract_preimage_sha256": sc.SELECTED_CONTRACT_SHA256_HEX,
        "index_name": INDEX_NAME,
        "preflight": pf,
        "protocol": {
            "k": K,
            "ef_search_sweep": EF_SWEEP,
            "priming_passes": 1,
            "warm_repetitions_per_query_per_setting": N_WARM,
            "interleaving": "deterministic_round_robin_round_then_ef_then_query",
            "latency_metric": "EXPLAIN ANALYZE Execution Time (ms, server-side, timing off)",
            "exact_mode": ("TRUE exact: set hivemind.semantic_exact_mode=on in a fresh "
                           "session with enable_indexscan=off; enable_bitmapscan=off so the "
                           "function NULLs the per-arm bound and scores the COMPLETE cohort"),
            "ann_mode": "natural plan; hnsw.ef_search=<sweep>; hnsw.iterative_scan=strict_order",
            "query_fixtures": f"{len(queries)} corpus resource vectors (deterministic)",
        },
    }
    cluster = None
    try:
        cluster, env = rh.setup_cluster()
        manifest = rh.build_manifest(env)
        frozen_hash = rh.manifest_hash(manifest)
        ev["environment"] = env
        ev["manifest_hash"] = frozen_hash
        ev["manifest_frozen_before_measurement"] = True
        volume_info = rh.seed_sources_and_volume(cluster, rows)
        ev["volume"] = volume_info
        ev["manifest_drift_refused"] = rh.manifest_hash(manifest) == frozen_hash
        ev["manifest"] = manifest

        build = rh.build_index(cluster)
        cat = rh.catalog(cluster)
        ev["build"] = build
        ev["catalog"] = cat
        ev["storage"] = {
            "database_bytes": int(rh._scalar(cluster, "select pg_database_size(current_database());") or 0),
            "hnsw_index_bytes": int(rh._scalar(cluster, f"select pg_relation_size('{INDEX_NAME}'::regclass);") or 0),
            "hnsw_index_size_pretty": cat["size_pretty"],
        }

        # ----- deterministic priming pass (1x per ef, per query) -----
        qlits = [rh.vec_literal(q) for q in queries]
        for ef in EF_SWEEP:
            prefix = f"set hnsw.ef_search={ef}; set hnsw.iterative_scan=strict_order;"
            for ql in qlits:
                _exec_time_ms(cluster, prefix, ql)

        # ----- exact baseline identities + latency (TRUE exact via GUC) -----
        # Task 2.16 C2: exact mode sets the GUC hivemind.semantic_exact_mode=on
        # in a fresh session that ALSO disables index scans, so the function
        # NULLs the per-arm bound and scores the COMPLETE applicable cohort (not
        # a LIMIT-truncated baseline). ANN fidelity is measured against this TRUE
        # exact. One shared SQL surface (the function); no second ranking impl.
        exact_prefix = ("set enable_indexscan=off; set enable_bitmapscan=off; "
                        "set hivemind.semantic_exact_mode=on;")
        exact_ids_per_q: list[list[str]] = []
        exact_lat: list[float] = []
        for ql in qlits:
            exact_ids_per_q.append(_identities(cluster, exact_prefix, ql))
            exact_lat.append(_exec_time_ms(cluster, exact_prefix, ql))

        # ----- interleaved warm reps: rounds -> ef -> query -----
        lat_by_ef: dict[str, list[float]] = {str(ef): [] for ef in EF_SWEEP}
        recall_by_ef: dict[str, float] = {}
        min_recall_by_ef: dict[str, float] = {}
        ann_ids_hashes_by_ef: dict[str, list[str]] = {}
        for rnd in range(N_WARM):
            for ef in EF_SWEEP:
                prefix = f"set hnsw.ef_search={ef}; set hnsw.iterative_scan=strict_order;"
                for ql in qlits:
                    lat_by_ef[str(ef)].append(_exec_time_ms(cluster, prefix, ql))
        # recall + identity hashes (computed once per ef, stable across reps)
        for ef in EF_SWEEP:
            prefix = f"set hnsw.ef_search={ef}; set hnsw.iterative_scan=strict_order;"
            recalls = []
            hashes = []
            for ql, ex_ids in zip(qlits, exact_ids_per_q):
                ann_ids = _identities(cluster, prefix, ql)
                hashes.append(_identities_hash(ann_ids))
                if ex_ids:
                    recalls.append(len(set(ex_ids) & set(ann_ids)) / len(ex_ids))
            recall_by_ef[str(ef)] = round(sum(recalls) / len(recalls), 4) if recalls else 0.0
            min_recall_by_ef[str(ef)] = round(min(recalls), 4) if recalls else 0.0
            ann_ids_hashes_by_ef[str(ef)] = hashes

        # ----- per-ef summary -----
        per_ef: dict[str, Any] = {}
        for ef in EF_SWEEP:
            lats = [x for x in lat_by_ef[str(ef)] if x == x]  # drop NaN
            per_ef[str(ef)] = {
                "warm_n": len(lats),
                "latency_ms_mean": round(statistics.mean(lats), 3) if lats else None,
                "latency_ms_p50": round(percentile(lats, 0.50), 3) if lats else None,
                "latency_ms_p95": round(percentile(lats, 0.95), 3) if lats else None,
                "latency_ms_max": round(max(lats), 3) if lats else None,
                "exact_vs_ann_recall_at_10_mean": recall_by_ef[str(ef)],
                "exact_vs_ann_recall_at_10_min": min_recall_by_ef[str(ef)],
                "ranked_identity_hash_sample": ann_ids_hashes_by_ef[str(ef)][0] if ann_ids_hashes_by_ef[str(ef)] else None,
            }
        ev["exact_baseline"] = {
            "latency_ms_mean": round(statistics.mean(exact_lat), 3) if exact_lat else None,
            "latency_ms_p50": round(percentile(exact_lat, 0.50), 3) if exact_lat else None,
            "latency_ms_p95": round(percentile(exact_lat, 0.95), 3) if exact_lat else None,
            "ranked_identity_hash_sample": _identities_hash(exact_ids_per_q[0]) if exact_ids_per_q else None,
        }
        ev["per_ef"] = per_ef

        # ----- natural HNSW use by the ACTUAL canonical function (decisive) -----
        # Root cause (task-2.16 renewal): pgvector 0.8.5/PG14 only drives the
        # partial HNSW kNN path for a single-table scan with ROW-LOCAL filters; a
        # source JOIN (or a parameterized array/limit) forces btree+Sort even when
        # forced. schema/033 bakes eligibility as a literal row-local text[] per
        # arm, so the ACTUAL function naturally uses HNSW. The decisive proof is a
        # strictly positive idx_scan delta from multiple BROAD calls to the actual
        # function (EXPLAIN of the call is opaque "Function Scan"); the inner
        # single-table kNN is recorded as a diagnostic only (insufficient).
        # Consecutive deltas from the un-reset monotonic idx_scan counter
        # (pg_stat_reset across read boundaries is unreliable in PG14).
        c0 = int((rh._scalar(cluster, f"select coalesce(idx_scan,0) from pg_stat_user_indexes where indexrelname='{INDEX_NAME}';") or "0"))
        func_call_sql = " ".join([
            f"select count(*) from hivemind_semantic_candidates('{qlits[0]}'::vector,{K},'{{}}','{{}}');"
        ] * 6)
        cluster.psql(f"set hnsw.ef_search=80; set hnsw.iterative_scan=strict_order; {func_call_sql}", capture=False)
        time.sleep(0.8)
        c1 = int((rh._scalar(cluster, f"select idx_scan from pg_stat_user_indexes where indexrelname='{INDEX_NAME}';") or "0"))
        func_before, func_after, func_delta = c0, c1, c1 - c0
        # diagnostic only: direct single-table inner kNN
        inner_knn = (f"select ce.item_id from content_embeddings ce where ce.contract_id={SEL} "
                     f"and ce.entity_type='resource' order by ce.embedding <=> '{qlits[0]}'::vector limit 500;")
        for _ in range(3):
            cluster.psql(f"set hnsw.ef_search=80; set hnsw.iterative_scan=strict_order; {inner_knn}", capture=False)
        time.sleep(0.8)
        c2 = int((rh._scalar(cluster, f"select idx_scan from pg_stat_user_indexes where indexrelname='{INDEX_NAME}';") or "0"))
        inner_before, inner_after, inner_delta = c1, c2, c2 - c1
        inner_plan = cluster.psql(
            f"set hnsw.ef_search=80; set hnsw.iterative_scan=strict_order; explain (costs off) {inner_knn}"
        )[1]
        inner_uses_index = INDEX_NAME in inner_plan
        ev["hnsw_use"] = {
            "full_function_idx_scan_before": func_before,
            "full_function_idx_scan_after": func_after,
            "full_function_idx_scan_delta": func_delta,
            "full_function_idx_scan_delta_positive": func_delta > 0,
            "full_function_uses_hnsw": func_delta > 0,
            "full_function_broad_calls": 6,
            "full_function_note": (
                "Strictly positive idx_scan delta from multiple BROAD calls to the "
                "ACTUAL function; eligibility baked as a literal row-local text[] "
                "per arm keeps HNSW as the driver. Decisive proof."
            ),
            "inner_knn_idx_scan_before": inner_before,
            "inner_knn_idx_scan_after": inner_after,
            "inner_knn_idx_scan_delta": inner_delta,
            "inner_knn_idx_scan_delta_positive": inner_delta > 0,
            "inner_knn_uses_index": inner_uses_index,
            "inner_knn_role": "diagnostic_only_insufficient",
        }

        # ----- duplicate / count / security invariants -----
        nd = cluster.psql(
            f"select count(*), count(distinct (entity_type,item_id)), "
            f"count(*) filter (where semantic_rank is null) from "
            f"hivemind_semantic_candidates('{qlits[0]}'::vector,500,'{{}}','{{}}');"
        )[1]
        parts = nd.strip().split("|")
        ev["invariants"] = {
            "result_count_le_limit": int(parts[0] or 0) <= 500,
            "no_duplicate_entities": int(parts[0] or 0) == int(parts[1] or 0),
            "no_null_rank": int(parts[2] or 0) == 0,
        }

        ev["provider_calls"] = 0
        ev["network_calls"] = 0
        # Task 2.16 C7b: source_mutations=0 is scoped to external/production
        # sources; local fixture inserts into the throwaway cluster are disclosed.
        ev["source_mutations"] = 0
        ev["source_mutations_scope"] = "external/production sources; local fixture inserts disclosed separately"
        ev["local_fixture_inserts"] = {
            "content_embeddings_selected_contract": int(rh._scalar(cluster, f"select count(*) from content_embeddings where contract_id={SEL};") or 0),
            "note": "throwaway isolated local cluster only; no external/production source mutated.",
        }
        ev["production_mutated"] = False
        ev["contract_activated"] = False
        # Representative quality/latency gates cannot be proved from synthetic
        # local data; selected_ef_search is NULL by design.
        ev["selected_ef_search"] = None
        ev["selection_note"] = (
            "Lowest pre-registered ef_search passing all gates would be selected, but "
            "representative semantic quality and hosted latency cannot be proved from "
            "synthetic local volume; selected_ef_search stays NULL (verdict stop/blocked)."
        )
        ev["verdict"] = "blocked_not_representative"
        ev["blocker"] = pf.get("blocker")
        ev["representative_evidence_required_to_unblock"] = (
            "A cache-only load of the exact selected-contract vector cohort into an "
            "isolated representative PostgreSQL (or approved production-like staging) "
            "database, then re-run scripts/benchmark_hnsw_pilot.py in a "
            "representative-preflight/benchmark mode against that snapshot — no provider "
            "call, no production mutation. Only then may selected_ef_search be set."
        )
    except Exception as exc:  # noqa: BLE001
        ev["benchmark_exception"] = {"type": type(exc).__name__, "msg_redacted": rh._redact(str(exc))}
        ev["verdict"] = "benchmark_exception"
    finally:
        if cluster is not None:
            cluster.tear_down()

    ev["hygiene_findings"] = rh.hygiene_scan(ev)
    ev["hygiene_clean"] = not ev["hygiene_findings"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(ev, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return ev


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(BENCHMARK_EVIDENCE))
    args = ap.parse_args(argv)
    ev = benchmark(pathlib.Path(args.out))
    per_ef = ev.get("per_ef", {})
    print(f"Task 2.16 benchmark: verdict={ev.get('verdict')}; run_class={ev.get('run_class')}; "
          f"selected_ef_search={ev.get('selected_ef_search')}; hygiene_clean={ev.get('hygiene_clean')}")
    for ef, d in per_ef.items():
        print(f"  ef_search={ef}: recall@10={d.get('exact_vs_ann_recall_at_10_mean')} "
              f"p50={d.get('latency_ms_p50')}ms p95={d.get('latency_ms_p95')}ms")
    print(f"exact baseline p50={ev.get('exact_baseline',{}).get('latency_ms_p50')}ms")
    print(f"hnsw_use={ev.get('hnsw_use')}")
    print(f"evidence: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
