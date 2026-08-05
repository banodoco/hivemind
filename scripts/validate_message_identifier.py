#!/usr/bin/env python3
"""Deterministic validator for the full-message exact-identifier path (task 1.6).

Two modes:
  * offline (default): contract consistency for the CHOSEN design (normalized
    full-message trigram, length-bounded) + the rejected-alternative extraction
    self-consistency (decision record). No DB.
  * HIVEMIND_EVAL_CLUSTER=1: + an isolated throwaway PG14 cluster proving
    schema/007 loads, the chosen index builds valid, a candidate query USES it
    (Bitmap Index Scan), and the rejected-alternative SQL extraction agrees with
    the Python reference byte-for-byte on every fixture (the decision-record
    parity proof).

Read-only on a throwaway cluster torn down after capture. No production mutation.
Offline output is safe (no source content / credentials).

Run::

    python3 scripts/validate_message_identifier.py
    HIVEMIND_EVAL_CLUSTER=1 python3 scripts/validate_message_identifier.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "scripts"), str(REPO_ROOT / "executors")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import scripts.message_identifier_index as M  # noqa: E402
from executors import message_identifier_index as REF  # noqa: E402

FIXTURES = REPO_ROOT / "eval" / "retrieval" / "fixtures" / "message-identifier-v1.json"


def _ok(msg): print(f"  ok: {msg}")
def _fail(msg): print(f"  FAIL: {msg}"); raise SystemExit(1)


def validate_offline() -> None:
    print("offline contract checks")
    if REF.CHOICE != "normalized_full_message_trigram_length_bounded":
        _fail(f"choice is {REF.CHOICE!r}")
    _ok(f"choice = {REF.CHOICE}")
    if REF.MESSAGE_IDENTIFIER_INDEX_VERSION < 2:
        _fail("contract version < 2 (v2 = evidence-based pivot)")
    _ok(f"version = {REF.MESSAGE_IDENTIFIER_INDEX_VERSION}")

    # chosen index expression + partial predicate
    if REF.INDEX_EXPRESSION != "hivemind_normalize_identifier(content)":
        _fail(f"index expression drifted: {REF.INDEX_EXPRESSION!r}")
    if "is_deleted = false" not in REF.PARTIAL_PREDICATE or "char_length(content)" not in REF.PARTIAL_PREDICATE:
        _fail(f"partial predicate missing eligibility/length bound: {REF.PARTIAL_PREDICATE!r}")
    _ok(f"index = gin({REF.INDEX_EXPRESSION} {REF.INDEX_OPCLASS}) WHERE {REF.PARTIAL_PREDICATE}")

    # candidate query contract shape (chosen design: containment primary)
    cq = M.candidate_query_sql(requested_limit=20)
    for needle in ("hivemind_normalize_identifier(m.content)", "LIKE '%' || q.k || '%'",
                   "is_deleted = false", "char_length(m.content)", "message_id::text", "LIMIT"):
        if needle not in cq:
            _fail(f"candidate query missing {needle!r}")
    if "<%" in cq:
        _fail("candidate query must not contain the permissive <% path (primary is containment)")
    _ok("candidate query contract shape (LIKE containment on normalized content, eligibility, snowflake::text, LIMIT)")
    if REF.candidate_limit(20) != 100:
        _fail("candidate_limit(20) != 100")
    _ok(f"candidate_limit(20) = {REF.candidate_limit(20)} (cap {REF.CANDIDATE_LIMIT_CAP})")
    if not REF.arm_should_fire("FLUX.1") or REF.arm_should_fire(""):
        _fail("arm_should_fire gating broken")
    _ok("arm_should_fire empty/whitespace gating")

    # schema/007 contains the chosen index (frozen identities)
    sql = M.schema_sql_text()
    for ident in (REF.INDEX_NAME, REF.INDEX_EXPRESSION, "gin_trgm_ops", "is_deleted = false"):
        if ident not in sql:
            _fail(f"schema/007 missing identity {ident!r}")
    # schema/007 must NOT contain the rejected side table (the pivot removed it)
    if "message_identifiers" in sql or "hivemind_extract_message_identifiers" in sql:
        _fail("schema/007 still contains the rejected side index (should be index-only)")
    _ok("schema/007 is the chosen index-only design (no side table/fn/trigger)")

    # rejected-alternative extraction: required families normalize to one compact key
    families = {
        "FLUX.1": "flux1", "Wan2.2": "wan22", "wan_2.2": "wan22",
        "LTX-Video": "ltxvideo", "ltx-2-19b-ic-lora-detailer": "ltx219bicloradetailer",
        "lightx2v_I2V_14B.safetensors": "lightx2vi2v14bsafetensors",
        "WanVideoSampler": "wanvideosampler",
        "force_clip_output=False": "forceclipoutput=false",
    }
    for raw, want in families.items():
        if want not in REF.extract_message_identifiers(raw):
            _fail(f"rejected-alt family {raw!r} did not produce compact {want!r}")
    _ok(f"rejected-alt required families normalize to one compact key ({len(families)} cases)")

    # frozen fixture corpus self-consistency (rejected-alt Python reference)
    fx = json.loads(FIXTURES.read_text())
    for f in fx["fixtures"]:
        got = sorted(REF.extract_message_identifiers(f["content"]).keys())
        if got != sorted(f["expected_compacts"]):
            _fail(f"fixture {f['name']!r}: got {got} expected {f['expected_compacts']}")
    _ok(f"frozen fixture corpus self-consistent ({len(fx['fixtures'])} fixtures)")
    print("offline: OK\n")


def validate_cluster() -> None:
    print("isolated-cluster checks (throwaway PG14; torn down after)")
    root = Path(tempfile.mkdtemp(prefix="mi_val_cluster_"))
    datadir = root / "data"
    env = {**os.environ, "PGHOST": str(root), "PGPORT": "55461",
           "PGUSER": "postgres", "PGDATABASE": "postgres"}

    def run(cmd, **kw):
        kw.setdefault("env", env); kw.setdefault("stdout", subprocess.PIPE)
        kw.setdefault("stderr", subprocess.PIPE); kw.setdefault("text", True)
        return subprocess.run(cmd, **kw)

    def sql(stmt, timeout=180):
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as tf:
            tf.write(stmt); s = tf.name
        try:
            return run(["psql", "-X", "-q", "-t", "-A", "-P", "pager=off",
                        "-v", "ON_ERROR_STOP=1", "-f", s], timeout=timeout)
        finally:
            try: os.unlink(s)
            except OSError: pass

    def sqlt(stmt, timeout=180):
        r = sql(stmt, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError(f"psql failed:\n{r.stderr or r.stdout}")
        return r.stdout

    try:
        for b in ("initdb", "pg_ctl", "psql"):
            if shutil.which(b) is None:
                _fail(f"PG binary {b} not on PATH")
        run(["initdb", "-D", str(datadir), "-U", "postgres", "-A", "trust",
             "--no-locale", "-E", "UTF8"], timeout=120)
        opts = f"-c listen_addresses='' -c unix_socket_directories='{root}' -p 55461"
        run(["pg_ctl", "-D", str(datadir), "-l", str(root / "pg.log"), "-o", opts, "-w", "start"], timeout=120)

        sql(M.prereq_schema_sql_text())
        sql("create extension if not exists pg_trgm;")
        sql(f"create table {M.SOURCE_TABLE} (message_id bigint primary key, content text, "
            f"is_deleted boolean not null default false, created_at timestamptz not null default now(), "
            f"channel_id bigint, author_id bigint, guild_id bigint);")
        # seed a few rows + build the chosen index
        sql(f"insert into {M.SOURCE_TABLE}(message_id, content, is_deleted) values "
            f"(1, 'FLUX.1 dev lora here', false), (2, 'wan2.2 WanVideoSampler node', false), "
            f"(3, 'controlnet settings', true);")  # 3 is soft-deleted
        if sql(M.schema_sql_text(), timeout=180).returncode != 0:
            _fail("schema/007 load failed")
        _ok("schema/005 + pg_trgm + discord_messages stub + schema/007 load cleanly")

        valid = sqlt(f"SELECT indisvalid FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                     f"WHERE c.relname='{REF.INDEX_NAME}';").strip().splitlines()[-1]
        if valid not in ("t", "true"):
            _fail(f"chosen index not valid (indisvalid={valid!r})")
        _ok("chosen index builds valid")

        # the candidate query USES the index (structurally; enable_seqscan=off because
        # a 3-row table seq-scans naturally — the production-scale proof is the rehearsal).
        # PRIMARY containment predicate (LIKE) is structurally index-served.
        plan = sqlt(
            f"SET enable_seqscan = off;\n"
            f"EXPLAIN WITH q AS (SELECT public.hivemind_normalize_identifier('FLUX.1') AS k) "
            f"SELECT m.message_id::text FROM {M.SOURCE_TABLE} m, q "
            f"WHERE m.is_deleted=false AND char_length(m.content) BETWEEN {REF.CONTENT_LENGTH_MIN} AND {REF.CONTENT_LENGTH_MAX} "
            f"AND public.hivemind_normalize_identifier(m.content) LIKE '%' || q.k || '%' LIMIT 10;")
        if REF.INDEX_NAME not in plan.lower() or "bitmap index scan" not in plan.lower():
            _fail(f"candidate query did not use the chosen index via Bitmap Index Scan:\n{plan}")
        _ok("candidate query uses the chosen index (Bitmap Index Scan, forced on the tiny fixture table)")

        # rejected-alternative SQL/Python extraction parity (decision-record proof, inline SQL)
        fx = json.loads(FIXTURES.read_text())
        mm = 0
        for f in fx["fixtures"]:
            content = f["content"]
            py = set(REF.extract_message_identifiers(content).keys())
            out = sqlt(
                "SELECT distinct on (norm.compact) norm.compact FROM ("
                " SELECT public.hivemind_normalize_identifier((rm)[1]) AS compact, rn"
                "   FROM regexp_matches($pc$" + content + "$pc$, '[A-Za-z0-9_.=-]+', 'g') WITH ORDINALITY AS r(rm, rn)"
                ") norm WHERE char_length(norm.compact) BETWEEN 3 AND 100 AND norm.compact ~ '[A-Za-z]'"
                " ORDER BY norm.compact, norm.rn LIMIT 256;")
            sq = set(l.strip() for l in out.splitlines() if l.strip())
            if py != sq:
                mm += 1; print(f"    MISMATCH {f['name']!r}: py-sql={py-sq} sql-py={sq-py}")
        if mm:
            _fail(f"rejected-alt SQL/Python parity: {mm} mismatch(es)")
        _ok(f"rejected-alt SQL/Python extraction parity: 0 mismatches on {len(fx['fixtures'])} fixtures")
        print("cluster: OK\n")
    finally:
        if (datadir / "postmaster.pid").exists():
            run(["pg_ctl", "-D", str(datadir), "-m", "fast", "-w", "stop"], timeout=60)
        shutil.rmtree(root, ignore_errors=True)


def main(argv=None) -> int:
    print("Task 1.6 full-message exact-identifier path — validator")
    validate_offline()
    if os.environ.get("HIVEMIND_EVAL_CLUSTER") == "1":
        validate_cluster()
    else:
        print("(set HIVEMIND_EVAL_CLUSTER=1 to also run the isolated-cluster proof)\n")
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
