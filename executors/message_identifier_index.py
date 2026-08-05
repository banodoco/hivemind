"""Frozen full-message exact-identifier path contract (plan task 1.6).

This module is the **authoritative Phase-1 reference contract** for task 1.6:
*select and implement the full-message exact-identifier path*. It records the
**evidence-based choice** (a normalized, length-bounded full-message trigram GIN
index — NOT a fanned-out identifier side index), the frozen index expression +
partial predicate + candidate-query contract the exact-identifier arm consumes
in task 1.7, and — as the decision record — the **measured-and-rejected
alternative** (a normalized identifier side index) so the choice is reproducible
from evidence rather than assumption.

It is intentionally **pure, offline, stdlib-only, and dependency-free**. It does
not connect to a database, does not call a provider, and does not create indexes
or RPCs (those are schema/007 + the live driver; the multi-arm candidate SQL is
task 1.7; the RPC is 1.9).

Everything below is a frozen decision. Bumping any ``*_VERSION`` constant is a
contract change that must re-derive the affected index/rows (plan AD-4); it is
never a silent edit.

THE MEASURED CHOICE (task-1.6 rehearsal, 2026-07-28, ~1.25M production-shaped rows)
-----------------------------------------------------------------------------------
Two designs were implemented and measured at production scale (see
docs/hybrid-search/phase1-message-identifier-index.md):

  A. CHOSEN — normalized full-message trigram GIN, length-bounded, partial on
     is_deleted=false. ~0.1 GB; GIN-served queries (sub-10ms class); bridges
     separator/case/Unicode variants (both sides normalized); AUTO-maintained by
     PostgreSQL (no trigger, no side table, no backfill); handles the >2047-char
     tokens FTS drops (task-1.3 finding 2) because trigram has no 2047 limit.
  B. REJECTED — normalized identifier side index (extract identifier runs into a
     side table + trigger + backfill). Measured ~1.5 GB (15x larger), seq-scan
     queries at p50≈2.7s, AND worse on spaced forms ("FLUX 1" splits to flux+1;
     a full-body normalize concatenates to flux1). Kept below as the rejected
     alternative + its frozen extraction grammar (parity-proven) so the decision
     is auditable.

The plan warned to "avoid an unbounded trigram index over 1.25M full message
bodies." The measurement resolves the uncertainty: Hivemind message bodies are
SHORT (0.3: mean≈77 chars, p99≈660), so the full-message trigram is BOUNDED
(~0.1 GB synthetic lower bound; the length-bounded partial predicate excludes
pathological megabyte bodies), not unbounded. The side index's per-identifier
fan-out (~8 rows/msg → ~10M rows + tuple + 3 indexes) is what is actually large.

Relationship to the frozen upstream contracts
---------------------------------------------
* :mod:`executors.identifier_normalization` (task 1.4) — re-exports the frozen
  ``normalize_identifier`` / ``normalize_identifier_preserve``; the CHOSEN index
  expression is ``hivemind_normalize_identifier(content)`` (IMMUTABLE, parity-
  proven in 1.4), so no new normalization is defined here.
* :mod:`executors.lexical_contract` (task 1.1) — ``is_deleted = false`` message
  eligibility (§8), snowflakes-as-text boundary (§9), and the exact-identifier
  arm spec (§4/§5: exact normalized equality **plus** a bounded trigram / ``<%``
  word-similarity path).
"""

from __future__ import annotations

import re
import unicodedata

# Single source of truth: the frozen task-1.4 normalization primitives.
from executors.identifier_normalization import (  # noqa: F401  (re-exported)
    normalize_identifier,
    normalize_identifier_preserve,
)

# ---------------------------------------------------------------------------
# Frozen contract versions
# ---------------------------------------------------------------------------

