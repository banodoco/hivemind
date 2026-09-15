"""Frozen lexical-search contract for Hivemind hybrid search (plan task 1.1).

This module is the **authoritative Phase-1 lexical reference contract**. It fixes
the canonical PostgreSQL text-search configuration, the exact weighted
``to_tsvector`` expressions, the query constructors, the identifier / query
normalizers, the eligibility predicates, the chunk-collapse rule, and the bounded
workflow-code lexical-document policy — everything plan tasks 1.2–1.10 must
implement against without reinterpretation.

It is intentionally **pure, offline, stdlib-only, and dependency-free**. It does
not connect to a database, does not call an embedding provider, and does not
create indexes or RPCs (those are tasks 1.2–1.9; this is the *contract*, not the
implementation). The behaviour pinned here was **confirmed against a real
isolated PostgreSQL 14 instance** (see
``docs/hybrid-search/phase1-lexical-contract.md`` § "Evidence"); the tokenization
facts in this module's docstrings are observed output, not memory.

Everything below is a frozen decision recorded in
``docs/hybrid-search/phase1-lexical-contract.{md,json}`` dated 2026-07-28.
Bumping any ``*_VERSION`` constant is a contract change that must re-derive the
affected lexical documents and representation hashes (plan AD-4); it is never a
silent edit.

References (read-only, not imported at runtime):
- Pumpernickel ``app/services/retrieval.py``: ``websearch_to_tsquery('simple')``
  + ``ts_rank(..., 32)`` + RRF K=60 (ported with attribution; no runtime dep).
- Hivemind workflow representation: ``executors/workflow_representation.py``
  (frozen 0.8 contract — precedence, delimiters, dedup, chunking, quarantine).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

# ---------------------------------------------------------------------------
# Frozen contract versions
# ---------------------------------------------------------------------------

#: The single canonical PostgreSQL text-search configuration for every entity and
#: representation. Frozen to ``'simple'``. See LEXICAL_CONFIG_RATIONALE below.
#: Bumping this re-derives every lexical document and every representation hash.
LEXICAL_CONFIG = "simple"

#: Bumped when the lexical-document weights, normalization, query-constructor
#: selection, or collapse rule changes. Part of the lexical-document freshness
#: check (mirrors the 0.8 representation-hash contract).
LEXICALIZATION_VERSION = 1

#: PostgreSQL ``ts_rank`` normalization flag (frozen to Pumpernickel's value).
#: 32 = the documented length-normalization flag: it divides the rank magnitude
#: by a function of the document length, so a symbol hit in a ~1.4M-char
#: workflow-Python document does not dominate shorter, more on-topic matches.
#: (Verified empirically: a long document scores lower at flag 32 than at 0 for
#: the same term frequency.) The exact bitmask semantics are PostgreSQL's; the
#: numeric constant and its dampening effect are what is frozen here.
TS_RANK_NORMALIZATION = 32

#: Reciprocal Rank Fusion constant (matches Pumpernickel; validated in 1.11).
RRF_K = 60

#: ts_rank default per-letter multipliers (PostgreSQL fixed defaults). Documented
#: so the weight map below is interpretable: A=1.0, B=0.4, C=0.2, D=0.1.
TS_RANK_WEIGHT_MULTIPLIERS = {"A": 1.0, "B": 0.4, "C": 0.2, "D": 0.1}

#: tsvector weight letters in descending rank influence.
WEIGHTS = ("A", "B", "C", "D")

# ---------------------------------------------------------------------------
# Entity / representation vocabulary (mirrors 0.8 + plan AD-2)
# ---------------------------------------------------------------------------

ENTITY_MESSAGE = "message"
ENTITY_RESOURCE = "resource"
ENTITY_DISTILLATION = "distillation"
ENTITY_TYPES = (ENTITY_MESSAGE, ENTITY_RESOURCE, ENTITY_DISTILLATION)

REP_PROSE = "prose"
REP_WORKFLOW_PYTHON = "workflow_python"
REPRESENTATION_TYPES = (REP_PROSE, REP_WORKFLOW_PYTHON)

#: Concrete public resource kinds (plan AD-1). ``workflow`` is a resource whose
#: lexical document has both a prose and a workflow_python representation.
RESOURCE_KIND_WORKFLOW = "workflow"

# ---------------------------------------------------------------------------
# The canonical regconfig decision (frozen)
# ---------------------------------------------------------------------------

LEXICAL_CONFIG_RATIONALE = """
The live Hivemind Discord index is
``idx_discord_messages_content_fts = USING gin (to_tsvector('english'::regconfig, content))``
(task 0.3 inventory; 85 MB). Pumpernickel uses ``'simple'``. A query only benefits
from an expression index when its configuration AND expression match the indexed
one exactly.

