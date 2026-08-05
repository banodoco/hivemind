#!/usr/bin/env python3
"""Local-only throwaway-PostgreSQL rehearsal for plan task 2.13.

This is the **production-shaped** pilot: it drives the REAL task-2.12 machinery
(workflow remediation stage -> resumable embedding backfill over a direct/session
``PostgresBackfillStore``) against a small but representative fixture, then audits
the DURABLE database state through the pure
:mod:`executors.embedding_coverage` evaluator.  Every C1-C6 verdict is therefore
derived from rows in ``content_embeddings`` + the source tables, never from a
hardcoded boolean.

Safety boundaries (frozen):
  * local throwaway PostgreSQL only (never production/staging);
  * deterministic fake provider only (zero network embedding calls);
  * never reads/prints/persists DB URLs, provider keys, source bodies, Python
    source, or matched secrets (the envelope is a bounded count/rate envelope);
  * never activates an embedding contract and never creates HNSW;
  * never executes stored Python.

If the sandbox denies the throwaway cluster, an honest ``unavailable`` envelope is
emitted and the one-command outside-sandbox rerun is printed.  A successful run
replaces the checked-in evidence file.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from executors import canonical_representations as canon  # noqa: E402
from executors import chunking as ch  # noqa: E402
from executors import embedding_backfill as eb  # noqa: E402
from executors import embedding_contract as ec  # noqa: E402
from executors import embedding_coverage as cov  # noqa: E402
from executors import postgres_backfill as pg  # noqa: E402
import lexical_pg as lp  # noqa: E402
from executors import workflow_representation as wr  # noqa: E402

EVIDENCE = REPO / "docs" / "hybrid-search" / "task-2.13-local-rehearsal.json"
DEFAULT_VIBECOMFY_ROOT = Path(
    os.environ.get("VIBECOMFY_ROOT", "/Users/peteromalley/Documents/reigh-workspace/vibecomfy")
)

#: The exact Snowflake above JavaScript's safe-integer range (2**53).  JavaScript
#: would round this to 9007199254740992; the rehearsal proves the exact value is
#: preserved end-to-end through item_id strings.
SNOWFLAKE_ABOVE_JS_SAFE = "9007199254740993"
DELETED_MESSAGE_ID = "8888888888888888888"
REJECTED_DISTILLATION_ID = "3"

SOURCES = ("approved_distillations", "pending_distillations", "resources", "messages")

# Minimal source-table + vector DDL (pgvector is intentionally NOT required: the
# local rehearsal uses a text domain named ``vector`` exactly like the accepted
# 2.12c rehearsal; production uses the real vector(384) type).
DDL = """
create table external_resources (id bigint primary key, kind text not null, source text not null default 'fixture', external_id text, title text not null, body text not null default '', author text, url text, metadata jsonb not null default '{}'::jsonb, payload jsonb, created_at timestamptz not null default now());
create table distillations (id bigint primary key, question text not null, conditions text, answer text not null, status text not null, created_at timestamptz not null default now());
create table discord_messages (message_id bigint primary key, content text, created_at timestamptz not null default now(), is_deleted boolean not null default false);
create table embedding_contracts (id bigint primary key, provider text not null, model text not null, dimension integer not null, status text not null default 'draft');
insert into embedding_contracts values (1, 'deterministic-fake', 'deterministic-fake', 384, 'draft');
create domain vector as text;
create table content_embeddings (contract_id bigint not null, entity_type text not null, item_id text not null, representation_type text not null, chunk_index integer not null, chunk_text text, embedding vector not null, representation_hash text not null, chunk_hash text not null, primary key(contract_id,entity_type,item_id,representation_type,chunk_index));
"""

# A representative, bounded fixture: approved + pending eligible distillations,
# one explicitly ineligible (rejected) distillation, an ordinary resource, the
# full workflow Python cohort (payload / body-only / duplicate body+payload /
# VibeComfy-recoverable / unavailable / quarantined), a representative Discord
# cohort including the exact JS-unsafe Snowflake, and a deleted out-of-cohort
# message.  No fixture row here is a hardcoded gate result.
SEED = """
insert into distillations values
 (1,'approved question',null,'approved answer','approved','2025-01-01'),
 (2,'pending question',null,'pending answer','pending','2025-01-02'),
 (3,'rejected question',null,'rejected answer','rejected','2025-01-03');
