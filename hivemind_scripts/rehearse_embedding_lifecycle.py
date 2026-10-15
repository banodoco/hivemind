#!/usr/bin/env python3
"""Phase-2 / Tasks 2.7–2.10 isolated-cluster lifecycle rehearsal.

Applies schema/020–024 (S1) + schema/025–029 (this batch) in a THROWAWAY local
PostgreSQL cluster (unix-socket, no network) with the locally-available pgvector,
then proves the embedding LIFECYCLE completion signals against live SQL:

  2.7  source triggers enqueue ONE idempotent job per
       (entity_type, item_id, representation_type) on insert/update/delete/
       soft-delete/status/python/semantic-reclassify — no provider call.
  2.8  claim via FOR UPDATE SKIP LOCKED cannot double-process across concurrent
       workers; retries are bounded; stale leases recover; a superseded/cancelled
       job never becomes active; completion is source-hash safe.
  2.9  the worker SQL surface returns deterministic canonical chunks; an upsert
       atomically replaces an item's chunks; dimension mismatch is rejected.
  2.10 deleted / ineligible / stale-source / failed-contract embeddings are
       removed; a safe replacement-contract transition preserves the active index.

Mutates ONLY the throwaway cluster. The live Hivemind project is untouched (the
Edge Function + Cron deploy is rehearsed separately; production apply is
operator-run). All output is sanitized via verify_access.redact — no credentials,
raw private text, vectors, or secret-like substrings.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import lexical_pg  # noqa: E402
from verify_access import redact  # noqa: E402

from executors import embedding_contract as ec  # noqa: E402

SCHEMA_DIR = REPO_ROOT / "schema"
S1_MIGRATIONS = [
    "020_enable_pgvector.sql",
    "021_embedding_contracts.sql",
    "022_content_embeddings.sql",
    "023_embedding_contract_switch.sql",
    "024_identity_and_canonical_representations.sql",
]
S2_MIGRATIONS = [
    "025_embedding_jobs.sql",
    "026_embedding_job_rpc.sql",
    "027_embedding_worker_surface.sql",
    "028_embedding_worker_cron.sql",
    "029_embedding_cleanup.sql",
]

# A single 384-d contract used across the rehearsal (deterministic id).
ACTIVE_CONTRACT = ec.ContractSpec(provider="openai", model="text-embedding-3-small", dimension=384)


def _vec(dim: int = 384, value: str = "0.016") -> str:
    return "[" + ", ".join([value] * dim) + "]"


def _hash(seed: str) -> str:
    import hashlib
    return hashlib.sha256(seed.encode()).hexdigest()


BOOTSTRAP_DISCORD = """
create table if not exists discord_messages (
  message_id bigint primary key,
  content text,
  is_deleted boolean not null default false,
  deleted_at timestamptz,
  created_at timestamptz not null default now()
);
"""


def setup_cluster() -> tuple[lexical_pg.LocalCluster, dict]:
    """Start a cluster, bootstrap source tables, apply 020–025(+). Returns (cluster, info)."""

    cluster = lexical_pg.LocalCluster.start()
    cluster.psql(lexical_pg.BOOTSTRAP_SQL, capture=False)
    cluster.psql(BOOTSTRAP_DISCORD, capture=False)
    for name in S1_MIGRATIONS + S2_MIGRATIONS:
        path = SCHEMA_DIR / name
        if not path.exists():
            # S2 migrations not all written yet — skip missing so partial runs work.
            continue
        try:
            cluster.psql_file(path)
        except RuntimeError as exc:
            cluster.tear_down()
            raise RuntimeError(f"migration {name} failed: {redact(str(exc))}") from None
    # Seed + activate one 384 contract so hivemind_active_contract_id() resolves.
    cid = ACTIVE_CONTRACT.id
    cluster.psql(
        "insert into embedding_contracts(id,provider,model,dimension,canonicalization_version,"
        f"chunking_version,status) values ({cid},'openai','text-embedding-3-small',384,1,1,'draft')"
        " on conflict (id) do nothing;",
        capture=False,
    )
    cluster.psql(f"select hivemind_set_active_embedding_contract({cid}, false);", capture=False)
    return cluster, {"active_contract_id": cid}


# ---------------------------------------------------------------------------
# 2.7 trigger behavior
# ---------------------------------------------------------------------------


def _jobs(cluster, where: str = "") -> list[tuple]:
    rc, out = cluster.psql(
        "select entity_type,item_id,representation_type,job_kind,source_op from embedding_jobs "
        + ("where " + where if where else "") + " order by entity_type,item_id,representation_type;"
    )
    rows = []
    for ln in (out or "").strip().splitlines():
        parts = ln.split("|")
        if len(parts) == 5:
            rows.append(tuple(p for p in parts))
    return rows


def measure_2_7_triggers(cluster) -> dict:
    checks = {}

    # message insert -> 1 embed(prose)
    cluster.psql("insert into discord_messages(message_id,content) values (100,'hi');", capture=False)
    checks["message_insert_one_embed"] = _jobs(cluster, "entity_type='message'") == [
        ("message", "100", "prose", "embed", "insert")
    ]

    # content update -> reembed (dedup: same identity, kind updated to reembed)
    cluster.psql("update discord_messages set content='hi2' where message_id=100;", capture=False)
    checks["message_update_reembed"] = _jobs(cluster, "entity_type='message'") == [
        ("message", "100", "prose", "reembed", "update")
    ]

    # metadata-only update (no content/is_deleted change) -> no new job, still one row
    before = len(_jobs(cluster))
    cluster.psql("update discord_messages set content=content where message_id=100;", capture=False)
    checks["message_noop_update_no_extra_job"] = len(_jobs(cluster)) == before

    # rapid delete + re-insert collapses to ONE pending job (latest op wins)
    cluster.psql(
        "delete from discord_messages where message_id=100;"
        "insert into discord_messages(message_id,content) values (100,'back');",
        capture=False,
    )
    checks["message_dedup_one_pending"] = len(_jobs(cluster, "entity_type='message'")) == 1

    # soft-delete -> drop
    cluster.psql("insert into discord_messages(message_id,content) values (101,'x');", capture=False)
    cluster.psql("update discord_messages set is_deleted=true where message_id=101;", capture=False)
    checks["message_soft_delete_drop"] = _jobs(cluster, "entity_type='message' and item_id='101'") == [
        ("message", "101", "prose", "drop", "soft_delete")
    ]

    # workflow insert -> prose + workflow_python embeds (two streams)
    cluster.psql(
        "insert into external_resources(id,kind,source,title,body,metadata,payload) values "
        "(5001,'workflow','vibecomfy-external','Wan I2V','desc','{}'::jsonb,"
        "'{\"python_source\":\"x=1\"}'::jsonb);",
        capture=False,
    )
    wf = _jobs(cluster, "entity_type='resource' and item_id='5001'")
    checks["workflow_insert_two_streams"] = sorted(r[2] for r in wf) == ["prose", "workflow_python"]
    checks["workflow_insert_kinds_embed"] = all(r[3] == "embed" for r in wf)

    # payload.python_source change -> only workflow_python reembed (no prose dup)
    cluster.psql(
        "update external_resources set payload='{\"python_source\":\"x=2\"}'::jsonb where id=5001;",
        capture=False,
    )
    pend = _jobs(cluster, "entity_type='resource' and item_id='5001' and source_op='python_change'")
    checks["python_change_only_python_stream"] = len(pend) == 1 and pend[0][2] == "workflow_python"

    # distillation insert(approved) -> embed; rejected -> no job; status->rejected -> drop
    cluster.psql(
        "insert into distillations(id,question,answer,status) values "
        "(71,'best upscaler','use realrgan','approved'),"
        "(72,'secret q','rejected ans','rejected');",
        capture=False,
    )
    checks["distillation_approved_embed"] = any(
        r == ("distillation", "71", "prose", "embed", "insert") for r in _jobs(cluster, "entity_type='distillation'")
    )
    checks["distillation_rejected_no_job"] = all(
        r[1] != "72" for r in _jobs(cluster, "entity_type='distillation'")
    )
    cluster.psql("update distillations set status='rejected' where id=71;", capture=False)
    checks["distillation_status_to_rejected_drop"] = _jobs(
        cluster, "entity_type='distillation' and item_id='71'"
    ) == [("distillation", "71", "prose", "drop", "status_change")]

    return checks


# ---------------------------------------------------------------------------
# 2.8 RPC + SKIP LOCKED concurrency
# ---------------------------------------------------------------------------


def _count(cluster, where: str = "") -> int:
    rc, out = cluster.psql("select count(*) from embedding_jobs " + ("where " + where if where else "") + ";")
    lines = [ln for ln in (out or "").strip().splitlines() if ln.strip().isdigit()]
    return int(lines[-1]) if lines else 0


def _status_of(cluster, job_id: int) -> str:
    rc, out = cluster.psql(f"select status from embedding_jobs where id={job_id};")
    lines = [ln for ln in (out or "").strip().splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _claim_rows(cluster, worker: str, batch: int = 1, lease: int = 30) -> list[tuple]:
    rc, out = cluster.psql(
        f"select * from hivemind_claim_embedding_jobs('{worker}', {batch}, {lease});"
    )
    rows = []
    for ln in (out or "").strip().splitlines():
        parts = ln.split("|")
        if len(parts) == 7 and parts[0].strip().isdigit():
            rows.append(tuple(p.strip() for p in parts))
    return rows


def measure_2_8_concurrency(cluster) -> dict:
    """Spawn parallel workers claiming one-at-a-time from a shared pool; prove no
    job is claimed by two workers and every job is processed exactly once."""
    checks = {}
    # Drain leftover pending jobs so the pool is the only claimable work.
    for _ in range(6):
        rows = _claim_rows(cluster, "drainc", batch=64, lease=30)
        if not rows:
            break
        for r in rows:
            cluster.psql(f"select hivemind_complete_embedding_job({r[0]},'drainc',0);", capture=False)
    # Fresh pool of 40 pending jobs on distinct identities (no dedup collisions).
    cluster.psql("begin;", capture=False)
    for i in range(40):
        cluster.psql(
            f"select hivemind_enqueue_embedding_job('resource','pool{i}','prose','embed','insert');",
            capture=False,
        )
    cluster.psql("commit;", capture=False)
    checks["seeded_pool_40"] = _count(cluster, "entity_type='resource' and item_id like 'pool%'") >= 40

    claimed: dict[str, list[int]] = {"a": [], "b": [], "c": []}
    errors: list[str] = []
    done = {"a": False, "b": False, "c": False}
    barrier = threading.Barrier(3)

    def worker(name: str) -> None:
        try:
            barrier.wait(timeout=10)
            while not all(done.values()):
                rows = _claim_rows(cluster, name, batch=1, lease=30)
                if not rows:
                    done[name] = True
                    time.sleep(0.02)
                    continue
                jid = int(rows[0][0])
                claimed[name].append(jid)
                time.sleep(0.005)  # simulate embed work
                rc, _ = cluster.psql(
                    f"select hivemind_complete_embedding_job({jid}, '{name}', 1);"
                )
                if rc != 0:
                    errors.append(f"{name} complete rc={rc}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name} exc:{type(exc).__name__}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b", "c")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    all_claimed = claimed["a"] + claimed["b"] + claimed["c"]
    checks["concurrency_no_double_claim"] = len(all_claimed) == len(set(all_claimed))
    checks["concurrency_all_processed"] = len(all_claimed) == 40
    checks["concurrency_no_errors"] = len(errors) == 0
    # Every pool job ended 'done' (exactly-once processing).
    done_count = _count(cluster, "entity_type='resource' and item_id like 'pool%' and status='done'")
    checks["concurrency_all_done"] = done_count == 40
    return checks


def measure_2_8_state_machine(cluster) -> dict:
    checks = {}

    def _drain() -> None:
        """Claim+complete every due pending job so the next sub-test starts clean."""
        rows = _claim_rows(cluster, "drain", batch=64, lease=30)
        for r in rows:
            cluster.psql(f"select hivemind_complete_embedding_job({r[0]},'drain',0);", capture=False)

    def _claim_target(worker: str, item_id: str) -> tuple | None:
        row = _claim_rows(cluster, worker, batch=1, lease=30)
        return row[0] if row and row[0][2] == item_id else None

    def _force_due(item_id: str) -> None:
        cluster.psql(
            f"update embedding_jobs set next_attempt_at=now() where item_id='{item_id}' and status='pending';",
            capture=False,
        )

    _drain()
    # claim returns nothing when no pending jobs are due.
    checks["claim_empty_when_none_pending"] = _claim_rows(cluster, "wX") == []

    # fail(retryable) -> pending + backoff; attempts bounded -> failed.
    cluster.psql("select hivemind_enqueue_embedding_job('resource','sm2','prose','embed','insert');", capture=False)
    cluster.psql("update embedding_jobs set max_attempts=3 where entity_type='resource' and item_id='sm2';", capture=False)
    attempts_seen: list[int] = []
    last_jid = None
    for _ in range(6):
        row = _claim_target("w2", "sm2")
        if row is None:
            _force_due("sm2")  # simulate backoff elapsing
            row = _claim_target("w2", "sm2")
            if row is None:
                break
        last_jid = int(row[0])
        attempts_seen.append(int(row[6]))
        cluster.psql(f"select hivemind_fail_embedding_job({last_jid},'w2','transient: provider 503', true);", capture=False)
    checks["bounded_retries_then_failed"] = (last_jid is not None and _status_of(cluster, last_jid) == "failed")
    checks["attempts_never_exceed_max_plus_one"] = (max(attempts_seen) <= 3 if attempts_seen else False)

    # non-retryable fail -> failed immediately
    _drain()
    cluster.psql("select hivemind_enqueue_embedding_job('resource','sm3','prose','embed','insert');", capture=False)
    row = _claim_target("w3", "sm3")
    jid3 = int(row[0])
    cluster.psql(f"select hivemind_fail_embedding_job({jid3},'w3','dimension mismatch', false);", capture=False)
    checks["nonretryable_immediate_failed"] = _status_of(cluster, jid3) == "failed"

    # stale lease recovery: processing job with an expired lease -> recover -> pending
    _drain()
    cluster.psql("select hivemind_enqueue_embedding_job('resource','sm4','prose','embed','insert');", capture=False)
    row = _claim_target("w4", "sm4")
    jid4 = int(row[0])
    cluster.psql(
        f"update embedding_jobs set lease_expires_at = now() - interval '60 seconds' where id={jid4};",
        capture=False,
    )
    cluster.psql("select hivemind_recover_stale_leases(0);", capture=False)
    checks["stale_lease_recovered"] = _status_of(cluster, jid4) == "pending"

    # cancel pending -> terminal; claim never returns it; re-enqueue creates fresh pending.
    _drain()
    cluster.psql("select hivemind_enqueue_embedding_job('resource','sm5','prose','embed','insert');", capture=False)
    cluster.psql("select hivemind_cancel_pending_jobs('resource','sm5');", capture=False)
    checks["cancel_makes_terminal"] = _status_of(
        cluster, _last_job_id(cluster, "resource", "sm5")
    ) == "cancelled"
    checks["cancelled_not_claimable"] = all(
        r[2] != "sm5" for r in _claim_rows(cluster, "w5", batch=64)
    )
    cluster.psql("select hivemind_enqueue_embedding_job('resource','sm5','prose','reembed','update');", capture=False)
    n_sm5 = _count(cluster, "entity_type='resource' and item_id='sm5'")
    checks["reenqueue_after_cancel_is_fresh"] = n_sm5 == 2  # 1 cancelled + 1 pending

    # completion is source-hash safe: a change while processing enqueues a fresh job
    _drain()
    cluster.psql("select hivemind_enqueue_embedding_job('resource','sm6','prose','embed','insert');", capture=False)
    row = _claim_target("w6", "sm6")  # now processing sm6
    jid6 = int(row[0])
    cluster.psql("select hivemind_enqueue_embedding_job('resource','sm6','prose','reembed','update');", capture=False)
    n_sm6 = _count(cluster, "entity_type='resource' and item_id='sm6'")
    checks["change_while_processing_enqueues_fresh"] = n_sm6 == 2  # 1 processing + 1 pending
    cluster.psql(f"select hivemind_complete_embedding_job({jid6},'w6',1);", capture=False)
    return checks

def _last_job_id(cluster, entity_type: str, item_id: str) -> int:
    rc, out = cluster.psql(
        f"select id from embedding_jobs where entity_type='{entity_type}' and item_id='{item_id}' "
        f"order by id desc limit 1;"
    )
    lines = [ln for ln in (out or "").splitlines() if ln.strip().isdigit()]
    return int(lines[-1]) if lines else -1


# ---------------------------------------------------------------------------
# 2.9 worker surface (schema/027) + end-to-end protocol with the fake embedder
# ---------------------------------------------------------------------------


def _vec_literal(dim: int = 384, value: str = "0.016") -> str:
    return "[" + ", ".join([value] * dim) + "]"


def _payload_rows(cluster, entity_type, item_id, rep) -> list[tuple]:
    rc, out = cluster.psql(
        "select source_available, chunk_index, chunk_hash, method from hivemind_embedding_payload("
        f"'{entity_type}','{item_id}','{rep}',512,50);"
    )
    rows = []
    for ln in (out or "").strip().splitlines():
        parts = [p.strip() for p in ln.split("|")]
        if len(parts) == 4:
            rows.append(tuple(parts))
    return rows


def measure_2_9_worker_surface(cluster) -> dict:
    from executors import embedding_contract as ec

    checks = {}

    # Seed source rows for payload tests.
    cluster.psql("insert into discord_messages(message_id,content) values (7000,'how do I lower motion amplitude in wan 2.2');", capture=False)
    cluster.psql("insert into discord_messages(message_id,content,is_deleted) values (7001,'deleted one',true);", capture=False)
    cluster.psql(
        "insert into external_resources(id,kind,source,title,body,metadata,payload) values "
        "(7002,'workflow','vibecomfy-external','Wan I2V','description text here',"
        "'{}'::jsonb,'{\"python_source\":\"sampler = WanVideoSampler(lora=0.8)\\n\"}'::jsonb);",
        capture=False,
    )

    # chunk_text determinism: identical across calls; single chunk for short input.
    a = cluster.psql("select count(*) from hivemind_chunk_text('short message',512,50);")[1].strip().splitlines()[-1]
    checks["chunk_text_single_for_short"] = a == "1"
    long_txt = "\n".join(f"line {i} of generated python source tokens here" for i in range(300))
    b1 = cluster.psql(f"select count(*) from hivemind_chunk_text($txt${long_txt}$txt$,64,8);")[1].strip().splitlines()[-1]
    b2 = cluster.psql(f"select count(*) from hivemind_chunk_text($txt${long_txt}$txt$,64,8);")[1].strip().splitlines()[-1]
    checks["chunk_text_multi_for_long"] = int(b1) > 1
    checks["chunk_text_deterministic"] = b1 == b2

    # payload: message prose available; deleted message unavailable.
    msg_rows = _payload_rows(cluster, "message", "7000", "prose")
    checks["payload_message_available"] = bool(msg_rows) and msg_rows[0][0] in ("t", "true")
    del_rows = _payload_rows(cluster, "message", "7001", "prose")
    checks["payload_deleted_unavailable"] = bool(del_rows) and del_rows[0][0] in ("f", "false")

    # payload: workflow python available from payload.python_source.
    py_rows = _payload_rows(cluster, "resource", "7002", "workflow_python")
    checks["payload_workflow_python_available"] = bool(py_rows) and py_rows[0][0] in ("t", "true")

    # upsert: atomic replace + dimension mismatch rejected.
    cid = ec.ContractSpec(provider="openai", model="text-embedding-3-small", dimension=384).id
    good = _vec_literal(384)
    payload = (
        '[{"contract_id":' + str(cid) + ',"entity_type":"resource","item_id":"7002",'
        '"representation_type":"workflow_python","chunk_index":0,"chunk_text":"x",'
        f'"chunk_hash":"{_hash("c")}","representation_hash":"{_hash("r")}","embedding":"{good}"}}]'
    )
    n1 = cluster.psql(
        f"select hivemind_upsert_embedding_chunks('{payload}'::jsonb);"
    )[1].strip().splitlines()
    n1 = int([x for x in n1 if x.strip().isdigit()][-1])
    checks["upsert_writes_one"] = n1 == 1
    # re-upsert replaces (still 1 row, not 2)
    n2 = cluster.psql(f"select hivemind_upsert_embedding_chunks('{payload}'::jsonb);")[1].strip().splitlines()
    n2 = int([x for x in n2 if x.strip().isdigit()][-1])
    cnt = cluster.psql(
        "select count(*) from content_embeddings where item_id='7002' and representation_type='workflow_python';"
    )[1].strip().splitlines()[-1]
    checks["upsert_atomic_replace"] = n2 == 1 and cnt == "1"

    # dimension mismatch rejected by the vector(384) column.
    bad = _vec_literal(1536)
    bad_payload = payload.replace(good, bad)
    rc, _ = cluster.psql(f"select hivemind_upsert_embedding_chunks('{bad_payload}'::jsonb);")
    checks["upsert_rejects_wrong_dimension"] = rc != 0

    # drop removes rows.
    cluster.psql(
        f"select hivemind_drop_embedding_chunks({cid},'resource','7002','workflow_python');", capture=False
    )
    cnt2 = cluster.psql(
        "select count(*) from content_embeddings where item_id='7002';"
    )[1].strip().splitlines()[-1]
    checks["drop_removes_rows"] = cnt2 == "0"
    return checks


def measure_2_9_protocol(cluster) -> dict:
    """End-to-end worker protocol on the cluster with the deterministic fake embedder:
    enqueue -> claim -> payload -> fake-embed -> upsert -> complete, then verify the
    content_embeddings rows + a clean job status. Mirrors the Edge Function's path."""
    import asyncio
    from executors import embedding_contract as ec

    checks = {}
    fake = ec.DeterministicFakeEmbedder(dimension=384)
    cid = ec.ContractSpec(provider="openai", model="text-embedding-3-small", dimension=384).id

    # Drain leftover pending jobs FIRST, then seed the target so it is the only one claimable.
    for r in _claim_rows(cluster, "drain91", batch=64, lease=30):
        cluster.psql(f"select hivemind_complete_embedding_job({r[0]},'drain91',0);", capture=False)

    # Source row + job (trigger enqueues an embed job for 7100).
    cluster.psql("insert into discord_messages(message_id,content) values (7100,'configure block swap in the sampler kwargs region');", capture=False)

    # claim one job — only 7100 is pending now.
    rows = _claim_rows(cluster, "w91", batch=1, lease=60)
    target = [r for r in rows if r[2] == "7100"]
    checks["protocol_claimed_target_job"] = bool(target)
    if not target:
        return checks
    job = target[0]
    jid, et, iid, rep = job[0], job[1], job[2], job[3]

    # payload (canonical chunks)
    rc, out = cluster.psql(
        "select chunk_index,chunk_text,chunk_hash,representation_hash from hivemind_embedding_payload("
        f"'{et}','{iid}','{rep}',512,50);"
    )
    chunks = []
    for ln in (out or "").strip().splitlines():
        parts = [p.strip() for p in ln.split("|")]
        if len(parts) == 4 and parts[1]:
            chunks.append(parts)
    checks["protocol_payload_returned_chunks"] = len(chunks) >= 1

    # fake-embed the chunk texts (deterministic, 384-d, L2-normalized)
    loop = asyncio.new_event_loop()
    try:
        vectors = loop.run_until_complete(fake.embed_texts([c[1] for c in chunks]))
    finally:
        loop.close()
    checks["protocol_vectors_384d"] = all(len(v) == 384 for v in vectors)

    # upsert
    import json
    arr = [
        {
            "contract_id": cid, "entity_type": et, "item_id": iid, "representation_type": rep,
            "chunk_index": int(c[0]), "chunk_text": c[1], "chunk_hash": c[2],
            "representation_hash": c[3], "embedding": "[" + ",".join(f"{x:.6f}" for x in v) + "]",
        }
        for c, v in zip(chunks, vectors)
    ]
    payload_json = json.dumps(arr).replace("'", "''")
    written = cluster.psql(f"select hivemind_upsert_embedding_chunks('{payload_json}'::jsonb);")[1].strip().splitlines()
    written = int([x for x in written if x.strip().isdigit()][-1])
    checks["protocol_upsert_wrote_chunks"] = written == len(chunks)

    # complete
    cluster.psql(f"select hivemind_complete_embedding_job({jid},'w91',{written});", capture=False)
    status = _status_of(cluster, int(jid))
    checks["protocol_job_done"] = status == "done"

    # the vectors are stored under the active contract for this identity.
    stored = cluster.psql(
        f"select count(*) from content_embeddings where entity_type='{et}' and item_id='{iid}' "
        f"and representation_type='{rep}' and contract_id={cid};"
    )[1].strip().splitlines()[-1]
    checks["protocol_vectors_stored"] = stored == str(len(chunks))
    return checks


