"""Red tests for Hivemind plan task 2.12b — the direct-session, resumable
historical embedding backfill engine and CLI.

These tests freeze a coherent PUBLIC API for two modules that do not exist yet:

  * ``executors.embedding_backfill`` — the engine.
  * ``scripts.backfill_embeddings`` — the CLI.

The suite is intentionally RED: importing those modules fails until they land,
which clearly identifies the missing implementation. Everything else is pure
stdlib with deterministic fakes — no network, no production, no real provider,
no sleeps, no filesystem writes.

Frozen engine surface (what the production adapter in the next slice implements):

  * ``validate_direct_database_url(url) -> str`` and
    ``DirectDatabaseURLRejected`` — accept ordinary direct/session PostgreSQL
    URLs; reject Supabase transaction-pooler endpoints, transaction pooling
    query modes, missing/unsupported schemes, and blank URLs — never echoing
    credentials.
  * Reason-code vocabulary: ``REASON_REMEDIATION_FAILED``, ``REASON_TRANSIENT``,
    ``REASON_PERMANENT``, ``REASON_EXHAUSTED``, ``REASON_SKIPPED_CURRENT``,
    ``REASON_UNAVAILABLE``, ``REASON_QUARANTINED``, ``REASON_CHECKPOINT_FAILED``,
    ``REASON_INTERRUPTED`` and the URL codes
    ``REASON_URL_BLANK`` / ``REASON_URL_BAD_SCHEME`` /
    ``REASON_URL_TRANSACTION_POOLER`` / ``REASON_URL_TRANSACTION_MODE``.
  * ``TransientProviderError`` / ``PermanentProviderError`` — classified,
    nonsecret provider failures.
  * ``BackfillConfig`` (frozen dataclass) — sources, apply, resume_run_id,
    since, until, batch_items, input_items, max_tokens_per_request,
    max_chars_per_request, rate_limit_per_minute, cost_cap_usd, max_retries,
    sample, report_path, contract_id, dimension.
  * ``BackfillReport`` — secret-safe aggregate result with ``to_log_dict()``.
  * ``RemediationReport`` — ``ok`` + secret-safe summary.
  * ``run_backfill(store, *, provider, remediation_stage, config, rate_limiter)``
    — the engine entry point. Dry-run is the default.
  * The injected store exposes: ``sources``, ``high_water``, ``eligible_total``,
    ``fetch_page``, ``current_freshness``, ``write_vectors``, ``create_run``,
    ``load_run``, ``checkpoint``, ``complete_run``, ``fail_run`` plus the
    NEVER-called surfaces ``build_hnsw_index`` / ``enable_hnsw`` (HNSW),
    ``patch_source_row`` / ``refresh_source_row`` (source mutation), and
    ``activate_contract``.

Frozen CLI surface:

  * ``build_parser()``, ``parse_args(argv)``, ``run(args, *, store=None,
    provider=None, remediation_stage=None, rate_limiter=None)``. ``run``
    validates direct-session mode before any work and accepts the injected
    stage for tests.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))

from executors import canonical_representations as canon  # noqa: E402
from executors import chunking as ch  # noqa: E402
from executors import embedding_contract as ec  # noqa: E402
from executors import embedding_backfill as eb  # noqa: E402  (RED until 2.12b lands)
from scripts import backfill_embeddings as cli  # noqa: E402  (RED until 2.12b lands)


# ---------------------------------------------------------------------------
# Deterministic fakes (no network, no sleeps, no filesystem)
# ---------------------------------------------------------------------------


def _fake_vector(text: str, dimension: int) -> list[float]:
    """Return a deterministic, valid test vector for every input text.

    The frozen contract fake is the default.  Its signed token projection can
    cancel to zero in this suite's deliberately tiny eight-bucket vectors, so
    use a fixed unit-vector fallback only for that invalid fixture outcome.
    """
    try:
        return ec.DeterministicFakeEmbedder(dimension=dimension)._embed_one(text)
    except ValueError as exc:
        if str(exc) != "embedding vector must not be all zeros":
            raise
        return [1.0] + [0.0] * (dimension - 1)


def msg(item_id: str, content: str, created_at: str = "2025-01-01") -> dict:
    return {
        "entity_type": "message",
        "item_id": str(item_id),
        "content": content,
        "created_at": created_at,
    }


def workflow(
    item_id: str,
    *,
    title: str = "Wan I2V",
    body: str = "A description of the pipeline.",
    python_source: str | None = "x = 1\n",
    metadata: dict | None = None,
    created_at: str = "2025-01-01",
) -> dict:
    payload = {}
    if python_source is not None:
        payload["python_source"] = python_source
    return {
        "entity_type": "resource",
        "kind": "workflow",
        "item_id": str(item_id),
        "title": title,
        "body": body,
        "payload": payload,
        "metadata": metadata or {},
        "created_at": created_at,
    }


class FakeProvider:
    """Records every embed request; can fail per-request with classified errors."""

    def __init__(self, *, dimension: int = 8, behaviors=None):
        self.model_name = "deterministic-fake"
        self.dimension = dimension
        self._behaviors = list(behaviors or [])
        self.requests: list[list[str]] = []
        # A secret-looking attribute that must NEVER appear in logs/reports.
        self.api_key = "sk-PROVIDER-KEY-DO-NOT-LEAK"

    def embed(self, texts):
        self.requests.append(list(texts))
        idx = len(self.requests) - 1
        if idx < len(self._behaviors) and self._behaviors[idx] is not None:
            raise self._behaviors[idx]
        return [_fake_vector(t, self.dimension) for t in texts]


class FakeRateLimiter:
    """Paces + backs off without any real sleep; doubles as the engine clock."""

    def __init__(self):
        self.acquires = 0
        self.sleeps: list[float] = []
        self._now = 0.0

    def acquire(self):
        self.acquires += 1

    def sleep(self, seconds):
        self.sleeps.append(float(seconds))
        self._now += float(seconds)

    def now(self):
        return self._now


class FakeRemediation:
    """Stand-in for executors.workflow_remediation reconciliation stage."""

    def __init__(self, *, ok: bool = True, summary: dict | None = None):
        self.ok = ok
        self.summary = summary or {"processed": 0, "stale": 0}
        self.calls: list[dict] = []
        self.ran_at: float | None = None

    def run(self, store, *, apply: bool):
        self.calls.append({"apply": apply})
        self.ran_at = store._clock.now()  # type: ignore[attr-defined]
        return eb.RemediationReport(ok=self.ok, summary=self.summary)


class FakeStore:
    """In-memory, deterministic store. Records every durable/HNSW/mutation call."""

    def __init__(self, rows_by_source: dict[str, list[dict]], *, high_water=None):
        self._rows = {s: sorted(list(rs), key=lambda r: str(r["item_id"]))
                      for s, rs in rows_by_source.items()}
        self._hw = dict(high_water or {s: "hw-" + s for s in rows_by_source})
        self.vectors: list[dict] = []
        self.checkpoints: list[dict] = []
        self.runs: dict[int, dict] = {}
        self._next_run = 1
        self.hnsw_calls: list[str] = []
        self.source_mutations: list[str] = []
        self.contract_activations: list[int] = []
        self.fetch_calls: list[dict] = []
        self._active_contract_id: int | None = None
        self._clock = FakeRateLimiter()  # default clock; tests may override
        # Toggle for the checkpoint-failure test.
        self.checkpoint_ok = True

    # -- read surface ----------------------------------------------------
    def sources(self):
        return tuple(self._rows.keys())

    def high_water(self, source):
        return self._hw.get(source)

    def eligible_total(self, source):
        return len(self._rows.get(source, []))

    def fetch_page(self, source, *, cursor, limit, since=None, until=None):
        self.fetch_calls.append({"source": source, "cursor": cursor,
                                 "limit": limit, "since": since, "until": until})
        rows = self._rows.get(source, [])
        out = []
        for r in rows:
            cid = str(r["item_id"])
            if cursor is not None and cid <= str(cursor):
                continue
            ca = r.get("created_at")
            if since is not None and ca is not None and ca < since:
                continue
            if until is not None and ca is not None and ca > until:
                continue
            out.append(dict(r))
            if len(out) >= limit:
                break
        return out

    def current_freshness(self, source, keys):
        """Return {identity_key: representation_hash} for already-stored vectors."""
        keyset = set(keys)
        found = {}
        for row in self.vectors:
            k = (row["entity_type"], row["item_id"], row["representation_type"],
                 row["chunk_index"])
            if k in keyset and row["contract_id"] == self._active_contract_id:
                found[k] = row["representation_hash"]
        return found

    def representation_current(self, source, expected):
        actual = {}
        for vector in self.vectors:
            if vector["contract_id"] != self._active_contract_id:
                continue
            identity = (vector["entity_type"], vector["item_id"], vector["representation_type"])
            actual.setdefault(identity, set()).add((vector["chunk_index"], vector["representation_hash"], vector["chunk_hash"]))
        return {identity: actual.get(identity, set()) == {(c.chunk_index, c.representation_hash, c.chunk_hash) for c in chunks}
                for identity, chunks in expected.items()}

    # -- write surface ---------------------------------------------------
    def write_vectors(self, source, rows):
        groups = {(r["contract_id"], r["entity_type"], r["item_id"], r["representation_type"]) for r in rows}
        self.vectors[:] = [v for v in self.vectors if (v["contract_id"], v["entity_type"], v["item_id"], v["representation_type"]) not in groups]
        for incoming in rows:
            self._upsert(incoming)

    def _upsert(self, incoming):
        for existing in self.vectors:
            if (
                existing["contract_id"] == incoming["contract_id"]
                and existing["entity_type"] == incoming["entity_type"]
                and existing["item_id"] == incoming["item_id"]
                and existing["representation_type"] == incoming["representation_type"]
                and existing["chunk_index"] == incoming["chunk_index"]
            ):
                existing.update(incoming)
                return
        self.vectors.append(dict(incoming))

    # -- run lifecycle ---------------------------------------------------
    def create_run(self, *, contract_id, sources, mode, high_water):
        run_id = self._next_run
        self._next_run += 1
        self._active_contract_id = contract_id
        self.runs[run_id] = {
            "run_id": run_id,
            "version": 1,
            "status": "running",
            "mode": mode,
            "contract_id": contract_id,
            "sources": tuple(sources),
            "cursors": {s: None for s in sources},
            "high_water": dict(high_water or {}),
            "counters": {s: {} for s in sources},
        }
        return dict(self.runs[run_id])

    def load_run(self, run_id):
        return dict(self.runs[int(run_id)])

    def checkpoint(self, run_id, source, *, expected_version, cursor, high_water,
                   processed, skipped, quarantined, unavailable, failed,
                   eligible, last_error):
        if not self.checkpoint_ok:
            return {"ok": False, "reason": "stale_version",
                    "version": expected_version, "status": "running"}
        run = self.runs[int(run_id)]
        run["version"] += 1
        run["cursors"][source] = cursor
        if high_water is not None:
            run["high_water"][source] = high_water
        c = run["counters"].setdefault(source, {})
        for name, delta in (("eligible", eligible), ("processed", processed),
                            ("skipped", skipped), ("quarantined", quarantined),
                            ("unavailable", unavailable), ("failed", failed)):
            c[name] = c.get(name, 0) + (delta or 0)
        self.checkpoints.append({"run_id": run_id, "source": source,
                                 "cursor": cursor, "version": run["version"],
                                 "last_error": last_error})
        return {"ok": True, "reason": "checkpointed",
                "version": run["version"], "status": "running"}

    def complete_run(self, run_id, expected_version):
        run = self.runs[int(run_id)]
        run["version"] += 1
        run["status"] = "completed"
        return {"ok": True, "version": run["version"], "status": "completed"}

    def fail_run(self, run_id, expected_version, error):
        run = self.runs[int(run_id)]
        run["version"] += 1
        run["status"] = "failed"
        run["last_error"] = error
        return {"ok": True, "version": run["version"], "status": "failed"}

    # -- NEVER-called surfaces (engine must not touch these) -------------
    def build_hnsw_index(self, *a, **k):
        self.hnsw_calls.append("build_hnsw_index")

    def enable_hnsw(self, *a, **k):
        self.hnsw_calls.append("enable_hnsw")

    def patch_source_row(self, *a, **k):
        self.source_mutations.append("patch_source_row")

    def refresh_source_row(self, *a, **k):
        self.source_mutations.append("refresh_source_row")

    def activate_contract(self, *a, **k):
        self.contract_activations.append(a[0] if a else None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(**over) -> "eb.BackfillConfig":
    base = dict(
        sources=("messages",),
        apply=True,
        resume_run_id=None,
        since=None,
        until=None,
        batch_items=100,
        input_items=64,
        max_tokens_per_request=8192,
        max_chars_per_request=None,
        rate_limit_per_minute=None,
        cost_cap_usd=None,
        max_retries=2,
        sample=None,
        report_path=None,
        contract_id=4242,
        dimension=8,
    )
    base.update(over)
    return eb.BackfillConfig(**base)


def _expected_chunks(row: dict) -> list:
    reps = canon.build_representations(row, row["entity_type"], item_id=row["item_id"])
    return ch.chunk_representations(reps)


def _vectors_for(store: FakeStore, item_id: str) -> list[dict]:
    return [v for v in store.vectors if v["item_id"] == str(item_id)]


# ---------------------------------------------------------------------------
# 1. Direct database URL validation
# ---------------------------------------------------------------------------


class DirectDatabaseURLTests(unittest.TestCase):
    def test_accepts_ordinary_direct_postgresql_url(self):
        url = "postgresql://hivemind:s3cret@db.ref.supabase.co:5432/postgres?sslmode=require"
        self.assertEqual(eb.validate_direct_database_url(url), url)

    def test_accepts_plain_local_postgres_url(self):
        url = "postgres://localhost:5432/hivemind"
        self.assertEqual(eb.validate_direct_database_url(url), url)

    def test_accepts_session_pooler_port_5432(self):
        url = "postgresql://user:pw@db.ref.supabase.co:5432/postgres"
        # Direct/session-mode (port 5432) is accepted.
        self.assertEqual(eb.validate_direct_database_url(url), url)

    def test_rejects_supabase_transaction_pooler_port_6543(self):
        url = "postgresql://user:s3cret-pw@aws-0-region.pooler.supabase.com:6543/postgres"
        with self.assertRaises(eb.DirectDatabaseURLRejected) as cm:
            eb.validate_direct_database_url(url)
        self.assertEqual(cm.exception.reason_code, eb.REASON_URL_TRANSACTION_POOLER)
        # Credentials are never echoed in the rejection.
        self.assertNotIn("s3cret-pw", str(cm.exception))

    def test_rejects_transaction_pooling_query_mode(self):
        url = "postgresql://user:s3cret-pw@db.ref.supabase.co:5432/postgres?pgbouncer=true"
        with self.assertRaises(eb.DirectDatabaseURLRejected) as cm:
            eb.validate_direct_database_url(url)
        self.assertEqual(cm.exception.reason_code, eb.REASON_URL_TRANSACTION_MODE)
        self.assertNotIn("s3cret-pw", str(cm.exception))

    def test_rejects_unsupported_scheme(self):
        for bad in ("mysql://u:p@host/db", "https://example.com/db",
                    "foo://u:p@host/db"):
            with self.assertRaises(eb.DirectDatabaseURLRejected) as cm:
                eb.validate_direct_database_url(bad)
            self.assertEqual(cm.exception.reason_code, eb.REASON_URL_BAD_SCHEME)
            self.assertNotIn(":p@", str(cm.exception))

    def test_rejects_blank_url(self):
        for blank in ("", "   ", None):
            with self.assertRaises(eb.DirectDatabaseURLRejected) as cm:
                eb.validate_direct_database_url(blank)  # type: ignore[arg-type]
            self.assertEqual(cm.exception.reason_code, eb.REASON_URL_BLANK)


# ---------------------------------------------------------------------------
# 2. Remediation runs first; its failure aborts all downstream work
# ---------------------------------------------------------------------------


class RemediationFirstTests(unittest.TestCase):
    def test_remediation_failure_aborts_before_any_work(self):
        store = FakeStore({"messages": [msg("100", "hello")]})
        provider = FakeProvider()
        rem = FakeRemediation(ok=False)
        report = eb.run_backfill(
            store, provider=provider, remediation_stage=rem,
            config=_config(), rate_limiter=FakeRateLimiter(),
        )
        self.assertEqual(report.status, "failed")
        self.assertEqual(report.reason_counts.get(eb.REASON_REMEDIATION_FAILED, 0), 1)
        self.assertEqual(len(provider.requests), 0)
        self.assertEqual(len(store.vectors), 0)
        self.assertEqual(len(store.checkpoints), 0)
        self.assertIsNone(store._active_contract_id)  # no run was created

    def test_remediation_runs_before_first_provider_request(self):
        rows = [msg("100", "first"), msg("200", "second")]
        store = FakeStore({"messages": rows})
        limiter = FakeRateLimiter()
        store._clock = limiter
        rem = FakeRemediation(ok=True)
        provider = FakeProvider()
        eb.run_backfill(
            store, provider=provider, remediation_stage=rem,
            config=_config(input_items=1), rate_limiter=limiter,
        )
        self.assertEqual(len(rem.calls), 1)
        self.assertGreater(len(provider.requests), 0)
        # Remediation timestamped itself before any provider request happened.
        self.assertIsNotNone(rem.ran_at)
        self.assertEqual(rem.ran_at, 0.0)  # ran before the clock advanced


# ---------------------------------------------------------------------------
# 3. Stable per-source paging: opaque cursors, high-water, dates, snowflakes
# ---------------------------------------------------------------------------


class PagingTests(unittest.TestCase):
    def test_pages_with_opaque_text_cursor_and_stops(self):
        rows = [msg(str(i).zfill(3), f"body {i}") for i in range(5)]
        store = FakeStore({"messages": rows})
        eb.run_backfill(
            store, provider=FakeProvider(), remediation_stage=FakeRemediation(),
            config=_config(input_items=1, batch_items=2), rate_limiter=FakeRateLimiter(),
        )
        # fetch_page was called repeatedly with opaque text cursors that advance.
        cursors = [c["cursor"] for c in store.fetch_calls]
        self.assertGreater(len(store.fetch_calls), 1)
        self.assertTrue(all(c is None or isinstance(c, str) for c in cursors))
        # Final persisted cursor is the last item_id, as an exact string.
        last = store.runs[1]["cursors"]["messages"]
        self.assertEqual(last, "004")
        self.assertIsInstance(last, str)

    def test_source_selection_limits_which_sources_run(self):
        store = FakeStore({
            "messages": [msg("100", "m1")],
            "resources": [msg("200", "r1")],
        })
        eb.run_backfill(
            store, provider=FakeProvider(), remediation_stage=FakeRemediation(),
            config=_config(sources=("messages",)), rate_limiter=FakeRateLimiter(),
        )
        fetched = {c["source"] for c in store.fetch_calls}
        self.assertEqual(fetched, {"messages"})

    def test_inclusive_date_window_filters_rows(self):
        rows = [
            msg("100", "jan", created_at="2025-01-10"),
            msg("200", "feb", created_at="2025-02-15"),
            msg("300", "mar", created_at="2025-03-20"),
        ]
        store = FakeStore({"messages": rows})
        report = eb.run_backfill(
            store, provider=FakeProvider(), remediation_stage=FakeRemediation(),
            config=_config(since="2025-02-01", until="2025-02-28"),
            rate_limiter=FakeRateLimiter(),
        )
        # Only the February row is eligible/embedded.
        ids = {v["item_id"] for v in store.vectors}
        self.assertEqual(ids, {"200"})
        self.assertEqual(report.per_source["messages"]["eligible"], 1)

    def test_discord_snowflake_preserved_as_exact_string(self):
        snowflake = "9223372036854775807"
        store = FakeStore({"messages": [msg(snowflake, "snowflake message")]})
        eb.run_backfill(
            store, provider=FakeProvider(), remediation_stage=FakeRemediation(),
            config=_config(), rate_limiter=FakeRateLimiter(),
        )
        cursor = store.runs[1]["cursors"]["messages"]
        self.assertEqual(cursor, snowflake)
        self.assertIsInstance(cursor, str)  # not coerced to int/float
        self.assertEqual({v["item_id"] for v in store.vectors}, {snowflake})


# ---------------------------------------------------------------------------
# 4. Dry-run is default: inventory/chunk/count only
# ---------------------------------------------------------------------------


class DryRunDefaultTests(unittest.TestCase):
    def test_dry_run_does_no_durable_work(self):
        rows = [msg("100", "hello world"), msg("200", "another message")]
        store = FakeStore({"messages": rows})
        provider = FakeProvider()
        report = eb.run_backfill(
            store, provider=provider, remediation_stage=FakeRemediation(),
            config=_config(apply=False), rate_limiter=FakeRateLimiter(),
        )
        self.assertEqual(report.mode, "dry_run")
        self.assertEqual(len(provider.requests), 0)
        self.assertEqual(len(store.vectors), 0)
        self.assertEqual(len(store.checkpoints), 0)
        self.assertIsNone(store._active_contract_id)  # no run created
        self.assertEqual(len(store.contract_activations), 0)
        # Inventory/chunk counts are still reported.
        self.assertEqual(report.per_source["messages"]["eligible"], 2)
        self.assertGreaterEqual(report.chunks_built, 2)
        self.assertEqual(report.vectors_written, 0)

    def test_cli_defaults_to_dry_run(self):
        ns = cli.parse_args(["--database-url", "postgres://u:p@host/db", "--source", "messages"])
        self.assertFalse(ns.apply)


# ---------------------------------------------------------------------------
# 5. Canonical representations + frozen chunkers are reused
# ---------------------------------------------------------------------------


class RepresentationReuseTests(unittest.TestCase):
    def test_chunks_match_frozen_chunker_hashes(self):
        row = msg("100", "\n\n".join(f"paragraph {i} about wan video." for i in range(8)))
        store = FakeStore({"messages": [row]})
        eb.run_backfill(
            store, provider=FakeProvider(), remediation_stage=FakeRemediation(),
            config=_config(), rate_limiter=FakeRateLimiter(),
        )
        expected = _expected_chunks(row)
        self.assertEqual(len(store.vectors), len(expected))
        got_hashes = {(v["representation_type"], v["chunk_index"], v["chunk_hash"])
                      for v in store.vectors}
        want_hashes = {(c.representation_type, c.chunk_index, c.chunk_hash) for c in expected}
        self.assertEqual(got_hashes, want_hashes)

    def test_workflow_prose_and_python_are_separate_streams_from_zero(self):
        row = workflow("2580", body="desc\n\nPython scratchpad source:\nx = 1\n",
                       python_source="x = 1\n")
        store = FakeStore({"resources": [row]})
        eb.run_backfill(
            store, provider=FakeProvider(), remediation_stage=FakeRemediation(),
            config=_config(sources=("resources",)),
            rate_limiter=FakeRateLimiter(),
        )
        by_type: dict[str, list[int]] = {}
        for v in store.vectors:
            by_type.setdefault(v["representation_type"], []).append(v["chunk_index"])
        self.assertEqual(set(by_type), {"prose", "workflow_python"})
        for rep_type, indexes in by_type.items():
            self.assertEqual(indexes, list(range(len(indexes))),
                             f"{rep_type} stream must start at chunk_index 0")
        # Same item id under both streams; no duplication of python bytes.
        self.assertEqual({v["item_id"] for v in store.vectors}, {"2580"})

    def test_quarantined_python_excluded_while_safe_prose_remains(self):
        secret = "token = 'sk-" + "A" * 60 + "'\n"
        row = workflow("999", body="a safe description of the workflow",
                       python_source=secret)
        store = FakeStore({"resources": [row]})
        report = eb.run_backfill(
            store, provider=FakeProvider(), remediation_stage=FakeRemediation(),
            config=_config(sources=("resources",)),
            rate_limiter=FakeRateLimiter(),
        )
        types = {v["representation_type"] for v in store.vectors}
        self.assertEqual(types, {"prose"})  # quarantined python excluded
        self.assertGreater(report.reason_counts.get(eb.REASON_QUARANTINED, 0), 0)
        # The secret never reached a stored vector's text either.
        for v in store.vectors:
            self.assertNotIn("sk-", v.get("chunk_text", ""))

    def test_unavailable_python_excluded_prose_still_eligible(self):
        row = workflow("777", body="prose only, no python evidence at all",
                       python_source=None)
        store = FakeStore({"resources": [row]})
        report = eb.run_backfill(
            store, provider=FakeProvider(), remediation_stage=FakeRemediation(),
            config=_config(sources=("resources",)),
            rate_limiter=FakeRateLimiter(),
        )
        types = {v["representation_type"] for v in store.vectors}
        self.assertEqual(types, {"prose"})
        self.assertEqual(report.reason_counts.get(eb.REASON_UNAVAILABLE, 0), 1)


# ---------------------------------------------------------------------------
# 6. Provider batches respect item/input + token/character limits
# ---------------------------------------------------------------------------


class ProviderBatchTests(unittest.TestCase):
    def _many_chunks_corpus(self, n: int) -> list[dict]:
        return [msg(str(i).zfill(4), f"message body number {i} " * 6) for i in range(n)]

    def test_input_item_limit_splits_provider_requests(self):
        rows = self._many_chunks_corpus(6)
        store = FakeStore({"messages": rows})
        provider = FakeProvider()
        eb.run_backfill(
            store, provider=provider, remediation_stage=FakeRemediation(),
            config=_config(input_items=2, max_tokens_per_request=10**9,
                           max_chars_per_request=None),
            rate_limiter=FakeRateLimiter(),
        )
        self.assertGreater(len(provider.requests), 1)
        for req in provider.requests:
            self.assertLessEqual(len(req), 2)

    def test_token_limit_splits_provider_requests(self):
        rows = self._many_chunks_corpus(6)
        store = FakeStore({"messages": rows})
        big = FakeProvider()
        eb.run_backfill(
            store, provider=big, remediation_stage=FakeRemediation(),
            config=_config(input_items=64, max_tokens_per_request=10**9),
            rate_limiter=FakeRateLimiter(),
        )
        store2 = FakeStore({"messages": rows})
        small = FakeProvider()
        eb.run_backfill(
            store2, provider=small, remediation_stage=FakeRemediation(),
            config=_config(input_items=64, max_tokens_per_request=4),
            rate_limiter=FakeRateLimiter(),
        )
        self.assertGreaterEqual(len(small.requests), len(big.requests))

    def test_character_limit_splits_provider_requests(self):
        rows = self._many_chunks_corpus(6)
        store = FakeStore({"messages": rows})
        provider = FakeProvider()
        eb.run_backfill(
            store, provider=provider, remediation_stage=FakeRemediation(),
            config=_config(input_items=64, max_tokens_per_request=10**9,
                           max_chars_per_request=20),
            rate_limiter=FakeRateLimiter(),
        )
        self.assertGreater(len(provider.requests), 1)
        # A batch bound never truncates a canonical chunk to fit.
        self.assertTrue(any(len(t) > 20 for req in provider.requests for t in req))

    def test_vectors_mapped_back_to_exact_chunks_deterministically(self):
        row = msg("100", "a bounded message body that becomes one chunk")
        store = FakeStore({"messages": [row]})
        dim = 12
        eb.run_backfill(
            store, provider=FakeProvider(dimension=dim),
            remediation_stage=FakeRemediation(),
            config=_config(dimension=dim), rate_limiter=FakeRateLimiter(),
        )
        for v in store.vectors:
            expected = _fake_vector(v["chunk_text"], dim)
            self.assertEqual(v["embedding"], expected)
            self.assertEqual(len(v["embedding"]), dim)


# ---------------------------------------------------------------------------
# 7. Injected rate limiter paces without real sleeps
# ---------------------------------------------------------------------------


class RateLimiterTests(unittest.TestCase):
    def test_acquire_called_once_per_provider_request(self):
        rows = [msg(str(i).zfill(3), f"body {i}") for i in range(4)]
        store = FakeStore({"messages": rows})
        provider = FakeProvider()
        limiter = FakeRateLimiter()
        eb.run_backfill(
            store, provider=provider, remediation_stage=FakeRemediation(),
            config=_config(input_items=1), rate_limiter=limiter,
        )
        self.assertEqual(limiter.acquires, len(provider.requests))
        self.assertGreater(limiter.acquires, 0)


# ---------------------------------------------------------------------------
# 8. Idempotency: skip already-current; reruns never duplicate
# ---------------------------------------------------------------------------


class IdempotencyTests(unittest.TestCase):
    def test_already_current_skipped_without_provider_call(self):
        row = msg("100", "stable message body")
        store = FakeStore({"messages": [row]})
        eb.run_backfill(
            store, provider=FakeProvider(), remediation_stage=FakeRemediation(),
            config=_config(), rate_limiter=FakeRateLimiter(),
        )
        # Second run: everything is already current -> no provider calls.
        provider2 = FakeProvider()
        report = eb.run_backfill(
            store, provider=provider2, remediation_stage=FakeRemediation(),
            config=_config(resume_run_id=None), rate_limiter=FakeRateLimiter(),
        )
        self.assertEqual(len(provider2.requests), 0)
        self.assertGreater(report.reason_counts.get(eb.REASON_SKIPPED_CURRENT, 0), 0)
        self.assertEqual(report.vectors_written, 0)

    def test_reruns_never_duplicate_vector_rows(self):
        rows = [msg("100", "first stable body"),
                msg("200", "second stable body")]
        store = FakeStore({"messages": rows})
        for _ in range(3):
            eb.run_backfill(
                store, provider=FakeProvider(), remediation_stage=FakeRemediation(),
                config=_config(), rate_limiter=FakeRateLimiter(),
            )
        keys = {(v["entity_type"], v["item_id"], v["representation_type"],
                 v["chunk_index"]) for v in store.vectors}
        self.assertEqual(len(store.vectors), len(keys))  # no duplicates
        self.assertEqual(len(keys), 2)

    def test_stale_tail_chunk_forces_atomic_replacement(self):
        row = msg("100", "stable message body")
        store = FakeStore({"messages": [row]})
        eb.run_backfill(store, provider=FakeProvider(), remediation_stage=FakeRemediation(),
                        config=_config(), rate_limiter=FakeRateLimiter())
        tail = dict(store.vectors[0]); tail["chunk_index"] = 99; tail["chunk_hash"] = "f" * 64
        store.vectors.append(tail)
        provider = FakeProvider()
        report = eb.run_backfill(store, provider=provider, remediation_stage=FakeRemediation(),
                                 config=_config(), rate_limiter=FakeRateLimiter())
        self.assertGreater(len(provider.requests), 0)
        self.assertEqual(report.vectors_written, 1)
        self.assertEqual([v["chunk_index"] for v in store.vectors], [0])


# ---------------------------------------------------------------------------
# 9. Write-before-checkpoint; checkpoint failure surfaced; resume replays
# ---------------------------------------------------------------------------


class CheckpointOrderingTests(unittest.TestCase):
    def test_write_happens_before_checkpoint(self):
        rows = [msg(str(i).zfill(3), f"body {i}") for i in range(3)]
        store = FakeStore({"messages": rows})
        eb.run_backfill(
            store, provider=FakeProvider(), remediation_stage=FakeRemediation(),
            config=_config(input_items=1), rate_limiter=FakeRateLimiter(),
        )
        self.assertGreater(len(store.vectors), 0)
        self.assertGreater(len(store.checkpoints), 0)
        # Every checkpoint advanced strictly past the rows already written.
        for ckpt in store.checkpoints:
            written_before = sum(
                1 for v in store.vectors if v["item_id"] <= str(ckpt["cursor"])
            )
            self.assertGreater(written_before, 0)

    def test_checkpoint_failure_surfaced_and_resume_replays_idempotently(self):
        rows = [msg(str(i).zfill(3), f"body {i}") for i in range(3)]
        store = FakeStore({"messages": rows})
        store.checkpoint_ok = False
        report = eb.run_backfill(
            store, provider=FakeProvider(), remediation_stage=FakeRemediation(),
            config=_config(input_items=1), rate_limiter=FakeRateLimiter(),
        )
        self.assertIn(report.status, ("failed", "interrupted"))
        self.assertGreater(report.reason_counts.get(eb.REASON_CHECKPOINT_FAILED, 0), 0)
        written_after_failure = len(store.vectors)
        self.assertGreaterEqual(written_after_failure, 1)  # write happened first
        # Resume: checkpoint now succeeds; idempotent writes do not duplicate.
        store.checkpoint_ok = True
        report2 = eb.run_backfill(
            store, provider=FakeProvider(), remediation_stage=FakeRemediation(),
            config=_config(input_items=1), rate_limiter=FakeRateLimiter(),
        )
        self.assertEqual(report2.status, "completed")
        keys = {(v["entity_type"], v["item_id"], v["representation_type"],
                 v["chunk_index"]) for v in store.vectors}
        self.assertEqual(len(store.vectors), len(keys))


# ---------------------------------------------------------------------------
# 10. Interrupted run resumes from persisted cursor/version
# ---------------------------------------------------------------------------


class ResumeTests(unittest.TestCase):
    def test_interrupted_run_resumes_with_no_lost_or_duplicate_rows(self):
        rows = [msg(str(i).zfill(3), f"body {i}") for i in range(4)]
        store = FakeStore({"messages": rows})
        limiter = FakeRateLimiter()
        store._clock = limiter
        # Provider raises KeyboardInterrupt on the second request.
        provider = FakeProvider(behaviors=[None, KeyboardInterrupt()])
        report = eb.run_backfill(
            store, provider=provider, remediation_stage=FakeRemediation(),
            config=_config(input_items=1), rate_limiter=limiter,
        )
        self.assertTrue(report.interrupted)
        self.assertEqual(report.reason_counts.get(eb.REASON_INTERRUPTED, 0), 1)
        committed = len(store.vectors)
        self.assertEqual(committed, 1)  # only the first batch committed
        run_id = report.run_id
        cursor_before = store.runs[run_id]["cursors"]["messages"]
        self.assertEqual(cursor_before, "000")

        # Resume from the persisted run id.
        provider2 = FakeProvider()
        report2 = eb.run_backfill(
            store, provider=provider2, remediation_stage=FakeRemediation(),
            config=_config(input_items=1, resume_run_id=run_id),
            rate_limiter=limiter,
        )
        self.assertFalse(report2.interrupted)
        self.assertEqual(report2.status, "completed")
        # All four rows present exactly once; counters accumulated.
        keys = {(v["entity_type"], v["item_id"], v["representation_type"],
                 v["chunk_index"]) for v in store.vectors}
        self.assertEqual(len(store.vectors), len(keys))
        self.assertEqual({v["item_id"] for v in store.vectors}, {"000", "001", "002", "003"})
        self.assertEqual(store.runs[run_id]["cursors"]["messages"], "003")


# ---------------------------------------------------------------------------
# 11. Retry/backoff classification; no advance past failed item; resume succeeds
# ---------------------------------------------------------------------------


class RetryClassificationTests(unittest.TestCase):
    def test_transient_failure_retried_with_backoff_then_succeeds(self):
        rows = [msg("100", "first"), msg("200", "second")]
        store = FakeStore({"messages": rows})
        limiter = FakeRateLimiter()
        # First request transient once, then succeeds on retry.
        provider = FakeProvider(behaviors=[eb.TransientProviderError("transient boom sk-LEAK-1")])
        report = eb.run_backfill(
            store, provider=provider, remediation_stage=FakeRemediation(),
            config=_config(input_items=1, max_retries=3), rate_limiter=limiter,
        )
        self.assertEqual(report.status, "completed")
        self.assertGreater(len(limiter.sleeps), 0)  # backoff pacing, no real sleep
        self.assertEqual(len(store.vectors), 2)

    def test_exhausted_transient_does_not_advance_past_failed_item(self):
        rows = [msg("100", "first"), msg("200", "second")]
        store = FakeStore({"messages": rows})
        provider = FakeProvider(behaviors=[
            None,  # first item ok + committed
            eb.TransientProviderError("always transient sk-LEAK-2"),
            eb.TransientProviderError("always transient sk-LEAK-2"),
            eb.TransientProviderError("always transient sk-LEAK-2"),
        ])
        report = eb.run_backfill(
            store, provider=provider, remediation_stage=FakeRemediation(),
            config=_config(input_items=1, max_retries=2), rate_limiter=FakeRateLimiter(),
        )
        self.assertIn(report.status, ("failed", "interrupted"))
        self.assertGreater(report.reason_counts.get(eb.REASON_EXHAUSTED, 0), 0)
        # Earlier committed work remains valid...
        self.assertEqual({v["item_id"] for v in store.vectors}, {"100"})
        # ...cursor did not advance past the failed item.
        self.assertEqual(store.runs[report.run_id]["cursors"]["messages"], "100")

    def test_permanent_failure_classified_nonsecretly_no_advance(self):
        rows = [msg("100", "first"), msg("200", "second")]
        store = FakeStore({"messages": rows})
        provider = FakeProvider(behaviors=[
            None,
            eb.PermanentProviderError("bad request sk-LEAK-3"),
        ])
        report = eb.run_backfill(
            store, provider=provider, remediation_stage=FakeRemediation(),
            config=_config(input_items=1, max_retries=3), rate_limiter=FakeRateLimiter(),
        )
        self.assertGreater(report.reason_counts.get(eb.REASON_PERMANENT, 0), 0)
        self.assertEqual({v["item_id"] for v in store.vectors}, {"100"})
        self.assertEqual(store.runs[report.run_id]["cursors"]["messages"], "100")

    def test_exhausted_failure_can_succeed_on_resume(self):
        rows = [msg("100", "first"), msg("200", "second")]
        store = FakeStore({"messages": rows})
        provider = FakeProvider(behaviors=[
            None, eb.TransientProviderError("t"), eb.TransientProviderError("t"),
            eb.TransientProviderError("t"),
        ])
        report = eb.run_backfill(
            store, provider=provider, remediation_stage=FakeRemediation(),
            config=_config(input_items=1, max_retries=2), rate_limiter=FakeRateLimiter(),
        )
        run_id = report.run_id
        # Resume with a healthy provider.
        report2 = eb.run_backfill(
            store, provider=FakeProvider(), remediation_stage=FakeRemediation(),
            config=_config(input_items=1, resume_run_id=run_id),
            rate_limiter=FakeRateLimiter(),
        )
        self.assertEqual(report2.status, "completed")
        self.assertEqual({v["item_id"] for v in store.vectors}, {"100", "200"})


# ---------------------------------------------------------------------------
# 12. Partial provider-batch failure keeps earlier committed work valid
# ---------------------------------------------------------------------------


class PartialFailureTests(unittest.TestCase):
    def test_later_batch_not_checkpointed_earlier_batch_kept(self):
        rows = [msg(str(i).zfill(3), f"body {i}") for i in range(4)]
        store = FakeStore({"messages": rows})
        # Two batches of two; the second batch fails permanently.
        provider = FakeProvider(behaviors=[
            None,
            eb.PermanentProviderError("second batch died sk-LEAK-4"),
        ])
        report = eb.run_backfill(
            store, provider=provider, remediation_stage=FakeRemediation(),
            config=_config(input_items=2), rate_limiter=FakeRateLimiter(),
        )
        # Earlier complete item is durable; the failed item is not checkpointed.
        self.assertEqual({v["item_id"] for v in store.vectors}, {"000"})
        self.assertGreater(report.reason_counts.get(eb.REASON_PERMANENT, 0), 0)
        self.assertEqual(store.runs[report.run_id]["cursors"]["messages"], "000")

    def test_multichunk_representation_is_atomic_and_resume_keeps_every_chunk(self):
        body = "\n\n".join("paragraph %04d " % i + ("word " * 600) for i in range(4))
        row = msg("999", body)
        expected = _expected_chunks(row)
        self.assertGreater(len(expected), 1)
        store = FakeStore({"messages": [row]})
        first = eb.run_backfill(
            store, provider=FakeProvider(behaviors=[None, eb.PermanentProviderError("bad")]),
            remediation_stage=FakeRemediation(), config=_config(max_tokens_per_request=20),
            rate_limiter=FakeRateLimiter(),
        )
        self.assertEqual(store.vectors, [])
        self.assertIsNone(store.runs[first.run_id]["cursors"]["messages"])
        second = eb.run_backfill(
            store, provider=FakeProvider(), remediation_stage=FakeRemediation(),
            config=_config(max_tokens_per_request=20, resume_run_id=first.run_id),
            rate_limiter=FakeRateLimiter(),
        )
        self.assertEqual(second.status, "completed")
        self.assertEqual({(v["chunk_index"], v["chunk_hash"]) for v in store.vectors},
                         {(c.chunk_index, c.chunk_hash) for c in expected})

    def test_small_character_batch_bound_never_truncates_hash_canonical_text(self):
        row = msg("full", "canonical bytes " * 100)
        store, provider = FakeStore({"messages": [row]}), FakeProvider()
        eb.run_backfill(store, provider=provider, remediation_stage=FakeRemediation(),
                        config=_config(max_chars_per_request=5), rate_limiter=FakeRateLimiter())
        self.assertEqual(provider.requests[0][0], store.vectors[0]["chunk_text"])
        self.assertGreater(len(provider.requests[0][0]), 5)


# ---------------------------------------------------------------------------
# 13. Source rows are never mutated by the embedding stage
# ---------------------------------------------------------------------------


class SourceImmutabilityTests(unittest.TestCase):
    def test_no_source_row_mutation_calls(self):
        store = FakeStore({"messages": [msg("100", "body"), msg("200", "body two")]})
        eb.run_backfill(
            store, provider=FakeProvider(), remediation_stage=FakeRemediation(),
            config=_config(), rate_limiter=FakeRateLimiter(),
        )
        self.assertEqual(store.source_mutations, [])


# ---------------------------------------------------------------------------
# 14. Logs/reports are secret-free
# ---------------------------------------------------------------------------


class SecretSafetyTests(unittest.TestCase):
    def test_report_excludes_credentials_bodies_python_and_exception_text(self):
        leak_body = "the answer involves sk-LEAK-BODY-12345"
        secret_python = "token = 'sk-" + "Z" * 60 + "'\n"
        rows = [
            msg("100", leak_body),
            workflow("200", body="safe prose", python_source=secret_python),
        ]
        store = FakeStore({"messages": [rows[0]], "resources": [rows[1]]})
        provider = FakeProvider(behaviors=[
            None,
            eb.TransientProviderError("detail with sk-LEAK-EXC-67890 inside"),
            eb.TransientProviderError("detail with sk-LEAK-EXC-67890 inside"),
            eb.TransientProviderError("detail with sk-LEAK-EXC-67890 inside"),
        ])
        report = eb.run_backfill(
            store, provider=provider, remediation_stage=FakeRemediation(),
            config=_config(sources=("messages", "resources"), input_items=1,
                           max_retries=2),
            rate_limiter=FakeRateLimiter(),
        )
        blob = repr(report.to_log_dict())
        for forbidden in (
            "sk-LEAK-BODY-12345",
            "sk-PROVIDER-KEY-DO-NOT-LEAK",
            "sk-LEAK-EXC-67890",
            "s3cret",
            "password",
        ):
            self.assertNotIn(forbidden, blob, f"report leaked {forbidden!r}")

    def test_url_validation_rejection_never_echoes_password(self):
        url = "postgresql://user:super-secret-pw@aws-0.pooler.supabase.com:6543/postgres"
        with self.assertRaises(eb.DirectDatabaseURLRejected) as cm:
            eb.validate_direct_database_url(url)
        self.assertNotIn("super-secret-pw", str(cm.exception))
        self.assertNotIn("super-secret-pw", repr(cm.exception))


# ---------------------------------------------------------------------------
# 15. The engine never builds/enables HNSW
# ---------------------------------------------------------------------------


class HNSWForbiddenTests(unittest.TestCase):
    def test_no_hnsw_calls_in_apply(self):
        store = FakeStore({"messages": [msg("100", "body")]})
        eb.run_backfill(
            store, provider=FakeProvider(), remediation_stage=FakeRemediation(),
            config=_config(), rate_limiter=FakeRateLimiter(),
        )
        self.assertEqual(store.hnsw_calls, [])

    def test_no_hnsw_calls_in_dry_run(self):
        store = FakeStore({"messages": [msg("100", "body")]})
        eb.run_backfill(
            store, provider=FakeProvider(), remediation_stage=FakeRemediation(),
            config=_config(apply=False), rate_limiter=FakeRateLimiter(),
        )
        self.assertEqual(store.hnsw_calls, [])


# ---------------------------------------------------------------------------
# 16. CLI parsing shape + defaults
# ---------------------------------------------------------------------------


class CLIParsingTests(unittest.TestCase):
    def test_exposes_all_documented_options(self):
        ns = cli.parse_args([
            "--database-url", "postgres://u:p@host:5432/db",
            "--apply",
            "--resume-run-id", "7",
            "--source", "messages",
            "--source", "resources",
            "--since", "2025-01-01",
            "--until", "2025-06-30",
            "--batch-items", "50",
            "--input-items", "16",
            "--max-tokens", "4096",
            "--max-chars", "32000",
            "--rate-limit", "600",
            "--cost-cap", "5.00",
            "--retries", "4",
            "--sample", "10",
            "--report-path", "out/report.json",
        ])
        self.assertEqual(ns.database_url, "postgres://u:p@host:5432/db")
        self.assertTrue(ns.apply)
        self.assertEqual(ns.resume_run_id, "7")
        self.assertEqual(ns.source, ["messages", "resources"])
        self.assertEqual(ns.since, "2025-01-01")
        self.assertEqual(ns.until, "2025-06-30")
        self.assertEqual(ns.batch_items, 50)
        self.assertEqual(ns.input_items, 16)
        self.assertEqual(ns.max_tokens, 4096)
        self.assertEqual(ns.max_chars, 32000)
        self.assertEqual(ns.rate_limit, 600)
        self.assertEqual(ns.cost_cap, 5.00)
        self.assertEqual(ns.retries, 4)
        self.assertEqual(ns.sample, 10)
        self.assertEqual(ns.report_path, "out/report.json")

    def test_defaults_are_dry_run_and_sensible(self):
        ns = cli.parse_args([
            "--database-url", "postgres://u:p@host:5432/db",
            "--source", "messages",
        ])
        self.assertFalse(ns.apply)
        self.assertIsNone(ns.resume_run_id)
        self.assertGreater(ns.batch_items, 0)
        self.assertGreater(ns.input_items, 0)
        self.assertGreater(ns.max_tokens, 0)
        self.assertGreaterEqual(ns.retries, 0)

    def test_build_parser_returns_argument_parser(self):
        import argparse
        self.assertIsInstance(cli.build_parser(), argparse.ArgumentParser)


# ---------------------------------------------------------------------------
# 17. CLI run validates direct-session mode before any work
# ---------------------------------------------------------------------------


class CLIRunTests(unittest.TestCase):
    def _ns(self, url, **over):
        base = [
            "--database-url", url,
            "--source", "messages",
        ]
        if over.get("apply"):
            base.append("--apply")
        return cli.parse_args(base)

    def test_run_rejects_transaction_pooler_before_any_work(self):
        store = FakeStore({"messages": [msg("100", "body")]})
        provider = FakeProvider()
        rem = FakeRemediation()
        ns = self._ns("postgres://u:p@aws-0.pooler.supabase.com:6543/postgres", apply=True)
        with self.assertRaises(eb.DirectDatabaseURLRejected):
            cli.run(ns, store=store, provider=provider,
                    remediation_stage=rem, rate_limiter=FakeRateLimiter())
        # Nothing happened: no fetch, no provider call, no run created.
        self.assertEqual(store.fetch_calls, [])
        self.assertEqual(provider.requests, [])
        self.assertEqual(len(rem.calls), 0)

    def test_run_accepts_direct_url_and_drives_engine(self):
        store = FakeStore({"messages": [msg("100", "body")]})
        provider = FakeProvider()
        rem = FakeRemediation()
        ns = self._ns("postgres://u:p@db.ref.supabase.co:5432/postgres", apply=True)
        report = cli.run(ns, store=store, provider=provider,
                         remediation_stage=rem, rate_limiter=FakeRateLimiter())
        self.assertEqual(report.status, "completed")
        self.assertEqual(len(rem.calls), 1)
        self.assertGreater(len(store.vectors), 0)

    def test_run_defaults_to_dry_run_with_valid_url(self):
        store = FakeStore({"messages": [msg("100", "body")]})
        provider = FakeProvider()
        ns = self._ns("postgres://u:p@db.ref.supabase.co:5432/postgres")
        report = cli.run(ns, store=store, provider=provider,
                         remediation_stage=FakeRemediation(),
                         rate_limiter=FakeRateLimiter())
        self.assertEqual(report.mode, "dry_run")
        self.assertEqual(len(provider.requests), 0)
        self.assertEqual(len(store.vectors), 0)


if __name__ == "__main__":
    unittest.main()