#: Bumped when the chosen index expression, partial predicate, length bound,
#: candidate-query contract, or (for the rejected alternative) the extraction
#: grammar changes. Part of every freshness check.
#:
#: v2: evidence-based pivot to the full-message trigram GIN.
#: v3: corrected the exact/variant bridge. The v2 candidate contract ranked an
#:     *exact-equality* predicate (``normalize(content) = normalize(:q)``) ahead of
#:     a permissive ``<%`` fallback. That equality compares the query to the ENTIRE
#:     normalized message, so it returns zero rows for an identifier embedded in
#:     prose (the normal case) — the EXPLAIN for ``WanVideoSampler`` proved it. v3
#:     makes the index-supported exact *normalized containment* predicate
#:     (``normalize(content) LIKE '%'||normalize(:q)||'%'``) the primary exact/
#:     variant bridge — it retrieves identifiers embedded in prose and preserves
#:     every separator-variant form via compact normalization on both sides — and
#:     demotes ``<%`` to an OPTIONAL bounded fallback for typo tolerance only. The
#:     index expression, partial predicate, and length bound are UNCHANGED.
MESSAGE_IDENTIFIER_INDEX_VERSION = 3

# ---------------------------------------------------------------------------
# THE CHOSEN DESIGN: normalized full-message trigram (length-bounded, partial)
# ---------------------------------------------------------------------------

CHOICE = "normalized_full_message_trigram_length_bounded"

#: The frozen chosen index expression (mirrors schema/007). The IMMUTABLE
#: compact normalizer (task 1.4) over the raw content. A GIN trigram index over
#: this expression bridges separator/case/Unicode variants on BOTH sides.
INDEX_EXPRESSION = "hivemind_normalize_identifier(content)"

#: The frozen GIN operator class for the chosen index.
INDEX_OPCLASS = "gin_trgm_ops"

#: Frozen index / candidate-query object identities (schema/007 mirrors these).
INDEX_NAME = "idx_discord_messages_identifier_trgm"
SOURCE_TABLE = "public.discord_messages"

#: The frozen partial predicate (eligibility + length bound), repeated verbatim
#: by the candidate query so the partial GIN index is usable. ``is_deleted=false``
#: makes eligibility structural (a soft-deleted message leaves the index
#: automatically; no trigger needed). The length bound excludes pathological
#: megabyte bodies so the index is BOUNDED by construction.
CONTENT_LENGTH_MIN = 1
CONTENT_LENGTH_MAX = 8000
PARTIAL_PREDICATE = (
    f"is_deleted = false AND char_length(content) BETWEEN {CONTENT_LENGTH_MIN} AND {CONTENT_LENGTH_MAX}"
)

#: Max normalized query length the candidate query will attempt (overlong query
#: -> skip the arm). Mirrors the task-1.5 ``MAX_QUERY_CHARS``.
MAX_QUERY_CHARS = 300

#: Frozen thresholds (mirror task-1.5 §"Exact query rules"). Word-similarity 0.3
#: is permissive enough for a short compact identifier needle against a longer
#: normalized body.
WORD_SIMILARITY_THRESHOLD = 0.3
SIMILARITY_THRESHOLD = 0.3

#: Candidate multiplier (plan AD-6): the arm returns up to this × the requested
#: global limit before the 1.7 merge collapses and limits. Capped to bound work.
CANDIDATE_MULTIPLIER = 5
CANDIDATE_LIMIT_CAP = 500

#: Frozen tie-break (mirrors task-1.1 §7 / task-1.5; snowflake-safe text id).
TIE_BREAK = "best_rank DESC NULLS LAST, created_at DESC NULLS LAST, message_id::text ASC"

#: The capacity gate (plan 0.7): new index storage must stay inside 12 GB.
STORAGE_GATE_GB = 12.0

