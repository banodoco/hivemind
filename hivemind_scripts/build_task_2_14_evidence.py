#!/usr/bin/env python3
"""Offline evidence builder for the task-2.14 final correction.

Strictly offline: no provider/network/DB calls, no mutation of the two paid
cache files (opened read-only via :mod:`eval.retrieval.semantic` helpers). It:

* loads the durable eval3 decision JSON + fixed golden file + immutable caches;
* computes the deterministic golden-query token estimate;
* builds the private sanitized replay bundle + runs the strict offline replay
  (zero provider/network calls) to obtain the replay proof;
* writes the tracked frozen/evidence manifest;
* enriches the tracked decision JSON with the selection, accounting,
  later-chunk diagnostic, non-winner state, Task 2.17 handoff, and replay
  result blocks; and re-renders the decision Markdown consistently.

Idempotent. Re-running reproduces byte-identical artifacts (deterministic
timestamp). It never changes the recorded winner or per-candidate metrics.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.retrieval import loader  # noqa: E402
from eval.retrieval import semantic as sem  # noqa: E402

DEFAULT_DECISION_JSON = REPO_ROOT / "docs" / "hybrid-search" / "task-2.14-embedding-decision.json"
DEFAULT_DECISION_MD = REPO_ROOT / "docs" / "hybrid-search" / "task-2.14-embedding-decision.md"
DEFAULT_MANIFEST = REPO_ROOT / "docs" / "hybrid-search" / "task-2.14-frozen-manifest.json"
DEFAULT_GOLDEN = REPO_ROOT / "eval" / "retrieval" / "golden" / "golden-v1.json"
DEFAULT_CACHE_DIR = sem.DEFAULT_CACHE_DIR
DEFAULT_BUNDLE = DEFAULT_CACHE_DIR / "replay-bundle.json"
DETERMINISTIC_TS = "2026-07-29T19:16:00+00:00"  # eval3 generation moment

# Task 2.17 handoff: PROPAGATION / VERIFICATION / ACCEPTANCE TESTS for the
# selected 384 / prose 512-50 / workflow_python 512-50 contract. This is a
# handoff ONLY — it does NOT activate production. Actual activation is a later,
# separate rollout decision.
TASK_2_17_HANDOFF = {
    "scope": (
        "propagation / verification / acceptance-test handoff for the selected "
        "384 / prose 512-50 / workflow_python 512-50 contract into production. "
        "This task does NOT activate production; actual activation is a later, "
        "separate rollout decision."
    ),
    "kind": "propagation-verification-acceptance-handoff (NOT production activation)",
    "dimension_surfaces_hardcoded_384": [
        "executors/embedding_contract.py: PILOT_DIMENSION_384 (=384) is the default in DeterministicFakeEmbedder and OpenAIEmbedder and the two pilot ContractSpec identities",
        "executors/embedding_backfill.py: backfill dimension default is 384",
        "schema/022_content_embeddings.sql: physical embedding vector(384) column + dimension trigger (rejects v_dimension <> 384)",
        "schema/027_embedding_worker_surface.sql: worker insert path assumes vector(384)",
    ],
    "chunk_config_surfaces_hardcoded_512_50": [
        "executors/chunking.py: DEFAULT_PROSE_CONFIG (target 512 / overlap 50) and the workflow_python default (target 512 / overlap 50)",
        "schema/027_embedding_worker_surface.sql: hivemind_chunk_text defaults p_target_tokens=512, p_overlap_tokens=50",
    ],
    "known_production_gaps_to_close": [
        "worker payload python_public_state: the embedding worker does not yet persist/propagate a workflow_python representation 'public_state' on its payload; production must add it so the selected workflow_python chunk stream is the one actually indexed",
        "quarantine handling: the production ingest/worker path must apply the same secret-quarantine the evaluator's canonical_representations applies, or quarantined python could be indexed in production",
        "stale-vector cleanup / re-embedding: production has no automated path to re-embed or clean stale vectors when content changes (the evaluator assumes immutable content); a cleanup/re-embedding job is required",
        "production/evaluator chunker parity: the production chunker (schema/027 + executors/chunking defaults) MUST be byte-identical to the evaluator's frozen chunker (target/overlap/normalization), or production rankings will diverge from this decision",
        "contract identity/version surface: dimension-only production ContractSpec ids are INSUFFICIENT — production must expose a contract identity/version surface that distinguishes the selected 512/50 small configuration from the large (1024/100, 2048/100) candidate, mirroring Candidate.eval_contract_id + chunk_config_version",
    ],
    "propagation_verification_gaps": [
        "later-chunk / workflow_python provenance: eval3's workflow_code Recall proves ENTITY recall (the right workflow was retrieved) but CANNOT prove that the workflow_python REPRESENTATION won best-chunk collapse, because eval3 per-case rankings persisted only entity keys, not matched_chunk_index / matched_representation (raw_eval3_ranking_provenance_not_persisted). Production acceptance tests must persist and assert matched_representation == workflow_python for the workflow_code cases; do not fabricate this provenance for eval3.",
    ],
    "dimension_mismatch_fixtures": [
        "schema/022_content_embeddings.sql: vector(384) column + trigger are the physical + data dimension-mismatch guard",
        "schema/023_embedding_contract_switch.sql: the active-contract switch must resolve to a 384-d contract",
        "eval/retrieval/semantic.py: EmbeddingCache validates cached-vector dimension on load and on store",
    ],
    "contract_version_fixtures": [
        "schema/024_identity_and_canonical_representations.sql: canonicalization/chunking version identity",
        "executors/embedding_contract.py: ContractSpec.contract_identity_input (provider+model+dimension+canonicalization_version+chunking_version)",
        "eval/retrieval/semantic.py: Candidate.eval_contract_id extends the production identity with chunk_config_version",
    ],
    "must_verify": [
        "production vector column + worker enforce dimension == 384 (any mismatch rejected)",
        "production chunker defaults == 512/50 for both prose and workflow_python (byte-parity with the evaluator)",
        "the active embedding contract id resolves to the selected 384 / 512-50 identity (with the chunk-config-version surface, not dimension-only)",
        "no 1536-d path is activated (capacity-disqualified at 16.4 GB > 12 GB gate)",
    ],
}


def _dumps(obj: Any) -> str:
    """Canonical tracked-evidence serialization (stable, hygiene-scanned)."""

    return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def _write_tracked(path: Path, text: str, *, mode: int = 0o644) -> str:
    """Atomically write + return the exact-file SHA-256 (mode-safe).

    Stages via :func:`sem.atomic_write_text` (private from first byte), hygiene-
    scans nothing here (callers scan the object first), and returns the SHA-256
    of the EXACT bytes written so the manifest can bind file-byte hashes.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    sem.atomic_write_text(path, text, mode=mode)
    return sem._sha256_file(path)


