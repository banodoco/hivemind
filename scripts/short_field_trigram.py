"""Normalized short-field trigram indexes — build/preflight/evidence (task 1.5).

This module is the **pure, importable, stdlib-only** core for plan task 1.5:

  * the **frozen** index identities for the two bounded, high-value short-field
    normalized trigram indexes — ``external_resources.title`` and
    ``distillations.question`` — built over the frozen ``public.
    hivemind_normalize_identifier`` (IMMUTABLE, schema/005 task 1.4) with the
    ``gin_trgm_ops`` operator class. The frozen contract (lexical-contract.md §5)
    matches a query term against these by **exact normalized equality plus a
    ``gin_trgm_ops`` similarity path**; this task adds the similarity-path indexes.
  * the additive/idempotent online ``CREATE INDEX CONCURRENTLY`` statements (the
    executable form; ``schema/006_short_field_trigram.sql`` is the documented
    artifact and a unit test pins the two agree);
  * the read-only **preflight** query set + verdict logic for the gated live build
    (also detects whether the schema/005 prerequisite is live);
  * the representative ``EXPLAIN (ANALYZE, BUFFERS)`` evidence queries that prove
    the normalized trigram index is used by the ``%`` and ``<%`` operators (and
    that the planner falls back to a seq scan without it);
  * plan-parsing helpers so tests can assert index usage against captured evidence;
  * the production-shaped rehearsal schema/seed SQL + capacity math.

It connects to **no** database and calls **no** CLI by itself. The operator
drivers ``scripts/rehearse_short_field_trigram.py`` (isolated local cluster) and
``scripts/live_short_field_trigram.py`` (Hivemind project, task-0.1 session-mode
access) do the I/O. Every human-facing line those drivers emit is routed through
:func:`redact`, reused verbatim from the task-0.1 access probe so the safety
boundary never drifts.

Frozen references:
  * ``executors.identifier_normalization.normalize_identifier`` (Python) and
    ``public.hivemind_normalize_identifier`` (SQL) — the frozen compact form.
  * ``docs/hybrid-search/phase1-lexical-contract.md`` §5 (ident arm: exact
    equality + ``gin_trgm_ops`` similarity path ``<%``).
  * ``docs/hybrid-search/phase1-identifier-normalization.md`` (IMMUTABLE, ICU).

Scope boundary (this task does NOT):
  * trigram-index large message/resource bodies (explicitly out; bodies stay on
    the raw ``gin_trgm_ops`` index from schema/001 and the lexical tsvectors);
  * implement the full-message exact-identifier side index (1.6);
  * implement the multi-arm candidate SQL / RPC that consumes these indexes (1.7).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Reuse the task-0.1 redaction boundary verbatim (scripts/verify_access.py).
from verify_access import redact  # noqa: E402

# ---------------------------------------------------------------------------
# Frozen index identities (two normalized short-field trigram indexes)
# ---------------------------------------------------------------------------
#
# The frozen contract (lexical-contract.md §5) names exactly the two short
# high-value fields — resource titles and distillation questions — to receive a
# normalized ``gin_trgm_ops`` index. We index the COMPACT form
# (``hivemind_normalize_identifier``) per the task-1.5 instruction and the §5
# contract: it collapses separator/case/Unicode variants (``Wan 2.2`` /
# ``Wan2.2`` / ``wan_2.2`` → ``wan22``) so a single trigram similarity passes
# all of them. The punctuation-preserving form (``_preserve``) is the frozen
# "punctuation-aware trigram path"; it remains an option for task 1.7 but is NOT
# indexed here (the compact form gives the best cross-variant recall and the
# task explicitly names ``hivemind_normalize_identifier``).

INDEX_SCHEMA = "public"

#: Frozen overlong protection. The trigram arm short-circuits when the NORMALIZED
#: query exceeds this (a pasted 10 KB blob is neither a meaningful identifier nor
#: cheap to compare); it also bounds the indexed value. Inventory title max=92 /
#: question max=81, so 300 captures all real data with ~3× margin. Defined early
#: so the partial predicates below can reference it.
MAX_NORM_FIELD_CHARS = 300
MAX_QUERY_CHARS = 300

#: The two field targets. Each entry: table, the source column, the frozen index
#: name, and the frozen partial-predicate SQL fragment (used by both the index
#: and the candidate query so the planner can use the partial index). Frozen so
#: preflight / rollback / the 1.7 candidate SQL all reference them by name.

#: External-resource title index. Resources have no status/soft-delete column
#: (0.2 §5: all rows eligible), so the partial predicate is the non-empty +
#: length-bounded normalized form. Inventory (0.3): title p50=49 / max=92 chars,
#: so 300 is a ~3× defensive ceiling that captures 100% of real data while
#: protecting the index from a future pathological overlong title (mirrors the
#: 1.3 finding-2 long-token risk: very long values bloat a trigram index).
TITLE_TABLE = "external_resources"
TITLE_COLUMN = "title"
TITLE_INDEX = "idx_external_resources_title_trgm_norm"
TITLE_PREDICATE = (
    f"char_length(hivemind_normalize_identifier({TITLE_COLUMN})) "
    f"BETWEEN 1 AND {MAX_NORM_FIELD_CHARS:d}"
)

#: Distillation question index. The frozen eligibility predicate
#: (lexical-contract.md §8: ``status IN ('pending','approved')``) IS the partial
#: predicate — so the trigram arm can never surface a rejected/superseded
#: distillation even before the RPC eligibility layer. Plus non-empty + length
#: bound. Inventory: question max=81 chars; 11 rows total.
QUESTION_TABLE = "distillations"
QUESTION_COLUMN = "question"
QUESTION_INDEX = "idx_distillations_question_trgm_norm"
QUESTION_PREDICATE = (
    f"status IN ('pending','approved') "
    f"AND char_length(hivemind_normalize_identifier({QUESTION_COLUMN})) "
    f"BETWEEN 1 AND {MAX_NORM_FIELD_CHARS:d}"
)

#: The two indexes, in dependency-free build order. Frozen.
TARGETS: tuple[dict, ...] = (
    {
        "table": TITLE_TABLE,
        "column": TITLE_COLUMN,
        "index_name": TITLE_INDEX,
        "predicate": TITLE_PREDICATE,
        "eligid": "(resources: no status column; all rows eligible)",
    },
    {
        "table": QUESTION_TABLE,
        "column": QUESTION_COLUMN,
        "index_name": QUESTION_INDEX,
        "predicate": QUESTION_PREDICATE,
        "eligid": "(distillations: status IN pending,approved)",
    },
)
INDEX_NAMES = tuple(t["index_name"] for t in TARGETS)

#: The existing RAW trigram indexes (schema/001) on the same fields. These are
#: NOT normalized (case/Unicode-sensitive); this task adds the normalized form
#: alongside them additively and never drops them.
EXISTING_RAW_TITLE_INDEX = "external_resources_title_trgm"
EXISTING_RAW_QUESTION_INDEX = "distillations_question_trgm"


# ---------------------------------------------------------------------------
# Frozen candidate-query rules (for task 1.7 — documented + proven, not built)
# ---------------------------------------------------------------------------

#: The frozen trigram operator class. ``gin_trgm_ops`` supports %, <%, %> (and
#: the strict <<% family) plus LIKE/ILIKE/regex against the indexed expression.
TRIGRAM_OPCLASS = "gin_trgm_ops"

#: The frozen indexed expression per target (the COMPACT normalized form).
def index_expression(table: str, column: str) -> str:
    """The exact frozen indexed expression: ``hivemind_normalize_identifier(col)``."""
    return f"hivemind_normalize_identifier({column})"


def fully_qualified_table(table: str) -> str:
    return f"{INDEX_SCHEMA}.{table}"


def normalize_expr(column: str) -> str:
    """The frozen compact-normalized expression of a column/query."""
    return f"hivemind_normalize_identifier({column})"


#: Frozen thresholds (pg_trgm GUCs). Defaults are 0.3 (similarity) / 0.6 (word);
#: the word default is too strict for SHORT normalized identifiers (a typo in a
#: 6-char compact key drops word_similarity fast), so the ident arm freezes a
#: forgiving 0.3 for BOTH. The candidate SQL sets these per session.
SIMILARITY_THRESHOLD = 0.3
WORD_SIMILARITY_THRESHOLD = 0.3

#: Frozen deterministic tie-break (mirrors lexical-contract.md §7; ``item_id``
#: is text / snowflake-safe). Applied to the trigram arm's ``lexical_rank``.
TIE_BREAK = "lexical_rank DESC NULLS LAST, created_at DESC NULLS LAST, id::text ASC"


def candidate_query_template(table: str, column: str, op: str, rank_fn: str,
                             q: str, limit: int = 20) -> str:
    """The frozen 1.7 candidate-query shape for one trigram arm.

    The NORMALIZED query is the needle; the indexed normalized field is the
    haystack (right operand of ``<%`` / either operand of ``%``), so the GIN
    index on ``hivemind_normalize_identifier(<col>)`` is used. The partial-index
    predicate is repeated verbatim so the planner can use the partial index.

    ``op`` is ``%`` (similarity) or ``<%`` (word similarity); ``rank_fn`` is the
    matching ``similarity`` / ``word_similarity``. ``q`` is the RAW query text;
    both sides normalize it identically. Eligibility/identity columns are read
    additively; no source row is mutated.
    """
    pred = next(t["predicate"] for t in TARGETS if t["table"] == table)
    qnorm = normalize_expr(f"'{q}'")
    return f"""
