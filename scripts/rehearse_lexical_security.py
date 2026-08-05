#!/usr/bin/env python3
"""Throwaway isolated-PostgreSQL rehearsal for the lexical security-hardening
migration (schema/011, hybrid-search plan task 1.12).

Proves on an ISOLATED throwaway PostgreSQL 14+ cluster (``initdb --auth=trust``,
temp data dir, ephemeral port + unix socket) that after applying migrations
001 -> 010 and then 011:

  * anon / authenticated are BLOCKED from executing
    ``hivemind_lexical_candidates`` (permission denied).
  * anon / authenticated are BLOCKED from reading AND writing
    ``lexical_documents`` and ``lexical_resource_python_state``.
  * anon is BLOCKED from executing ``hivemind_workflow_python_state(bigint)``.
  * service_role CAN call the hardened RPC ``hivemind_lexical_search``
    (the sole legitimate read path).
  * Eligibility still holds: rejected distillation / soft-deleted message /
    quarantined workflow_python never appear in RPC results (the RPC reaches
    the helper via SECURITY DEFINER / service_role, so revoking public execute
    on the helper does NOT break the RPC).
  * 011 is idempotent (applying it twice is a no-op).

ROLLBACK: REVOKE has no clean inverse — the only way to restore the pre-011
default-privilege state is to explicitly re-GRANT. This script does NOT roll
back (it tears the throwaway cluster down), but a production rollback would
restore privileges via explicit GRANT, e.g.::

    -- restoring the PRE-011 default-executable / default-table-privilege state
    -- (NOT recommended; 011 is the hardened state. Documented for completeness.)
    grant execute on function public.hivemind_lexical_candidates(
        text,int,text[],text[],text[],timestamptz,text[],text[],boolean,boolean)
      to public, anon, authenticated;
    grant all on public.lexical_documents to anon, authenticated;
    grant all on public.lexical_resource_python_state to anon, authenticated;
    grant execute on function public.hivemind_workflow_python_state(bigint)
      to public, anon, authenticated;

No Docker, no network, no production mutation. Writes a verdict JSON to
``docs/hybrid-search/phase1-lexical-011-rehearsal.json``.

Reuses the cluster lifecycle + seeding in :mod:`scripts.rehearse_lexical_candidate`
(:mod:`scripts.lexical_pg` LocalCluster).
"""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import rehearse_lexical_candidate as R  # noqa: E402
import lexical_pg as LP  # noqa: E402

SCHEMA_011 = REPO / "schema" / "011_lexical_security_hardening.sql"
VERDICT_PATH = REPO / "docs" / "hybrid-search" / "phase1-lexical-011-rehearsal.json"

# Supabase default that the bare initdb cluster does NOT replicate: anon,
# authenticated, and service_role all have USAGE on schema public so they can
# resolve `public.<object>`. Restored here so the per-object privilege (the
# thing 011 narrows) is what actually decides access — not an unrelated
# schema-resolution failure.
GRANT_SUPABASE_DEFAULTS = (
    "grant usage on schema public to anon, authenticated, service_role;"
)


# ---------------------------------------------------------------------------
# Role-scoped probes
# ---------------------------------------------------------------------------


def role_errors(cluster: LP.LocalCluster, role: str, sql: str) -> bool:
    """True if `SET ROLE <role>; <sql>` errors (rc != 0)."""
    rc, _ = cluster.psql(f"set role {role}; {sql}")
    cluster.psql("reset role;")
    return rc != 0


def role_ok(cluster: LP.LocalCluster, role: str, sql: str) -> tuple[bool, str]:
    rc, out = cluster.psql(f"set role {role}; {sql}")
    cluster.psql("reset role;")
    return rc == 0, out


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