insert into external_resources values
 (10,'article','fixture','a','ordinary resource','ordinary prose body',null,null,'{}','{}','2025-01-03'),
 (11,'workflow','fixture','payload','payload workflow','prose only here',null,null,'{}','{"python_source":"x = 1\\n"}','2025-01-03'),
 (12,'workflow','fixture','body','body workflow',E'prose\\n\\nPython scratchpad source:\\ny = 2\\n',null,null,'{}','{}','2025-01-03'),
 (13,'workflow','fixture','duplicate','duplicate workflow',E'prose\\n\\nPython ready-template source:\\nz = 3\\n',null,null,'{}','{"python_source":"z = 3\\n"}','2025-01-03'),
 (14,'workflow','fixture','recoverable','recoverable workflow','prose only here',null,null,'{}',jsonb_build_object('corpus_path','tests/snapshots/empty_image_red_smoke_required.api.json'),'2025-01-03'),
 (15,'workflow','fixture','unavailable','unavailable workflow','safe prose',null,null,'{}','{}','2025-01-03'),
 (16,'workflow','fixture','quarantined','quarantined workflow','safe prose',null,null,'{}',jsonb_build_object('python_source',$$token = 'sk-abcdefghijklmnopqrstuvwxyz0123456789'
$$),'2025-01-03');
insert into discord_messages values
 (9007199254740993,'snowflake message above js safe integer range','2025-01-04',false),
 (9007199254740992,'ordinary cohort message one','2025-01-04',false),
 (9007199254740994,'ordinary cohort message two','2025-01-04',false),
 (9007199254740995,'ordinary cohort message three','2025-01-04',false),
 (9007199254740996,'ordinary cohort message four','2025-01-04',false),
 (9007199254740997,'ordinary cohort message five','2025-01-04',false),
 (8888888888888888888,'deleted out of cohort message','2025-01-04',true);
