"""Direct/session PostgreSQL adapters for the task-2.12 operator commands.

This is deliberately a small ``psql`` adapter, rather than a second REST or
    database-driver client.  It is for an operator-owned direct/session connection
    only: the URL is parsed into a child-process libpq environment, SQL is sent over
    stdin, and neither source text nor credentials are placed in a shell
command.  The pure engines remain the authority for canonicalization, chunking,
quarantine, batching, retries, and cursor progression.

Nothing here activates contracts, creates HNSW, or executes stored Python.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qsl, unquote, urlsplit

from executors import embedding_backfill as eb
from executors import embedding_contract as ec
from executors.selected_contract import (
    CHUNK_CONFIG_IDENTITY_VERSION,
    HISTORICAL_DIMENSION_ONLY_ID,
    SELECTED_CANONICALIZATION_VERSION,
    SELECTED_CONTRACT_ID,
    SELECTED_DIMENSION,
    SELECTED_MODEL,
    SELECTED_PROVIDER,
    SELECTED_CHUNKING_VERSION,
    SELECTED_CHUNK_CONFIG_IDENTITY,
    SELECTED_PROSE_TARGET_TOKENS,
    SELECTED_PROSE_OVERLAP_TOKENS,
    SELECTED_PYTHON_TARGET_TOKENS,
    SELECTED_PYTHON_OVERLAP_TOKENS,
    SELECTED_CONTRACT_SHA256_HEX,
    EVAL_CONTRACT_ID_HEX,
)
from executors import lexical_documents as ld
from executors import workflow_remediation as wrm

__all__ = [
    "PostgresBackfillStore", "PostgresWorkflowStore", "WorkflowRemediationStage",
    "OpenAIProvider", "PsqlSession", "PacingRateLimiter", "vibecomfy_recoverer",
]


def _q(value: Any) -> str:
    """Render one literal for a private temporary SQL file (never a shell)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _json(value: Any) -> str:
    return _q(json.dumps(value, ensure_ascii=False, separators=(",", ":"))) + "::jsonb"


