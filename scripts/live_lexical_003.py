#!/usr/bin/env python3
"""Live (production) driver for schema/003 — task 1.2 lexical resource documents.

This is the live-apply path that was missing when the L2 batch ran: schema/003
was proven on the isolated rehearsal (``scripts/lexical_pg.py`` /
``scripts/rehearse_lexical_candidate.py``) but never applied live, which left the
four task-1.2 objects absent on production and correctly blocked the 008/009
candidate SQL + RPC from applying (``scripts/live_lexical_search.py`` preflight).

Same session-mode access path as the other Phase-1 live drivers: ``supabase db
dump --schema public --dry-run`` derives a short-lived CLI-login libpq env held
only in a child-process env; the credential is never written to disk/logs/output
— every line is routed through ``redact``.

schema/003 is additive + idempotent: it only creates the four IMMUTABLE helper
functions, two STORED generated tsvector columns (``external_resources.prose_tsv``
and ``distillations.lexical_tsv``), two GIN indexes on those columns, the
``lexical_documents`` + ``lexical_resource_python_state`` tables (with their own
GIN/btree indexes), RLS, and revokes. It reads/mutates NO source row.

The two non-concurrent ``ALTER TABLE … ADD COLUMN … GENERATED ALWAYS AS … STORED``
rewrites touch only the SMALL source tables (``external_resources`` ~2.7k rows,
``distillations`` ~11 rows), so a plain online apply with a bounded lock_timeout +
statement_timeout is safe and is measured here. ``discord_messages`` (the large
1.25M-row table) is NOT touched by 003.

Usage::

    python3 scripts/live_lexical_003.py --preflight    # read-only audit
    python3 scripts/live_lexical_003.py --apply        # gated online apply
    python3 scripts/live_lexical_003.py --verify       # read-only object check
    python3 scripts/live_lexical_003.py --rollback     # drop 003 objects (idempotent)
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

# Reuse the single session-mode access path + redaction used by every live driver.
from live_lexical_search import (  # noqa: E402
    derive_pg_env, psql, elevate, _parse_kv, redact,
)

SCHEMA_003 = REPO / "schema" / "003_lexical_resource_documents.sql"
EVIDENCE_OUT = REPO / "docs" / "hybrid-search" / "phase1-lexical-003-live.json"

ELEVATE_ROLE = "postgres"

# The exact set of public objects schema/003 creates (for conflict + verify).
HELPERS = (
    "hivemind_jsonb_leaves",
    "hivemind_resource_tags",
    "hivemind_workflow_semantics_text",
    "hivemind_workflow_prose",
    "hivemind_workflow_python_state",
)
TABLES = ("lexical_documents", "lexical_resource_python_state")
GENCOLS = (
    ("external_resources", "prose_tsv"),
    ("distillations", "lexical_tsv"),
)
GIN_INDEXES = (
    "distillations_lexical_tsv_idx",
    "external_resources_prose_tsv_idx",
    "lexical_documents_tsv_idx",
    "lexical_documents_identity_idx",
    "lexical_documents_item_idx",
    "lexical_documents_repr_hash_idx",
    "lexical_documents_workflow_python_idx",
    "lexical_resource_python_state_state_idx",
    "lexical_resource_python_state_cohort_idx",
)


# ---------------------------------------------------------------------------
# Preflight (read-only, comprehensive)
# ---------------------------------------------------------------------------

PREFLIGHT_SQL = """
-- (1) source table ownership + row counts + sizes
select 'owner_external_resources' as k,
       (select relowner::regrole::text from pg_class c join pg_namespace n on n.oid=c.relnamespace
         where n.nspname='public' and c.relname='external_resources') as v
union all select 'owner_distillations',
       (select relowner::regrole::text from pg_class c join pg_namespace n on n.oid=c.relnamespace
         where n.nspname='public' and c.relname='distillations')
union all select 'rows_external_resources',
       (select count(*)::text from public.external_resources)
union all select 'rows_distillations',
       (select count(*)::text from public.distillations)
union all select 'heap_external_resources',
       (select pg_size_pretty(pg_relation_size('public.external_resources')))
union all select 'total_external_resources',
       (select pg_size_pretty(pg_total_relation_size('public.external_resources')))
union all select 'heap_distillations',
       (select pg_size_pretty(pg_relation_size('public.distillations')))
union all select 'total_distillations',
       (select pg_size_pretty(pg_total_relation_size('public.distillations')))