def run_checks(cluster: LP.LocalCluster) -> list[dict[str, Any]]:
    """Run every security/eligibility/idempotence check. Returns list of
    {name, ok, detail}."""
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    # --------------------------------------------------------------
    # 1. anon / authenticated blocked from hivemind_lexical_candidates.
    # --------------------------------------------------------------
    cand_sql = "select * from public.hivemind_lexical_candidates('x', 1) limit 1;"
    for role in ("anon", "authenticated"):
        add(f"blocked_candidate_fn:{role}", role_errors(cluster, role, cand_sql),
            f"SET ROLE {role}; candidate fn call must ERROR (permission denied)")

    # --------------------------------------------------------------
    # 2. anon / authenticated blocked from reading the two tables.
    # --------------------------------------------------------------
    for role in ("anon", "authenticated"):
        add(f"blocked_read_lexical_documents:{role}",
            role_errors(cluster, role, "select count(*) from public.lexical_documents;"),
            f"SET ROLE {role}; select count(*) lexical_documents must ERROR")
        add(f"blocked_read_python_state:{role}",
            role_errors(cluster, role, "select count(*) from public.lexical_resource_python_state;"),
            f"SET ROLE {role}; select count(*) lexical_resource_python_state must ERROR")

    # --------------------------------------------------------------
    # 3. anon blocked from WRITING the two tables (read+write both closed).
    # --------------------------------------------------------------
    ins_doc = (
        "insert into public.lexical_documents "
        "(entity_type, item_id, representation_type, chunk_index, "
        "chunk_text, representation_hash, chunk_hash) values "
        "('resource','9999','workflow_python',0,'x','h','h');"
    )
    ins_state = (
        "insert into public.lexical_resource_python_state "
        "(resource_id, kind, cohort, public_state, available) values "
        "(999998,'workflow','payload_python','safe',true);"
    )
    add("blocked_write_lexical_documents:anon",
        role_errors(cluster, "anon", ins_doc),
        "anon insert into lexical_documents must ERROR")
    add("blocked_write_python_state:anon",
        role_errors(cluster, "anon", ins_state),
        "anon insert into lexical_resource_python_state must ERROR")

    # --------------------------------------------------------------
    # 4. anon blocked from the workflow_python_state helper.
    # --------------------------------------------------------------
    add("blocked_workflow_python_state_helper:anon",
        role_errors(cluster, "anon", "select public.hivemind_workflow_python_state(20);"),
        "anon select workflow_python_state(20) must ERROR")

    # --------------------------------------------------------------
    # 5. service_role CAN call the hardened RPC (legitimate read path).
    # --------------------------------------------------------------
    rpc_sql = ("select public.hivemind_lexical_search('WanVideoSampler',5,"
               "'{}','{}','{}',null,'{}','{}','lexical')::text;")
    ok, out = role_ok(cluster, "service_role", rpc_sql)
    detail = f"service_role RPC call rc_ok={ok}"
    if ok:
        start = out.find("{")
        end = out.rfind("}")
        if start >= 0 and end > start:
            try:
                env = json.loads(out[start:end + 1])
                detail += f"; count={env.get('count')} results={len(env.get('results', []))}"
            except json.JSONDecodeError as exc:
                ok = False
                detail += f"; JSON decode failed: {exc}"
        else:
            ok = False
            detail += "; no JSON envelope in output"
    add("service_role_rpc_callable", ok, detail)

    # --------------------------------------------------------------
    # 6. Eligibility preserved (RPC reaches helper via SECURITY DEFINER).
    # --------------------------------------------------------------
    # 6a. Rejected distillation absent.
    cluster.psql(
        "insert into public.distillations (id, question, conditions, answer, "
        "confidence, status, author_id) overriding system value values "
        "(777,'How do I reduce motion strength rejected','x','y','low','rejected',1) "
        "on conflict do nothing;"
    )
    resp = R.call_rpc(cluster, "reduce motion strength")
    dist_ids = {r["item_id"] for r in resp["results"] if r["kind"] == "distillation"}
    add("eligibility_rejected_distillation_absent",
        "777" not in dist_ids,
        f"rejected distillation 777 absent from RPC results; dist_ids={sorted(dist_ids)}")

    # 6b. Soft-deleted message absent.
    deleted_msg = str(1_000_000_000_000_000_000 + 0)  # i=0 is soft-deleted in seed
    resp = R.call_rpc(cluster, "sampler settings for video")
    msg_ids = {r["item_id"] for r in resp["results"] if r["kind"] == "message"}
    add("eligibility_softdeleted_message_absent",
        deleted_msg not in msg_ids,
        f"soft-deleted message {deleted_msg} absent from RPC results")

    # 6c. Quarantined workflow_python: the CODE never surfaces (structural
    # exclusion + the candidate SQL's safe_wf gate). Quarantine excludes only
    # the workflow_python representation, not the resource's prose — so the
    # precise assertion is: zero workflow_python docs for the quarantined
    # resource AND its code never ranks via the workflow_python arms.
    cluster.psql(
        "insert into public.external_resources (id, kind, source, external_id, "
        "title, body, author, url, metadata) overriding system value values "
        "(31337,'workflow','vibecomfy-external','w31337','QuarantinedCredentialWorkflow',"
        "'Workflow prose mentions WanVideoSampler but its code is quarantined',"
        "'agent',null,'{}') on conflict do nothing;"
    )
    cluster.psql(
        "insert into public.lexical_resource_python_state "
        "(resource_id, kind, cohort, public_state, available, chunk_count) values "
        "(31337,'workflow','payload_python','quarantined',false,0) "
        "on conflict (resource_id) do update set public_state='quarantined', available=false;"
    )
    # Structural: zero workflow_python docs for the quarantined resource.
    rc, out = cluster.psql(
        "select count(*) from public.lexical_documents "
        "where item_id='31337' and representation_type='workflow_python';"
    )
    quar_docs = out.strip()
    add("eligibility_quarantined_zero_workflow_docs",
        rc == 0 and quar_docs == "0",
        f"quarantined resource 31337 workflow_python doc count = {quar_docs} (expect 0)")
    # Gate: the candidate fn never emits a workflow_python row for 31337.
    rc, out = cluster.psql(
        "select representation_type from public.hivemind_lexical_candidates("
        "'WanVideoSampler', 500, '{workflow}','{}','{}',null,'{}','{}',false,false) "
        "where item_id='31337';"
    )
    reps = {ln.strip() for ln in out.splitlines() if ln.strip()}
    add("eligibility_quarantined_code_never_ranks",
        rc == 0 and "workflow_python" not in reps,
        f"candidate fn never emits workflow_python for 31337; reps={sorted(reps)}")
    # The helper is a PRIVATE internal routine. After 011, NO role (including
    # service_role) can call hivemind_workflow_python_state DIRECTLY — 003 only
    # made it reachable via the public default, which 011 revoked. It is reached
    # ONLY through the SECURITY DEFINER RPC (which runs as its owner postgres).
    # The proof the path is intact is therefore: the RPC succeeds (it internally
    # calls the helper). That is already proven above (service_role_rpc_callable
    # returned count=5). Confirm the direct-call is denied as a defense check.
    direct_ok, _ = role_ok(cluster, "service_role",
                           "select public.hivemind_workflow_python_state(31337);")
    add("helper_not_directly_callable_by_service_role",
        not direct_ok,
        "direct service_role call to workflow_python_state must be DENIED; "
        "it is reached only via the SECURITY DEFINER RPC (proven callable above)")

    # --------------------------------------------------------------
    # 7. Idempotence: applying 011 a second time is a no-op.
    # --------------------------------------------------------------
    try:
        cluster.psql_file(SCHEMA_011)
        add("migration_idempotent", True, "re-applied 011 without error")
    except Exception as exc:  # noqa: BLE001
        add("migration_idempotent", False, f"re-apply errored: {exc}")

    return checks


