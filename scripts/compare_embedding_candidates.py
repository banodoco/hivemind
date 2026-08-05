#!/usr/bin/env python3
"""Compare the four embedding-candidate contracts (plan task 2.14).

Evaluation-only CLI. It is **not** the production semantic path and never
mutates production. Four clearly separated modes:

* ``offline``  — deterministic fixture mechanics (synthetic corpus, fake
  embedder). No network, no provider, fully reproducible. Used by tests and to
  prove byte-stable decision output.
* ``freeze``   — live **read-only** corpus snapshot + preflight. Reads
  distillations / resources / a deterministic message sample (including every
  golden message identity) over the public PostgREST anon path, builds the
  chunked cohort, projects spend/storage, and writes a sanitized frozen
  manifest. No provider call.
* ``evaluate`` — the real bounded OpenAI evaluation (cache-first) of all four
  candidates, within the fixed cost cap. Writes the sanitized decision JSON + MD.
* ``replay``   — strict cache-only replay. Fails closed on any miss and proves
  zero provider calls with the same rankings/metrics/winner.

Secret boundary: the approved OpenAI key is read ONLY via the established
:func:`scripts.smoke_embedding_provider.load_approved_key` path (env var or one
approved env file). It is never printed, logged, or persisted. Tracked evidence
is scanned by :func:`eval.retrieval.semantic.scan_envelope` for vectors, keys,
URLs, and raw workflow-Python markers before it is written.

Examples::

    # offline mechanics (no network)
    python3 scripts/compare_embedding_candidates.py offline --out-dir /tmp/t214

    # freeze the real read-only snapshot + manifest (no provider call)
    python3 scripts/compare_embedding_candidates.py freeze \\
        --cache-dir .cache/hivemind-semantic-eval \\
        --manifest docs/hybrid-search/task-2.14-frozen-manifest.json

    # real bounded evaluation (approved key)
    python3 scripts/compare_embedding_candidates.py evaluate \\
        --key-source-env <approved .env> \\
        --cache-dir .cache/hivemind-semantic-eval

    # strict cache-only replay (zero provider calls)
    python3 scripts/compare_embedding_candidates.py replay \\
        --cache-dir .cache/hivemind-semantic-eval
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.retrieval import semantic as sem  # noqa: E402
from eval.retrieval.loader import load_corpus, load_golden_set  # noqa: E402
from eval.retrieval.schema import CorpusItem  # noqa: E402
from executors import embedding_contract as ec  # noqa: E402
from executors import entity_identity as ei  # noqa: E402

DEFAULT_GOLDEN = REPO_ROOT / "eval" / "retrieval" / "golden" / "golden-v1.json"
DEFAULT_FIXTURE_CORPUS = REPO_ROOT / "eval" / "retrieval" / "golden" / "corpus-v1.json"
DEFAULT_MANIFEST = REPO_ROOT / "docs" / "hybrid-search" / "task-2.14-frozen-manifest.json"
#: Distinct freeze-INPUT manifest (live read-only snapshot + preflight). This is
#: NOT the final evidence manifest; keeping them separate prevents the freeze
#: input from colliding with / overwriting the tracked final evidence manifest.
DEFAULT_FREEZE_INPUT_MANIFEST = REPO_ROOT / "docs" / "hybrid-search" / "task-2.14-freeze-input-manifest.json"
DEFAULT_DECISION_JSON = REPO_ROOT / "docs" / "hybrid-search" / "task-2.14-embedding-decision.json"
DEFAULT_DECISION_MD = REPO_ROOT / "docs" / "hybrid-search" / "task-2.14-embedding-decision.md"

#: Public anon PostgREST read path (read-only). The project ref is public.
DEFAULT_ENDPOINT = "https://ujlwuvkrxlvoswwkerdf.supabase.co/rest/v1"
DEFAULT_ANON_KEY = "sb_publishable_O38oPBafrBoFrpi_rlWJvA_UJrulFsx"

#: Deterministic message-sample size at the frozen high-water boundary.
MESSAGE_SAMPLE_SIZE = 5000


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _postgrest_get(
    endpoint: str, anon_key: str, path: str, *, timeout: float = 60.0
) -> Any:
    url = f"{endpoint.rstrip('/')}/{path.lstrip('/')}"
    req = urllib.request.Request(
        url,
        headers={
            "apikey": anon_key,
            "Authorization": f"Bearer {anon_key}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"PostgREST HTTP {exc.code} for {path}") from exc
    return json.loads(body)


def _load_approved_key(env_path: str | None) -> str | None:
    """Reuse the established one-key reader (task 2.1). Never prints the value."""

    spec = urllib.request  # placeholder to satisfy lint of unused import path
    import importlib.util

    spec_path = REPO_ROOT / "scripts" / "smoke_embedding_provider.py"
    mod = importlib.util.spec_from_file_location("_smoke", spec_path)
    assert mod is not None and mod.loader is not None
    smoke = importlib.util.module_from_spec(mod)
    mod.loader.exec_module(smoke)
    key, _src = smoke.load_approved_key(env_path)
    return key


# ---------------------------------------------------------------------------
# Cohort construction
# ---------------------------------------------------------------------------


def _corpus_item_from_message(row: dict[str, Any]) -> CorpusItem:
    mid = str(row["message_id"])
    return CorpusItem(
        kind="message",
        source="messages",
        item_id=mid,
        title="",
        body=str(row.get("content") or ""),
        author=str(row.get("author_name")) if row.get("author_name") else None,
        context=str(row.get("channel_name")) if row.get("channel_name") else None,
        created_at=str(row.get("created_at")) if row.get("created_at") else None,
        metadata={},
    )


def _cohort_entity_from_corpus_item(item: CorpusItem) -> sem.CohortEntity:
    """Derive a canon row from a CorpusItem (offline fixture path)."""
    ek = item.entity_kind()
    if ek == "message":
        canon_row = {"content": item.body}
        entity_type = ei.ENTITY_MESSAGE
    elif ek == "distillation":
        canon_row = {"question": item.title, "answer": item.body}
        entity_type = ei.ENTITY_DISTILLATION
    else:
        canon_row = {
            "kind": item.kind,
            "title": item.title,
            "body": item.body,
            "metadata": item.metadata or {},
        }
        entity_type = ei.ENTITY_RESOURCE
    return sem.CohortEntity(
        entity_type=entity_type,
        item_id=item.item_id,
        corpus_item=item,
        canon_row=canon_row,
    )


def cohort_entities_from_corpus(items: Iterable[CorpusItem]) -> list[sem.CohortEntity]:
    return [_cohort_entity_from_corpus_item(it) for it in items]


# ---------------------------------------------------------------------------
# Live read-only cohort reader (freeze only; no provider call)
# ---------------------------------------------------------------------------


def _golden_message_ids(golden_path: Path) -> set[str]:
    golden = load_golden_set(golden_path)
    out: set[str] = set()
    for case in golden.cases:
        for j in case.expected:
            if j.key()[0] == "message":
                out.add(j.item_id)
    return out


def _paginate_keyset(
    endpoint: str,
    anon_key: str,
    table: str,
    select: str,
    order_col: str,
    n: int,
    *,
    extra_filter: str = "",
    page: int = 1000,
) -> list[dict[str, Any]]:
    """Keyset-paginate ``order_col DESC`` to fetch *n* rows (indexed PK friendly)."""
    rows: list[dict[str, Any]] = []
    last_id: str | None = None
    while len(rows) < n:
        parts = [f"select={urllib.parse.quote(select)}", f"order={order_col}.desc", f"limit={page}"]
        flt = extra_filter
        if last_id is not None:
            conj = "&" if flt else ""
            flt = f"{flt}{conj}{order_col}=lt.{last_id}"
        if flt:
            parts.append(flt)
        batch = _postgrest_get(endpoint, anon_key, f"/{table}?{'&'.join(parts)}")
        if not batch:
            break
        rows.extend(batch)
        last_id = str(batch[-1][order_col])
        if len(batch) < page:
            break
    return rows[:n]


def read_real_cohort_entities(
    *,
    endpoint: str,
    anon_key: str,
    golden_path: Path,
    message_sample_size: int = MESSAGE_SAMPLE_SIZE,
) -> tuple[list[sem.CohortEntity], dict[str, Any]]:
    """Read the frozen real read-only cohort (no provider call).

    Returns (entities, sanitized_counts). Snowflakes stay exact strings. Every
    judged golden identity is verified present (a missing judged identity is a
    disqualifier downstream; recorded here as a freeze integrity flag).
    """

    counts: dict[str, Any] = {"sources": {}, "high_water": {}}
    entities: list[sem.CohortEntity] = []

    # --- distillations (eligible: status in pending/approved) ---
    dist_rows = _postgrest_get(
        endpoint,
        anon_key,
        "/distillations?select=id,question,conditions,answer,status,confidence,created_at,author_id"
        "&status=in.(pending,approved)&order=id.asc",
    )
    dist_status: dict[str, int] = {}
    for r in dist_rows:
        dist_status[r.get("status", "?")] = dist_status.get(r.get("status", "?"), 0) + 1
        item = CorpusItem(
            kind="distillation",
            source="distillations",
            item_id=str(r["id"]),
            title=str(r.get("question") or ""),
            body=str(r.get("answer") or ""),
            author=str(r.get("author_id")) if r.get("author_id") else None,
            created_at=str(r.get("created_at")) if r.get("created_at") else None,
            status=str(r.get("status")) if r.get("status") else None,
            metadata={},
        )
        canon_row = {
            "question": r.get("question"),
            "conditions": r.get("conditions"),
            "answer": r.get("answer"),
            "status": r.get("status"),
        }
        entities.append(
            sem.CohortEntity(
                entity_type=ei.ENTITY_DISTILLATION,
                item_id=str(r["id"]),
                corpus_item=item,
                canon_row=canon_row,
            )
        )
    counts["sources"]["distillations"] = {"rows": len(dist_rows), "by_status": dist_status}

    # --- external_resources (all kinds; resources + workflows) ---
    res_rows = _paginate_keyset(
        endpoint, anon_key, "external_resources",
        "id,kind,source,title,body,metadata,payload,author,created_at,url",
        "id", n=10_000, page=1000,
    )
    res_kind: dict[str, int] = {}
    malformed_resources_skipped = 0
    for r in res_rows:
        res_kind[r.get("kind", "?")] = res_kind.get(r.get("kind", "?"), 0) + 1
        # Defensive: a source row whose kind is outside the citation vocabulary
        # (e.g. an upstream typo) is skipped, not coerced — never indexed as a
        # valid entity. Recorded in counts; never a judged golden identity here.
        from eval.retrieval.schema import ALLOWED_KINDS

        if str(r.get("kind") or "") not in ALLOWED_KINDS:
            malformed_resources_skipped += 1
            continue
        item = CorpusItem(
            kind=str(r.get("kind") or "resource"),
            source=str(r.get("source") or ""),
            item_id=str(r["id"]),
            title=str(r.get("title") or ""),
            body=str(r.get("body") or ""),
            author=str(r.get("author")) if r.get("author") else None,
            context=None,
            created_at=str(r.get("created_at")) if r.get("created_at") else None,
            url=str(r.get("url")) if r.get("url") else None,
            metadata=r.get("metadata") if isinstance(r.get("metadata"), dict) else {},
        )
        canon_row = {
            "kind": r.get("kind"),
            "title": r.get("title"),
            "body": r.get("body"),
            "metadata": r.get("metadata") if isinstance(r.get("metadata"), dict) else {},
            "payload": r.get("payload") if isinstance(r.get("payload"), dict) else {},
        }
        entities.append(
            sem.CohortEntity(
                entity_type=ei.ENTITY_RESOURCE,
                item_id=str(r["id"]),
                corpus_item=item,
                canon_row=canon_row,
            )
        )
    counts["sources"]["external_resources"] = {
        "rows": len(res_rows),
        "by_kind": res_kind,
        "malformed_kind_skipped": malformed_resources_skipped,
    }

    # --- messages: deterministic sample at frozen high-water boundary ---
    golden_msg_ids = _golden_message_ids(golden_path)
    top_rows = _paginate_keyset(
        endpoint, anon_key, "message_feed",
        "message_id,content,author_name,channel_name,created_at",
        "message_id", n=message_sample_size, page=1000,
    )
    top_ids = {str(r["message_id"]) for r in top_rows}
    high_water = top_rows[0]["message_id"] if top_rows else None
    counts["high_water"]["message_id"] = str(high_water) if high_water else None
    counts["high_water"]["message_sample_size_target"] = message_sample_size

    missing_golden = golden_msg_ids - top_ids
    extra_rows: list[dict[str, Any]] = []
    if missing_golden:
        for mid in sorted(missing_golden):
            try:
                got = _postgrest_get(
                    endpoint, anon_key,
                    f"/message_feed?message_id=eq.{mid}"
                    "&select=message_id,content,author_name,channel_name,created_at",
                )
            except RuntimeError:
                got = []
            if got:
                extra_rows.extend(got)
    # Merge: include golden rows; if extra pushes over the cap, drop the lowest-id
    # non-golden rows deterministically to keep exactly message_sample_size.
    merged: dict[str, dict[str, Any]] = {str(r["message_id"]): r for r in top_rows}
    for r in extra_rows:
        merged[str(r["message_id"])] = r
    if len(merged) > message_sample_size:
        non_golden = sorted((mid for mid in merged if mid not in golden_msg_ids))
        drop = len(merged) - message_sample_size
        for mid in non_golden[:drop]:
            merged.pop(mid, None)
    msg_rows = list(merged.values())
    for r in msg_rows:
        item = _corpus_item_from_message(r)
        entities.append(
            sem.CohortEntity(
                entity_type=ei.ENTITY_MESSAGE,
                item_id=item.item_id,
                corpus_item=item,
                canon_row={"content": r.get("content")},
            )
        )
    counts["sources"]["messages"] = {
        "sample_size": len(msg_rows),
        "golden_message_ids_required": len(golden_msg_ids),
        "golden_message_ids_present": len(golden_msg_ids & {str(r["message_id"]) for r in msg_rows}),
        "deterministic_rule": (
            "top-N by message_id desc from frozen high-water, unioned with every "
            "golden message identity, lowest non-golden ids dropped to hold N"
        ),
    }

    # Integrity: every judged golden identity must be present.
    golden = load_golden_set(golden_path)
    judged_keys = set()
    for case in golden.cases:
        for j in case.expected:
            if j.grade >= 1:
                judged_keys.add(j.key())
    present = {e.entity_key for e in entities}
    missing_judged = sorted(k for k in judged_keys if k not in present)
    counts["integrity"] = {
        "judged_identities_required": len(judged_keys),
        "judged_identities_present": len(judged_keys) - len(missing_judged),
        "missing_judged_identities": len(missing_judged),
    }
    return entities, counts


# ---------------------------------------------------------------------------
# Cohort preflight (cost + storage projection) for the frozen manifest
# ---------------------------------------------------------------------------


def _preflight(cohorts: dict[sem.Candidate, sem.ChunkedCohort]) -> dict[str, Any]:
    """Project provider spend (small-config tokens × all 4) + storage per dim."""
    out: dict[str, Any] = {"per_candidate": {}, "aggregate": {}}
    total_tokens = 0
    for cand in sem.CANDIDATES:
        cohort = cohorts[cand]
        toks = sum(max(1, _est_tokens(t)) for t in cohort.unique_texts.values())
        cost = toks / 1_000_000.0 * sem.PRICE_PER_1M_TOKENS_USD
        total_tokens += toks
        cap = sem.CAPACITY_FACTS[cand.dimension]
        out["per_candidate"][cand.name] = {
            "dimension": cand.dimension,
            "n_unique_embeddable_texts": cohort.n_unique_embeddable_texts,
            "projected_input_tokens": toks,
            "projected_cost_usd": round(cost, 6),
            "full_corpus_storage_gb": cap["full_corpus_storage_gb"],
            "capacity_verdict": cap["verdict"],
        }
    total_cost = total_tokens / 1_000_000.0 * sem.PRICE_PER_1M_TOKENS_USD
    out["aggregate"] = {
        "projected_input_tokens_all_candidates": total_tokens,
        "projected_cost_usd_all_candidates": round(total_cost, 6),
        "spend_cap_usd": sem.SPEND_CAP_USD,
        "within_spend_cap": total_cost <= sem.SPEND_CAP_USD,
        "storage_gate_gb": sem.STORAGE_GATE_GB,
        "price_assumption": f"${sem.PRICE_PER_1M_TOKENS_USD}/1M input tokens (text-embedding-3-small, any dimension)",
    }
    return out


def _est_tokens(text: str) -> int:
    from executors import workflow_representation as wr

    return wr.estimate_tokens(text)


# ---------------------------------------------------------------------------
# Evaluation drivers (shared by offline / evaluate / replay)
# ---------------------------------------------------------------------------


def _offline_embed_fn(dimension: int) -> Callable[[Sequence[str]], "list[list[float]]"]:
    """Deterministic fake embedder wrapped as a sync batch callable (no network)."""
    fake = ec.DeterministicFakeEmbedder(dimension=dimension)

    def _fn(texts: Sequence[str]) -> list[list[float]]:
        return asyncio.run(fake.embed_texts(list(texts)))

    return _fn


def _run_all_candidates(
    cohorts: dict[sem.Candidate, sem.ChunkedCohort],
    golden_path: Path,
    *,
    cache_dir: Path,
    api_key: str | None,
    offline: bool,
    replay_only: bool,
    ks: tuple[int, ...] = (1, 5, 10),
    progress: Callable[[str], None] | None = None,
) -> list[sem.CandidateMetrics]:
    golden = load_golden_set(golden_path)
    # Gap 4: ONE shared aggregate actual-usage guard across ALL real-path
    # candidates, so the hard cap does NOT reset between candidates/dimensions.
    # Offline/fake-embedder clients are zero-cost and never gated (guard=None);
    # replay_only has a complete cache (no misses) and never invokes a transport.
    aggregate_guard = None if offline else sem.AggregateUsageGuard(cap_usd=sem.SPEND_CAP_USD)
    results: list[sem.CandidateMetrics] = []
    for cand in sem.CANDIDATES:
        cohort = cohorts[cand]
        cache_path = cache_dir / f"cache.{cand.dimension}.jsonl"
        cache = sem.EmbeddingCache(cache_path, fail_closed=replay_only)
        if offline:
            embed_fn = _offline_embed_fn(cand.dimension)
            client = sem.ProviderClient(
                candidate=cand, cache=cache, api_key=None, embed_fn=embed_fn
            )
        else:
            transport = sem._ReplayTransport() if replay_only else None
            client = sem.ProviderClient(
                candidate=cand,
                cache=cache,
                api_key=api_key,
                transport=transport,
                replay_only=replay_only,
                aggregate_guard=aggregate_guard,
            )
        if progress:
            progress(f"evaluating {cand.name} (dim={cand.dimension}, chunks={cohort.n_chunks})")
        m = sem.evaluate_candidate(cand, cohort, golden, client, ks=ks)
        results.append(m)
        if replay_only:
            replay_calls = getattr(client.transport, "calls", 0) if client.transport else 0
            m.account["provider_calls_replay"] = replay_calls
        cache.compact()
    return results


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Evidence hygiene: scan before writing tracked artifacts.
    violations = sem.scan_envelope(obj)
    if violations:
        raise RuntimeError(f"evidence hygiene violations {violations} writing {path}")
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=False) + "\n", encoding="utf-8")


def render_markdown(envelope: dict[str, Any], *, mode: str, cohort_counts: dict[str, Any], preflight: dict[str, Any] | None) -> str:
    lines: list[str] = []
    A = lines.append
    A("# Task 2.14 — Embedding Dimension & Chunk-Configuration Decision")
    A("")
    A(f"- Mode: `{mode}`")
    A("- Exact-cosine local evaluation (numpy). **Not** HNSW/SQL/Edge latency.")
    A("- Golden set: `golden/2026-07-28/v1` (112 cases: 104 judged, 8 no-hit, 141 judgments).")
    A("- Capacity facts frozen from task 0.7: 384-d full corpus 4.59 GB (PASS); 1536-d 16.4 GB (FAIL the 12 GB gate).")
    A("")
    cands = envelope["candidates"]
    A("## Candidate metrics")
    A("")
    A("| Candidate | Dim | recall@10 | MRR | nDCG@10 | wf_code R@10 | later-chunk R@10 | cost $ | eligible |")
    A("|---|---|---|---|---|---|---|---|---|")
    for c in cands:
        A(
            f"| {c['candidate']['name']} | {c['candidate']['dimension']} | "
            f"{c['overall'].get('recall@10', 0):.4f} | {c['overall'].get('mrr', 0):.4f} | "
            f"{c['overall'].get('ndcg@10', 0):.4f} | {c['workflow_code_recall_at_10']:.4f} | "
            f"{c['long_resource_chunk_recall_at_10']:.4f} | {c['provider_account'].get('cost_usd', 0):.4f} | "
            f"{'yes' if c['eligible_for_selection'] else 'no (' + ','.join(c['disqualify_reasons']) + ')'} |"
        )
    A("")
    w = envelope.get("winner")
    A("## Decision")
    A("")
    if w:
        A(f"- **Selected production dimension:** `{w['dimension']}`")
        A(f"- **Prose chunk contract:** target `{w['prose']['target_tokens']}` / overlap `{w['prose']['overlap_tokens']}` tokens")
        A(f"- **Workflow-Python chunk contract:** target `{w['python']['target_tokens']}` / overlap `{w['python']['overlap_tokens']}` tokens")
        A(f"- **Selection rationale:** {envelope['winner_rationale'].get('selection_key_used')}")
        A(f"- **Eligible ranking:** {envelope['winner_rationale'].get('eligible_ranking')}")
    else:
        A("- No eligible candidate (see disqualify reasons).")
    A("")
    if preflight:
        A("## Preflight (frozen, pre-call)")
        A("")
        A(f"- Projected spend (all 4 candidates): `${preflight['aggregate']['projected_cost_usd_all_candidates']}` "
          f"of `${preflight['aggregate']['spend_cap_usd']}` cap — "
          f"within cap: `{preflight['aggregate']['within_spend_cap']}`")
        A(f"- Price assumption: {preflight['aggregate']['price_assumption']}")
        A("")
    A("## Provider accounting (per candidate)")
    A("")
    A("| Candidate | requests | inputs | tokens | cache hits | cache misses |")
    A("|---|---|---|---|---|---|")
    for c in cands:
        a = c["provider_account"]
        A(f"| {c['candidate']['name']} | {a.get('api_requests',0)} | {a.get('embedded_inputs',0)} | "
          f"{a.get('input_tokens_from_usage',0)} | {a.get('cache_hits',0)} | {a.get('cache_misses',0)} |")
    A("")
    A("## Honest limitations")
    A("")
    A("- Ranking is **local exact cosine** over the frozen cohort — not HNSW/SQL/Edge.")
    A("- The 1536-d dimension is evaluated for quality but **capacity-disqualified** (16.4 GB > 12 GB gate).")
    A("- Cohort is a frozen read-only snapshot; production has no vector/schema/worker deployment yet.")
    A("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI subcommands
# ---------------------------------------------------------------------------


def _build_cohorts(entities: list[sem.CohortEntity]) -> dict[sem.Candidate, sem.ChunkedCohort]:
    return {cand: sem.build_chunked_cohort(entities, cand) for cand in sem.CANDIDATES}


# ---------------------------------------------------------------------------
# Future authorized freeze architecture: raw private frozen-snapshot binding
# ---------------------------------------------------------------------------
#
# The eval3 run did NOT persist a raw frozen snapshot before the provider calls,
# which is why exact-cosine retrieval cannot be recomputed offline from raw text
# (see the task-2.14 correction limitation). The architecture below is the
# correct future shape: an authorized ``freeze`` writes the raw private frozen
# inputs (cohort rows + per-candidate chunk texts + identity->chunk map) to a
# private, gitignored location BEFORE any provider call, and records only the
# snapshot's SHA-256 in the tracked manifest. ``evaluate`` and ``replay`` then
# load that exact snapshot and fail closed on a hash mismatch. This network
# freeze path is NOT executed in this offline correction; only the binding
# contract is implemented here.

RAW_FROZEN_SNAPSHOT_VERSION = 2

# Gap 3: a DISTINCT, versioned freeze-INPUT manifest contract (separate from the
# final evidence manifest). A snapshot binding is incomplete without it: the
# future snapshot-driven ``evaluate`` rebuilds cohorts from the snapshot with the
# CURRENT chunker/canonicalization/config, so every freeze-time fact bound here
# must be recomputed and compared before any provider/offline-embedding call, or
# chunker/canonicalization/config drift goes undetected.
FREEZE_INPUT_MANIFEST_KIND = "task-2.14-embedding-decision-freeze-input"
FREEZE_INPUT_CONTRACT_VERSION = 1


def _sha256_json(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def freeze_input_candidate_facts(cohorts: Mapping[sem.Candidate, sem.ChunkedCohort]) -> dict[str, Any]:
    """Per-candidate chunk-map identity/hash/count facts (gap 3).

    Each candidate records the exact chunk-map (entity_kind, item_id,
    representation_type, chunk_index, chunk_hash, representation_hash) plus
    counts and the unique-embeddable-texts identity. The chunk_map hash detects
    chunker drift (chunk_index / chunk_hash) and canonicalization drift
    (representation_hash); the unique-texts hash detects canonicalization/text
    drift independently of the map. Counts alone would NOT detect drift, so the
    full identity map is hashed.
    """

    per: dict[str, Any] = {}
    for cand in sem.CANDIDATES:
        cohort = cohorts[cand]
        chunk_map = [
            {
                "entity_kind": ch.entity_kind,
                "item_id": ch.item_id,
                "representation_type": ch.representation_type,
                "chunk_index": ch.chunk_index,
                "chunk_hash": ch.chunk_hash,
                "representation_hash": ch.representation_hash,
            }
            for ch in cohort.chunks
        ]
        per[cand.name] = {
            "dimension": cand.dimension,
            "eval_contract_id": cand.eval_contract_id,
            "n_chunks": cohort.n_chunks,
            "n_unique_embeddable_texts": cohort.n_unique_embeddable_texts,
            "n_entities": cohort.n_entities,
            "chunk_map_sha256": _sha256_json(chunk_map),
            "unique_texts_sha256": _sha256_json(sorted(cohort.unique_texts.keys())),
        }
    return per


def freeze_input_entity_facts(entities: Sequence[sem.CohortEntity]) -> dict[str, Any]:
    """Deterministic cohort/entity identity facts (gap 3).

    The full ordered set of entity keys is hashed (plus per-kind counts) so any
    added/dropped/reordered entity is detected when the snapshot is rebuilt.
    """

    by_kind: dict[str, int] = {}
    for e in entities:
        kind = e.entity_key[0]
        by_kind[kind] = by_kind.get(kind, 0) + 1
    keys = sorted(e.entity_key for e in entities)
    return {
        "n_entities": len(entities),
        "entity_keys_sha256": _sha256_json(keys),
        "entities_by_kind": dict(sorted(by_kind.items())),
    }


def build_freeze_input_contract(
    entities: Sequence[sem.CohortEntity],
    cohorts: Mapping[sem.Candidate, sem.ChunkedCohort],
    *,
    golden_path: Path,
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """The complete freeze-input contract block embedded in the freeze manifest.

    Binds: manifest kind/version; the fixed golden SHA literal + facts; the
    candidate-grid hash + frozen decision-policy hash; the raw snapshot exact
    content hash/mode/parent-mode; the full cohort/entity identity facts; and the
    exact per-candidate chunk-map identity/hash/count facts. This is the contract
    ``verify_frozen_snapshot_binding`` requires and ``cmd_evaluate`` re-verifies
    after rebuilding entities/cohorts from the snapshot.
    """

    golden = sem.golden_file_facts(golden_path)
    if golden["sha256"] != sem.GOLDEN_SHA256:
        raise RuntimeError(
            f"freeze golden sha256 {golden['sha256']} != fixed literal {sem.GOLDEN_SHA256}"
        )
    policy = sem.sanitize_report([], None)["decision_policy"]
    return {
        "kind": FREEZE_INPUT_MANIFEST_KIND,
        "freeze_input_contract_version": FREEZE_INPUT_CONTRACT_VERSION,
        "golden": golden,
        "candidate_grid_hash": sem.candidate_grid_hash(),
        "decision_policy_hash": sem.decision_policy_hash(policy),
        "cohort_entity_facts": freeze_input_entity_facts(entities),
        "per_candidate_chunk_facts": freeze_input_candidate_facts(cohorts),
        "raw_frozen_snapshot": {
            "version": snapshot["version"],
            "path": snapshot["path"],
            "content_sha256": snapshot["content_sha256"],
            "n_entities": snapshot["n_entities"],
            "mode": oct(sem.PRIVATE_FILE_MODE),
            "parent_mode": oct(sem.PRIVATE_DIR_MODE),
            "note": (
                "raw private frozen inputs written before any provider call; the "
                "snapshot hash + private file/parent modes are bound here and "
                "verified fail-closed; chunk/entity facts are recomputed and "
                "compared after the snapshot is rebuilt"
            ),
        },
    }


def _require_freeze_input_contract(manifest: Mapping[str, Any], *, golden_path: Path | None) -> None:
    """Reject an incomplete snapshot-only manifest (gap 3).

    A manifest that carries ``raw_frozen_snapshot`` MUST also carry the complete
    versioned freeze-input contract (kind/version/golden/grid/policy/entity/
    chunk-map facts). Raises before any provider call otherwise.
    """

    if manifest.get("kind") != FREEZE_INPUT_MANIFEST_KIND:
        raise RuntimeError(
            f"incomplete freeze-input manifest: kind must be "
            f"{FREEZE_INPUT_MANIFEST_KIND!r} (snapshot-only manifest rejected)"
        )
    if manifest.get("freeze_input_contract_version") != FREEZE_INPUT_CONTRACT_VERSION:
        raise RuntimeError(
            f"incomplete freeze-input manifest: contract version must be "
            f"{FREEZE_INPUT_CONTRACT_VERSION} (got {manifest.get('freeze_input_contract_version')!r})"
        )
    required = (
        "golden", "candidate_grid_hash", "decision_policy_hash",
        "cohort_entity_facts", "per_candidate_chunk_facts", "raw_frozen_snapshot",
    )
    for key in required:
        if key not in manifest:
            raise RuntimeError(
                f"incomplete freeze-input manifest: missing required contract field "
                f"{key!r} (snapshot-only manifest rejected)"
            )
    mg = manifest["golden"]
    if mg.get("sha256") != sem.GOLDEN_SHA256:
        raise RuntimeError("freeze-input manifest golden sha256 != fixed literal")
    if manifest["candidate_grid_hash"] != sem.candidate_grid_hash():
        raise RuntimeError("freeze-input manifest candidate_grid_hash drift")
    policy = sem.sanitize_report([], None)["decision_policy"]
    if manifest["decision_policy_hash"] != sem.decision_policy_hash(policy):
        raise RuntimeError("freeze-input manifest decision_policy_hash drift")
    if golden_path is not None:
        actual_g = sem.golden_file_facts(golden_path)
        if actual_g["sha256"] != sem.GOLDEN_SHA256:
            raise RuntimeError("actual golden file sha256 != fixed literal")
        if actual_g["sha256"] != mg["sha256"]:
            raise RuntimeError("actual golden file != manifest golden binding")


def verify_rebuilt_freeze_facts(
    manifest: Mapping[str, Any],
    cohorts: Mapping[sem.Candidate, sem.ChunkedCohort],
    entities: Sequence[sem.CohortEntity],
) -> None:
    """Recompute and compare every bound chunk/entity fact after rebuild (gap 3).

    Called by ``cmd_evaluate`` after reconstructing entities/cohorts from the
    snapshot with the CURRENT chunker/canonicalization/config. Any chunk-map
    hash/count/identity drift, entity drift, or canonicalization drift fails
    closed BEFORE any provider or offline-embedding call.
    """

    per = freeze_input_candidate_facts(cohorts)
    ent = freeze_input_entity_facts(entities)
    if per != manifest["per_candidate_chunk_facts"]:
        raise RuntimeError(
            "rebuilt per-candidate chunk facts drift from the freeze-input "
            "binding (chunker/canonicalization/config drift detected)"
        )
    if ent != manifest["cohort_entity_facts"]:
        raise RuntimeError(
            "rebuilt cohort entity facts drift from the freeze-input binding"
        )


def write_raw_frozen_snapshot(
    entities: list[sem.CohortEntity],
    cohorts: dict[sem.Candidate, sem.ChunkedCohort],
    out_path: Path,
) -> dict[str, Any]:
    """Write the raw private frozen inputs (PRIVATE/gitignored; never tracked).

    Persists ALL deterministic :class:`CorpusItem` state used downstream by cohort
    filtering and canonicalization — ``kind, source, item_id, title, body, author,
    context, url, metadata, created_at, status`` — plus the ``canon_row`` and
    ``entity_type`` each entity was built under, and each candidate's unique chunk
    texts (``chunk_hash -> text``) + identity->chunk map. This lets a future
    ``evaluate``/``replay`` recompute exact-cosine retrieval deterministically
    from raw text WITHOUT re-reading the live cohort.

    The file is mode 0600 under a private (0700) cache dir, written atomically and
    private from its first byte (see :func:`sem.atomic_write_text`). Only its
    SHA-256 (returned here) is ever recorded in tracked evidence. It contains raw
    content and is therefore PRIVATE — never committed, never hygiene-scanned as
    tracked evidence, never shipped.
    """

    import hashlib

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": RAW_FROZEN_SNAPSHOT_VERSION,
        "n_entities": len(entities),
        "entities": [
            {
                "entity_type": e.entity_type,
                "item_id": e.item_id,
                "entity_kind": e.corpus_item.entity_kind(),
                # Full deterministic CorpusItem state (filters + identity).
                "corpus_item": {
                    "kind": e.corpus_item.kind,
                    "source": e.corpus_item.source,
                    "item_id": e.corpus_item.item_id,
                    "title": e.corpus_item.title,
                    "body": e.corpus_item.body,
                    "author": e.corpus_item.author,
                    "context": e.corpus_item.context,
                    "url": e.corpus_item.url,
                    "metadata": e.corpus_item.metadata or {},
                    "created_at": e.corpus_item.created_at,
                    "status": e.corpus_item.status,
                },
                "canon_row": e.canon_row,
            }
            for e in entities
        ],
        "per_candidate": {
            cand.name: {
                "unique_texts": cohort.unique_texts,  # chunk_hash -> raw text
                "chunk_map": [
                    {
                        "entity_kind": ch.entity_kind,
                        "item_id": ch.item_id,
                        "representation_type": ch.representation_type,
                        "chunk_index": ch.chunk_index,
                        "chunk_hash": ch.chunk_hash,
                        "representation_hash": ch.representation_hash,
                    }
                    for ch in cohort.chunks
                ],
            }
            for cand, cohort in cohorts.items()
        },
    }
    # Preserve source-object insertion order.  Workflow prose v1 flattens
    # nested ``metadata.workflow_semantics`` mappings in their source order;
    # serializing the raw snapshot with ``sort_keys=True`` silently reordered
    # those mappings.  Loading such a snapshot then changed thousands of prose
    # chunk hashes even though the persisted chunk map/texts were exact, so the
    # snapshot failed its own rebuild binding.  The snapshot hash still binds
    # the exact bytes, and Python's JSON encoder preserves the deterministic
    # insertion order of the already-frozen row objects.
    blob = json.dumps(payload, sort_keys=False, ensure_ascii=False)
    content_sha256 = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    sem.atomic_write_text(out_path, blob, mode=sem.PRIVATE_FILE_MODE)
    return {
        "version": RAW_FROZEN_SNAPSHOT_VERSION,
        "path": str(out_path),
        "content_sha256": content_sha256,
        "n_entities": len(entities),
        "per_candidate_chunk_counts": {
            cand.name: cohort.n_chunks for cand, cohort in cohorts.items()
        },
    }


def load_raw_frozen_snapshot(path: Path) -> dict[str, Any]:
    """Load + structurally validate a raw private frozen snapshot (PRIVATE).

    Fails closed if the snapshot file or its direct parent dir has a permissive
    mode (file must be 0600; parent must be 0700).
    """

    sem.assert_private_file(path, mode=sem.PRIVATE_FILE_MODE)
    sem.assert_private_dir(Path(path).parent, mode=sem.PRIVATE_DIR_MODE)
    return json.loads(Path(path).read_text(encoding="utf-8"))


def entities_from_raw_snapshot(snapshot: Mapping[str, Any]) -> list[sem.CohortEntity]:
    """Reconstruct the exact :class:`CohortEntity` list from a verified snapshot.

    Rebuilds every :class:`CorpusItem` with its full deterministic filter/identity
    state (so cohort filtering + canonicalization are identical to the freeze) and
    re-attaches the original ``canon_row`` + ``entity_type``. Used by the future
    snapshot-driven ``evaluate``/raw replay so it never calls the live reader.
    """

    if int(snapshot.get("version", 0)) != RAW_FROZEN_SNAPSHOT_VERSION:
        raise RuntimeError(
            f"raw frozen snapshot version {snapshot.get('version')} != "
            f"required {RAW_FROZEN_SNAPSHOT_VERSION}"
        )
    out: list[sem.CohortEntity] = []
    for e in snapshot["entities"]:
        ci = e["corpus_item"]
        item = CorpusItem(
            kind=ci["kind"],
            source=ci.get("source", ""),
            item_id=str(ci["item_id"]),
            title=ci.get("title", "") or "",
            body=ci.get("body", "") or "",
            author=ci.get("author"),
            context=ci.get("context"),
            url=ci.get("url"),
            metadata=ci.get("metadata") if isinstance(ci.get("metadata"), dict) else {},
            created_at=ci.get("created_at"),
            status=ci.get("status"),
        )
        out.append(
            sem.CohortEntity(
                entity_type=e["entity_type"],
                item_id=str(e["item_id"]),
                corpus_item=item,
                canon_row=dict(e["canon_row"]),
            )
        )
    return out


def verify_frozen_snapshot_binding(
    manifest: Mapping[str, Any] | None,
    cache_dir: Path,
    *,
    repo_root: Path | None = None,
    golden_path: Path | None = None,
) -> dict[str, Any] | None:
    """Fail closed if a snapshot hash is recorded but the file is absent/changed.

    Returns the verified snapshot record, or ``None`` when no snapshot binding is
    declared (e.g. the offline fixture path, or the eval3 run that predates this
    architecture). Called by ``evaluate``/``replay`` so a future authorized run
    is bound to the exact frozen inputs the decision was made on.

    Gap 3: a manifest that declares ``raw_frozen_snapshot`` MUST carry the
    COMPLETE versioned freeze-input contract (kind/version/golden/grid/policy/
    entity/chunk-map facts). An incomplete snapshot-only manifest is REJECTED
    here, before any provider call.

    A repo-relative snapshot path (e.g.
    ``.cache/hivemind-semantic-eval/raw-frozen-snapshot.json``) is anchored at the
    repo root — NOT at ``cache_dir.parent`` (which would resolve to the bogus
    ``.cache/.cache/...`` double-cache path). Fails closed on a missing file, a
    hash mismatch, or a permissive file/parent mode.
    """

    if not manifest:
        return None
    snap = manifest.get("raw_frozen_snapshot")
    if not snap:
        return None
    # Gap 3: the complete freeze-input contract is MANDATORY for a snapshot
    # binding. An incomplete snapshot-only manifest fails closed here.
    _require_freeze_input_contract(manifest, golden_path=golden_path)
    path = sem.resolve_private_path(snap["path"], anchor=repo_root)
    sem.assert_private_file(path, mode=sem.PRIVATE_FILE_MODE)
    sem.assert_private_dir(path.parent, mode=sem.PRIVATE_DIR_MODE)
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != snap["content_sha256"]:
        raise RuntimeError(
            "frozen snapshot hash mismatch: binding is stale or tampered "
            f"({actual} != {snap['content_sha256']})"
        )
    return snap


def cmd_offline(args: argparse.Namespace) -> int:
    corpus = load_corpus(args.corpus)
    entities = cohort_entities_from_corpus(corpus.items)
    cohorts = _build_cohorts(entities)
    golden = load_golden_set(args.golden)
    cache_dir = Path(args.cache_dir)
    metrics = _run_all_candidates(
        cohorts, args.golden, cache_dir=cache_dir, api_key=None,
        offline=True, replay_only=False, ks=_parse_ks(args.ks),
    )
    winner = sem.select_winner(metrics)
    envelope = sem.sanitize_report(metrics, winner)
    envelope["mode"] = "offline"
    envelope["generated_at"] = args.generated_at or _utc_now_iso()
    envelope["golden_version"] = "golden/2026-07-28/v1"
    # Deterministic core (timestamps stripped) for byte-stability checks.
    envelope["deterministic_core"] = _deterministic_core(envelope)
    out = Path(args.out_dir)
    _write_json(out / "task-2.14-offline-decision.json", envelope)
    return 0


def _deterministic_core(envelope: dict[str, Any]) -> dict[str, Any]:
    """Subset of the envelope that must be byte-identical across offline runs."""
    return {
        "candidates": [
            {
                "name": c["candidate"]["name"],
                "dimension": c["candidate"]["dimension"],
                "overall": c["overall"],
                "workflow_code_recall_at_10": c["workflow_code_recall_at_10"],
                "long_resource_chunk_recall_at_10": c["long_resource_chunk_recall_at_10"],
                "eligible_for_selection": c["eligible_for_selection"],
                "disqualify_reasons": c["disqualify_reasons"],
            }
            for c in envelope["candidates"]
        ],
        "winner": envelope.get("winner", {}).get("name") if envelope.get("winner") else None,
        "winner_rationale": envelope.get("winner_rationale"),
    }


def cmd_freeze(args: argparse.Namespace) -> int:
    entities, counts = read_real_cohort_entities(
        endpoint=args.endpoint, anon_key=args.anon_key, golden_path=Path(args.golden),
        message_sample_size=args.message_sample,
    )
    cohorts = _build_cohorts(entities)
    preflight = _preflight(cohorts)
    # Future architecture: persist the raw private frozen inputs BEFORE any
    # provider call, and record only the snapshot SHA-256 in the tracked manifest.
    snapshot_path = Path(args.cache_dir) / "raw-frozen-snapshot.json"
    snapshot = write_raw_frozen_snapshot(entities, cohorts, snapshot_path)
    snapshot["path"] = ".cache/hivemind-semantic-eval/raw-frozen-snapshot.json"
    # Gap 3: the complete versioned freeze-input contract. evaluate/replay require
    # this (an incomplete snapshot-only manifest is rejected) and re-verify every
    # bound chunk/entity/golden/grid/policy fact after rebuilding from the snapshot.
    contract = build_freeze_input_contract(
        entities, cohorts, golden_path=Path(args.golden), snapshot=snapshot
    )
    manifest = {
        "task": "2.14-embedding-decision-freeze",
        "kind": FREEZE_INPUT_MANIFEST_KIND,
        "freeze_input_contract_version": FREEZE_INPUT_CONTRACT_VERSION,
        "frozen_at": args.generated_at or _utc_now_iso(),
        "golden_version": "golden/2026-07-28/v1",
        "read_access": "read-only public PostgREST anon path (no mutation, no provider call)",
        "candidate_grid": [c.to_sanitized_dict() for c in sem.CANDIDATES],
        "cohort_counts": counts,
        "per_candidate_chunk_counts": {
            cand.name: cohort.sanitized_counts() for cand, cohort in cohorts.items()
        },
        "preflight": preflight,
        "decision_policy": sem.sanitize_report([], None)["decision_policy"],
        # Contract fields verified by verify_frozen_snapshot_binding +
        # verify_rebuilt_freeze_facts (gap 3).
        "golden": contract["golden"],
        "candidate_grid_hash": contract["candidate_grid_hash"],
        "decision_policy_hash": contract["decision_policy_hash"],
        "cohort_entity_facts": contract["cohort_entity_facts"],
        "per_candidate_chunk_facts": contract["per_candidate_chunk_facts"],
        # PRIVATE snapshot binding (the file itself is gitignored; only its hash
        # is tracked). evaluate/replay fail closed if this hash does not match.
        "raw_frozen_snapshot": contract["raw_frozen_snapshot"],
    }
    out = Path(args.manifest)
    _write_json(out, manifest)
    print(json.dumps({
        "frozen_entities": len(entities),
        "preflight_within_cap": preflight["aggregate"]["within_spend_cap"],
        "projected_cost_all_candidates_usd": preflight["aggregate"]["projected_cost_usd_all_candidates"],
        "missing_judged_identities": counts["integrity"]["missing_judged_identities"],
        "raw_frozen_snapshot_sha256": snapshot["content_sha256"],
        "manifest": str(out),
    }, indent=2))
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    cache_dir = Path(args.cache_dir)
    # Future raw evaluate is SNAPSHOT-DRIVEN by default: a verified raw frozen
    # snapshot binding (recorded in a freeze-input manifest) is MANDATORY. The
    # cohort is reconstructed exactly from the snapshot — the live reader is NOT
    # called. There is no silent fallback to a live read.
    manifest = None
    if getattr(args, "frozen_manifest", None):
        manifest = json.loads(Path(args.frozen_manifest).read_text(encoding="utf-8"))
    snapshot_binding = verify_frozen_snapshot_binding(
        manifest, cache_dir, repo_root=REPO_ROOT, golden_path=Path(args.golden)
    )

    if snapshot_binding is not None:
        # Snapshot-driven path: reconstruct the exact frozen cohort offline.
        snap_path = sem.resolve_private_path(snapshot_binding["path"], anchor=REPO_ROOT)
        snapshot = load_raw_frozen_snapshot(snap_path)
        entities = entities_from_raw_snapshot(snapshot)
        counts = {
            "snapshot_driven": True,
            "n_entities": len(entities),
            "note": "cohort reconstructed from the verified raw frozen snapshot; live reader not called",
        }
    elif getattr(args, "allow_live_read", False):
        # Explicitly-named UNSAFE / live developer mode. Impossible to confuse
        # with the release path: it requires both --frozen-manifest to be absent
        # (or have no snapshot binding) AND this explicit opt-in flag. It still
        # honors the normal preflight spend cap. NOT executed in this offline task.
        entities, counts = read_real_cohort_entities(
            endpoint=args.endpoint, anon_key=args.anon_key, golden_path=Path(args.golden),
            message_sample_size=args.message_sample,
        )
    else:
        raise SystemExit(
            "future raw evaluate requires a verified raw frozen snapshot binding "
            "(--frozen-manifest with raw_frozen_snapshot); refusing to silently "
            "fall back to a live cohort read. An explicitly named unsafe live "
            "developer mode exists (--allow-live-read) but is NOT the release path."
        )
    cohorts = _build_cohorts(entities)
    # Gap 3: recompute and compare EVERY bound chunk/entity fact after rebuilding
    # entities/cohorts from the snapshot with the CURRENT chunker/
    # canonicalization/config. Any drift fails closed BEFORE any provider or
    # offline-embedding call.
    if snapshot_binding is not None and manifest is not None:
        verify_rebuilt_freeze_facts(manifest, cohorts, entities)
    preflight = _preflight(cohorts)
    if not preflight["aggregate"]["within_spend_cap"]:
        raise SystemExit(f"preflight projected spend exceeds cap; aborting before any provider call")
    api_key = _load_approved_key(args.key_source_env) if not args.offline_embedder else None
    if api_key is None and not args.offline_embedder:
        raise SystemExit("no approved OpenAI key reachable (set OPENAI_API_KEY or --key-source-env)")
    metrics = _run_all_candidates(
        cohorts, Path(args.golden), cache_dir=cache_dir, api_key=api_key,
        offline=bool(args.offline_embedder), replay_only=False, ks=_parse_ks(args.ks),
        progress=lambda msg: print(msg, file=sys.stderr),
    )
    winner = sem.select_winner(metrics)
    envelope = sem.sanitize_report(metrics, winner)
    envelope["mode"] = "offline-embedder" if args.offline_embedder else "real-provider"
    envelope["generated_at"] = args.generated_at or _utc_now_iso()
    envelope["golden_version"] = "golden/2026-07-28/v1"
    envelope["cohort_counts"] = counts
    envelope["preflight"] = preflight
    _write_json(Path(args.out_json), envelope)
    md = render_markdown(envelope, mode=envelope["mode"], cohort_counts=counts, preflight=preflight)
    Path(args.out_md).parent.mkdir(parents=True, exist_ok=True)
    # MD hygiene: scan for keys/URLs/vectors.
    violations = sem.scan_envelope(md)
    if violations:
        raise RuntimeError(f"markdown hygiene violations {violations}")
    Path(args.out_md).write_text(md + "\n", encoding="utf-8")
    print(json.dumps({
        "winner": winner.candidate.name if winner else None,
        "generated_json": args.out_json,
        "generated_md": args.out_md,
    }, indent=2))
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """Strict cache/evidence replay. Fully offline.

    Loads ONLY the private replay bundle + the tracked decision/golden/manifest
    + the two immutable caches and runs :func:`sem.strict_offline_replay`. It
    never reads a live cohort and never instantiates a provider transport, so it
    succeeds with the endpoint deliberately unreachable and reports zero
    provider/network calls while reproducing the recorded winner. (The legacy
    raw-text replay semantics, which required a live cohort read, are not the
    default; this is the strict evidence replay.)
    """

    cache_dir = Path(args.cache_dir)
    golden_path = Path(args.golden)
    decision_path = Path(args.decision_json)
    manifest_path = Path(args.frozen_manifest) if getattr(args, "frozen_manifest", None) else DEFAULT_MANIFEST
    bundle_path = Path(args.bundle) if getattr(args, "bundle", None) else (cache_dir / "replay-bundle.json")
    # Explicit, mandatory Markdown path (gap 1): the evidence pair is NOT
    # optional. When the manifest binds an evidence_pair, strict replay requires
    # BOTH the decision JSON and the decision Markdown paths and fails closed if
    # either is absent/None/unreadable — it never derives an optional file that
    # can be silently skipped.
    md_path = Path(args.decision_md)

    # Fail closed BEFORE reading private artifacts: the private cache parent dir
    # must be 0700 and the private replay bundle must be 0600. A permissive mode
    # means the private artifact may have been exposed -> refuse to load it.
    sem.assert_private_dir(cache_dir, mode=sem.PRIVATE_DIR_MODE)
    sem.assert_private_file(bundle_path, mode=sem.PRIVATE_FILE_MODE)

    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    golden = load_golden_set(golden_path)
    golden_query_tokens = int(sem.golden_query_token_estimate(golden)["estimated_input_tokens"])

    proof = sem.strict_offline_replay(
        bundle=bundle,
        decision=decision,
        golden_path=golden_path,
        cache_dir=cache_dir,
        manifest=manifest,
        golden_query_tokens=golden_query_tokens,
        bundle_path=str(bundle_path),
        decision_json_path=str(decision_path),
        decision_md_path=str(md_path),
    )
    print(json.dumps({
        "mode": "strict-offline-evidence-cache-replay",
        "zero_provider_calls": proof["zero_provider_calls"],
        "zero_network_calls": proof["zero_network_calls"],
        "provider_calls_attempted": proof["provider_calls_attempted"],
        "network_calls": proof["network_calls"],
        "winner_reproduced": proof["winner_reproduced"],
        "eligible_ranking_reproduced": proof["eligible_ranking_reproduced"],
        "checks_passed": proof["n_checks"],
    }, indent=2))
    return 0 if proof["zero_provider_calls"] and proof["zero_network_calls"] else 2


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------


def _parse_ks(raw: str) -> tuple[int, ...]:
    return tuple(int(p) for p in raw.split(",") if p.strip())


def _common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--golden", default=str(DEFAULT_GOLDEN))
    p.add_argument("--endpoint", default=os.environ.get("HIVEMIND_API_URL", DEFAULT_ENDPOINT))
    p.add_argument("--anon-key", default=os.environ.get("HIVEMIND_ANON_KEY", DEFAULT_ANON_KEY))
    p.add_argument("--message-sample", type=int, default=MESSAGE_SAMPLE_SIZE)
    p.add_argument("--cache-dir", default=str(sem.DEFAULT_CACHE_DIR))
    p.add_argument("--generated-at", default=None, help="Deterministic timestamp for tests.")
    p.add_argument("--ks", default="1,5,10")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Task 2.14 embedding candidate comparison.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_off = sub.add_parser("offline", help="Deterministic fixture mechanics (no network).")
    _common_args(p_off)
    p_off.add_argument("--corpus", default=str(DEFAULT_FIXTURE_CORPUS))
    p_off.add_argument("--out-dir", default=str(REPO_ROOT / "docs" / "hybrid-search"))
    p_off.set_defaults(func=cmd_offline)

    p_fr = sub.add_parser("freeze", help="Live read-only snapshot + preflight manifest.")
    _common_args(p_fr)
    p_fr.add_argument("--manifest", default=str(DEFAULT_FREEZE_INPUT_MANIFEST),
                      help="Freeze-INPUT manifest (distinct from the final evidence manifest).")
    p_fr.set_defaults(func=cmd_freeze)

    p_ev = sub.add_parser("evaluate", help="Real bounded OpenAI evaluation.")
    _common_args(p_ev)
    p_ev.add_argument("--key-source-env", default=None)
    p_ev.add_argument("--offline-embedder", action="store_true",
                      help="Use the deterministic fake embedder (no provider spend).")
    p_ev.add_argument("--out-json", default=str(DEFAULT_DECISION_JSON))
    p_ev.add_argument("--out-md", default=str(DEFAULT_DECISION_MD))
    p_ev.add_argument("--frozen-manifest", default=None,
                      help="Freeze-input manifest whose raw-snapshot binding drives the cohort "
                           "(mandatory for the release evaluate path).")
    p_ev.add_argument("--allow-live-read", action="store_true",
                      help="UNSAFE live developer mode: read the live cohort instead of the "
                           "snapshot. NOT the release path; honors normal safety gates.")
    p_ev.set_defaults(func=cmd_evaluate)

    p_rp = sub.add_parser("replay", help="Strict cache-only replay (zero provider calls).")
    _common_args(p_rp)
    p_rp.add_argument("--decision-json", default=str(DEFAULT_DECISION_JSON),
                      help="Tracked eval3 decision JSON to replay against.")
    p_rp.add_argument("--decision-md", default=str(DEFAULT_DECISION_MD),
                      help="Tracked eval3 decision Markdown (the evidence pair is NOT optional: "
                           "when the manifest binds evidence_pair, BOTH JSON and MD are required).")
    p_rp.add_argument("--bundle", default=None,
                      help="Private replay bundle JSON (default: <cache-dir>/replay-bundle.json).")
    p_rp.add_argument("--frozen-manifest", default=str(DEFAULT_MANIFEST),
                      help="Tracked frozen/evidence manifest binding the caches/bundle/pair.")
    p_rp.set_defaults(func=cmd_replay)
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
