"""Retriever adapters for the Hivemind retrieval evaluation harness.

Defines the :class:`Retriever` protocol and a registry of named systems. Two
kinds of adapter ship today:

* **Real adapters** that faithfully model a system the corpus already supports:
  - :class:`LegacyIlikeAdapter` — a deterministic offline model of Hivemind's
    *current* production search: two PostgREST ``ILIKE`` passes against
    ``unified_feed`` (distillations first, then everything else), each
    independently capped at ``limit`` (the "doubled-limit" behaviour), with
    case-insensitive substring matching on title/body.

* **Deterministic fixture adapters** that prove the harness contract without
  inventing unimplemented endpoints:
  - :class:`StubAdapter` — always empty (a no-result / fully-degraded floor).
  - :class:`OracleAdapter` — returns the judged relevant items in grade order
    (a perfect ceiling; should score ~1.0 Recall/MRR/nDCG within the limit).
  - :class:`ReverseAdapter` — deterministic but anti-legacy ordering, used to
    exercise tie-break and order-sensitivity tests.
  - :class:`ErrorAdapter` / :class:`TimeoutAdapter` — always fail, used to prove
    failure / timeout accounting without crashing the run.

Extension point for later tasks (NOT wired to any endpoint here):

* :class:`RemoteSearchAdapter` — a generic adapter that calls *any* configured
  search URL (the existing PostgREST ``unified_feed`` path or, later, the hybrid
  Edge Function from tasks 3.1–3.10). It is opt-in: it raises a clear
  configuration error until an operator sets ``HIVEMIND_SEARCH_URL``. Tasks
  1.11 (lexical RPC), 2.14/3.10 (semantic/hybrid), and 5.9 (post-backfill
  re-eval) plug real systems in by pointing this adapter at their endpoint or by
  registering a new adapter via :func:`register_adapter`.

Offline adapters MUST NOT import anything outside the standard library or this
package. ``RemoteSearchAdapter`` is the only adapter that touches the network,
and only when explicitly configured.
"""

from __future__ import annotations

import re
import urllib.request
from typing import Any, Callable, Protocol

from eval.retrieval.schema import (
    ALLOWED_KINDS,
    Corpus,
    CorpusItem,
    JudgedItem,
    Query,
    Result,
)

# ---------------------------------------------------------------------------
# Failure signal exceptions
# ---------------------------------------------------------------------------


class RetrievalError(Exception):
    """An adapter failed for a non-timeout reason (recorded as ``error``)."""


class RetrievalTimeout(Exception):
    """An adapter exceeded its latency budget (recorded as ``timeout``)."""


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class Retriever(Protocol):
    """A retrieval system under test.

    Implementations may be offline models (legacy/fixture adapters) or remote
    callers (the live Edge function). ``name`` identifies the system in reports.
    """

    name: str

    def retrieve(self, query: Query) -> list[Result]:
        """Return ranked results for *query* (rank 1 = index 0)."""
        ...


# ---------------------------------------------------------------------------
# Shared filter helpers (reused by future lexical/hybrid adapters)
# ---------------------------------------------------------------------------


def requested_kinds(filters: dict[str, Any]) -> list[str] | None:
    """Return the kinds a query restricts to, or ``None`` for "all kinds"."""
    kinds = filters.get("kinds")
    if not kinds:
        return None
    return list(kinds)


def passes_filters(item: CorpusItem, filters: dict[str, Any]) -> bool:
    """Apply the AD-1 source/kind/date filters that are endpoint-independent.

    Used by offline adapters. ``item_ids`` / ``channels`` / ``authors`` are
    *not* applied here: the legacy production path never honored them, so the
    legacy adapter must not either. Newer lexical/hybrid adapters (1.11/3.10)
    apply ``item_ids``/``channels``/``authors`` in their own candidate SQL.
    """
    kinds = requested_kinds(filters)
    if kinds is not None:
        # A kind filter accepts both the literal kind and its entity alias, so a
        # kinds=[workflow] filter matches kind=resource workflow rows and vice
        # versa (AD-1 alias).
        accepted = set()
        for k in kinds:
            accepted.add(k)
            if k == "workflow":
                accepted.add("resource")
            if k == "resource":
                accepted.add("workflow")
        if item.kind not in accepted:
            return False
    sources = filters.get("sources")
    if sources and item.source not in set(sources):
        return False
    since = filters.get("since")
    if since and (item.created_at is None or item.created_at < since):
        return False
    return True


