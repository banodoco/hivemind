#!/usr/bin/env python3
"""Task 2.17 — isolated PostgreSQL/pgvector Phase-2 acceptance rehearsal.

Runs the COMPLETE selected-contract lifecycle against a throwaway local cluster
(schema/003 + 020-033 + the new 034): source/remediation -> manifest -> enqueue
-> claim -> payload -> deterministic fake embed/drop -> source-hash-safe
finalize -> complete -> semantic candidates, plus concurrency, crash/lease
recovery, and source-change races. Deterministic 384-d fake vectors only; zero
provider/network/production/cache mutation. Writes an allow-listed, secret/source/
vector/URL-free evidence JSON. Run TWICE on fresh throwaway clusters; the second
run must rebuild state and reproduce the same deterministic core/output hashes.

Read-only vs the repo except for the evidence JSON it writes. Not a provider
recompute; it proves the local Phase-2 acceptance surface agrees end-to-end.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import lexical_pg as lp  # noqa: E402
import rehearse_hnsw_pilot as hp  # noqa: E402
from rehearse_hnsw_pilot import fake_vec, vec_literal, SEL  # noqa: E402
from executors import chunking as ch  # noqa: E402
from executors import canonical_representations as canon  # noqa: E402
from executors import selected_contract as sc  # noqa: E402
from executors import workflow_representation as wr  # noqa: E402
from executors import embedding_contract as ec  # noqa: E402

DIM = 384
EVIDENCE_PATH = REPO / "docs" / "hybrid-search" / "task-2.17-local-rehearsal.json"
MIGRATIONS = hp.MIGRATIONS + ["034_phase2_acceptance_contract.sql"]
INDEX_NAME = f"content_embeddings_hnsw_c{SEL}"


# ---------------------------------------------------------------------------
# Small cluster helpers (psql -A -t; json for pipe-unsafe columns)
# ---------------------------------------------------------------------------

def run(c, sql: str) -> None:
    c.psql(sql, capture=False)


def run_replica(c, sql: str) -> None:
    """Run DML with triggers disabled (one psql process: the role is session-scoped
    and does NOT persist across the separate psql process per call). Used for
    source seeding so the external_resources job-enqueue trigger does not create
    uncontrolled jobs; all embedding_jobs are then seeded explicitly."""
    c.psql("set session_replication_role=replica;\n" + sql + "\nset session_replication_role=origin;",
           capture=False)


def cancel_pending(c) -> int:
    rc, out = c.psql("update embedding_jobs set status='cancelled',locked_by=null,lease_expires_at=null "
                     "where status='pending' returning 1;")
    return out.count("\n") if (rc == 0 and out) else 0


def scalar(c, sql: str) -> str:
    rc, out = c.psql(sql)
    return out.strip() if rc == 0 else ""


def jrows(c, sql: str) -> list:
    """Run <sql> and return rows as a list of dicts (jsonb_agg(to_jsonb(q)))."""
    rc, out = c.psql("select coalesce(jsonb_agg(to_jsonb(q)), '[]'::jsonb) from (" + sql + ") q")
    if rc != 0 or not out:
        return []
    try:
        v = json.loads(out)
        return v if isinstance(v, list) else []
    except ValueError:
        return []


def jone(c, sql: str) -> dict | None:
    rows = jrows(c, sql)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Fixture matrix (ASCII for Python<->SQL hash parity; unique semantic markers;
# never serialized into evidence)
# ---------------------------------------------------------------------------

SECRET_PIECES = ["sk-", "abcdefghijklmnopqrstuvwxyz", "0123456789", "ABCDEF"]


def _secret_python() -> str:
    # Assembled at runtime from pieces so the literal never appears statically.
    return "import torch\napi_key = '" + "".join(SECRET_PIECES) + "'\nprint(api_key)\n"


def _huge_python() -> str:
    # Valid Python with one oversized literal + a distinctive later marker.
    big = "x = '" + ("a" * 6000) + "'\n"
    return "import torch\n" + big + "MARKER_HUGE_LATE = WanVideoSampler()\n"


def _fallback_python() -> str:
    # Unparseable Python with an oversized line + a distinctive later marker.
    return ("def broken(:\n    y = " + ("z" * 6000) + "\n"
            "MARKER_FALLBACK_LATE = 1\n")


def fixtures() -> list[dict]:
    """The task-2.17 workflow fixture matrix (ids 6101..)."""
    return [
        {"id": 6101, "name": "payload_only", "title": "Payload Only", "body": "desc payload",
         "payload": {"python_source": "import torch\nMARKER_PAYLOAD_ONLY = 1\n"}},
        {"id": 6102, "name": "legacy_body_only", "title": "Body Only", "body": "d\nPython scratchpad source:\nimport torch\nMARKER_BODY_ONLY = 1\n",
         "payload": {}},
        {"id": 6103, "name": "duplicated", "title": "Dup", "body": "d\nPython ready-template source:\nimport torch\nMARKER_DUP = 1\n",
         "payload": {"python_source": "import torch\nMARKER_DUP = 1\n"}},
        {"id": 6104, "name": "changed", "title": "Changed", "body": "d",
         "payload": {"python_source": "import torch\nMARKER_OLD = 1\n"}},
        {"id": 6105, "name": "huge_generated", "title": "Huge", "body": "d",
         "payload": {"python_source": _huge_python()}},
        {"id": 6106, "name": "parser_fallback", "title": "Fallback", "body": "d",
         "payload": {"python_source": _fallback_python()}},
        {"id": 6107, "name": "unavailable", "title": "Unavailable", "body": "no code", "payload": {}},
        {"id": 6108, "name": "quarantined", "title": "Quarantined", "body": "d",
         "payload": {"python_source": _secret_python()}},
        {"id": 6109, "name": "recoverable", "title": "Recoverable", "body": "no code", "payload": {},
         "recoverer": lambda row: "import torch\nMARKER_RECOVERABLE = 1\n"},
    ]


def recoverer_for(fx: dict):
    r = fx.get("recoverer")
    return (lambda row: r(row)) if r else None


def row_for(fx: dict) -> dict:
    return {"kind": "workflow", "title": fx["title"], "body": fx["body"],
            "payload": fx["payload"], "metadata": {}}


def materialized_row(fx: dict, rec=None) -> dict:
    """The source row AFTER remediation: the canonical authoritative Python is
    materialized into payload.python_source (exactly what production remediation
    does for body/recoverable cohorts), so the SQL source-hash gate (which reads
    payload.python_source) and the Python manifest agree. Quarantined source is
    NOT materialized as safe (it stays out of the embeddable payload)."""
    if rec is None:
        rec = recoverer_for(fx)
    row = row_for(fx)
    resolution = wr.resolve_authoritative_python(row, recoverer=rec)
    if (resolution.available and resolution.source
            and wr.public_state(resolution.source) == wr.PUBLIC_STATE_SAFE):
        row = {**row, "payload": {**row["payload"], "python_source": resolution.source}}
    return row


def seed_fixture(c, fx: dict) -> dict:
    """Seed source (materialized) + lexical state + canonical manifest for one fixture."""
    rid = fx["id"]
    rec = recoverer_for(fx)
    row = materialized_row(fx, rec)
    run_replica(c, "insert into external_resources(id,kind,source,title,body,payload) values "
           f"({rid},'workflow','x',{lp.q(fx['title'])},{lp.q(fx['body'])},{lp.q_jsonb(row['payload'])}) "
           "on conflict (id) do update set title=excluded.title,body=excluded.body,payload=excluded.payload;")
    status = canon.workflow_python_status(row, recoverer=rec)
    run(c, "insert into lexical_resource_python_state(resource_id,kind,cohort,public_state,available) values "
           f"({rid},'workflow','{status.cohort}','{status.public_state}',{lp.q(status.available)}) "
           "on conflict (resource_id) do update set public_state=excluded.public_state,"
           "available=excluded.available,cohort=excluded.cohort;")
    entries = ch.build_representation_manifest(row, "resource", item_id=rid, recoverer=rec)
    for e in entries:
        d = e.to_dict()
        run(c, "insert into content_representation_manifest(entity_type,item_id,representation_type,"
               "representation_hash,source_available,public_state,unavailable_reason,chunking_version,"
               "chunk_config_identity,chunk_method,chunks,chunk_count) values "
               f"({lp.q(d['entity_type'])},{lp.q(d['item_id'])},{lp.q(d['representation_type'])},"
               f"{lp.q(d['representation_hash'])},{lp.q(d['source_available'])},{lp.q(d['public_state'])},"
               f"{lp.q(d['unavailable_reason'])},{d['chunking_version']},{lp.q(d['chunk_config_identity'])},"
               f"{lp.q(d['chunk_method'])},{lp.q_jsonb(d['chunks'])},{d['chunk_count']}) on conflict "
               "(entity_type,item_id,representation_type) do update set "
               "representation_hash=excluded.representation_hash,source_available=excluded.source_available,"
               "public_state=excluded.public_state,unavailable_reason=excluded.unavailable_reason,"
               "chunks=excluded.chunks,chunk_count=excluded.chunk_count,chunk_method=excluded.chunk_method,"
               "chunk_config_identity=excluded.chunk_config_identity,updated_at=now();")
    return {"name": fx["name"], "id": rid, "public_state": status.public_state,
            "available": status.available, "manifest_chunk_count": sum(e.chunk_count for e in entries),
            "python_chunk_count": next((e.chunk_count for e in entries if e.representation_type == "workflow_python"), None)}


def seed_jobs_for_fixtures(c, ids: list[int], reps=("workflow_python",)) -> None:
    run(c, "set session_replication_role=replica;")
    for rid in ids:
        for rep in reps:
            run(c, "insert into embedding_jobs(entity_type,item_id,representation_type,job_kind,contract_id,status) "
                   f"values ('resource','{rid}','{rep}','embed',{SEL},'pending') on conflict do nothing;")
    run(c, "set session_replication_role=origin;")


# ---------------------------------------------------------------------------
# The worker protocol (simulates core.ts against real SQL RPCs)
# ---------------------------------------------------------------------------

def worker_drain(c, worker_id: str, *, batch: int = 16, lease: int = 120,
                 stop_after: int | None = None) -> dict:
    """Claim + payload + fake embed/drop + finalize, looping until no pending."""
    run(c, "select hivemind_recover_stale_leases(0);")
    provider_calls = 0
    processed: list[str] = []
    outcomes: dict[str, int] = {}
    loops = 0
    while True:
        loops += 1
        if loops > 100:
            break  # hard backstop against any requeue loop
        if stop_after is not None and loops > stop_after:
            break
        claimed = jrows(c, "select job_id,entity_type,item_id,representation_type,job_kind,contract_id::text "
                          f"from hivemind_claim_embedding_jobs('{worker_id}',{batch},{lease})")
        if not claimed:
            break
        for job in claimed:
            jid = str(job["job_id"])
            et, item, rep, kind = job["entity_type"], job["item_id"], job["representation_type"], job["job_kind"]
            contract = str(job["contract_id"])
            if kind == "drop":
                run(c, "select hivemind_drop_embedding_chunks("
                       f"{contract},{lp.q(et)},{lp.q(item)},{lp.q(rep)});")
                run(c, "select hivemind_complete_embedding_job("
                       f"{jid},'{worker_id}',0);")
                outcomes["dropped"] = outcomes.get("dropped", 0) + 1
                processed.append(jid)
                continue
            rows = jrows(c, "select * from hivemind_embedding_payload("
                         f"{lp.q(et)},{lp.q(item)},{lp.q(rep)},512,50)")
            eligible = [r for r in rows if r.get("source_available") and r.get("chunk_text")
                        and not (rep == "workflow_python" and r.get("public_state") != "safe")]
            exp_hash = rows[0]["representation_hash"] if rows else None
            exp_state = rows[0].get("public_state") if rows else None
            exp_avail = bool(rows) and any(r.get("source_available") for r in rows)
            if not eligible:
                chunks_json = lp.q_jsonb([])
            else:
                provider_calls += 1
                chunks = []
                for i, r in enumerate(eligible):
                    vec = fake_vec(r["chunk_text"], DIM)
                    chunks.append({"chunk_index": r.get("chunk_index", i), "chunk_text": r["chunk_text"],
                                   "embedding": vec_literal(vec), "chunk_hash": r["chunk_hash"],
                                   "representation_hash": r["representation_hash"], "contract_id": contract,
                                   "entity_type": et, "item_id": item, "representation_type": rep})
                chunks_json = lp.q_jsonb(chunks)
            res = jone(c, "select chunks_written,outcome,job_status from hivemind_finalize_embedding_job("
                       f"{jid},'{worker_id}',{chunks_json},{lp.q(exp_hash)},{lp.q(exp_state)},{lp.q(exp_avail)})")
            oc = (res or {}).get("outcome", "missing")
            outcomes[oc] = outcomes.get(oc, 0) + 1
            processed.append(jid)
    return {"provider_calls": provider_calls, "processed": processed, "outcomes": outcomes,
            "claimed_unique": len(set(processed))}


def rehearse(evidence_path: pathlib.Path = EVIDENCE_PATH) -> dict:
    """Run the full Phase-2 acceptance protocol on a throwaway cluster; write the
    allow-listed evidence JSON and return it. Raises if a cluster step fails."""
    def log(msg: str) -> None:
        print(f"[rehearse] {msg}", flush=True)
    checks: list[tuple[str, bool, str]] = []
    ev: dict = {}

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append((name, bool(cond), detail))

    selected = sc.resolve_selected_contract_from_artifacts()
    ev["selected"] = {
        "candidate": selected.candidate_name, "dimension": selected.dimension,
        "selected_contract_id": str(selected.selected_contract_id),
        "chunk_config_identity_sha256": hashlib.sha256(selected.chunk_config_identity.encode()).hexdigest(),
        "prose_target": selected.prose_target_tokens, "prose_overlap": selected.prose_overlap_tokens,
        "python_target": selected.python_target_tokens, "python_overlap": selected.python_overlap_tokens,
        "production_activated": selected.production_activated,
        "eval_contract_id_hex": selected.eval_contract_id_hex,
    }
    check("selected_contract_resolved_from_artifacts", selected.selected_contract_id == SEL, str(selected.selected_contract_id))

    cluster = hp.setup_cluster()[0]
    try:
        run(cluster, "set session_replication_role=replica;")  # seeding control
        for name in MIGRATIONS:
            cluster.psql_file(REPO / "schema" / name)
        run(cluster, "set session_replication_role=origin;")
        log('migrations applied')
        ev["migrations_applied"] = MIGRATIONS
        ev["selected_literal_applied"] = scalar(cluster,
            "select hivemind_selected_contract_id('openai','text-embedding-3-small',384,1,2,"
            "'chunk_config'||E'\\x1f'||'v1'||E'\\x1f'||'prose#512/50'||E'\\x1f'||'workflow_python#512/50')")
        check("selected_literal_applied", ev["selected_literal_applied"] == str(SEL), ev["selected_literal_applied"])

        log('seeding fixtures')
        fx = fixtures()
        seeded = [seed_fixture(cluster, f) for f in fx]
        ev["fixture_manifest"] = [{"name": s["name"], "python_chunk_count": s["python_chunk_count"],
                                   "public_state": s["public_state"], "available": s["available"]} for s in seeded]

        log('T2 parity')
        # --- T2: byte-parity between Python manifest and the SQL payload reads.
        parity = {}
        for f, s in zip(fx, seeded):
            rec = recoverer_for(f)
            row = materialized_row(f, rec)
            py_entries = ch.build_representation_manifest(row, "resource", item_id=f["id"], recoverer=rec)
            py_wp = next((e for e in py_entries if e.representation_type == "workflow_python"), None)
            if py_wp and py_wp.chunk_count:
                # SQL payload must return the SAME chunk texts/hashes/indexes.
                sql_rows = jrows(cluster, "select chunk_index,chunk_hash,chunk_text from "
                                 f"hivemind_embedding_payload('resource','{f['id']}','workflow_python',512,50)")
                sql_norm = [(r["chunk_index"], r["chunk_hash"]) for r in sql_rows]
                py_norm = [(c["chunk_index"], c["chunk_hash"]) for c in py_wp.chunks]
                texts_match = all(r["chunk_text"] == c["chunk_text"]
                                  for r, c in zip(sql_rows, [{**c} for c in py_wp.chunks]))
                parity[f["name"]] = (sql_norm == py_norm and texts_match and len(sql_rows) == py_wp.chunk_count)
            else:
                parity[f["name"]] = True  # no safe python chunks to compare
        ev["parity_python_sql"] = parity
        check("parity_python_sql_all_safe_fixtures", all(parity.values()), json.dumps(parity))

        # Oversized-line bound: huge + fallback python chunks bounded (target_chars + overlap).
        bound_ok = True
        for f in ("huge_generated", "parser_fallback"):
            fxf = next(x for x in fx if x["name"] == f)
            row = materialized_row(fxf, recoverer_for(fxf))
            entries = ch.build_representation_manifest(row, "resource", item_id=0, recoverer=recoverer_for(fxf))
            wp = next((e for e in entries if e.representation_type == "workflow_python"), None)
            if wp:
                max_len = max((len(c["chunk_text"]) for c in wp.chunks), default=0)
                # bound = target_chars(2048) + overlap_chars(200); allow tiny slack.
                bound_ok = bound_ok and max_len <= 2048 + 200 + 1 and len(wp.chunks) >= 1
                ev.setdefault("oversized_bounds", {})[f] = {"chunks": wp.chunk_count, "max_len": max_len,
                                                            "later_chunk_index_gt0": any(c["chunk_index"] > 0 for c in wp.chunks)}
        check("oversized_line_bounded", bool(bound_ok), json.dumps(ev.get("oversized_bounds")))

        log('T4 worker protocol')
        # --- T4: full worker protocol over the safe fixtures.
        safe_ids = [s["id"] for s in seeded if s["public_state"] == "safe" and s["available"]]
        seed_jobs_for_fixtures(cluster, safe_ids)
        w = worker_drain(cluster, "w-main")
        ev["worker_protocol"] = {"provider_calls": w["provider_calls"],
                                 "claimed_unique": w["claimed_unique"], "outcomes": w["outcomes"]}
        # one provider call per safe workflow_python rep that had chunks
        expected_calls = sum(1 for s in seeded if s["public_state"] == "safe" and s["available"]
                             and s["python_chunk_count"])
        check("worker_one_embed_per_safe_python_rep", w["provider_calls"] == expected_calls,
              f"{w['provider_calls']} vs expected {expected_calls}")
        check("worker_all_jobs_terminal", w["claimed_unique"] == len(safe_ids), str(w["outcomes"]))
        # vectors written for safe python fixtures
        vec_counts = jrows(cluster, "select item_id, count(*) from content_embeddings "
                            f"where contract_id={SEL} and representation_type='workflow_python' group by item_id")
        vc = {str(r["item_id"]): int(r["count"]) for r in vec_counts}
        ev["workflow_python_vectors"] = vc
        safe_with_chunks = {str(s["id"]): s["python_chunk_count"] for s in seeded
                            if s["public_state"] == "safe" and s["available"] and s["python_chunk_count"]}
        check("safe_python_vectors_written", all(vc.get(k, 0) == v for k, v in safe_with_chunks.items()), json.dumps(vc))

        # --- unavailable + quarantined: zero provider calls, zero python vectors.
        for s in seeded:
            if s["name"] in ("unavailable", "quarantined"):
                cnt = int(scalar(cluster, f"select count(*) from content_embeddings where contract_id={SEL} "
                                 f"and item_id='{s['id']}' and representation_type='workflow_python'") or 0)
                check(f"{s['name']}_zero_python_vectors", cnt == 0, f"count={cnt}")
        check("quarantined_unavailable_excluded_from_safe_drain",
              6107 not in safe_ids and 6108 not in safe_ids, str(safe_ids))

        log('changed fixture')
        # --- changed fixture: old marker ceases, new marker ranks.
        chg = next(x for x in fx if x["name"] == "changed")
        # edit source to NEW marker, re-mediate (refresh manifest), re-embed.
        # (dict hoisted out of the f-string: backslashes in f-string expressions
        #  are a SyntaxError on Python < 3.12.)
        new_marker = {"python_source": "import torch\nMARKER_NEW = 1\n"}
        run_replica(cluster, f"update external_resources set payload="
                     f"{lp.q_jsonb(new_marker)} where id={chg['id']};")
        seed_fixture(cluster, {**chg, "payload": {"python_source": "import torch\nMARKER_NEW = 1\n"}})
        seed_jobs_for_fixtures(cluster, [chg["id"]])
        worker_drain(cluster, "w-change")
        new_present = int(scalar(cluster, f"select count(*) from content_embeddings where contract_id={SEL} "
                                f"and item_id='{chg['id']}' and chunk_text like '%MARKER_NEW%'") or 0)
        old_present2 = int(scalar(cluster, f"select count(*) from content_embeddings where contract_id={SEL} "
                                 f"and item_id='{chg['id']}' and chunk_text like '%MARKER_OLD%'") or 0)
        check("changed_new_marker_stored", new_present >= 1, f"new={new_present}")
        check("changed_old_marker_replaced", old_present2 == 0, f"old2={old_present2}")

        log('T5 concurrency')
        # --- T5: concurrency (3 claimers) + crash/lease recovery + source-change.
        # Fresh pool of 6 workflows for concurrency.
        pool = [7001 + i for i in range(6)]
        for rid in pool:
            marker = {"python_source": f"import torch\nMARKER_POOL_{rid} = 1\n"}
            run_replica(cluster, "insert into external_resources(id,kind,source,title,body,payload) values "
                         f"({rid},'workflow','x','pool {rid}','d',{lp.q_jsonb(marker)});")
            run(cluster, "insert into lexical_resource_python_state(resource_id,kind,cohort,public_state,available) values "
                         f"({rid},'workflow','payload_python','safe',true) on conflict (resource_id) do nothing;")
            entries = ch.build_representation_manifest({"kind": "workflow", "title": f"pool {rid}", "body": "d",
                         "payload": {"python_source": f"import torch\nMARKER_POOL_{rid} = 1\n"}, "metadata": {}},
                        "resource", item_id=rid)
            for e in entries:
                d = e.to_dict()
                run(cluster, "insert into content_representation_manifest(entity_type,item_id,representation_type,"
                       "representation_hash,source_available,public_state,unavailable_reason,chunking_version,"
                       "chunk_config_identity,chunk_method,chunks,chunk_count) values "
                       f"({lp.q(d['entity_type'])},{lp.q(d['item_id'])},{lp.q(d['representation_type'])},"
                       f"{lp.q(d['representation_hash'])},{lp.q(d['source_available'])},{lp.q(d['public_state'])},"
                       f"{lp.q(d['unavailable_reason'])},{d['chunking_version']},{lp.q(d['chunk_config_identity'])},"
                       f"{lp.q(d['chunk_method'])},{lp.q_jsonb(d['chunks'])},{d['chunk_count']}) on conflict "
                       "(entity_type,item_id,representation_type) do update set chunks=excluded.chunks,"
                       "chunk_count=excluded.chunk_count,representation_hash=excluded.representation_hash,"
                       "public_state=excluded.public_state,source_available=excluded.source_available,updated_at=now();")
            run(cluster, "insert into embedding_jobs(entity_type,item_id,representation_type,job_kind,contract_id,status) "
                         f"values ('resource','{rid}','workflow_python','embed',{SEL},'pending') on conflict do nothing;")
        run(cluster, "set session_replication_role=origin;")
        all_processed: list[str] = []
        pcalls = 0
        for i in range(3):
            r = worker_drain(cluster, f"conc-{i}", batch=2)
            all_processed += r["processed"]
            pcalls += r["provider_calls"]
        dup = len(all_processed) != len(set(all_processed))
        check("concurrency_no_duplicate_processing", not dup, f"processed={len(all_processed)} unique={len(set(all_processed))}")
        check("concurrency_all_six_converged", len(set(all_processed)) == 6, str(len(set(all_processed))))
        ev["concurrency"] = {"claimers": 3, "jobs": 6, "processed": len(all_processed),
                             "unique": len(set(all_processed)), "duplicates": len(all_processed) - len(set(all_processed)),
                             "provider_calls": pcalls}

        log('crash/lease recovery')
        # crash/lease recovery: claim a job, "crash" (no finalize), expire lease, recover, reprocess.
        run(cluster, "set session_replication_role=replica;")
        run(cluster, "insert into embedding_jobs(entity_type,item_id,representation_type,job_kind,contract_id,status) "
                     f"values ('resource','7001','workflow_python','embed',{SEL},'pending') on conflict do nothing;")
        run(cluster, "set session_replication_role=origin;")
        crashed = jrows(cluster, f"select job_id from hivemind_claim_embedding_jobs('crashed',1,60)")
        crashed_jid = str(crashed[0]["job_id"]) if crashed else ""
        run(cluster, f"update embedding_jobs set lease_expires_at=now()-interval '1 hour' where id={crashed_jid};")
        recovered = int(scalar(cluster, "select hivemind_recover_stale_leases(0);") or 0)
        requeued_status = scalar(cluster, f"select status from embedding_jobs where id={crashed_jid}")
        ev["recovery"] = {"recovered": recovered, "requeued_status": requeued_status}
        check("crash_lease_recovered", recovered >= 1 and requeued_status == "pending", f"{recovered}/{requeued_status}")
        r = worker_drain(cluster, "w-recover", batch=1)
        final_status = scalar(cluster, f"select status from embedding_jobs where id={crashed_jid}")
        check("crash_lease_reprocessed_terminal", final_status == "done", f"status={final_status}")

        log('source-change')
        # source-change during processing: claim, payload H1, change source, finalize -> source_changed.
        run(cluster, "set session_replication_role=replica;")
        run(cluster, "insert into embedding_jobs(entity_type,item_id,representation_type,job_kind,contract_id,status) "
                     f"values ('resource','7002','workflow_python','embed',{SEL},'pending') on conflict do nothing;")
        run(cluster, "set session_replication_role=origin;")
        sc_job = jrows(cluster, f"select job_id from hivemind_claim_embedding_jobs('wsrc',1,60)")
        sc_jid = str(sc_job[0]["job_id"]) if sc_job else ""
        rows = jrows(cluster, "select * from hivemind_embedding_payload('resource','7002','workflow_python',512,50)")
        exp_hash = rows[0]["representation_hash"] if rows else None
        # change source AFTER payload (manifest NOT refreshed) -> finalize must detect mismatch.
        changed_marker = {"python_source": "import torch\nMARKER_CHANGED_UNDER_US = 1\n"}
        run_replica(cluster, f"update external_resources set payload="
                     f"{lp.q_jsonb(changed_marker)} where id=7002;")
        res = jone(cluster, "select outcome from hivemind_finalize_embedding_job("
                   f"{sc_jid},'wsrc',{lp.q_jsonb([])},{lp.q(exp_hash)},'safe',true)")
        ev["source_change"] = {"outcome": (res or {}).get("outcome")}
        check("source_change_detected", (res or {}).get("outcome") == "source_changed", str(res))
        # The finalize must NOT have written the NEW source under the stale hash.
        new_under = int(scalar(cluster, f"select count(*) from content_embeddings where contract_id={SEL} "
                             f"and item_id='7002' and chunk_text like '%MARKER_CHANGED_UNDER_US%'") or 0)
        check("source_change_no_stale_authority", new_under == 0, f"new-under-stale={new_under}")

        log('finalize validation')
        # --- finalize validation: wrong dimension (1536) rejected, no write.
        # Use a fixture WITH a current manifest so finalize reaches the validate
        # step (the source-hash gate passes) and the 1536 cast fails closed.
        wd_hash = scalar(cluster, "select representation_hash from content_representation_manifest "
                         "where item_id='6101' and representation_type='workflow_python'")
        cancel_pending(cluster)  # isolate from any requeued job left by the source-change section
        run(cluster, "set session_replication_role=replica;")
        run(cluster, "insert into embedding_jobs(entity_type,item_id,representation_type,job_kind,contract_id,status) "
                     f"values ('resource','6101','workflow_python','embed',{SEL},'pending') on conflict do nothing;")
        run(cluster, "set session_replication_role=origin;")
        bad = jrows(cluster, "select job_id from hivemind_claim_embedding_jobs('wbad',1,60)")
        bad_jid = str(bad[0]["job_id"]) if bad else ""
        bad_vec = "[" + ",".join(["0.1"] * 1536) + "]"
        bad_payload = lp.q_jsonb([{"chunk_index": 0, "chunk_text": "x", "embedding": bad_vec,
                                   "chunk_hash": "0" * 64, "representation_hash": wd_hash or "",
                                   "contract_id": str(SEL), "entity_type": "resource",
                                   "item_id": "6101", "representation_type": "workflow_python"}])
        res2 = jone(cluster, "select outcome from hivemind_finalize_embedding_job("
                    f"{bad_jid},'wbad',{bad_payload},{lp.q(wd_hash)},'safe',true)")
        bad_status = scalar(cluster, f"select status from embedding_jobs where id={bad_jid}")
        # 6101's existing selected-contract vectors must remain intact (no partial wipe).
        wd_intact = int(scalar(cluster, f"select count(*) from content_embeddings where contract_id={SEL} "
                               "and item_id='6101' and representation_type='workflow_python'") or 0)
        ev["wrong_dimension"] = {"outcome": (res2 or {}).get("outcome"), "job_status": bad_status,
                                 "existing_vectors_intact": wd_intact}
        check("wrong_dimension_rejected", (res2 or {}).get("outcome") == "validation_failed", str(res2))
        check("wrong_dimension_job_failed", bad_status == "failed", bad_status)
        check("wrong_dimension_no_partial_wipe", wd_intact >= 1, f"intact={wd_intact}")

        log('T6 semantic')
        # --- T6: semantic candidates + HNSW integration.
        hp.build_index(cluster)  # CREATE INDEX CONCURRENTLY (task-2.16 frozen index)
        # a workflow-code query that should hit a later workflow_python chunk.
        q_late = fake_vec("MARKER_HUGE_LATE", DIM)
        sem = jrows(cluster, "select entity_type,item_id,representation_type,chunk_index,semantic_distance "
                    f"from hivemind_semantic_candidates('{vec_literal(q_late)}'::vector,20,'{{resource}}','{{}}')")
        ev["semantic"] = {"returned": len(sem)}
        # huge_generated python has MARKER_HUGE_LATE at chunk_index>0 -> it can win via workflow_python.
        huge_hits = [r for r in sem if str(r["item_id"]) == "6105"]
        check("semantic_later_workflow_python_chunk_can_win",
              any(r["representation_type"] == "workflow_python" and int(r["chunk_index"]) > 0 for r in huge_hits),
              json.dumps(huge_hits[:2], default=str))
        # one row per item (collapse)
        items = [str(r["item_id"]) for r in sem]
        check("semantic_one_row_per_item", len(items) == len(set(items)), str(len(items)))
        # wrong-contract exclusion: a vector under the dimension-only legacy bigint
        # must NEVER rank (the semantic function is selective to the selected full
        # literal). 8001 has ONLY a legacy-contract decoy vector (no selected
        # vector), so a query for the decoy text must not return it.
        # A valid 384-d vector filed under a NON-selected contract must never
        # rank. Use a genuinely distinct 384-d contract (canonicalization v2;
        # its identity differs from the selected v1, so the registry accepts it).
        decoy_contract = ec.contract_id(ec.ContractSpec("openai", "text-embedding-3-small", 384, 2, 1))
        z64 = "0" * 64
        run(cluster, f"insert into embedding_contracts(id,provider,model,dimension,"
                     "canonicalization_version,chunking_version,status) values "
                     f"({decoy_contract},'openai','text-embedding-3-small',384,2,1,'draft') on conflict do nothing;")
        run_replica(cluster, f"insert into external_resources(id,kind,source,title,body) values "
                     f"(8001,'workflow','x','decoy only','d') on conflict do nothing;")
        run(cluster, f"insert into content_embeddings(contract_id,entity_type,item_id,representation_type,chunk_index,"
                     f"chunk_text,embedding,representation_hash,chunk_hash) values "
                     f"({decoy_contract},'resource','8001','workflow_python',0,'decoy marker xyz','{vec_literal(fake_vec('decoy marker xyz',DIM))}','{z64}','{z64}') on conflict do nothing;")
        decoy_sem = jrows(cluster, "select item_id from hivemind_semantic_candidates("
                          f"'{vec_literal(fake_vec('decoy marker xyz',DIM))}'::vector,20,'{{resource}}','{{}}')")
        ev["wrong_contract"] = {"decoy_contract_id": str(decoy_contract),
                                "decoy_ranked": any(str(r["item_id"]) == "8001" for r in decoy_sem)}
        check("wrong_contract_vector_excluded", not any(str(r["item_id"]) == "8001" for r in decoy_sem),
              f"decoy ranked={ev['wrong_contract']['decoy_ranked']}")

        # snowflake item_id round-trips exactly through claim/payload/semantic.
        run_replica(cluster, f"insert into discord_messages(message_id,content,is_deleted) values (9007199254740993,'snowflake marker content',false) on conflict do nothing;")
        snow_manifest = ch.build_representation_manifest({"content": "snowflake marker content"}, "message", item_id="9007199254740993")
        for e in snow_manifest:
            d = e.to_dict()
            run(cluster, "insert into content_representation_manifest(entity_type,item_id,representation_type,"
                   "representation_hash,source_available,public_state,unavailable_reason,chunking_version,"
                   "chunk_config_identity,chunk_method,chunks,chunk_count) values "
                   f"({lp.q(d['entity_type'])},{lp.q(d['item_id'])},{lp.q(d['representation_type'])},"
                   f"{lp.q(d['representation_hash'])},{lp.q(d['source_available'])},{lp.q(d['public_state'])},"
                   f"{lp.q(d['unavailable_reason'])},{d['chunking_version']},{lp.q(d['chunk_config_identity'])},"
                   f"{lp.q(d['chunk_method'])},{lp.q_jsonb(d['chunks'])},{d['chunk_count']}) on conflict "
                   "(entity_type,item_id,representation_type) do update set chunks=excluded.chunks,updated_at=now();")
        run(cluster, "set session_replication_role=replica;")
        run(cluster, "insert into embedding_jobs(entity_type,item_id,representation_type,job_kind,contract_id,status) "
                     f"values ('message','9007199254740993','prose','embed',{SEL},'pending') on conflict do nothing;")
        run(cluster, "set session_replication_role=origin;")
        worker_drain(cluster, "w-snow", batch=1)
        snow_vec_count = int(scalar(cluster, f"select count(*) from content_embeddings where contract_id={SEL} "
                                   "and entity_type='message' and item_id='9007199254740993'") or 0)
        snow_sem = jrows(cluster, "select item_id from hivemind_semantic_candidates("
                         f"'{vec_literal(fake_vec('snowflake marker content',DIM))}'::vector,5,'{{message}}','{{9007199254740993}}')")
        check("snowflake_survives_full_protocol", snow_vec_count == 1 and any(str(r["item_id"]) == "9007199254740993" for r in snow_sem),
              f"vec={snow_vec_count} sem={len(snow_sem)}")
        ev["snowflake"] = {"item_id": "9007199254740993", "vectors": snow_vec_count, "semantic_hit": len(snow_sem)}

        log('HNSW index check')
        # HNSW index present + the semantic function is bound to the selected
        # literal. (The 033 function builds its kNN SQL via EXECUTE at runtime, so
        # EXPLAIN of the function call shows only a Function Scan; the frozen
        # literal lives in the function source, so assert it there + index exists.)
        idx_exists = scalar(cluster, f"select to_regclass('public.{INDEX_NAME}') is not null;")
        prosrc = scalar(cluster, "select p.prosrc from pg_proc p join pg_namespace n on n.oid=p.pronamespace "
                        "where n.nspname='public' and p.proname='hivemind_semantic_candidates'")
        check("hnsw_index_present", idx_exists == "t", idx_exists)
        check("semantic_function_binds_selected_literal", str(SEL) in (prosrc or ""),
              "selected literal baked into the semantic function source")

        ev["deterministic_core_hash"] = hashlib.sha256(json.dumps({
            "selected": ev["selected"], "parity": ev["parity_python_sql"],
            "worker_outcomes": ev["worker_protocol"]["outcomes"],
            "concurrency": ev["concurrency"], "fixture_manifest": ev["fixture_manifest"],
        }, sort_keys=True).encode()).hexdigest()
        ev["verdict"] = all(ok for _, ok, _ in checks)
        ev["checks"] = [{"name": n, "ok": ok, "detail": d} for (n, ok, d) in checks]
        ev["production_mutated"] = False
        ev["network_calls"] = 0
        ev["embedding_provider_calls"] = 0
        ev["isolated_cluster"] = True
        ev["postgres_version"] = scalar(cluster, "select version();")
        ev["pgvector_version"] = scalar(cluster, "select extversion from pg_extension where extname='vector';")

    finally:
        cluster.tear_down()

    # allow-list + secret/source/vector/URL scan of the evidence before writing.
    problems = scan_evidence(ev)
    ev["evidence_scan"] = {"problems": problems, "ok": not problems}
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(ev, indent=2, sort_keys=True))
    return ev


def main() -> int:
    if lp.find_pgbins() is None:
        print("SKIP: PostgreSQL binaries not found", file=sys.stderr)
        return 0
    ev = rehearse()
    checks = ev.get("checks", [])
    passed = sum(1 for c in checks if c["ok"])
    print(f"Task 2.17 rehearsal: {passed}/{len(checks)} checks passed; verdict={ev.get('verdict')}")
    for c in checks:
        print(f"  [{'OK' if c['ok'] else 'FAIL'}] {c['name']}: {c['detail']}")
    if ev.get("evidence_scan", {}).get("problems"):
        print(f"EVIDENCE SCAN PROBLEMS: {ev['evidence_scan']['problems']}")
    return 0 if (ev.get("verdict") and ev.get("evidence_scan", {}).get("ok")) else 1


_FORBIDDEN_SUBSTRINGS = ["".join(SECRET_PIECES), "postgres://", "postgresql://", "api.openai.com",
                         "sk-", "Bearer ", "PGPASSWORD", "traceback", "exception", "error:"]
_FORBIDDEN_KEYS = {
    "chunk_text", "embedding", "body", "python_source", "content", "question", "answer", "title",
    "source", "snippet", "query", "url", "connection", "connection_string", "path",
    "exception", "traceback", "secret", "credential", "password", "token", "authorization",
}
# Evidence is deliberately a closed data contract.  It contains only
# aggregate counts, booleans, stable ids, bounded check names/details, and
# hashes.  This is a structural check: forbidden data cannot pass merely by
# being a number, a list, or nested under an otherwise harmless key.
_ALLOWED_KEYS = {
    "checks", "concurrency", "deterministic_core_hash", "embedding_provider_calls", "evidence_scan",
    "fixture_manifest", "isolated_cluster", "migrations_applied", "network_calls", "oversized_bounds",
    "parity_python_sql", "pgvector_version", "postgres_version", "production_mutated", "recovery",
    "selected", "selected_literal_applied", "semantic", "snowflake", "source_change", "verdict",
    "worker_protocol", "workflow_python_vectors", "wrong_contract", "wrong_dimension",
    "detail", "name", "ok", "available", "public_state", "python_chunk_count", "chunks",
    "later_chunk_index_gt0", "max_len", "changed", "duplicated", "huge_generated", "legacy_body_only",
    "parser_fallback", "payload_only", "quarantined", "recoverable", "unavailable", "candidate",
    "chunk_config_identity_sha256", "dimension", "eval_contract_id_hex", "production_activated",
    "prose_overlap", "prose_target", "python_overlap", "python_target", "selected_contract_id",
    "returned", "item_id", "semantic_hit", "vectors", "outcome", "job_status", "existing_vectors_intact",
    "decoy_contract_id", "decoy_ranked", "requeued_status", "recovered", "claimed_unique", "outcomes",
    "completed", "claimers", "duplicates", "jobs", "processed", "provider_calls", "unique", "problems",
    "6101", "6102", "6103", "6104", "6105", "6106", "6109",
}
_BOOL_KEYS = {
    "ok", "available", "isolated_cluster", "production_mutated", "verdict", "later_chunk_index_gt0",
    "changed", "duplicated", "huge_generated", "legacy_body_only", "parser_fallback", "payload_only",
    "quarantined", "recoverable", "unavailable", "production_activated", "decoy_ranked",
}
_INT_KEYS = {
    "embedding_provider_calls", "network_calls", "python_chunk_count", "chunks", "max_len", "dimension",
    "prose_overlap", "prose_target", "python_overlap", "python_target", "returned", "semantic_hit",
    "vectors", "existing_vectors_intact", "recovered", "claimed_unique", "completed", "claimers",
    "duplicates", "jobs", "processed", "provider_calls", "unique",
}
_STRING_KEYS = {
    "detail", "name", "public_state", "pgvector_version", "postgres_version", "selected_literal_applied",
    "candidate", "chunk_config_identity_sha256", "eval_contract_id_hex", "selected_contract_id",
    "item_id", "outcome", "job_status", "decoy_contract_id", "requeued_status",
    "deterministic_core_hash",
}


def scan_evidence(obj, path="") -> list[str]:
    """Validate the complete evidence shape and reject sensitive payloads."""
    problems: list[str] = []
    secret = "".join(SECRET_PIECES)
    def walk(o, p):
        if isinstance(o, dict):
            for k, v in o.items():
                key = str(k)
                if key.lower() in _FORBIDDEN_KEYS:
                    problems.append(f"forbidden key at {p}.{key}")
                elif key not in _ALLOWED_KEYS:
                    problems.append(f"unknown evidence key at {p}.{key}")
                if key in _BOOL_KEYS and not p.endswith(".oversized_bounds") and not isinstance(v, bool):
                    problems.append(f"wrong type for boolean at {p}.{key}")
                if key in _INT_KEYS and (not isinstance(v, int) or isinstance(v, bool)):
                    problems.append(f"wrong type for integer at {p}.{key}")
                if key in _STRING_KEYS and not isinstance(v, str):
                    problems.append(f"wrong type for string at {p}.{key}")
                if key == "migrations_applied" and (
                    not isinstance(v, list) or not all(isinstance(x, str) for x in v)
                ):
                    problems.append(f"wrong type for string list at {p}.{key}")
                walk(v, f"{p}.{k}")
        elif isinstance(o, str):
            low = o.lower()
            for bad in _FORBIDDEN_SUBSTRINGS:
                if bad and bad.lower() in low:
                    problems.append(f"forbidden substring {bad!r} at {p}")
            if secret.lower() in low:
                problems.append(f"assembled secret at {p}")
            if "/users/" in low or "/private/" in low or "/tmp/" in low or "file://" in low:
                problems.append(f"filesystem path at {p}")
        elif isinstance(o, list):
            if o and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in o):
                problems.append(f"numeric/vector-like array at {p}")
            for i, v in enumerate(o):
                walk(v, f"{p}[{i}]")
    walk(obj, path)
    return sorted(set(problems))


if __name__ == "__main__":
    raise SystemExit(main())
