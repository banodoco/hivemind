#!/usr/bin/env python3
"""Search the Hivemind corpus — scoped raw-table per-token search, client-ranked.

Why this exists (and why it no longer queries ``unified_feed``):

The old executor issued ``or=(title.ilike.*<query>*,body.ilike.*<query>*)``
against the ``unified_feed`` VIEW.  That is a UNION of messages + resources +
distillations built with ``jsonb_build_object`` and lateral joins; PostgREST
cannot push an ILIKE filter on it to any index, the anon role has a 3s
statement timeout, and a multi-word phrase (a literal substring that almost
never occurs in the corpus) returns zero rows even when the corpus has the
answer.  Per-token OR over the view blows the statement budget (HTTP 500 /
SQLSTATE 57014) because the 1.28M-row derived-view scan exceeds 3s.

The fix mirrors VibeComfy's client (``hivemind_clients.py``): search the raw,
index-backed tables directly —

  * discord messages      -> ``message_feed``        (content ilike, recency-ordered, ~0.2-0.4s)
  * workflows / resources -> ``external_resources``  (kind btree + title/body trigram GIN)
  * distillations         -> ``distillations``       (small curated table; status pending|approved,
                                                      mirroring the unified_feed distillation branch)

Each scope is queried in parallel.  Message and distillation scopes use the
per-token **OR** (the only shape measured fast on ``message_feed`` — its AND
intersection times out); the resources scope tries a title **AND** first
(high precision, ~0.12s) and widens to a title+body **OR** when the AND is
thin and fast.  Hits are ranked client-side (distinctive tokens in title
weigh more than body, approved distillations and parseable workflows float,
phrase matches bonus) and merged deterministically.

Hard rules (verified live 2026-08-19):
  * NEVER send a multi-token ``or=(...)`` to ``unified_feed`` (the 57014).
  * NEVER ``select=*`` — and never select ``payload`` on external_resources
    (full Comfy JSON).
  * NEVER ``Prefer: count=exact``; always ``limit`` + server recency order.
  * Never primary-search a multi-word string as one ILIKE phrase.
  * Never ``and=`` over ``message_feed`` (2 tokens ≈ 2s, 3 tokens → 57014).
  * This PostgREST build rejects ``or:`` nested inside ``and=`` (PGRST100).

A phrase-ILIKE third pass is deliberately NOT implemented: with substring
semantics a row containing the exact phrase also contains every token of it,
so the OR pass already matches it — a phrase pass would be a redundant
request with zero added recall.
"""

from __future__ import annotations

import argparse
import re
import sys
import threading
import time
import urllib.error
from datetime import datetime
from typing import Any

# -- dual-import guard (T5 pattern) -------------------------------------------
try:
    from .._common import (
        output_json,
        postgrest_get,
        resolve_anon_key,
        resolve_endpoint,
    )
except ImportError:
    import os as _os

    _HERE = _os.path.dirname(_os.path.abspath(__file__))
    _EXECUTORS = _os.path.dirname(_HERE)
    sys.path.insert(0, _EXECUTORS)
    from _common import (  # type: ignore[import-not-found]
        output_json,
        postgrest_get,
        resolve_anon_key,
        resolve_endpoint,
    )

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NUDGE = (
    "No distillation results found — consider researching this question "
    "and submitting a cited distillation to help the next person."
)

# Search hits are leads; the full row is get_item's job.
_BODY_LIMIT = 400

# Distinctive-token budget for the SQL predicate vs. the client ranker.
_SQL_TOKEN_CAP = 4
_RANK_TOKEN_CAP = 8
_MAX_ILIKE_ARMS = 12

# Pass-A (AND) -> Pass-B (OR) fallback gate (resources scope only).
_AND_MIN_ROWS = 3
_AND_MAX_MS = 400.0

# Candidate pool fetched per scope before client ranking (measured fast:
# message_feed OR + recency order answers 100 rows in ~0.4s).  The final
# result list is capped at the requested --limit.
_POOL_LIMIT = 100

# Client deadline for a recency-ordered OR attempt.  The backend sort is
# token-dependent (0.1s for ``wan``, ~0.5s for ``sora``, ~1.6s for
# ``upscale``, 2-3s borderline 57014 for ``hotshot``), so the ordered attempt
# is bounded and degrades to the unordered OR (measured ~0.1-0.4s) on
# timeout or statement-timeout.
_ORDERED_OR_TIMEOUT_S = 0.5