def _write_json(path: Path, obj: Any) -> None:
    violations = sem.scan_envelope(obj)
    if violations:
        raise RuntimeError(f"evidence hygiene violations {violations} writing {path}")
    _write_tracked(path, _dumps(obj))


def _non_selection_reason(cand_entry: dict[str, Any], decision: dict[str, Any]) -> str:
    name = cand_entry["candidate"]["name"]
    reasons = cand_entry.get("disqualify_reasons") or []
    if reasons:
        return "capacity-disqualified (" + ", ".join(reasons) + ")"
    # Eligible but not selected: lower priority on the frozen tiebreak.
    overall = cand_entry["overall"]
    winner = next(c for c in decision["candidates"] if c["candidate"]["name"] == decision["winner"]["name"])
    parts = []
    if overall.get("recall@10") != winner["overall"].get("recall@10"):
        return f"lower recall@10 ({overall.get('recall@10')} < {winner['overall'].get('recall@10')})"
    if overall.get("mrr") != winner["overall"].get("mrr"):
        parts.append(f"mrr {overall.get('mrr')} < {winner['overall'].get('mrr')}")
    if overall.get("ndcg@10") != winner["overall"].get("ndcg@10"):
        parts.append(f"ndcg@10 {overall.get('ndcg@10')} < {winner['overall'].get('ndcg@10')}")
    if cand_entry["workflow_code_recall_at_10"] != winner["workflow_code_recall_at_10"]:
        parts.append("lower workflow_code_recall@10")
    if cand_entry["long_resource_chunk_recall_at_10"] != winner["long_resource_chunk_recall_at_10"]:
        parts.append("lower long_resource_chunk_recall@10")
    if not parts:
        parts.append("higher standalone candidate cost on the final tiebreak axis")
    return "lower priority on frozen tiebreak (" + "; ".join(parts) + f") [{name}]"