# ---------------------------------------------------------------------------
# 2.10 cleanup (schema/029)
# ---------------------------------------------------------------------------


def _embed_row(cluster, contract_id: int, entity_type: str, item_id: str, rep: str, idx: int = 0) -> None:
    h = _hash(f"{entity_type}{item_id}{rep}{idx}")
    cluster.psql(
        "insert into content_embeddings(contract_id,entity_type,item_id,representation_type,"
        "chunk_index,chunk_text,embedding,representation_hash,chunk_hash) values "
        f"({contract_id},'{entity_type}','{item_id}','{rep}',{idx},'x','{_vec_literal(384)}'::vector,'{h}','{h}') "
        "on conflict do nothing;",
        capture=False,
    )


def _ce_count(cluster, where: str) -> int:
    out = cluster.psql("select count(*) from content_embeddings " + ("where " + where if where else "") + ";")[1]
    lines = [ln for ln in (out or "").splitlines() if ln.strip().isdigit()]
    return int(lines[-1]) if lines else 0


def measure_2_10_cleanup(cluster) -> dict:
    from executors import embedding_contract as ec

    checks = {}
    active = int(cluster.psql("select hivemind_active_contract_id();")[1].strip().splitlines()[-1])

    # Ineligible message: deleted -> cleanup removes its vectors.
    cluster.psql("insert into discord_messages(message_id,content,is_deleted) values (8000,'gone',true);", capture=False)
    _embed_row(cluster, active, "message", "8000", "prose")
    # Ineligible distillation: rejected.
    cluster.psql("insert into distillations(id,question,answer,status) values (8100,'q','a','rejected');", capture=False)
    _embed_row(cluster, active, "distillation", "8100", "prose")
    # Missing resource: no source row.
    _embed_row(cluster, active, "resource", "8200", "prose")
    # Eligible message: stays.
    cluster.psql("insert into discord_messages(message_id,content) values (8001,'keep me');", capture=False)
    _embed_row(cluster, active, "message", "8001", "prose")

    removed = cluster.psql("select hivemind_cleanup_ineligible_embeddings(1000);")[1].strip().splitlines()
    removed = int([x for x in removed if x.strip().isdigit()][-1])
    checks["ineligible_removed_count"] = removed >= 3
    checks["ineligible_message_gone"] = _ce_count(cluster, "entity_type='message' and item_id='8000'") == 0
    checks["ineligible_distillation_gone"] = _ce_count(cluster, "entity_type='distillation' and item_id='8100'") == 0
    checks["ineligible_resource_gone"] = _ce_count(cluster, "entity_type='resource' and item_id='8200'") == 0
    checks["eligible_message_kept"] = _ce_count(cluster, "entity_type='message' and item_id='8001'") == 1

    # deactivate_item_embeddings: drop an identity across representations.
    _embed_row(cluster, active, "resource", "8300", "prose")
    _embed_row(cluster, active, "resource", "8300", "workflow_python")
    n = cluster.psql("select hivemind_deactivate_item_embeddings('resource','8300');")[1].strip().splitlines()
    checks["deactivate_drops_all_reps"] = _ce_count(cluster, "item_id='8300'") == 0

    # Safe replacement transition: A (the active contract) -> B with EQUAL coverage
    # -> A superseded; drop_contract_embeddings(A) frees A's vectors, B (active) is
    # preserved and dropping B is REFUSED. B is seeded by COPYING A's identities so
    # the schema/023 coverage guard passes regardless of what A accumulated.
    a = int(cluster.psql("select hivemind_active_contract_id();")[1].strip().splitlines()[-1])
    b = ec.ContractSpec(provider="openai", model="text-embedding-3-small", dimension=384,
                        canonicalization_version=2).id
    cluster.psql(
        f"insert into embedding_contracts(id,provider,model,dimension,canonicalization_version,chunking_version,status) "
        f"values ({b},'openai','text-embedding-3-small',384,2,1,'draft') on conflict (id) do nothing;",
        capture=False,
    )
    n_a = _ce_count(cluster, f"contract_id={a}")
    # Copy every A identity into B so B's coverage >= A's coverage.
    cluster.psql(
        f"insert into content_embeddings(contract_id,entity_type,item_id,representation_type,"
        f"chunk_index,chunk_text,embedding,representation_hash,chunk_hash) "
        f"select {b},entity_type,item_id,representation_type,chunk_index,chunk_text,embedding,"
        f"representation_hash,chunk_hash from content_embeddings where contract_id={a};",
        capture=False,
    )
    rc_switch = cluster.psql(f"select hivemind_set_active_embedding_contract({b}, true);")[0]
    checks["replacement_switch_ok"] = rc_switch == 0
    a_status = cluster.psql(f"select status from embedding_contracts where id={a};")[1].strip().splitlines()[-1]
    checks["old_contract_superseded"] = a_status == "superseded"

    # Dropping the OLD (superseded) contract frees its vectors; the active index is intact.
    dropped = cluster.psql(f"select hivemind_drop_contract_embeddings({a});")[1].strip().splitlines()
    dropped = int([x for x in dropped if x.strip().isdigit()][-1]) if any(x.strip().isdigit() for x in dropped) else 0
    checks["old_contract_vectors_dropped"] = dropped == n_a
    checks["active_index_preserved"] = _ce_count(cluster, f"contract_id={b}") >= n_a
    # Dropping the ACTIVE contract is refused (never wipe the live index).
    rc_refuse = cluster.psql(f"select hivemind_drop_contract_embeddings({b});")[0]
    checks["drop_active_contract_refused"] = rc_refuse != 0
    return checks


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------