Observed (isolated PostgreSQL 14, 2026-07-28):
  to_tsvector('english','WanVideoSampler') @@ websearch_to_tsquery('simple','WanVideoSampler')  -> FALSE
  to_tsvector('english','WanVideoSampler') @@ websearch_to_tsquery('english','WanVideoSampler') -> TRUE
  to_tsvector('simple','WanVideoSampler')  @@ websearch_to_tsquery('simple','WanVideoSampler')  -> TRUE
  EXPLAIN of a ``simple`` query: Bitmap Index Scan on the *simple* index only; it
  CANNOT use the ``english`` expression index (expression-index matching requires
  an identical expression).

Stemming/stopword differences observed on the same instance:
  to_tsvector('simple','WanVideoSampler')  -> 'wanvideosampler'
  to_tsvector('english','WanVideoSampler') -> 'wanvideosampl'   <- the snowball stemmer strips '-er'
  to_tsvector('english','... running configs ...') -> 'run','config' (stemmed); stopwords dropped
  to_tsvector('simple','... running configs ...')  -> 'running','configs' (literal); kept

Decision: adopt ``'simple'`` for EVERY entity and representation — messages,
resource title/prose, workflow prose, workflow Python, distillation
question/conditions/answer — and for EVERY query constructor. Uniform config
guarantees indexed-vector / query-vector agreement across all sources, so a
``websearch_to_tsquery('simple', q)`` can never mismatch an indexed expression.

This drops the only ``english`` advantage (morphological stemming for paraphrase
queries), which the embedding layer (Phase 2) closes — the lexical arm is the
exact/keyword arm, and exact identifier recall is a blocking gate. ``simple`` is
also strictly better for the global/multilingual Discord corpus: it never mangles
community terms or CJK text and removes no tokens.