"""


# ---------------------------------------------------------------------------
# Deterministic fake provider + clock (the only provider/clock used here)
# ---------------------------------------------------------------------------


class FakeProvider:
    """Deterministic fake provider; counts every call (zero network)."""

    model_name = "deterministic-fake"
    provider_name = "deterministic-fake"
    dimension = 384

    def __init__(self) -> None:
        self.calls = 0
        self._fake = ec.DeterministicFakeEmbedder(dimension=384)

    def embed(self, texts):
        import asyncio
        self.calls += 1
        return asyncio.run(self._fake.embed_texts(list(texts)))


class Clock:
    def acquire(self):
        return None

    def sleep(self, _seconds):
        return None

    def now(self):
        return 0.0


def _config(*, apply: bool) -> eb.BackfillConfig:
    return eb.BackfillConfig(
        sources=SOURCES, apply=apply, contract_id=1, dimension=384,
        batch_items=2, input_items=2, max_tokens_per_request=16,
    )


# ---------------------------------------------------------------------------
# Durable-state coverage collector (works against any direct/session PsqlSession)
# ---------------------------------------------------------------------------


def _scalar(session: pg.PsqlSession, sql: str) -> str:
    return (session.run(sql) or "").strip()


def _expected_by_representation(row: dict[str, Any]) -> dict[str, list[cov.ChunkIdentity]]:
    """Recompute the expected chunk identity set the frozen builder+chunker emit.

    This is byte-for-byte the same path ``embedding_backfill._build_chunks`` uses,
    so the expected set is what the worker/backfill SHOULD have stored.
    """

    reps = canon.build_representations(row, row["entity_type"], item_id=row["item_id"])
    chunks = ch.chunk_representations(reps)
    by_rt: dict[str, list[cov.ChunkIdentity]] = {}
    for c in chunks:
        by_rt.setdefault(c.representation_type, []).append(
            cov.ChunkIdentity(c.chunk_index, c.representation_hash, c.chunk_hash)
        )
    return by_rt


def _stored_map(session: pg.PsqlSession, contract_id: int) -> dict[tuple[str, str, str], list[cov.ChunkIdentity]]:
    rows = session.json_rows(
        "select entity_type,item_id,representation_type,chunk_index,representation_hash,chunk_hash "
        "from content_embeddings where contract_id=" + str(int(contract_id)) + " "
        "order by entity_type,item_id,representation_type,chunk_index"
    )
    out: dict[tuple[str, str, str], list[cov.ChunkIdentity]] = {}
    for r in rows:
        out.setdefault(
            (str(r["entity_type"]), str(r["item_id"]), str(r["representation_type"])), []
        ).append(cov.ChunkIdentity(int(r["chunk_index"]), str(r["representation_hash"]), str(r["chunk_hash"])))
    return out


def _workflow_samples(session: pg.PsqlSession) -> list[cov.WorkflowSample]:
    rows = session.json_rows(
        "select id::text, payload->'python_provenance'->>'cohort' as cohort, "
        "payload->>'python_public_state' as public_state, "
        "payload->>'python_representation_version' as prep_ver, "
        "payload->>'python_sha256' as py_sha, "
        "payload->>'python_representation_sha256' as rep_sha, "
        "payload->>'python_reason' as reason "
        "from external_resources where kind='workflow' order by id"
    )
    samples: list[cov.WorkflowSample] = []
    for r in rows:
        cohort = str(r.get("cohort") or wr.COHORT_UNAVAILABLE)
        materialized = bool(
            r.get("prep_ver") and r.get("py_sha") and r.get("rep_sha")
        )
        samples.append(
            cov.WorkflowSample(
                item_id=str(r["id"]),
                cohort=cohort,
                public_state=str(r.get("public_state") or wr.PUBLIC_STATE_SAFE),
                materialized_versioned_hashed=materialized,
                unavailable_reason=r.get("reason"),
            )
        )
    return samples


def collect_coverage(
    session: pg.PsqlSession, store: pg.PostgresBackfillStore, *, contract_id: int
) -> tuple[list[cov.RepresentationSample], list[cov.WorkflowSample]]:
    """Build coverage samples from durable DB state (reusable for a real session).

    Enumerates every ENABLED-cohort source row (via the same ``fetch_page`` the
    backfill uses), recomputes its expected representations, and pairs them with
    the actual ``content_embeddings`` chunk set.  Quarantined workflow Python is
    modelled as a non-eligible, empty-expected representation so any illicit
    storage is detected.  Any stored vector whose identity is not expected
    (orphan / ineligible) is appended as a non-eligible sample (C6).
    """

    stored = _stored_map(session, contract_id)
    reps: list[cov.RepresentationSample] = []
    covered_keys: set[tuple[str, str, str]] = set()

    for source in SOURCES:
        cursor: str | None = None
        while True:
            page = store.fetch_page(source, cursor=cursor, limit=1000)
            if not page:
                break
            for row in page:
                expected_by_rt = _expected_by_representation(row)
                for rt, expected in expected_by_rt.items():
                    key = (row["entity_type"], row["item_id"], rt)
                    covered_keys.add(key)
                    reps.append(
                        cov.RepresentationSample(
                            row["entity_type"], row["item_id"], rt, source,
                            eligible=True, expected=tuple(expected),
                            stored=tuple(stored.get(key, ())),
                        )
                    )
                # Quarantined workflow Python: never emitted, never eligible, but
                # sampled so illicit storage is caught by C6.
                if row["entity_type"] == "resource" and (row.get("kind") == "workflow"):
                    status = canon.workflow_python_status(row)
                    if status.public_state == wr.PUBLIC_STATE_QUARANTINED and wr.REP_WORKFLOW_PYTHON not in expected_by_rt:
                        key = ("resource", row["item_id"], wr.REP_WORKFLOW_PYTHON)
                        covered_keys.add(key)
                        reps.append(
                            cov.RepresentationSample(
                                "resource", row["item_id"], wr.REP_WORKFLOW_PYTHON,
                                source, eligible=False, expected=(),
                                stored=tuple(stored.get(key, ())),
                            )
                        )
            cursor = str(page[-1]["item_id"])

    # Orphan / ineligible stored vectors: anything stored that is not an expected
    # eligible representation (a deleted message, a rejected distillation, a
    # stray tail).  Clean pilot -> none.
    for key, chunks in stored.items():
        if key in covered_keys:
            continue
        reps.append(
            cov.RepresentationSample(
                key[0], key[1], key[2], "ineligible", eligible=False,
                expected=(), stored=tuple(chunks),
            )
        )

    return reps, _workflow_samples(session)


# ---------------------------------------------------------------------------
# Evidence envelope (bounded, secret-free)
# ---------------------------------------------------------------------------


def _scan_clean(text: str) -> bool:
    """Defense in depth: the envelope text holds no secret value or URL."""

    if wr.scan_secrets(text):
        return False
    for forbidden in ("postgresql://", "postgres://", "password", "OPENAI_API_KEY", "sk-"):
        if forbidden in text:
            return False
    return True


def _write(data: dict) -> None:
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _envelope(
    *,
    status: str,
    coverage: dict | None = None,
    checks: dict | None = None,
    counts: dict | None = None,
    reason: str | None = None,
    exception_type: str | None = None,
) -> dict:
    data: dict[str, Any] = {
        "task": "2.13",
        "local_only": True,
        "provider": "deterministic-fake",
        "status": status,
        "coverage": dict(coverage or {}),
        "checks": dict(checks or {}),
        "counts": dict(counts or {}),
        "secrets_or_urls_present": False,
    }
    if reason is not None:
        data["reason"] = reason
    if exception_type is not None:
        data["exception_type"] = exception_type
    # Final defense-in-depth scan of the whole envelope.
    data["secrets_or_urls_present"] = not _scan_clean(json.dumps(data, sort_keys=True))
    return data


# ---------------------------------------------------------------------------
# Stale-tail probe (proves detection + repair from durable state)
# ---------------------------------------------------------------------------


def _stored_chunks_for(
    session: pg.PsqlSession, contract_id: int, entity_type: str, item_id: str, rep: str
) -> int:
    return int(_scalar(
        session,
        "select count(*) from content_embeddings where contract_id=" + str(int(contract_id))
        + " and entity_type=" + pg._q(entity_type) + " and item_id=" + pg._q(item_id)
        + " and representation_type=" + pg._q(rep),
    ) or 0)


def _stale_tail_probe(
    session: pg.PsqlSession, store: pg.PostgresBackfillStore, provider: FakeProvider,
    remediation: pg.WorkflowRemediationStage, *, contract_id: int,
) -> dict[str, Any]:
    """Inject one stale tail chunk, prove C5 detects it, then prove a re-run repairs it."""

    target = ("resource", "10", "prose")
    before = _stored_chunks_for(session, contract_id, *target)
    # Inject a tail chunk an older chunker might have left behind.
    session.run(
        "insert into content_embeddings (contract_id,entity_type,item_id,representation_type,"
        "chunk_index,chunk_text,embedding,representation_hash,chunk_hash) values ("
        + str(int(contract_id)) + "," + pg._q(target[0]) + "," + pg._q(target[1]) + ","
        + pg._q(target[2]) + ",999,'stale tail placeholder','[0]'::vector,"
        + pg._q("stale_tail_representation_hash") + "," + pg._q("stale_tail_chunk_hash") + ");"
    )
    after_inject = _stored_chunks_for(session, contract_id, *target)
    reps_after_inject, _ = collect_coverage(session, store, contract_id=contract_id)
    report_after_inject = cov.evaluate_coverage(reps_after_inject, [])
    detected = report_after_inject.gates[cov.GATE_C5].numerator >= 1
    # Re-running the backfill must repair it: representation_current sees the
    # mismatched set, re-embeds, and write_vectors atomically replaces the rep.
    calls_before_repair = provider.calls
    eb.run_backfill(store, provider=provider, remediation_stage=remediation, config=_config(apply=True), rate_limiter=Clock())
    after_repair = _stored_chunks_for(session, contract_id, *target)
    reps_after_repair, _ = collect_coverage(session, store, contract_id=contract_id)
    report_after_repair = cov.evaluate_coverage(reps_after_repair, [])
    repaired = (
        after_repair == before
        and report_after_repair.gates[cov.GATE_C5].numerator == 0
        and provider.calls > calls_before_repair
    )
    return {
        "target_representation": list(target),
        "stored_chunks_before": before,
        "stored_chunks_after_inject": after_inject,
        "c5_stale_detected_after_inject": detected,
        "stored_chunks_after_repair": after_repair,
        "c5_stale_zero_after_repair": report_after_repair.gates[cov.GATE_C5].numerator == 0,
        "repair_made_provider_call": provider.calls > calls_before_repair,
        "stale_tail_detected_and_repaired": bool(detected and repaired),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

#: Gates whose denominators the pilot fixture is required to populate.  The
#: generic ``rate(0, 0) == 1.0`` convention makes a coverage-equality gate
#: vacuously perfect on an empty population; the pilot fixture deliberately
#: seeds these cohorts (C1 distillations+resources, C2 enabled messages, C3
#: recoverable workflows, and one unavailable workflow row for C4), so the
#: rehearsal must prove each denominator is non-zero in evidence.
REQUIRED_PILOT_GATES = (cov.GATE_C1, cov.GATE_C2, cov.GATE_C3, cov.GATE_C4)


def _required_populations_nonempty(report: cov.CoverageReport) -> bool:
    """Every required pilot gate must carry a non-zero denominator.

    This is the evidence-layer check that turns the generic empty-population
    convention into an evidenced, non-vacuous pass for the real pilot: a
    ``0/0`` denominator here means a cohort the fixture failed to seed, which
    is a rehearsal failure regardless of the gate's comparator verdict.
    """

    return all(report.gates[gid].denominator > 0 for gid in REQUIRED_PILOT_GATES)


def _run_cluster(cluster: lp.LocalCluster, *, vibecomfy_root: Path) -> dict:
    cluster.psql(DDL, capture=False)
    cluster.psql_file(REPO / "schema" / "030_embedding_backfill_runs.sql")
    cluster.psql_file(REPO / "schema" / "031_workflow_remediation_runs.sql")
    cluster.psql(SEED, capture=False)

    session = pg.PsqlSession(f"postgresql://postgres@localhost:{cluster.port}/postgres")
    store = pg.PostgresBackfillStore(session)
    remediation = pg.WorkflowRemediationStage(
        pg.PostgresWorkflowStore(session), vibecomfy_root=vibecomfy_root, batch_size=2
    )
    provider = FakeProvider()

    # 1. Remediation runs FIRST and materializes workflow Python BEFORE any
    #    embedding.  Proved from durable state: zero vectors + zero provider
    #    calls exist while the recoverable workflow is already materialized.
    remediation.run(store, apply=True)
    vectors_after_remediation = int(_scalar(session, "select count(*) from content_embeddings;") or 0)
    calls_after_remediation = provider.calls
    r14 = session.json_one(
        "select (payload->>'python_representation_version') is not null as prep, "
        "(payload->>'python_sha256') is not null as pysha, "
        "(payload->'python_provenance'->>'cohort') as cohort "
        "from external_resources where id=14"
    ) or {}
    remediation_before_embedding = (
        vectors_after_remediation == 0
        and calls_after_remediation == 0
        and bool(r14.get("prep"))
        and bool(r14.get("pysha"))
        and r14.get("cohort") == wr.COHORT_RECOVERABLE
    )

    # 2. Dry-run inventories with no provider call / no vector write.
    dry = eb.run_backfill(store, provider=provider, remediation_stage=remediation, config=_config(apply=False), rate_limiter=Clock())
    dry_run_safe = provider.calls == 0 and dry.vectors_written == 0 and dry.chunks_built > 0

    # 3. Apply: remediation (idempotent hash-skip) then embed every eligible rep.
    apply_report = eb.run_backfill(store, provider=provider, remediation_stage=remediation, config=_config(apply=True), rate_limiter=Clock())
    calls_after_apply = provider.calls
    vectors_after_apply = int(_scalar(session, "select count(*) from content_embeddings;") or 0)

    # 4. Coverage from durable state.
    reps, workflows = collect_coverage(session, store, contract_id=1)
    coverage_report = cov.evaluate_coverage(
        reps, workflows,
        reason_codes=tuple(apply_report.reason_counts.keys()),
        extra={"snowflake_item_ids": sorted({r.item_id for r in reps if r.entity_type == "message"})},
    )

    # 5. Re-run (idempotence): zero added provider calls, zero duplicate vectors.
    rerun = eb.run_backfill(store, provider=provider, remediation_stage=remediation, config=_config(apply=True), rate_limiter=Clock())
    vectors_after_rerun = int(_scalar(session, "select count(*) from content_embeddings;") or 0)
    calls_after_rerun = provider.calls

    # 6. Static durable-state facts.
    contract_draft = _scalar(session, "select status from embedding_contracts where id=1;") == "draft"
    no_hnsw = _scalar(
        session,
        "select count(*) from pg_indexes where indexdef ilike '%hnsw%';",
    ) == "0"
    snowflake_stored_exact = _scalar(
        session,
        "select count(*) from content_embeddings where entity_type='message' and item_id="
        + pg._q(SNOWFLAKE_ABOVE_JS_SAFE) + ";",
    ) != "0"
    snowflake_not_rounded = _scalar(
        session,
        "select count(*) from content_embeddings where entity_type='message' and item_id="
        + pg._q("9007199254740992") + " and chunk_text like '%snowflake%';",
    ) == "0"
    deleted_message_vectors = int(_scalar(
        session,
        "select count(*) from content_embeddings where entity_type='message' and item_id="
        + pg._q(DELETED_MESSAGE_ID) + ";",
    ) or 0)
    rejected_distillation_vectors = int(_scalar(
        session,
        "select count(*) from content_embeddings where entity_type='distillation' and item_id="
        + pg._q(REJECTED_DISTILLATION_ID) + ";",
    ) or 0)

    # 7. Stale-tail detection + repair probe (separate, after idempotence).
    stale_probe = _stale_tail_probe(session, store, provider, remediation, contract_id=1)
    # Re-collect final coverage AFTER the probe repaired the tail.  The workflow
    # audit records are preserved and passed so the post-probe C3/C4 evidence
    # reflects the real pilot population rather than a vacuous 0/0.
    reps_final, workflows_final = collect_coverage(session, store, contract_id=1)
    final_coverage = cov.evaluate_coverage(reps_final, workflows_final)

    all_vectors_eligible = (
        coverage_report.ineligible_indexed_representations == 0
        and coverage_report.orphan_vectors == 0
    )

    checks = {
        "remediation_before_embedding": remediation_before_embedding,
        "dry_run_zero_provider_calls_zero_vectors": dry_run_safe,
        "all_gates_passed": coverage_report.all_gates_passed,
        "invariants_clean": coverage_report.invariants_clean,
        "reason_vocabulary_bounded": coverage_report.reason_vocabulary_bounded,
        "all_vectors_belong_to_eligible_item": all_vectors_eligible,
        "rerun_zero_added_provider_calls": calls_after_rerun == calls_after_apply,
        "rerun_zero_duplicate_vectors": vectors_after_rerun == vectors_after_apply,
        "contract_remains_draft": contract_draft,
        "no_hnsw_created": no_hnsw,
        "snowflake_exact_above_js_safe": snowflake_stored_exact and snowflake_not_rounded,
        "deleted_message_has_zero_vectors": deleted_message_vectors == 0,
        "rejected_distillation_has_zero_vectors": rejected_distillation_vectors == 0,
        "stale_tail_detected_and_repaired": stale_probe["stale_tail_detected_and_repaired"],
        "required_pilot_populations_nonempty": _required_populations_nonempty(coverage_report),
        "final_required_pilot_populations_nonempty": _required_populations_nonempty(final_coverage),
    }
    counts = {
        "dry_chunks": dry.chunks_built,
        "apply_vectors": apply_report.vectors_written,
        "vectors_after_apply": vectors_after_apply,
        "vectors_after_rerun": vectors_after_rerun,
        "provider_calls_after_remediation": calls_after_remediation,
        "provider_calls_after_apply": calls_after_apply,
        "provider_calls_after_rerun": calls_after_rerun,
        "provider_calls_added_by_rerun": calls_after_rerun - calls_after_apply,
        "rerun_status": rerun.status,
        "eligible_message_cohort": sum(
            1 for r in reps if r.source == "messages" and r.eligible and r.expected_nonempty
        ),
        "deleted_message_vectors": deleted_message_vectors,
        "rejected_distillation_vectors": rejected_distillation_vectors,
    }
    coverage_envelope = coverage_report.to_envelope()
    coverage_envelope["final_coverage_after_stale_probe"] = final_coverage.to_envelope()["gates"]
    coverage_envelope["stale_tail_probe"] = stale_probe

    ok = all(checks.values())
    status = "passed" if ok else "failed"
    return _envelope(
        status=status, coverage=coverage_envelope, checks=checks, counts=counts,
    )


def main() -> int:
    root = DEFAULT_VIBECOMFY_ROOT
    fixture = root / "tests" / "snapshots" / "empty_image_red_smoke_required.api.json"
    if not fixture.is_file():
        envelope = _envelope(status="unavailable", reason="vibecomfy_fixture_unavailable")
        _write(envelope)
        print(json.dumps({"status": "unavailable", "reason": "vibecomfy_fixture_unavailable"}, sort_keys=True))
        return 2
    try:
        cluster = lp.LocalCluster.start()
    except Exception as exc:
        envelope = _envelope(
            status="unavailable", reason="throwaway_postgres_unavailable",
            exception_type=type(exc).__name__,
        )
        _write(envelope)
        print(json.dumps({
            "status": "unavailable", "reason": "throwaway_postgres_unavailable",
            "exception_type": type(exc).__name__,
            "rerun": "python3 scripts/rehearse_embedding_pilot.py",
        }, sort_keys=True))
        return 2
    try:
        envelope = _run_cluster(cluster, vibecomfy_root=root)
        _write(envelope)
        print(json.dumps({
            "status": envelope["status"],
            "all_gates_passed": envelope["coverage"].get("all_gates_passed"),
            "checks": envelope["checks"],
        }, sort_keys=True))
        return 0 if envelope["status"] == "passed" else 1
    except Exception as exc:
        # Never serialize psql diagnostics, source rows, Python source, or URLs.
        envelope = _envelope(status="failed", reason="rehearsal_exception", exception_type=type(exc).__name__)
        _write(envelope)
        print(json.dumps({
            "status": "failed", "reason": "rehearsal_exception",
            "exception_type": type(exc).__name__,
        }, sort_keys=True))
        return 1
    finally:
        cluster.tear_down()


if __name__ == "__main__":
    raise SystemExit(main())
