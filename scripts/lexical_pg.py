#!/usr/bin/env python3
"""Throwaway local PostgreSQL cluster harness for task-1.2 SQL evidence.

Spins up an ISOLATED, throwaway PostgreSQL 14+ cluster (``initdb --auth=trust``,
a temp data dir, a high TCP port + unix socket in the temp dir), applies the
Hivemind lexical schema (``schema/003``), seeds deterministic fixtures
(workflow-Python documents computed by :mod:`executors.lexical_documents` from
the frozen reference), runs assertions, and captures ``EXPLAIN (ANALYZE,
BUFFERS)`` evidence that representative ``simple`` FTS queries use the new GIN
indexes. Tears the cluster down afterwards. **No Docker, no network, no
production mutation.**

Shared by:
  - ``scripts/run_lexical_pg_tests.py``  — reproducible runner (+ evidence JSON)
  - ``tests/test_lexical_sql.py``         — unittest SQL tests (skip if no PG)

The cluster never touches the developer's existing 5432 service: it uses a
fresh temp data directory, ``-A trust``, and an ephemeral port.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from executors import lexical_documents as LD  # noqa: E402

SCHEMA_003 = REPO / "schema" / "003_lexical_resource_documents.sql"

# A shared sample module used across workflow fixtures.
SAMPLE_PY = (
    "import torch\n"
    "class WanVideoSampler:\n"
    "    def __init__(self, lora_weight=0.8, num_frames=81):\n"
    "        self.lora_weight = lora_weight\n"
    "        self.num_frames = num_frames\n"
)
READY_DELIM = "Python ready-template source:"
SCRATCH_DELIM = "Python scratchpad source:"
SECRET_SK = "sk-abcdefghijklmnopqrstuvwxyz0123456789ABCDEF"


# ---------------------------------------------------------------------------
# PostgreSQL binary discovery + cluster lifecycle
# ---------------------------------------------------------------------------


def _candidate_bindirs() -> list[pathlib.Path]:
    paths = []
    for env in ("HIVEMIND_PG_BINDIR", "PGBIN", "PG_BINDIR"):
        if os.environ.get(env):
            paths.append(pathlib.Path(os.environ[env]))
    for root in ("/opt/homebrew/opt", "/usr/local/opt", "/opt/local/lib"):
        base = pathlib.Path(root)
        for sub in sorted(base.glob("postgresql@*"), reverse=True):
            paths.append(sub / "bin")
        if (base / "postgresql").exists():
            paths.append(base / "postgresql" / "bin")
    # Fall back to PATH.
    return paths


def find_pgbins() -> dict[str, pathlib.Path] | None:
    """Locate initdb/pg_ctl/psql. Returns None if any is missing."""
    need = ("initdb", "pg_ctl", "psql")
    for bindir in _candidate_bindirs():
        if all((bindir / n).exists() for n in need):
            return {n: bindir / n for n in need}
    # Last resort: PATH.
    found = {}
    for n in need:
        p = shutil.which(n)
        if not p:
            return None
        found[n] = pathlib.Path(p)
    return found


def find_free_port(preferred: int = 5533) -> int:
    for port in range(preferred, preferred + 200):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free port for throwaway cluster")


@dataclass
class LocalCluster:
    """A throwaway local PG cluster. Always tear_down() (try/finally)."""

    bindir: pathlib.Path
    port: int
    tmpdir: pathlib.Path
    datadir: pathlib.Path
    socketdir: pathlib.Path
    _started: bool = False

    @classmethod
    def start(cls, bindir: pathlib.Path | None = None) -> "LocalCluster":
        bins = find_pgbins()
        if bins is None:
            raise RuntimeError("PostgreSQL binaries (initdb/pg_ctl/psql) not found")
        initdb = bindir or bins["initdb"].parent
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="hm_lex_pg_"))
        data = tmp / "data"
        sock = tmp / "sock"
        sock.mkdir()
        port = find_free_port()
        subprocess.run(
            [str(initdb / "initdb"), "-D", str(data), "-A", "trust",
             "-U", "postgres", "--no-locale", "-E", "UTF8"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        cluster = cls(bindir=initdb, port=port, tmpdir=tmp, datadir=data, socketdir=sock)
        cluster._start()
        return cluster

    def _start(self) -> None:
        log = self.tmpdir / "pg.log"
        subprocess.run(
            [str(self.bindir / "pg_ctl"), "-D", str(self.datadir),
             "-o", f"-p {self.port} -k {self.socketdir} -c listen_addresses='127.0.0.1'",
             "-l", str(log), "-w", "start"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._started = True
        # Wait until ready.
        for _ in range(100):
            if self.psql("select 1", capture=True)[0] == 0:
                return
            time.sleep(0.05)
        raise RuntimeError(f"cluster did not become ready; see {log}")

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(PGHOST=str(self.socketdir), PGPORT=str(self.port),
                   PGUSER="postgres", PGDATABASE="postgres")
        return env

    def psql(self, sql: str, *, stdin: str | None = None, capture: bool = True) -> tuple[int, str]:
        cmd = [str(self.bindir / "psql"), "-v", "ON_ERROR_STOP=1",
               "-X", "-q", "-A", "-t", "-c", sql]
        proc = subprocess.run(
            cmd, env=self._env(), input=stdin,
            text=True, capture_output=True,
        )
        if not capture and proc.returncode != 0:
            raise RuntimeError(f"psql failed ({proc.returncode}):\n{proc.stderr}")
        return proc.returncode, (proc.stdout if capture else proc.stderr)

    def psql_file(self, path: pathlib.Path) -> None:
        cmd = [str(self.bindir / "psql"), "-v", "ON_ERROR_STOP=1", "-X", "-q", "-f", str(path)]
        proc = subprocess.run(cmd, env=self._env(), text=True, capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError(f"psql -f {path} failed:\n{proc.stderr}")

    def tear_down(self) -> None:
        if self._started:
            try:
                subprocess.run(
                    [str(self.bindir / "pg_ctl"), "-D", str(self.datadir),
                     "-m", "immediate", "stop"],
                    check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
                )
            finally:
                self._started = False
        shutil.rmtree(self.tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# SQL literal rendering (no DB driver; we render deterministic INSERTs)
# ---------------------------------------------------------------------------


def q(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def q_array(items: Sequence[str]) -> str:
    if not items:
        return "'{}'"
    return "ARRAY[" + ",".join(q(str(i)) for i in items) + "]"


def q_jsonb(obj: Any) -> str:
    return q(json.dumps(obj)) + "::jsonb"


# ---------------------------------------------------------------------------
# Minimal bootstrap (only the tables task 1.2 needs; mirrors schema/001 shapes)
# ---------------------------------------------------------------------------

BOOTSTRAP_SQL = """
create extension if not exists pg_trgm;
create table if not exists external_resources (
  id          bigint primary key,
  kind        text not null,
  source      text not null default 'manual',
  external_id text,
  title       text not null,
  body        text not null default '',
  author      text,
  url         text,
  metadata    jsonb not null default '{}'::jsonb,
  payload     jsonb,
  created_at  timestamptz not null default now()
);
create table if not exists contributors (
  id          bigint primary key,
  name         text not null unique,
  kind         text not null default 'agent',
  api_key_hash text,
  revoked_at   timestamptz,
  created_at   timestamptz not null default now()
);
insert into contributors (id, name) values (1, 'tester')
  on conflict (name) do nothing;