Consequence for the live index: the existing ``english`` index on
``discord_messages`` CANNOT serve the canonical ``simple`` query, so task 1.3
builds a NEW ``simple`` index. Whether the superseded ``english`` index is dropped
(85 MB reclamation) is an additive 1.3 storage decision; it is never required by
the canonical query path.
""".strip()

# ---------------------------------------------------------------------------
# Weighted tsvector expressions (frozen per representation)
# ---------------------------------------------------------------------------
#
# Weight map (PostgreSQL default ts_rank multipliers: A=1.0 B=0.4 C=0.2 D=0.1):
#   A (high)   — title / question
#   B (medium) — tags + projected workflow_semantics (aliases, nodes, models,
#                custom_nodes, conditions for distillations)
#   C (normal) — prose body / answer
#   D (lowest) — workflow Python code chunks (uniform; weight only matters across
#                representations, and chunks collapse to one best per resource)
#
# These are the EXACT expressions task 1.2 implements. All are IMMUTABLE-suitable
# (fixed regconfig literal; IMMUTABLE helper functions reading only row columns).


@dataclass(frozen=True)
class TsvArm:
    """One setweight arm: a field label, its source-expression text, and its weight."""

    field: str          # logical name (title / tags / body / python_chunk / ...)
    source: str         # the SQL text feeding to_tsvector (see *_SOURCE constants)
    weight: str         # 'A' | 'B' | 'C' | 'D'


@dataclass(frozen=True)
class TsvSpec:
    """The full weighted to_tsvector recipe for one representation."""

    entity_type: str
    representation_type: str
    config: str                 # always LEXICAL_CONFIG
    arms: tuple[TsvArm, ...]
    chunked: bool               # True => one document per (item, rep, chunk_index)
    identity: tuple[str, ...]   # the lexical-document identity columns

    def expression(self) -> str:
        """Render the exact weighted to_tsvector SQL expression.

        ``{C}`` is the frozen regconfig; ``{S}`` each arm's source. A null source
        is coalesced to '' so the arm contributes nothing rather than nulling the
        whole vector. Concatenation is tsvector ``||``.
        """
        if not self.arms:
            # Single bare field, unweighted (e.g. message content).
            raise ValueError("use BARE_FIELD specs for single-arm representations")
        parts = []
        for arm in self.arms:
            parts.append(
                f"setweight(to_tsvector({self.config!r}::regconfig, "
                f"coalesce({arm.source}, '')), '{arm.weight}')"
            )
        return " || ".join(parts)


# Source-expression fragments (frozen). Task 1.2 implements the IMMUTABLE SQL
# functions referenced here (hivemind_resource_tags, hivemind_workflow_prose,
# hivemind_workflow_semantics_text) to mirror workflow_representation.py exactly.
SOURCE_MESSAGE_CONTENT = "content"
SOURCE_RESOURCE_TITLE = "title"
SOURCE_RESOURCE_TAGS = "hivemind_resource_tags(metadata)"
SOURCE_RESOURCE_PROSE = "hivemind_workflow_prose(body, kind)"   # strips python blocks when kind=workflow
SOURCE_RESOURCE_SEMANTICS = "hivemind_workflow_semantics_text(metadata)"  # incl. searchable_aliases
SOURCE_DISTILLATION_QUESTION = "question"
SOURCE_DISTILLATION_CONDITIONS = "conditions"
SOURCE_DISTILLATION_ANSWER = "answer"
SOURCE_WORKFLOW_PYTHON_CHUNK = "chunk_text"   # column of the per-chunk document table


#: Message / prose — single bare field, no weighting (messages are short; no
#: title). Stored as a bare to_tsvector, NOT a TsvSpec (no arms).
MESSAGE_BARE_SOURCE = SOURCE_MESSAGE_CONTENT

#: Distillation / prose — weighted, single document (max answer ~562 chars).
DISTILLATION_SPEC = TsvSpec(
    entity_type=ENTITY_DISTILLATION,
    representation_type=REP_PROSE,
    config=LEXICAL_CONFIG,
    arms=(
        TsvArm("question", SOURCE_DISTILLATION_QUESTION, "A"),
        TsvArm("conditions", SOURCE_DISTILLATION_CONDITIONS, "B"),
        TsvArm("answer", SOURCE_DISTILLATION_ANSWER, "C"),
    ),
    chunked=False,
    identity=("entity_type", "item_id", "representation_type", "chunk_index"),
)

#: Resource / prose — weighted, single document for normal-length prose.
#: Over-long prose (rare: the both-cohort Python block is stripped first) falls
#: back to chunked documents via the resource prose chunker (0.8 prose_512).
RESOURCE_PROSE_SPEC = TsvSpec(
    entity_type=ENTITY_RESOURCE,
    representation_type=REP_PROSE,
    config=LEXICAL_CONFIG,
    arms=(
        TsvArm("title", SOURCE_RESOURCE_TITLE, "A"),
        TsvArm("tags_semantics", f"{SOURCE_RESOURCE_TAGS} || ' ' || {SOURCE_RESOURCE_SEMANTICS}", "B"),
        TsvArm("body", SOURCE_RESOURCE_PROSE, "C"),
    ),
    chunked=False,   # True only for the over-long fallback path
    identity=("entity_type", "item_id", "representation_type", "chunk_index"),
)

#: Resource / workflow_python — one document PER CODE CHUNK (uniform weight C).
#: Always chunked; workflow Python is up to ~1.4M chars (~354K tokens, ~76 chunks
#: at 512 tokens). Quarantined Python is never present here (0.8 §7).
WORKFLOW_PYTHON_SPEC = TsvSpec(
    entity_type=ENTITY_RESOURCE,
    representation_type=REP_WORKFLOW_PYTHON,
    config=LEXICAL_CONFIG,
    arms=(TsvArm("python_chunk", SOURCE_WORKFLOW_PYTHON_CHUNK, "C"),),
    chunked=True,
    identity=("entity_type", "item_id", "representation_type", "chunk_index"),
)

#: Index of all weighted specs (messages handled separately as a bare field).
WEIGHTED_SPECS = {
    (ENTITY_DISTILLATION, REP_PROSE): DISTILLATION_SPEC,
    (ENTITY_RESOURCE, REP_PROSE): RESOURCE_PROSE_SPEC,
    (ENTITY_RESOURCE, REP_WORKFLOW_PYTHON): WORKFLOW_PYTHON_SPEC,
}

# ---------------------------------------------------------------------------
# Query constructors (frozen)
# ---------------------------------------------------------------------------

#: The default query constructor for every user query (plan "Query parsing").
#: ``websearch_to_tsquery`` supports multiple terms (implicit AND), quoted
#: phrases, ``-``exclusion, and ``OR`` without requiring callers to build
#: tsquery syntax, and is forgiving of bad input. Always passes the explicit
#: frozen config; NEVER relies on the database default.
QUERY_CONSTRUCTOR_DEFAULT = "websearch_to_tsquery"
#: Phrase constructor for exact multi-token names that tokenize safely under
#: ``simple`` (e.g. "LTX-Video" -> 'ltx' <-> 'video' adjacency). Plan "Exact".
QUERY_CONSTRUCTOR_PHRASE = "phraseto_tsquery"
#: ``plainto_tsquery`` is NOT used (it lacks phrase/exclusion support and is
#: stricter); retained here only as a documented non-default for completeness.
QUERY_CONSTRUCTOR_PLAIN = "plainto_tsquery"

#: Websearch operator characters that, if present, mean the user is already
#: directing the parser and the phrase arm should not also fire on the whole text.
WEBSEARCH_OPERATOR_CHARS = set('"-|()')


def _quoted_phrase(query: str) -> str | None:
    """Return the inside of a whole-query ``"..."`` quoted phrase, else None."""
    q = query.strip()
    if len(q) >= 2 and q.startswith('"') and q.endswith('"'):
        return q[1:-1]
    return None


def query_arms(query: str) -> tuple[dict, ...]:
    """The parallel FTS query arms to run for ``query`` (plan 1.7 multi-arm model).

    The lexical candidate SQL does NOT pick one constructor exclusively; it runs
    the relevant arms in parallel and merges their (de-duplicated, collapsed)
    candidates. Each returned arm names its constructor and the normalized text to
    pass it (always with the frozen ``'simple'`` config).

    Arms:
      - ``fts``    : ``websearch_to_tsquery`` on the normalized query (default;
                     AND-of-terms, honors user-typed phrase/exclusion/OR).
      - ``phrase`` : ``phraseto_tsquery`` when the query is a single exact name
                     or a whole quoted phrase — tight adjacency for exact names.
      - ``ident``  : the normalized exact-identifier form for the side index +
                     trigram arm (see :func:`normalize_identifier`); a string,
                     not a tsquery.

    ``ident`` is ALWAYS produced (the exact-identifier arm runs for every query);
    ``fts`` runs whenever the query has content; ``phrase`` runs only for the
    single-name / quoted-phrase shapes above.
    """
    qn = normalize_query(query)
    arms: list[dict] = []
    if qn:
        arms.append({"arm": "fts", "constructor": QUERY_CONSTRUCTOR_DEFAULT, "text": qn})

    phrase_text: str | None = None
    quoted = _quoted_phrase(query)
    raw = qn
    is_single_name = bool(raw) and not any(c.isspace() for c in raw) and not (
        WEBSEARCH_OPERATOR_CHARS & set(raw)
    )
    if quoted is not None and quoted.strip():
        phrase_text = quoted.strip()
    elif is_single_name:
        # A bare single identifier or one-token name (no spaces, no operators)
        # — phrase it for exact-name adjacency.
        phrase_text = raw
    if phrase_text:
        arms.append({"arm": "phrase", "constructor": QUERY_CONSTRUCTOR_PHRASE, "text": phrase_text})

    ident = normalize_identifier(query)
    if ident:
        arms.append({"arm": "ident", "constructor": "normalized_identifier", "text": ident})
    return tuple(arms)


def query_constructor_choice(query: str) -> str:
    """Back-compat shim: the *primary* constructor for ``query``.

    Returns ``phraseto_tsquery`` for an exact-name / quoted-phrase shape and
    ``websearch_to_tsquery`` (the default) otherwise. Prefer :func:`query_arms`
    for the parallel-arm contract; this helper exists for compact decisions.
    """
    arms = query_arms(query)
    for arm in arms:
        if arm["constructor"] == QUERY_CONSTRUCTOR_PHRASE:
            return QUERY_CONSTRUCTOR_PHRASE
    return QUERY_CONSTRUCTOR_DEFAULT


# ---------------------------------------------------------------------------
# Normalization (frozen, IMMUTABLE-suitable)
# ---------------------------------------------------------------------------

#: Characters treated as identifier separators / punctuation, collapsed/removed
#: by :func:`normalize_identifier`. Frozen so a name and its punctuation variants
#: share one normalized form (e.g. "Wan 2.2" == "Wan2.2" == "wan_2.2").
_IDENTIFIER_SEPARATOR_RE = re.compile(r"[\s.\-_/\\,:;(){}\[\]@\"'`]+")


def normalize_query(query: str) -> str:
    """Stable query text for FTS query constructors.

    Unicode NFC + collapse all whitespace runs to single spaces + strip. Mirrors
    Pumpernickel's ``normalize_query_for_embedding`` (ported). This is applied to
    the text handed to ``websearch_to_tsquery``/``phraseto_tsquery``; the frozen
    config does the lowercasing and tokenization, so this never lowercases itself.
    """
    if query is None:
        return ""
    text = unicodedata.normalize("NFC", str(query))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_identifier(value: str) -> str:
    """Compact normalized form for the exact-identifier arm.

    NFC, lowercase, strip, then remove every separator/punctuation character in
    :data:`_IDENTIFIER_SEPARATOR_RE`, keeping alphanumerics only. Deterministic
    and reversible-per-class: punctuation/spelling variants collapse to one key.

    Observed need (isolated PG14): ``to_tsvector('simple','Wan 2.2')`` ->
    ``'2.2' 'wan'`` but ``websearch_to_tsquery('simple','Wan2.2')`` ->
    ``'wan2.2'``, and the two do NOT match (``@@ -> false``). Normalizing both to
    ``wan22`` makes the exact-identifier arm bridge the spelling_variant pair.

    This is the IMMUTABLE contract task 1.4 implements as
    ``public.hivemind_normalize_identifier(text)`` for use in expression indexes
    on resource titles and distillation questions. It is applied identically to
    the indexed value and the query term.
    """
    if value is None:
        return ""
    text = unicodedata.normalize("NFC", str(value)).lower().strip()
    text = _IDENTIFIER_SEPARATOR_RE.sub("", text)
    return text


def identifier_aliases(name: str) -> tuple[str, ...]:
    """Extra normalized forms to index for a name, to bridge variants.

    Returns the compact form (:func:`normalize_identifier`) plus the
    whitespace-collapsed lower form. Both are stored on the identifier side index
    so ``Wan 2.2``, ``Wan2.2``, ``wan2.2`` and ``WAN 2.2`` all resolve to the same
    rows via the normalized column AND a trigram-similarity path.
    """
    compact = normalize_identifier(name)
    spaced = re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(name)).lower().strip())
    forms = []
    for form in (compact, spaced):
        if form and form not in forms:
            forms.append(form)
    return tuple(forms)


# ---------------------------------------------------------------------------
# Eligibility predicates (frozen — service-role RLS bypass requires encoding)
# ---------------------------------------------------------------------------

#: Message soft-delete predicate. Task 0.2 found ``message_feed`` does NOT filter
#: ``is_deleted`` (6,987 deleted messages are currently searchable); the lexical
#: RPC encodes it itself because the service role bypasses RLS.
MESSAGE_ELIGIBLE = "m.is_deleted = false"

#: Message author opt-out predicate (DEFAULT-OFF, flag-bound). Task 0.2 found
#: ``members.allow_content_sharing`` (4/7,672 = false) exists but is enforced
#: NOWHERE in the live read path, and README/SKILL.md state opt-out is an
#: export-time (HF dataset) concept only. To "preserve all current eligibility",
#: this predicate is bound to an implementation-safe flag (``hivemind.author_optout_enabled``,
#: default ``false``): at launch the search behavior is unchanged (no opt-out),
#: and it can be enabled later without a schema change. When enabled it joins
#: ``members`` and excludes ``allow_content_sharing = false`` authors.
MESSAGE_AUTHOR_OPTOUT = (
    "(NOT :author_optout_enabled OR EXISTS "
    "(SELECT 1 FROM public.members mb WHERE mb.member_id = m.author_id "
    "AND COALESCE(mb.allow_content_sharing, true) = true))"
)

#: Bot / system author predicate (DEFAULT-OFF, same flag family). Task 0.2 found
#: 19 bot + 2 system members; the live path does not exclude them and they carry
#: real workflow discussion. Default: include (preserve behavior). Same
#: off-by-default flag can exclude them later.
MESSAGE_BOT_POLICY = (
    "(NOT :bots_excluded OR NOT EXISTS "
    "(SELECT 1 FROM public.members mb WHERE mb.member_id = m.author_id "
    "AND COALESCE(mb.bot, false) OR COALESCE(mb.system, false)))"
)

#: Distillation visibility predicate. RLS is ``status <> 'rejected`` and the
#: unified_feed branch is ``status IN ('pending','approved')`` (net identical for
#: the feed). Service-role bypass means the RPC re-encodes this.
DISTILLATION_ELIGIBLE = "d.status IN ('pending', 'approved')"

#: Resource base predicate. Resources have no status/soft-delete column (0.2 §5);
#: all rows are eligible for the prose representation.
RESOURCE_ELIGIBLE = "true"

#: Workflow-Python representation gate. Only ``kind = 'workflow'`` resources carry
#: a workflow_python representation, and ONLY when the authoritative Python is
#: resolved to cohort payload_python / body_python / recoverable AND public state
#: is ``safe`` (0.8 §7). Quarantined Python never produces a lexical document.
WORKFLOW_PYTHON_ELIGIBLE = (
    "r.kind = 'workflow' AND hivemind_workflow_python_state(r.id) = 'safe'"
)


@dataclass(frozen=True)
class Eligibility:
    """The eligibility predicate set for one entity's lexical candidate query."""

    entity_type: str
    base_table: str          # fully-qualified source relation
    identity_column: str     # the item_id source column, cast to text
    predicates: tuple[str, ...]