class PsqlSession:
    """A credential-safe direct/session ``psql`` transport.

    The database URL is accepted only after the pure validator has rejected
    blank, non-PostgreSQL, and transaction-pooler forms.  Passwords stay in the
    child environment (``PGPASSWORD``), never argv, output, reports, or SQL.
    """

    def __init__(self, database_url: str, *, psql_bin: str = "psql") -> None:
        self._url = eb.validate_direct_database_url(database_url)
        self._psql_bin = psql_bin
        self._env = self._connection_env(self._url)

    @staticmethod
    def _connection_env(url: str) -> dict[str, str]:
        parts = urlsplit(url)
        if not parts.hostname or not parts.path or parts.path == "/":
            raise eb.DirectDatabaseURLRejected(eb.REASON_URL_BAD_SCHEME)
        env = dict(os.environ)
        env["PGHOST"] = parts.hostname
        env["PGPORT"] = str(parts.port or 5432)
        env["PGDATABASE"] = unquote(parts.path.lstrip("/").split("/", 1)[0])
        if parts.username:
            env["PGUSER"] = unquote(parts.username)
        if parts.password is not None:
            env["PGPASSWORD"] = unquote(parts.password)
        for key, value in parse_qsl(parts.query, keep_blank_values=False):
            normalized = key.lower()
            if normalized in {"sslmode", "sslrootcert", "sslcert", "sslkey", "connect_timeout"}:
                env["PG" + normalized.upper()] = value
        return env

    def run(self, sql: str, *, timeout: float = 120.0) -> str:
        """Run private SQL over stdin and return stdout on success.

        Error details can contain server values.  Callers deliberately receive a
        category-only exception and must not serialize source/credential text.
        """
        try:
            proc = subprocess.run(
                [self._psql_bin, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-A", "-t"],
                env=self._env, text=True, input="set client_min_messages=warning;\n" + sql,
                capture_output=True, timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("psql_unavailable") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("psql_timeout") from exc
        if proc.returncode:
            raise RuntimeError("psql_failed")
        return proc.stdout.strip()

    def json_rows(self, query: str) -> list[dict[str, Any]]:
        text = self.run("select coalesce(jsonb_agg(to_jsonb(q)), '[]'::jsonb) from (" + query + ") q;")
        try:
            decoded = json.loads(text or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError("psql_json_protocol") from exc
        return decoded if isinstance(decoded, list) else []

    def json_one(self, query: str) -> dict[str, Any] | None:
        rows = self.json_rows(query)
        return rows[0] if rows else None


_SOURCE_SQL: dict[str, tuple[str, str]] = {
    "approved_distillations": (
        "distillations", "d.status = 'approved'",
    ),
    "pending_distillations": (
        "distillations", "d.status = 'pending'",
    ),
    "distillations": (
        "distillations", "d.status in ('approved','pending')",
    ),
    "resources": ("external_resources", "true"),
    "messages": ("discord_messages", "coalesce(m.is_deleted, false) = false"),
}


class PostgresBackfillStore:
    """Real source paging/vector/run-state adapter for ``embedding_backfill``."""

    def __init__(self, session: PsqlSession) -> None:
        self.session = session
        self._bounds: dict[str, str | None] = {}
        self._contract_id: int | None = None

    def sources(self) -> tuple[str, ...]:
        return tuple(_SOURCE_SQL)

    def set_snapshot_bounds(self, bounds: dict[str, str | None]) -> None:
        self._bounds = {str(k): (str(v) if v is not None else None) for k, v in bounds.items()}

    def validate_contract(self, *, contract_id: int, dimension: int, provider: str, model: str) -> None:
        """Fail closed before provider calls; never activate or mutate a contract."""
        if int(contract_id) != SELECTED_CONTRACT_ID or int(dimension) != SELECTED_DIMENSION:
            raise ValueError("content_embeddings_fixed_dimension")
        row = self.session.json_one(
            "select id,provider,model,dimension,canonicalization_version,chunking_version "
            "from public.embedding_contracts where id=" + str(int(contract_id))
        )
        if not row or int(row.get("dimension", 0)) != SELECTED_DIMENSION:
            raise ValueError("embedding_contract_incompatible")
        if (
            str(row.get("provider")) != SELECTED_PROVIDER
            or str(row.get("model")) != SELECTED_MODEL
            or str(provider) != SELECTED_PROVIDER
            or str(model) != SELECTED_MODEL
            or int(row.get("canonicalization_version", 0)) != SELECTED_CANONICALIZATION_VERSION
            or int(row.get("chunking_version", 0)) != SELECTED_CHUNKING_VERSION
        ):
            raise ValueError("embedding_contract_incompatible")
        registry = self.session.json_one(
            "select selected_contract_id,base_contract_id,provider,model,dimension,"
            "canonicalization_version,chunking_version,chunk_config_version,"
            "prose_target_tokens,prose_overlap_tokens,python_target_tokens,python_overlap_tokens,"
            "chunk_config_identity,eval_contract_id,full_config_sha256,selection_state "
            "from public.embedding_contract_config_registry where selected_contract_id="
            + str(SELECTED_CONTRACT_ID)
            + " and selection_state in ('selected_draft','active')"
        )
        expected = {
            "selected_contract_id": SELECTED_CONTRACT_ID,
            "base_contract_id": HISTORICAL_DIMENSION_ONLY_ID,
            "provider": SELECTED_PROVIDER,
            "model": SELECTED_MODEL,
            "dimension": SELECTED_DIMENSION,
            "canonicalization_version": SELECTED_CANONICALIZATION_VERSION,
            "chunking_version": SELECTED_CHUNKING_VERSION,
            "chunk_config_version": CHUNK_CONFIG_IDENTITY_VERSION,
            "prose_target_tokens": SELECTED_PROSE_TARGET_TOKENS,
            "prose_overlap_tokens": SELECTED_PROSE_OVERLAP_TOKENS,
            "python_target_tokens": SELECTED_PYTHON_TARGET_TOKENS,
            "python_overlap_tokens": SELECTED_PYTHON_OVERLAP_TOKENS,
            "chunk_config_identity": SELECTED_CHUNK_CONFIG_IDENTITY,
            "eval_contract_id": EVAL_CONTRACT_ID_HEX,
            "full_config_sha256": SELECTED_CONTRACT_SHA256_HEX,
        }
        if not registry or any(str(registry.get(k)) != str(v) for k, v in expected.items()):
            raise ValueError("embedding_contract_config_incompatible")
        # In a production pgvector database this is literally vector(384).
        # The catalog check catches a mistakenly pointed sibling/migration table.
        physical = self.session.run(
            "select format_type(a.atttypid,a.atttypmod) from pg_attribute a "
            "join pg_class c on c.oid=a.attrelid join pg_namespace n on n.oid=c.relnamespace "
            "where n.nspname='public' and c.relname='content_embeddings' and a.attname='embedding' and not a.attisdropped;"
        )
        # Local deterministic rehearsals may use a text domain named ``vector``
        # because pgvector is intentionally not required there; production must
        # report the literal constrained type.
        if physical and physical not in {"vector", "vector(384)"}:
            raise ValueError("content_embeddings_fixed_dimension")
        self._contract_id = int(contract_id)

    def _source(self, source: str) -> tuple[str, str]:
        try:
            return _SOURCE_SQL[source]
        except KeyError as exc:
            raise ValueError("unsupported_source") from exc

    def high_water(self, source: str) -> str | None:
        table, predicate = self._source(source)
        alias = "d" if table == "distillations" else "m" if table == "discord_messages" else "r"
        row = self.session.json_one(
            f"select max({alias}.{ 'message_id' if table == 'discord_messages' else 'id' })::text as high_water "
            f"from public.{table} {alias} where {predicate}"
        )
        return str(row["high_water"]) if row and row.get("high_water") is not None else None

    def eligible_total(self, source: str) -> int:
        table, predicate = self._source(source)
        alias = "d" if table == "distillations" else "m" if table == "discord_messages" else "r"
        value = self.session.run(
            f"select count(*) from public.{table} {alias} where {predicate};"
        )
        return int(value or 0)

    def fetch_page(self, source: str, *, cursor: str | None, limit: int, since: str | None = None, until: str | None = None) -> list[dict[str, Any]]:
        table, predicate = self._source(source)
        if limit < 1:
            return []
        if table == "distillations":
            query = (
                "select 'distillation'::text as entity_type, d.id::text as item_id, "
                "d.question, d.conditions, d.answer, d.created_at "
                "from public.distillations d where " + predicate
            )
            ident, created = "d.id", "d.created_at"
        elif table == "external_resources":
            query = (
                "select 'resource'::text as entity_type, r.id::text as item_id, r.kind, r.title, r.body, "
                "r.metadata, r.payload, r.created_at from public.external_resources r where " + predicate
            )
            ident, created = "r.id", "r.created_at"
        else:
            query = (
                "select 'message'::text as entity_type, m.message_id::text as item_id, m.content, m.created_at "
                "from public.discord_messages m where " + predicate
            )
            ident, created = "m.message_id", "m.created_at"
        if cursor is not None:
            query += f" and {ident} > {_q(str(cursor))}::bigint"
        bound = self._bounds.get(source)
        if bound is not None:
            query += f" and {ident} <= {_q(bound)}::bigint"
        if since is not None:
            query += f" and {created} >= {_q(since)}::timestamptz"
        if until is not None:
            query += f" and {created} <= {_q(until)}::timestamptz"
        query += f" order by {ident} asc limit {int(limit)}"
        return self.session.json_rows(query)

    def current_freshness(self, source: str, keys: set[tuple[str, str, str, int]]) -> dict[tuple[str, str, str, int], str]:
        if not keys or self._contract_id is None:
            return {}
        values = ",".join(
            "(" + ",".join((_q(a), _q(b), _q(c), str(int(d)))) + ")"
            for a, b, c, d in sorted(keys)
        )
        rows = self.session.json_rows(
            "select e.entity_type,e.item_id,e.representation_type,e.chunk_index,e.representation_hash "
            "from public.content_embeddings e join (values " + values + ") as k(entity_type,item_id,representation_type,chunk_index) "
            "on (e.entity_type,e.item_id,e.representation_type,e.chunk_index)=(k.entity_type,k.item_id,k.representation_type,k.chunk_index) "
            f"where e.contract_id={int(self._contract_id)}"
        )
        return {(str(r["entity_type"]), str(r["item_id"]), str(r["representation_type"]), int(r["chunk_index"])): str(r["representation_hash"]) for r in rows}

    def representation_current(self, source: str, expected: dict[tuple[str, str, str], list[Any]]) -> dict[tuple[str, str, str], bool]:
        """Compare complete stored and expected chunk identity/hash sets.

        Fetching all rows for each representation is intentional: querying only
        expected indexes cannot see stale tail chunks left by an old chunker.
        """
        if not expected or self._contract_id is None:
            return {identity: False for identity in expected}
        values = ",".join("(" + ",".join(_q(v) for v in identity) + ")" for identity in sorted(expected))
        rows = self.session.json_rows(
            "select e.entity_type,e.item_id,e.representation_type,e.chunk_index,e.representation_hash,e.chunk_hash "
            "from public.content_embeddings e join (values " + values + ") as k(entity_type,item_id,representation_type) "
            "on (e.entity_type,e.item_id,e.representation_type)=(k.entity_type,k.item_id,k.representation_type) "
            "where e.contract_id=" + str(int(self._contract_id))
        )
        actual: dict[tuple[str, str, str], set[tuple[int, str, str]]] = {}
        for row in rows:
            identity = (str(row["entity_type"]), str(row["item_id"]), str(row["representation_type"]))
            actual.setdefault(identity, set()).add((int(row["chunk_index"]), str(row["representation_hash"]), str(row["chunk_hash"])))
        return {
            identity: actual.get(identity, set()) == {(c.chunk_index, c.representation_hash, c.chunk_hash) for c in chunks}
            for identity, chunks in expected.items()
        }

    def write_vectors(self, source: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        # A changed representation's chunks are replaced together.  The pure
        # engine only passes a representation when it is stale, so this removes
        # obsolete tail chunks without touching any source row.
        groups = sorted({(int(r["contract_id"]), str(r["entity_type"]), str(r["item_id"]), str(r["representation_type"])) for r in rows})
        deletes = "\n".join(
            "delete from public.content_embeddings where contract_id=%d and entity_type=%s and item_id=%s and representation_type=%s;" % (a, _q(b), _q(c), _q(d))
            for a, b, c, d in groups
        )
        values: list[str] = []
        for row in rows:
            vector = ec.validate_vectors([row["embedding"]], dimension=len(row["embedding"]))[0]
            literal = "[" + ",".join(format(float(v), ".17g") for v in vector) + "]"
            values.append("(" + ",".join((str(int(row["contract_id"])), _q(row["entity_type"]), _q(row["item_id"]), _q(row["representation_type"]), str(int(row["chunk_index"])), _q(row["chunk_text"]), _q(literal) + "::vector", _q(row["representation_hash"]), _q(row["chunk_hash"]))) + ")")
        self.session.run(
            "begin;\n" + deletes + "\ninsert into public.content_embeddings (contract_id,entity_type,item_id,representation_type,chunk_index,chunk_text,embedding,representation_hash,chunk_hash) values " + ",".join(values) + ";\ncommit;",
            timeout=300,
        )

    def write_manifest(self, source: str, entries: Iterable[Any]) -> None:
        """Upsert canonical chunk-manifest rows (task-2.17 worker payload source).

        The manifest is the ONE chunk source of truth the incremental worker
        reads (schema/034 hivemind_embedding_payload), so writing it here makes
        the backfill and the worker embed byte-identical chunks. A missing
        manifest table (pre-034 cluster) is a no-op.
        """
        entries = list(entries)
        if not entries:
            return
        present = self.session.run("select to_regclass('public.content_representation_manifest') is not null;")
        if present != "t":
            return
        value_rows: list[str] = []
        for entry in entries:
            d = entry.to_dict() if hasattr(entry, "to_dict") else dict(entry)
            value_rows.append(
                "(" + ",".join((
                    _q(d["entity_type"]), _q(d["item_id"]), _q(d["representation_type"]),
                    _q(d["representation_hash"]), _q(bool(d["source_available"])), _q(d["public_state"]),
                    _q(d.get("unavailable_reason")), str(int(d["chunking_version"])),
                    _q(d["chunk_config_identity"]), _q(d.get("chunk_method")),
                    _json(d["chunks"]), str(int(d["chunk_count"])),
                )) + ")"
            )
        self.session.run(
            "insert into public.content_representation_manifest "
            "(entity_type,item_id,representation_type,representation_hash,source_available,"
            "public_state,unavailable_reason,chunking_version,chunk_config_identity,chunk_method,"
            "chunks,chunk_count) values " + ",".join(value_rows) +
            " on conflict (entity_type,item_id,representation_type) do update set "
            "representation_hash=excluded.representation_hash,source_available=excluded.source_available,"
            "public_state=excluded.public_state,unavailable_reason=excluded.unavailable_reason,"
            "chunking_version=excluded.chunking_version,chunk_config_identity=excluded.chunk_config_identity,"
            "chunk_method=excluded.chunk_method,chunks=excluded.chunks,chunk_count=excluded.chunk_count,"
            "updated_at=now();",
            timeout=300,
        )

    def drop_representation(self, source: str, entity_type: str, item_id: str, representation_type: str) -> None:
        """Drop an item's stored vectors for one representation (task-2.17 defect #6).

        A workflow whose Python is now quarantined/unavailable must not leave its
        prior workflow_python vectors ranking. Idempotent; no source row touched.
        """
        if self._contract_id is None:
            return
        self.session.run(
            "delete from public.content_embeddings where contract_id=" + str(int(self._contract_id))
            + " and entity_type=" + _q(entity_type) + " and item_id=" + _q(item_id)
            + " and representation_type=" + _q(representation_type) + ";"
        )

    def create_run(self, *, contract_id: int, sources: tuple[str, ...], mode: str, high_water: dict[str, str | None]) -> dict[str, Any]:
        self._contract_id = int(contract_id)
        source_array = "array[" + ",".join(_q(s) for s in sources) + "]::text[]"
        run_id = int(self.session.run(
            f"select public.hivemind_create_embedding_backfill_run({int(contract_id)}, 'full', {source_array});"
        ))
        # schema/030 intentionally keeps create small; persist every source's
        # high-water immediately through its CAS RPC so an interruption before
        # the first provider batch still resumes the identical snapshot.
        version = 1
        for source in sources:
            resp = self.checkpoint(
                run_id, source, expected_version=version, cursor=None,
                high_water=high_water.get(source), processed=0, skipped=0,
                quarantined=0, unavailable=0, failed=0, eligible=0,
                last_error=None,
            )
            if not resp.get("ok"):
                raise RuntimeError("initial_snapshot_checkpoint_failed")
            version = int(resp.get("version", version))
        self.set_snapshot_bounds(high_water)
        return {"run_id": run_id, "version": version, "cursors": {s: None for s in sources}, "high_water": dict(high_water)}

    def load_run(self, run_id: int) -> dict[str, Any]:
        run = self.session.json_one(
            "select run_id,version,status,contract_id from public.embedding_backfill_runs where run_id=" + str(int(run_id))
        )
        if not run:
            raise RuntimeError("missing_backfill_run")
        cursors = self.session.json_rows(
            "select source,cursor,high_water,eligible_count,processed_count,skipped_count,quarantined_count,unavailable_count,failed_count from public.embedding_backfill_cursors where run_id=" + str(int(run_id))
        )
        self._contract_id = int(run["contract_id"])
        bounds = {str(c["source"]): (str(c["high_water"]) if c.get("high_water") is not None else None) for c in cursors}
        self.set_snapshot_bounds(bounds)
        run["cursors"] = {str(c["source"]): (str(c["cursor"]) if c.get("cursor") is not None else None) for c in cursors}
        run["high_water"] = bounds
        run["counters"] = {
            str(c["source"]): {
                "eligible": int(c.get("eligible_count") or 0), "processed": int(c.get("processed_count") or 0),
                "skipped": int(c.get("skipped_count") or 0), "quarantined": int(c.get("quarantined_count") or 0),
                "unavailable": int(c.get("unavailable_count") or 0), "failed": int(c.get("failed_count") or 0),
            } for c in cursors
        }
        return run

    def checkpoint(self, run_id: int, source: str, *, expected_version: int, cursor: str | None, high_water: str | None, processed: int, skipped: int, quarantined: int, unavailable: int, failed: int, eligible: int, last_error: str | None) -> dict[str, Any]:
        row = self.session.json_one(
            "select public.hivemind_checkpoint_embedding_backfill(" + ",".join((str(int(run_id)), _q(source), str(int(expected_version)), _q(cursor), _q(high_water), str(int(processed)), str(int(skipped)), str(int(quarantined)), str(int(unavailable)), str(int(failed)), str(int(eligible)), _q(last_error))) + ") as value"
        )
        return dict(row.get("value") or {}) if row else {"ok": False, "reason": "rpc_missing"}

    def complete_run(self, run_id: int, expected_version: int) -> dict[str, Any]:
        row = self.session.json_one(f"select public.hivemind_complete_embedding_backfill_run({int(run_id)},{int(expected_version)}) as value")
        return dict(row.get("value") or {}) if row else {"ok": False}

    def fail_run(self, run_id: int, expected_version: int, error: str | None) -> dict[str, Any]:
        row = self.session.json_one("select public.hivemind_fail_embedding_backfill_run(" + ",".join((str(int(run_id)), str(int(expected_version)), _q(error))) + ") as value")
        return dict(row.get("value") or {}) if row else {"ok": False}


class PostgresWorkflowStore:
    """Source-enrichment store: only workflow payload/body/metadata may change."""

    def __init__(self, session: PsqlSession) -> None:
        self.session = session

    def high_water(self) -> int | None:
        value = self.session.run("select max(id) from public.external_resources where kind='workflow';")
        return int(value) if value else None

    # -- task-2.12 durable remediation lifecycle ------------------------
    def create_remediation_run(self, high_water: int | None) -> dict[str, Any]:
        run_id = int(self.session.run(
            "select public.hivemind_create_workflow_remediation_run(" + _q(high_water) + ");"
        ))
        return {"run_id": run_id, "version": 1, "cursor": None, "high_water": high_water, "counters": {}}

    def load_remediation_run(self, run_id: int) -> dict[str, Any]:
        row = self.session.json_one(
            "select r.run_id,r.version,r.status,c.cursor,c.high_water,r.processed_count,r.materialized_count,r.skipped_count,r.unavailable_count,r.failed_count "
            "from public.workflow_remediation_runs r join public.workflow_remediation_cursors c using(run_id) where r.run_id=" + str(int(run_id))
        )
        if not row or row.get("status") != "running":
            raise RuntimeError("missing_or_terminal_remediation_run")
        return {
            "run_id": int(row["run_id"]), "version": int(row["version"]),
            "cursor": int(row["cursor"]) if row.get("cursor") is not None else None,
            "high_water": int(row["high_water"]) if row.get("high_water") is not None else None,
            "counters": {"processed": int(row["processed_count"]), "materialized": int(row["materialized_count"]),
                         "skipped": int(row["skipped_count"]), "unavailable": int(row["unavailable_count"]), "failed": int(row["failed_count"])},
        }

    def checkpoint_remediation(self, run_id: int, expected_version: int, cursor: int | None,
                               high_water: int | None, processed: int, materialized: int,
                               skipped: int, unavailable: int, failed: int, reason: str | None,
                               failed_resource_id: int | None) -> dict[str, Any]:
        row = self.session.json_one(
            "select public.hivemind_checkpoint_workflow_remediation(" + ",".join((
                str(int(run_id)), str(int(expected_version)), _q(cursor), _q(high_water),
                str(int(processed)), str(int(materialized)), str(int(skipped)), str(int(unavailable)),
                str(int(failed)), _q(reason), _q(failed_resource_id),
            )) + ") as value"
        )
        return dict(row.get("value") or {}) if row else {"ok": False}

    def complete_remediation_run(self, run_id: int, expected_version: int) -> dict[str, Any]:
        row = self.session.json_one(
            "select public.hivemind_complete_workflow_remediation(" + str(int(run_id)) + "," + str(int(expected_version)) + ") as value"
        )
        return dict(row.get("value") or {}) if row else {"ok": False}

    def eligible_total(self) -> int:
        return int(self.session.run("select count(*) from public.external_resources where kind='workflow';") or 0)

    def fetch_page(self, *, after_id: int | None, high_water: int | None, limit: int) -> list[dict[str, Any]]:
        where = "kind='workflow'"
        if after_id is not None:
            where += f" and id>{int(after_id)}"
        if high_water is not None:
            where += f" and id<={int(high_water)}"
        return self.session.json_rows("select id,kind,title,body,url,metadata,payload,source,external_id,created_at from public.external_resources where " + where + f" order by id asc limit {int(limit)}")

    def current(self, row_id: int) -> dict[str, Any] | None:
        return self.session.json_one("select id,payload,body,metadata from public.external_resources where id=" + str(int(row_id)))

    def patch(self, row_id: int, update: dict[str, Any]) -> None:
        # Identity/native artifacts are deliberately not in this update list.
        self.session.run("update public.external_resources set payload=" + _json(update["payload"]) + ", body=" + _q(update["body"]) + ", metadata=" + _json(update["metadata"]) + " where id=" + str(int(row_id)) + " and kind='workflow';")

    def refresh(self, row_id: int) -> None:
        # The existing lexical contract derives from authoritative source rows.
        # A full refresh is intentionally left to its established operator
        # command; this trigger-free call only refreshes derived workflow state
        # if task-1.2 tables are installed.
        exists = self.session.run("select to_regclass('public.lexical_resource_python_state') is not null;")
        if exists != "t":
            return
        row = self.current(row_id)
        if not row:
            raise RuntimeError("missing_workflow_after_patch")
        row["kind"] = "workflow"
        state, docs = ld.compute_workflow_python_documents(row)
        state_values = ",".join((
            _q(state.resource_id), _q(state.kind), _q(state.cohort), _q(state.public_state),
            _q(state.available), _q(state.body_duplicate), _q(state.delimiter), _q(state.derivation),
            _q(state.representation_hash), "array[" + ",".join(_q(x) for x in state.secret_reason_codes) + "]::text[]",
            str(state.canonicalization_version), str(state.secret_scan_version), str(state.chunking_version), str(state.chunk_count),
        ))
        docs_values = []
        for doc in docs:
            docs_values.append("(" + ",".join((
                _q(doc.entity_type), _q(doc.item_id), _q(doc.representation_type), str(doc.chunk_index),
                _q(doc.chunk_text), _q(doc.matched_anchor), str(doc.source_offset_start), str(doc.source_offset_end),
                _q(doc.representation_hash), _q(doc.chunk_hash), _q(doc.quarantine_state), str(doc.lexicalization_version),
                str(doc.canonicalization_version), str(doc.chunking_version), str(doc.secret_scan_version), _q(doc.method),
            )) + ")")
        sql = (
            "begin;\n"
            "delete from public.lexical_documents where entity_type='resource' and item_id=" + _q(str(row_id)) + " and representation_type='workflow_python';\n"
            "delete from public.lexical_resource_python_state where resource_id=" + str(int(row_id)) + ";\n"
            "insert into public.lexical_resource_python_state (resource_id,kind,cohort,public_state,available,body_duplicate,delimiter,derivation,representation_hash,secret_reason_codes,canonicalization_version,secret_scan_version,chunking_version,chunk_count) values (" + state_values + ");\n"
        )
        if docs_values:
            sql += "insert into public.lexical_documents (entity_type,item_id,representation_type,chunk_index,chunk_text,matched_anchor,source_offset_start,source_offset_end,representation_hash,chunk_hash,quarantine_state,lexicalization_version,canonicalization_version,chunking_version,secret_scan_version,method) values " + ",".join(docs_values) + ";\n"
        self.session.run(sql + "commit;", timeout=300)

    def record_failure(self, row_id: int, reason: str) -> None:
        # The lifecycle checkpoint records the retryable row failure in its own
        # owner-only table.  Keep this method deliberately source-row inert.
        _ = (row_id, reason)


def vibecomfy_recoverer(vibecomfy_root: Path | None) -> Callable[[dict[str, Any]], str | None]:
    """Build the approved VibeComfy recovery adapter; never execute stored code."""
    def recover(row: dict[str, Any]) -> str | None:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        candidate: Any = payload.get("workflow_json") or payload.get("workflow") or payload.get("compiled_api")
        corpus = payload.get("corpus_path")
        if candidate is None and isinstance(corpus, str) and vibecomfy_root is not None:
            root = vibecomfy_root.resolve()
            path = (root / corpus).resolve() if not Path(corpus).is_absolute() else Path(corpus).resolve()
            if root not in path.parents and path != root:
                raise wrm.CorpusPathRejected("corpus_path_unauthorized")
            candidate = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(candidate, dict):
            return None
        # Import only when recovery is actually required.  This calls the
        # existing VibeComfy parser/converter; it never imports/executes the
        # recovered Python text.
        if vibecomfy_root is not None:
            import sys
            root_text = str(vibecomfy_root)
            if root_text not in sys.path:
                sys.path.insert(0, root_text)
        from vibecomfy.porting.convert import port_convert_workflow  # type: ignore[import-not-found]
        from vibecomfy.registry import workflow_from_file  # type: ignore[import-not-found]
        # The existing registry parser owns JSON-format detection/conversion.
        # It reads a short-lived JSON artifact, never arbitrary Python.
        with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as tmp:
            json.dump(candidate, tmp, ensure_ascii=False)
            source_path = Path(tmp.name)
        try:
            workflow = workflow_from_file(str(source_path))
            return str(port_convert_workflow(workflow, raw_workflow=candidate, validate=False).text)
        finally:
            source_path.unlink(missing_ok=True)
    return recover


class WorkflowRemediationStage:
    """Adapter that makes remediation the mandatory first backfill stage."""
    def __init__(self, store: PostgresWorkflowStore, *, vibecomfy_root: Path | None, batch_size: int = 100, sample: int | None = None) -> None:
        self.store, self.vibecomfy_root, self.batch_size, self.sample = store, vibecomfy_root, batch_size, sample

    def run(self, _embedding_store: Any, *, apply: bool) -> eb.RemediationReport:
        report = wrm.reconcile(self.store, recoverer=vibecomfy_recoverer(self.vibecomfy_root), vibecomfy_root=self.vibecomfy_root, apply=apply, batch_size=self.batch_size, sample=self.sample)
        return eb.RemediationReport(ok=not bool(report.get("failed")), summary={k: report[k] for k in ("processed", "materialized", "unavailable", "failed", "cursor") if k in report})


class OpenAIProvider:
    """Synchronous bridge to Hivemind's existing async OpenAI embedder."""
    def __init__(self, *, dimension: int = 384, api_key: str | None = None) -> None:
        self._embedder = ec.OpenAIEmbedder(api_key=api_key, dimension=dimension)
        self.provider_name = "openai"
        self.model_name, self.dimension = self._embedder.model_name, self._embedder.dimension
        if not self._embedder.has_credential():
            raise RuntimeError("embedding_provider_key_missing")

    def embed(self, texts: Iterable[str]) -> list[list[float]]:
        try:
            return asyncio.run(self._embedder.embed_texts(list(texts)))
        except ec.EmbeddingError as exc:
            # The established provider boundary only exposes safe category text
            # here.  HTTP 4xx and malformed response/validation failures cannot
            # improve by retrying; 429/5xx/transport remain retryable.
            category = str(exc)
            if any(token in category for token in ("HTTP 400", "HTTP 401", "HTTP 403", "HTTP 404", "HTTP 422", "missing 'data'", "returned ")):
                raise eb.PermanentProviderError(type(exc).__name__) from exc
            raise eb.TransientProviderError(type(exc).__name__) from exc


class PacingRateLimiter:
    def __init__(self, per_minute: int | None) -> None:
        self._interval = 60.0 / per_minute if per_minute and per_minute > 0 else 0.0
        self._last = 0.0

    def acquire(self) -> None:
        if self._interval:
            remaining = self._interval - (time.monotonic() - self._last)
            if remaining > 0:
                time.sleep(remaining)
        self._last = time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))

    def now(self) -> float:
        return time.monotonic()