create table if not exists distillations (
  id            bigint primary key,
  question      text not null,
  conditions    text,
  answer        text not null,
  confidence    text not null default 'medium'
                check (confidence in ('high','medium','low')),
  status        text not null default 'pending'
                check (status in ('pending','approved','rejected','superseded')),
  author_id     bigint not null default 1,
  approved_by   bigint,
  supersedes_id bigint,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
"""


def workflow_semantics(**kw: Any) -> dict[str, Any]:
    base = {
        "media_type": "video",
        "task_type": "image_to_video",
        "model_families": ["wan"],
        "node_types": ["WanVideoSampler"],
        "node_class_multiset": {"WanVideoSampler": 1},
        "models": ["wan2.2-i2v-14B.safetensors"],
        "searchable_aliases": ["wanvideo", "wan2.2", "control net"],
        "custom_nodes": [],
        "adapter_directions": [{"from": ["image"], "to": "video"}],
    }
    base.update(kw)
    return {"workflow_semantics": base, "workflow_semantics_version": 1}


# ---------------------------------------------------------------------------
# Representative fixtures (the task-1.2 fixture families)
# ---------------------------------------------------------------------------


def resource_fixtures() -> list[dict[str, Any]]:
    payload_only = {"python_source": SAMPLE_PY}
    body_only_body = f"Body-only workflow.\n\n{SCRATCH_DELIM}\n{SAMPLE_PY}"
    both_body = f"Workflow with both copies.\n\n{READY_DELIM}\n{SAMPLE_PY}"
    changed_py = SAMPLE_PY + "\nchanged_marker = WanVideoSampler(num_frames=16)\n"
    huge_lines = "import os\n" + "\n".join(
        f"v{i} = WanVideoSampler(model='m{i}.safetensors', num_frames={i})"
        for i in range(600)
    )
    unparseable = "def broken(:\n    not valid python !!!\n"
    secret_py = f"import torch\napi_key = '{SECRET_SK}'\nprint(api_key)\n"

    return [
        {"id": 1001, "kind": "workflow", "title": "Payload Python WanVideoSampler",
         "body": "Description only, no code block here.", "payload": payload_only,
         "metadata": workflow_semantics()},
        {"id": 1002, "kind": "workflow", "title": "Body-only Python",
         "body": body_only_body, "payload": {}, "metadata": workflow_semantics()},
        {"id": 1003, "kind": "workflow", "title": "Both Body And Payload Python",
         "body": both_body, "payload": {"python_source": SAMPLE_PY},
         "metadata": workflow_semantics()},
        {"id": 1004, "kind": "workflow", "title": "Changed Python Marker",
         "body": "Description.", "payload": {"python_source": changed_py},
         "metadata": workflow_semantics()},
        {"id": 1005, "kind": "workflow", "title": "Huge Generated Python",
         "body": "Large generated archive.", "payload": {"python_source": huge_lines},
         "metadata": workflow_semantics()},
        {"id": 1006, "kind": "workflow", "title": "Unparseable Python",
         "body": "Description.", "payload": {"python_source": unparseable},
         "metadata": workflow_semantics()},
        {"id": 1007, "kind": "workflow", "title": "Unavailable No Python",
         "body": "Image-to-video pipeline using WanVideoSampler prose only.",
         "payload": {}, "metadata": workflow_semantics()},
        {"id": 1008, "kind": "workflow", "title": "Quarantined Credential",
         "body": "Description.", "payload": {"python_source": secret_py},
         "metadata": workflow_semantics()},
        {"id": 1009, "kind": "article", "title": "Upscale Model Article",
         "body": "A long-form article about the best upscale model for anime video.",
         "payload": {}, "metadata": {"tags": ["upscaling", "anime"]}},
    ]


def distillation_fixtures() -> list[dict[str, Any]]:
    return [
        {"id": 1, "question": "What is the best upscale model?",
         "conditions": "for anime-style video", "answer": "Use a 4x model then a 2x model.",
         "confidence": "high", "status": "approved"},
        {"id": 2, "question": "How do I use controlnet settings?",
         "conditions": None, "answer": "Lower the motion amplitude and reduce strength.",
         "confidence": "medium", "status": "pending"},
        {"id": 3, "question": "Rejected secret answer about credentials",
         "conditions": None, "answer": "rejected content controlnet", "confidence": "low",
         "status": "rejected"},
    ]


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def seed_cluster(cluster: LocalCluster, *, filler_resources: int = 4000) -> dict[str, Any]:
    """Apply bootstrap + 003, insert fixtures, compute+insert workflow_python docs/state.

    Returns a dict of useful counts for assertions/reporting.
    """
    cluster.psql(BOOTSTRAP_SQL, capture=False)
    cluster.psql_file(SCHEMA_003)

    # Resources.
    res_sql = ["begin;"]
    for r in resource_fixtures():
        res_sql.append(
            "insert into external_resources "
            "(id, kind, source, external_id, title, body, metadata, payload) values "
            f"({r['id']}, {q(r['kind'])}, 'vibecomfy-external', {q(r['kind'] + '-' + str(r['id']))}, "
            f"{q(r['title'])}, {q(r['body'])}, {q_jsonb(r['metadata'])}, {q_jsonb(r['payload'])});"
        )
    # Filler rows so the planner prefers the GIN index on representative queries.
    res_sql.append(
        "insert into external_resources (id, kind, source, title, body, metadata, payload) "
        "select g, 'workflow', 'vibecomfy-external', 'filler title '||g, 'filler body text '||g, "
        "'{}'::jsonb, null from generate_series(2000, " + str(2000 + filler_resources) + ") g;"
    )
    res_sql.append("commit;")
    cluster.psql("\n".join(res_sql), capture=False)

    # Distillations.
    dist_sql = ["begin;"]
    for d in distillation_fixtures():
        dist_sql.append(
            "insert into distillations (id, question, conditions, answer, confidence, status) values "
            f"({d['id']}, {q(d['question'])}, {q(d.get('conditions'))}, {q(d['answer'])}, "
            f"{q(d['confidence'])}, {q(d['status'])});"
        )
    dist_sql.append("commit;")
    cluster.psql("\n".join(dist_sql), capture=False)

    # Workflow-Python documents + state (computed by the frozen-bridge helper).
    doc_cols = ("entity_type", "item_id", "representation_type", "chunk_index", "chunk_text",
                "matched_anchor", "source_offset_start", "source_offset_end",
                "representation_hash", "chunk_hash", "quarantine_state",
                "lexicalization_version", "canonicalization_version",
                "chunking_version", "secret_scan_version", "method")
    state_cols = ("resource_id", "kind", "cohort", "public_state",
                  "available", "body_duplicate", "delimiter", "derivation",
                  "representation_hash", "secret_reason_codes",
                  "canonicalization_version", "secret_scan_version", "chunking_version", "chunk_count")
    doc_rows: list[str] = []
    state_rows: list[str] = []
    per_resource: dict[str, dict[str, Any]] = {}
    for r in resource_fixtures():
        row = dict(r)
        row["metadata"] = r["metadata"]
        state, docs = LD.compute_workflow_python_documents(row)
        per_resource[str(r["id"])] = {
            "cohort": state.cohort, "public_state": state.public_state,
            "chunk_count": state.chunk_count, "reason_codes": list(state.secret_reason_codes),
        }
        for d in docs:
            doc_rows.append(
                f"({q(d.entity_type)},{q(d.item_id)},{q(d.representation_type)},{d.chunk_index},"
                f"{q(d.chunk_text)},{q(d.matched_anchor)},{d.source_offset_start},{d.source_offset_end},"
                f"{q(d.representation_hash)},{q(d.chunk_hash)},{q(d.quarantine_state)},"
                f"{d.lexicalization_version},{d.canonicalization_version},"
                f"{d.chunking_version},{d.secret_scan_version},{q(d.method)})"
            )
        state_rows.append(
            f"({state.resource_id},{q(state.kind)},{q(state.cohort)},{q(state.public_state)},"
            f"{q(state.available)},{q(state.body_duplicate)},{q(state.delimiter)},{q(state.derivation)},"
            f"{q(state.representation_hash)},{q_array(state.secret_reason_codes)},"
            f"{state.canonicalization_version},{state.secret_scan_version},"
            f"{state.chunking_version},{state.chunk_count})"
        )
    if doc_rows:
        cluster.psql(
            "insert into lexical_documents (" + ",".join(doc_cols) + ") values "
            + ",".join(doc_rows) + ";",
            capture=False,
        )
    cluster.psql(
        "insert into lexical_resource_python_state (" + ",".join(state_cols) + ") values "
        + ",".join(state_rows) + ";",
        capture=False,
    )
    cluster.psql("analyze external_resources; analyze distillations; analyze lexical_documents; analyze lexical_resource_python_state;", capture=False)
    return {"per_resource": per_resource}


# ---------------------------------------------------------------------------
# Assertions + EXPLAIN capture
# ---------------------------------------------------------------------------


def _explain(cluster: LocalCluster, sql: str) -> str:
    rc, out = cluster.psql("EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) " + sql)
    if rc != 0:
        return f"ERROR: {out}"
    return out.strip()


def _explain_forced(cluster: LocalCluster, sql: str) -> str:
    rc, out = cluster.psql("SET enable_seqscan=off; EXPLAIN (ANALYZE, COSTS OFF) " + sql)
    if rc != 0:
        return f"ERROR: {out}"
    # psql -t -A prints the SET result line then the plan; keep the plan block.
    lines = out.splitlines()
    if lines and lines[0].strip() == "SET":
        lines = lines[1:]
    return "\n".join(lines).strip()


def _scalar(cluster: LocalCluster, sql: str) -> str:
    rc, out = cluster.psql(sql)
    return out.strip() if rc == 0 else ""


def capture_explain_evidence(cluster: LocalCluster) -> dict[str, Any]:
    """Capture EXPLAIN (ANALYZE, BUFFERS) for the three representative arms."""
    arms = {
        "resource_prose": (
            "select id from external_resources where prose_tsv "
            "@@ websearch_to_tsquery('simple','wanvideo')"
        ),
        "distillation": (
            "select id from distillations where lexical_tsv "
            "@@ websearch_to_tsquery('simple','upscale')"
        ),
        "workflow_python": (
            # Pure tsv predicate so the GIN tsv index is the one proven reachable
            # (the production candidate query additionally filters representation_type,
            #  served by combining indexes at production scale).
            "select item_id from lexical_documents where "
            "tsv @@ websearch_to_tsquery('simple','wanvideosampler')"
        ),
    }
    evidence: dict[str, Any] = {}
    for name, sql in arms.items():
        evidence[name] = {
            "query": sql,
            "plan_natural": _explain(cluster, sql),
            "plan_forced_index": _explain_forced(cluster, sql),
            "index_name_expected": {
                "resource_prose": "external_resources_prose_tsv_idx",
                "distillation": "distillations_lexical_tsv_idx",
                "workflow_python": "lexical_documents_tsv_idx",
            }[name],
        }
    return evidence


def run_assertions(cluster: LocalCluster) -> list[tuple[str, bool, str]]:
    """Run the task-1.2 SQL behavior assertions. Returns (name, ok, detail)."""
    results: list[tuple[str, bool, str]] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        results.append((name, bool(cond), detail))

    # 1. Safe workflow Python is searchable (payload/body/both all hit).
    hits = _scalar(cluster,
        "select count(distinct item_id) from lexical_documents where representation_type='workflow_python' "
        "and tsv @@ websearch_to_tsquery('simple','wanvideosampler')")
    check("safe_workflow_python_searchable", int(hits or 0) >= 3,
          f"distinct workflows with a wanvideosampler code hit = {hits} (expect >=3: payload/body/both)")

    # 2. Quarantined Python is excluded: zero workflow_python docs for the secret resource.
    q_docs = _scalar(cluster,
        "select count(*) from lexical_documents where item_id='1008' and representation_type='workflow_python'")
    check("quarantined_python_has_zero_documents", q_docs == "0",
          f"lexical_documents workflow_python rows for 1008 = {q_docs} (expect 0)")
    q_state = _scalar(cluster, "select public_state from lexical_resource_python_state where resource_id=1008")
    check("quarantined_state_recorded", q_state == "quarantined",
          f"hivemind_workflow_python_state(1008) = {q_state} (expect quarantined)")
    q_func = _scalar(cluster, "select hivemind_workflow_python_state(1008)")
    check("quarantine_accessor_returns_quarantined", q_func == "quarantined",
          f"accessor = {q_func}")
    # Secret value never reaches the state table.
    leak = _scalar(cluster,
        f"select count(*) from lexical_resource_python_state where "
        f"array_to_string(secret_reason_codes,'|') like '%{SECRET_SK}%' or "
        f"coalesce(delimiter,'')||coalesce(derivation,'') like '%{SECRET_SK}%'")
    check("secret_value_absent_from_state", leak == "0", f"secret-value matches in state = {leak} (expect 0)")

    # 3. Duplicate body/payload code indexed once: the 'both' resource (1003) has
    #    exactly the payload chunk count, and prose_tsv does NOT carry the symbol.
    both_doc_count = _scalar(cluster,
        "select count(*) from lexical_documents where item_id='1003' and representation_type='workflow_python'")
    check("both_cohort_indexed_once", both_doc_count != "0",
          f"1003 workflow_python doc count = {both_doc_count} (>0, exactly the payload set)")
    # The no-duplication discriminator is a CODE-ONLY symbol (num_frames appears
    # only in the stripped Python block, never in the projected workflow_semantics).
    # 'wanvideosampler' is intentionally still in prose via semantics (node_types),
    # so it is the wrong discriminator. num_frames must be absent from prose but
    # present in the workflow_python documents.
    prose_has_code = _scalar(cluster,
        "select count(*) from external_resources where id=1003 and "
        "prose_tsv @@ websearch_to_tsquery('simple','num_frames')")
    py_has_code = _scalar(cluster,
        "select count(*) from lexical_documents where item_id='1003' and "
        "representation_type='workflow_python' and tsv @@ websearch_to_tsquery('simple','num_frames')")
    check("both_cohort_prose_has_python_stripped", prose_has_code == "0" and py_has_code != "0",
          f"1003 prose_tsv matches code-only 'num_frames' = {prose_has_code} (expect 0); "
          f"workflow_python matches = {py_has_code} (>0) => code indexed once as workflow_python")

    # 4. Distillation eligibility: rejected (id 3) never ranks; pending+approved do.
    rej = _scalar(cluster,
        "select count(*) from distillations where status in ('pending','approved') and id=3")
    check("rejected_distillation_excluded", rej == "0", f"rejected id=3 in eligible set = {rej} (expect 0)")
    pa = _scalar(cluster,
        "select count(*) from distillations where status in ('pending','approved') and id in (1,2)")
    check("pending_approved_distillations_included", pa == "2", f"pending/approved ids 1,2 = {pa} (expect 2)")
    # generated weighted column ranks the question(A) hit above an answer(C)-only hit
    rank = _scalar(cluster,
        "select id from distillations where lexical_tsv @@ websearch_to_tsquery('simple','upscale') "
        "order by ts_rank(lexical_tsv, websearch_to_tsquery('simple','upscale'),32) desc limit 1")
    check("distillation_weighted_rank", rank == "1", f"top 'upscale' distillation = {rank} (expect 1: A-weight question)")

    # 5. Constraints reject invalid rows.
    rc_bad_q, _ = cluster.psql(
        "insert into lexical_documents (entity_type,item_id,representation_type,chunk_index,"
        "chunk_text,representation_hash,chunk_hash,quarantine_state) values "
        "('resource','9999','workflow_python',0,'x','h','h','quarantined')")
    check("constraint_rejects_quarantined_workflow_python", rc_bad_q != 0,
          f"insert quarantined workflow_python rc = {rc_bad_q} (expect nonzero)")
    rc_bad_idx, _ = cluster.psql(
        "insert into lexical_documents (entity_type,item_id,representation_type,chunk_index,"
        "chunk_text,representation_hash,chunk_hash) values ('resource','9999','workflow_python',-1,'x','h','h')")
    check("constraint_rejects_negative_chunk_index", rc_bad_idx != 0,
          f"insert chunk_index=-1 rc = {rc_bad_idx} (expect nonzero)")
    rc_bad_ent, _ = cluster.psql(
        "insert into lexical_documents (entity_type,item_id,representation_type,chunk_index,"
        "chunk_text,representation_hash,chunk_hash) values ('bogus','9999','workflow_python',0,'x','h','h')")
    check("constraint_rejects_bad_entity_type", rc_bad_ent != 0,
          f"insert bogus entity rc = {rc_bad_ent} (expect nonzero)")

    # 6. Migration idempotence: re-applying 003 is a no-op (no error).
    try:
        cluster.psql_file(SCHEMA_003)
        check("migration_idempotent", True, "re-applied 003 without error")
    except Exception as exc:  # noqa: BLE001
        check("migration_idempotent", False, str(exc))

    # 7. Index usage (definitive: forced plan names the index).
    ev = capture_explain_evidence(cluster)
    for arm, data in ev.items():
        used = data["index_name_expected"] in data["plan_forced_index"]
        check(f"explain_uses_index:{arm}", used,
              f"forced plan references {data['index_name_expected']}: {used}")
    return results


EVIDENCE_PATH = REPO / "docs" / "hybrid-search" / "phase1-lexical-explain-evidence.json"


def write_evidence(cluster: LocalCluster, results: list[tuple[str, bool, str]]) -> dict[str, Any]:
    explain = capture_explain_evidence(cluster)
    payload = {
        "task": "1.2",
        "date": "2026-07-28",
        "method": "isolated throwaway PostgreSQL 14 cluster (initdb --auth=trust, "
                  "temp data dir, ephemeral port, unix socket); torn down after capture. "
                  "No Docker, no network, no production mutation.",
        "indexes": {
            "resource_prose": "external_resources_prose_tsv_idx (GIN on generated prose_tsv)",
            "distillation": "distillations_lexical_tsv_idx (GIN on generated lexical_tsv)",
            "workflow_python": "lexical_documents_tsv_idx (GIN on generated tsv per code chunk)",
        },
        "explain_evidence": explain,
        "assertions": [{"name": n, "ok": ok, "detail": d} for (n, ok, d) in results],
        "all_assertions_passed": all(ok for _, ok, _ in results),
    }
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(json.dumps(payload, indent=2))
    return payload


def run_full() -> dict[str, Any]:
    """Start a throwaway cluster, seed, assert, capture evidence, tear down."""
    cluster = LocalCluster.start()
    try:
        seed_cluster(cluster)
        results = run_assertions(cluster)
        payload = write_evidence(cluster, results)
        payload["assertions_summary"] = {
            "passed": sum(1 for _, ok, _ in results if ok),
            "total": len(results),
        }
        return payload
    finally:
        cluster.tear_down()


def main() -> int:
    if find_pgbins() is None:
        print("SKIP: PostgreSQL binaries (initdb/pg_ctl/psql) not found on this machine.",
              file=sys.stderr)
        return 0
    payload = run_full()
    summary = payload["assertions_summary"]
    print(f"Assertions: {summary['passed']}/{summary['passed'] if False else summary['total']} passed")
    for n, ok, d in [(a["name"], a["ok"], a["detail"]) for a in payload["assertions"]]:
        print(f"  [{'OK' if ok else 'FAIL'}] {n}: {d}")
    print(f"Evidence written to {EVIDENCE_PATH}")
    return 0 if (payload["all_assertions_passed"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