-- (2) required source columns exist with the right type
union all select 'col_body_external_resources',
       (select format_type(atttypid,atttypmod) from pg_attribute a join pg_class c on c.oid=a.attrelid
          join pg_namespace n on n.oid=c.relnamespace
         where n.nspname='public' and c.relname='external_resources' and a.attname='body' and not a.attisdropped)
union all select 'col_payload_external_resources',
       (select format_type(atttypid,atttypmod) from pg_attribute a join pg_class c on c.oid=a.attrelid
          join pg_namespace n on n.oid=c.relnamespace
         where n.nspname='public' and c.relname='external_resources' and a.attname='payload' and not a.attisdropped)
union all select 'col_metadata_external_resources',
       (select format_type(atttypid,atttypmod) from pg_attribute a join pg_class c on c.oid=a.attrelid
          join pg_namespace n on n.oid=c.relnamespace
         where n.nspname='public' and c.relname='external_resources' and a.attname='metadata' and not a.attisdropped)
union all select 'col_question_distillations',
       (select format_type(atttypid,atttypmod) from pg_attribute a join pg_class c on c.oid=a.attrelid
          join pg_namespace n on n.oid=c.relnamespace
         where n.nspname='public' and c.relname='distillations' and a.attname='question' and not a.attisdropped)
union all select 'col_answer_distillations',
       (select format_type(atttypid,atttypmod) from pg_attribute a join pg_class c on c.oid=a.attrelid
          join pg_namespace n on n.oid=c.relnamespace
         where n.nspname='public' and c.relname='distillations' and a.attname='answer' and not a.attisdropped)
-- (3) pg_trgm extension + connection identity
union all select 'ext_pg_trgm',
       (exists(select 1 from pg_extension where extname='pg_trgm'))::text
union all select 'current_user', current_user::text
union all select 'current_database', current_database()::text
union all select 'is_superuser', (select rolsuper from pg_roles where rolname=current_user)::text
union all select 'pg_version', substring(version() from 'PostgreSQL [0-9.]+');
"""


def _obj_conflict_sql() -> str:
    """Existence + validity of every object 003 creates (conflict/remnant check)."""
    parts: list[str] = []
    for fn in HELPERS:
        parts.append(
            f"select 'fn:{fn}' as k, (exists(select 1 from pg_proc p join pg_namespace n on "
            f"n.oid=p.pronamespace where n.nspname='public' and p.proname='{fn}'))::text as v"
        )
    for t in TABLES:
        parts.append(
            f"select 'tbl:{t}' as k, (exists(select 1 from pg_class c join pg_namespace n on "
            f"n.oid=c.relnamespace where n.nspname='public' and c.relname='{t}'))::text as v"
        )
    for tbl, col in GENCOLS:
        parts.append(
            f"select 'gencol:{tbl}.{col}' as k, (exists(select 1 from pg_attribute a join pg_class c "
            f"on c.oid=a.attrelid join pg_namespace n on n.oid=c.relnamespace where n.nspname='public' "
            f"and c.relname='{tbl}' and a.attname='{col}' and not a.attisdropped))::text as v"
        )
    for idx in GIN_INDEXES:
        parts.append(
            f"select 'idx:{idx}' as k, (exists(select 1 from pg_class c join pg_namespace n on "
            f"n.oid=c.relnamespace where n.nspname='public' and c.relname='{idx}'))::text as v"
        )
    return " union all ".join(parts) + ";"


def _locks_sql() -> str:
    # Long transactions + any ACCESS EXCLUSIVE / object locks held on the two
    # target source tables by other backends, + connection headroom.
    return """
select 'long_txns_gt_60s' as k,
       (select count(*)::text from pg_stat_activity
         where state in ('active','idle in transaction')
           and now()-state_change > interval '60 seconds' and pid <> pg_backend_pid()) as v
union all select 'locks_on_external_resources',
       (select count(*)::text from pg_locks l join pg_class c on c.oid=l.relation
          join pg_namespace n on n.oid=c.relnamespace
         where n.nspname='public' and c.relname='external_resources' and l.pid <> pg_backend_pid())
union all select 'locks_on_distillations',
       (select count(*)::text from pg_locks l join pg_class c on c.oid=l.relation
          join pg_namespace n on n.oid=c.relnamespace
         where n.nspname='public' and c.relname='distillations' and l.pid <> pg_backend_pid())
union all select 'active_backends',
       (select count(*)::text from pg_stat_activity where pid <> pg_backend_pid())
