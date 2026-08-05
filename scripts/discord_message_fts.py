"""Canonical Discord-message FTS index — build/preflight/evidence helpers (task 1.3).

This module is the **pure, importable, stdlib-only** core for plan task 1.3:

  * the frozen index identity (name, table, expression), cross-checked against
    ``executors.lexical_contract`` (the frozen 1.1 contract) so the build and the
    eventual candidate query (1.7) reference the *same* expression;
  * the additive/idempotent online ``CREATE INDEX CONCURRENTLY`` statement and its
    rollback (the executable form; ``schema/004_discord_message_fts.sql`` is the
    documented artifact and a unit test pins the two agree);
  * the read-only **preflight** query set and verdict logic (gated live build);
  * the representative ``EXPLAIN (ANALYZE, BUFFERS)`` evidence queries — always on
    ``'simple'``, always encoding ``is_deleted = false`` and snowflakes-as-text;
  * plan-parsing helpers so tests can assert index usage against captured evidence;
  * the production-shaped rehearsal schema/seed SQL and cancellation/lock model.

It connects to **no** database and calls **no** CLI by itself: the operator
drivers ``scripts/rehearse_discord_fts.py`` (isolated local cluster) and
``scripts/live_discord_fts.py`` (Hivemind project, task-0.1 session-mode access)
do the I/O. Every human-facing line those drivers emit is routed through
:func:`redact`, reused verbatim from the task-0.1 access probe so the safety
boundary never drifts.

Frozen reference: ``executors.lexical_contract`` (config ``'simple'``, message
bare source ``content``), ``docs/hybrid-search/phase1-lexical-contract.md`` §3.
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

from executors import lexical_contract as LC  # noqa: E402

# ---------------------------------------------------------------------------
# Frozen index identity (cross-checked against the 1.1 lexical contract)
# ---------------------------------------------------------------------------

#: The schema/owner of the underlying table and the new index.
INDEX_SCHEMA = "public"
#: The underlying table the plan says to search (NOT the message_feed view).
INDEX_TABLE = "discord_messages"
#: The message prose source column (lexical_contract.MESSAGE_BARE_SOURCE).
INDEX_COLUMN = LC.MESSAGE_BARE_SOURCE
assert INDEX_COLUMN == "content", "frozen contract drifted: message bare source"
#: The frozen text-search configuration (lexical_contract.LEXICAL_CONFIG).
LEXICAL_CONFIG = LC.LEXICAL_CONFIG
assert LEXICAL_CONFIG == "simple", "frozen contract drifted: lexical config"

#: The new canonical index name. Frozen so preflight/rollback/1.7 reference it.
INDEX_NAME = "idx_discord_messages_content_fts_simple"

#: The existing 'english' index this *supplements* (retained until the 1.11 gate).
ENGLISH_INDEX_NAME = "idx_discord_messages_content_fts"

#: The fully-qualified new index.
INDEX_QUALNAME = f"{INDEX_SCHEMA}.{INDEX_NAME}"

#: The frozen indexed expression (lexical-contract.md §3). This MUST be byte-for-byte
#: the expression the candidate query uses, or the planner cannot match it.
def index_expression() -> str:
    """The exact frozen indexed expression: ``to_tsvector('simple', coalesce(content,''))``."""
    return (
        f"to_tsvector({LEXICAL_CONFIG!r}::regconfig, coalesce({INDEX_COLUMN}, ''))"
    )


def fully_qualified_table() -> str:
    return f"{INDEX_SCHEMA}.{INDEX_TABLE}"


# ---------------------------------------------------------------------------
# The additive / idempotent online build statement + rollback
# ---------------------------------------------------------------------------

#: Supabase Pro included disk (phase0-capacity.md §4). Used only for headroom math.
SUPABASE_PRO_DISK_BYTES = 8 * 1024 ** 3

#: How many multiples of the projected index size must remain free on the disk.
HEADROOM_MULTIPLE = 3


def build_statement(
    *,
    lock_timeout_s: int = 30,
    statement_timeout_s: int | None = 1800,
    index_name: str = INDEX_NAME,
) -> str:
    """The exact SQL the live/rehearsal driver executes for the online build.

    Two statements, both in ONE psql session so the ``SET`` persists to the
    ``CREATE INDEX CONCURRENTLY`` (each statement autocommits — ``psql -f`` does
    not wrap in a transaction block, which is required because CIC forbids one):

      SET lock_timeout = '<s>';            -- fail fast on a transient lock conflict
      [SET statement_timeout = '<s>';]     -- generous cap so a wedged build dies
      CREATE INDEX CONCURRENTLY IF NOT EXISTS <name>
          ON public.discord_messages USING gin (to_tsvector('simple', coalesce(content,'')));

    ``lock_timeout`` is the safety bound: a concurrent build takes only brief SHARE
    locks at start/end; if it cannot acquire one within ``lock_timeout`` it aborts
    cleanly rather than queuing behind a long writer. ``statement_timeout`` is the
    liveness bound for the build itself (disabled when ``None``).
    """
    lines = [f"SET lock_timeout = '{int(lock_timeout_s)}s';"]
    if statement_timeout_s is not None:
        lines.append(f"SET statement_timeout = '{int(statement_timeout_s)}s';")
    lines.append(
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name}\n"
        f"    ON {fully_qualified_table()}\n"
        f"    USING gin ({index_expression()});"
    )
    return "\n".join(lines) + "\n"


def rollback_statement(*, concurrent: bool = True, index_name: str = INDEX_NAME) -> str:
    """The online rollback: drop the canonical index (safe no-op if absent)."""
    kw = "CONCURRENTLY " if concurrent else ""
    return f"DROP INDEX {kw}IF EXISTS {INDEX_SCHEMA}.{index_name};"


# ---------------------------------------------------------------------------
# Read-only preflight queries (live Hivemind) + verdict logic
# ---------------------------------------------------------------------------

def preflight_queries(index_name: str = INDEX_NAME) -> "list[tuple[str, str]]":
    """Ordered (label, read-only SQL) checks the live driver runs before building.

    Each query is read-only (SELECT against catalogs/stats). They establish:
    identity of the target table/column; the existing FTS indexes; row estimate;
    storage headroom; invalid/concurrent index remnants; in-progress builds;
    long/locking transactions; lock + statement timeout settings; session mode.
    """
    q_relid = f"""
        SELECT c.relname AS table_name,
               a.attname AS column_name,
               a.atttypid::regtype::text AS column_type,
               a.attnotnull AS not_null,
               c.reltuples::bigint AS est_rows,
               s.n_live_tup::bigint AS live_tup
          FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
          JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid AND NOT a.attisdropped
     LEFT JOIN pg_catalog.pg_stat_user_tables s ON s.relid = c.oid
         WHERE n.nspname = 'public' AND c.relname = '{INDEX_TABLE}'
           AND a.attname = '{INDEX_COLUMN}' AND c.relkind = 'r';
    """
    q_fts_indexes = f"""
        SELECT ic.relname AS index_name,
               i.indisvalid AS is_valid,
               i.indisready AS is_ready,
               pg_get_indexdef(i.indexrelid) AS indexdef,
               pg_relation_size(i.indexrelid) AS bytes
          FROM pg_catalog.pg_index i
          JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid
          JOIN pg_catalog.pg_class tc ON tc.oid = i.indrelid
          JOIN pg_catalog.pg_namespace n ON n.oid = ic.relnamespace
         WHERE n.nspname = 'public' AND tc.relname = '{INDEX_TABLE}'
           AND pg_get_indexdef(i.indexrelid) LIKE '%to_tsvector%';
    """
    q_invalid_remnants = f"""
        SELECT ic.relname AS index_name, i.indisvalid, i.indisready, pg_get_indexdef(i.indexrelid)
          FROM pg_catalog.pg_index i
          JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid
          JOIN pg_catalog.pg_class tc ON tc.oid = i.indrelid
          JOIN pg_catalog.pg_namespace n ON n.oid = ic.relnamespace
         WHERE n.nspname = 'public' AND tc.relname = '{INDEX_TABLE}'
           AND i.indisvalid = false;
    """
    q_in_progress = """
        SELECT pid, relid::regclass::text AS rel, command, phase,
               blocks_done, blocks_total, tuples_done, tuples_total
          FROM pg_catalog.pg_stat_progress_create_index;
    """
    q_storage = """
        SELECT current_database() AS db,
               pg_database_size(current_database()) AS db_bytes,
               (SELECT setting::bigint FROM pg_catalog.pg_settings WHERE name = 'max_connections') AS max_conns
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
    q_locks_on_rel = f"""
        SELECT l.locktype, a.usename, a.state, l.granted,
               now() - a.query_start AS query_age,
               l.mode
          FROM pg_catalog.pg_locks l
          JOIN pg_catalog.pg_stat_activity a ON a.pid = l.pid
     LEFT JOIN pg_catalog.pg_class c ON c.oid = l.relation
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE l.locktype = 'relation'
           AND n.nspname = 'public' AND c.relname = '{INDEX_TABLE}'
           AND l.pid <> pg_backend_pid();
    """
    q_settings = """
        SELECT name, setting, unit
          FROM pg_catalog.pg_settings
         WHERE name IN ('statement_timeout','lock_timeout','default_transaction_isolation',
                        'maintenance_work_mem','max_parallel_maintenance_workers');
    """
    # Connection-mode probe (session vs pooler). The driver derives PGHOST/PGPORT
    # from `supabase db dump --dry-run` and passes them here as bind values; this
    # query is evaluated in Python (see evaluate_preflight), not SQL.
    return [
        ("table_column_identity", q_relid.strip()),
        ("existing_fts_indexes", q_fts_indexes.strip()),
        ("invalid_index_remnants", q_invalid_remnants.strip()),
        ("in_progress_index_builds", q_in_progress.strip()),
        ("database_storage", q_storage.strip()),
        ("long_or_locking_transactions", q_long_txns.strip()),
        ("relation_locks", q_locks_on_rel.strip()),
        ("timeout_and_maintenance_settings", q_settings.strip()),
    ]


