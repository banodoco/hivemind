"""Direct-session, resumable historical embedding backfill engine (plan 2.12b).

Pure, synchronous, injected-interface engine. No network/provider construction,
no database driver, no filesystem writes, no environment reads, no real sleeps.
The provider, durable run store, remediation stage, and rate limiter are all
injected; this module only orchestrates them so the public surface is fully
deterministic and offline-testable.

It reuses :mod:`executors.canonical_representations` and
:mod:`executors.chunking` verbatim — it does NOT duplicate the representation or
chunking algorithms. Item ids and cursors are preserved as opaque strings
(snowflake-safe). Reports carry only ids/counts/categories/cursors/timing —
never credentials, raw content, Python source, exception text, or DB URLs.

The engine never touches source-row mutation, contract activation, or HNSW
surfaces; those exist on the injected store only so a production adapter (next
slice) can implement them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence
from urllib.parse import parse_qsl, urlsplit

from executors import canonical_representations as canon
from executors import chunking as ch
from executors import embedding_contract as ec
from executors.selected_contract import (
    SELECTED_CONTRACT_ID,
    SELECTED_DIMENSION,
)

__all__ = [
    "REASON_REMEDIATION_FAILED",
    "REASON_TRANSIENT",
    "REASON_PERMANENT",
    "REASON_EXHAUSTED",
    "REASON_SKIPPED_CURRENT",
    "REASON_UNAVAILABLE",
    "REASON_QUARANTINED",
    "REASON_CHECKPOINT_FAILED",
    "REASON_INTERRUPTED",
    "REASON_URL_BLANK",
    "REASON_URL_BAD_SCHEME",
    "REASON_URL_TRANSACTION_POOLER",
    "REASON_URL_TRANSACTION_MODE",
    "DirectDatabaseURLRejected",
    "TransientProviderError",
    "PermanentProviderError",
    "validate_direct_database_url",
    "BackfillConfig",
    "BackfillReport",
    "RemediationReport",
    "BackfillStore",
    "Provider",
    "RateLimiter",
    "RemediationStage",
    "run_backfill",
]


# ---------------------------------------------------------------------------
# Nonsecret reason vocabulary (frozen)
# ---------------------------------------------------------------------------

REASON_REMEDIATION_FAILED = "remediation_failed"
REASON_TRANSIENT = "transient_provider_error"
REASON_PERMANENT = "permanent_provider_error"
REASON_EXHAUSTED = "retries_exhausted"
REASON_SKIPPED_CURRENT = "skipped_current"
REASON_UNAVAILABLE = "representation_unavailable"
REASON_QUARANTINED = "quarantined_python_excluded"
REASON_CHECKPOINT_FAILED = "checkpoint_failed"
REASON_INTERRUPTED = "interrupted"
REASON_COST_CAP = "cost_cap_reached"

# text-embedding-3-small input pricing used only for a conservative operator
# stop gate.  This is deliberately an estimate (tokenizer-independent chunk
# estimates); it is not persisted as billing truth.
DEFAULT_INPUT_COST_PER_MILLION_TOKENS_USD = 0.02

REASON_URL_BLANK = "url_blank"
REASON_URL_BAD_SCHEME = "url_bad_scheme"
REASON_URL_TRANSACTION_POOLER = "url_transaction_pooler"
REASON_URL_TRANSACTION_MODE = "url_transaction_mode"


# ---------------------------------------------------------------------------
# Classified provider failures (never retain exception text)
# ---------------------------------------------------------------------------


class TransientProviderError(Exception):
    """A retryable provider failure (rate limit, transient HTTP, timeout).

    Classified by type only; the message may contain provider context and is
    NEVER retained in reports or logs.
    """


class PermanentProviderError(Exception):
    """A non-retryable provider failure (bad request, bad dimension).

    Classified by type only; the message is never retained.
    """


# ---------------------------------------------------------------------------
# Direct-session database URL validation (fail closed, never echo credentials)
# ---------------------------------------------------------------------------


class DirectDatabaseURLRejected(Exception):
    """Raised when a database URL is not a direct/session PostgreSQL connection.

    Carries only the nonsecret ``reason_code`` — never the rejected URL, host,
    or any credential substring.
    """

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


_DIRECT_SCHEMES = ("postgres", "postgresql")
_TRANSACTION_POOLER_PORT = 6543


def validate_direct_database_url(url: Any) -> str:
    """Validate that *url* is a direct/session PostgreSQL connection string.

    Accepts ``postgres``/``postgresql`` direct or session-pooler (port 5432)
    URLs. Rejects Supabase transaction-pooler endpoints (port 6543), transaction
    pooling query modes (``pgbouncer=true`` / ``pool_mode=transaction``),
    unsupported schemes, and blank URLs. The rejection carries only a reason
    code — credentials are never echoed back.
    """

    if url is None or not str(url).strip():
        raise DirectDatabaseURLRejected(REASON_URL_BLANK)
    raw = str(url)
    parts = urlsplit(raw)
    if parts.scheme.lower() not in _DIRECT_SCHEMES:
        raise DirectDatabaseURLRejected(REASON_URL_BAD_SCHEME)
    if parts.port == _TRANSACTION_POOLER_PORT:
        raise DirectDatabaseURLRejected(REASON_URL_TRANSACTION_POOLER)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    pgbouncer = str(query.get("pgbouncer", "")).strip().lower()
    pool_mode = str(query.get("pool_mode", "")).strip().lower()
    if pgbouncer in ("1", "true", "yes", "on") or pool_mode == "transaction":
        raise DirectDatabaseURLRejected(REASON_URL_TRANSACTION_MODE)
    return raw


# ---------------------------------------------------------------------------
# Config + reports
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackfillConfig:
    """Frozen configuration for one backfill invocation."""

    sources: tuple[str, ...]
    apply: bool = False
    resume_run_id: int | None = None
    since: str | None = None
    until: str | None = None
    batch_items: int = 100
    input_items: int = 64
    max_tokens_per_request: int = 8192
    max_chars_per_request: int | None = None
    rate_limit_per_minute: int | None = None
    cost_cap_usd: float | None = None
    max_retries: int = 2
    sample: int | None = None
    report_path: str | None = None
    # The historical bigint is dimension-only and is intentionally not a
    # valid default for this selected full-config backfill.
    contract_id: int = SELECTED_CONTRACT_ID
    dimension: int = SELECTED_DIMENSION

    def __post_init__(self) -> None:
        if not self.sources:
            raise ValueError("BackfillConfig.sources must be non-empty")
        if self.batch_items < 1:
            raise ValueError("batch_items must be positive")
        if self.input_items < 1:
            raise ValueError("input_items must be positive")
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")


@dataclass(frozen=True)
class RemediationReport:
    """Result of the pre-backfill remediation stage (secret-safe summary)."""

    ok: bool
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass
class BackfillReport:
    """Secret-safe aggregate backfill result.

    Only ids/counts/categories/cursors/timing are exposed. ``to_log_dict`` never
    emits credentials, raw content, Python source, exception text, or DB URLs.
    """

    status: str
    mode: str
    run_id: int | None
    interrupted: bool
    reason_counts: dict[str, int]
    per_source: dict[str, dict[str, int]]
    chunks_built: int
    vectors_written: int
    cursors: dict[str, str | None]
    elapsed_s: float = 0.0

    def to_log_dict(self) -> dict[str, Any]:
        """Return a secret-safe dict of ids/counts/categories/cursors/timing."""

        return {
            "status": self.status,
            "mode": self.mode,
            "run_id": self.run_id,
            "interrupted": self.interrupted,
            "reason_counts": dict(self.reason_counts),
            "per_source": {
                source: dict(counts) for source, counts in self.per_source.items()
            },
            "chunks_built": self.chunks_built,
            "vectors_written": self.vectors_written,
            "cursors": dict(self.cursors),
            "elapsed_s": self.elapsed_s,
        }


# ---------------------------------------------------------------------------
# Injected collaborator protocols
# ---------------------------------------------------------------------------


class Provider(Protocol):
    """Injected embedding provider. ``embed(texts) -> list[list[float]]``."""

    model_name: str
    dimension: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class RateLimiter(Protocol):
    """Injected pacer/clock: ``acquire`` paces, ``sleep`` backs off, ``now`` clocks."""

    def acquire(self) -> None: ...

    def sleep(self, seconds: float) -> None: ...

    def now(self) -> float: ...


class RemediationStage(Protocol):
    """Injected pre-backfill remediation stage (task 2.12a surface)."""

    def run(self, store: "BackfillStore", *, apply: bool) -> RemediationReport: ...


class BackfillStore(Protocol):
    """Injected durable run + vector store (production: Supabase; tests: fake).

    The ``build_hnsw_index`` / ``enable_hnsw`` (HNSW), ``patch_source_row`` /
    ``refresh_source_row`` (source mutation), and ``activate_contract`` surfaces
    are intentionally part of the protocol so a production adapter can implement
    them — the engine itself NEVER calls them.
    """

    def sources(self) -> tuple[str, ...]: ...
    def high_water(self, source: str) -> str | None: ...
    def eligible_total(self, source: str) -> int: ...
    def fetch_page(
        self, source: str, *, cursor: str | None, limit: int,
        since: str | None = None, until: str | None = None,
    ) -> list[dict[str, Any]]: ...
    def current_freshness(
        self, source: str, keys: set[tuple[str, str, str, int]]
    ) -> dict[tuple[str, str, str, int], str]: ...
    # Optional stronger freshness hook.  A production store must compare the
    # complete stored identity/hash set for every representation, not merely
    # the expected keys (which would miss stale tail chunks).  The legacy
    # key-level hook above remains for small injected test stores.
    def representation_current(
        self, source: str, expected: dict[tuple[str, str, str], list[ch.EmbeddingChunk]]
    ) -> dict[tuple[str, str, str], bool]: ...
    def write_vectors(self, source: str, rows: list[dict[str, Any]]) -> None: ...
    def create_run(
        self, *, contract_id: int, sources: tuple[str, ...], mode: str,
        high_water: dict[str, str | None],
    ) -> dict[str, Any]: ...
    def load_run(self, run_id: int) -> dict[str, Any]: ...
    def checkpoint(
        self, run_id: int, source: str, *, expected_version: int,
        cursor: str | None, high_water: str | None, processed: int, skipped: int,
        quarantined: int, unavailable: int, failed: int, eligible: int,
        last_error: str | None,
    ) -> dict[str, Any]: ...
    def complete_run(self, run_id: int, expected_version: int) -> dict[str, Any]: ...
    def fail_run(
        self, run_id: int, expected_version: int, error: str | None
    ) -> dict[str, Any]: ...
    # NEVER called by the engine:
    def build_hnsw_index(self, *args: Any, **kwargs: Any) -> None: ...
    def enable_hnsw(self, *args: Any, **kwargs: Any) -> None: ...
    def patch_source_row(self, *args: Any, **kwargs: Any) -> None: ...
    def refresh_source_row(self, *args: Any, **kwargs: Any) -> None: ...
    def activate_contract(self, *args: Any, **kwargs: Any) -> None: ...
    # Task 2.17 OPTIONAL surfaces (duck-typed via getattr in _apply; a store that
    # does not implement them is unaffected). write_manifest persists the canonical
    # chunk manifest (the worker payload source of truth); drop_representation
    # removes a representation's stale vectors when its Python is now
    # quarantined/unavailable (defect #6). Both are no-ops on a pre-034 cluster.
    def write_manifest(self, source: str, entries: list[Any]) -> None: ...
    def drop_representation(self, source: str, entity_type: str, item_id: str, representation_type: str) -> None: ...


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


@dataclass
class _ChunkRecord:
    """One pending chunk paired with its source row and provider-facing text."""

    chunk: ch.EmbeddingChunk
    row: dict[str, Any]
    send_text: str
    tokens: int


def _backoff_seconds(attempt: int) -> float:
    """Bounded injected-sleep backoff (no real sleep; paced by the limiter)."""

    return 0.1 * (2 ** attempt)


def _embed_with_retry(
    provider: Provider, texts: list[str], limiter: RateLimiter, config: BackfillConfig
) -> tuple[str, list[list[float]] | None]:
    """Embed one batch with bounded retry.

    Returns ``("ok", vectors)`` on success, ``("permanent", None)`` for a
    classified permanent failure, ``("exhausted", None)`` when transient retries
    are exhausted, and ``("interrupted", None)`` for a cooperative interrupt.
    Exception text is never retained — only the classified category.
    """

    attempts = config.max_retries + 1
    for attempt in range(attempts):
        limiter.acquire()
        try:
            vectors = provider.embed(texts)
        except KeyboardInterrupt:
            return ("interrupted", None)
        except PermanentProviderError:
            return ("permanent", None)
        except TransientProviderError:
            if attempt < config.max_retries:
                limiter.sleep(_backoff_seconds(attempt))
                continue
            return ("exhausted", None)
        except Exception:
            # Unknown failures are treated as transient (retryable). They are
            # never retained; only the category reaches the report.
            if attempt < config.max_retries:
                limiter.sleep(_backoff_seconds(attempt))
                continue
            return ("exhausted", None)
        return ("ok", vectors)
    return ("exhausted", None)


def _make_batches(
    records: list[_ChunkRecord], config: BackfillConfig
) -> list[list[_ChunkRecord]]:
    """Split pending chunks into provider batches under all limits.

    Batches respect the per-request item count, token budget, and character
    budget. A single canonical chunk that alone exceeds a batching limit is sent
    intact on its own: limits never truncate the bytes whose hashes are stored.
    The chunk-to-vector positional mapping is
    never split within a batch, so each batch maps its vectors back exactly.
    """

    batches: list[list[_ChunkRecord]] = []
    batch: list[_ChunkRecord] = []
    items: set[tuple[str, str]] = set()
    tokens = 0
    chars = 0
    for rec in records:
        item_key = (rec.chunk.entity_type, rec.chunk.item_id)
        new_item = item_key not in items
        next_items = len(items) + (1 if new_item else 0)
        over_items = bool(config.input_items) and next_items > config.input_items
        over_tokens = (
            bool(config.max_tokens_per_request)
            and (tokens + rec.tokens) > config.max_tokens_per_request
        )
        over_chars = (
            bool(config.max_chars_per_request)
            and (chars + len(rec.send_text)) > config.max_chars_per_request
        )
        if batch and (over_items or over_tokens or over_chars):
            batches.append(batch)
            batch = []
            items = set()
            tokens = 0
            chars = 0
        batch.append(rec)
        items.add(item_key)
        tokens += rec.tokens
        chars += len(rec.send_text)
    if batch:
        batches.append(batch)
    return batches


def _build_chunks(row: dict[str, Any]) -> tuple[list[ch.EmbeddingChunk], bool, bool]:
    """Build canonical chunks for *row* reusing the frozen representation+chunker.

    Returns ``(chunks, quarantined_python, unavailable_python)`` so the engine
    can count the nonsecret reason categories without re-scanning text.
    """

    entity_type = row.get("entity_type")
    reps = canon.build_representations(row, entity_type, item_id=row.get("item_id"))
    chunks = ch.chunk_representations(reps)
    quarantined = False
    unavailable = False
    if entity_type == "resource" and (row.get("kind") == "workflow"):
        status = canon.workflow_python_status(row)
        if status.available and not status.has_representation:
            quarantined = True
        elif not status.available:
            unavailable = True
    elif not chunks:
        unavailable = True
    return chunks, quarantined, unavailable


def _drop_representations(row: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Representations whose vectors must be dropped (task-2.17 defect #6).

    A workflow whose authoritative Python is now quarantined or unavailable must
    not leave its prior workflow_python vectors in place. The canonical manifest
    emits a state-only (chunk_count == 0) workflow_python entry for exactly those
    cases; the engine drops their stored vectors so stale code cannot rank.
    """

    entity_type = row.get("entity_type")
    if entity_type != "resource" or (row.get("kind") != "workflow"):
        return []
    drop: list[tuple[str, str, str]] = []
    for entry in ch.build_representation_manifest(row, entity_type, item_id=row.get("item_id")):
        if entry.representation_type == ch.REP_WORKFLOW_PYTHON and entry.chunk_count == 0:
            drop.append((entry.entity_type, entry.item_id, entry.representation_type))
    return drop