MESSAGE_ELIGIBILITY = Eligibility(
    entity_type=ENTITY_MESSAGE,
    base_table="public.discord_messages",
    identity_column="message_id::text",
    predicates=(MESSAGE_ELIGIBLE, MESSAGE_AUTHOR_OPTOUT, MESSAGE_BOT_POLICY),
)

DISTILLATION_ELIGIBILITY = Eligibility(
    entity_type=ENTITY_DISTILLATION,
    base_table="public.distillations",
    identity_column="id::text",
    predicates=(DISTILLATION_ELIGIBLE,),
)

RESOURCE_PROSE_ELIGIBILITY = Eligibility(
    entity_type=ENTITY_RESOURCE,
    base_table="public.external_resources",
    identity_column="id::text",
    predicates=(RESOURCE_ELIGIBLE,),
)

WORKFLOW_PYTHON_ELIGIBILITY = Eligibility(
    entity_type=ENTITY_RESOURCE,
    base_table="public.external_resources",
    identity_column="id::text",
    predicates=(WORKFLOW_PYTHON_ELIGIBLE,),
)

# ---------------------------------------------------------------------------
# Bounded workflow-code lexical-document policy (frozen, keyed by chunk identity)
# ---------------------------------------------------------------------------

#: Document identity for every lexical document (frozen key).
#: (entity_type, item_id, representation_type, chunk_index). Matches the 0.8
#: embedding identity shape so a chunk in the lexical index and the embedding
#: table share an addressable, collapsible identity.
LEXICAL_DOCUMENT_IDENTITY = (
    "entity_type", "item_id", "representation_type", "chunk_index"
)

