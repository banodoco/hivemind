"""Focused task-2.12c adapter tests (offline; no database or provider)."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from executors import postgres_backfill as pg  # noqa: E402


class RecordingSession:
    def __init__(self) -> None:
        self.sql: list[str] = []
        self.rows: list[dict] = []
        self.scalar = ""

    def run(self, sql: str, *, timeout: float = 120.0) -> str:
        self.sql.append(sql)
        return self.scalar

    def json_rows(self, sql: str):
        self.sql.append(sql)
        return list(self.rows)

    def json_one(self, sql: str):
        self.sql.append(sql)
        return self.rows[0] if self.rows else None


class SessionBoundaryTests(unittest.TestCase):
    def test_url_becomes_libpq_environment_not_process_argv(self):
        env = pg.PsqlSession._connection_env(
            "postgresql://user:secret-pass@localhost:5432/hivemind?sslmode=require"
        )
        self.assertEqual(env["PGHOST"], "localhost")
        self.assertEqual(env["PGPASSWORD"], "secret-pass")
        self.assertNotIn("DATABASE_URL", env)

    def test_psql_transport_does_not_stage_sql_in_a_named_file(self):
        source = Path(pg.__file__).read_text(encoding="utf-8")
        self.assertIn("input=\"set client_min_messages=warning;", source)
        self.assertNotIn('"-f", str(path)', source)


class SourcePagingTests(unittest.TestCase):
    def test_message_page_uses_numeric_snowflake_cursor_and_high_water(self):
        session = RecordingSession()
        session.rows = [{"entity_type": "message", "item_id": "9007199254740993", "content": "x"}]
        store = pg.PostgresBackfillStore(session)  # type: ignore[arg-type]
        store.set_snapshot_bounds({"messages": "9007199254740993"})
        rows = store.fetch_page("messages", cursor="9007199254740992", limit=5,
                                since="2025-01-01", until="2025-12-31")
        self.assertEqual(rows[0]["item_id"], "9007199254740993")
        sql = session.sql[-1]
        self.assertIn("m.message_id > '9007199254740992'::bigint", sql)
        self.assertIn("m.message_id <= '9007199254740993'::bigint", sql)
        self.assertIn("order by m.message_id asc", sql)

    def test_source_rejects_unknown_names(self):
        with self.assertRaises(ValueError):
            pg.PostgresBackfillStore(RecordingSession()).fetch_page("unknown", cursor=None, limit=1)  # type: ignore[arg-type]


class WriteSafetyTests(unittest.TestCase):
    def test_vector_replacement_only_touches_derived_table(self):
        session = RecordingSession()
        store = pg.PostgresBackfillStore(session)  # type: ignore[arg-type]
        vector = [1.0] + [0.0] * 7
        store.write_vectors("messages", [{
            "contract_id": 7, "entity_type": "message", "item_id": "9007199254740993",
            "representation_type": "prose", "chunk_index": 0, "chunk_text": "safe text",
            "embedding": vector, "representation_hash": "a" * 64, "chunk_hash": "b" * 64,
        }])
        sql = session.sql[-1]
        self.assertIn("delete from public.content_embeddings", sql)
        self.assertIn("insert into public.content_embeddings", sql)
        self.assertNotIn("external_resources", sql)
        self.assertNotIn("embedding_contracts", sql)
        self.assertNotIn("hnsw", sql.lower())

    def test_complete_representation_freshness_queries_all_stored_chunks(self):
        session = RecordingSession()
        store = pg.PostgresBackfillStore(session)  # type: ignore[arg-type]
        store._contract_id = 7
        class Chunk:
            chunk_index = 0; representation_hash = "a" * 64; chunk_hash = "b" * 64
        store.representation_current("messages", {("message", "1", "prose"): [Chunk()]})
        self.assertIn("from public.content_embeddings", session.sql[-1])
        self.assertNotIn("chunk_index)=", session.sql[-1])

    def test_workflow_patch_has_no_identity_or_artifact_column(self):
        session = RecordingSession()
        store = pg.PostgresWorkflowStore(session)  # type: ignore[arg-type]
        store.patch(22, {"payload": {"python_source": "x=1"}, "body": "prose", "metadata": {"x": 1}})
        sql = session.sql[-1]
        self.assertIn("set payload=", sql)
        self.assertIn(" body=", sql)
        self.assertIn(" metadata=", sql)
        for forbidden in ("title=", "url=", "source=", "external_id=", "id="):
            self.assertNotIn(forbidden, sql.split(" set ", 1)[1].split(" where ", 1)[0])


class ReportShapeTests(unittest.TestCase):
    def test_sql_strings_never_require_a_shell_or_database_url(self):
        session = RecordingSession()
        store = pg.PostgresBackfillStore(session)  # type: ignore[arg-type]
        store.fetch_page("resources", cursor=None, limit=1)
        self.assertNotIn("postgresql://", session.sql[-1])
        self.assertNotIn("password", session.sql[-1].lower())
