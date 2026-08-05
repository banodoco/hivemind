#!/usr/bin/env python3
"""Live (production) driver for the lexical candidate SQL + RPC (tasks 1.7–1.9).

Read-only preflight + gated online apply + read-only evidence + rollback, against
the Hivemind Supabase project, over the SAME session-mode access path as the other
Phase-1 live drivers (``supabase db dump --schema public --dry-run`` derives a
short-lived CLI-login libpq env held only in a child-process env; the credential is
never written to disk/logs/output — every line is routed through ``redact``).

The migration set is additive + idempotent:
  * schema/008 — the canonical candidate function ``hivemind_lexical_candidates``
    + the bounded workflow-code fragment trigram index (CREATE INDEX CONCURRENTLY).
  * schema/009 — the hardened SECURITY DEFINER RPC ``hivemind_lexical_search``
    (revoke from public/anon/authenticated; grant to service_role).

Apply is gated on the isolated-cluster rehearsal verdict being GREEN and a read-only
preflight being GREEN. Rollback drops the RPC, the candidate function, and the index
(no source row is touched). Read-only throughout except the explicit ``--apply``.

Usage::

    python3 scripts/live_lexical_search.py --preflight      # read-only state check
    python3 scripts/live_lexical_search.py --apply          # gated online apply
    python3 scripts/live_lexical_search.py --evidence       # read-only EXPLAIN + smoke
    python3 scripts/live_lexical_search.py --rollback       # drop the objects
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from verify_access import redact  # noqa: E402

SCHEMA_008 = REPO / "schema" / "008_lexical_candidate_sql.sql"
SCHEMA_009 = REPO / "schema" / "009_lexical_search_rpc.sql"
REHEARSAL_EVIDENCE = REPO / "docs" / "hybrid-search" / "phase1-lexical-candidate-rehearsal.json"
EVIDENCE_OUT = REPO / "docs" / "hybrid-search" / "phase1-lexical-live.json"

ELEVATE_ROLE = "postgres"


def _tail(text: str) -> str:
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def derive_pg_env() -> tuple[dict, str, str]:
    """Derive a short-lived CLI-login libpq env (credential in child-process env only)."""
    from verify_access import parse_dryrun_pg_env

    proc = subprocess.run(
        ["supabase", "db", "dump", "--schema", "public", "--dry-run"],
        capture_output=True, text=True, timeout=40.0, stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"supabase db dump --dry-run failed (rc={proc.returncode}): {redact(_tail(proc.stderr))}"
        )
    pg = parse_dryrun_pg_env(proc.stdout)
    if "PGHOST" not in pg or "PGPASSWORD" not in pg:
        raise RuntimeError("could not derive CLI login env (PGHOST/PGPASSWORD) from dry-run")
    return {**os.environ, **pg}, pg.get("PGHOST", ""), pg.get("PGPORT", "")


def psql(env: dict, sql: str, *, timeout: float = 60.0, on_error_stop: bool = True) -> Any:
    args = ["psql", "-X", "-q", "-t", "-A", "-P", "pager=off",
            "-v", f"ON_ERROR_STOP={'1' if on_error_stop else '0'}", "-c", sql]
    proc = subprocess.run(args, env=env, capture_output=True, text=True, timeout=timeout)
    return proc


def elevate(sql_text: str) -> str:
    return f"SET ROLE {ELEVATE_ROLE};\n" + sql_text


# ---------------------------------------------------------------------------
# Preflight (read-only)
# ---------------------------------------------------------------------------


PREFLIGHT_SQL = """
select 'fn_normalize' as k,
       (exists (select 1 from pg_proc p join pg_namespace n on n.oid=p.pronamespace
                 where n.nspname='public' and p.proname='hivemind_normalize_identifier'
                   and p.pronargs=1))::text as v
union all select 'tbl_lexical_documents',
       (exists (select 1 from pg_class c join pg_namespace n on n.oid=c.relnamespace
                 where n.nspname='public' and c.relname='lexical_documents'))::text
union all select 'col_prose_tsv',
       (exists (select 1 from pg_attribute a join pg_class c on c.oid=a.attrelid
                  join pg_namespace n on n.oid=c.relnamespace
                 where n.nspname='public' and c.relname='external_resources'
                   and a.attname='prose_tsv' and not a.attisdropped))::text
union all select 'col_lexical_tsv',
       (exists (select 1 from pg_attribute a join pg_class c on c.oid=a.attrelid
                  join pg_namespace n on n.oid=c.relnamespace
                 where n.nspname='public' and c.relname='distillations'
                   and a.attname='lexical_tsv' and not a.attisdropped))::text