# Bound on the unordered OR fallback too, so a pathological token
# (``hotshotxl`` measured 0.6-3.5s) or a hung connection can never block a
# search for the full 30s default.
_UNORDERED_OR_TIMEOUT_S = 10.0

# Bound on the external_resources title+body recall pass before degrading to
# title-only (the body shape is token-dependent: ~0.4s for ``ltx``, ~2.2s
# for ``hotshot``).
_BODY_OR_TIMEOUT_S = 0.8

# Token regex from VibeComfy's client: alnum plus a few safe punctuation
# characters, so tokens can never smuggle PostgREST/ILIKE syntax through.
_QUERY_TOKEN_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9+._-]*")

# Generic words that carry no search identity (VibeComfy client parity).
_SEARCH_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "at",
        "build",
        "can",
        "create",
        "for",
        "generate",
        "graph",
        "happen",
        "happening",
        "how",
        "image",
        "in",
        "is",
        "make",
        "of",
        "on",
        "please",
        "show",
        "the",
        "this",
        "to",
        "video",
        "what",
        "whats",
        "with",
    }
)

# Generic classifier/task words that widen an ILIKE search without improving
# recall (VibeComfy client parity).
_HIVEMIND_FALLBACK_STOPWORDS = frozenset(
    {
        "research",
        "goal",
        "find",
        "finding",
        "working",
        "work",
        "include",
        "including",
        "required",
        "requires",
        "custom",
        "nodes",
        "node",
        "checkpoint",
        "checkpoints",
        "model",
        "models",
        "loader",
        "loaders",
        "latent",
        "sampling",
        "setup",
        "setups",
        "frame",
        "frames",
        "generating",
        "generation",
        "needed",
        "using",
        "use",
        "used",
        "switch",
        "switching",
        "switches",
        "change",
        "changing",
        "convert",
        "converting",
        "making",
        "apply",
        "applying",
        "set",
        "setting",
        "add",
        "adding",
        "remove",
        "removing",
        "replace",
        "replacing",
        "workflow",
        "workflows",
        "comfy",
        "comfyui",
        "videos",
        "images",
        "audio",
    }
)

# Spelling variants expanded client-side: each token's alternates are ORed
# within the token's own arm (capped globally at _MAX_ILIKE_ARMS).
_TOKEN_VARIANTS: dict[str, tuple[str, ...]] = {
    "ipadapter": ("ipadapter", "ip-adapter", "ip_adapter"),
    "ltx": ("ltx", "ltxv", "lightricks"),
    "hotshot": ("hotshot", "hotshotxl", "hot shot"),
    "wan2.1": ("wan2.1", "wan 2.1", "wan2_1"),
}

# ---------------------------------------------------------------------------
# Scope surfaces (raw tables only — NEVER unified_feed)
# ---------------------------------------------------------------------------

# Projections: explicit columns, never ``select=*`` and never ``payload``.
_MESSAGE_COLUMNS = "message_id,content,author_name,channel_name,created_at,guild_id,channel_id"
_RESOURCE_COLUMNS = "id,kind,source,title,body,author,url,metadata,created_at"
_DISTILLATION_COLUMNS = "id,question,conditions,answer,confidence,status,created_at"
# Index-backed thread surface (schema/036): no author_name/channel_name here.
_THREAD_COLUMNS = "message_id,content,created_at,guild_id,channel_id,thread_id"

_MESSAGE_SOURCE = "banodoco-discord"   # baked into the view; the only message source
_DISTILLATION_SOURCE = "hivemind"     # baked into the view; the only distillation source

_RESOURCE_COLUMNS_FORBIDDEN = ("payload",)  # never project the Comfy JSON blob


def _resource_kind_filter(kinds: list[str]) -> list[str] | None:
    """Map user kind tokens to concrete external_resources kinds.

    ``resource`` is a meta-kind (every external_resources row IS a resource),
    so it contributes no filter; concrete kinds (workflow, article, ...) pass
    through.  Returns None when no concrete kind was named.
    """
    concrete = [k for k in kinds if k not in ("message", "distillation", "resource")]
    if not concrete:
        return None
    return concrete


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------