def refresh_identity_to_current_chunking(decision: dict[str, Any]) -> dict[str, Any]:
    """Re-derive each candidate's identity block (and the winner's) from the
    frozen ``sem.CANDIDATES`` grid so the decision carries the CURRENT chunking
    identity.

    The identity fields (provider/model/dimension/versions/chunk-config/eval id)
    are a pure function of the candidate configuration, independent of the eval3
    run, so re-deriving them under CHUNKING v2 is correct. The frozen eval3
    QUALITY metrics, cohort counts, and provider usage live in sibling keys and
    are preserved verbatim — the v2 chunker is a cohort no-op (proven by the
    zero-call equivalence proof), so the metrics are unchanged.
    """

    by_name = {c.name: c for c in sem.CANDIDATES}
    carried = {}
    for entry in decision["candidates"]:
        name = entry["candidate"]["name"]
        if name not in by_name:
            raise RuntimeError(f"decision candidate {name!r} not in frozen grid")
        carried[name] = entry["candidate"]
        entry["candidate"] = by_name[name].to_sanitized_dict()
    wname = decision["winner"]["name"]
    if wname not in by_name:
        raise RuntimeError(f"decision winner {wname!r} not in frozen grid")
    decision["winner"] = by_name[wname].to_sanitized_dict()
    decision["identity_refresh"] = {
        "chunking_version": sem.CHUNKING_VERSION,
        "chunk_config_identity_version": sem.CHUNK_CONFIG_IDENTITY_VERSION,
        "candidate_grid_hash": sem.candidate_grid_hash(),
        "note": (
            "candidate/winner identity blocks re-derived from the frozen candidate grid "
            "under the current chunking version (CHUNKING v2, bounded oversized workflow-"
            "Python fallback). eval3 quality metrics, cohort counts, and provider usage are "
            "the frozen eval3 results, unchanged: the v2 chunker is cohort-no-op, proven by "
            "the zero-call equivalence proof (docs/hybrid-search/task-2.14-v2-chunking-"
            "equivalence.json)."
        ),
    }
    return decision


