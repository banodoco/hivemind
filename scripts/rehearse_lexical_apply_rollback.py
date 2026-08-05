#!/usr/bin/env python3
"""Isolated-cluster rehearsal of the EXACT dependency-ordered apply + rollback
for the live-merge batch: schema/003 -> schema/008 -> schema/009.

The existing ``scripts/rehearse_lexical_candidate.py`` proves the FORWARD apply
of the full 001 + 003..009 chain (18/18). This rehearsal adds the missing half
the coordinator asked for: rehearse the reverse-order ROLLBACK of exactly the
three batch migrations (009 -> 008 -> 003), prove it leaves NO batch object
behind, prove it NEVER touches a source row, and prove the forward chain is
idempotent (re-apply after rollback is clean).

Uses the same throwaway local PostgreSQL cluster (initdb --auth=trust, temp data
dir, ephemeral port) as ``scripts/lexical_pg.py``. No Docker, no network, no
production mutation.
"""

from __future__ import annotations

import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import lexical_pg as LP  # noqa: E402
import rehearse_lexical_candidate as R  # noqa: E402

SCHEMA_DIR = REPO / "schema"
EVIDENCE_OUT = REPO / "docs" / "hybrid-search" / "phase1-lexical-apply-rollback-rehearsal.json"

# The exact DROP statements the live drivers run, rehearsed here against the
# isolated cluster. 009+008 first (RPC -> candidate fn -> trigram index), then
# 003 (derived tables -> generated columns -> helper functions).
ROLLBACK_009_008_SQL = """
drop function if exists public.hivemind_lexical_search(text,int,text[],text[],text[],timestamptz,text[],text[],text);
drop function if exists public.hivemind_lexical_candidates(text,int,text[],text[],text[],timestamptz,text[],text[],boolean,boolean);
drop index if exists public.lexical_documents_python_chunk_trgm_idx;
"""

ROLLBACK_003_SQL = """
drop table if exists public.lexical_documents cascade;
drop table if exists public.lexical_resource_python_state cascade;
alter table public.external_resources drop column if exists prose_tsv;
alter table public.distillations drop column if exists lexical_tsv;
drop function if exists public.hivemind_workflow_python_state(bigint);
drop function if exists public.hivemind_workflow_prose(text, text);
drop function if exists public.hivemind_workflow_semantics_text(jsonb);
drop function if exists public.hivemind_resource_tags(jsonb);
drop function if exists public.hivemind_jsonb_leaves(jsonb);
"""

BATCH_MIGRATIONS = [
    "003_lexical_resource_documents.sql",
    "008_lexical_candidate_sql.sql",
    "009_lexical_search_rpc.sql",
]

SEED_SQL = """
insert into public.external_resources (id, kind, source, external_id, title, body, author, url, metadata) overriding system value
values
  (5001, 'workflow', 'vibecomfy-external', 'wf-5001', 'WanVideoSampler workflow',
   'Pipeline using WanVideoSampler for image to video.', 'tester', 'https://example.com/5001',
   '{"workflow_semantics": {"node_types": ["WanVideoSampler"], "models": ["wan2.2-i2v-14B.safetensors"]}}'::jsonb),
  (5002, 'workflow', 'vibecomfy-external', 'wf-5002', 'CogVideoX workflow',
   'CogVideoX based sampler pipeline.', 'tester', 'https://example.com/5002', '{}'::jsonb)
on conflict (id) do nothing;
insert into public.contributors (id, name, kind) overriding system value
values (1, 'rehearse-author', 'agent') on conflict (id) do nothing;
insert into public.distillations (id, question, conditions, answer, confidence, status, author_id) overriding system value
values (9001, 'How do I use WanVideoSampler?', 'for i2v', 'Set num_frames and lora_weight.', 'high', 'approved', 1)
on conflict (id) do nothing;
"""


def _present(cluster: LP.LocalCluster, kind: str, name: str) -> bool:
    if kind == "fn":
        q = ("select exists(select 1 from pg_proc p join pg_namespace n on n.oid=p.pronamespace "
             f"where n.nspname='public' and p.proname='{name}')")
    elif kind == "tbl":
        q = ("select exists(select 1 from pg_class c join pg_namespace n on n.oid=c.relnamespace "
             f"where n.nspname='public' and c.relname='{name}')")
    elif kind == "idx":
        q = ("select exists(select 1 from pg_class c join pg_namespace n on n.oid=c.relnamespace "
             f"where n.nspname='public' and c.relname='{name}')")
    else:  # gencol tbl.col
        tbl, col = name.split(".")
        q = ("select exists(select 1 from pg_attribute a join pg_class c on c.oid=a.attrelid "
             f"join pg_namespace n on n.oid=c.relnamespace where n.nspname='public' "
             f"and c.relname='{tbl}' and a.attname='{col}' and not a.attisdropped)")
    rc, out = cluster.psql(q)
    return out.strip() == "t"


def _source_checksum(cluster: LP.LocalCluster) -> dict:
    rc, out = cluster.psql(
        "select (select count(*) from public.external_resources where id in (5001,5002)) || '|' || "
        "coalesce((select sum(char_length(body)) from public.external_resources where id in (5001,5002)),0) || '|' || "
        "(select count(*) from public.distillations where id=9001) || '|' || "
        "coalesce((select char_length(answer) from public.distillations where id=9001),0);"
    )
    return {"checksum": out.strip()}