union all select 'fn_workflow_python_state',
       (exists (select 1 from pg_proc p join pg_namespace n on n.oid=p.pronamespace
                 where n.nspname='public' and p.proname='hivemind_workflow_python_state'))::text
union all select 'idx_msg_fts_simple',
       (exists (select 1 from pg_class c join pg_namespace n on n.oid=c.relnamespace
                 where n.nspname='public' and c.relname='idx_discord_messages_content_fts_simple'))::text
union all select 'idx_msg_ident_trgm',
       (exists (select 1 from pg_class c join pg_namespace n on n.oid=c.relnamespace
                 where n.nspname='public' and c.relname='idx_discord_messages_identifier_trgm'))::text
union all select 'fn_candidates',
       (exists (select 1 from pg_proc p join pg_namespace n on n.oid=p.pronamespace
                 where n.nspname='public' and p.proname='hivemind_lexical_candidates'))::text
union all select 'fn_rpc',
       (exists (select 1 from pg_proc p join pg_namespace n on n.oid=p.pronamespace
                 where n.nspname='public' and p.proname='hivemind_lexical_search'))::text
union all select 'idx_python_chunk_trgm',
       (exists (select 1 from pg_class c join pg_namespace n on n.oid=c.relnamespace
                 where n.nspname='public' and c.relname='lexical_documents_python_chunk_trgm_idx'))::text;
"""

LD_STATS_SQL = """
select 'lexical_documents_rows', count(*)::text from public.lexical_documents
union all select 'lexical_documents_python_chunks',
       count(*)::text from public.lexical_documents where representation_type='workflow_python';
"""

LONG_TXN_SQL = """
select count(*)::text from pg_stat_activity
 where state in ('active','idle in transaction')
   and now()-state_change > interval '60 seconds'
   and pid <> pg_backend_pid();