def enrich_decision(decision: dict[str, Any], golden_qe: dict[str, Any]) -> dict[str, Any]:
    """Add the selection / accounting / handoff / replay blocks in place."""

    # 1. Normalize the decision policy tiebreak label + note (consistency).
    pol = decision["decision_policy"]
    pol["selection_tiebreak"] = [
        "overall_recall_at_10",
        "mrr",
        "ndcg_at_10",
        "workflow_code_recall_at_10",
        "long_resource_chunk_recall_at_10",
        "lower_standalone_candidate_cost",
    ]
    pol["selection_tiebreak_note"] = (
        "the cost axis is the deterministic order-independent standalone "
        "candidate cost, NOT the order-dependent incremental cache-miss cost"
    )

    # Normalize the winner rationale's selection key to the exact frozen
    # selection semantics. The durable eval3 JSON may carry the stale wording
    # "lower actual cost" (the historical order-dependent incremental spend);
    # rebuilds must emit the deterministic, order-independent standalone
    # candidate cost axis. Single source of truth: sem.SELECTION_KEY_USED.
    wr = decision.setdefault("winner_rationale", {})
    wr["selection_key_used"] = sem.SELECTION_KEY_USED

    # 2. Per-candidate: later-chunk diagnostic, selection cost, selection state.
    preflight = decision["preflight"]["per_candidate"]
    gq_tokens = int(golden_qe["estimated_input_tokens"])
    winner_name = decision["winner"]["name"]
    for c in decision["candidates"]:
        name = c["candidate"]["name"]
        c.setdefault("later_chunk_diagnostic", {
            "available": False,
            "reason": sem.LATER_CHUNK_UNAVAILABLE_REASON,
            "later_chunk_hit_rate": None,
            "note": (
                "eval3 per-case rankings persist only entity keys, not "
                "matched_chunk_index, so later_chunk_hit_rate is not recomputable; "
                "long_resource_chunk_recall_at_10 is the frozen selection signal"
            ),
        })
        corpus_tokens = int(preflight[name]["projected_input_tokens"])
        total = corpus_tokens + gq_tokens
        c["selection_cost_usd"] = round(total / 1_000_000.0 * sem.PRICE_PER_1M_TOKENS_USD, 6)
        is_winner = name == winner_name
        c["selection_state"] = {
            "selected": is_winner,
            "active": False,  # nothing is production-activated in this task
            "evaluation_only": not is_winner,
            "production_activated": False,
            "non_selection_reason": None if is_winner else _non_selection_reason(c, decision),
        }

    # 3. Accounting block.
    decision["accounting"] = sem.build_evidence_accounting(decision, golden_qe)

    # 4. Selection block.
    w = decision["winner"]
    decision["selection"] = {
        "selected_production_dimension": w["dimension"],
        "selected_prose_chunk_contract": w["prose"],
        "selected_workflow_python_chunk_contract": w["python"],
        "selected_evaluation_contract_id": w["eval_contract_id"],
        "selected_chunk_config_identity": w["chunk_config_identity"],
        "selected_base_contract_id_dimension_only": w["base_contract_id_dimension_only"],
        "production_chunking_version_handoff": {
            "canonicalization_version": w["canonicalization_version"],
            "chunking_version": w["chunking_version"],
            "chunk_config_version": w["chunk_config_version"],
        },
        "production_activated": False,
        "note": (
            "selection recorded by task 2.14; task 2.17 is a "
            "propagation/verification/acceptance-test handoff only — actual "
            "activation is a later, separate rollout decision"
        ),
    }

    # 5. Task 2.17 handoff.
    decision["task_2_17_handoff"] = TASK_2_17_HANDOFF

    # 6. Historical-defect disclosure (top-level, mirrored from accounting).
    decision["historical_defect_disclosure"] = decision["accounting"]["historical_duplicate_attempts"]
    return decision


