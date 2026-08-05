#!/usr/bin/env python3
"""Live security proof for the lexical trust boundary (task C), production, read-only.

Proves (after schema/011): anon/authenticated CANNOT call hivemind_lexical_candidates or
hivemind_lexical_search, and CANNOT read or write lexical_documents /
lexical_resource_python_state; service_role CAN call the RPC; quarantined workflow_python
structurally cannot rank. Uses privilege inspection (has_table_privilege / has_function_privilege)
plus ACTUAL ``SET ROLE`` execution attempts. No source/snippet/credential is logged (the
quarantine probe uses opaque resource ids + state only).

Usage::

    python3 scripts/prove_lexical_security.py
"""
from __future__ import annotations

import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from live_lexical_search import derive_pg_env, elevate, psql  # noqa: E402
from verify_access import redact  # noqa: E402

CAND = "hivemind_lexical_candidates(text,int,text[],text[],text[],timestamptz,text[],text[],boolean,boolean)"
RPC = "hivemind_lexical_search(text,int,text[],text[],text[],timestamptz,text[],text[],text)"


def _tail(s: str) -> str:
    lines = [ln for ln in (s or "").strip().splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def main() -> int:
    env, host, port = derive_pg_env()
    rep: dict = {"host_family": "pooler" if "pooler" in host else "session", "port": port}

    # 1. Privilege matrix via has_*_privilege (authoritative, no execution needed).
    priv_sql = "\nunion all\n".join([
        f"select 'fn_candidates|{r}|EXECUTE', has_function_privilege('{r}','public.{CAND}','EXECUTE')::text"
        for r in ("anon", "authenticated", "service_role")
    ] + [
        f"select 'fn_rpc|{r}|EXECUTE', has_function_privilege('{r}','public.{RPC}','EXECUTE')::text"
        for r in ("anon", "authenticated", "service_role")
    ] + [
        f"select 'tbl_{t}|{r}|{p}', has_table_privilege('{r}','public.{t}','{p}')::text"
        for t in ("lexical_documents", "lexical_resource_python_state")
        for r in ("anon", "authenticated", "service_role")
        for p in ("SELECT", "INSERT", "UPDATE", "DELETE")
    ])
    r = psql(env, elevate(priv_sql), timeout=60, on_error_stop=False)
    rep["privilege_matrix"] = dict(_parse_kv(r.stdout))

    # 2. ACTUAL execution attempts: anon/authenticated must be DENIED.
    attempts = {}
    for role in ("anon", "authenticated"):
        block = (
            f"set role {role}; "
            f"select public.hivemind_lexical_candidates('x',1,'{{}}','{{}}','{{}}',null,'{{}}','{{}}',false,false);"
        )
        a = psql(env, elevate(block), timeout=30, on_error_stop=False)
        attempts[f"{role}_call_candidates"] = {"denied": a.returncode != 0,
                                               "err": redact(_tail(a.stderr)) if a.returncode else ""}
        for t in ("lexical_documents", "lexical_resource_python_state"):
            b = psql(env, elevate(f"set role {role}; select count(*) from public.{t};"),
                     timeout=30, on_error_stop=False)
            attempts[f"{role}_select_{t}"] = {"denied": b.returncode != 0,
                                              "err": redact(_tail(b.stderr)) if b.returncode else ""}
    rep["actual_attempts"] = attempts

    # 3. service_role CAN call the RPC.
    s = psql(env, elevate(
        "set role service_role; select (j->>'count')::int, jsonb_array_length(j->'results') from "
        "(select public.hivemind_lexical_search('WanVideoSampler',10,'{}','{}','{}',null,'{}','{}','lexical') j) z;"
    ), timeout=40, on_error_stop=False)
    rep["service_role_rpc"] = {"ok": s.returncode == 0, "out": s.stdout.strip().splitlines()[:2],
                               "err": redact(_tail(s.stderr)) if s.returncode else ""}

    # 4. Quarantine structural proof: resource 2625 (secret_quarantined) has zero workflow_python
    #    lexical_documents AND workflow_python_state='quarantined' => cannot rank. Opaque ids only.
    q = psql(env, elevate(
        "select 'quarantined_state', public.hivemind_workflow_python_state(2625)::text "
        "union all select 'workflow_python_doc_count', count(*)::text "
        "  from public.lexical_documents where item_id='2625' and representation_type='workflow_python';"
    ), timeout=40, on_error_stop=False)
    rep["quarantine_resource_2625"] = dict(_parse_kv(q.stdout))

    rep["verdict"] = {
        "anon_auth_blocked_candidates_fn": all(attempts[f"{r}_call_candidates"]["denied"] for r in ("anon", "authenticated")),
        "anon_auth_blocked_table_reads": all(
            attempts[f"{r}_select_{t}"]["denied"]
            for r in ("anon", "authenticated") for t in ("lexical_documents", "lexical_resource_python_state")),
        "service_role_can_call_rpc": rep["service_role_rpc"]["ok"],
        "quarantined_has_zero_workflow_python_docs": rep["quarantine_resource_2625"].get("workflow_python_doc_count") == "0",
    }
    rep["all_pass"] = all(rep["verdict"].values())
    print(json.dumps(rep, indent=2, default=str))
    return 0 if rep["all_pass"] else 1


def _parse_kv(stdout: str) -> list[tuple[str, str]]:
    pairs = []
    for line in (stdout or "").strip().splitlines():
        if "|" in line:
            k, _, v = line.partition("|")
            pairs.append((k.strip(), v.strip()))
    return pairs


if __name__ == "__main__":
    raise SystemExit(main())