def rehearse(out_path: pathlib.Path | None = None) -> dict[str, Any]:
    cluster = LP.LocalCluster.start()
    payload: dict[str, Any]
    try:
        R.reset_schema(cluster)
        R.bootstrap(cluster)  # applies 001 + 003..009
        cluster.psql_file(R.SCHEMA_DIR / "010_lexical_latency_fix.sql")
        cluster.psql_file(SCHEMA_011)
        # Restore the Supabase-default schema USAGE the bare cluster lacks so
        # the per-object privilege (the thing 011 narrows) is what decides.
        cluster.psql(GRANT_SUPABASE_DEFAULTS, capture=False)
        R.seed(cluster, n_messages=4000)
        checks = run_checks(cluster)
        payload = {
            "task": "1.12",
            "date": "2026-07-29",
            "method": (
                "isolated throwaway PostgreSQL 14+ cluster (initdb --auth=trust, "
                "temp data dir, ephemeral port, unix socket); torn down after "
                "capture. No Docker, no network, no production mutation."
            ),
            "migrations_applied": list(R.MIGRATIONS) + [
                "010_lexical_latency_fix.sql", "011_lexical_security_hardening.sql",
            ],
            "trust_boundary": {
                "candidate_fn": "hivemind_lexical_candidates — revoked from public/anon/authenticated",
                "tables": ["lexical_documents", "lexical_resource_python_state"],
                "helper": "hivemind_workflow_python_state — revoked from public/anon/authenticated",
                "preserved_rpc": "hivemind_lexical_search grant to service_role (009:268) untouched",
                "note": "anon/authenticated retain NOTHING on these four objects; "
                        "RLS (no permissive policy) remains as defense-in-depth.",
            },
            "rollback_note": (
                "REVOKE has no clean inverse. Rollback restores default privileges via "
                "explicit GRANT (candidate fn -> public/anon/authenticated; both tables "
                "-> anon/authenticated; helper -> public/anon/authenticated). NOT "
                "recommended — 011 is the hardened state. See module docstring."
            ),
            "checks": checks,
            "all_pass": all(c["ok"] for c in checks),
            "n_pass": sum(1 for c in checks if c["ok"]),
            "n_total": len(checks),
        }
    finally:
        cluster.tear_down()

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="Lexical security-hardening (011) rehearsal.")
    p.add_argument("--out-path", type=pathlib.Path, default=VERDICT_PATH)
    args = p.parse_args(argv)
    payload = rehearse(args.out_path)
    verdict = "PASS" if payload["all_pass"] else "FAIL"
    print(f"011 rehearsal verdict: {verdict} "
          f"({payload['n_pass']}/{payload['n_total']} checks)")
    for c in payload["checks"]:
        flag = "OK  " if c["ok"] else "FAIL"
        print(f"  [{flag}] {c['name']}: {c['detail']}")
    print(f"verdict written to {args.out_path}")
    return 0 if payload["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