CHOICE_RATIONALE = f"""
CHOSEN: a normalized full-message trigram GIN index, length-bounded, partial on
is_deleted=false:

    CREATE INDEX CONCURRENTLY {INDEX_NAME}
      ON {SOURCE_TABLE}
      USING gin ({INDEX_EXPRESSION} {INDEX_OPCLASS})
      WHERE {PARTIAL_PREDICATE};

Why this beat the side index (B) on the measured axes (1.25M rehearsal):
  * SIZE — ~0.1 GB vs the side index's ~1.5 GB (15x). Message bodies are short
    (0.3: mean≈77 chars), so a trigram over the body is compact; the side index
    fans out to ~8 rows/msg (~10M rows + tuple header + 3 indexes).
  * QUERY — GIN-served (sub-10ms class, like the task-1.3 FTS index). The side
    index's candidate query seq-scanned at p50≈2.7s in the rehearsal.
  * QUALITY — normalizing the WHOLE body concatenates spaced/dotted forms
    ("FLUX 1" -> flux1, "Wan 2.2" -> wan22) so a no-space query ("FLUX.1"->flux1,
    "Wan2.2"->wan22) matches. The side index splits on whitespace and loses that
    join ("FLUX 1" -> flux + dropped-1 -> no flux1 row).
  * VARIANT bridging — both sides run through hivemind_normalize_identifier, so
    case/separator/Unicode variants collapse exactly (task-1.4 contract).
  * MAINTENANCE — PostgreSQL maintains the GIN index automatically on
    insert/update/delete; the partial predicate handles soft-delete (the row
    leaves the index when is_deleted flips true). NO trigger, NO side table, NO
    backfill, NO per-row cost on the external Discord-ingestion path (the side
    index added a ~3.25x write slowdown via its trigger).
  * LONG-TOKEN GAP — trigram has no 2047-char limit, so this arm ALSO recovers
    the very-long tokens the task-1.3 FTS index drops (finding 2).

The plan's "avoid an unbounded trigram index" concern is resolved by measurement
(the index is bounded at ~0.1 GB) AND by construction (the ``char_length(content)
BETWEEN {CONTENT_LENGTH_MIN} AND {CONTENT_LENGTH_MAX}`` partial predicate excludes
pathological megabyte bodies, so the index can never grow unbounded).
""".strip()