def _query_tokens(query: str) -> list[str]:
    """Split *query* into safe tokens (alnum + ``+._-`` runes)."""
    return [m.group(0) for m in _QUERY_TOKEN_RE.finditer(query)]


def _distinctive_tokens(query: str, cap: int) -> list[str]:
    """Tokens of *query* that carry search identity, capped at *cap*.

    Drops stopwords and pure-digit tokens; if nothing distinctive remains,
    falls back to the raw tokens (minus digits) so a generic query still
    searches.  Original order preserved.
    """
    raw = _query_tokens(query)
    if not raw:
        return []
    stop = _SEARCH_STOPWORDS | _HIVEMIND_FALLBACK_STOPWORDS
    tokens = [t for t in raw if t.casefold() not in stop and not t.isdigit()]
    if not tokens:
        tokens = [t for t in raw if not t.isdigit()]
    return tokens[:cap]


def _variants(token: str) -> tuple[str, ...]:
    """Spelling alternates for *token* (or the token itself)."""
    return _TOKEN_VARIANTS.get(token.casefold(), (token,))


def _ilike_arms(columns: tuple[str, ...], token: str) -> list[str]:
    """PostgREST ILIKE patterns for one token across *columns* + its variants.

    Variants containing whitespace (e.g. ``hot shot``, ``wan 2.1``) are
    skipped: a space in the pattern defeats the trigram GIN index and forces
    a full scan (measured 2.1s for ``*hot shot*`` on message_feed vs 0.2s
    for ``*hotshot*``).  The client ranker still sees the full variant list
    via :func:`_variants`.
    """
    arms: list[str] = []
    for variant in _variants(token):
        if any(ch.isspace() for ch in variant):
            continue
        for column in columns:
            arms.append(f"{column}.ilike.*{variant}*")
    return arms[:_MAX_ILIKE_ARMS]


def _phrase_tokens(query: str) -> str | None:
    """Original-order distinctive tokens joined into a phrase, or None."""
    tokens = _distinctive_tokens(query, _SQL_TOKEN_CAP)
    if not tokens:
        return None
    return " ".join(tokens)


# ---------------------------------------------------------------------------
# Query construction
# ---------------------------------------------------------------------------


def _select_for(table: str) -> str:
    if table == "message_feed":
        return _MESSAGE_COLUMNS
    if table == "message_filters":
        return _THREAD_COLUMNS
    if table == "external_resources":
        return _RESOURCE_COLUMNS
    return _DISTILLATION_COLUMNS


def _scope_params(
    table: str,
    tokens: list[str],
    *,
    sources: list[str] | None,
    since: str | None,
    limit: int,
    mode: str = "or",
    kind_filter: list[str] | None = None,
    ordered: bool = True,
    title_only: bool = False,
    channel: str | None = None,
    author: str | None = None,
    thread: str | None = None,
) -> dict[str, str] | None:
    """Build PostgREST params for one scope.

    ``mode`` is ``"and"`` (every token must match — high precision, supported
    only where the table is small enough that the intersection is cheap) or
    ``"or"`` (any token may match — recall).  ``ordered`` controls the server
    ``order=created_at.desc`` (dropped when a statement-timeout fallback needs
    the cheaper shape).  ``channel``/``author``/``thread`` apply to the
    message surfaces only (``channel_name=eq``/``author_name=eq`` on
    message_feed, ``thread_id=eq`` on message_filters).  Returns None when
    the sources filter excludes the scope.

    Measured live 2026-08-19 (anon role, 3s statement budget):

      * ``message_feed`` AND of 2+ tokens is 1.9-2.6s+ (3 tokens -> 57014);
        its per-token OR answers in ~0.1-0.5s.  Messages are OR-only, and a
        57014 on the ordered shape falls back to the unordered OR.
      * ``external_resources`` title AND is ~0.12s (small indexed table) —
        AND first; the OR fallback is title+body UNORDERED (~0.3s).  An
        ORDERED title+body OR sorts the huge body-match set (~2.1s, over
        budget), so the recall pass skips the server sort entirely and lets
        client ranking pick the best matches.
      * ``distillations`` is tiny; OR over (question, answer, conditions) is
        instant.

    This PostgREST build rejects ``or:`` nested inside ``and=`` (PGRST100),
    so the AND pass is a flat ``and=`` of canonical tokens on ONE column
    (title for resources); variant spellings are OR-pass-only.
    """
    if not tokens:
        return None

    params: dict[str, str] = {
        "select": _select_for(table),
        "limit": str(limit),
    }
    if ordered:
        params["order"] = "created_at.desc"
    if since:
        params["created_at"] = f"gte.{since}"

    if table == "message_feed":
        if sources is not None and _MESSAGE_SOURCE not in sources:
            return None
        if channel:
            params["channel_name"] = f"eq.{channel}"
        if author:
            params["author_name"] = f"eq.{author}"
        params["or"] = "(" + ",".join(_or_arms(("content",), tokens)) + ")"
        return params

    if table == "message_filters":
        if sources is not None and _MESSAGE_SOURCE not in sources:
            return None
        if thread:
            params["thread_id"] = f"eq.{thread}"
        params["or"] = "(" + ",".join(_or_arms(("content",), tokens)) + ")"
        return params

    if table == "external_resources":
        if sources is not None:
            params["source"] = f"in.({','.join(sources)})"
        if kind_filter:
            params["kind"] = f"in.({','.join(kind_filter)})"
        if mode == "and":
            # Flat AND of canonical tokens on title: precise, index-friendly.
            params["and"] = "(" + ",".join(f"title.ilike.*{t}*" for t in tokens) + ")"
        else:
            # Title+body OR — the recall pass (title-only when the body
            # shape is slow).  Unordered: see the timing note above; client
            # ranking prefers titled hits (+5 vs +3) anyway.
            columns = ("title",) if title_only else ("title", "body")
            params["or"] = "(" + ",".join(_or_arms(columns, tokens)) + ")"
        return params

    if table == "distillations":
        if sources is not None and _DISTILLATION_SOURCE not in sources:
            return None
        # Mirror the unified_feed distillation branch: pending + approved.
        params["status"] = "in.(pending,approved)"
        params["or"] = "(" + ",".join(_or_arms(("question", "answer", "conditions"), tokens)) + ")"
        return params

    return None  # pragma: no cover - unknown scope


