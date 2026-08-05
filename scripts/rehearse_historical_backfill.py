#!/usr/bin/env python3
"""Local-only throwaway PostgreSQL rehearsal for task 2.12c.

The fixture is intentionally small but production-shaped: approved/pending
distillations, ordinary resources, workflow source cohorts, and an exact
Snowflake beyond JavaScript's safe integer range.  It never accepts a database
URL and never calls the hosted provider.  A successful run replaces the
checked-in evidence file with a secret-free count/check envelope.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from executors import embedding_backfill as eb  # noqa: E402
from executors import embedding_contract as ec  # noqa: E402
from executors import postgres_backfill as pg  # noqa: E402
import lexical_pg as lp  # noqa: E402

EVIDENCE = REPO / "docs" / "hybrid-search" / "task-2.12c-local-rehearsal.json"


class FakeProvider:
    """The only provider used by this local rehearsal."""
    model_name = "deterministic-fake"
    provider_name = "deterministic-fake"
    dimension = 384

    def __init__(self) -> None:
        self.calls = 0
        self._fake = ec.DeterministicFakeEmbedder(dimension=384)

    def embed(self, texts):
        import asyncio
        self.calls += 1
        return asyncio.run(self._fake.embed_texts(texts))


class Clock:
    def acquire(self):
        return None
    def sleep(self, _seconds):
        return None
    def now(self):
        return 0.0


DDL = """
create table external_resources (id bigint primary key, kind text not null, source text not null default 'fixture', external_id text, title text not null, body text not null default '', author text, url text, metadata jsonb not null default '{}'::jsonb, payload jsonb, created_at timestamptz not null default now());
create table distillations (id bigint primary key, question text not null, conditions text, answer text not null, status text not null, created_at timestamptz not null default now());
create table discord_messages (message_id bigint primary key, content text, created_at timestamptz not null default now(), is_deleted boolean not null default false);
create table embedding_contracts (id bigint primary key, provider text not null, model text not null, dimension integer not null, status text not null default 'draft');
insert into embedding_contracts values (1, 'deterministic-fake', 'deterministic-fake', 384, 'draft');
create domain vector as text;
create table content_embeddings (contract_id bigint not null, entity_type text not null, item_id text not null, representation_type text not null, chunk_index integer not null, chunk_text text, embedding vector not null, representation_hash text not null, chunk_hash text not null, primary key(contract_id,entity_type,item_id,representation_type,chunk_index));
"""


def _write(data: dict) -> None:
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _envelope(*, status: str, checks: dict | None = None, counts: dict | None = None,
              reason: str | None = None, exception_type: str | None = None) -> dict:
    """Build the bounded, secret-free rehearsal evidence envelope."""
    data = {
        "task": "2.12c",
        "local_only": True,
        "provider": "deterministic-fake",
        "status": status,
        "checks": dict(checks or {}),
        "counts": dict(counts or {}),
        "secrets_or_urls_present": False,
    }
    if reason is not None:
        data["reason"] = reason
    if exception_type is not None:
        data["exception_type"] = exception_type
    return data


def main() -> int:
    root = Path(os.environ.get("VIBECOMFY_ROOT", "/Users/peteromalley/Documents/reigh-workspace/vibecomfy"))
    fixture = root / "tests" / "snapshots" / "empty_image_red_smoke_required.api.json"
    if not fixture.is_file():
        _write(_envelope(status="unavailable", reason="vibecomfy_fixture_unavailable"))
        print(json.dumps({"status": "unavailable", "reason": "vibecomfy_fixture_unavailable"}, sort_keys=True))
        return 2
    try:
        cluster = lp.LocalCluster.start()
    except Exception as exc:
        # Sandboxed runners commonly deny loopback binds.  Record that outcome
        # explicitly so an older blocked/passed artifact cannot masquerade as
        # evidence for this invocation.
        _write(_envelope(status="unavailable", reason="throwaway_postgres_unavailable",
                         exception_type=type(exc).__name__))
        print(json.dumps({"status": "unavailable", "reason": "throwaway_postgres_unavailable", "exception_type": type(exc).__name__}, sort_keys=True))
        return 2
    try:
        cluster.psql(DDL, capture=False)
        cluster.psql_file(REPO / "schema" / "030_embedding_backfill_runs.sql")
        cluster.psql_file(REPO / "schema" / "031_workflow_remediation_runs.sql")
        cluster.psql("""
          insert into distillations values (1,'approved question',null,'approved answer','approved','2025-01-01'),(2,'pending question',null,'pending answer','pending','2025-01-02');
          insert into external_resources values
           (10,'article','fixture','a','ordinary resource','ordinary prose',null,null,'{}','{}','2025-01-03'),
           (11,'workflow','fixture','payload','payload workflow','prose',null,null,'{}','{"python_source":"x = 1\\n"}','2025-01-03'),
           (12,'workflow','fixture','body','body workflow','prose\\n\\nPython scratchpad source:\\ny = 2\\n',null,null,'{}','{}','2025-01-03'),
           (13,'workflow','fixture','duplicate','duplicate workflow','prose\\n\\nPython ready-template source:\\nz = 3\\n',null,null,'{}','{"python_source":"z = 3\\n"}','2025-01-03'),
           (14,'workflow','fixture','recoverable','recoverable workflow','prose',null,null,'{}',jsonb_build_object('corpus_path','tests/snapshots/empty_image_red_smoke_required.api.json'),'2025-01-03'),
           (15,'workflow','fixture','unavailable','unavailable workflow','safe prose',null,null,'{}','{}','2025-01-03'),
           (16,'workflow','fixture','quarantined','quarantined workflow','safe prose',null,null,'{}',jsonb_build_object('python_source',$$token = 'sk-abcdefghijklmnopqrstuvwxyz0123456789'\n$$),'2025-01-03');
          insert into discord_messages values (9007199254740993,'snowflake message','2025-01-04',false);
        """, capture=False)
        session = pg.PsqlSession(f"postgresql://postgres@localhost:{cluster.port}/postgres")
        store = pg.PostgresBackfillStore(session)
        remediation = pg.WorkflowRemediationStage(pg.PostgresWorkflowStore(session), vibecomfy_root=root, batch_size=2)
        provider = FakeProvider()
        config = eb.BackfillConfig(sources=("approved_distillations", "pending_distillations", "resources", "messages"), apply=False, contract_id=1, dimension=384, batch_items=2, input_items=2, max_tokens_per_request=16)
        report = eb.run_backfill(store, provider=provider, remediation_stage=remediation, config=config, rate_limiter=Clock())
        calls_after_dry = provider.calls
        before = cluster.psql("select count(*) from content_embeddings;", capture=True)[1].strip()
        apply_report = eb.run_backfill(store, provider=provider, remediation_stage=remediation, config=eb.BackfillConfig(sources=config.sources, apply=True, contract_id=1, dimension=384, batch_items=2, input_items=2, max_tokens_per_request=16), rate_limiter=Clock())
        calls_after_apply = provider.calls
        rerun = eb.run_backfill(store, provider=provider, remediation_stage=remediation, config=eb.BackfillConfig(sources=config.sources, apply=True, contract_id=1, dimension=384, batch_items=2, input_items=2, max_tokens_per_request=16), rate_limiter=Clock())
        vectors = cluster.psql("select count(*) from content_embeddings;", capture=True)[1].strip()
        workflow_state = json.loads(cluster.psql("""
          select coalesce(jsonb_object_agg(id::text, jsonb_build_object(
            'cohort', payload->'python_provenance'->>'cohort',
            'python_nonempty', char_length(btrim(coalesce(payload->>'python_source',''))) > 0,
            'public_state', payload->>'python_public_state',
            'reason', payload->>'python_reason'
          ) order by id), '{}'::jsonb)
          from external_resources where kind='workflow';
        """, capture=True)[1].strip() or "{}")
        no_hnsw = cluster.psql("select count(*) from pg_indexes where indexdef ilike '%hnsw%';", capture=True)[1].strip() == "0"
        inactive = cluster.psql("select status from embedding_contracts where id=1;", capture=True)[1].strip() == "draft"
        recovered = workflow_state.get("14", {})
        unavailable = workflow_state.get("15", {})
        quarantined = workflow_state.get("16", {})
        checks = {
            "dry_run_zero_vectors": before == "0",
            "dry_run_zero_provider_calls": calls_after_dry == 0,
            "real_vibecomfy_recovery": (
                recovered.get("cohort") == "recoverable"
                and recovered.get("python_nonempty") is True
            ),
            "quarantine_and_unavailable": (
                unavailable.get("cohort") == "unavailable"
                and unavailable.get("reason") == "no_source_artifact"
                and quarantined.get("public_state") == "quarantined"
            ),
            "snowflake_exact": '9007199254740993' in json.dumps(report.to_log_dict()),
            "apply_wrote_vectors": int(vectors or 0) > 0,
            "rerun_completed": rerun.status == 'completed',
            "hash_skip_reduced_provider_calls": provider.calls == calls_after_apply,
            "no_hnsw_evidence": no_hnsw,
            "contract_not_activated": inactive,
        }
        counts = {
            "dry_chunks": report.chunks_built,
            "apply_vectors": apply_report.vectors_written,
            "vectors_after_rerun": int(vectors or 0),
            "provider_calls_dry_run": calls_after_dry,
            "provider_calls_after_apply": calls_after_apply,
            "provider_calls_after_rerun": provider.calls,
            "provider_calls_added_by_rerun": provider.calls - calls_after_apply,
        }
        if not all(checks.values()):
            _write(_envelope(status="failed", checks=checks, counts=counts,
                             reason="rehearsal_checks_failed"))
            print(json.dumps({"status":"failed","checks":checks}, sort_keys=True))
            return 1
        _write(_envelope(status="passed", checks=checks, counts=counts))
        return 0
    except Exception as exc:
        # Keep unexpected rehearsal failures bounded to a category; in
        # particular, never serialize psql diagnostics, source rows, or URLs.
        _write(_envelope(status="failed", reason="rehearsal_exception",
                         exception_type=type(exc).__name__))
        print(json.dumps({"status": "failed", "reason": "rehearsal_exception",
                          "exception_type": type(exc).__name__}, sort_keys=True))
        return 1
    finally:
        cluster.tear_down()


if __name__ == "__main__":
    raise SystemExit(main())