#: Pilot code-chunk configuration (frozen; mirrors 0.8 CHUNK_CONFIGS python_512).
#: The production chunk configuration is chosen in task 2.14; this is the pilot.
WORKFLOW_PYTHON_CHUNK_TARGET_TOKENS = 512
WORKFLOW_PYTHON_CHUNK_OVERLAP_TOKENS = 50

#: Pilot prose-chunk configuration for the over-long-prose fallback path.
RESOURCE_PROSE_CHUNK_TARGET_TOKENS = 512
RESOURCE_PROSE_CHUNK_OVERLAP_TOKENS = 50

#: Prose documents above this normalized length are chunked instead of stored as
#: one vector. Generous (rarely hit because the both-cohort Python block is
#: stripped from prose first); chosen so a single tsvector never approaches a
#: noisy size while NEVER silently truncating (0.8 coverage_ok guard).
PROSE_SINGLE_DOC_MAX_CHARS = 64_000   # ~16K tokens; the largest stripped prose is far under this

#: Snippet anchor window (mirrors 0.8 redact_snippet).
SNIPPET_WINDOW_CHARS = 240

#: No-silent-truncation contract: a workflow-Python representation MUST be fully
#: covered by its chunk set (0.8 ``coverage_ok``), and every chunk is indexed as
#: its own lexical document. No Python is ever head/tail-truncated to fit a single
#: vector. Over-limit documents are split, never dropped.