def render_markdown(decision: dict[str, Any]) -> str:
    A: list[str] = []
    A.append("# Task 2.14 — Embedding Dimension & Chunk-Configuration Decision")
    A.append("")
    A.append(f"- Mode: `{decision['mode']}`")
    A.append("- Exact-cosine local evaluation (numpy). **Not** HNSW/SQL/Edge latency.")
    A.append(f"- Golden set: `{decision['golden_version']}` (112 cases: 104 judged, 8 no-hit, 141 judgments).")
    A.append("- Capacity facts frozen from task 0.7: 384-d full corpus 4.59 GB (PASS); 1536-d 16.4 GB (FAIL the 12 GB gate).")
    A.append(f"- Generated (eval3): `{decision.get('generated_at')}`")
    A.append("")
    A.append("## Candidate metrics")
    A.append("")
    A.append("| Candidate | Dim | recall@10 | MRR | nDCG@10 | wf_code R@10 | long-res R@10 | later-chunk hit | standalone $ | eligible |")
    A.append("|---|---|---|---|---|---|---|---|---|---|")
    for c in decision["candidates"]:
        lcd = c["later_chunk_diagnostic"]
        later = "n/a" if not lcd.get("available") else f"{lcd.get('later_chunk_hit_rate'):.4f}"
        elig = "yes" if c["eligible_for_selection"] else "no (" + ",".join(c["disqualify_reasons"]) + ")"
        A.append(
            f"| {c['candidate']['name']} | {c['candidate']['dimension']} | "
            f"{c['overall'].get('recall@10', 0):.4f} | {c['overall'].get('mrr', 0):.4f} | "
            f"{c['overall'].get('ndcg@10', 0):.4f} | {c['workflow_code_recall_at_10']:.4f} | "
            f"{c['long_resource_chunk_recall_at_10']:.4f} | {later} | "
            f"{c['selection_cost_usd']:.4f} | {elig} |"
        )
    A.append("")
    A.append("> `long_res R@10` = `long_resource_chunk_recall_at_10` (the frozen selection signal). "
             "`later-chunk hit` = `later_chunk_hit_rate`, which is **unavailable** for eval3 "
             "(`raw_eval3_ranking_provenance_not_persisted`) and never fabricated.")
    A.append("")
    A.append("## Decision")
    A.append("")
    s = decision["selection"]
    A.append(f"- **Selected production dimension:** `{s['selected_production_dimension']}`")
    A.append(f"- **Prose chunk contract:** target `{s['selected_prose_chunk_contract']['target_tokens']}` / overlap `{s['selected_prose_chunk_contract']['overlap_tokens']}` tokens")
    A.append(f"- **Workflow-Python chunk contract:** target `{s['selected_workflow_python_chunk_contract']['target_tokens']}` / overlap `{s['selected_workflow_python_chunk_contract']['overlap_tokens']}` tokens")
    A.append(f"- **Selected evaluation contract id:** `{s['selected_evaluation_contract_id']}` (`{s['selected_chunk_config_identity']}`)")
    A.append(f"- **Production chunking-version handoff:** canonicalization v{s['production_chunking_version_handoff']['canonicalization_version']}, chunking v{s['production_chunking_version_handoff']['chunking_version']}, chunk-config v{s['production_chunking_version_handoff']['chunk_config_version']}")
    A.append(f"- **Production activated:** `{s['production_activated']}` (task 2.17 is a propagation/verification/acceptance-test handoff only; actual activation is a later, separate rollout decision)")
    A.append(f"- **Selection rationale:** {decision['winner_rationale'].get('selection_key_used')}")
    A.append(f"- **Eligible ranking:** {decision['winner_rationale'].get('eligible_ranking')}")
    A.append("")
    A.append("### Non-winner state (machine-readable in JSON `candidates[].selection_state`)")
    A.append("")
    for c in decision["candidates"]:
        st = c["selection_state"]
        reason = st["non_selection_reason"] or "selected winner"
        A.append(f"- `{c['candidate']['name']}`: selected=`{st['selected']}`, active=`{st['active']}`, evaluation_only=`{st['evaluation_only']}`, production_activated=`{st['production_activated']}` — {reason}")
    A.append("")
    A.append("## Provider accounting (eval3 exact incremental bake-off)")
    A.append("")
    acc = decision["accounting"]["eval3_actual_incremental_bakeoff"]
    A.append("| Candidate | requests | inputs | tokens | cost $ | cache hits | cache misses |")
    A.append("|---|---|---|---|---|---|---|")
    for name, a in acc["per_candidate"].items():
        A.append(f"| {name} | {a['api_requests']} | {a['embedded_inputs']} | {a['input_tokens_from_usage']} | {a['cost_usd']:.4f} | {a['cache_hits']} | {a['cache_misses']} |")
    A.append("")
    A.append(f"- **eval3 exact total:** {acc['total_input_tokens']} input tokens = **${acc['total_cost_usd']}** (shared dimension-cache reuse recorded per candidate).")
    A.append("")
    A.append("### Standalone candidate accounting (order-independent tiebreak cost)")
    A.append("")
    sa = decision["accounting"]["standalone_candidate_accounting"]["per_candidate"]
    A.append("| Candidate | corpus texts | corpus tok | golden tok | total tok | cost $ |")
    A.append("|---|---|---|---|---|---|")
    for name, a in sa.items():
        A.append(f"| {name} | {a['corpus_unique_texts']} | {a['corpus_estimated_input_tokens']} | {a['golden_query_estimated_tokens']} | {a['total_estimated_input_tokens']} | {a['estimated_cost_usd']:.4f} |")
    A.append("")
    A.append("### Historical duplicate-attempt disclosure")
    A.append("")
    h = decision["accounting"]["historical_duplicate_attempts"]
    A.append(f"- **Exact earlier spend:** `{h['exact_spend']}` — {h['exact_spend_reason']}")
    A.append(f"- **Destructive cache resets disclosed:** {h['destructive_cache_resets_disclosed']}; duplicate provider work was performed in the interrupted attempts.")
    A.append(f"- **Evaluation attempts:** {h['evaluation_attempts']}; per-full-bakeoff projection `${h['per_full_bakeoff_projected_cost_usd']}`.")
    A.append(f"- **Conservative reconstructed estimate:** **${h['conservative_reconstructed_estimate_usd']}** — {h['estimate_label']}")
    A.append(f"- Estimate within `${decision['accounting']['spend_cap_usd']}` cap: `{h['within_spend_cap']}`. {h['within_spend_cap_note']}.")
    A.append("")
    if decision.get("preflight"):
        pf = decision["preflight"]["aggregate"]
        A.append("## Preflight (frozen, pre-call)")
        A.append("")
        A.append(f"- Projected spend (all 4 candidates): `${pf['projected_cost_usd_all_candidates']}` of `${pf['spend_cap_usd']}` cap — within cap: `{pf['within_spend_cap']}`")
        A.append(f"- Price assumption: {pf['price_assumption']}")
        A.append("")
    rp = decision.get("evidence_replay", {}).get("strict_offline_replay", {})
    A.append("## Immutable offline evidence replay")
    A.append("")
    A.append(f"- Provider calls attempted: `{rp.get('provider_calls_attempted')}`; network calls: `{rp.get('network_calls')}`.")
    A.append(f"- Winner reproduced from frozen policy: `{rp.get('winner_reproduced')}`; checks passed: `{rp.get('n_checks')}`.")
    A.append(f"- Replay bundle: `{decision['evidence_replay']['replay_bundle']['path']}` (file sha256 `{decision['evidence_replay']['replay_bundle']['file_sha256'][:16]}…`).")
    A.append(f"- Evidence pair id: `{decision['evidence_replay']['evidence_pair_id']}` (shared by the JSON + Markdown; bound by the manifest).")
    A.append(f"- Frozen manifest: `{decision['evidence_replay']['frozen_manifest_path']}`.")
    A.append("")
    A.append("## Honest limitations")
    A.append("")
    A.append("- Ranking is **local exact cosine** over the eval3 cohort — not HNSW/SQL/Edge.")
    A.append("- The 1536-d dimension is evaluated for quality but **capacity-disqualified** (16.4 GB > 12 GB gate).")
    A.append("- The eval3 cohort was an **observed bounded read-only eval3 cohort whose raw contents were NOT transactionally frozen/persisted and cannot be raw-replayed** (the raw texts / identity-to-chunk mapping were not persisted before the provider calls).")
    A.append("- Task 2.17 is a **propagation / verification / acceptance-test handoff**; it does NOT activate production. Actual activation is a later, separate rollout decision, and there are known production gaps to close first (see `task_2_17_handoff.known_production_gaps_to_close`).")
    A.append("- **`later_chunk_hit_rate` is unavailable** for eval3 (`raw_eval3_ranking_provenance_not_persisted`); eval3 workflow_code Recall proves entity recall but CANNOT prove the workflow_python representation won. `long_resource_chunk_recall_at_10` is the frozen selection signal and is unaffected.")
    A.append("- **Exact earlier-attempt spend is unavailable**; the conservative reconstructed estimate is an estimate, NOT a guaranteed upper bound and NOT proof that true spend was below the $25 cap (in-flight/retry billing and golden-query tokens were omitted from the frozen preflight).")
    acc_runtime = decision["accounting"]["runtime_spend_gates"]["aggregate_actual_usage_guard"]
    A.append(f"- **Runtime failed-attempt billing is unavailable and NOT counted.** The aggregate actual-usage guard records usage ONLY for a response the provider returned; a failed attempt (transport error / HTTP 429 / 5xx, retries bounded to `{acc_runtime['retries_bounded_max_attempts']}` total attempts) reports no usage and is not counted. The recorded aggregate token/cost total is the sum of successful-response usage only and is **NOT proof against billed-but-unreported failed attempts** (see `accounting.runtime_spend_gates.aggregate_actual_usage_guard`).")
    A.append("- The strict replay is a **cryptographic evidence/cache replay**, not a recomputation of exact-cosine retrieval from raw text.")
    A.append("")
    return "\n".join(A)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Offline task-2.14 evidence builder.")
    ap.add_argument("--decision-json", default=str(DEFAULT_DECISION_JSON))
    ap.add_argument("--decision-md", default=str(DEFAULT_DECISION_MD))
    ap.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    ap.add_argument("--golden", default=str(DEFAULT_GOLDEN))
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    ap.add_argument("--bundle", default=str(DEFAULT_BUNDLE))
    args = ap.parse_args(argv)

    decision = json.loads(Path(args.decision_json).read_text(encoding="utf-8"))
    golden_path = Path(args.golden)
    cache_dir = Path(args.cache_dir)
    bundle_path = Path(args.bundle)

    golden = loader.load_golden_set(golden_path)
    golden_qe = sem.golden_query_token_estimate(golden)

    # Re-derive candidate/winner identity to the current chunking version (v2),
    # preserving the frozen eval3 metrics/counts/usage in their sibling keys.
    decision = refresh_identity_to_current_chunking(decision)

    # Enrich the decision (selection / accounting / handoff / per-candidate state).
    decision = enrich_decision(decision, golden_qe)

    # The private cache parent dir must be 0700 (the bundle/snapshot live here).
    # The two paid cache files are already 0600 and are NEVER chmod'd here.
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(cache_dir, sem.PRIVATE_DIR_MODE)

    # Build the private sanitized replay bundle (no raw text / vectors).
    bundle = sem.build_replay_bundle(
        decision, golden_path=golden_path, cache_dir=cache_dir, golden_query_estimate=golden_qe
    )
    # Write the bundle PRIVATE (0600) + atomically; capture exact file bytes hash.
    if sem.scan_envelope(bundle):
        raise RuntimeError("bundle hygiene violation before write")
    bundle_file_sha256 = _write_tracked(
        bundle_path, _dumps(bundle), mode=sem.PRIVATE_FILE_MODE
    )

    def _rel(p: Path) -> str:
        try:
            return str(p.resolve().relative_to(REPO_ROOT))
        except ValueError:
            return str(p)

    bundle_rel = _rel(bundle_path)
    manifest_rel = _rel(Path(args.manifest))
    pair_id = sem.evidence_pair_id(decision)

    # Strict offline replay against the real immutable caches (read-only).
    # The pre-manifest carries the bundle's exact FILE hash so replay verifies
    # the written file bytes (item 12), not only the canonical object hash.
    replay_proof = sem.strict_offline_replay(
        bundle=bundle,
        decision=decision,
        golden_path=golden_path,
        cache_dir=cache_dir,
        manifest={
            "decision_deterministic_core_sha256": bundle["decision_deterministic_core_sha256"],
            "candidate_grid_hash": bundle["candidate_grid_hash"],
            "decision_policy_hash": bundle["decision_policy_hash"],
            "eval3_cohort_manifest_hash": bundle["eval3_cohort_manifest_hash"],
            "accounting_sha256": bundle["accounting_sha256"],
            "golden": bundle["golden"],
            "immutable_caches": bundle["immutable_caches"],
            "eval3_cache_record_count": bundle["eval3_cache_record_count"],
            "replay_bundle": {
                "version": bundle["bundle_version"],
                "file_sha256": bundle_file_sha256,
                "canonical_sha256": sem._sha256_json(bundle),
            },
        },
        golden_query_tokens=int(golden_qe["estimated_input_tokens"]),
        bundle_path=str(bundle_path),
    )

    # Attach the evidence-replay summary to the tracked decision JSON, including
    # the shared evidence-pair ID (derived from the deterministic core, NOT from
    # any file bytes, so there is no recursive self-hashing). The pair ID is also
    # set at the decision top level so verify_evidence_pair can bind it.
    decision["evidence_pair_id"] = pair_id
    decision["evidence_replay"] = {
        "mode": "strict-offline-evidence-cache-replay",
        "evidence_pair_id": pair_id,
        "replay_bundle": {
            "version": bundle["bundle_version"],
            "path": bundle_rel,
            "file_sha256": bundle_file_sha256,
            "canonical_sha256": sem._sha256_json(bundle),
        },
        "frozen_manifest_path": manifest_rel,
        "strict_offline_replay": replay_proof,
        "limitation": bundle["limitation"],
    }
    decision["generated_at"] = DETERMINISTIC_TS

    # Write the decision JSON (atomic, 0644) + capture exact file bytes hash.
    if sem.scan_envelope(decision):
        raise RuntimeError("decision hygiene violation before write")
    decision_json_text = _dumps(decision)
    decision_json_sha = _write_tracked(Path(args.decision_json), decision_json_text)

    # Write the decision Markdown (atomic, 0644) with the shared pair ID embedded.
    md = render_markdown(decision)
    if sem.scan_envelope(md):
        raise RuntimeError("markdown hygiene violation before write")
    if pair_id not in md:
        raise RuntimeError("decision markdown does not carry the evidence pair id")
    decision_md_sha = _write_tracked(Path(args.decision_md), md + "\n")

    # The manifest is published LAST: it binds the exact published JSON + Markdown
    # byte hashes, the shared pair ID, and the bundle file-byte hash. It does NOT
    # hash itself, so publication order removes any recursive self-hashing.
    manifest = sem.build_frozen_manifest(
        decision, golden_path=golden_path, cache_dir=cache_dir, bundle=bundle,
        replay_proof=replay_proof, bundle_path=bundle_rel,
        bundle_file_sha256=bundle_file_sha256,
        evidence_pair={
            "id": pair_id,
            "decision_json": {"path": _rel(Path(args.decision_json)), "sha256": decision_json_sha},
            "decision_md": {"path": _rel(Path(args.decision_md)), "sha256": decision_md_sha},
            "note": (
                "shared canonical pair ID (from the decision deterministic core) + "
                "exact SHA-256 of the published JSON and Markdown file bytes; the "
                "manifest is published last so these bind the pair without recursion"
            ),
        },
    )
    _write_json(Path(args.manifest), manifest)

    print(json.dumps({
        "winner": decision["winner"]["name"],
        "replay_zero_provider_calls": replay_proof["zero_provider_calls"],
        "replay_checks": replay_proof["n_checks"],
        "eval3_total_cost_usd": decision["accounting"]["eval3_actual_incremental_bakeoff"]["total_cost_usd"],
        "historical_reconstructed_estimate_usd": decision["accounting"]["historical_duplicate_attempts"]["conservative_reconstructed_estimate_usd"],
        "evidence_pair_id": pair_id,
        "bundle_file_sha256": bundle_file_sha256,
        "decision_json_sha256": decision_json_sha,
        "decision_md_sha256": decision_md_sha,
        "bundle": str(bundle_path),
        "manifest": str(args.manifest),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