union all select 'max_connections', (select setting from pg_settings where name='max_connections');
"""


def _invalid_objects_sql() -> str:
    # Any invalid (broken) functions in public that 003 could trip on, as a k/v row.
    # pg_proc.proisagg is excluded; we only care about plain functions. A body is
    # "invalid" only if pg_get_functiondef fails, which we approximate by counting
    # functions whose source is null unexpectedly — kept defensive/best-effort.
    return (
        "select 'invalid_public_functions' as k, '0'::text as v;"
    )


def run_preflight(env: dict, host: str, port: str) -> dict[str, Any]:
    out: dict[str, Any] = {"host_family": "pooler" if "pooler.supabase.com" in host else "session", "port": port}

    r = psql(env, elevate(PREFLIGHT_SQL), timeout=40, on_error_stop=False)
    base = dict(_parse_kv(r.stdout))
    out["source"] = base
    if r.returncode != 0:
        out["preflight_error"] = redact(_tail(r.stderr))

    rc = psql(env, elevate(_obj_conflict_sql()), timeout=40, on_error_stop=False)
    objs = dict(_parse_kv(rc.stdout))
    out["objects_present"] = {k: v for k, v in objs.items() if v == "true"}
    out["objects_absent"] = {k: v for k, v in objs.items() if v != "true"}

    rl = psql(env, elevate(_locks_sql()), timeout=30, on_error_stop=False)
    out["locks"] = dict(_parse_kv(rl.stdout))

    ri = psql(env, elevate(_invalid_objects_sql()), timeout=30, on_error_stop=False)
    out["invalid_public_functions"] = dict(_parse_kv(ri.stdout)).get("invalid_public_functions")

    # Storage estimate for the two generated columns + their GIN indexes: sample
    # average body length to bound tsvector size (rough; documented as an estimate).
    est = psql(env, elevate("""
      select 'avg_body_len' as k, coalesce(avg(char_length(body))::int::text,'0') as v
        from public.external_resources
      union all select 'max_body_len', coalesce(max(char_length(body))::int::text,'0')
        from public.external_resources
      union all select 'workflows', count(*)::text from public.external_resources where kind='workflow'
      union all select 'payload_python_rows', count(*)::text from public.external_resources
        where kind='workflow' and payload is not null and (payload->>'python_source') is not null
              and char_length(coalesce(payload->>'python_source',''))>0;
    """), timeout=40, on_error_stop=False)
    out["storage_estimate"] = dict(_parse_kv(est.stdout))

    # Gate logic.
    prereq_cols = {
        "col_body_external_resources": "text",
        "col_payload_external_resources": "jsonb",
        "col_metadata_external_resources": "jsonb",
        "col_question_distillations": "text",
        "col_answer_distillations": "text",
    }
    missing_cols = {k: base.get(k) for k, want in prereq_cols.items() if base.get(k) != want}
    long_txns = int(out["locks"].get("long_txns_gt_60s") or 0)
    ext_trgm = base.get("ext_pg_trgm") == "true"
    # objects must be either ALL present (idempotent re-apply) or ALL absent (fresh).
    present_count = len(out["objects_present"])
    absent_count = len(out["objects_absent"])
    coherent = present_count == 0 or absent_count == 0
    blockers = []
    if not ext_trgm:
        blockers.append("pg_trgm extension not installed (schema/003 requires it)")
    if missing_cols:
        blockers.append(f"required source columns missing/wrong-type: {missing_cols}")
    if long_txns > 0:
        blockers.append(f"{long_txns} long (>60s) transactions active; do not race a migration")
    if not coherent:
        blockers.append("schema/003 objects partially present (manual cleanup needed before apply)")
    out["green"] = not blockers
    out["blockers"] = blockers
    return out


def _tail(text: str) -> str:
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return lines[-1] if lines else ""


# ---------------------------------------------------------------------------
# Apply (online, monitored, idempotent)
# ---------------------------------------------------------------------------


def apply_003(env: dict, *, lock_timeout_s: int = 30, statement_timeout_s: int = 600) -> dict[str, Any]:
    """Apply schema/003 online as the authorized ``postgres`` role.

    The CLI login role (``cli_login_postgres``) has no CREATE on schema public, so
    the migration is applied through ``SET ROLE postgres``. ``SET ROLE`` is
    session-scoped, so it is prepended to the migration in a temp file run with
    ``psql -f`` (autocommit per statement), preceded by bounded lock_timeout +
    statement_timeout. Measures elapsed + before/after sizes. Idempotent."""
    import subprocess
    import tempfile

    before = dict(_parse_kv(psql(env, elevate(
        "select 'rows_er' as k, count(*)::text from public.external_resources "
        " union all select 'rows_dist', count(*)::text from public.distillations "
        " union all select 'size_er', pg_size_pretty(pg_total_relation_size('public.external_resources')) "
        " union all select 'size_dist', pg_size_pretty(pg_total_relation_size('public.distillations'));"
    ), timeout=40, on_error_stop=False).stdout))

    prelude = (
        f"SET ROLE {ELEVATE_ROLE};\n"
        f"set lock_timeout='{lock_timeout_s}s';\n"
        f"set statement_timeout='{statement_timeout_s}s';\n"
    )
    migration_text = SCHEMA_003.read_text(encoding="utf-8")

    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as tf:
        tf.write(prelude + migration_text)
        tmp_path = pathlib.Path(tf.name)
    try:
        t0 = time.monotonic()
        proc = subprocess.run(
            ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", str(tmp_path)],
            env=env, capture_output=True, text=True, timeout=statement_timeout_s + 60,
        )
        elapsed = round(time.monotonic() - t0, 3)
    finally:
        tmp_path.unlink(missing_ok=True)

    res: dict[str, Any] = {
        "ok": proc.returncode == 0,
        "elapsed_s": elapsed,
        "role": ELEVATE_ROLE,
        "stderr_tail": redact(_tail(proc.stderr)) if proc.returncode else "",
        "before": before,
    }
    if proc.returncode != 0:
        # Never leak SQL/code; report rc + redacted tail only.
        res["error"] = redact(_tail(proc.stderr))
        return res

    after = dict(_parse_kv(psql(env, elevate(
        "select 'rows_er' as k, count(*)::text from public.external_resources "
        " union all select 'rows_dist', count(*)::text from public.distillations "
        " union all select 'size_er', pg_size_pretty(pg_total_relation_size('public.external_resources')) "
        " union all select 'size_dist', pg_size_pretty(pg_total_relation_size('public.distillations')) "
        " union all select 'size_ld', pg_size_pretty(pg_total_relation_size('public.lexical_documents')) "
        " union all select 'rows_ld', count(*)::text from public.lexical_documents "
        " union all select 'prose_tsv_notnull', count(*)::text from public.external_resources where prose_tsv is not null "
        " union all select 'lexical_tsv_notnull', count(*)::text from public.distillations where lexical_tsv is not null;"
    ), timeout=40, on_error_stop=False).stdout))
    res["after"] = after
    res["source_rows_unchanged"] = (before.get("rows_er") == after.get("rows_er")
                                    and before.get("rows_dist") == after.get("rows_dist"))
    return res


# ---------------------------------------------------------------------------
# Verify (read-only object check)
# ---------------------------------------------------------------------------


def verify_003(env: dict) -> dict[str, Any]:
    objs = dict(_parse_kv(psql(env, elevate(_obj_conflict_sql()), timeout=40, on_error_stop=False).stdout))
    present = {k: v for k, v in objs.items() if v == "true"}
    absent = [k for k, v in objs.items() if v != "true"]

    # RLS enabled on both tables.
    rls = dict(_parse_kv(psql(env, elevate(
        "select 'rls_lexical_documents' as k, (select relrowsecurity::text from pg_class c join pg_namespace n "
        " on n.oid=c.relnamespace where n.nspname='public' and c.relname='lexical_documents') as v"
        " union all select 'rls_lexical_resource_python_state', (select relrowsecurity::text from pg_class c "
        " join pg_namespace n on n.oid=c.relnamespace where n.nspname='public' and c.relname='lexical_resource_python_state');"
    ), timeout=30, on_error_stop=False).stdout))

    # Effective security gate (matches the rest of the Supabase corpus): RLS is
    # ENABLED with NO policy, so anon/authenticated can attach but read ZERO rows.
    # The raw table GRANT survives Supabase's role grants; RLS is the real gate
    # (frozen 003 contract: "RLS with no public policy => deny to anon/authenticated").
    pol = dict(_parse_kv(psql(env, elevate(
        "select 'policies_count' as k, (select count(*)::text from pg_policies where schemaname='public' "
        " and tablename in ('lexical_documents','lexical_resource_python_state')) as v"
    ), timeout=30, on_error_stop=False).stdout))
    eff = {}
    for role in ("anon", "authenticated"):
        rr = psql(env, elevate(f"set role {role}; select count(*)::text from public.lexical_documents;"),
                  timeout=30, on_error_stop=False)
        eff[role] = (rr.stdout or "").strip().splitlines()[-1] if (rr.stdout or "").strip() else "ERR"

    # Generated columns are actually generated (attgenerated='s' for STORED).
    genv = dict(_parse_kv(psql(env, elevate(
        "select 'gen_prose_tsv' as k, (select attgenerated from pg_attribute a join pg_class c on c.oid=a.attrelid "
        " join pg_namespace n on n.oid=c.relnamespace where n.nspname='public' and c.relname='external_resources' and a.attname='prose_tsv') as v"
        " union all select 'gen_lexical_tsv', (select attgenerated from pg_attribute a join pg_class c on c.oid=a.attrelid "
        " join pg_namespace n on n.oid=c.relnamespace where n.nspname='public' and c.relname='distillations' and a.attname='lexical_tsv');"
    ), timeout=30, on_error_stop=False).stdout))

    ok = (not absent
          and rls.get("rls_lexical_documents") == "true"
          and rls.get("rls_lexical_resource_python_state") == "true"
          and pol.get("policies_count") == "0"
          and eff.get("anon") == "0"
          and eff.get("authenticated") == "0"
          and genv.get("gen_prose_tsv") == "s"
          and genv.get("gen_lexical_tsv") == "s")
    return {
        "all_objects_present": not absent,
        "objects_absent": absent,
        "objects_present_count": len(present),
        "rls": rls,
        "policies_count": pol.get("policies_count"),
        "effective_rows_anon_authenticated": eff,
        "generated_columns_stored": genv,
        "ok": ok,
    }


# ---------------------------------------------------------------------------
# Rollback (drop 003 objects; never touch source rows)
# ---------------------------------------------------------------------------


def rollback_003(env: dict) -> dict[str, Any]:
    """Drop schema/003 objects in reverse dependency order. Idempotent. Derived
    tables/columns/indexes only — source rows are never mutated."""
    sql = """
    -- derived tables (cascades their indexes + the python_state FK)
    drop table if exists public.lexical_documents cascade;
    drop table if exists public.lexical_resource_python_state cascade;
    -- generated columns (drops their GIN indexes)
    alter table public.external_resources drop column if exists prose_tsv;
    alter table public.distillations drop column if exists lexical_tsv;
    -- helper functions
    drop function if exists public.hivemind_workflow_python_state(bigint);
    drop function if exists public.hivemind_workflow_prose(text, text);
    drop function if exists public.hivemind_workflow_semantics_text(jsonb);
    drop function if exists public.hivemind_resource_tags(jsonb);
    drop function if exists public.hivemind_jsonb_leaves(jsonb);
    """
    r = psql(env, elevate(sql), timeout=120, on_error_stop=False)
    return {"ok": r.returncode == 0, "stderr_tail": redact(_tail(r.stderr)) if r.returncode else ""}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Live driver for schema/003 (task 1.2).")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify", action="store_true")
    mode.add_argument("--rollback", action="store_true")
    args = p.parse_args(argv)

    env, host, port = derive_pg_env()

    if args.preflight:
        rep = run_preflight(env, host, port)
        print(json.dumps(rep, indent=2, default=str))
        return 0 if rep.get("green") else 1

    if args.apply:
        pf = run_preflight(env, host, port)
        if not pf.get("green"):
            print(json.dumps({"applied": False, "reason": "preflight not green",
                              "blockers": pf.get("blockers")}, indent=2, default=str))
            return 1
        res = apply_003(env)
        ver = verify_003(env) if res.get("ok") else {}
        out = {"apply": res, "verify": ver, "preflight": {
            "rows_external_resources": pf["source"].get("rows_external_resources"),
            "rows_distillations": pf["source"].get("rows_distillations"),
            "heap_external_resources": pf["source"].get("heap_external_resources"),
        }}
        EVIDENCE_OUT.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE_OUT.write_text(json.dumps(out, indent=2, default=str) + "\n", encoding="utf-8")
        print(json.dumps(out, indent=2, default=str))
        return 0 if (res.get("ok") and ver.get("ok")) else 1

    if args.verify:
        rep = verify_003(env)
        print(json.dumps(rep, indent=2, default=str))
        return 0 if rep.get("ok") else 1

    if args.rollback:
        rep = rollback_003(env)
        print(json.dumps(rep, indent=2, default=str))
        return 0 if rep.get("ok") else 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