def run_backfill(
    store: BackfillStore,
    *,
    provider: Provider,
    remediation_stage: RemediationStage,
    config: BackfillConfig,
    rate_limiter: RateLimiter,
) -> BackfillReport:
    """Run the backfill engine over the injected store.

    Remediation runs first (apply flag passed through); its failure aborts all
    downstream work. Dry-run (default) inventories chunks/counts without any
    provider call, vector write, durable run, checkpoint, contract activation,
    or HNSW. Apply mode creates/resumes the durable run, paces every provider
    request through the limiter, writes-then-checkpoints each successful batch,
    and never advances past an uncommitted failed item.
    """

    start = rate_limiter.now()
    reason_counts: dict[str, int] = {}

    # The real adapter validates the existing, non-active contract and the
    # physical vector(384) table before remediation can lead to provider work.
    # Kept duck-typed so pure unit stores remain minimal.
    validator = getattr(store, "validate_contract", None)
    if config.apply and callable(validator):
        try:
            if int(getattr(provider, "dimension", -1)) != int(config.dimension):
                raise ValueError("provider_dimension_incompatible")
            validator(
                contract_id=config.contract_id, dimension=config.dimension,
                provider=getattr(provider, "provider_name", "openai"),
                model=getattr(provider, "model_name", ""),
            )
        except (TypeError, ValueError, RuntimeError):
            return BackfillReport(
                status="failed", mode="apply", run_id=None, interrupted=False,
                reason_counts={REASON_PERMANENT: 1},
                per_source={s: {} for s in config.sources}, chunks_built=0,
                vectors_written=0, cursors={s: None for s in config.sources},
                elapsed_s=rate_limiter.now() - start,
            )

    # 1. Remediation runs before any provider request or durable run.
    remediation = remediation_stage.run(store, apply=config.apply)
    if not remediation.ok:
        reason_counts[REASON_REMEDIATION_FAILED] = 1
        return BackfillReport(
            status="failed",
            mode="apply" if config.apply else "dry_run",
            run_id=None,
            interrupted=False,
            reason_counts=reason_counts,
            per_source={s: {} for s in config.sources},
            chunks_built=0,
            vectors_written=0,
            cursors={s: None for s in config.sources},
            elapsed_s=rate_limiter.now() - start,
        )

    if not config.apply:
        return _dry_run(store, config, rate_limiter, start)
    return _apply(store, provider, config, rate_limiter, start)