SELECT id::text AS item_id
  FROM {fully_qualified_table(table)}
 WHERE {pred}
   AND {qnorm} {op} {normalize_expr(column)}
 ORDER BY {rank_fn}({qnorm}, {normalize_expr(column)}) DESC NULLS LAST,
          created_at DESC NULLS LAST, id::text ASC
 LIMIT {int(limit)};
""".strip()


# ---------------------------------------------------------------------------
# Additive / idempotent online build statements + rollback
# ---------------------------------------------------------------------------

#: Supabase Pro included disk (phase0-capacity.md §4). Used only for headroom math.
SUPABASE_PRO_DISK_BYTES = 8 * 1024 ** 3

#: How many multiples of the projected index size must remain free on the disk.
HEADROOM_MULTIPLE = 3


def build_statements(
    *,
    lock_timeout_s: int = 30,
    statement_timeout_s: int | None = 1800,
) -> str:
    """The exact SQL the live/rehearsal driver executes for the online builds.

    Each target is its own ``CREATE INDEX CONCURRENTLY`` (CIC forbids a
    transaction block, so ``psql -f`` — which does not wrap in one — runs each
    statement with autocommit). The two ``SET`` statements precede BOTH builds
    in one psql session so they persist::

      SET lock_timeout = '<s>';            -- fail fast on a transient lock conflict
      [SET statement_timeout = '<s>';]     -- liveness cap so a wedged build dies
      CREATE INDEX CONCURRENTLY IF NOT EXISTS <name>
          ON public.<table> USING gin (hivemind_normalize_identifier(<col>) gin_trgm_ops)
          WHERE <frozen partial predicate>;
      CREATE INDEX CONCURRENTLY IF NOT EXISTS <name2> ON ...;

    The partial predicate encodes eligibility (distillation status) + a
    non-empty/length bound on the normalized form, mirroring the candidate query.
    """
    lines = [f"SET lock_timeout = '{int(lock_timeout_s)}s';"]
    if statement_timeout_s is not None:
        lines.append(f"SET statement_timeout = '{int(statement_timeout_s)}s';")
    for t in TARGETS:
        lines.append(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {t['index_name']}\n"
            f"    ON {fully_qualified_table(t['table'])}\n"
            f"    USING gin ({index_expression(t['table'], t['column'])} {TRIGRAM_OPCLASS})\n"
            f"    WHERE {t['predicate']};"
        )
    return "\n".join(lines) + "\n"


def rollback_statements(*, concurrent: bool = True) -> str:
    """The online rollback: drop BOTH task-1.5 indexes (safe no-op if absent).

    Only drops the task-1.5 normalized indexes. The raw schema/001 trigram
    indexes and the schema/005 prerequisite are left in place.
    """
    kw = "CONCURRENTLY " if concurrent else ""
    return "\n".join(
        f"DROP INDEX {kw}IF EXISTS {INDEX_SCHEMA}.{name};" for name in INDEX_NAMES
    ) + "\n"


def rollback_statement(name: str = TITLE_INDEX, *, concurrent: bool = True) -> str:
    """Single-index online rollback (matches the 1.3 driver shape)."""
    kw = "CONCURRENTLY " if concurrent else ""
    return f"DROP INDEX {kw}IF EXISTS {INDEX_SCHEMA}.{name};"


# ---------------------------------------------------------------------------
# Read-only preflight queries (live Hivemind) + verdict logic
# ---------------------------------------------------------------------------

def schema_005_applied_check() -> str:
    """Read-only check that the schema/005 prerequisite objects are live.

    Confirms the IMMUTABLE normalize function + the ICU collation it depends on
    exist in public (the indexes cannot be created without them). Returns one
    row of (fn_exists, collation_exists).
    """
    return """
        SELECT
          EXISTS (SELECT 1 FROM pg_catalog.pg_proc p
                   JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
                   WHERE n.nspname = 'public' AND p.proname = 'hivemind_normalize_identifier'
                     AND p.pronargs = 1) AS fn_exists,
          EXISTS (SELECT 1 FROM pg_catalog.pg_collation
                   WHERE collnamespace = 'public'::regnamespace
                     AND collname = 'hivemind_unicode') AS collation_exists;
    """.strip()


def _target_identity_query() -> str:
    """One read-only query: column identity + row estimate for BOTH targets."""
    return f"""
        SELECT c.relname AS table_name,
               a.attname AS column_name,
               a.atttypid::regtype::text AS column_type,
               a.attnotnull AS not_null,
               c.reltuples::bigint AS est_rows,
               s.n_live_tup::bigint AS live_tup
          FROM (VALUES
            ('{TITLE_TABLE}', '{TITLE_COLUMN}'),
            ('{QUESTION_TABLE}', '{QUESTION_COLUMN}')
          ) AS want(want_table, want_column)
          JOIN pg_catalog.pg_class c
            ON c.relname = want_table AND c.relkind = 'r'
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
          JOIN pg_catalog.pg_attribute a
            ON a.attrelid = c.oid AND a.attname = want_column AND NOT a.attisdropped
     LEFT JOIN pg_catalog.pg_stat_user_tables s ON s.relid = c.oid;
    """.strip()


def preflight_queries() -> "list[tuple[str, str]]":
    """Ordered (label, read-only SQL) checks the live driver runs before building.

    Establishes: schema/005 prerequisite present; target table/column identity +
    row estimate; existing trigram indexes on the targets (raw + normalized);
    invalid remnants; in-progress builds; storage headroom; long/locking
    transactions; relation locks; timeout/maintenance settings. Connection mode
    is evaluated in Python from the derived PGHOST/PGPORT (see evaluate_preflight).
    """
    q_trgm_indexes = f"""
        SELECT tc.relname AS table_name,
               ic.relname AS index_name,
               i.indisvalid AS is_valid,
               i.indisready AS is_ready,
               pg_get_indexdef(i.indexrelid) AS indexdef,
               pg_relation_size(i.indexrelid) AS bytes
          FROM pg_catalog.pg_index i
          JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid
          JOIN pg_catalog.pg_class tc ON tc.oid = i.indrelid
          JOIN pg_catalog.pg_namespace n ON n.oid = ic.relnamespace
         WHERE n.nspname = 'public'
           AND tc.relname IN ('{TITLE_TABLE}', '{QUESTION_TABLE}')
           AND pg_get_indexdef(i.indexrelid) LIKE '%gin_trgm_ops%';
    """
    q_invalid = f"""
        SELECT ic.relname AS index_name, i.indisvalid, i.indisready,
               pg_get_indexdef(i.indexrelid)
          FROM pg_catalog.pg_index i
          JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid
          JOIN pg_catalog.pg_class tc ON tc.oid = i.indrelid
          JOIN pg_catalog.pg_namespace n ON n.oid = ic.relnamespace
         WHERE n.nspname = 'public'
           AND tc.relname IN ('{TITLE_TABLE}', '{QUESTION_TABLE}')
           AND i.indisvalid = false;
    """
    q_in_progress = """
        SELECT pid, relid::regclass::text AS rel, command, phase,
               blocks_done, blocks_total, tuples_done, tuples_total
          FROM pg_catalog.pg_stat_progress_create_index;
    """
    q_storage = """
        SELECT current_database() AS db,
               pg_database_size(current_database()) AS db_bytes
          FROM pg_catalog.pg_database WHERE datname = current_database();
    """
    q_long_txns = """
        SELECT pid, state, wait_event_type, wait_event,
               now() - xact_start AS xact_age, now() - query_start AS query_age,
               left(query, 80) AS query_head
          FROM pg_catalog.pg_stat_activity
         WHERE datname = current_database()
           AND (xact_start IS NOT NULL OR state = 'idle in transaction')
           AND pid <> pg_backend_pid()
         ORDER BY xact_start NULLS LAST
         LIMIT 10;
    """
    q_locks = f"""
        SELECT l.locktype, a.usename, a.state, l.granted,
               now() - a.query_start AS query_age, l.mode, c.relname
          FROM pg_catalog.pg_locks l
          JOIN pg_catalog.pg_stat_activity a ON a.pid = l.pid
     LEFT JOIN pg_catalog.pg_class c ON c.oid = l.relation
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE l.locktype = 'relation'
           AND n.nspname = 'public'
           AND c.relname IN ('{TITLE_TABLE}', '{QUESTION_TABLE}')
           AND l.pid <> pg_backend_pid();
    """
    q_settings = """
        SELECT name, setting, unit
          FROM pg_catalog.pg_settings
         WHERE name IN ('statement_timeout','lock_timeout','maintenance_work_mem',
                        'max_parallel_maintenance_workers');
    """
    return [
        ("schema_005_prerequisite", schema_005_applied_check()),
        ("target_identity", _target_identity_query()),
        ("existing_trgm_indexes", q_trgm_indexes.strip()),
        ("invalid_index_remnants", q_invalid.strip()),
        ("in_progress_index_builds", q_in_progress.strip()),
        ("database_storage", q_storage.strip()),
        ("long_or_locking_transactions", q_long_txns.strip()),
        ("relation_locks", q_locks.strip()),
        ("timeout_and_maintenance_settings", q_settings.strip()),
    ]


def estimate_index_bytes(est_rows: int) -> int:
    """Rough projection of the combined normalized trigram index size.

    A GIN trigram index is ~3 trigrams/char + posting lists. Calibrated to the
    existing raw indexes (schema/001 title index is a few hundred KB at ~2,759
    rows) we model generously at ~400 B/row for short fields. A planning
    heuristic only; the rehearsal measures the real number (these tables are
    tiny: 2,759 resources + 11 distillations).
    """
    return max(0, int(est_rows)) * 400


def _is_true(v) -> bool:
    return str(v).strip().lower() in ("t", "true", "1", "yes")


def evaluate_preflight(
    parsed: dict,
    *,
    pghost: str = "",
    pgport: str = "",
    disk_bytes: int = SUPABASE_PRO_DISK_BYTES,
) -> dict:
    """Turn parsed preflight rows into a green/red verdict with explicit reasons.

    Green requires: schema/005 prerequisite present; both target tables/columns
    present; no invalid remnants; no in-progress builds; storage headroom;
    session-mode (non-transaction-pooler) connection. Existing normalized
    indexes (already valid) are reported and let ``--apply`` skip a rebuild.
    """
    checks: list[dict] = []
    reasons: list[str] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "pass": bool(ok), "detail": detail})
        if not ok:
            reasons.append(f"{name}: {detail}")

    # --- schema/005 prerequisite (the indexes cannot be created without it) -----
    pre = parsed.get("schema_005_prerequisite", [])
    if pre and pre[0]:
        fn_ok, col_ok = (list(pre[0]) + [None, None])[:2]
        pre_ok = _is_true(fn_ok) and _is_true(col_ok)
        add("schema_005_prerequisite", pre_ok,
            f"normalize_fn={fn_ok} icu_collation={col_ok}")
    else:
        add("schema_005_prerequisite", False, "no prerequisite check row")

    # --- target identity + row estimate ---------------------------------------
    id_rows = parsed.get("target_identity", [])
    found = {(r[0], r[1]) for r in id_rows if r and r[0] and r[1]}
    want = {(TITLE_TABLE, TITLE_COLUMN), (QUESTION_TABLE, QUESTION_COLUMN)}
    identity_ok = want <= found
    add("target_identity", identity_ok,
        f"targets_found={sorted(found)} want={sorted(want)}")
    est_rows = sum(int((list(r) + [0, 0])[4] or (list(r) + [0, 0])[5] or 0)
                   for r in id_rows if r and len(r) >= 5 and str(r[0]) in
                   (TITLE_TABLE, QUESTION_TABLE)) if id_rows else 0

    # --- existing trigram indexes (informational) -----------------------------
    trgm = parsed.get("existing_trgm_indexes", [])
    existing_names = [r[1] for r in trgm if r and len(r) > 1]
    already_valid = all(
        any(r[1] == name and _is_true(r[2]) for r in trgm if r and len(r) > 2)
        for name in INDEX_NAMES)
    add("existing_trgm_indexes", True,
        f"trgm_indexes={existing_names or 'none'}; "
        f"normalized_targets_already_valid={already_valid}")

    # --- invalid remnants (BLOCKER) -------------------------------------------
    inv = [r for r in parsed.get("invalid_index_remnants", []) if r and r[0]]
    add("no_invalid_remnants", not inv,
        f"{len(inv)} invalid index(es): {[r[0] for r in inv]}" if inv else "none")

    # --- in-progress builds (BLOCKER) -----------------------------------------
    prog = [r for r in parsed.get("in_progress_index_builds", []) if r and r[0]]
    add("no_in_progress_build", not prog,
        f"{len(prog)} CREATE INDEX in progress" if prog else "none")

    # --- long / locking transactions (risk) -----------------------------------
    long_tx = [r for r in parsed.get("long_or_locking_transactions", []) if r and r[0]]
    add("no_blocking_long_txn", not long_tx,
        f"{len(long_tx)} open txn(s)" if long_tx else "none")

    # --- relation locks (risk) ------------------------------------------------
    locks = [r for r in parsed.get("relation_locks", []) if r and r[0]]
    add("no_relation_locks", not locks,
        f"{len(locks)} holder(s) on targets" if locks else "none")

    # --- storage headroom -----------------------------------------------------
    db_rows = parsed.get("database_storage", [])
    db_bytes = int(db_rows[0][1]) if db_rows and db_rows[0][1] else 0
    est_index = estimate_index_bytes(est_rows)
    free = max(0, disk_bytes - db_bytes)
    add("storage_headroom", free >= est_index * HEADROOM_MULTIPLE,
        f"db≈{db_bytes/1e9:.2f}GB disk={disk_bytes/1e9:.0f}GB free≈{free/1e9:.2f}GB "
        f"need≈{est_index*HEADROOM_MULTIPLE/1e9:.4f}GB (idx est≈{est_index/1e3:.0f}KB)")

    # --- settings (informational) ---------------------------------------------
    set_rows = {r[0]: (r[1], r[2]) for r in parsed.get(
        "timeout_and_maintenance_settings", []) if r and r[0]}
    add("settings_inspected", True,
        "; ".join(f"{k}={set_rows[k][0]}{set_rows[k][1] or ''}" for k in sorted(set_rows))
        or "none")

    # --- connection mode (session vs transaction-pooler) ----------------------
    pooler_host = "pooler.supabase.com" in pghost
    if pooler_host and pgport in ("6543", "6544"):
        conn_mode, conn_ok = "transaction_pooler", False
    elif pooler_host:
        conn_mode, conn_ok = "session", True
    else:
        conn_mode, conn_ok = "direct", True
    add("session_mode_connection", conn_ok, f"host_family={conn_mode} port={pgport or '?'}")

    # A build is *blocked* only by an OPERATIONAL gate failing. The schema/005
    # prerequisite is NOT a blocker: if absent, ``--apply`` applies it first
    # (idempotent, additive DDL) — exactly the task-1.5 instruction. It stays in
    # ``checks`` (informational) and drives ``schema_005_needed``.
    green = all(c["pass"] for c in checks if c["name"] in {
        "target_identity", "no_invalid_remnants", "no_in_progress_build",
        "storage_headroom", "session_mode_connection",
    })
    return {
        "green": green,
        "checks": checks,
        "reasons": reasons,
        "est_rows": est_rows,
        "est_index_bytes": est_index,
        "headroom_bytes": free,
        "conn_mode": conn_mode,
        "already_valid": already_valid,
        "schema_005_needed": not (pre and pre[0] and _is_true(pre[0][0])
                                  and _is_true(pre[0][1])),
    }


# ---------------------------------------------------------------------------
# Representative EXPLAIN evidence queries (prove the normalized index is used)
# ---------------------------------------------------------------------------

#: Representative normalized-identifier needles drawn from the frozen golden set
#: (lexical-contract.md §10; identifier-normalization fixtures). Each is a form a
#: user would type that MUST match across separator/case variants via the
#: compact-normalized trigram index. ``op`` is the frozen trigram operator
#: exercised; ``table`` is the target.
#:
#: PRIMARY operator is ``<%`` (word similarity) per the frozen contract §5 — the
#: ident arm matches an identifier that is a *substring/word* of the field, so a
#: short query (``Wan2.2``→``wan22``) still hits a long title whose compact form
#: starts with it. ``%`` (full similarity) is exercised too: it serves near-full
#: title matches but returns LOW similarity for a short needle in a long field
#: (the reason ``<%`` is primary).
REPRESENTATIVE_QUERIES = (
    ("title_wordsim_flux", "FLUX.1", "<%", TITLE_TABLE),            # dotted name, substring
    ("title_wordsim_wan_variant", "Wan2.2", "<%", TITLE_TABLE),     # spelling variant → wan22
    ("title_wordsim_symbol", "WanVideoSampler", "<%", TITLE_TABLE), # node class substring
    ("title_similarity_full", "LTX-Video fast video workflow", "%", TITLE_TABLE),  # near-full
    ("question_wordsim_upscale", "best upscale model", "<%", QUESTION_TABLE),      # question
)


def _evidence_sql(label: str, q: str, op: str, table: str, *, force_index: bool) -> str:
    """Build one EXPLAIN evidence query in the frozen candidate shape.

    The normalized query is the needle; the indexed normalized field is the
    haystack (right operand of ``<%`` / either operand of ``%``), so the GIN
    index on ``hivemind_normalize_identifier(<col>)`` is used. The partial
    predicate is repeated verbatim; ranking is the matching similarity function;
    the frozen tie-break + limit close it.

    ``force_index`` prepends ``SET enable_seqscan = off`` so the planner MUST use
    the index — used only to prove STRUCTURAL usability on the tiny 11-row
    distillation table (whose natural plan is, correctly, a seq scan).
    """
    column = QUESTION_COLUMN if table == QUESTION_TABLE else TITLE_COLUMN
    rank_fn = "word_similarity" if op == "<%" else "similarity"
    qnorm = f"hivemind_normalize_identifier('{q}')"
    pred = next(t["predicate"] for t in TARGETS if t["table"] == table)
    guc = "pg_trgm.word_similarity_threshold" if op == "<%" else "pg_trgm.similarity_threshold"
    thr = WORD_SIMILARITY_THRESHOLD if op == "<%" else SIMILARITY_THRESHOLD
    prefix = "SET enable_seqscan = off;\n" if force_index else ""
    return f"""
{prefix}SET {guc} = {thr};
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, SUMMARY ON)
SELECT id::text AS item_id,
       {rank_fn}({qnorm}, hivemind_normalize_identifier({column})) AS lexical_rank
  FROM {fully_qualified_table(table)}
 WHERE {pred}
   AND {qnorm} {op} hivemind_normalize_identifier({column})
 ORDER BY lexical_rank DESC NULLS LAST, created_at DESC NULLS LAST, id::text ASC
 LIMIT 20;
