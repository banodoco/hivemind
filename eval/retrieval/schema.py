"""Golden-set + corpus schema for the Hivemind retrieval evaluation harness.

Stdlib only (dataclasses + manual validation). This module is the single source
of truth for what a corpus row, a graded judgment, and a golden query look like.

Design notes (ported and adapted from Pumpernickel ``eval/retrieval/schema.py``):

* Item identity is ``(kind, item_id)`` with ``item_id`` **always a string**, so
  Discord snowflakes survive every JSON boundary exactly (see the plan, AD-1/AD-4
  and the schema-eligibility map's snowflake finding).
* ``kind`` uses the citation vocabulary ``message | resource | distillation``.
  ``workflow`` is accepted as a backwards-compatible alias of ``resource`` per
  AD-1; ``ItemId.key()`` normalises the alias so a workflow hit and a resource
  hit on the same id are the same item.
* Relevance is **graded** (``JudgedItem.grade``). Binary judgements are the
  special case ``grade == 1``. Graded judgements drive nDCG; the relevant set
  for Recall/MRR is ``grade >= 1``.
* Golden queries carry ``categories`` (a case may belong to several), structured
  ``filters`` (kinds/sources/item_ids/since/...), an ``expect_no_hit`` flag for
  expected no-result queries, and reviewer ``notes``.
* IDs are validated for safety (see :func:`validate_item_id`) and ``item_ids``
  filters require exactly one ``kinds`` value so a bare id is never ambiguous.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------

#: Entity-level kinds (embedding/citation identity). ``workflow`` aliases
#: ``resource`` on the public search surface (AD-1).
ENTITY_KINDS: frozenset[str] = frozenset({"message", "resource", "distillation"})

#: Concrete resource kinds that may appear as ``kind`` on workflow/long-resource
#: rows. These all normalise to entity kind ``resource``.
RESOURCE_KINDS: frozenset[str] = frozenset(
    {"article", "workflow", "transcript", "resource"}
)

#: Every ``kind`` value the harness accepts on a corpus row, result, or judgment.
ALLOWED_KINDS: frozenset[str] = ENTITY_KINDS | RESOURCE_KINDS

#: Search modes (AD-1). Only ``legacy`` is wired to a real adapter today; the
#: others are forward-compatible labels consumed by remote adapters (1.11/3.10).
MODES: frozenset[str] = frozenset({"legacy", "lexical", "semantic", "hybrid"})

#: Maximum item-id length. Bounds downstream filters; the live Edge function
#: also bounds ``item_ids`` arrays (AD-1 / Search API section).
MAX_ITEM_ID_LEN: int = 128


def normalize_kind(kind: str) -> str:
    """Return the entity kind for *kind*.

    ``workflow`` and the concrete resource kinds collapse to ``resource`` so the
    workflow/resource alias (AD-1) is transparent to identity matching. Raises
    :class:`ValueError` for an unknown kind.
    """
    if kind in ENTITY_KINDS:
        return kind
    if kind in RESOURCE_KINDS:
        return "resource"
    raise ValueError(f"unknown kind {kind!r}; expected one of {sorted(ALLOWED_KINDS)}")


# ---------------------------------------------------------------------------
# ID safety
# ---------------------------------------------------------------------------


class SchemaError(ValueError):
    """A corpus/golden-set document failed validation."""


def validate_item_id(item_id: Any, *, context: str = "item_id") -> str:
    """Validate and return a safe string item id.

    IDs travel as strings everywhere (snowflake-safe). We reject:

    * non-string / ``None`` values,
    * empty or whitespace-only ids,
    * ids longer than :data:`MAX_ITEM_ID_LEN`,
    * ids containing control/whitespace characters that could break a downstream
      filter, log line, or (in a future remote adapter) an allow-listed identity
      predicate. The harness never interpolates ids into SQL, but rejecting
      unsafe shapes here is defense-in-depth and matches the plan's
      "reject ambiguous or cross-kind bare ids" / bounded-allow-list rule.
    """
    if not isinstance(item_id, str):
        raise SchemaError(
            f"{context} must be a string, got {type(item_id).__name__}: {item_id!r}"
        )
    if not item_id or not item_id.strip():
        raise SchemaError(f"{context} must be a non-empty, non-blank string")
    if len(item_id) > MAX_ITEM_ID_LEN:
        raise SchemaError(
            f"{context} length {len(item_id)} exceeds {MAX_ITEM_ID_LEN} characters"
        )
    if any(ch.isspace() for ch in item_id):
        raise SchemaError(f"{context} must not contain whitespace: {item_id!r}")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in item_id):
        raise SchemaError(f"{context} must not contain control characters: {item_id!r}")
    return item_id


def validate_filters(filters: Any) -> dict[str, Any]:
    """Validate a golden-case filter block and return a normalised dict.

    Enforces the AD-1 rule: when ``item_ids`` is present, exactly one ``kinds``
    value is allowed so each id is interpreted inside one unambiguous namespace.
    All list fields are coerced to lists of validated string ids / kinds.
    """
    if filters is None:
        return {}
    if not isinstance(filters, dict):
        raise SchemaError(f"filters must be a mapping, got {type(filters).__name__}")
    out: dict[str, Any] = {}

    kinds = filters.get("kinds")
    if kinds is not None:
        out["kinds"] = _coerce_str_list(kinds, "kinds")
        for k in out["kinds"]:
            if k not in ALLOWED_KINDS:
                raise SchemaError(f"filter kind {k!r} is not in {sorted(ALLOWED_KINDS)}")

    sources = filters.get("sources")
    if sources is not None:
        out["sources"] = _coerce_str_list(sources, "sources")

    item_ids = filters.get("item_ids")
    if item_ids is not None:
        out["item_ids"] = [validate_item_id(x, context="item_ids[]") for x in _coerce_str_list(item_ids, "item_ids")]
        if not out["item_ids"]:
            raise SchemaError("item_ids, when present, must be non-empty")
        kinds_present = "kinds" in out
        if not kinds_present or len(out["kinds"]) != 1:
            raise SchemaError(
                "item_ids require exactly one kinds value so ids are unambiguous "
                "(AD-1); pass a single kind alongside item_ids"
            )

    for opt_list in ("channels", "authors"):
        val = filters.get(opt_list)
        if val is not None:
            out[opt_list] = _coerce_str_list(val, opt_list)

    since = filters.get("since")
    if since is not None:
        if not isinstance(since, str) or not since.strip():
            raise SchemaError("since must be a non-empty ISO-8601 string")
        out["since"] = since.strip()

    mode = filters.get("mode")
    if mode is not None:
        if mode not in MODES:
            raise SchemaError(f"mode {mode!r} is not in {sorted(MODES)}")
        out["mode"] = mode

    # Unknown keys are preserved (forward-compat) but flagged via a stored copy so
    # remote adapters can opt into them later without a schema change.
    extras = {k: v for k, v in filters.items() if k not in out}
    if extras:
        out["extra"] = extras
    return out


def _coerce_str_list(value: Any, name: str) -> list[str]:
    if isinstance(value, str):
        # Allow "a,b" CLI-style shorthand in hand-authored files.
        items = [p.strip() for p in value.split(",")]
        return [p for p in items if p]
    if not isinstance(value, list):
        raise SchemaError(f"{name} must be a list or comma-string, got {type(value).__name__}")
    out: list[str] = []
    for x in value:
        if isinstance(x, str) and x.strip():
            out.append(x.strip())
        elif isinstance(x, (int, float)) and not isinstance(x, bool):
            # Numeric ids are accepted on input and coerced to string (snowflake-safe).
            out.append(str(x))
        else:
            raise SchemaError(f"{name} entries must be non-empty strings, got {x!r}")
    return out


# ---------------------------------------------------------------------------
# Core identity + result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ItemId:
    """Stable item identity: ``(kind, item_id)``.

    ``kind`` is the *original* kind (may be ``workflow``); :meth:`entity_kind`
    normalises the workflow/resource alias for matching.
    """

    kind: str
    item_id: str

    def __post_init__(self) -> None:
        # Keep construction honest even when built directly.
        if self.kind not in ALLOWED_KINDS:
            raise SchemaError(f"unknown kind {self.kind!r}")
        validate_item_id(self.item_id, context="ItemId.item_id")

    def entity_kind(self) -> str:
        """Entity-level kind (``workflow`` → ``resource``)."""
        return normalize_kind(self.kind)

    def key(self) -> tuple[str, str]:
        """Match key: ``(entity_kind, item_id)``. Workflows == resources."""
        return (self.entity_kind(), self.item_id)


@dataclass(frozen=True)
class Result:
    """A single ranked result returned by an adapter.

    ``score`` and diagnostic fields are optional and additive; ranking is
    implied by list order (rank 1 = index 0).
    """

    kind: str
    item_id: str
    score: float | None = None
    matched_representation: str | None = None  # prose | workflow_python (2.14/3.3)
    #: Index of the chunk that won the best-chunk collapse (0 = first chunk).
    #: Populated by the 2.14 semantic retriever so a future raw-snapshot eval can
    #: compute an honest later-chunk hit rate. ``None`` when not applicable.
    matched_chunk_index: int | None = None
    matched_snippet: str | None = None

    def key(self) -> tuple[str, str]:
        return (normalize_kind(self.kind), self.item_id)


@dataclass
class Query:
    """A single evaluated query: the search text, a result cap, and filters.

    ``case_id`` and ``expected`` are **ground-truth context** populated by the
    runner. Real adapters (legacy/lexical/semantic/hybrid) ignore them; only
    fixture adapters (e.g. the perfect Oracle) consult them. Threading them
    here keeps the :class:`Retriever` protocol a single-argument ``retrieve``.
    """

    query: str
    limit: int = 20
    filters: dict[str, Any] = field(default_factory=dict)
    case_id: str | None = None
    expected: list["JudgedItem"] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Corpus (unified_feed shape)
# ---------------------------------------------------------------------------


@dataclass
class CorpusItem:
    """One searchable row, mirroring the public ``unified_feed`` shape.

    Fields: ``kind, source, item_id, title, body, author, context, url,
    metadata, created_at`` plus a ``status`` for distillations. This is the
    canonical public row shape (plan, "Existing systems / Hivemind").
    """

    kind: str
    source: str
    item_id: str
    title: str = ""
    body: str = ""
    author: str | None = None
    context: str | None = None
    url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None  # ISO-8601 string; used for deterministic order
    status: str | None = None  # distillations: pending|approved|rejected|superseded

    def __post_init__(self) -> None:
        if self.kind not in ALLOWED_KINDS:
            raise SchemaError(f"corpus item kind {self.kind!r} not in {sorted(ALLOWED_KINDS)}")
        validate_item_id(self.item_id, context="CorpusItem.item_id")
        self._id = ItemId(self.kind, self.item_id)

    @property
    def id(self) -> ItemId:
        return self._id

    def searchable_text(self) -> str:
        """Concatenated title + body — the surface the legacy ILIKE matches."""
        return f"{self.title}\n{self.body}".strip()

    def entity_kind(self) -> str:
        return self._id.entity_kind()

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "CorpusItem":
        kind = row.get("kind")
        item_id = row.get("item_id")
        if kind is None or item_id is None:
            raise SchemaError("corpus row requires 'kind' and 'item_id'")
        item_id = _coerce_id(item_id)
        md = row.get("metadata") or {}
        if not isinstance(md, dict):
            raise SchemaError("corpus 'metadata' must be a mapping if present")
        return cls(
            kind=str(kind),
            source=str(row.get("source", "")),
            item_id=item_id,
            title=str(row.get("title", "") or ""),
            body=str(row.get("body", "") or ""),
            author=_opt_str(row.get("author")),
            context=_opt_str(row.get("context")),
            url=_opt_str(row.get("url")),
            metadata=md,
            created_at=_opt_str(row.get("created_at")),
            status=_opt_str(row.get("status")),
        )


def _opt_str(v: Any) -> str | None:
    if v is None:
        return None
    return str(v)


def _coerce_id(v: Any) -> str:
    """Coerce a raw item id to a string (snowflake-safe).

    IDs must be strings at every boundary. Hand-authored files sometimes use
    bare JSON numbers; we coerce those here so a 19-digit snowflake is never
    rounded. Non-numeric junk still fails :func:`validate_item_id`.
    """
    if isinstance(v, bool):
        raise SchemaError(f"item id must not be a boolean: {v!r}")
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return v
    raise SchemaError(f"item id must be a string or int, got {type(v).__name__}: {v!r}")


# ---------------------------------------------------------------------------
# Golden set (graded judgments)
# ---------------------------------------------------------------------------


@dataclass
class JudgedItem:
    """A graded relevance judgment for one item on one query."""

    kind: str
    item_id: str
    grade: int
    note: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ALLOWED_KINDS:
            raise SchemaError(f"judged kind {self.kind!r} not in {sorted(ALLOWED_KINDS)}")
        validate_item_id(self.item_id, context="JudgedItem.item_id")
        if not isinstance(self.grade, int) or isinstance(self.grade, bool):
            raise SchemaError(f"grade must be an int, got {self.grade!r}")
        if self.grade < 0:
            raise SchemaError(f"grade must be >= 0, got {self.grade}")

    def key(self) -> tuple[str, str]:
        return (normalize_kind(self.kind), self.item_id)


#: Recommended grade scale (informational). 0 = explicitly not relevant, 3 = perfect.
GRADE_SCALE: dict[int, str] = {0: "not_relevant", 1: "marginal", 2: "relevant", 3: "perfect"}


# Canonical golden-set categories the plan calls out. Free-form labels are also
# allowed; the report breaks down by every label a case carries.
CATEGORIES: frozenset[str] = frozenset(
    {
        "exact_name",          # dotted/versioned/hyphenated identifiers, filenames
        "workflow_code",       # python imports/symbols/node classes/kwargs/fragments
        "multi_term",          # several required terms
        "paraphrase",          # conceptual restatement, low lexical overlap
        "settings",            # settings / troubleshooting
        "named_author",        # author-scoped
        "channel_scoped",      # channel filter
        "time_scoped",         # since filter
        "cross_source",        # answer spans message + resource / distillation
        "best_is_distillation",
        "best_is_message",
        "best_is_resource",
        "long_resource_chunk", # answer lives in a later chunk
        "workflow_only",       # kinds=[workflow]
        "single_workflow",     # kinds=[workflow] + item_ids=[one id]
        "spelling_variant",
        "no_hit",              # expect_no_hit = True
    }
)


@dataclass
class GoldenCase:
    """One judged query."""

    id: str
    query: str
    expected: list[JudgedItem] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    filters: dict[str, Any] = field(default_factory=dict)
    expect_no_hit: bool = False
    limit: int = 20
    notes: str | None = None

    def __post_init__(self) -> None:
        validate_item_id(self.id, context="GoldenCase.id")
        if not isinstance(self.query, str) or not self.query.strip():
            raise SchemaError(f"GoldenCase {self.id!r}: query must be a non-empty string")
        if not isinstance(self.limit, int) or isinstance(self.limit, bool) or self.limit < 1:
            raise SchemaError(f"GoldenCase {self.id!r}: limit must be a positive int")
        self.filters = validate_filters(self.filters)
        # de-dup judgments by key, keeping the highest grade (deterministic)
        best: dict[tuple[str, str], JudgedItem] = {}
        for j in self.expected:
            k = j.key()
            if k not in best or j.grade > best[k].grade:
                best[k] = j
        self.expected = [best[k] for k in sorted(best)]

        # Category hygiene: strip empties; unknown labels allowed but lowercased.
        cats: list[str] = []
        for c in self.categories:
            if not isinstance(c, str) or not c.strip():
                continue
            cats.append(c.strip())
        self.categories = cats

        if self.expect_no_hit:
            if self.expected:
                raise SchemaError(
                    f"GoldenCase {self.id!r}: expect_no_hit=True but expected is non-empty"
                )
            if "no_hit" not in self.categories:
                self.categories.append("no_hit")
        else:
            if not self.expected:
                raise SchemaError(
                    f"GoldenCase {self.id!r}: expected is empty and expect_no_hit is False "
                    "(set expect_no_hit: true for expected no-result queries)"
                )

    @property
    def is_judged(self) -> bool:
        """True when there is at least one relevant item (excludes no-hit cases)."""
        return any(j.grade >= 1 for j in self.expected)

    def relevant_keys(self) -> set[tuple[str, str]]:
        """Keys of items judged relevant (grade >= 1)."""
        return {j.key() for j in self.expected if j.grade >= 1}

    def grade_map(self) -> dict[tuple[str, str], int]:
        """Map item key -> relevance grade (0 for everything else)."""
        return {j.key(): j.grade for j in self.expected}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "GoldenCase":
        cid = raw.get("id")
        if cid is None:
            raise SchemaError("golden case requires 'id'")
        expected_raw = raw.get("expected", []) or []
        if not isinstance(expected_raw, list):
            raise SchemaError(f"GoldenCase {cid!r}: 'expected' must be a list")
        expected: list[JudgedItem] = []
        for e in expected_raw:
            if not isinstance(e, dict):
                raise SchemaError(f"GoldenCase {cid!r}: each expected entry must be a mapping")
            kind = e.get("kind")
            item_id = e.get("item_id")
            grade = e.get("grade", 1)
            if kind is None or item_id is None:
                raise SchemaError(
                    f"GoldenCase {cid!r}: expected entry requires 'kind' and 'item_id'"
                )
            expected.append(
                JudgedItem(
                    kind=str(kind),
                    item_id=_coerce_id(item_id),
                    grade=int(grade),
                    note=_opt_str(e.get("note")),
                )
            )
        # Backwards-compat shorthand: expected_ids + expected_kind.
        for e in raw.get("expected_ids", []) or []:
            expected.append(
                JudgedItem(kind=str(raw.get("expected_kind", "message")), item_id=_coerce_id(e), grade=1)
            )

        return cls(
            id=str(cid),
            query=str(raw.get("query", "")),
            expected=expected,
            categories=list(raw.get("categories", []) or []),
            filters=raw.get("filters"),
            expect_no_hit=bool(raw.get("expect_no_hit", False)),
            limit=int(raw.get("limit", 20)),
            notes=_opt_str(raw.get("notes")),
        )


@dataclass
class Corpus:
    """Collection of corpus items, indexed by item key for fast lookup."""

    items: list[CorpusItem] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._by_key: dict[tuple[str, str], CorpusItem] = {
            it.id.key(): it for it in self.items
        }

    @classmethod
    def from_records(cls, records: list[dict[str, Any]]) -> "Corpus":
        items = [CorpusItem.from_dict(r) for r in records]
        return cls(items=items)

    def by_key(self, key: tuple[str, str]) -> CorpusItem | None:
        return self._by_key.get(key)

    def keys(self) -> set[tuple[str, str]]:
        return set(self._by_key)


@dataclass
class GoldenSet:
    """A collection of golden cases."""

    cases: list[GoldenCase] = field(default_factory=list)

    @classmethod
    def from_records(cls, records: list[dict[str, Any]]) -> "GoldenSet":
        cases = [GoldenCase.from_dict(r) for r in records]
        seen: set[str] = set()
        for c in cases:
            if c.id in seen:
                raise SchemaError(f"duplicate golden case id {c.id!r}")
            seen.add(c.id)
        return cls(cases=cases)

    @property
    def judged(self) -> list[GoldenCase]:
        return [c for c in self.cases if c.is_judged]

    @property
    def no_hit(self) -> list[GoldenCase]:
        return [c for c in self.cases if c.expect_no_hit]