def _dry_run(
    store: BackfillStore, config: BackfillConfig, limiter: RateLimiter, start: float
) -> BackfillReport:
    """Inventory chunks/counts only — no provider, vectors, runs, or HNSW."""

    per_source: dict[str, dict[str, int]] = {
        s: {
            "eligible": 0, "processed": 0, "skipped": 0,
            "quarantined": 0, "unavailable": 0, "failed": 0,
        }
        for s in config.sources
    }
    reason_counts: dict[str, int] = {}
    chunks_built = 0
    cursors: dict[str, str | None] = {}

    # Real direct/session stores expose an optional snapshot hook.  Keeping it
    # duck-typed preserves the accepted pure-store API while ensuring a dry-run
    # inventories the same bounded population as an apply run.
    bounds = {source: store.high_water(source) for source in config.sources}
    setter = getattr(store, "set_snapshot_bounds", None)
    if callable(setter):
        setter(bounds)

    for source in config.sources:
        cursor: str | None = None
        while True:
            page = store.fetch_page(
                source, cursor=cursor, limit=config.batch_items,
                since=config.since, until=config.until,
            )
            if not page:
                break
            for row in page:
                if config.sample is not None and per_source[source]["eligible"] >= config.sample:
                    break
                chunks, quarantined, unavailable = _build_chunks(row)
                chunks_built += len(chunks)
                per_source[source]["eligible"] += 1
                if quarantined:
                    per_source[source]["quarantined"] += 1
                    reason_counts[REASON_QUARANTINED] = (
                        reason_counts.get(REASON_QUARANTINED, 0) + 1
                    )
                if unavailable:
                    per_source[source]["unavailable"] += 1
            cursor = str(page[-1]["item_id"])
            if config.sample is not None and per_source[source]["eligible"] >= config.sample:
                break
        cursors[source] = cursor

    return BackfillReport(
        status="completed",
        mode="dry_run",
        run_id=None,
        interrupted=False,
        reason_counts=reason_counts,
        per_source=per_source,
        chunks_built=chunks_built,
        vectors_written=0,
        cursors=cursors,
        elapsed_s=limiter.now() - start,
    )