#: The frozen candidate query contract consumed by task 1.7.
CANDIDATE_QUERY_RATIONALE = f"""
The exact-identifier arm's PRIMARY exact/variant bridge is an index-supported
exact NORMALIZED CONTAINMENT predicate over the whole message body, with a
bounded optional ``<%`` fallback for typo tolerance only; one row per message;
frozen deterministic tie-break; eligibility encoded; snowflake-safe text id:

  WITH q AS (SELECT public.hivemind_normalize_identifier(:q) AS k)
  SELECT m.message_id::text AS item_id,
         CASE WHEN public.hivemind_normalize_identifier(m.content) = q.k THEN 1.0
              ELSE 0.9 END AS lexical_rank,
         (public.hivemind_normalize_identifier(m.content) = q.k) AS exact
    FROM q
    JOIN {SOURCE_TABLE} m
      ON m.is_deleted = false
     AND char_length(m.content) BETWEEN {CONTENT_LENGTH_MIN} AND {CONTENT_LENGTH_MAX}
     AND public.hivemind_normalize_identifier(m.content) LIKE '%' || q.k || '%'
   ORDER BY exact DESC,
            m.created_at DESC NULLS LAST, m.message_id::text ASC
   LIMIT :candidate_limit;

Why containment, not equality (the v3 correction): the v2 arm ranked an
``=`` predicate (``normalize(content) = normalize(:q)``) ahead of a permissive
``<%`` fallback. Equality compares the query to the ENTIRE normalized message,
so for an identifier embedded in prose ("I am using WanVideoSampler ...") the
normalized body is the whole message and equality returns ZERO rows — the
rehearsal EXPLAIN for ``WanVideoSampler`` proved it (rows=0). The slow ``<%``
fallback was then doing all the work, scoring 5-15% of the corpus for compound
identifiers (``WanVideoSampler``≈125k, ``CogVideoX``≈189k rows on the rehearsal)
at 1.2-1.8 s warm.

Containment fixes both. The normalized query is a SUBSTRING of the normalized
whole body, so it retrieves identifiers embedded in prose AND — because compact
normalization (task 1.4) joins dotted/versioned/hyphenated/filename/Python-symbol/
keyword-argument/alias forms on BOTH sides — one predicate preserves every
required variant class (Recall@10 = 1.0 on the golden set, proven in the
rehearsal). The GIN trigram expression index serves ``LIKE '%' || k || '%'``
(proven by EXPLAIN on production-shaped data AND live read-only evidence: Bitmap
Index Scan, ``Index Cond: (...normalize...) ~~ '%needle%'``) because the
``'||'``-built pattern constant-folds to ``'%needle%'`` so pg_trgm extracts the
needle's trigrams. The candidate set is therefore bounded by the INDEX (only
messages that actually contain the identifier): ``WanVideoSampler`` containment
≈1.2k rows on the rehearsal vs 125k under ``<%``, so compound identifiers no
longer score 5-15% of the corpus. Ranking is deterministic and bounded —
whole-body exact (1.0) ahead of contained (0.9), then created_at desc,
message_id::text asc — with NO per-row word_similarity scoring on the primary
path.

The permissive ``<%`` (word-similarity) path is NOT in the primary SQL. It is an
OPTIONAL bounded fallback task 1.7 may layer for typo/near-miss tolerance only:
if used, raise the threshold (``word_similarity_threshold = {WORD_SIMILARITY_THRESHOLD}``
is the floor; prefer a length-scaled threshold for long needles), hard-cap its
candidate scan, and EXCLUDE rows already found by containment — so it never
scores 5-15% of the corpus for compound identifiers. The arm is SKIPPED when
normalize_identifier(:q) is empty or its length exceeds {MAX_QUERY_CHARS}.
candidate_limit = min({CANDIDATE_MULTIPLIER} * limit, {CANDIDATE_LIMIT_CAP}).
""".strip()


def candidate_limit(requested_limit: int) -> int:
    """The frozen per-arm candidate limit for a requested global limit."""
    if requested_limit <= 0:
        return 0
    return min(CANDIDATE_MULTIPLIER * requested_limit, CANDIDATE_LIMIT_CAP)


def normalize_query_key(query: str | None) -> str:
    """The compact key for a query (empty for empty/whitespace queries)."""
    return normalize_identifier("" if query is None else query)


def arm_should_fire(query: str | None) -> bool:
    """Whether the exact-identifier arm should run for a query (non-empty, bounded)."""
    key = normalize_query_key(query)
    if not key:
        return False
    return len(key) <= MAX_QUERY_CHARS


# ---------------------------------------------------------------------------
# REJECTED ALTERNATIVE B — normalized identifier side index (decision record)
# ---------------------------------------------------------------------------
# The grammar + extraction below are the FULLY-EVALUATED REJECTED ALTERNATIVE.
# They are parity-proven (Python <-> IMMUTABLE SQL) on the frozen fixture corpus
# (eval/retrieval/fixtures/message-identifier-v1.json) so the rejection is
# auditable: B was implemented, measured at production scale, and lost to A on
# size (15x), query latency (seq scan), quality (spaced forms), and operational
# cost (trigger write amplification). The SQL mirror lived in schema/007 v1; the
# chosen design (A) replaced it. These helpers remain as the frozen reference for
# the decision record and the rejected-alternative rehearsal/validator parity.

REJECTED_ALTERNATIVE = "normalized_identifier_side_index"

#: The ASCII "identifier-internal" run class for the rejected side index.
IDENTIFIER_RUN_CHARS = "A-Za-z0-9_.=-"
IDENTIFIER_RUN_RE = re.compile("[" + IDENTIFIER_RUN_CHARS + "]+")