def estimate_index_bytes(est_rows: int) -> int:
    """Rough projection of the new index size from a row estimate.

    Calibrated to the live ``english`` index (85 MB at ~1.25M rows ≈ 70 B/row;
    phase0-inventory.md §4). ``simple`` keeps stopwords and does not stem, so its
    per-document lexeme set is marginally larger — model 1.25x the english rate.
    A planning heuristic only; the rehearsal measures the real number.
    """
    per_row = int(70 * 1.25)
    return max(0, int(est_rows)) * per_row


def evaluate_preflight(
    parsed: dict,
    *,
    pghost: str = "",
    pgport: str = "",
    disk_bytes: int = SUPABASE_PRO_DISK_BYTES,
    est_rows_override: int | None = None,
) -> dict:
    """Turn parsed preflight rows into a green/red verdict with explicit reasons.

    ``parsed`` maps each preflight label to its raw row list (list[list/tuple]).
    Returns::

        {green: bool, checks: [{name, pass, detail}], reasons: [...],
         est_rows, est_index_bytes, headroom_bytes, conn_mode}
    """
    checks: list[dict] = []
    reasons: list[str] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "pass": bool(ok), "detail": detail})
        if not ok:
            reasons.append(f"{name}: {detail}")

    # --- identity -----------------------------------------------------------
    id_rows = parsed.get("table_column_identity", [])
    if not id_rows:
        add("table_column_identity", False, "no discord_messages.content row found")
        col_ok, est_rows = False, 0
    else:
        _, col, coltype, notnull, reltuples, n_live = (list(id_rows[0]) + [None] * 6)[:6]
        col_ok = (col == INDEX_COLUMN and coltype == "text")
        add("table_column_identity", col_ok,
            f"column={col} type={coltype} not_null={notnull}")
        est_rows = est_rows_override if est_rows_override is not None else (
            int(n_live or reltuples or 0) or 0
        )

    # --- existing FTS indexes (informational) -------------------------------
    fts_rows = parsed.get("existing_fts_indexes", [])
    fts_names = [r[0] for r in fts_rows]
    # indisvalid arrives from psql as 't'/'f', not a Python bool.
    def _is_true(v) -> bool:
        return str(v).strip().lower() in ("t", "true", "1", "yes")

    already_valid = any(r[0] == INDEX_NAME and _is_true(r[1]) for r in fts_rows)
    add("target_index_absent_or_known",
        True,
        f"existing fts indexes={fts_names or 'none'}; target already valid={already_valid}")

    # --- invalid remnants (BLOCKER) -----------------------------------------
    inv_rows = [r for r in parsed.get("invalid_index_remnants", []) if r and r[0]]
    if inv_rows:
        # An invalid index with OUR name is a hard blocker (IF NOT EXISTS would
        # skip it). An invalid index with another name is a warning+blocker too,
        # because it indicates a prior failed concurrent build on this relation.
        add("no_invalid_remnants", False,
            f"{len(inv_rows)} invalid index(es) on {INDEX_TABLE}: {[r[0] for r in inv_rows]}")
    else:
        add("no_invalid_remnants", True, "none")

    # --- in-progress builds (BLOCKER) ---------------------------------------
    prog = [r for r in parsed.get("in_progress_index_builds", []) if r and r[0]]
    if prog:
        add("no_in_progress_build", False,
            f"{len(prog)} CREATE INDEX in progress (pid={prog[0][0]})")
    else:
        add("no_in_progress_build", True, "none")

    # --- long / locking transactions (risk) ---------------------------------
    long_tx = [r for r in parsed.get("long_or_locking_transactions", []) if r and r[0]]
    add("no_blocking_long_txn",
        not long_tx,
        f"{len(long_tx)} open txn(s) on db" + (f"; oldest age={long_tx[0][3]}" if long_tx else ""))

    # --- relation locks (risk) ----------------------------------------------
    locks = [r for r in parsed.get("relation_locks", []) if r and r[0]]
    add("no_relation_locks",
        not locks,
        f"{len(locks)} holder(s) of locks on {INDEX_TABLE}" + (f"; modes={[r[5] for r in locks]}" if locks else ""))

    # --- storage headroom ----------------------------------------------------
    db_rows = parsed.get("database_storage", [])
    db_bytes = int(db_rows[0][1]) if db_rows and db_rows[0][1] else 0
    est_index = estimate_index_bytes(est_rows)
    free = max(0, disk_bytes - db_bytes)
    headroom_ok = free >= est_index * HEADROOM_MULTIPLE
    add("storage_headroom", headroom_ok,
        f"db≈{db_bytes/1e9:.2f}GB disk={disk_bytes/1e9:.0f}GB free≈{free/1e9:.2f}GB "
        f"need≈{est_index*HEADROOM_MULTIPLE/1e9:.2f}GB (idx est≈{est_index/1e6:.0f}MB ×{HEADROOM_MULTIPLE})")

    # --- settings (informational; build sets its own bounds) ----------------
    set_rows = {r[0]: (r[1], r[2]) for r in parsed.get("timeout_and_maintenance_settings", []) if r and r[0]}
    add("settings_inspected", True,
        "; ".join(f"{k}={set_rows[k][0]}{set_rows[k][1] or ''}" for k in sorted(set_rows)) or "none")

    # --- connection mode (session vs transaction-pooler) -------------------
    # The build needs a persistent SESSION (one backend for SET + CIC). Supabase
    # session mode is port 5432 (whether via the pooler hostname or direct);
    # that is the path the task-0.1 audit validated. The hard blocker is the
    # TRANSACTION pooler (port 6543), which multiplexes and cannot reliably host
    # a multi-statement CIC. (verify_access reports this same session(5432) mode.)
    pooler_host = "pooler.supabase.com" in pghost
    txn_pooler = pooler_host and pgport in ("6543", "6544")
    if txn_pooler:
        conn_mode = "transaction_pooler"
        conn_ok = False
    elif pooler_host:
        # Pooler session mode (port 5432) pins a backend for the session — the
        # path the task-0.1 audit validated. OK for a multi-statement CIC.
        conn_mode = "session"
        conn_ok = True
    else:
        # Direct (non-pooler) host, any port — owner/session access. OK.
        conn_mode = "direct"
        conn_ok = True
    add("session_mode_connection", conn_ok,
        f"host_family={conn_mode} port={pgport or '?'}")

    # A build is *blocked* (not green) if any hard blocker fails. Soft risks
    # (long txn / locks / headroom) are surfaced as reasons but only headroom is
    # a hard stop, because a concurrent build is designed to tolerate writers;
    # a true ACCESS EXCLUSIVE holder is rare and the lock_timeout guards it.
    green = all(c["pass"] for c in checks if c["name"] in {
        "table_column_identity", "no_invalid_remnants", "no_in_progress_build",
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
    }


# ---------------------------------------------------------------------------
# Representative EXPLAIN evidence queries (always 'simple', is_deleted=false,
# snowflakes-as-text)
# ---------------------------------------------------------------------------

#: Representative query terms drawn from the frozen golden set (1.1 §10).
REPRESENTATIVE_QUERIES = (
    ("websearch_default", "WanVideoSampler"),
    ("phrase_exact_name", "controlnet settings"),
    ("websearch_dotted_name", "FLUX.1"),
)

# Always cast snowflakes to text and encode is_deleted=false — the exact shape the
# 1.7 candidate SQL will use. No message_feed/unified_feed change, no RPC built here.
EVIDENCE_QUERY_TEMPLATE = """
-- {label}: {comment}
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, SUMMARY ON)
SELECT m.message_id::text AS item_id,
       m.channel_id::text AS channel_id,
       ts_rank(to_tsvector('simple'::regconfig, coalesce(m.content, '')),
               {tsq}, 32) AS lexical_rank
  FROM {tbl} m
 WHERE m.is_deleted = false
   AND to_tsvector('simple'::regconfig, coalesce(m.content, '')) @@ {tsq}
 ORDER BY lexical_rank DESC, m.created_at DESC, m.message_id::text ASC
 LIMIT 20;
"""


def evidence_queries(index_name: str = INDEX_NAME) -> "list[tuple[str, str]]":
    """The saved EXPLAIN (ANALYZE, BUFFERS) plans proving the index is used.

    Every plan queries the underlying table with the EXACT indexed expression,
    encodes ``is_deleted = false`` (the 1.1 eligibility predicate), casts Discord
    snowflakes to text, ranks with ``ts_rank(..., 32)`` (frozen flag), and uses the
    frozen deterministic tie-break. Returns (label, sql) pairs.
    """
    out: list[tuple[str, str]] = []
    for label, term in REPRESENTATIVE_QUERIES:
        if label.startswith("phrase"):
            tsq = f"phraseto_tsquery({LEXICAL_CONFIG!r}::regconfig, '{term}')"
            comment = f"phraseto_tsquery('simple', {term!r})"
        else:
            tsq = f"websearch_to_tsquery({LEXICAL_CONFIG!r}::regconfig, '{term}')"
            comment = f"websearch_to_tsquery('simple', {term!r})"
        out.append((label, EVIDENCE_QUERY_TEMPLATE.format(
            label=label, comment=comment, tbl=fully_qualified_table(), tsq=tsq,
        ).strip()))
    return out


# A pre-1.3 baseline plan: the same query with NO simple index present shows the
# english index is unreachable (seq/bitmap scan). Rehearsal-only diagnostic.
def baseline_no_simple_index_query() -> str:
    term = REPRESENTATIVE_QUERIES[0][1]
    return f"""
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, SUMMARY ON)
SELECT m.message_id::text
  FROM {fully_qualified_table()} m
 WHERE m.is_deleted = false
   AND to_tsvector('simple'::regconfig, coalesce(m.content, '')) @@ websearch_to_tsquery('simple','{term}')
 LIMIT 20;
""".strip()


# ---------------------------------------------------------------------------
# EXPLAIN plan parsing — assert index usage against captured evidence
# ---------------------------------------------------------------------------

def parse_explain_plan(plan_text: str, index_name: str = INDEX_NAME) -> dict:
    """Classify a saved EXPLAIN plan: which FTS index (if any) it used.

    Looks for ``Index ... Scan on <index_name>`` / ``Bitmap Index Scan on
    <index_name>`` and the english index, and whether a Seq Scan appeared.
    Conservative: matches index names literally.
    """
    text = plan_text or ""
    simple_hit = re.search(r"(Index Scan|Bitmap Index Scan)[^\n]*\b" + re.escape(index_name) + r"\b", text)
    english_hit = re.search(r"(Index Scan|Bitmap Scan|Bitmap Index Scan)[^\n]*\b" + re.escape(ENGLISH_INDEX_NAME) + r"\b", text)
    # Node lines are prefixed with "->" in EXPLAIN; match the node name anywhere.
    seq = re.search(r"\bSeq Scan\b", text)
    bitmap_heap = re.search(r"\bBitmap Heap Scan\b", text)
    return {
        "uses_simple_index": bool(simple_hit),
        "uses_english_index": bool(english_hit),
        "is_seq_scan": bool(seq),
        "is_bitmap_heap_scan": bool(bitmap_heap),
        "plan_present": bool(text.strip()),
    }


# ---------------------------------------------------------------------------
# Production-shaped rehearsal schema + deterministic seed (isolated cluster)
# ---------------------------------------------------------------------------

def rehearsal_schema_sql() -> str:
    """A discord_messages table mirroring the live columns the index touches.

    Only the columns the build/evidence read are needed, but we keep the real
    shape (message_id bigint PK, content text nullable, is_deleted bool,
    channel_id/author_id/guild_id bigint, created_at) so size/plan realism holds.
    """
    return f"""
DROP TABLE IF EXISTS {fully_qualified_table()};
CREATE TABLE {fully_qualified_table()} (
  message_id   bigint PRIMARY KEY,
  channel_id   bigint NOT NULL,
  author_id    bigint NOT NULL,
  guild_id     bigint NOT NULL,
  content      text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  is_deleted   boolean NOT NULL DEFAULT false
);
""".strip()


# Realistic video-gen vocabulary sampled for filler content. Deterministic
# selection by row index keeps the rehearsal reproducible (no RNG).
_FILLER_WORDS = (
    "flux", "model", "lora", "video", "image", "sampler", "scheduler", "step",
    "cfg", "vae", "upscale", "latent", "prompt", "seed", "batch", "node", "render",
    "workflow", "checkpoint", "training", "comfyui", "frame", "motion", "denoise",
    "controlnet", "settings", "gpu", "memory", "queue", "error", "thanks", "link",
    "diffusion", "transformer", "attention", "embedding", "inference", "weights",
    "fp8", "bf16", "fp16", "tensor", "cuda", "pytorch", "diffusers", "pipeline",
    "refiner", "guidance", "noise", "timestep", "clip", "text", "encoder", "decode",
    "kohya", "dreambooth", "dataset", "epoch", "loss", "learning", "rate", "adamw",
    "ipadapter", "segment", "mask", "inpaint", "outpaint", "brush", "canvas",
    "generate", "parameter", "tweak", "default", "value", "range", "increase",
    "decrease", "lower", "higher", "fix", "crash", "update", "install", "custom",
    "safetensors", "ckpt", "gguf", "quantized", "unet", "diT", "mmdit", "wan",
    "hunyuan", "ltx", "cogvideo", "animate", "interpolate", "frame", "fps",
    "resolution", "aspect", "crop", "resize", "tile", "cascade", "stage",
)


def rehearsal_seed_sql(n_rows: int = 1_250_000) -> str:
    """Deterministic production-shaped seed for the isolated rehearsal cluster.

    One INSERT over ``generate_series``: content is shaped to the inventory
    distribution (p50≈46, mean≈77, p99≈660 chars) — ~2% NULL (attachments-only),
    short filler otherwise, a few long outliers, and a sparse scattering of
    golden needles drawn from the 1.1 exact_name/workflow_code families so
    evidence queries return real hits. ~0.56% of rows are soft-deleted (mirrors
    the live 6,987 / 1,251,991 ratio). Everything derives from the row index ``g``
    via a CASE whose needle residues never collide with the NULL residue (offset
    13 and 37 are not multiples of 50), so counts are exact and reproducible.

    Lexeme diversity is shaped to production: besides a ~100-word video-gen
    dictionary, each filler row carries two bounded pseudo-unique tokens
    (``md5(g % K)`` substrings) and a version string, so the distinct-lexeme count
    — and therefore the GIN index size — is in the production ballpark rather than
    the trivially-small index a tiny fixed vocabulary would produce.
    """
    vocab = ", ".join(f"('{w}')" for w in _FILLER_WORDS)
    n = len(_FILLER_WORDS)
    return f"""
WITH vocab(a) AS (SELECT array_agg(w) FROM (VALUES {vocab}) v(w))
INSERT INTO {fully_qualified_table()}
  (message_id, channel_id, author_id, guild_id, content, created_at, is_deleted)
SELECT g,
       (g % 900) + 100000,                       -- a few hundred channels
       (g % 7000) + 1000000,                     -- a few thousand authors
       3270000 + (g % 5),
       CASE
         WHEN g % 50    = 0  THEN NULL           -- ~2% attachments-only (null content)
         WHEN g % 1250  = 13 THEN 'WanVideoSampler'
         WHEN g % 1667  = 13 THEN 'controlnet settings'
         WHEN g % 2000  = 13 THEN 'FLUX.1'
         WHEN g % 5000  = 13 THEN 'IPAdapterFaceIDKolors'
         WHEN g % 8000  = 13 THEN 'lightx2v_I2V_14B.safetensors'
         WHEN g % 2000  = 37 THEN repeat('long render log line ' || g::text || ' ', 40)
         ELSE substring(
                (SELECT a FROM vocab)[(g % {n}) + 1]            || ' ' ||
                (SELECT a FROM vocab)[(g * 7  % {n}) + 1]       || ' ' ||
                substring(md5((g % 45000)::text), 1, 7)         || ' ' ||  -- pseudo-unique
                substring(md5((g % 30000)::text), 9, 7)         || ' ' ||  -- pseudo-unique
                'v' || (g % 12)::text || '.' || (g % 30)::text,
                1, 120)
       END,
       now() - (g % 100000) * interval '1 minute',
       (g % 180 = 0)                             -- ~0.56% soft-deleted (live ratio)
  FROM generate_series(1, {n_rows}) g;
""".strip()


def summarize() -> dict:
    """Compact machine-readable summary of the task-1.3 index identity."""
    return {
        "task": "1.3",
        "index_schema": INDEX_SCHEMA,
        "index_table": INDEX_TABLE,
        "index_column": INDEX_COLUMN,
        "lexical_config": LEXICAL_CONFIG,
        "index_name": INDEX_NAME,
        "index_expression": index_expression(),
        "english_index_retained": ENGLISH_INDEX_NAME,
        "build_statement": build_statement(),
        "rollback_statement": rollback_statement(),
    }