def _legacy_sort_key(item: CorpusItem) -> tuple[Any, ...]:
    """Deterministic order modelling the production distillations-first merge.

    Production ``ILIKE`` is not relevance-ranked; PostgREST returns rows in
    storage order. We model a stable, documented order: ``created_at`` desc
    (None last), then ``(entity_kind, item_id)`` asc. This makes the legacy
    adapter reproducible without pretending it ranks by relevance.
    """
    created = item.created_at or ""
    # desc on created_at → negate by sorting on a reversed key; we sort the
    # whole list with reverse=True on a composite below instead.
    return (created, item.entity_kind(), item.item_id)


def _ilike_match(item: CorpusItem, needle: str) -> bool:
    """Case-insensitive substring match on title OR body (PostgREST ilike.*q*).

    The query is treated as a literal substring, matching the executor's
    escaping of literal ``*`` (see ``executors/search/run.py::_ilike_clause``).
    """
    if not needle:
        return False
    n = needle.lower()
    return n in (item.title or "").lower() or n in (item.body or "").lower()


# ---------------------------------------------------------------------------
# Real adapter: legacy two-pass ILIKE
# ---------------------------------------------------------------------------


class LegacyIlikeAdapter:
    """Deterministic offline model of the current production search.

    Faithfully reproduces the behaviour documented in
    ``executors/search/run.py`` and the plan's "Why this work is needed":

    * two passes against ``unified_feed`` — distillations, then everything else;
    * each pass is an ``or=(title.ilike.*q*, body.ilike.*q*)`` substring match;
    * each pass is independently capped at ``limit``;
    * the two lists are concatenated (distillations first), so a response can
      contain up to ``2 * limit`` rows (the "doubled-limit" behaviour);
    * optional ``kinds`` / ``sources`` / ``since`` filters apply.

    It deliberately does NOT honour ``item_ids`` / ``channels`` / ``authors``
    because the legacy endpoint never did — those arrive with the lexical/hybrid
    work (1.8 / 3.3).
    """

    name = "legacy"

    def __init__(self, corpus: Corpus) -> None:
        self._corpus = corpus

    def retrieve(self, query: Query) -> list[Result]:
        needle = query.query.strip()
        filters = query.filters or {}
        kinds = requested_kinds(filters)

        want_distillations = kinds is None or "distillation" in kinds
        want_others = kinds is None or any(k != "distillation" for k in kinds)

        # Build the two passes over filtered, ilike-matched candidates.
        dist_rows: list[CorpusItem] = []
        other_rows: list[CorpusItem] = []
        for item in self._corpus.items:
            if not passes_filters(item, filters):
                continue
            if not _ilike_match(item, needle):
                continue
            if item.entity_kind() == "distillation":
                if want_distillations:
                    dist_rows.append(item)
            else:
                if want_others:
                    other_rows.append(item)

        dist_sorted = _sort_legacy(dist_rows)[: query.limit]
        other_sorted = _sort_legacy(other_rows)[: query.limit]

        return _to_results(dist_sorted + other_sorted)


def _sort_legacy(rows: list[CorpusItem]) -> list[CorpusItem]:
    """Stable sort: created_at desc, then (entity_kind, item_id) asc."""
    return sorted(
        rows,
        key=lambda it: (it.created_at or "", it.entity_kind(), it.item_id),
        reverse=True,
    )


def _to_results(items: list[CorpusItem]) -> list[Result]:
    return [Result(kind=it.kind, item_id=it.item_id) for it in items]