def _or_arms(columns: tuple[str, ...], tokens: list[str]) -> list[str]:
    """Flat OR arms for *tokens* across *columns*, variants included.

    The arm budget is shared fairly across tokens (per-token
    ``_MAX_ILIKE_ARMS // len(tokens)``) so one multi-variant token cannot
    starve the rest of the query.
    """
    per_token = max(1, _MAX_ILIKE_ARMS // max(1, len(tokens)))
    arms = [arm for t in tokens for arm in _ilike_arms(columns, t)[:per_token]]
    return arms[:_MAX_ILIKE_ARMS]


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def _query_table(
    table: str,
    params: dict[str, str],
    *,
    endpoint: str,
    anon_key: str,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """Call *postgrest_get* for *table* and return the rows as a list."""
    result = postgrest_get(
        table, params=params, endpoint=endpoint, anon_key=anon_key, timeout=timeout
    )
    if isinstance(result, list):
        return result  # type: ignore[no-any-return]
    if isinstance(result, dict):
        return [result]  # PostgREST returns a single object when limit=1
    return []


def _run_scope(
    table: str,
    tokens: list[str],
    *,
    sources: list[str] | None,
    since: str | None,
    limit: int,
    endpoint: str,
    anon_key: str,
    kind_filter: list[str] | None = None,
    channel: str | None = None,
    author: str | None = None,
    thread: str | None = None,
) -> tuple[list[tuple[str, dict[str, Any]]], list[str]]:
    """Fetch one scope's rows (Pass A AND, Pass B OR fallback).

    Returns ``(stamped_rows, errors)`` where stamped rows are
    ``(table, row)`` pairs.  A scope failure degrades: the error is recorded
    and other scopes still contribute.
    """
    errors: list[str] = []
    rows: list[tuple[str, dict[str, Any]]] = []

    def _fetch(
        mode: str,
        ordered: bool = True,
        timeout: float = 30.0,
        title_only: bool = False,
    ) -> list[tuple[str, dict[str, Any]]]:
        params = _scope_params(
            table,
            tokens,
            sources=sources,
            since=since,
            limit=limit,
            mode=mode,
            kind_filter=kind_filter,
            ordered=ordered,
            title_only=title_only,
            channel=channel,
            author=author,
            thread=thread,
        )
        if params is None:
            return []
        return [
            (table, row)
            for row in _query_table(table, params, endpoint=endpoint, anon_key=anon_key, timeout=timeout)
        ]

    def _or_robust() -> list[tuple[str, dict[str, Any]]]:
        """Recency-ordered OR, bounded client-side, degrading to unordered.

        The backend ``order=created_at.desc`` sort is token-dependent
        (0.1s for ``wan``, ~1.6s for ``upscale``, 2-3s borderline 57014 for
        ``hotshot``).  Bound the ordered attempt; on timeout or
        statement-timeout return the unordered pool (bounded) instead of
        failing the scope.
        """
        try:
            return _fetch("or", ordered=True, timeout=_ORDERED_OR_TIMEOUT_S)
        except (urllib.error.HTTPError, TimeoutError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and not _is_statement_timeout(exc):
                raise
            return _fetch("or", ordered=False, timeout=_UNORDERED_OR_TIMEOUT_S)

    try:
        if table == "external_resources":
            # AND first — high precision.  The AND is fetched UNORDERED:
            # ``order=created_at.desc`` over a common-token match set costs
            # ~1.9s (measured for ``ltx``), the filter itself is identical,
            # and the pool is client-ranked anyway.  Widen with the title+body
            # OR (unordered recall pass) only when the AND was thin and fast;
            # the body shape is itself token-dependent (~0.4s for ``ltx``,
            # ~2.2s for ``hotshot``), so it is bounded and degrades to a
            # title-only OR (~0.16s) instead of stalling the scope.
            start = time.monotonic()
            and_rows = _fetch("and", ordered=False)
            elapsed_ms = (time.monotonic() - start) * 1000.0
            rows.extend(and_rows)
            if len(and_rows) < _AND_MIN_ROWS and elapsed_ms < _AND_MAX_MS:
                try:
                    rows.extend(_fetch("or", ordered=False, timeout=_BODY_OR_TIMEOUT_S))
                except (urllib.error.HTTPError, TimeoutError) as exc:
                    if isinstance(exc, urllib.error.HTTPError) and not _is_statement_timeout(exc):
                        raise
                    rows.extend(_fetch("or", ordered=False, title_only=True))
        elif table == "message_feed":
            # Messages are OR-only (AND times out); use the robust OR.
            rows.extend(_or_robust())
        elif table == "message_filters":
            # Thread-scoped messages: index-backed (schema/036), OR-only.
            rows.extend(_or_robust())
        else:
            # distillations: tiny table, OR with recency order is instant.
            rows.extend(_fetch("or"))
    except urllib.error.HTTPError as exc:
        errors.append(f"{table}: API error {exc.code} {exc.reason}")
    except Exception as exc:  # URLError, JSON decode failure, socket errors
        errors.append(f"{table}: {type(exc).__name__}: {exc}")
    return rows, errors


def _is_statement_timeout(exc: urllib.error.HTTPError) -> bool:
    """True when an HTTPError carries Postgres SQLSTATE 57014.

    PostgREST surfaces the backend statement timeout as HTTP 500 with the
    SQLSTATE in the body — the query is valid, the backend just hit its 3s
    statement budget.
    """
    if exc.code != 500:
        return False
    try:
        body = exc.read(800).decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - defensive
        return False
    return '"57014"' in body or "canceling statement due to statement timeout" in body


def _run_scopes(
    scopes: list[tuple[str, list[str]]],
    *,
    sources: list[str] | None,
    since: str | None,
    limit: int,
    endpoint: str,
    anon_key: str,
    kind_filters: dict[str, list[str]] | None = None,
    channel: str | None = None,
    author: str | None = None,
    thread: str | None = None,
) -> tuple[list[tuple[str, dict[str, Any]]], list[str]]:
    """Run every scope in parallel; merge rows and per-scope errors."""
    results: list[list[tuple[str, dict[str, Any]]]] = [[] for _ in scopes]
    errors: list[list[str]] = [[] for _ in scopes]
    kind_filters = kind_filters or {}

    def _work(index: int) -> None:
        table, tokens = scopes[index]
        try:
            results[index], errors[index] = _run_scope(
                table,
                tokens,
                sources=sources,
                since=since,
                limit=limit,
                endpoint=endpoint,
                anon_key=anon_key,
                kind_filter=kind_filters.get(table),
                channel=channel,
                author=author,
                thread=thread,
            )
        except Exception as exc:  # pragma: no cover - _run_scope catches its own
            # A worker thread never propagates; record the failure so a dead
            # scope is a warning (or an error when every scope died), never a
            # silent empty success.
            errors[index] = [f"{table}: {type(exc).__name__}: {exc}"]

    threads = [threading.Thread(target=_work, args=(i,)) for i in range(len(scopes))]
    for worker in threads:
        worker.start()
    for worker in threads:
        worker.join()

    rows: list[tuple[str, dict[str, Any]]] = []
    for batch in results:
        rows.extend(batch)
    return rows, [message for batch in errors for message in batch]


# ---------------------------------------------------------------------------
# Merge & rank
# ---------------------------------------------------------------------------


def _natural_id(table: str, row: dict[str, Any]) -> str | None:
    """Stable string id for dedupe/ordering (snowflake-safe)."""
    if table in ("message_feed", "message_filters"):
        raw = row.get("message_id")
    else:
        raw = row.get("id")
    return str(raw) if raw is not None else None


def _created_at_ts(row: dict[str, Any]) -> float:
    raw = row.get("created_at")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    if not isinstance(raw, str) or not raw.strip():
        return 0.0
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.timestamp()
    except ValueError:
        return 0.0


def _score_hit(row: dict[str, Any], table: str, tokens: list[str], phrase: str | None) -> int:
    """Deterministic relevance score for one row.

    +5 per distinctive token (or spelling variant of it) in title/question,
    +3 per token in body/content/answer and conditions/context, +4 approved
    distillation, +3 parseable workflow, +2 exact phrase substring.
    Tokens are deduplicated so ``wan wan`` cannot double-count.
    """
    title = str(row.get("title") or row.get("question") or "").casefold()
    body = str(row.get("body") or row.get("content") or row.get("answer") or "").casefold()
    context = str(row.get("context") or row.get("conditions") or "").casefold()
    score = 0
    seen: set[str] = set()
    for token in tokens:
        key = token.casefold()
        if key in seen:
            continue
        seen.add(key)
        for variant in _variants(token):
            needle = variant.casefold()
            if needle in title:
                score += 5
                break
        for variant in _variants(token):
            needle = variant.casefold()
            if needle in body:
                score += 3
                break
        for variant in _variants(token):
            needle = variant.casefold()
            if needle in context:
                score += 3
                break
    if table == "distillations" and str(row.get("status") or "").casefold() == "approved":
        score += 4
    if table == "external_resources":
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        semantics = metadata.get("workflow_semantics")
        if isinstance(semantics, dict):
            gates = semantics.get("promotion_gates")
            if isinstance(gates, dict) and gates.get("parseable_workflow") is True:
                score += 3
        elif metadata.get("has_workflow_json") is True:
            score += 3
    if phrase:
        needle = phrase.casefold()
        if needle in title or needle in body:
            score += 2
    return score


def _clip(text: str) -> str:
    """Collapse whitespace and clip to the search-hit body limit."""
    text = " ".join(text.split())
    if len(text) <= _BODY_LIMIT:
        return text
    return text[: _BODY_LIMIT - 1].rstrip() + "…"


def _shape_hit(row: dict[str, Any], table: str) -> dict[str, Any]:
    """Map a raw-table row onto the unified_feed public shape.

    ``kind/source/item_id/title/body/author/context/url/metadata/created_at``
    so get_item and downstream consumers keep resolving these hits.
    """
    if table == "message_feed":
        message_id = str(row.get("message_id"))
        channel_id = row.get("channel_id")
        guild_id = row.get("guild_id")
        permalink = (
            f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
            if guild_id is not None and channel_id is not None
            else None
        )
        body = row.get("content") or ""
        return {
            "kind": "message",
            "source": _MESSAGE_SOURCE,
            "item_id": message_id,
            "title": None,
            "body": _clip(body),
            "author": row.get("author_name"),
            "context": row.get("channel_name"),
            "url": permalink,
            "metadata": None,
            "created_at": row.get("created_at"),
            "truncated": len(body) > _BODY_LIMIT,
        }
    if table == "message_filters":
        # Thread surface (schema/036): no author_name/channel_name columns.
        message_id = str(row.get("message_id"))
        channel_id = row.get("channel_id")
        guild_id = row.get("guild_id")
        permalink = (
            f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
            if guild_id is not None and channel_id is not None
            else None
        )
        body = row.get("content") or ""
        return {
            "kind": "message",
            "source": _MESSAGE_SOURCE,
            "item_id": message_id,
            "title": None,
            "body": _clip(body),
            "author": None,
            "context": None,
            "url": permalink,
            "metadata": {"thread_id": row.get("thread_id")},
            "created_at": row.get("created_at"),
            "truncated": len(body) > _BODY_LIMIT,
        }
    if table == "distillations":
        body = row.get("answer") or ""
        return {
            "kind": "distillation",
            "source": _DISTILLATION_SOURCE,
            "item_id": str(row.get("id")),
            "title": row.get("question"),
            "body": _clip(body),
            "author": None,
            "context": row.get("conditions"),
            "url": None,
            "metadata": {"status": row.get("status"), "confidence": row.get("confidence")},
            "created_at": row.get("created_at"),
            "truncated": len(body) > _BODY_LIMIT,
        }
    # external_resources
    body = row.get("body") or ""
    return {
        "kind": row.get("kind"),
        "source": row.get("source"),
        "item_id": str(row.get("id")),
        "title": row.get("title"),
        "body": _clip(body),
        "author": row.get("author"),
        "context": None,
        "url": row.get("url"),
        "metadata": row.get("metadata"),
        "created_at": row.get("created_at"),
        "truncated": len(body) > _BODY_LIMIT,
    }


def _annotate_paging(result: dict[str, object], limit: int, offset: int) -> None:
    """Add page/page-count/next-offset to the JSON result and a stderr hint.

    ``next_offset`` lets any consumer page forward without reconstructing
    the offset — re-run the same command with ``--offset <next_offset>``.
    The human summary goes to STDERR so stdout stays a pure JSON contract
    for agents.  ``total`` is the ranked pool extent (bounded by the
    per-scope fetch), not the corpus match count — exact counts on the
    1.28M-row corpus need ``Prefer: count=exact``, which the transport
    deliberately never sends.
    """
    total = int(result.get("total") or 0)
    limit = max(1, limit)
    pages = (total + limit - 1) // limit if total else 0
    page = offset // limit + 1 if total else 0
    result["page"] = page
    result["pages"] = pages
    has_more = bool(result.get("has_more"))
    result["next_offset"] = offset + limit if has_more else None

    shown = len(result["results"])
    if shown:
        start = offset + 1
        end = offset + shown
        line = f"Showing {start}-{end} of {total} results (page {page} of {pages})"
        if has_more:
            line += f" - next: --limit {limit} --offset {offset + limit}"
        else:
            line += " - end of results"
    elif total:
        line = f"No results on this page (offset {offset}) - {total} in pool"
    else:
        line = "No results found"
    print(line, file=sys.stderr)


def _merge_results(
    table_rows: list[tuple[str, dict[str, Any]]],
    tokens: list[str],
    phrase: str | None,
    limit: int,
    *,
    had_distillations: bool,
    offset: int = 0,
    sort: str = "relevance",
) -> dict[str, object]:
    """Dedupe, rank, page, and shape the merged scope rows.

    ``limit``/``offset`` slice the deterministically ranked pool, so
    ``--offset`` pages through the pool (stable across invocations while
    the corpus is unchanged).  ``sort`` is ``"relevance"`` (default:
    score, then recency, then table+id) or ``"recent"`` (created_at desc,
    then score).  The pool itself is bounded by the per-scope fetch limit,
    so deep pages eventually return fewer rows; ``total``/``has_more``
    report the pool extent.
    """
    seen: set[tuple[str, str]] = set()
    scored: list[tuple[int, float, str, str, dict[str, Any]]] = []
    for table, row in table_rows:
        natural = _natural_id(table, row)
        if natural is None:
            continue
        key = (table, natural)
        if key in seen:
            continue
        seen.add(key)
        score = _score_hit(row, table, tokens, phrase)
        recency = _created_at_ts(row)
        if sort == "recent":
            scored.append((recency, score, table, natural, row))
        else:
            scored.append((score, recency, table, natural, row))
    scored.sort(reverse=True)

    page = scored[offset : offset + limit]
    results = [_shape_hit(row, tbl) for _, _, tbl, _, row in page]
    result: dict[str, object] = {
        "results": results,
        "count": len(results),
        "total": len(scored),
        "has_more": offset + len(results) < len(scored),
    }
    if not had_distillations:
        result["nudge"] = _NUDGE
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hivemind.search",
        description="Search the Hivemind corpus (messages, resources, distillations).",
    )
    parser.add_argument("--query", required=True, help="Search query string.")
    parser.add_argument("--kinds", help="Comma-separated kind filter (message,resource,workflow,distillation,...).")
    parser.add_argument("--sources", help="Comma-separated source filter (banodoco-discord,hivemind,youtube,...).")
    parser.add_argument("--since", help="ISO-8601 timestamp lower bound.")
    parser.add_argument(
        "--channel", help="Discord channel name — messages only (e.g. wan_chatter)."
    )
    parser.add_argument(
        "--author", help="Discord author display name — messages only (e.g. Kijai)."
    )
    parser.add_argument(
        "--thread",
        help="Discord thread id (snowflake) — messages in that thread only.",
    )
    parser.add_argument(
        "--limit", type=int, default=20, help="Max results per page (default: 20)."
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Skip this many ranked results (page with --limit; default: 0).",
    )
    parser.add_argument(
        "--sort",
        choices=("relevance", "recent"),
        default="relevance",
        help="Order results by relevance (default: score, then recency) or by recency (created_at desc, then score).",
    )
    parser.add_argument(
        "--out", help="Write JSON output to this file instead of stdout."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    endpoint = resolve_endpoint()
    anon_key = resolve_anon_key()

    user_kinds = [k.strip() for k in args.kinds.split(",")] if args.kinds else None
    user_sources = [s.strip() for s in args.sources.split(",")] if args.sources else None

    want_messages = user_kinds is None or "message" in user_kinds
    want_distillations = user_kinds is None or "distillation" in user_kinds
    want_resources = user_kinds is None or any(
        k not in ("message", "distillation") for k in user_kinds
    )

    # SQL predicate tokens (few, arm-budgeted) vs ranking tokens (more
    # context for the client scorer).
    tokens = _distinctive_tokens(args.query, _SQL_TOKEN_CAP)
    rank_tokens = _distinctive_tokens(args.query, _RANK_TOKEN_CAP)
    phrase = _phrase_tokens(args.query)

    if not tokens:
        output_json(
            {"error": "query contains no searchable tokens (letters or numbers)"},
            args.out,
        )
        return 2

    scopes: list[tuple[str, list[str]]] = []
    if args.thread:
        # Thread search uses the index-backed message_filters surface.
        if not want_messages:
            output_json(
                {"error": "--thread searches messages; use --kinds message (or omit --kinds)"},
                args.out,
            )
            return 2
        scopes.append(("message_filters", tokens))
    elif args.channel or args.author:
        # Channel/author filters exist only on message_feed; the other
        # scopes are skipped rather than guessed at.
        if not want_messages:
            output_json(
                {"error": "--channel/--author search messages; use --kinds message (or omit --kinds)"},
                args.out,
            )
            return 2
        scopes.append(("message_feed", tokens))
    else:
        if want_messages:
            scopes.append(("message_feed", tokens))
        if want_resources:
            scopes.append(("external_resources", tokens))
        if want_distillations:
            scopes.append(("distillations", tokens))
    if not scopes:
        output_json({"error": "no searchable kinds selected"}, args.out)
        return 2

    kind_filters: dict[str, list[str]] = {}
    if user_kinds is not None and want_resources:
        kind_filter = _resource_kind_filter(user_kinds)
        if kind_filter:
            kind_filters["external_resources"] = kind_filter

    raw_rows, errors = _run_scopes(
        scopes,
        sources=user_sources,
        since=args.since,
        limit=_POOL_LIMIT,
        endpoint=endpoint,
        anon_key=anon_key,
        kind_filters=kind_filters,
        channel=args.channel,
        author=args.author,
        thread=args.thread,
    )

    if not raw_rows and errors:
        output_json({"error": "; ".join(errors)}, args.out)
        return 2

    had_distillations = any(table == "distillations" for table, _ in raw_rows)
    merged = _merge_results(
        raw_rows,
        rank_tokens,
        phrase,
        max(1, args.limit),
        had_distillations=had_distillations,
        offset=max(0, args.offset),
        sort=args.sort,
    )
    if errors:
        merged["warnings"] = errors
    _annotate_paging(merged, max(1, args.limit), max(0, args.offset))
    output_json(merged, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