""".strip()


def evidence_queries() -> "list[tuple[str, str]]":
    """Natural EXPLAIN (ANALYZE, BUFFERS) plans — real planner behavior per target.

    At production scale the title index (2,759 rows) IS used; the question index
    (11 rows) is too small for the planner to prefer it (correct — a seq scan of
    11 rows is cheaper). Use :func:`forced_evidence_queries` to prove the
    question index is structurally usable regardless of table size.
    """
    return [(label, _evidence_sql(label, q, op, table, force_index=False))
            for label, q, op, table in REPRESENTATIVE_QUERIES]


def forced_evidence_queries() -> "list[tuple[str, str]]":
    """Structural-usability EXPLAIN plans with ``enable_seqscan = off``.

    Proves every target × operator is SERVED by its normalized index independent
    of table size (the 11-row distillation table's natural plan seq-scans). The
    natural plan (:func:`evidence_queries`) is the production-behavior truth.
    """
    return [(f"{label}__forced", _evidence_sql(label, q, op, table, force_index=True))
            for label, q, op, table in REPRESENTATIVE_QUERIES]


def baseline_no_norm_index_query() -> str:
    """A rehearsal-only diagnostic: a normalized ``<%`` title query, used to show
    the planner seq-scans when the normalized index is absent (it cannot use the
    raw title index because the query normalizes the haystack expression)."""
    qnorm = "hivemind_normalize_identifier('FLUX.1')"
    return f"""
SET pg_trgm.word_similarity_threshold = {WORD_SIMILARITY_THRESHOLD};
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, SUMMARY ON)
SELECT id::text
  FROM {fully_qualified_table(TITLE_TABLE)}
 WHERE {TITLE_PREDICATE}
   AND {qnorm} <% hivemind_normalize_identifier({TITLE_COLUMN})
 LIMIT 20;