# ---------------------------------------------------------------------------
# Fixture adapters
# ---------------------------------------------------------------------------


class StubAdapter:
    """Always returns []. A no-result floor and a fully-degraded baseline."""

    name = "stub"

    def __init__(self, corpus: Corpus | None = None) -> None:
        self._corpus = corpus

    def retrieve(self, query: Query) -> list[Result]:
        return []


class OracleAdapter:
    """Perfect ceiling: returns judged relevant items in grade order.

    Uses the ground-truth ``expected`` threaded through :class:`Query`. Within
    ``limit`` it should score Recall@k = MRR = nDCG@10 = 1.0. Excludes grade-0
    (explicitly not-relevant) judgments.
    """

    name = "oracle"

    def __init__(self, corpus: Corpus | None = None) -> None:
        self._corpus = corpus

    def retrieve(self, query: Query) -> list[Result]:
        judged = [j for j in (query.expected or []) if j.grade > 0]
        judged.sort(key=lambda j: (-j.grade, normalize_key(j)))
        out: list[Result] = []
        for j in judged[: query.limit]:
            out.append(Result(kind=j.kind, item_id=j.item_id, score=float(j.grade)))
        return out


def normalize_key(j: JudgedItem) -> tuple[str, str]:
    """Sort helper: entity-key tuple for deterministic tie-breaks."""
    return (j.key()[0], j.key()[1])


class ReverseAdapter:
    """Deterministic, anti-legacy ordering over ilike matches.

    Matches the same candidates as the legacy adapter but sorts by
    ``(entity_kind, item_id)`` ascending — the opposite tie direction — so the
    two adapters disagree on order for multi-hit queries. Used to prove that
    MRR/Recall are rank-sensitive and that tie-breaks are deterministic.
    """

    name = "reverse"

    def __init__(self, corpus: Corpus) -> None:
        self._corpus = corpus

    def retrieve(self, query: Query) -> list[Result]:
        needle = query.query.strip()
        filters = query.filters or {}
        matched = [
            it
            for it in self._corpus.items
            if passes_filters(it, filters) and _ilike_match(it, needle)
        ]
        matched.sort(key=lambda it: (it.entity_kind(), it.item_id))
        return _to_results(matched[: query.limit])


class ErrorAdapter:
    """Always raises :class:`RetrievalError`. Proves error accounting."""

    name = "error"

    def __init__(self, corpus: Corpus | None = None) -> None:
        self._corpus = corpus

    def retrieve(self, query: Query) -> list[Result]:
        raise RetrievalError("forced adapter error (fixture)")


class TimeoutAdapter:
    """Always raises :class:`RetrievalTimeout`. Proves timeout accounting."""

    name = "timeout"

    def __init__(self, corpus: Corpus | None = None) -> None:
        self._corpus = corpus

    def retrieve(self, query: Query) -> list[Result]:
        raise RetrievalTimeout("forced adapter timeout (fixture)")


# ---------------------------------------------------------------------------
# Extension point: generic remote adapter (opt-in, not wired to any endpoint)
# ---------------------------------------------------------------------------


