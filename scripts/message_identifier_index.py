"""Pure, importable driver module for the full-message exact-identifier path (task 1.6).

Operational companion to ``executors.message_identifier_index`` (the frozen
contract) and ``schema/007_message_identifier_index.sql`` (the chosen migration).
Holds the frozen object identities, build/rollback SQL, read-only preflight query
set + verdict logic, evidence queries (EXPLAIN + hit counts), the candidate-query
contract SQL the task-1.7 arm consumes, and the rehearsal schema/seed helpers.

Pure and offline: importing it touches no database. The rehearsal harness
(``scripts/rehearse_message_identifier.py``) and the live driver
(``scripts/live_message_identifier.py``) import it and never re-derive these
identities locally, so the safety boundary never drifts.

Re-exports the frozen contract (single source of truth) from the reference module.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "schema" / "007_message_identifier_index.sql"
PREREQ_SCHEMA_PATH = REPO_ROOT / "schema" / "005_identifier_normalization.sql"

# The frozen contract (single source of truth). Re-exported for drivers/tests.
from executors.message_identifier_index import (  # noqa: F401,E402
    MESSAGE_IDENTIFIER_INDEX_VERSION,
    CHOICE,
    CHOICE_RATIONALE,
    CANDIDATE_QUERY_RATIONALE,
    INDEX_NAME,
    INDEX_EXPRESSION,
    INDEX_OPCLASS,
    SOURCE_TABLE,
    PARTIAL_PREDICATE,
    CONTENT_LENGTH_MIN,
    CONTENT_LENGTH_MAX,
    MAX_QUERY_CHARS,
    WORD_SIMILARITY_THRESHOLD,
    SIMILARITY_THRESHOLD,
    CANDIDATE_MULTIPLIER,
    CANDIDATE_LIMIT_CAP,
    TIE_BREAK,
    STORAGE_GATE_GB,
    candidate_limit,
    normalize_query_key,
    arm_should_fire,
    summarize,
    # rejected alternative B (decision record)
    REJECTED_ALTERNATIVE,
    extract_message_identifiers,
    summarize_extraction,
)

# ---------------------------------------------------------------------------
# Frozen object identities (cross-checked by tests against schema/007)
# ---------------------------------------------------------------------------

SCHEMA = "public"
#: The capacity gate (plan 0.7): new index storage must stay inside 12 GB.


def schema_sql_text() -> str:
    """The canonical migration text (read-only; never mutated)."""
    return SCHEMA_PATH.read_text()


def prereq_schema_sql_text() -> str:
    return PREREQ_SCHEMA_PATH.read_text()


def fully_qualified_index() -> str:
    return f"{SCHEMA}.{INDEX_NAME}"


# ---------------------------------------------------------------------------
# Build / rollback SQL (CIC, online, idempotent; bounded lock_timeout).
# ---------------------------------------------------------------------------

def build_statement(*, lock_timeout_s: int = 30, statement_timeout_s: int = 3600) -> str:
    """CREATE INDEX CONCURRENTLY the chosen normalized full-message trigram GIN.

    The migration (schema/007) already contains this CIC; this builder is the
    standalone form the live driver / rehearsal use with explicit timeouts. The
    expression + partial predicate are frozen and MUST match the candidate query.
    """
    return (
        f"SET lock_timeout = '{lock_timeout_s}s';\n"
        f"SET statement_timeout = '{statement_timeout_s}s';\n"
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME}\n"
        f"    ON {SOURCE_TABLE}\n"
        f"    USING gin ({INDEX_EXPRESSION} {INDEX_OPCLASS})\n"
        f"    WHERE {PARTIAL_PREDICATE};\n"
    )


def rollback_statement(*, concurrently: bool = True) -> str:
    """Online, idempotent rollback. Drops ONLY the additive index; no source row."""
    cc = "CONCURRENTLY" if concurrently else ""
    return f"DROP INDEX {cc} IF EXISTS {fully_qualified_index()};\n"


# ---------------------------------------------------------------------------
# Candidate query contract (consumed by task 1.7). Normalized containment over the body.
# ---------------------------------------------------------------------------

def candidate_query_sql(*, requested_limit: int = 20) -> str:
    """The frozen exact-identifier arm candidate query (task 1.7 consumes this).

    PRIMARY path: index-supported exact normalized CONTAINMENT — the normalized
    query is a SUBSTRING of the normalized whole message body. This retrieves an
    identifier EMBEDDED IN PROSE (the v2 whole-body equality path missed those:
    equality compares the query to the entire message, so embedded identifiers
    returned zero rows). Compact normalization joins dotted/versioned/hyphenated/
    filename/Python-symbol/keyword-argument/alias forms on BOTH sides, so one
    containment predicate preserves every required variant class. The GIN trigram
    expression index serves the LIKE (proven by EXPLAIN on production-shaped data
    + live read-only evidence: the ``'||'``-built pattern constant-folds to
    ``'%needle%'`` so pg_trgm extracts the needle's trigrams).

    Deterministic, bounded ranking: whole-body exact (rank 1.0) ahead of contained
    (rank 0.9), then created_at desc, message_id::text asc. NO per-row
    word_similarity scoring on the primary path — the candidate set is bounded by
    the index (only messages that actually contain the identifier), so compound
    identifiers no longer score 5-15% of the corpus. The permissive ``<%`` fuzzy
    path is an OPTIONAL bounded fallback for typo tolerance (see
    CANDIDATE_QUERY_RATIONALE), NOT in this primary SQL. Snowflake-safe text id;
    eligibility encoded. Returns ``message_id::text AS item_id`` + a
    ``lexical_rank`` + an ``exact`` flag.
    """
    lim = candidate_limit(requested_limit)
    return f"""
WITH q AS (SELECT public.hivemind_normalize_identifier(:q) AS k)
SELECT m.message_id::text AS item_id,
       CASE WHEN public.hivemind_normalize_identifier(m.content) = q.k THEN 1.0 ELSE 0.9 END AS lexical_rank,
       (public.hivemind_normalize_identifier(m.content) = q.k) AS exact
  FROM q
  JOIN {SOURCE_TABLE} m
    ON m.is_deleted = false
   AND char_length(m.content) BETWEEN {CONTENT_LENGTH_MIN} AND {CONTENT_LENGTH_MAX}
   AND public.hivemind_normalize_identifier(m.content) LIKE '%' || q.k || '%'
 ORDER BY exact DESC,
          m.created_at DESC NULLS LAST,
          m.message_id::text ASC
 LIMIT {lim};
"""


# ---------------------------------------------------------------------------
# Read-only preflight (live driver). Green/red verdict on operational gates.
# ---------------------------------------------------------------------------

def preflight_queries() -> list[tuple[str, str]]:
    return [
        ("source_table_shape", f"""
            SELECT a.attname, a.atttypid::regtype::text
              FROM pg_catalog.pg_attribute a
              JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
              JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname='public' AND c.relname='discord_messages'
               AND a.attname IN ('message_id','content','is_deleted','created_at')
               AND NOT a.attisdropped ORDER BY a.attname;
        """),
        ("prereq_005_present", f"""
            SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_proc p
              JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
              WHERE n.nspname='public' AND p.proname='hivemind_normalize_identifier' AND p.pronargs=1);
        """),
        ("target_index_state", f"""
            SELECT coalesce(i.indisvalid::text,'absent'),
                   coalesce(i.indisready::text,'absent'),
                   coalesce(pg_relation_size(c.oid)::text,'0')
              FROM pg_catalog.pg_class c
              LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
              LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.oid
             WHERE c.relname = '{INDEX_NAME}' AND n.nspname = '{SCHEMA}';
        """),
        ("est_rows", f"SELECT reltuples::bigint FROM pg_class WHERE relname='discord_messages';"),
        ("n_eligible", f"""
            SELECT count(*) FROM {SOURCE_TABLE}
             WHERE is_deleted=false AND content IS NOT NULL
               AND char_length(content) BETWEEN {CONTENT_LENGTH_MIN} AND {CONTENT_LENGTH_MAX};
        """),
        ("db_size_bytes", "SELECT pg_database_size(current_database());"),
        ("invalid_indexes", """
            SELECT c.relname FROM pg_index i
              JOIN pg_class c ON c.oid=i.indexrelid
              JOIN pg_class t ON t.oid=i.indrelid
             WHERE t.relname='discord_messages' AND NOT i.indisvalid;
        """),
        ("long_txns", """
            SELECT count(*) FROM pg_stat_activity
             WHERE state <> 'idle' AND xact_start IS NOT NULL
               AND now()-xact_start > interval '60 seconds';
        """),
        ("relation_locks", f"""
            SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid=l.relation
             WHERE c.relname='discord_messages' AND l.granted=false;
        """),
    ]


def evaluate_preflight(parsed: dict[str, list[list[str]]], pghost: str = "", pgport: str = "") -> dict:
    checks: list[dict] = []
    reasons: list[str] = []

    def add(name, ok, detail):
        checks.append({"name": name, "pass": bool(ok), "detail": detail})
        if not ok:
            reasons.append(f"{name}: {detail}")

    shape = {r[0]: r[1] for r in parsed.get("source_table_shape", []) if r}
    add("content_is_text", shape.get("content") == "text", f"content={shape.get('content')!r}")
    add("is_deleted_is_boolean", shape.get("is_deleted") == "boolean",
        f"is_deleted={shape.get('is_deleted')!r}")

    has005 = parsed.get("prereq_005_present", [["f"]])[0][0] in ("t", "true")
    add("schema_005_present", has005, "normalize fn live" if has005 else "schema/005 not applied")

    ti = parsed.get("target_index_state", [])
    already_valid = False
    if ti and ti[0] and ti[0][0] not in (None, "", "absent"):
        valid, ready = ti[0][0], ti[0][1] if len(ti[0]) > 1 else "?"
        already_valid = (valid in ("t", "true"))
        add("target_index_absent_or_valid", valid in ("t", "true") or ready in ("t", "true"),
            f"indisvalid={valid} indisready={ready}")
    else:
        add("target_index_absent_or_valid", True, "absent (fresh build)")

    est = parsed.get("est_rows", [["0"]])[0][0]
    n_elig = parsed.get("n_eligible", [["0"]])
    n_elig_val = int(n_elig[0][0]) if n_elig and str(n_elig[0][0]).lstrip("-").isdigit() else 0
    add("has_eligible_messages", n_elig_val > 0, f"eligible={n_elig_val:,} (est_total={est})")

    db_bytes = parsed.get("db_size_bytes", [["0"]])
    db_bytes_val = int(db_bytes[0][0]) if db_bytes and str(db_bytes[0][0]).isdigit() else 0
    # GIN trigram over short bodies: ~0.08 KB/eligible-msg conservative (1.25M -> ~0.1GB)
    est_index_bytes = int(n_elig_val * 0.08 * 1024)
    add("storage_gate", est_index_bytes < STORAGE_GATE_GB * 1e9,
        f"est_index≈{est_index_bytes/1e9:.2f}GB vs {STORAGE_GATE_GB}GB gate (db≈{db_bytes_val/1e9:.2f}GB)")

    invalid = len(parsed.get("invalid_indexes", []))
    add("no_invalid_index_remnants", invalid == 0, f"{invalid} invalid index(es)")

    long_tx = parsed.get("long_txns", [["0"]])
    long_tx_val = int(long_tx[0][0]) if long_tx and str(long_tx[0][0]).isdigit() else 0
    add("no_blocking_long_txns", long_tx_val == 0, f"{long_tx_val} long txn(s)")
    locks = parsed.get("relation_locks", [["0"]])
    locks_val = int(locks[0][0]) if locks and str(locks[0][0]).isdigit() else 0
    add("no_relation_locks", locks_val == 0, f"{locks_val} ungranted lock(s)")

    is_pooler_txn = pgport == "6543"
    add("session_mode_connection", not is_pooler_txn,
        f"host={pghost or '?'} port={pgport or '?'}"
        + (" (txn pooler 6543 BLOCKS CIC)" if is_pooler_txn else ""))

    green = all(c["pass"] for c in checks)
    return {"green": green, "checks": checks, "reasons": reasons,
            "est_rows": int(est) if str(est).isdigit() else 0,
            "n_eligible": n_elig_val, "est_index_bytes": est_index_bytes,
            "already_valid": already_valid}


# ---------------------------------------------------------------------------
# Evidence queries (read-only EXPLAIN + hit counts). Used by rehearsal + live.
# ---------------------------------------------------------------------------

def evidence_queries() -> list[tuple[str, str]]:
    """Representative containment queries; the rehearsal/live driver EXPLAIN these
    to PROVE the GIN trigram index serves the primary containment path (Bitmap
    Index Scan, no seq scan) and to expose the candidate cardinality. Includes the
    compound (``WanVideoSampler``) and common-word (``controlnet``) cases that were
    slow under the old ``<%`` arm — containment keeps them index-served and bounded.
    """
    def contain(label: str, needle: str) -> tuple[str, str]:
        # needle is a fixed safe literal (no quotes); the pattern constant-folds.
        return (label, f"""
            EXPLAIN (ANALYZE, BUFFERS)
            WITH q AS (SELECT public.hivemind_normalize_identifier('{needle}') AS k)
            SELECT m.message_id::text FROM {SOURCE_TABLE} m, q
             WHERE m.is_deleted=false
               AND char_length(m.content) BETWEEN {CONTENT_LENGTH_MIN} AND {CONTENT_LENGTH_MAX}
               AND public.hivemind_normalize_identifier(m.content) LIKE '%' || q.k || '%'
             ORDER BY m.created_at DESC NULLS LAST, m.message_id::text ASC
             LIMIT 20;
        """)
    return [
        contain("contain_flux1", "FLUX.1"),
        contain("contain_wanvideosampler", "WanVideoSampler"),   # compound (was 1.2s under <%)
        contain("contain_controlnet", "controlnet"),             # common-word containment
    ]


def parse_explain_plan(plan: str) -> dict:
    text = plan or ""
    low = text.lower()
    return {
        "uses_identifier_index": INDEX_NAME.lower() in low,
        "uses_index_scan": ("index scan" in low) or ("bitmap index scan" in low),
        "is_seq_scan": "seq scan" in low,
        "plan": text,
    }


# ---------------------------------------------------------------------------
# Rehearsal schema + seed (production-shaped ~1.25M rows).
# ---------------------------------------------------------------------------

def rehearsal_schema_sql() -> str:
    """Load schema/005 + pg_trgm + a discord_messages stub (mirrors the live shape)."""
    stub = f"""
    create extension if not exists pg_trgm;
    create table {SOURCE_TABLE} (
      message_id bigint primary key,
      content text,
      is_deleted boolean not null default false,
      created_at timestamptz not null default now(),
      channel_id bigint, author_id bigint, guild_id bigint
    );
    create index _dm_created_idx on {SOURCE_TABLE}(created_at);
    """
    return prereq_schema_sql_text() + "\n" + stub


# Cycled body templates. MOSTLY prose (realistic: most Discord messages are
# prose without a specific identifier); a minority carry one identifier so the
# corpus has realistic, SPARSE identifier density (~a few %, not 30%). The Recall
# probe plants explicit targets separately, so recall does not depend on density.
_REHEARSAL_TEMPLATES = [
    "the model is working well after the update lets try a render",
    "anyone got the new sampler settings for video thanks",
    "upscale step looks better than the default try it",
    "my vae is producing artifacts on dark frames anyone else",
    "the scheduler noise is too high for this checkpoint imo",
    "control net is great for keeping the character consistent",
    "what lora weight are people using for anime style",
    "try a lower cfg and more steps it cleaned up for me",
    "the prompt encoder is slow on my card any tips",
    "mask the region and inpaint only there it works",
    "motion is jittery reduce the frame count and retry",
    "reference video helps a lot with temporal consistency",
    "best distillation for upscaling video attention tuner patch speed",
    "qwen image edit vs the other editor compare them side by side",
    "looping sampler msr gave me decent results overnight",
    "audio workflows custom build share your json please",
    "the channel has been busy with new model releases lately",
    "checkpoint loaded fine fp8 is fine for inference on my rig",
    "encode decode latent then mask the face and rerun",
    "hicho said you do not have to use it for every frame",
]


def rehearsal_seed_sql(rows: int) -> str:
    """Generate ~rows production-shaped Discord messages, SET-BASED (fast).

    Bodies are cycled prose templates; identifiers are injected SPARSELY (~1 in 60
    messages carries one of the golden identifiers), mirroring realistic Discord
    identifier density (a specific model/node name is in a small fraction of
    messages, not 30%). ~0.56% soft-deleted tail, ~2% null content (0.3 shape), a
    ~1/2003 heavy tail (>8000 chars) excluded by the length-bounded partial
    predicate. Set-based so 1.25M rows seed in seconds.

    Realistic density matters for the query-latency measurement: the candidate-arm
    cost scales with the candidate COUNT for a given identifier, and a realistic
    identifier is in ~0.1-0.5% of messages (not the 30% a needle-dense seed would
    imply). The measured index SIZE is still a conservative lower bound (synthetic
    prose under-represents real lexeme diversity); the live measurement is
    authoritative for byte size.
    """
    n = int(rows)
    vals = ", ".join(f"('{t.replace(chr(39), chr(39)+chr(39))}')" for t in _REHEARSAL_TEMPLATES)
    ntmpl = len(_REHEARSAL_TEMPLATES)
    big = "repeat('x' || ' flux1 wan22 WanVideoSampler ', 360)"  # ~11000 chars -> excluded
    return f"""
    WITH tmpl(tn, body) AS (
        SELECT row_number() OVER (), body FROM (VALUES {vals}) AS x(body)
    ),
    seq AS (SELECT generate_series(1, {n}) AS g)
    INSERT INTO {SOURCE_TABLE}(message_id, content, is_deleted)
    SELECT g,
           CASE WHEN (g % 50) = 0 THEN NULL
                WHEN (g % 2003) = 0 THEN {big}
                ELSE (SELECT body FROM tmpl WHERE tn = ((g % {ntmpl}) + 1))
                  || CASE WHEN (g % 60) = 0 THEN
                       ' ' || (ARRAY['FLUX.1','Wan2.2','wan_2.2','WanVideoSampler',
                         'IPAdapterFaceIDKolors','ltx-2-19b-ic-lora-detailer',
                         'wan2.2_animate_14B','lightx2v_I2V_14B.safetensors',
                         'model.safetensors','.gguf','force_clip_output=False',
                         'Flux2Scheduler','BerniniConditioning','BlockifyMask',
                         'CogVideoX','VAEDecode','controlnet'])[1 + (g % 17)]
                     ELSE '' END
           END,
           (g % 178) = 0
      FROM seq;
    ANALYZE {SOURCE_TABLE};
    """


__all__ = [
    "SCHEMA", "SOURCE_TABLE", "INDEX_NAME", "INDEX_EXPRESSION", "INDEX_OPCLASS",
    "PARTIAL_PREDICATE", "CONTENT_LENGTH_MIN", "CONTENT_LENGTH_MAX",
    "STORAGE_GATE_GB", "SCHEMA_PATH", "PREREQ_SCHEMA_PATH",
    "schema_sql_text", "prereq_schema_sql_text", "fully_qualified_index",
    "build_statement", "rollback_statement", "candidate_query_sql",
    "preflight_queries", "evaluate_preflight", "evidence_queries", "parse_explain_plan",
    "rehearsal_schema_sql", "rehearsal_seed_sql",
    "MESSAGE_IDENTIFIER_INDEX_VERSION", "CHOICE", "CHOICE_RATIONALE",
    "CANDIDATE_QUERY_RATIONALE", "MAX_QUERY_CHARS", "WORD_SIMILARITY_THRESHOLD",
    "SIMILARITY_THRESHOLD", "CANDIDATE_MULTIPLIER", "CANDIDATE_LIMIT_CAP", "TIE_BREAK",
    "candidate_limit", "normalize_query_key", "arm_should_fire", "summarize",
    "REJECTED_ALTERNATIVE", "extract_message_identifiers", "summarize_extraction",
]