""".strip()


def parse_explain_plan(plan_text: str, index_name: str | None = None) -> dict:
    """Classify a saved EXPLAIN plan: did a target normalized trigram index fire?

    Matches ``Index ... Scan on <index_name>`` / ``Bitmap Index Scan on
    <index_name>`` for the given index (or any task-1.5 index when ``None``),
    the RAW indexes, and whether a Seq Scan / Bitmap Heap Scan appeared.
    Conservative: matches index names literally.
    """
    text = plan_text or ""
    want = (index_name,) if index_name else INDEX_NAMES
    norm_hit = any(
        re.search(r"(Index Scan|Bitmap Index Scan)[^\n]*\b" + re.escape(n) + r"\b", text)
        for n in want)
    raw_names = (EXISTING_RAW_TITLE_INDEX, EXISTING_RAW_QUESTION_INDEX)
    raw_hit = any(
        re.search(r"(Index Scan|Bitmap Scan|Bitmap Index Scan)[^\n]*\b" + re.escape(n) + r"\b",
                  text) for n in raw_names)
    return {
        "uses_normalized_index": bool(norm_hit),
        "uses_raw_trgm_index": bool(raw_hit),
        "is_seq_scan": bool(re.search(r"\bSeq Scan\b", text)),
        "is_bitmap_heap_scan": bool(re.search(r"\bBitmap Heap Scan\b", text)),
        "plan_present": bool(text.strip()),
    }


# ---------------------------------------------------------------------------
# Production-shaped rehearsal schema + deterministic seed (isolated cluster)
# ---------------------------------------------------------------------------

def rehearsal_load_005_sql() -> str:
    """Load the schema/005 prerequisite (collation + functions) into the cluster.

    The rehearsal cluster needs the FROZEN ``hivemind_normalize_identifier`` to
    build the normalized expression indexes, so it loads the canonical 1.4
    migration verbatim. Idempotent. (The alias table is also created but empty.)
    """
    sql_path = REPO_ROOT / "schema" / "005_identifier_normalization.sql"
    return sql_path.read_text()


def rehearsal_schema_sql() -> str:
    """Mirror the live external_resources + distillations shape the index touches.

    Only the columns the build/evidence read are needed, but we keep the real
    shape (id PK, title/question text NOT NULL, kind/source/status, created_at)
    so plan/size realism holds. Drops first so a re-run is clean.
    """
    return f"""