class RemoteSearchAdapter:
    """Generic adapter that calls a configured search URL.

    This is the extension point for tasks 1.11 / 2.14 / 3.10 / 5.9. It is **opt-in
    and unconfigured by default**: construction raises :class:`RetrieverConfigError`
    unless ``HIVEMIND_SEARCH_URL`` (or the constructor ``url`` argument) is set.

    It deliberately does not hardcode a hybrid Edge endpoint (that endpoint does
    not exist yet — task 3.1+). An operator points it at whichever endpoint is
    live: the existing PostgREST ``unified_feed`` path for a legacy/lexical
    comparison, or the future search Edge Function once deployed. Network use is
    gated behind the ``HIVEMIND_EVAL_NETWORK`` opt-in flag so offline test suites
    never hit the network.

    The response contract is the public search shape (``results`` → list of rows
    with ``kind``/``item_id``). Adapters for richer endpoints can subclass and
    override :meth:`parse`.
    """

    name = "remote"

    def __init__(
        self,
        corpus: Corpus | None = None,
        *,
        url: str | None = None,
        anon_key: str | None = None,
        timeout: float = 5.0,
    ) -> None:
        import os

        self._url = url or os.environ.get("HIVEMIND_SEARCH_URL")
        self._key = anon_key or os.environ.get("HIVEMIND_ANON_KEY") or ""
        self._timeout = timeout
        if not self._url:
            raise RetrieverConfigError(
                "RemoteSearchAdapter requires HIVEMIND_SEARCH_URL (or url=). "
                "Set it to a live search endpoint to compare that system; the "
                "hybrid Edge Function is added by task 3.1+."
            )

    def retrieve(self, query: Query) -> list[Result]:
        import json
        import os

        if os.environ.get("HIVEMIND_EVAL_NETWORK") not in ("1", "true", "True"):
            raise RetrieverConfigError(
                "RemoteSearchAdapter needs HIVEMIND_EVAL_NETWORK=1 to touch the network"
            )
        payload = {
            "query": query.query,
            "limit": query.limit,
            "filters": _prune_filters(query.filters),
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "apikey": self._key,
                "Authorization": f"Bearer {self._key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                body = resp.read().decode("utf-8")
        except TimeoutError as exc:  # pragma: no cover - network path
            raise RetrievalTimeout(str(exc)) from exc
        parsed = json.loads(body)
        return self.parse(parsed)

    def parse(self, response: Any) -> list[Result]:
        rows = response.get("results", response) if isinstance(response, dict) else response
        out: list[Result] = []
        for row in rows or []:
            kind = str(row.get("kind"))
            item_id = row.get("item_id")
            if kind not in ALLOWED_KINDS or item_id is None:
                continue
            out.append(
                Result(
                    kind=kind,
                    item_id=str(item_id),
                    score=row.get("score"),
                    matched_representation=row.get("matched_representation"),
                    matched_snippet=row.get("matched_snippet"),
                )
            )
        return out


def _prune_filters(filters: dict[str, Any]) -> dict[str, Any]:
    """Drop internal keys before sending filters to a remote endpoint."""
    return {k: v for k, v in (filters or {}).items() if k != "extra"}


class RetrieverConfigError(Exception):
    """A retriever was misconfigured (e.g. remote adapter with no URL)."""


# ---------------------------------------------------------------------------
# Real adapter: indexed lexical search (Phase 1, tasks 1.7–1.11)
# ---------------------------------------------------------------------------


# Mirror of the SQL candidate arms (schema/008) as a deterministic offline model
# over the corpus, so the frozen evaluation can measure lexical relevance with
# no network and no DB. Parity with the SQL is enforced by an isolated-cluster
# test (tests/test_lexical_candidate_sql.py); this is the eval's reproducible
# model, the way LegacyIlikeAdapter models the legacy ILIKE path.
_SIMPLE_TOKEN_RE = re.compile(r"[a-z0-9]+")
# The frozen separator set (mirrors schema/005 hivemind_normalize_identifier).
_IDENT_SEP_RE = re.compile(r"[\s.\-_/\\,:;(){}\[\]@\"'`]+")


def lexical_simple_tokens(text: str) -> list[str]:
    """Tokens mirroring ``to_tsvector('simple', text)`` enough for AND-matching:
    lowercase alphanumeric runs. (``'simple'`` lowercases and splits on
    non-word characters; this is the matching-relevant shape.)"""
    return _SIMPLE_TOKEN_RE.findall((text or "").lower())


def lexical_norm_identifier(text: str) -> str:
    """Mirror of ``public.hivemind_normalize_identifier`` (compact key)."""
    return _IDENT_SEP_RE.sub("", (text or "").lower())


def lexical_passes_filters(item: CorpusItem, filters: dict[str, Any]) -> bool:
    """The full AD-1 + Phase-1 filter set the lexical RPC honors.

    Unlike the legacy :func:`passes_filters`, this ALSO honors ``channels``
    (``item.context``), ``authors`` (``item.author``), and ``item_ids``
    (the bounded identity allow-list) — those are exactly the filters the
    lexical candidate SQL applies (tasks 1.8/3.3). Kinds use the
    workflow↔resource alias.
    """
    kinds = requested_kinds(filters)
    if kinds is not None:
        # Build the accepted *entity* kinds. Any resource-kind in the filter
        # accepts entity_kind 'resource' (workflow aliases resource).
        wants_message = any(k == "message" for k in kinds)
        wants_dist = any(k == "distillation" for k in kinds)
        wants_res = any(k != "message" and k != "distillation" for k in kinds)
        ek = item.entity_kind()
        ok = (ek == "message" and wants_message) or (
            ek == "distillation" and wants_dist
        ) or (ek == "resource" and wants_res)
        if not ok:
            return False

    sources = filters.get("sources")
    if sources and item.source not in set(sources):
        return False
    since = filters.get("since")
    if since and (item.created_at is None or item.created_at < since):
        return False
    channels = filters.get("channels")
    if channels and (item.context is None or item.context not in set(channels)):
        return False
    authors = filters.get("authors")
    if authors and (item.author is None or item.author not in set(authors)):
        return False
    item_ids = filters.get("item_ids")
    if item_ids and item.item_id not in set(item_ids):
        return False
    return True


class LexicalAdapter:
    """Deterministic offline model of the indexed lexical candidate SQL.

    Mirrors the arms of ``public.hivemind_lexical_candidates`` (schema/008):

    * **fts**   — ``websearch_to_tsquery('simple', q)`` is an AND of terms; an
      item matches when every query token appears in its title+body surface.
    * **ident** — normalized identifier CONTAINMENT (the task-1.6 contract) over
      the title (band 0.95), a workflow code/body surface (0.93), or a message
      body (0.90). Whole-body identifier equality would be 1.00 (rare offline).

    A definitional hit (title / question / code) outranks a passing message-prose
    mention, matching the SQL bands so a workflow that defines a node outranks
    messages that merely mention it. Kinds/source/date/channel/author/item_id
    filters apply before ranking; results collapse to one row per identity and
    tie-break by ``created_at`` desc then ``(entity_kind, item_id)`` asc.
    """

    name = "lexical"

    def __init__(self, corpus: Corpus) -> None:
        self._corpus = corpus

    def retrieve(self, query: Query) -> list[Result]:
        q = (query.query or "").strip()
        filters = query.filters or {}
        q_tokens = lexical_simple_tokens(q)
        q_id = lexical_norm_identifier(q)
        q_token_set = set(q_tokens)

        scored: list[tuple[float, str | None, CorpusItem]] = []
        for item in self._corpus.items:
            if not lexical_passes_filters(item, filters):
                continue
            rank, rep = _lexical_score(item, q_tokens, q_token_set, q_id)
            if rank is not None:
                scored.append((rank, rep, item))

        # Deterministic global order: lexical_rank desc, created_at desc, then
        # stable (entity_kind, item_id) asc — mirrors the SQL TIE_BREAK.
        scored.sort(
            key=lambda r: (
                -r[0],
                _created_sort_key(r[2]),
                r[2].entity_kind(),
                r[2].item_id,
            )
        )
        out: list[Result] = []
        for rank, rep, item in scored[: query.limit]:
            out.append(
                Result(
                    kind=item.kind,
                    item_id=item.item_id,
                    score=rank,
                    matched_representation=rep,
                )
            )
        return out


def _created_sort_key(item: CorpusItem) -> str:
    """created_at DESC via a reversed string key (None sorts last)."""
    c = item.created_at
    # Negate lexicographically by mapping chars; None -> smallest so it sorts last.
    if not c:
        return "\U0010FFFF"
    # Invert each char so ascending sort yields descending dates.
    return "".join(chr(0x10FFFF - ord(ch)) for ch in c)


def _lexical_score(
    item: CorpusItem,
    q_tokens: list[str],
    q_token_set: set[str],
    q_id: str,
) -> tuple[float | None, str | None]:
    """Return (lexical_rank, matched_representation) or (None, None) if no match.

    Bands mirror schema/008: title/question ident 0.95, workflow code ident 0.93,
    message ident 0.90, FTS (capped below 0.90) for multi-term relevance.
    """
    title = item.title or ""
    body = item.body or ""
    is_resource = item.entity_kind() == "resource"

    rank: float | None = None
    rep: str | None = None

    # ---- identifier CONTAINMENT arm (task 1.6 contract) ----
    if q_id:
        title_id = lexical_norm_identifier(title)
        if title_id and q_id in title_id:
            rank, rep = 0.95, "prose"
        body_id = lexical_norm_identifier(body)
        if q_id in body_id:
            # A workflow body carries its node/model/code surface; a message
            # body is prose. Code-definition > prose-mention.
            brank = 0.93 if is_resource else 0.90
            brep = "workflow_python" if is_resource else "prose"
            if rank is None or brank > rank:
                rank, rep = brank, brep

    # ---- FTS arm: websearch_to_tsquery is an AND of all query tokens ----
    if q_tokens:
        surface_tokens = set(lexical_simple_tokens(f"{title}\n{body}"))
        if q_token_set <= surface_tokens:  # every query token present (AND)
            title_tokens = set(lexical_simple_tokens(title))
            title_frac = (
                sum(1 for t in q_token_set if t in title_tokens) / len(q_token_set)
            )
            fts = min(0.89, 0.30 + 0.45 + 0.12 * title_frac)
            if rank is None or fts > rank:
                rank, rep = fts, "prose"

    return rank, rep


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

#: Adapter factories registered by name. ``build_adapter(name, corpus)`` returns
#: an instance. Tasks 1.11 / 2.14 / 3.10 / 5.9 add real systems either by
#: configuring :class:`RemoteSearchAdapter` or by calling :func:`register_adapter`.
AdapterFactory = Callable[..., Retriever]

ADAPTERS: dict[str, AdapterFactory] = {
    "legacy": LegacyIlikeAdapter,
    "lexical": LexicalAdapter,
    "stub": StubAdapter,
    "oracle": OracleAdapter,
    "reverse": ReverseAdapter,
    "error": ErrorAdapter,
    "timeout": TimeoutAdapter,
    "remote": RemoteSearchAdapter,
}


def register_adapter(name: str, factory: AdapterFactory) -> None:
    """Register a new adapter factory under *name* (extension point)."""
    if not name or not isinstance(name, str):
        raise ValueError("adapter name must be a non-empty string")
    ADAPTERS[name] = factory


def build_adapter(name: str, corpus: Corpus | None = None, **kwargs: Any) -> Retriever:
    """Construct a registered adapter by name.

    ``corpus`` is forwarded to adapters that need it (legacy/reverse); fixture
    adapters that do not accept it ignore it. Extra ``kwargs`` are forwarded.
    """
    if name not in ADAPTERS:
        raise ValueError(
            f"unknown adapter {name!r}; registered: {sorted(ADAPTERS)}"
        )
    factory = ADAPTERS[name]

    # Forward corpus only if the factory accepts it.
    try:
        import inspect

        sig = inspect.signature(factory)
        params = sig.parameters
        if "corpus" in params and corpus is not None:
            return factory(corpus, **kwargs)
        if corpus is not None and any(
            p.kind is inspect.Parameter.VAR_POSITIONAL
            or p.kind is inspect.Parameter.VAR_KEYWORD
            for p in params.values()
        ):
            return factory(corpus, **kwargs)
    except (TypeError, ValueError):
        pass
    return factory(**kwargs)


def available_adapters() -> list[str]:
    """Names of all registered adapters, sorted."""
    return sorted(ADAPTERS)