def _apply(
    store: BackfillStore, provider: Provider, config: BackfillConfig,
    limiter: RateLimiter, start: float,
) -> BackfillReport:
    """Create/resume the durable run with item checkpoints.

    Provider requests are deliberately allowed to split a long representation,
    but its vectors are held in memory until *all* of its canonical chunks have
    succeeded.  ``write_vectors`` therefore receives a complete representation
    replacement, and an item cursor is advanced only after every applicable
    representation is durable.  This is the boundary that prevents stale-tail
    deletion and partial-item checkpoints.
    """

    sources = config.sources
    high_water = {s: store.high_water(s) for s in sources}
    if config.resume_run_id is not None:
        run = store.load_run(config.resume_run_id)
        run_id = int(run["run_id"])
        run_version = int(run.get("version", 1))
        cursors: dict[str, str | None] = {
            s: run.get("cursors", {}).get(s) for s in sources
        }
        high_water = {
            s: run.get("high_water", {}).get(s, high_water.get(s)) for s in sources
        }
        persisted_counters = run.get("counters", {})
    else:
        run = store.create_run(
            contract_id=config.contract_id, sources=sources, mode="apply",
            high_water=high_water,
        )
        run_id = int(run["run_id"])
        run_version = int(run.get("version", 1))
        cursors = {s: run.get("cursors", {}).get(s) for s in sources}
        persisted_counters = {}

    setter = getattr(store, "set_snapshot_bounds", None)
    if callable(setter):
        setter(high_water)

    per_source: dict[str, dict[str, int]] = {
        s: {
            "eligible": 0, "processed": 0, "skipped": 0,
            "quarantined": 0, "unavailable": 0, "failed": 0,
            **{str(k): int(v) for k, v in dict(persisted_counters.get(s, {})).items()
               if str(k) in {"eligible", "processed", "skipped", "quarantined", "unavailable", "failed"}},
        }
        for s in sources
    }
    reason_counts: dict[str, int] = {}
    chunks_built = 0
    vectors_written = 0
    interrupted = False
    status = "completed"
    stop = False
    estimated_spend_usd = 0.0

    for source in sources:
        if stop:
            break
        cursor = cursors.get(source)
        while True:
            page = store.fetch_page(
                source, cursor=cursor, limit=config.batch_items,
                since=config.since, until=config.until,
            )
            if not page:
                break

            page_failed = False
            for row in page:
                if config.sample is not None and per_source[source]["eligible"] >= config.sample:
                    break
                chunks, quarantined, unavailable = _build_chunks(row)
                chunks_built += len(chunks)
                # Group expected chunks by representation.  Empty Python is
                # intentionally absent, while prose remains eligible.
                expected: dict[tuple[str, str, str], list[ch.EmbeddingChunk]] = {}
                for chunk in chunks:
                    expected.setdefault((chunk.entity_type, chunk.item_id, chunk.representation_type), []).append(chunk)

                # Task 2.17: (a) persist the canonical chunk manifest (the worker
                # payload source of truth — backfill and worker then embed
                # byte-identical chunks); (b) drop representations whose Python
                # is now quarantined/unavailable so stale code cannot rank
                # (defect #6). Both are optional, duck-typed store methods so
                # pure fake stores are unaffected.
                manifest_writer = getattr(store, "write_manifest", None)
                if callable(manifest_writer):
                    manifest_writer(
                        source,
                        ch.build_representation_manifest(
                            row, row.get("entity_type"), item_id=row.get("item_id")
                        ),
                    )
                dropper = getattr(store, "drop_representation", None)
                if callable(dropper):
                    for det, did, drep in _drop_representations(row):
                        dropper(source, det, did, drep)

                strong = getattr(store, "representation_current", None)
                if callable(strong):
                    current = strong(source, expected)
                else:
                    keys = {(c.entity_type, c.item_id, c.representation_type, c.chunk_index) for c in chunks}
                    known = store.current_freshness(source, keys) if keys else {}
                    current = {
                        identity: bool(rep_chunks) and all(
                            known.get((c.entity_type, c.item_id, c.representation_type, c.chunk_index)) == c.representation_hash
                            for c in rep_chunks
                        )
                        for identity, rep_chunks in expected.items()
                    }

                item_vectors = 0
                item_failed = False
                item_reason: str | None = None
                stale_reps = [identity for identity in expected if not current.get(identity, False)]
                for identity in stale_reps:
                    records = [_ChunkRecord(chunk=c, row=row, send_text=c.text, tokens=c.token_estimate)
                               for c in expected[identity]]
                    completed: list[dict[str, Any]] = []
                    for batch in _make_batches(records, config):
                        estimated_batch_cost = (
                            sum(record.tokens for record in batch)
                            * DEFAULT_INPUT_COST_PER_MILLION_TOKENS_USD / 1_000_000.0
                        )
                        if config.cost_cap_usd is not None and estimated_spend_usd + estimated_batch_cost > config.cost_cap_usd:
                            status = "stopped_cost_cap"
                            reason_counts[REASON_COST_CAP] = reason_counts.get(REASON_COST_CAP, 0) + 1
                            item_failed, item_reason = True, REASON_COST_CAP
                            break
                        category, vectors = _embed_with_retry(provider, [r.send_text for r in batch], limiter, config)
                        if category == "interrupted":
                            interrupted, status = True, "interrupted"
                            reason_counts[REASON_INTERRUPTED] = reason_counts.get(REASON_INTERRUPTED, 0) + 1
                            item_failed, item_reason = True, REASON_INTERRUPTED
                            break
                        if category in {"permanent", "exhausted"}:
                            status = "failed"
                            code = REASON_PERMANENT if category == "permanent" else REASON_EXHAUSTED
                            reason_counts[code] = reason_counts.get(code, 0) + 1
                            item_failed, item_reason = True, code
                            break
                        # A malformed provider response is deterministic and is
                        # never zip-shortened into an apparently valid write.
                        if vectors is None or len(vectors) != len(batch):
                            status = "failed"
                            reason_counts[REASON_PERMANENT] = reason_counts.get(REASON_PERMANENT, 0) + 1
                            item_failed, item_reason = True, REASON_PERMANENT
                            break
                        try:
                            checked = ec.validate_vectors(vectors, dimension=int(provider.dimension))
                        except (TypeError, ValueError):
                            status = "failed"
                            reason_counts[REASON_PERMANENT] = reason_counts.get(REASON_PERMANENT, 0) + 1
                            item_failed, item_reason = True, REASON_PERMANENT
                            break
                        for rec, vector in zip(batch, checked):
                            c = rec.chunk
                            completed.append({
                                "contract_id": config.contract_id, "entity_type": c.entity_type,
                                "item_id": c.item_id, "representation_type": c.representation_type,
                                "chunk_index": c.chunk_index, "chunk_text": c.text,
                                "embedding": vector, "representation_hash": c.representation_hash,
                                "chunk_hash": c.chunk_hash,
                            })
                        estimated_spend_usd += estimated_batch_cost
                    if item_failed:
                        break
                    # This call is representation-atomic: it receives every
                    # chunk only after all provider batches have succeeded.
                    store.write_vectors(source, completed)
                    item_vectors += len(completed)
                if item_failed:
                    # Do not start another representation of this item.
                    pass

                if item_failed:
                    per_source[source]["failed"] += 1
                    # Persist a failure delta without advancing the item cursor;
                    # a later resume replays the intact item idempotently.
                    resp = store.checkpoint(
                        run_id, source, expected_version=run_version, cursor=cursor,
                        high_water=None, processed=0, skipped=0, quarantined=0,
                        unavailable=0, failed=1, eligible=0, last_error=item_reason,
                    )
                    if resp.get("ok"):
                        run_version = int(resp.get("version", run_version))
                    else:
                        reason_counts[REASON_CHECKPOINT_FAILED] = reason_counts.get(REASON_CHECKPOINT_FAILED, 0) + 1
                    page_failed = True
                    break

                all_current = bool(expected) and not stale_reps
                eligible_delta = 1
                skipped_delta = 1 if all_current else 0
                quarantined_delta = 1 if quarantined else 0
                unavailable_delta = 1 if unavailable else 0
                if skipped_delta:
                    per_source[source]["skipped"] += 1
                    reason_counts[REASON_SKIPPED_CURRENT] = reason_counts.get(REASON_SKIPPED_CURRENT, 0) + 1
                if quarantined_delta:
                    per_source[source]["quarantined"] += 1
                    reason_counts[REASON_QUARANTINED] = reason_counts.get(REASON_QUARANTINED, 0) + 1
                if unavailable_delta:
                    per_source[source]["unavailable"] += 1
                    reason_counts[REASON_UNAVAILABLE] = reason_counts.get(REASON_UNAVAILABLE, 0) + 1
                per_source[source]["eligible"] += 1
                per_source[source]["processed"] += 1
                cursor = str(row["item_id"])
                resp = store.checkpoint(
                    run_id, source, expected_version=run_version, cursor=cursor,
                    high_water=high_water.get(source), processed=1, skipped=skipped_delta,
                    quarantined=quarantined_delta, unavailable=unavailable_delta,
                    failed=0, eligible=eligible_delta, last_error=None,
                )
                if not resp.get("ok"):
                    status = "failed"
                    reason_counts[REASON_CHECKPOINT_FAILED] = (
                        reason_counts.get(REASON_CHECKPOINT_FAILED, 0) + 1
                    )
                    page_failed = True
                    break
                run_version = int(resp.get("version", run_version))
                vectors_written += item_vectors

            if page_failed:
                stop = True
                break

            if config.sample is not None and per_source[source]["eligible"] >= config.sample:
                break
        cursors[source] = cursor

    if not stop and status == "completed":
        resp = store.complete_run(run_id, expected_version=run_version)
        run_version = int(resp.get("version", run_version))

    return BackfillReport(
        status=status,
        mode="apply",
        run_id=run_id,
        interrupted=interrupted,
        reason_counts=reason_counts,
        per_source=per_source,
        chunks_built=chunks_built,
        vectors_written=vectors_written,
        cursors=cursors,
        elapsed_s=limiter.now() - start,
    )