def rehearse() -> dict:
    ev: dict = {"task": "live-merge apply+rollback rehearsal", "migrations": BATCH_MIGRATIONS}
    checks: list[dict] = []
    cluster = LP.LocalCluster.start()
    try:
        R.reset_schema(cluster)
        R.bootstrap(cluster)  # 001 + 003..009 in dependency order (proven forward apply)
        cluster.psql(SEED_SQL, capture=False)
        before = _source_checksum(cluster)

        # 1. All batch objects present after forward apply.
        present_after_apply = {
            "fn:hivemind_workflow_python_state": _present(cluster, "fn", "hivemind_workflow_python_state"),
            "tbl:lexical_documents": _present(cluster, "tbl", "lexical_documents"),
            "tbl:lexical_resource_python_state": _present(cluster, "tbl", "lexical_resource_python_state"),
            "gencol:external_resources.prose_tsv": _present(cluster, "gencol", "external_resources.prose_tsv"),
            "gencol:distillations.lexical_tsv": _present(cluster, "gencol", "distillations.lexical_tsv"),
            "fn:hivemind_lexical_candidates": _present(cluster, "fn", "hivemind_lexical_candidates"),
            "fn:hivemind_lexical_search": _present(cluster, "fn", "hivemind_lexical_search"),
            "idx:lexical_documents_python_chunk_trgm_idx": _present(cluster, "idx", "lexical_documents_python_chunk_trgm_idx"),
        }
        checks.append({"name": "forward_apply_all_batch_objects_present",
                       "ok": all(present_after_apply.values()), "detail": present_after_apply})

        # 2. Rollback 009 -> 008 (RPC, candidate fn, trigram index).
        cluster.psql(ROLLBACK_009_008_SQL, capture=False)
        gone_008_009 = {
            "fn:hivemind_lexical_search": not _present(cluster, "fn", "hivemind_lexical_search"),
            "fn:hivemind_lexical_candidates": not _present(cluster, "fn", "hivemind_lexical_candidates"),
            "idx:trgm": not _present(cluster, "idx", "lexical_documents_python_chunk_trgm_idx"),
            "tbl:lexical_documents_still_present": _present(cluster, "tbl", "lexical_documents"),
        }
        checks.append({"name": "rollback_009_008_drops_rpc_candidate_index",
                       "ok": all(gone_008_009.values()), "detail": gone_008_009})

        # 3. Rollback 003 (tables, generated columns, helpers).
        cluster.psql(ROLLBACK_003_SQL, capture=False)
        gone_003 = {
            "tbl:lexical_documents": not _present(cluster, "tbl", "lexical_documents"),
            "tbl:lexical_resource_python_state": not _present(cluster, "tbl", "lexical_resource_python_state"),
            "gencol:prose_tsv": not _present(cluster, "gencol", "external_resources.prose_tsv"),
            "gencol:lexical_tsv": not _present(cluster, "gencol", "distillations.lexical_tsv"),
            "fn:hivemind_workflow_python_state": not _present(cluster, "fn", "hivemind_workflow_python_state"),
            "fn:hivemind_workflow_prose": not _present(cluster, "fn", "hivemind_workflow_prose"),
            "fn:hivemind_jsonb_leaves": not _present(cluster, "fn", "hivemind_jsonb_leaves"),
        }
        checks.append({"name": "rollback_003_drops_tables_gencols_helpers",
                       "ok": all(gone_003.values()), "detail": gone_003})

        # 4. Source rows unchanged by rollback.
        after = _source_checksum(cluster)
        checks.append({"name": "rollback_preserves_source_rows",
                       "ok": before == after, "detail": {"before": before, "after": after}})

        # 5. Forward re-apply after rollback is clean (idempotence of the chain).
        reapplied = True
        err = ""
        try:
            for name in BATCH_MIGRATIONS:
                cluster.psql_file(SCHEMA_DIR / name)
        except Exception as exc:  # noqa: BLE001
            reapplied = False
            err = str(exc)
        present_after_reapply = {
            "fn:hivemind_lexical_search": _present(cluster, "fn", "hivemind_lexical_search"),
            "tbl:lexical_documents": _present(cluster, "tbl", "lexical_documents"),
            "gencol:external_resources.prose_tsv": _present(cluster, "gencol", "external_resources.prose_tsv"),
        }
        checks.append({"name": "forward_reapply_after_rollback_idempotent",
                       "ok": reapplied and all(present_after_reapply.values()),
                       "detail": {"reapplied": reapplied, "error": err, "objects": present_after_reapply}})

        # 6. Re-apply is itself idempotent (apply twice).
        idem = True
        try:
            cluster.psql_file(SCHEMA_DIR / "003_lexical_resource_documents.sql")
            cluster.psql_file(SCHEMA_DIR / "008_lexical_candidate_sql.sql")
            cluster.psql_file(SCHEMA_DIR / "009_lexical_search_rpc.sql")
        except Exception as exc:  # noqa: BLE001
            idem = False
        checks.append({"name": "double_apply_idempotent", "ok": idem, "detail": {}})
    finally:
        cluster.tear_down()

    ev["checks"] = checks
    ev["all_pass"] = all(c["ok"] for c in checks)
    ev["n_pass"] = sum(1 for c in checks if c["ok"])
    ev["n_total"] = len(checks)
    EVIDENCE_OUT.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_OUT.write_text(json.dumps(ev, indent=2) + "\n")
    return ev


def main() -> int:
    if LP.find_pgbins() is None:
        print("SKIP: PostgreSQL binaries not found", file=sys.stderr)
        return 0
    ev = rehearse()
    for c in ev["checks"]:
        print(f"  [{'OK' if c['ok'] else 'FAIL'}] {c['name']}")
    print(f"Rehearsal: {ev['n_pass']}/{ev['n_total']} passed; all_pass={ev['all_pass']}")
    return 0 if ev["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