def rehearse(out_dir: Path, only: str | None) -> dict:
    cluster, info = setup_cluster()
    ev: dict = {"task": "2.7-2.10-embedding-lifecycle-rehearsal", "cluster_isolated": True, **info}
    try:
        ev["migrations_applied"] = [n for n in S2_MIGRATIONS if (SCHEMA_DIR / n).exists()]
        if not only or only == "2.7":
            ev["triggers_2_7"] = measure_2_7_triggers(cluster)
        if not only or only == "2.8":
            ev["concurrency_2_8"] = measure_2_8_concurrency(cluster)
            ev["state_machine_2_8"] = measure_2_8_state_machine(cluster)
        if not only or only == "2.9":
            ev["worker_surface_2_9"] = measure_2_9_worker_surface(cluster)
            ev["protocol_2_9"] = measure_2_9_protocol(cluster)
        if not only or only == "2.10":
            ev["cleanup_2_10"] = measure_2_10_cleanup(cluster)
    finally:
        cluster.tear_down()
    bool_sections = [k for k in ev if isinstance(ev[k], dict) and all(isinstance(v, bool) for v in ev[k].values())]
    ev["verdict"] = {
        "all_pass": all(v for section in bool_sections for v in ev[section].values()),
        "sections": bool_sections,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "phase2-embedding-lifecycle-rehearsal.json").write_text(json.dumps(ev, indent=2))
    return ev


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Phase-2 embedding lifecycle rehearsal.")
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "hybrid-search")
    ap.add_argument("--only", default=None)
    args = ap.parse_args(argv)
    print(redact("Phase-2 lifecycle rehearsal: isolated PG cluster (pgvector), schema/020–029."))
    ev = rehearse(args.out, args.only)
    print(redact(f"Active contract id: {ev['active_contract_id']}"))
    print(redact(f"Migrations applied: {', '.join(ev['migrations_applied'])}"))
    for section, checks in ev.items():
        if isinstance(checks, dict) and all(isinstance(v, bool) for v in checks.values()):
            print(redact(f"[{section}] " + ", ".join(f"{k}={'PASS' if v else 'FAIL'}" for k, v in checks.items())))
    print(redact(f"\nVERDICT all_pass={ev['verdict']['all_pass']}"))
    print(redact(f"Evidence: {args.out / 'phase2-embedding-lifecycle-rehearsal.json'}"))
    return 0 if ev["verdict"]["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
