#!/usr/bin/env python3
"""Phase-2 / Tasks 2.2–2.5 isolated-cluster rehearsal of schema/020–024.

Applies the embedding-foundation migrations in a THROWAWAY local PostgreSQL
cluster (unix-socket-only, no network) built with the locally-compiled pgvector,
and proves the task completion signals against the live `vector` extension:

  2.2  pgvector enabled + reversible; `vector` type + `<=>` cosine op available;
       `drop extension ... cascade` cleanly reverses it.
  2.3  embedding_contracts + content_embeddings apply; DIMENSION MIXING is
       rejected at BOTH layers (physical vector(384) + the data trigger);
       prose vs workflow_python are distinct identities under one PK; the atomic
       same-dimension active-contract switch refuses a low-coverage replacement
       and supersedes the old active contract; one_active_contract_per_dimension
       holds. contract_id SQL == Python (parity anchor).
  2.4  result_kind -> entity_type mapping SQL == Python; a 19-digit Discord
       snowflake item_id round-trips as exact text.
  2.5  canonical message/resource/distillation text + representation_hash are
       identical in Python and SQL on ASCII/NFC fixtures (cross-language parity).

It mutates ONLY the throwaway cluster. The live Hivemind project is untouched
(production apply is operator-run at the approved window; see each migration
header). All output is routed through verify_access.redact. No credential,
private-content vector, secret, or live corpus row is touched or printed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Reuse the task-1.6 isolated-cluster harness (same PG binaries, same pattern).
from scripts.rehearse_message_identifier import RehearsalCluster  # noqa: E402
from verify_access import redact  # noqa: E402

from executors import embedding_contract as ec  # noqa: E402
from executors import entity_identity as ei  # noqa: E402
from executors import canonical_representations as cr  # noqa: E402
from executors import workflow_representation as wr  # noqa: E402

SCHEMA_DIR = REPO_ROOT / "schema"
MIGRATIONS = [
    "020_enable_pgvector.sql",
    "021_embedding_contracts.sql",
    "022_content_embeddings.sql",
    "023_embedding_contract_switch.sql",
    "024_identity_and_canonical_representations.sql",
]


def _vec_literal(dim: int, value: str = "0.016") -> str:
    """Build a deterministic vector(N) text literal for rehearsal inserts."""

    inner = ", ".join([value] * dim)
    return f"[{inner}]"


def _hash64(seed: str) -> str:
    import hashlib
    return hashlib.sha256(seed.encode()).hexdigest()


def _apply_migrations(db: RehearsalCluster) -> dict:
    applied = []
    for name in MIGRATIONS:
        path = SCHEMA_DIR / name
        sql_text = path.read_text(encoding="utf-8")
        r = db.sql(sql_text, timeout=120, on_error_stop=True)
        if r.returncode != 0:
            raise RuntimeError(
                f"migration {name} failed:\n{redact(r.stderr or r.stdout)}"
            )
        applied.append(name)
    return {"migrations_applied": applied}


# ---------------------------------------------------------------------------
# 2.2 — pgvector enablement + reversibility
# ---------------------------------------------------------------------------


def measure_pgvector(db: RehearsalCluster) -> dict:
    ext = db.sqlt(
        "select extversion from pg_extension where extname='vector';"
    ).strip()
    version = (ext.splitlines() or [""])[-1].strip()
    # vector type usable?
    type_ok = db.sqlt(
        "select (('[0.1,0.2,0.3,0.4]'::vector(4)) is not null)::text;"
    ).strip().splitlines()[-1]
    # cosine operator available?
    cos_ok = db.sqlt(
        "select ('[1,0]'::vector(2) <=> '[0,1]'::vector(2) is not null)::text;"
    ).strip().splitlines()[-1]
    return {
        "extension_version": version,
        "vector_type_usable": type_ok in ("t", "true"),
        "cosine_operator_usable": cos_ok in ("t", "true"),
    }


# ---------------------------------------------------------------------------
# 2.3 — contract_id parity + dimension mixing + identity + switch
# ---------------------------------------------------------------------------


def measure_contract_id_parity(db: RehearsalCluster) -> dict:
    sql_id = int(db.sqlt(
        "select hivemind_contract_id('openai','text-embedding-3-small',384,1,1);"
    ).strip().splitlines()[-1])
    py_spec = ec.ContractSpec(provider="openai", model="text-embedding-3-small", dimension=384)
    py_id = py_spec.id
    return {
        "sql_id": sql_id,
        "python_id": py_id,
        "parity": sql_id == py_id,
    }


def measure_dimension_mixing(db: RehearsalCluster) -> dict:
    # Two draft contracts: one 384, one 1536.
    c384 = ec.ContractSpec(provider="openai", model="m", dimension=384).id
    c1536 = ec.ContractSpec(provider="openai", model="m", dimension=1536).id
    db.sqlt(
        f"insert into embedding_contracts(id,provider,model,dimension,canonicalization_version,chunking_version,status) "
        f"values ({c384},'openai','m',384,1,1,'draft'), ({c1536},'openai','m',1536,1,1,'draft');"
    )
    h = _hash64("rep")

    # 1. physical: 384 vector into vector(384) -> OK
    ok_384 = db.sql(
        "insert into content_embeddings(contract_id,entity_type,item_id,representation_type,"
        "chunk_index,embedding,representation_hash,chunk_hash) "
        f"values ({c384},'resource','1','prose',0,'{_vec_literal(384)}'::vector,'{h}','{h}');",
        on_error_stop=False,
    )
    physical_ok = ok_384.returncode == 0

    # 2. physical: 1536 vector into vector(384) -> ERROR (dimension mismatch)
    bad_1536 = db.sql(
        "insert into content_embeddings(contract_id,entity_type,item_id,representation_type,"
        "chunk_index,embedding,representation_hash,chunk_hash) "
        f"values ({c384},'resource','2','prose',0,'{_vec_literal(1536)}'::vector,'{h}','{h}');",
        on_error_stop=True,
    )
    physical_reject = bad_1536.returncode != 0

    # 3. data trigger: file a 384 vector under the 1536 contract -> ERROR
    trigger_reject = db.sql(
        "insert into content_embeddings(contract_id,entity_type,item_id,representation_type,"
        "chunk_index,embedding,representation_hash,chunk_hash) "
        f"values ({c1536},'resource','3','prose',0,'{_vec_literal(384)}'::vector,'{h}','{h}');",
        on_error_stop=True,
    )
    data_trigger_reject = trigger_reject.returncode != 0 and "dimension mixing" in (
        (trigger_reject.stderr or "") + (trigger_reject.stdout or "")
    )

    return {
        "physical_384_into_384_ok": physical_ok,
        "physical_1536_into_384_rejected": physical_reject,
        "data_trigger_384_under_1536_contract_rejected": data_trigger_reject,
    }


def measure_identity_and_snowflake(db: RehearsalCluster) -> dict:
    c384 = ec.ContractSpec(provider="openai", model="m", dimension=384).id
    h = _hash64("snow")
    snow = "1234567890123456789"  # 19-digit Discord-style snowflake (>2^53)
    # Insert message + resource prose + workflow python under one (entity_type,item_id)
    db.sqlt(
        "insert into content_embeddings(contract_id,entity_type,item_id,representation_type,"
        "chunk_index,embedding,representation_hash,chunk_hash) values "
        f"({c384},'message','{snow}','prose',0,'{_vec_literal(384)}'::vector,'{h}','{h}'),"
        f"({c384},'resource','42','prose',0,'{_vec_literal(384)}'::vector,'{h}','{h}'),"
        f"({c384},'resource','42','workflow_python',0,'{_vec_literal(384)}'::vector,'{h}','{h}');"
    )
    # Snowflake round-trips as exact text
    got = db.sqlt(
        f"select item_id from content_embeddings where entity_type='message';"
    ).strip().splitlines()[-1]
    # Prose vs python are distinct PK rows for the same resource
    n_rep_rows = int(db.sqlt(
        "select count(*) from content_embeddings where entity_type='resource' and item_id='42';"
    ).strip().splitlines()[-1])
    rep_types = db.sqlt(
        "select string_agg(distinct representation_type, ',' order by representation_type) "
        "from content_embeddings where entity_type='resource' and item_id='42';"
    ).strip().splitlines()[-1]
    return {
        "snowflake_roundtrips_exact_text": got == snow,
        "snowflake_value": got,
        "distinct_representation_types_for_one_resource": sorted(rep_types.split(",")),
        "representation_row_count_for_resource_42": n_rep_rows,
        "prose_and_python_distinct_identity": n_rep_rows == 2 and rep_types == "prose,workflow_python",
    }


def measure_contract_switch(db: RehearsalCluster) -> dict:
    # Two 384 contracts A and B; A active first. Distinct model namespace ('sw')
    # so these contract ids never collide with the dimension-mixing fixtures above.
    a = ec.ContractSpec(provider="openai", model="sw", dimension=384, canonicalization_version=1).id
    b = ec.ContractSpec(provider="openai", model="sw", dimension=384, canonicalization_version=2).id
    db.sqlt(
        f"insert into embedding_contracts(id,provider,model,dimension,canonicalization_version,chunking_version,status) "
        f"values ({a},'openai','sw',384,1,1,'draft'),({b},'openai','sw',384,2,1,'draft');"
    )
    h = _hash64("sw")
    # A covers 3 identities, B covers 2 -> activating B with coverage must FAIL
    db.sqlt(
        "insert into content_embeddings(contract_id,entity_type,item_id,representation_type,"
        "chunk_index,embedding,representation_hash,chunk_hash) values "
        f"({a},'resource','a1','prose',0,'{_vec_literal(384)}'::vector,'{h}','{h}'),"
        f"({a},'resource','a2','prose',0,'{_vec_literal(384)}'::vector,'{h}','{h}'),"
        f"({a},'resource','a3','prose',0,'{_vec_literal(384)}'::vector,'{h}','{h}'),"
        f"({b},'resource','b1','prose',0,'{_vec_literal(384)}'::vector,'{h}','{h}'),"
        f"({b},'resource','b2','prose',0,'{_vec_literal(384)}'::vector,'{h}','{h}');"
    )
    # Make A active via the switch (no prior active -> coverage guard skipped).
    db.sqlt(f"select hivemind_set_active_embedding_contract({a}, true);")
    active_after_a = int(db.sqlt(
        "select count(*) from embedding_contracts where status='active' and dimension=384;"
    ).strip().splitlines()[-1])

    # Activate B with coverage required -> B covers 2 < A's 3 -> must FAIL
    fail_low = db.sql(
        f"select hivemind_set_active_embedding_contract({b}, true);", on_error_stop=True
    )
    low_coverage_rejected = fail_low.returncode != 0

    # Top up B to cover 3 identities, then switch -> OK; A becomes superseded.
    db.sqlt(
        "insert into content_embeddings(contract_id,entity_type,item_id,representation_type,"
        f"chunk_index,embedding,representation_hash,chunk_hash) values "
        f"({b},'resource','b3','prose',0,'{_vec_literal(384)}'::vector,'{h}','{h}');"
    )
    ok = db.sql(
        f"select hivemind_set_active_embedding_contract({b}, true);", on_error_stop=False
    )
    switch_ok = ok.returncode == 0
    a_status = db.sqlt(
        f"select status from embedding_contracts where id={a};"
    ).strip().splitlines()[-1]
    b_status = db.sqlt(
        f"select status from embedding_contracts where id={b};"
    ).strip().splitlines()[-1]
    active_after_b = int(db.sqlt(
        "select count(*) from embedding_contracts where status='active' and dimension=384;"
    ).strip().splitlines()[-1])
    return {
        "one_active_after_first_activate": active_after_a == 1,
        "low_coverage_replacement_rejected": low_coverage_rejected,
        "equal_coverage_switch_ok": switch_ok,
        "old_contract_superseded": a_status == "superseded",
        "new_contract_active": b_status == "active",
        "one_active_after_switch": active_after_b == 1,
    }


# ---------------------------------------------------------------------------
# 2.4 — result_kind -> entity_type mapping parity
# ---------------------------------------------------------------------------


def measure_identity_mapping_parity(db: RehearsalCluster) -> dict:
    kinds = ["message", "resource", "workflow", "article", "transcript", "distillation"]
    out = {}
    for k in kinds:
        sql_v = db.sqlt(
            f"select hivemind_entity_type_for_result_kind('{k}');"
        ).strip().splitlines()[-1]
        py_v = ei.entity_type_for_result_kind(k)
        out[k] = {"sql": sql_v, "python": py_v, "parity": sql_v == py_v}
    return out


# ---------------------------------------------------------------------------
# 2.5 — canonical text + hash parity (Python vs SQL)
# ---------------------------------------------------------------------------


def _scalar_text(db: RehearsalCluster, sql_expr: str) -> str:
    """Return the EXACT text a scalar SQL expression yields, newlines included.

    Wraps the value in sentinels so embedded newlines survive (psql -A -t prints
    a multi-line text field verbatim; ``splitlines()[-1]`` would drop all but the
    last line, which is why earlier text-parity checks were wrong).
    """

    out = db.sqlt(f"select '<V>' || coalesce(({sql_expr}), '') || '</V>';")
    start = out.index("<V>") + 3
    end = out.rindex("</V>")
    return out[start:end]


def measure_canonical_parity(db: RehearsalCluster) -> dict:
    cases = []

    # message
    msg = "how do I lower the motion amplitude in Wan 2.2"
    sql_msg = _scalar_text(db, f"hivemind_canonical_message_text($q${msg}$q$)")
    py_msg = cr.canonical_message_text(msg)
    sql_msg_h = db.sqlt(
        f"select hivemind_representation_hash($q${msg}$q$);"
    ).strip().splitlines()[-1]
    py_msg_h = wr.representation_hash(msg)
    cases.append({
        "type": "message", "text_parity": sql_msg == py_msg,
        "hash_parity": sql_msg_h == py_msg_h, "hash": py_msg_h,
    })

    # resource (with tags) — hash the SQL-BUILT text on both sides (stronger than
    # hashing Python's text): it proves SQL-built and Python-built text hash alike.
    title, body, tags = "WanVideo I2V guide", "use WanVideoSampler with LoRA", "wan video lora"
    sql_res = _scalar_text(
        db,
        "hivemind_canonical_resource_text($q${}$q$,$q${}$q$,$q${}$q$)".format(title, body, tags),
    )
    py_res = cr.canonical_resource_text(title, body, tags)
    sql_res_h = db.sqlt(
        "select hivemind_representation_hash($q${}$q$);".format(sql_res)
    ).strip().splitlines()[-1]
    py_res_h = wr.representation_hash(py_res)
    cases.append({
        "type": "resource", "text_parity": sql_res == py_res,
        "hash_parity": sql_res_h == py_res_h, "hash": py_res_h,
    })

    # distillation
    q, cond, ans = "best upscaler for anime video", "for 12GB VRAM", "use RealESRGAN x2"
    sql_dis = _scalar_text(
        db,
        "hivemind_canonical_distillation_text($q${}$q$,$q${}$q$,$q${}$q$)".format(q, cond, ans),
    )
    py_dis = cr.canonical_distillation_text(q, cond, ans)
    sql_dis_h = db.sqlt(
        "select hivemind_representation_hash($q${}$q$);".format(sql_dis)
    ).strip().splitlines()[-1]
    py_dis_h = wr.representation_hash(py_dis)
    cases.append({
        "type": "distillation", "text_parity": sql_dis == py_dis,
        "hash_parity": sql_dis_h == py_dis_h, "hash": py_dis_h,
    })

    # empty-conditions distillation (skip-empty parity)
    sql_dis2 = _scalar_text(
        db,
        "hivemind_canonical_distillation_text($q${}$q$,$q${}$q$,$q${}$q$)".format(q, "", ans),
    )
    py_dis2 = cr.canonical_distillation_text(q, "", ans)
    cases.append({
        "type": "distillation_empty_conditions",
        "text_parity": sql_dis2 == py_dis2,
    })

    return {"cases": cases}


# ---------------------------------------------------------------------------
# Rollback (reversibility)
# ---------------------------------------------------------------------------


def measure_rollback(db: RehearsalCluster) -> dict:
    steps = []
    # Roll back in reverse dependency order: drop 024 fns, 023 fn, 022 table,
    # 021 table, then 020 extension cascade.
    for stmt in [
        "drop function if exists hivemind_representation_hash(text);"
        " drop function if exists hivemind_normalize_for_hash(text);"
        " drop function if exists hivemind_canonical_distillation_text(text,text,text);"
        " drop function if exists hivemind_canonical_resource_text(text,text,text);"
        " drop function if exists hivemind_canonical_message_text(text);"
        " drop function if exists hivemind_entity_type_for_result_kind(text);",
        "drop function if exists hivemind_set_active_embedding_contract(bigint, boolean);",
        "drop table if exists content_embeddings cascade;",
        "drop table if exists embedding_contracts cascade;",
        "drop extension if exists vector cascade;",
    ]:
        r = db.sql(stmt, on_error_stop=False)
        steps.append({"stmt_head": stmt[:48], "ok": r.returncode == 0})
    vec_gone = db.sql(
        "select count(*) from pg_extension where extname='vector';",
        on_error_stop=False,
    ).stdout.strip().splitlines()
    vec_gone = (vec_gone or ["1"])[-1] == "0"
    tbl_gone = db.sql(
        "select count(*) from pg_tables where tablename='content_embeddings';",
        on_error_stop=False,
    ).stdout.strip().splitlines()
    tbl_gone = (tbl_gone or ["1"])[-1] == "0"
    return {"steps": steps, "vector_extension_dropped": vec_gone, "table_dropped": tbl_gone}


def _verdict(ev: dict) -> dict:
    pv = ev["pgvector"]
    cid = ev["contract_id_parity"]
    dm = ev["dimension_mixing"]
    ident = ev["identity_and_snowflake"]
    sw = ev["contract_switch"]
    mp = ev["identity_mapping_parity"]
    cp = ev["canonical_parity"]
    rb = ev["rollback"]
    map_all = all(v["parity"] for v in mp.values())
    hash_all = all(c.get("hash_parity", True) for c in cp["cases"])
    text_all = all(c.get("text_parity", True) for c in cp["cases"])
    checks = {
        "2.2_vector_type_usable": pv["vector_type_usable"],
        "2.2_cosine_operator_usable": pv["cosine_operator_usable"],
        "2.3_contract_id_python_sql_parity": cid["parity"],
        "2.3_dimension_mixing_physical_rejected": dm["physical_1536_into_384_rejected"],
        "2.3_dimension_mixing_data_trigger_rejected": dm["data_trigger_384_under_1536_contract_rejected"],
        "2.3_prose_vs_python_distinct_identity": ident["prose_and_python_distinct_identity"],
        "2.3_low_coverage_switch_rejected": sw["low_coverage_replacement_rejected"],
        "2.3_equal_coverage_switch_ok": sw["equal_coverage_switch_ok"],
        "2.3_one_active_contract_after_switch": sw["one_active_after_switch"],
        "2.4_snowflake_roundtrips_exact_text": ident["snowflake_roundtrips_exact_text"],
        "2.4_result_kind_mapping_parity": map_all,
        "2.5_canonical_text_parity": text_all,
        "2.5_canonical_hash_parity": hash_all,
        "rollback_vector_extension_dropped": rb["vector_extension_dropped"],
        "rollback_table_dropped": rb["table_dropped"],
    }
    return {"all_pass": all(checks.values()), "checks": checks}


def rehearse(out_dir: Path) -> dict:
    db = RehearsalCluster()
    ev: dict = {"task": "2.2-2.5-embedding-schema-rehearsal", "cluster_isolated": True}
    try:
        db.start()
        ev["pg_version"] = db.sqlt("SHOW server_version;").strip()
        ev["migrations"] = _apply_migrations(db)
        ev["pgvector"] = measure_pgvector(db)
        ev["contract_id_parity"] = measure_contract_id_parity(db)
        ev["dimension_mixing"] = measure_dimension_mixing(db)
        ev["identity_and_snowflake"] = measure_identity_and_snowflake(db)
        ev["contract_switch"] = measure_contract_switch(db)
        ev["identity_mapping_parity"] = measure_identity_mapping_parity(db)
        ev["canonical_parity"] = measure_canonical_parity(db)
        ev["rollback"] = measure_rollback(db)
        ev["verdict"] = _verdict(ev)
    finally:
        db.stop()
        db.destroy()

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "phase2-embedding-schema-rehearsal.json").write_text(
        json.dumps(ev, indent=2)
    )
    return ev


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Phase-2 embedding-foundation rehearsal.")
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "hybrid-search")
    args = ap.parse_args(argv)
    print(redact("Phase 2 rehearsal: isolated PG cluster (pgvector), schema/020–024."))
    ev = rehearse(args.out)
    v = ev["verdict"]
    print(redact(f"PG version: {ev.get('pg_version','?').strip()}"))
    print(redact(f"Migrations applied: {', '.join(ev['migrations']['migrations_applied'])}"))
    print(redact(f"pgvector: type={ev['pgvector']['vector_type_usable']} "
                 f"cosine={ev['pgvector']['cosine_operator_usable']} "
                 f"v{ev['pgvector']['extension_version']}"))
    print(redact(f"contract_id parity (SQL==Python): {ev['contract_id_parity']['parity']} "
                 f"(id={ev['contract_id_parity']['python_id']})"))
    dm = ev["dimension_mixing"]
    print(redact(f"dimension mixing: physical_1536_rejected={dm['physical_1536_into_384_rejected']} "
                 f"trigger_rejected={dm['data_trigger_384_under_1536_contract_rejected']}"))
    sw = ev["contract_switch"]
    print(redact(f"contract switch: low_cov_rejected={sw['low_coverage_replacement_rejected']} "
                 f"equal_cov_ok={sw['equal_coverage_switch_ok']} one_active={sw['one_active_after_switch']}"))
    print(redact(f"snowflake exact text: {ev['identity_and_snowflake']['snowflake_roundtrips_exact_text']} "
                 f"({ev['identity_and_snowflake']['snowflake_value']})"))
    print(redact(f"mapping parity: {all(v['parity'] for v in ev['identity_mapping_parity'].values())}"))
    cp = ev["canonical_parity"]
    print(redact(f"canonical parity: text={all(c.get('text_parity',True) for c in cp['cases'])} "
                 f"hash={all(c.get('hash_parity',True) for c in cp['cases'])}"))
    print(redact(f"rollback: vector_dropped={ev['rollback']['vector_extension_dropped']} "
                 f"table_dropped={ev['rollback']['table_dropped']}"))
    print(redact(f"\nVERDICT all_pass={v['all_pass']}"))
    for k, val in v["checks"].items():
        print(redact(f"  [{'PASS' if val else 'FAIL'}] {k}"))
    print(redact(f"Evidence: {args.out / 'phase2-embedding-schema-rehearsal.json'}"))
    return 0 if v["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