@dataclass(frozen=True)
class ChunkCollapseRule:
    """How chunked lexical matches collapse to one ranked identity (frozen)."""

    # 1. Within one representation_type, keep the single best-scoring chunk per item.
    within_representation: str = "best ts_rank per (entity_type, item_id, representation_type)"
    # 2. Across representation_types for the same item, keep the best representation
    #    (prose vs workflow_python) and carry its (representation_type, chunk_index,
    #    matched_snippet) onto the identity.
    across_representations: str = "best ts_rank per (entity_type, item_id); carry matched_representation/snippet"
    # 3. The item then enters global ranking exactly once.
    one_identity: str = "exactly one ranked row per (entity_type, item_id)"


CHUNK_COLLAPSE_RULE = ChunkCollapseRule()

# ---------------------------------------------------------------------------
# Ranking inputs (frozen)
# ---------------------------------------------------------------------------

#: Lexical rank expression. Weighted tsvector + frozen tsquery + normalization 32.
LEXICAL_RANK_SQL = "ts_rank(tsv, tsq, {0})".format(TS_RANK_NORMALIZATION)

#: Deterministic global tie-break (plan "Query parsing"). Within equal lexical
#: rank, prefer newer, then stable identity. item_id is text (snowflake-safe).
TIE_BREAK = (
    "ORDER BY lexical_rank DESC NULLS LAST, "
    "created_at DESC NULLS LAST, "
    "entity_type ASC, item_id ASC"
)