create extension if not exists pg_trgm;
DROP TABLE IF EXISTS {fully_qualified_table(QUESTION_TABLE)};
DROP TABLE IF EXISTS {fully_qualified_table(TITLE_TABLE)};
CREATE TABLE {fully_qualified_table(TITLE_TABLE)} (
  id          bigint PRIMARY KEY,
  kind        text NOT NULL,
  source      text NOT NULL,
  title       text NOT NULL,
  body        text NOT NULL DEFAULT '',
  metadata    jsonb NOT NULL DEFAULT '{{}}'::jsonb,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE {fully_qualified_table(QUESTION_TABLE)} (
  id          bigint PRIMARY KEY,
  question    text NOT NULL,
  conditions  text,
  answer      text NOT NULL DEFAULT '',
  status      text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','approved','rejected','superseded')),
  confidence  text NOT NULL DEFAULT 'medium',
  created_at  timestamptz NOT NULL DEFAULT now()
);
""".strip()


# Deterministic video-gen title vocabulary + golden identifiers. The seed scatters
# real model/node/filename identifiers (so evidence queries return hits across
# separator/case variants) plus filler titles (so the index size is realistic).
_GOLDEN_TITLES = (
    ("Wan 2.2 Image to Video with Florence2 and LoRA", "workflow"),
    ("Wan2.2 I2V LoRA detailer workflow", "workflow"),
    ("wan_2.2 image-to-video (fp8)", "workflow"),
    ("FLUX.1 [dev] text-to-image workflow", "workflow"),
    ("FLUX1 dev controlnet stack", "workflow"),
    ("LTX-Video fast video workflow", "workflow"),
    ("ltx-2-19b-ic-lora-detailer", "workflow"),
    ("lightx2v_I2V_14B.safetensors loader", "workflow"),
    ("WanVideoSampler with multiple LoRAs", "workflow"),
    ("IPAdapterFaceIDKolors face swap workflow", "workflow"),
    ("ControlNet settings for anime video", "article"),
    ("Best upscale model for 4K video", "article"),
    ("How to lower the motion amplitude", "article"),
    ("reduce motion strength in Wan 2.2", "article"),
    ("Hunyuan Video sampler settings guide", "blog_post"),
    ("CogVideoX frame interpolation", "transcript"),
    ("fp8 vs bf16 memory comparison", "article"),
    ("Kohya dreambooth training defaults", "article"),
    ("DiT / MMDiT attention explained", "blog_post"),
)

# Deterministic filler words to pad the title corpus to a production-shaped ~2,759
# rows with realistic length (inventory title p50=49 / max=92).
_FILLER = (
    "workflow", "model", "guide", "tutorial", "comparison", "settings",
    "sampler", "scheduler", "checkpoint", "lora", "adapter", "upscale",
    "video", "image", "node", "queue", "fix", "crash", "review", "demo",
)


def rehearsal_seed_sql(n_titles: int = 2_759, n_questions: int = 11) -> str:
    """Deterministic production-shaped seed for the isolated rehearsal cluster.

    Builds ~``n_titles`` resource rows (golden identifiers repeated + filler so
    a query like ``Wan2.2`` hits the ``Wan 2.2``/``wan_2.2`` rows via the compact
    key ``wan22``, proving cross-variant matching) and ``n_questions``
    distillation questions across statuses (pending/approved/rejected — proving
    the status partial predicate excludes rejected rows). A couple of titles are
    all-punctuation or overlong to exercise the length/non-empty bounds.

    Everything derives from the row index; no RNG, fully reproducible.
    """
    g = len(_GOLDEN_TITLES)
    golden_vals = ", ".join(f"('{t}','{k}')" for t, k in _GOLDEN_TITLES)
    fill_vals = ", ".join(f"('{w}')" for w in _FILLER)
    f = len(_FILLER)
    questions_sql = """
INSERT INTO public.distillations (id, question, conditions, answer, status, confidence, created_at)
SELECT g,
       CASE
         WHEN g % 2 = 0 THEN 'What is the best upscale model for 4K anime video?'
         WHEN g % 3 = 0 THEN 'How do I configure ControlNet settings for Wan 2.2?'
         WHEN g % 5 = 0 THEN 'Which sampler reduces motion strength best?'
         ELSE 'How to lower the motion amplitude in WanVideoSampler?'
       END,
       'in this case',
       'answer body',
       CASE WHEN g % 4 = 0 THEN 'rejected'
            WHEN g % 3 = 0 THEN 'approved'
            ELSE 'pending' END,
       'medium',
       now() - (g % 1000) * interval '1 minute'
  FROM generate_series(1, {nq}) g;
""".format(nq=n_questions).strip()
    return f"""
-- resource titles: golden identifiers + realistic filler (~{n_titles} rows)
WITH golden(title, kind) AS (SELECT title, kind FROM (VALUES {golden_vals}) v(title, kind)),
     filler(word) AS (SELECT word FROM (VALUES {fill_vals}) v(word))
INSERT INTO {fully_qualified_table(TITLE_TABLE)} (id, kind, source, title, created_at)
SELECT g,
       COALESCE(gk.kind, CASE WHEN g % 4 = 0 THEN 'article' WHEN g % 5 = 0 THEN 'transcript' ELSE 'workflow' END),
       'vibecomfy-external',
       CASE
         WHEN g <= {g} THEN gk.title
         WHEN g % 7 = 0 THEN '... --- ...'                          -- all separators -> empty compact (excluded)
         WHEN g % 11 = 0 THEN repeat('overlong-title-token-' || g::text || ' ', 30)  -- overlong (excluded by bound)
         ELSE (SELECT word FROM filler WHERE word IS NOT NULL OFFSET (g % {f}) LIMIT 1)
              || ' ' || (SELECT word FROM filler WHERE word IS NOT NULL OFFSET ((g*3) % {f}) LIMIT 1)
              || ' v' || (g % 20)::text || '.' || (g % 10)::text
       END,
       now() - (g % 50000) * interval '1 minute'
  FROM generate_series(1, {n_titles}) g
  LEFT JOIN LATERAL (SELECT title, kind FROM golden WHERE title IS NOT NULL
                      OFFSET ((g - 1) % {g}) LIMIT 1) gk ON g <= {g};

-- distillation questions across statuses
{questions_sql}

ANALYZE {fully_qualified_table(TITLE_TABLE)};
ANALYZE {fully_qualified_table(QUESTION_TABLE)};
""".strip()


def summarize() -> dict:
    """Compact machine-readable summary of the task-1.5 index identities."""
    return {
        "task": "1.5",
        "index_schema": INDEX_SCHEMA,
        "targets": [
            {"table": t["table"], "column": t["column"], "index_name": t["index_name"],
             "expression": index_expression(t["table"], t["column"]),
             "opclass": TRIGRAM_OPCLASS, "partial_predicate": t["predicate"]}
            for t in TARGETS
        ],
        "normalized_form": "compact (public.hivemind_normalize_identifier)",
        "thresholds": {"similarity": SIMILARITY_THRESHOLD,
                       "word_similarity": WORD_SIMILARITY_THRESHOLD},
        "length_bounds": {"max_normalized_field_chars": MAX_NORM_FIELD_CHARS,
                          "max_query_chars": MAX_QUERY_CHARS},
        "tie_break": TIE_BREAK,
        "build_statements": build_statements(),
        "rollback_statements": rollback_statements(),
        "deferred": ["full-message exact-identifier path (1.6)",
                     "multi-arm candidate SQL / RPC (1.7)",
                     "trigram on large bodies (explicitly out)"],
    }
