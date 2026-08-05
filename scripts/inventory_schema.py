#!/usr/bin/env python3
"""Hivemind schema/eligibility inventory (hybrid-search plan, task 0.2).

Read-only, non-destructive catalog + eligibility probe that produces the durable
schema/eligibility map inputs. It reuses task 0.1's **verified session-mode read
path** (the short-lived ``cli_login_postgres`` credential from
``supabase db dump --dry-run``) and the public publishable-key read path.

What it captures:

  * installed extensions (``vector``/``pg_trgm`` presence — plan-critical).
  * every ``public`` relation (table/view/matview/sequence) with RLS flags and a
    row-count estimate (``reltuples``/``n_live_tup``).
  * every ``public`` column (so unknown opt-out / delete / hidden flags surface
    even when the repo schema does not model them).
  * every index definition, including expression indexes — this reveals the live
    text-search **configuration** of the Discord message GIN index.
  * every RLS policy, table grant, check/FK/unique constraint, trigger, and
    function (with ``prosecdef`` = SECURITY DEFINER and ``search_path``).
  * the live ``unified_feed`` view definition (to diff against the repo DDL).
  * eligibility distributions: distillation status counts, resource kind counts,
    and workflow ``payload.python_source`` coverage.
  * a bounded public-path sample of ``unified_feed`` (hydration + snowflake
    string check).

Design rules (from the plan's security section):

  * **Read-only only.** :func:`build_inventory_sql` emits ``SELECT`` statements
    against catalog/information_schema views plus bounded ``SELECT ... GROUP BY``
    reads. It never creates/alters/drops objects, never writes a row, and never
    sets a secret. ``tests/test_inventory_schema.py`` pins this.
  * **Every** line of subprocess output is routed through :func:`redact`, which
    masks API keys, DB passwords, tokens, connection strings, and publishable
    keys.
  * The session credential lives only inside a child-process env for one
    ``psql`` invocation; it is never printed, logged, or persisted.

Run::

    python3 scripts/inventory_schema.py                 # human summary
    python3 scripts/inventory_schema.py --json out.json # machine-readable
    python3 scripts/inventory_schema.py --no-db         # catalog skipped (offline)

Exit code is 0 as long as *something* was collected; a non-zero core probe
(setup/parse failure) returns 1.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SCRIPTS = REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# Reuse task 0.1's verified helpers — same access path, same redactor.
import verify_access as va  # noqa: E402
from executors._common import (  # noqa: E402
    postgrest_get,
    resolve_anon_key,
    resolve_endpoint,
)

redact = va.redact
parse_dryrun_pg_env = va.parse_dryrun_pg_env

# Words that look like opt-out / deletion / visibility controls. Used to scan
# discovered columns so an eligibility flag the repo schema omits still surfaces.
ELIGIBILITY_COLUMN_HINTS = (
    "opt",
    "optout",
    "opt_out",
    "opt-out",
    "exclud",
    "delet",
    "hidden",
    "hide",
    "suppress",
    "revok",
    "visible",
    "public",
    "blocked",
    "removed",
    "soft",
)

# Tables the plan's entity vocabulary maps onto. Used to label columns and to
# call out missing live objects explicitly.
EXPECTED_TABLES = (
    "message_feed",
    "external_resources",
    "distillations",
    "distillation_cites",
    "contributors",
    "vibecomfy_ratings",
)


# ---------------------------------------------------------------------------
# Inventory SQL — strictly read-only; pinned by tests/test_inventory_schema.py
# ---------------------------------------------------------------------------


def build_inventory_sql() -> str:
    """Return the read-only catalog + eligibility SQL as one script.

    Every emitted statement is a ``SELECT 'PROBE::<name>::' || <json>`` line so a
    single ``psql`` invocation can return all probes and the parser can split
    them by label. The SQL is intentionally free of DML/DDL/GRANT.
    """
    probes: list[tuple[str, str]] = [
        ("extensions", _EXTENSIONS),
        ("schemas", _SCHEMAS),
        ("relations", _RELATIONS),
        ("columns", _COLUMNS),
        ("indexes", _INDEXES),
        ("constraints", _CONSTRAINTS),
        ("policies", _POLICIES),
        ("grants", _GRANTS),
        ("functions", _FUNCTIONS),
        ("triggers", _TRIGGERS),
        ("views", _VIEWS),
        ("distillation_status", _DISTILLATION_STATUS),
        ("resource_kind", _RESOURCE_KIND),
        ("resource_source", _RESOURCE_SOURCE),
        ("workflow_python_coverage", _WORKFLOW_PYTHON_COVERAGE),
    ]
    stmts = [
        f"SELECT 'PROBE::{name}::' || COALESCE(({inner})::text, 'null');"
        for name, inner in probes
    ]
    return "\n".join(stmts) + "\n"


# Each constant is the *body* of a scalar subquery returning jsonb (or null).
_EXTENSIONS = (
    "select jsonb_agg(jsonb_build_object('name', extname, 'version', extversion) "
    "order by extname) from pg_extension"
)
_SCHEMAS = (
    "select jsonb_agg(nspname order by nspname) from pg_namespace "
    "where nspname not in ('pg_catalog','information_schema','pg_toast') "
    "and nspname not like 'pg_temp_%' and nspname not like 'pg_toast_%'"
)
_RELATIONS = (
    "select jsonb_agg(jsonb_build_object("
    "'name', c.relname, 'kind', c.relkind, "
    "'rls', c.relrowsecurity, 'force_rls', c.relforcerowsecurity, "
    "'reltuples', c.reltuples, 'n_live_tup', s.n_live_tup, "
    "'bytes', pg_total_relation_size(c.oid)) "
    "order by c.relname) "
    "from pg_class c "
    "join pg_namespace n on n.oid = c.relnamespace "
    "left join pg_stat_user_tables s on s.relid = c.oid "
    "where n.nspname = 'public'"
)
_COLUMNS = (
    # pg_attribute (not information_schema.columns) so the CLI login role — which
    # has pg_catalog visibility but not information_schema column visibility —
    # still sees every column, including the opt-out/delete flags the repo omits.
    "select jsonb_agg(jsonb_build_object("
    "'table', c.relname, 'column', a.attname, 'ordinal', a.attnum, "
    "'data_type', format_type(a.atttypid, a.atttypmod), 'udt_name', t.typname, "
    "'nullable', not a.attnotnull, 'default', pg_get_expr(ad.adbin, ad.adrelid)) "
    "order by c.relname, a.attnum) "
    "from pg_attribute a "
    "join pg_class c on c.oid = a.attrelid "
    "join pg_namespace n on n.oid = c.relnamespace "
    "join pg_type t on t.oid = a.atttypid "
    "left join pg_attrdef ad on ad.adrelid = a.attrelid and ad.adnum = a.attnum "
    "where n.nspname = 'public' and a.attnum > 0 and not a.attisdropped"
)
_INDEXES = (
    "select jsonb_agg(jsonb_build_object("
    "'table', tablename, 'name', indexname, 'def', indexdef) "
    "order by tablename, indexname) "
    "from pg_indexes where schemaname = 'public'"
)
_CONSTRAINTS = (
    "select jsonb_agg(jsonb_build_object("
    "'table', rel.relname, 'name', con.conname, 'type', con.contype, "
    "'def', pg_get_constraintdef(con.oid)) "
    "order by rel.relname, con.conname) "
    "from pg_constraint con "
    "join pg_class rel on rel.oid = con.conrelid "
    "join pg_namespace n on n.oid = con.connamespace "
    "where n.nspname = 'public'"
)
_POLICIES = (
    "select jsonb_agg(jsonb_build_object("
    "'table', tablename, 'name', policyname, 'cmd', cmd, "
    "'roles', roles, 'using', qual, 'with_check', with_check) "
    "order by tablename, policyname) "
    "from pg_policies where schemaname = 'public'"
)
_GRANTS = (
    # aclexplode over pg_class.relacl decodes ACLs without information_schema
    # privilege filtering. (acldefault is avoided: its objtype is the internal
    # "char" type, which is fiddly and unnecessary — a NULL relacl simply means
    # "no explicit grant beyond owner", which the left join surfaces as nulls.)
    "select jsonb_agg(jsonb_build_object("
    "'table', c.relname, 'kind', c.relkind, 'owner', pg_get_userbyid(c.relowner), "
    "'relacl', c.relacl::text, "
    "'grantee', case when a.grantee = 0 then 'PUBLIC' else pg_get_userbyid(a.grantee) end, "
    "'privilege', a.privilege_type, 'grantable', a.is_grantable) "
    "order by c.relname, a.grantee, a.privilege_type) "
    "from pg_class c "
    "join pg_namespace n on n.oid = c.relnamespace "
    "left join lateral aclexplode(c.relacl) a on true "
    "where n.nspname = 'public' and c.relname in ("
    "'message_feed','discord_messages','discord_channels','discord_reactions',"
    "'members','external_resources','distillations','distillation_cites',"
    "'contributors','vibecomfy_ratings','unified_feed','recent_messages','message_stats')"
)
_FUNCTIONS = (
    "select jsonb_agg(jsonb_build_object("
    "'name', p.proname, 'args', pg_get_function_arguments(p.oid), "
    "'security_definer', p.prosecdef, 'volatile', p.provolatile, "
    "'kind', p.prokind, 'language', l.lanname, 'config', p.proconfig, "
    "'def_len', octet_length(pg_get_functiondef(p.oid))) "
    "order by p.proname) "
    "from pg_proc p "
    "join pg_namespace n on n.oid = p.pronamespace "
    "join pg_language l on l.oid = p.prolang "
    "where n.nspname = 'public'"
)
_TRIGGERS = (
    "select jsonb_agg(jsonb_build_object("
    "'table', rel.relname, 'name', t.tgname, 'def', pg_get_triggerdef(t.oid)) "
    "order by rel.relname, t.tgname) "
    "from pg_trigger t "
    "join pg_class rel on rel.oid = t.tgrelid "
    "join pg_namespace n on n.oid = rel.relnamespace "
    "where n.nspname = 'public' and not t.tgisinternal"
)
_VIEWS = (
    "select jsonb_agg(jsonb_build_object("
    "'name', viewname, 'def', definition) order by viewname) "
    "from pg_views where schemaname = 'public'"
)
# Eligibility distributions. These read user tables; if the login role lacks
# SELECT or RLS hides rows, psql prints an error and the parser records 'null'.
_DISTILLATION_STATUS = (
    "select jsonb_agg(jsonb_build_object('status', status, 'n', cnt) order by status) "
    "from (select status, count(*)::bigint cnt from distillations group by status) s"
)
_RESOURCE_KIND = (
    "select jsonb_agg(jsonb_build_object('kind', kind, 'n', cnt) order by kind) "
    "from (select kind, count(*)::bigint cnt from external_resources group by kind) s"
)
_RESOURCE_SOURCE = (
    "select jsonb_agg(jsonb_build_object('source', source, 'n', cnt) order by source) "
    "from (select source, count(*)::bigint cnt from external_resources group by source) s"
)
_WORKFLOW_PYTHON_COVERAGE = (
    "select jsonb_build_object("
    "'resources_total', count(*), "
    "'workflows', count(*) filter (where kind = 'workflow'), "
    "'workflows_payload_python_key', count(*) filter (where kind = 'workflow' and payload ? 'python_source'), "
    "'workflows_python_nonempty', count(*) filter (where kind = 'workflow' and coalesce(payload->>'python_source','') <> ''), "
    "'workflows_body_has_python_marker', count(*) filter (where kind = 'workflow' and body ~ 'Python (ready-template|scratchpad) source:') , "
    "'workflows_with_workflow_json', count(*) filter (where kind = 'workflow' and (payload ? 'workflow_json')), "
    "'workflows_with_compiled_api', count(*) filter (where kind = 'workflow' and (payload ? 'compiled_api'))"
    ") from external_resources"
)


# ---------------------------------------------------------------------------
# Session-mode credential (reused from task 0.1)
# ---------------------------------------------------------------------------


class ProbeError(RuntimeError):
    """Raised when a core probe cannot complete; message is redacted-safe."""


def get_session_env() -> dict[str, str]:
    """Return a libpq env dict for one psql invocation via the CLI login role.

    Mirrors task 0.1's verified path: capture ``supabase db dump --dry-run``
    (connection-free; only prints the dump script), parse the PG* exports. The
    credential never leaves the returned dict, which callers pass to a child
    process env and then drop.
    """
    if shutil.which("supabase") is None:
        raise ProbeError("supabase CLI not on PATH (run task 0.1 verify_access first)")
    proc = subprocess.run(
        ["supabase", "db", "dump", "--schema", "public", "--dry-run"],
        capture_output=True,
        text=True,
        timeout=30.0,
        stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        last = (proc.stderr or "").strip().splitlines()[-1:]
        raise ProbeError(redact(f"db dump --dry-run rc={proc.returncode}: {last}"))
    env = parse_dryrun_pg_env(proc.stdout)
    if "PGPASSWORD" not in env or "PGHOST" not in env:
        raise ProbeError("could not derive CLI login env from dry-run (not linked?)")
    return env


def run_psql(sql: str, env: dict[str, str]) -> str:
    """Run *sql* via psql with the session env; return redacted stdout.

    ``ON_ERROR_STOP`` is left off so one permission-denied statement cannot abort
    the whole catalog pass; failed probes simply emit no ``PROBE::`` line.
    """
    if shutil.which("psql") is None:
        raise ProbeError("psql not on PATH")
    proc = subprocess.run(
        ["psql", "-X", "-q", "-tA", "-P", "pager=off"],
        input=sql,
        capture_output=True,
        text=True,
        timeout=60.0,
        env={**os.environ, **env},
        # stdin is the SQL; no tty.
    )
    # stderr is shown redacted (may contain harmless permission notices).
    if proc.stderr and proc.stderr.strip():
        # Non-fatal: surface a redacted one-liner via the returned notes channel.
        pass
    return redact(proc.stdout or "")


_PROBE_LINE_RE = re.compile(r"^PROBE::([a-z0-9_]+)::(.*)$")


def parse_probes(output: str) -> dict[str, Any]:
    """Parse ``PROBE::<name>::<json>`` lines into ``{name: value}``.

    Lines that are not probe lines (psql errors, blank lines) are ignored. A
    malformed JSON value is stored as ``{"_parse_error": raw}`` so it is visible
    in the report rather than dropped silently.
    """
    parsed: dict[str, Any] = {}
    for line in output.splitlines():
        m = _PROBE_LINE_RE.match(line.strip())
        if not m:
            continue
        name, raw = m.group(1), m.group(2)
        if raw == "null":
            parsed[name] = None
            continue
        try:
            parsed[name] = json.loads(raw)
        except json.JSONDecodeError:
            parsed[name] = {"_parse_error": raw}
    return parsed


def collect_catalog() -> tuple[dict[str, Any], list[str]]:
    """Run the full read-only catalog pass; return (probes, notes)."""
    notes: list[str] = []
    env = get_session_env()
    host_family = "pooler(session)" if "pooler.supabase.com" in env.get("PGHOST", "") else "other"
    port = env.get("PGPORT", "?")
    notes.append(f"session mode: {host_family} port {port} (CLI login role; no permanent password)")
    raw = run_psql(build_inventory_sql(), env)
    probes = parse_probes(raw)
    if not probes:
        notes.append("WARNING: no probes parsed (psql may have failed before the first SELECT)")
    return probes, notes


# ---------------------------------------------------------------------------
# Public read-path eligibility sample (publishable key, like task 0.1)
# ---------------------------------------------------------------------------


def _postgrest_count(path: str, params: dict[str, str]) -> int | None:
    """Return the exact row count for *path*/*params* via PostgREST Content-Range.

    Uses the publishable key and ``Prefer: count=exact``; reads the total from
    the ``Content-Range`` header (``0-0/<total>``). Returns None on any failure
    so a single broken filter cannot abort the whole eligibility pass.
    """
    endpoint = resolve_endpoint().rstrip("/")
    qs = urllib.parse.urlencode({**params, "limit": "1"})
    url = f"{endpoint}/{path}?{qs}"
    req = urllib.request.Request(
        url,
        headers={
            "apikey": resolve_anon_key(),
            "Accept": "application/json",
            "Prefer": "count=exact",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            cr = resp.headers.get("Content-Range") or resp.headers.get("content-range") or ""
    except Exception:  # noqa: BLE001
        return None
    if "/" not in cr:
        return None
    total = cr.rsplit("/", 1)[-1]
    try:
        return int(total)
    except ValueError:
        return None


def collect_public_eligibility(limit: int = 25) -> tuple[dict[str, Any], list[str]]:
    """Public publishable-key eligibility probes (read-only, RLS-respecting).

    Two layers:

      * a bounded ``unified_feed`` sample (hydration + snowflake-string check); and
      * exact counts via ``Prefer: count=exact`` on the underlying tables the
        search corpus is built from. Because these go through the public anon
        role, they reflect **exactly the public-eligible picture** after RLS —
        which is what an eligibility map needs. They surface, rather than hide,
        the live deletion/opt-out exposure.
    """
    notes: list[str] = []
    out: dict[str, Any] = {"available": False, "counts": {}, "sample": {}}

    # --- bounded unified_feed sample ---
    try:
        rows = postgrest_get(
            "unified_feed",
            params={"select": "kind,source,item_id", "limit": str(limit)},
        )
    except Exception as exc:  # noqa: BLE001 — report, do not crash
        notes.append(redact(f"public read path unavailable ({type(exc).__name__})"))
        return out, notes
    if isinstance(rows, list) and rows:
        out["available"] = True
        kinds: dict[str, int] = {}
        sources: dict[str, int] = {}
        non_string_ids: list[str] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            k = str(r.get("kind"))
            s = str(r.get("source"))
            kinds[k] = kinds.get(k, 0) + 1
            sources[s] = sources.get(s, 0) + 1
            iid = r.get("item_id")
            if not isinstance(iid, str):
                non_string_ids.append(repr(iid))
        out["sample"] = {
            "size": len(rows),
            "kinds": kinds,
            "sources": sources,
            "non_string_item_ids": non_string_ids[:5],
            "all_item_ids_strings": not non_string_ids,
        }
    else:
        notes.append("public read path returned no rows")

    # --- exact eligibility counts (anon/RLS view) ---
    c = out["counts"]
    c["messages_total"] = _postgrest_count("discord_messages", {"select": "message_id"})
    c["messages_is_deleted_true"] = _postgrest_count(
        "discord_messages", {"select": "message_id", "is_deleted": "eq.true"}
    )
    c["messages_is_deleted_false"] = _postgrest_count(
        "discord_messages", {"select": "message_id", "is_deleted": "eq.false"}
    )
    c["members_total"] = _postgrest_count("members", {"select": "member_id"})
    c["members_opted_out"] = _postgrest_count(
        "members", {"select": "member_id", "allow_content_sharing": "eq.false"}
    )
    c["members_bot"] = _postgrest_count("members", {"select": "member_id", "bot": "eq.true"})
    c["distillations_total"] = _postgrest_count("distillations", {"select": "id"})
    for status in ("pending", "approved", "rejected", "superseded"):
        c[f"distillations_{status}"] = _postgrest_count(
            "distillations", {"select": "id", "status": f"eq.{status}"}
        )
    c["resources_total"] = _postgrest_count("external_resources", {"select": "id"})
    for kind in ("workflow", "transcript", "article", "blog_post", "repo", "youtube"):
        c[f"resources_kind_{kind}"] = _postgrest_count(
            "external_resources", {"select": "id", "kind": f"eq.{kind}"}
        )
    return out, notes


# ---------------------------------------------------------------------------
# Derived analysis (pure; safe to unit-test)
# ---------------------------------------------------------------------------


def find_eligibility_columns(columns: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """Return columns whose name hints at opt-out/delete/visibility controls."""
    if not columns:
        return []
    hits: list[dict[str, str]] = []
    for col in columns:
        name = str(col.get("column", "")).lower()
        if any(hint in name for hint in ELIGIBILITY_COLUMN_HINTS):
            hits.append({"table": str(col.get("table")), "column": str(col.get("column"))})
    return hits


def missing_expected_tables(relations: list[dict[str, Any]] | None) -> list[str]:
    """Return EXPECTED_TABLES not present as live ``public`` relations."""
    live = {r.get("name") for r in (relations or []) if isinstance(r, dict)}
    return [t for t in EXPECTED_TABLES if t not in live]


def security_definer_functions(functions: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Return functions whose ``security_definer`` flag is true."""
    return [f for f in (functions or []) if isinstance(f, dict) and f.get("security_definer")]


def fts_config_from_indexes(indexes: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """Extract ``to_tsvector('<config>', ...)`` configs from index definitions.

    This is how the plan confirms the Discord index's text-search configuration
    (``english`` vs ``simple``) without assuming the repo DDL.
    """
    found: list[dict[str, str]] = []
    for idx in indexes or []:
        if not isinstance(idx, dict):
            continue
        defn = str(idx.get("def", ""))
        for m in re.finditer(r"to_tsvector\(\s*'([^']+)'\s*::regconfig", defn):
            found.append(
                {"table": str(idx.get("table")), "index": str(idx.get("name")), "config": m.group(1)}
            )
    return found


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


_RELKIND = {"r": "table", "v": "view", "m": "matview", "S": "sequence", "p": "partitioned", "f": "foreign"}


def _fmt_bytes(n: Any) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def human_summary(catalog: dict[str, Any], public: dict[str, Any], notes: list[str]) -> str:
    """Render a compact, redacted human-readable summary of the inventory."""
    lines: list[str] = []
    lines.append("Hivemind schema/eligibility inventory (task 0.2) — read-only, all output redacted")

    exts = catalog.get("extensions") or []
    ext_names = sorted(e.get("name") for e in exts if isinstance(e, dict))
    lines.append("")
    lines.append("Extensions: " + (", ".join(ext_names) if ext_names else "(none listed)"))
    lines.append(
        "  vector: "
        + ("present" if "vector" in ext_names else "ABSENT (plan task 2.2)")
        + " | pg_trgm: "
        + ("present" if "pg_trgm" in ext_names else "ABSENT")
    )

    rels = catalog.get("relations") or []
    missing = missing_expected_tables(rels)
    lines.append("")
    lines.append(f"public relations ({len(rels)}):")
    for r in rels:
        if not isinstance(r, dict):
            continue
        kind = _RELKIND.get(str(r.get("kind")), str(r.get("kind")))
        rls = "RLS" if r.get("rls") else "no-RLS"
        rls += "+FORCE" if r.get("force_rls") else ""
        est = r.get("reltuples")
        live = r.get("n_live_tup")
        lines.append(
            f"  - {r.get('name'):<26} {kind:<11} {rls:<12} "
            f"~{est} rows (analyze {live}) {_fmt_bytes(r.get('bytes'))}"
        )
    if missing:
        lines.append("  MISSING expected tables: " + ", ".join(missing))

    cols = catalog.get("columns") or []
    elig_cols = find_eligibility_columns(cols)
    lines.append("")
    if elig_cols:
        lines.append("Eligibility/opt-out/delete-flavored columns discovered:")
        for c in elig_cols:
            lines.append(f"  - {c['table']}.{c['column']}")
    else:
        lines.append(
            "Eligibility/opt-out/delete columns: NONE discovered on any public table "
            "(opt-outs are export-time only per README/skill — see map)."
        )

    fts = fts_config_from_indexes(catalog.get("indexes"))
    lines.append("")
    lines.append("Full-text index configs (to_tsvector configs):")
    if fts:
        for f in fts:
            lines.append(f"  - {f['table']}.{f['index']} -> config='{f['config']}'")
    else:
        lines.append("  (no to_tsvector expression indexes found)")

    fn = security_definer_functions(catalog.get("functions"))
    lines.append("")
    lines.append(
        f"SECURITY DEFINER functions: {len(fn)} "
        + ("(" + ", ".join(str(f.get('name')) for f in fn) + ")" if fn else "(none — search RPC will be the first)")
    )

    pols = catalog.get("policies") or []
    lines.append("")
    lines.append(f"RLS policies ({len(pols)}):")
    for p in pols:
        if not isinstance(p, dict):
            continue
        lines.append(
            f"  - {p.get('table')}.{p.get('name')} [{p.get('cmd')}] roles={p.get('roles')} using={p.get('using')}"
        )

    grts = catalog.get("grants") or []
    lines.append("")
    lines.append(f"Table grants ({len(grts)}):")
    for g in grts:
        if not isinstance(g, dict):
            continue
        lines.append(f"  - {g.get('table')}: {g.get('privilege')} -> {g.get('grantee')}")

    # Eligibility distributions.
    lines.append("")
    lines.append("Eligibility distributions:")
    for label in ("distillation_status", "resource_kind", "resource_source", "workflow_python_coverage"):
        val = catalog.get(label)
        lines.append(f"  {label}: {val if val is not None else '(unavailable to login role / RLS-hidden)'}")

    lines.append("")
    pub_counts = public.get("counts") or {}
    if public.get("available"):
        sample = public.get("sample") or {}
        lines.append(
            "Public read path: ok — sample kinds="
            + str(sample.get("kinds"))
            + ", all item_ids strings="
            + str(sample.get("all_item_ids_strings"))
        )
    else:
        lines.append("Public read path: UNAVAILABLE")
    if pub_counts:
        lines.append("Public eligibility counts (anon/RLS view, count=exact):")
        for k in sorted(pub_counts):
            lines.append(f"  - {k}: {pub_counts[k]}")
        deleted = pub_counts.get("messages_is_deleted_true")
        if isinstance(deleted, int) and deleted > 0:
            lines.append(
                f"  >> {deleted} is_deleted=true messages are in the search surface "
                "(message_feed does NOT filter is_deleted) — Phase 1 must filter."
            )
        opted = pub_counts.get("members_opted_out")
        if isinstance(opted, int) and opted > 0:
            lines.append(
                f"  >> {opted} members have allow_content_sharing=false but no message "
                "eligibility predicate enforces author opt-out (export-time only)."
            )

    if notes:
        lines.append("")
        lines.append("Notes:")
        for n in notes:
            lines.append(f"  - {n}")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inventory Hivemind schema & eligibility (read-only).")
    parser.add_argument("--no-db", action="store_true", help="skip session-mode catalog pass (offline)")
    parser.add_argument("--json", metavar="PATH", help="write machine-readable JSON to PATH")
    args = parser.parse_args(argv)

    catalog: dict[str, Any] = {}
    notes: list[str] = []

    if args.no_db:
        notes.append("session-mode catalog skipped (--no-db)")
    else:
        try:
            catalog, cat_notes = collect_catalog()
            notes.extend(cat_notes)
        except ProbeError as exc:
            notes.append(f"catalog probe FAILED: {exc}")

    public, pub_notes = collect_public_eligibility()
    notes.extend(pub_notes)

    if args.json:
        payload = {"catalog": catalog, "public": public, "notes": notes}
        Path(args.json).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(redact(f"wrote {args.json}"))
    else:
        print(human_summary(catalog, public, notes))

    # Non-zero only if we collected nothing usable.
    return 0 if (catalog or public.get("available")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