# ---------------------------------------------------------------------------
# Snowflake-string boundary (frozen — plan invariant, 0.2 gap)
# ---------------------------------------------------------------------------

#: Every identity / filter crosses SQL and JSON boundaries as TEXT. Discord
#: snowflakes (message_id, channel_id, author_id, guild_id) are bigint in the DB
#: but are cast to text at the lexical candidate boundary:
SNOWFLAKE_TEXT_COLUMNS = (
    "discord_messages.message_id::text",
    "discord_messages.channel_id::text",
    "discord_messages.author_id::text",
    "discord_messages.guild_id::text",
)
#: item_ids arriving from the Edge function are validated JSON strings and bound
#: via an allow-listed identity predicate — NEVER interpolated SQL (plan 3.1/3.7).
ITEM_IDS_ARE_TEXT = True


def summarize() -> dict:
    """Return a compact dict of the frozen contract (for the validator / docs)."""
    return {
        "lexical_config": LEXICAL_CONFIG,
        "lexicalization_version": LEXICALIZATION_VERSION,
        "ts_rank_normalization": TS_RANK_NORMALIZATION,
        "rrf_k": RRF_K,
        "weight_multipliers": TS_RANK_WEIGHT_MULTIPLIERS,
        "query_constructors": {
            "default": QUERY_CONSTRUCTOR_DEFAULT,
            "phrase": QUERY_CONSTRUCTOR_PHRASE,
        },
        "specs": [
            {
                "entity_type": s.entity_type,
                "representation_type": s.representation_type,
                "config": s.config,
                "chunked": s.chunked,
                "arms": [{"field": a.field, "source": a.source, "weight": a.weight} for a in s.arms],
                "identity": list(s.identity),
                "expression": s.expression(),
            }
            for s in WEIGHTED_SPECS.values()
        ],
        "message_bare_source": MESSAGE_BARE_SOURCE,
        "chunk_collapse": {
            "identity": list(LEXICAL_DOCUMENT_IDENTITY),
            "within_representation": CHUNK_COLLAPSE_RULE.within_representation,
            "across_representations": CHUNK_COLLAPSE_RULE.across_representations,
            "one_identity": CHUNK_COLLAPSE_RULE.one_identity,
        },
        "workflow_python_chunk": {
            "target_tokens": WORKFLOW_PYTHON_CHUNK_TARGET_TOKENS,
            "overlap_tokens": WORKFLOW_PYTHON_CHUNK_OVERLAP_TOKENS,
        },
        "snippet_window_chars": SNIPPET_WINDOW_CHARS,
        "lexical_rank_sql": LEXICAL_RANK_SQL,
        "tie_break": TIE_BREAK,
    }