"""


def run_preflight(env: dict, host: str, port: str) -> dict:
    out: dict[str, Any] = {"host_family": "pooler" if "pooler.supabase.com" in host else "session",
                           "port": port}
    r = psql(env, elevate(PREFLIGHT_SQL), timeout=40, on_error_stop=False)
    present = dict(_parse_kv(r.stdout))
    out["objects"] = present
    out["preflight_raw_rc"] = r.returncode
    if r.returncode != 0:
        out["preflight_error"] = redact(_tail(r.stderr))

    r2 = psql(env, elevate(LD_STATS_SQL), timeout=40, on_error_stop=False)
    out["lexical_documents"] = dict(_parse_kv(r2.stdout))

    r3 = psql(env, elevate(LONG_TXN_SQL), timeout=30, on_error_stop=False)
    out["long_txns_gt_60s"] = (dict(_parse_kv(r3.stdout)).get("count") or
                               (r3.stdout.strip() or "").splitlines()[:1])

    # Green iff: every Phase-1 prerequisite the candidate SQL + RPC reference is
    # live (schema/003 task-1.2 objects, schema/004/005/007 indexes/fns), AND the
    # 008/009 objects are either already present (idempotent) or absent (fresh).
    prereq_keys = (
        "fn_normalize",            # schema/005
        "idx_msg_fts_simple",      # schema/004
        "idx_msg_ident_trgm",      # schema/007
        "tbl_lexical_documents",   # schema/003 (task 1.2)
        "col_prose_tsv",           # schema/003
        "col_lexical_tsv",         # schema/003
        "fn_workflow_python_state",  # schema/003
    )
    missing = [k for k in prereq_keys if present.get(k) != "true"]
    out["prereq_missing"] = missing
    out["prereq_ok"] = not missing
    out["green"] = not missing
    if missing:
        out["blocker"] = (
            "candidate SQL/RPC live apply is blocked: Phase-1 prerequisites from "
            "schema/003 (task 1.2) are not yet live on production ("
            + ", ".join(missing)
            + "). Apply schema/003 first, then 008, then 009. The 008/009 code is "
            "proven correct on the isolated-cluster rehearsal (full 003->009 chain)."
        )
    return out


def parse_rpc_envelope(raw: str) -> dict:
    """Parse the lexical-search RPC envelope from a psql row OR a JSON string.

    The RPC (schema/009:239-249) returns an ENVELOPE object::

        {"results": [...], "count": <int>, "meta": {...}}

    It does NOT return a bare array, so ``jsonb_array_length`` on the top-level
    object yields NULL (the bug this function fixes).

    This pure helper accepts either of two shapes and returns
    ``{"count": int, "results_len": int}``:

    1. The ``count|results_len`` two-column row produced by the live evidence
       arm's ``-t -A`` psql query (e.g. ``"20|20"``). This is the preferred
       live form: the SQL reads ``(j->>'count')::int`` and
       ``jsonb_array_length(j->'results')`` so it never calls
       ``jsonb_array_length`` on the top-level envelope.
    2. The raw envelope JSON object itself (single column). This is the
       regression form: it reproduces what the OLD buggy code was fed and
       proves the object is now read correctly instead of being passed to
       ``jsonb_array_length``.

    Returns ``{"count": -1, "results_len": -1}`` when the input cannot be
    parsed (never raises) so callers can treat a failed smoke as a failure
    rather than crashing the evidence capture.
    """
    import json as _json

    text = (raw or "").strip()
    if not text:
        return {"count": -1, "results_len": -1}

    # Form 1: two-column "count|results_len" psql row (preferred live form).
    # psql -A uses an unquoted pipe separator; a leading/standalone NULL would
    # come through as the literal token but we still coerce defensively.
    if "|" in text and not text.startswith("{"):
        left, _, right = text.partition("|")
        try:
            count = int(left.strip())
        except (TypeError, ValueError):
            count = -1
        try:
            results_len = int(right.strip())
        except (TypeError, ValueError):
            results_len = -1
        return {"count": count, "results_len": results_len}

    # Form 2: the raw envelope JSON object (or a JSON-encoded anything).
    try:
        obj = _json.loads(text)
    except (TypeError, ValueError):
        return {"count": -1, "results_len": -1}

    if isinstance(obj, dict):
        results = obj.get("results", [])
        results_len = len(results) if isinstance(results, list) else -1
        try:
            count = int(obj.get("count", -1))
        except (TypeError, ValueError):
            count = -1
        return {"count": count, "results_len": results_len}

    return {"count": -1, "results_len": -1}


def _parse_kv(stdout: str) -> list[tuple[str, str]]:
    pairs = []
    for line in (stdout or "").strip().splitlines():
        if "|" in line:
            k, _, v = line.partition("|")
            pairs.append((k.strip(), v.strip()))
        elif line.strip():
            pairs.append((line.strip(), ""))
    return pairs


def rehearsal_gate() -> dict:
    if not REHEARSAL_EVIDENCE.exists():
        return {"available": False, "green": False,
                "reason": f"rehearsal evidence not found: {REHEARSAL_EVIDENCE.name}"}
    ev = json.loads(REHEARSAL_EVIDENCE.read_text())
    v = ev.get("verdict", {})
    return {"available": True, "green": bool(v.get("all_pass")),
            "n_pass": v.get("n_pass"), "n_total": v.get("n_total")}


# ---------------------------------------------------------------------------
# Apply / rollback
# ---------------------------------------------------------------------------


def apply_migrations(env: dict, *, lock_timeout_s: int = 30,
                     statement_timeout_s: int = 600) -> dict:
    """Apply schema/008 then schema/009 online (functions + CONCURRENT index).

    The CLI login role has no CREATE on schema public, so each migration runs as
    ``SET ROLE postgres``. SET ROLE is session-scoped, so it is prepended to the
    migration in a temp file run with ``psql -f`` (autocommit per statement, which
    keeps CREATE INDEX CONCURRENTLY legal), preceded by bounded lock_timeout +
    statement_timeout."""
    import tempfile
    prelude = (
        f"SET ROLE {ELEVATE_ROLE};\n"
        f"set lock_timeout='{lock_timeout_s}s';\n"
        f"set statement_timeout='{statement_timeout_s}s';\n"
    )
    results = {}
    for label, path in (("008", SCHEMA_008), ("009", SCHEMA_009)):
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as tf:
            tf.write(prelude + path.read_text(encoding="utf-8"))
            tmp = pathlib.Path(tf.name)
        try:
            proc = subprocess.run(
                ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", str(tmp)],
                env=env, capture_output=True, text=True, timeout=statement_timeout_s + 60,
            )
        finally:
            tmp.unlink(missing_ok=True)
        results[label] = {"ok": proc.returncode == 0,
                          "stderr_tail": redact(_tail(proc.stderr)) if proc.returncode else ""}
        if proc.returncode != 0:
            break
    return {"applied": results, "all_ok": all(r.get("ok") for r in results.values())}


def rollback(env: dict) -> dict:
    """Drop the RPC, the candidate function, and the fragment index. Idempotent."""
    sql = """
    drop function if exists public.hivemind_lexical_search(text,int,text[],text[],text[],timestamptz,text[],text[],text);
    drop function if exists public.hivemind_lexical_candidates(text,int,text[],text[],text[],timestamptz,text[],text[],boolean,boolean);
    drop index if exists public.lexical_documents_python_chunk_trgm_idx;
    """
    r = psql(env, elevate(sql), timeout=120, on_error_stop=False)
    return {"ok": r.returncode == 0, "stderr_tail": redact(_tail(r.stderr)) if r.returncode else ""}


# ---------------------------------------------------------------------------
# Evidence (read-only)
# ---------------------------------------------------------------------------


def capture_evidence(env: dict) -> dict:
    ev: dict[str, Any] = {}
    # Function validity + grant state (security check).
    r = psql(env, elevate("""
      select p.proname, pg_function_is_visible(p.oid)
        from pg_proc p join pg_namespace n on n.oid=p.pronamespace
       where n.nspname='public'
         and p.proname in ('hivemind_lexical_candidates','hivemind_lexical_search');
    """), timeout=40, on_error_stop=False)
    ev["functions"] = r.stdout.strip().splitlines()

    # RPC smoke: count + results-length over a real query, reading the
    # ENVELOPE fields directly. The RPC returns {results,count,meta} (not a
    # bare array), so we read count via ->>'count' and the array length via
    # jsonb_array_length(j->'results') — never jsonb_array_length on the
    # top-level object (the pre-fix bug yielded NULL on a healthy RPC).
    # No message bodies, snippets, or credentials are recorded: count + length only.
    r2 = psql(env, elevate(
        "select (j->>'count')::int, jsonb_array_length(j->'results') from "
        "(select public.hivemind_lexical_search('WanVideoSampler',10,'{}','{}','{}',null,'{}','{}','lexical') j) s;"
    ), timeout=40, on_error_stop=False)
    ev["rpc_smoke_rc"] = r2.returncode
    if r2.returncode == 0:
        parsed = parse_rpc_envelope(r2.stdout)
        ev["rpc_smoke_count"] = parsed["count"]
        ev["rpc_smoke_results_len"] = parsed["results_len"]
    else:
        ev["rpc_smoke_count"] = -1
        ev["rpc_smoke_results_len"] = -1
        ev["rpc_smoke_error"] = redact(_tail(r2.stderr))

    # Representative arm EXPLAIN index use (read-only).
    ev["explain"] = {}
    arms = {
        "message_ident": (
            "explain select m.message_id::text from public.discord_messages m "
            "where m.is_deleted=false and char_length(m.content) between 1 and 8000 "
            "and public.hivemind_normalize_identifier(m.content) like '%wanvideosampler%' limit 500"
        ),
        "message_fts": (
            "explain select message_id::text from public.discord_messages "
            "where to_tsvector('simple'::regconfig,coalesce(content,'')) "
            "@@ websearch_to_tsquery('simple'::regconfig,'sampler video') and is_deleted=false limit 500"
        ),
    }
    for name, sql in arms.items():
        r3 = psql(env, elevate(sql), timeout=60, on_error_stop=False)
        plan = r3.stdout
        ev["explain"][name] = {
            "rc": r3.returncode,
            "uses_index": ("index scan" in plan.lower() or "bitmap" in plan.lower()),
            "plan_tail": redact(_tail(plan)),
        }
    return ev


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Live driver for the lexical candidate SQL + RPC.")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--rollback", action="store_true")
    mode.add_argument("--evidence", action="store_true")
    args = p.parse_args(argv)

    env, host, port = derive_pg_env()

    if args.preflight:
        rep = run_preflight(env, host, port)
        rep["rehearsal_gate"] = rehearsal_gate()
        print(json.dumps(rep, indent=2, default=str))
        return 0 if rep.get("green") else 1

    if args.apply:
        rg = rehearsal_gate()
        if not rg.get("green"):
            print(json.dumps({"applied": False, "reason": "rehearsal gate not green", "rehearsal": rg}))
            return 1
        pf = run_preflight(env, host, port)
        if not pf.get("green"):
            print(json.dumps({"applied": False, "reason": "preflight not green", "preflight": pf}))
            return 1
        res = apply_migrations(env)
        print(json.dumps(res, indent=2, default=str))
        return 0 if res["all_ok"] else 1

    if args.rollback:
        print(json.dumps(rollback(env), indent=2, default=str))
        return 0

    if args.evidence:
        ev = capture_evidence(env)
        EVIDENCE_OUT.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE_OUT.write_text(json.dumps(ev, indent=2, default=str) + "\n", encoding="utf-8")
        print(json.dumps(ev, indent=2, default=str))
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