MIN_TERM_CHARS = 3
MAX_TERM_CHARS = 100
MAX_TERMS_PER_MESSAGE = 256

TERM_MUST_CONTAIN_LETTER_RE = re.compile("[A-Za-z]")


def _eligible(compact: str) -> bool:
    if len(compact) < MIN_TERM_CHARS or len(compact) > MAX_TERM_CHARS:
        return False
    return TERM_MUST_CONTAIN_LETTER_RE.search(compact) is not None


def extract_message_identifiers(content: str | None) -> dict[str, str]:
    """REJECTED alternative B's extraction: {compact: preserve} per eligible run.

    Kept as the frozen decision-record reference (parity-proven vs the IMMUTABLE
    SQL function it implies). NFC-normalizes input first (the SQL mirror cannot
    NFC; the documented Python/SQL boundary inherited from task 1.4). NOT used by
    the chosen design (A normalizes the whole body in one GIN expression).
    """
    text = "" if content is None else unicodedata.normalize("NFC", str(content))
    out: dict[str, str] = {}
    for run in IDENTIFIER_RUN_RE.findall(text):
        compact = normalize_identifier(run)
        if not _eligible(compact):
            continue
        if compact not in out:
            out[compact] = normalize_identifier_preserve(run)
        if len(out) >= MAX_TERMS_PER_MESSAGE:
            break
    return out


def summarize_extraction(content: str | None) -> dict:
    rows = extract_message_identifiers(content)
    return {"n_distinct": len(rows), "compacts": sorted(rows.keys()),
            "capped": len(rows) >= MAX_TERMS_PER_MESSAGE}


def summarize() -> dict:
    return {
        "message_identifier_index_version": MESSAGE_IDENTIFIER_INDEX_VERSION,
        "choice": CHOICE,
        "index_name": INDEX_NAME,
        "index_expression": INDEX_EXPRESSION,
        "partial_predicate": PARTIAL_PREDICATE,
        "content_length_max": CONTENT_LENGTH_MAX,
        "max_query_chars": MAX_QUERY_CHARS,
        "word_similarity_threshold": WORD_SIMILARITY_THRESHOLD,
        "candidate_multiplier": CANDIDATE_MULTIPLIER,
        "candidate_limit_cap": CANDIDATE_LIMIT_CAP,
        "tie_break": TIE_BREAK,
        "rejected_alternative": REJECTED_ALTERNATIVE,
        "rejected_extract_example": summarize_extraction(
            "try Wan2.2 / FLUX.1 with force_clip_output=False and "
            "https://host/u/lightx2v_I2V_14B.safetensors (512px)"),
    }


__all__ = [
    "MESSAGE_IDENTIFIER_INDEX_VERSION",
    "CHOICE", "CHOICE_RATIONALE", "CANDIDATE_QUERY_RATIONALE",
    "INDEX_NAME", "INDEX_EXPRESSION", "INDEX_OPCLASS", "SOURCE_TABLE",
    "PARTIAL_PREDICATE", "CONTENT_LENGTH_MIN", "CONTENT_LENGTH_MAX",
    "MAX_QUERY_CHARS", "WORD_SIMILARITY_THRESHOLD", "SIMILARITY_THRESHOLD",
    "CANDIDATE_MULTIPLIER", "CANDIDATE_LIMIT_CAP", "TIE_BREAK", "STORAGE_GATE_GB",
    "candidate_limit", "normalize_query_key", "arm_should_fire",
    # rejected alternative B (decision record)
    "REJECTED_ALTERNATIVE", "IDENTIFIER_RUN_CHARS", "IDENTIFIER_RUN_RE",
    "MIN_TERM_CHARS", "MAX_TERM_CHARS", "MAX_TERMS_PER_MESSAGE",
    "extract_message_identifiers", "summarize_extraction",
    "summarize",
    "normalize_identifier", "normalize_identifier_preserve",
]
