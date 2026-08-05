"""Tests for plan task 1.3 — the canonical Discord-message FTS index.

Covers the deliverables the task names: dry-run, preflight verdict logic, unit
(frozen identity / SQL text), SQL plan parsing (index usage), idempotence, and
cancellation/rollback behavior.

Offline by default (no network, no live DB, no provider): the frozen-identity,
SQL-text, preflight-verdict, redaction, and plan-parsing tests pin pure logic,
and the rehearsal/live evidence artifacts captured by the operator scripts are
asserted as offline JSON. Two integration classes (idempotence, cancellation)
spin a throwaway local PostgreSQL cluster and are skipped automatically when PG
binaries are absent, so the suite still runs green on a DB-less machine.
"""

from __future__ import annotations

import json
import shutil
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

import scripts.discord_message_fts as M  # noqa: E402
import executors.lexical_contract as LC  # noqa: E402
from verify_access import redact  # noqa: E402

SCHEMA_004 = _REPO / "schema" / "004_discord_message_fts.sql"
REHEARSAL_JSON = _REPO / "docs" / "hybrid-search" / "phase1-message-fts-rehearsal.json"
LIVE_JSON = _REPO / "docs" / "hybrid-search" / "phase1-message-fts-live.json"


# ---------------------------------------------------------------------------
# Frozen identity (cross-checked against the 1.1 lexical contract)
# ---------------------------------------------------------------------------

class TestFrozenIdentity(unittest.TestCase):
    def test_config_and_column_match_frozen_contract(self):
        self.assertEqual(M.LEXICAL_CONFIG, LC.LEXICAL_CONFIG)
        self.assertEqual(M.LEXICAL_CONFIG, "simple")
        self.assertEqual(M.INDEX_COLUMN, LC.MESSAGE_BARE_SOURCE)
        self.assertEqual(M.INDEX_COLUMN, "content")

    def test_index_name_is_frozen(self):
        self.assertEqual(M.INDEX_NAME, "idx_discord_messages_content_fts_simple")
        self.assertNotEqual(M.INDEX_NAME, M.ENGLISH_INDEX_NAME)

    def test_expression_is_the_frozen_one(self):
        self.assertEqual(M.index_expression(),
                         "to_tsvector('simple'::regconfig, coalesce(content, ''))")

    def test_table_is_the_underlying_table_not_a_view(self):
        self.assertEqual(M.fully_qualified_table(), "public.discord_messages")


# ---------------------------------------------------------------------------
# Build / rollback SQL text + idempotence (dry-run shape)
# ---------------------------------------------------------------------------

class TestBuildAndRollbackSQL(unittest.TestCase):
    def test_build_statement_shape(self):
        sql = M.build_statement()
        self.assertIn("SET lock_timeout = '30s';", sql)
        self.assertIn("SET statement_timeout = '1800s';", sql)
        self.assertIn("CREATE INDEX CONCURRENTLY IF NOT EXISTS", sql)
        self.assertIn(M.INDEX_NAME, sql)
        self.assertIn("USING gin", sql)
        self.assertIn(M.index_expression(), sql)
        # Outside a transaction: no BEGIN/COMMIT wrapper.
        self.assertNotIn("BEGIN", sql)
        self.assertNotIn("COMMIT", sql)

    def test_build_statement_timeout_optional(self):
        sql = M.build_statement(statement_timeout_s=None)
        self.assertNotIn("statement_timeout", sql)
        self.assertIn("lock_timeout", sql)

    def test_build_statement_custom_timeouts(self):
        sql = M.build_statement(lock_timeout_s=10, statement_timeout_s=90)
        self.assertIn("SET lock_timeout = '10s';", sql)
        self.assertIn("SET statement_timeout = '90s';", sql)

    def test_rollback_statement_shape(self):
        sql = M.rollback_statement()
        self.assertEqual(sql,
                         "DROP INDEX CONCURRENTLY IF EXISTS public."
                         "idx_discord_messages_content_fts_simple;")

    def test_idempotent_reapply_and_redrop_are_no_ops(self):
        # IF NOT EXISTS => a second CREATE is a safe no-op; IF EXISTS => ditto drop.
        self.assertIn("IF NOT EXISTS", M.build_statement())
        self.assertIn("IF EXISTS", M.rollback_statement())

    def test_expression_in_build_matches_frozen(self):
        # The expression executed must equal the indexed expression exactly,
        # or the planner cannot match the expression index.
        self.assertIn(M.index_expression(), M.build_statement())


# ---------------------------------------------------------------------------
# schema/004 artifact consistency (no drift between file and module)
# ---------------------------------------------------------------------------

class TestSchemaFile(unittest.TestCase):
    def setUp(self):
        self.sql = SCHEMA_004.read_text()

    def test_file_exists_and_has_guard(self):
        self.assertTrue(SCHEMA_004.exists())
        self.assertIn("preflight guard", self.sql)
        self.assertIn("discord_messages.content", self.sql)

    def test_file_uses_concurrent_idempotent_index(self):
        self.assertIn("CREATE INDEX CONCURRENTLY IF NOT EXISTS", self.sql)
        self.assertIn(M.INDEX_NAME, self.sql)
        self.assertIn(M.index_expression(), self.sql)

    def test_file_documents_rollback(self):
        self.assertIn("DROP INDEX CONCURRENTLY IF EXISTS", self.sql)
        self.assertIn(M.INDEX_NAME, self.sql)

    def test_file_keeps_english_index(self):
        # The migration neither drops nor depends on the english index.
        self.assertIn("english", self.sql)
        self.assertNotIn("DROP INDEX idx_discord_messages_content_fts;", self.sql)

    def test_no_transaction_block_around_concurrent(self):
        # The file is applied with autocommit; it must not wrap CIC in a txn.
        self.assertNotIn("BEGIN;", self.sql)
        self.assertNotIn("COMMIT;", self.sql)

    def test_expression_matches_module_exactly(self):
        self.assertIn(M.index_expression(), self.sql)


# ---------------------------------------------------------------------------
# Preflight verdict logic (pure, on synthetic parsed inputs)
# ---------------------------------------------------------------------------

def _pf(**overrides):
    """A clean-green preflight parsed-input base, overridable per test."""
    base = {
        "table_column_identity": [["discord_messages", "content", "text", "f", "1250000", "1250000"]],
        "existing_fts_indexes": [["idx_discord_messages_content_fts", "t", "t", "CREATE INDEX ...english...", "89000000"]],
        "invalid_index_remnants": [],
        "in_progress_index_builds": [],
        "database_storage": [["postgres", "2260000000", "100"]],
        "long_or_locking_transactions": [],
        "relation_locks": [],
        "timeout_and_maintenance_settings": [
            ["statement_timeout", "120000", "ms"],
            ["lock_timeout", "0", "ms"],
            ["maintenance_work_mem", "131072", "kB"],
        ],
    }
    base.update(overrides)
    return base


class TestPreflightVerdict(unittest.TestCase):
    def test_green_on_clean_session_connection(self):
        v = M.evaluate_preflight(_pf(), pghost="aws-0.pooler.supabase.com", pgport="5432")
        self.assertTrue(v["green"], v["reasons"])
        self.assertEqual(v["conn_mode"], "session")
        self.assertFalse(v["already_valid"])  # target absent

    def test_green_on_direct_connection(self):
        v = M.evaluate_preflight(_pf(), pghost="db.ujlwuvkrxlvoswwkerdf.supabase.co", pgport="5432")
        self.assertTrue(v["green"])
        self.assertEqual(v["conn_mode"], "direct")

    def test_red_on_transaction_pooler(self):
        v = M.evaluate_preflight(_pf(), pghost="aws-0.pooler.supabase.com", pgport="6543")
        self.assertFalse(v["green"])
        self.assertEqual(v["conn_mode"], "transaction_pooler")

    def test_red_on_invalid_remnant(self):
        v = M.evaluate_preflight(_pf(invalid_index_remnants=[
            [M.INDEX_NAME, "f", "f", "CREATE INDEX CONCURRENTLY ... simple ..."]]),
            pghost="db.x.supabase.co", pgport="5432")
        self.assertFalse(v["green"])
        self.assertTrue(any("invalid" in r for r in v["reasons"]))

    def test_red_on_in_progress_build(self):
        v = M.evaluate_preflight(_pf(in_progress_index_builds=[
            ["123", "public.discord_messages", "CREATE INDEX", "building index", "100", "200", "50", "100"]]),
            pghost="db.x.supabase.co", pgport="5432")
        self.assertFalse(v["green"])
        self.assertTrue(any("in progress" in r for r in v["reasons"]))

    def test_red_on_insufficient_headroom(self):
        # Tiny disk, large projected index -> headroom fails.
        v = M.evaluate_preflight(_pf(database_storage=[["postgres", str(8 * 10**9 - 10**8), "100"]]),
                                 pghost="db.x.supabase.co", pgport="5432",
                                 disk_bytes=8 * 10**9, est_rows_override=1_250_000)
        self.assertFalse(v["green"])
        self.assertTrue(any("headroom" in r for r in v["reasons"]))

    def test_red_on_wrong_column_type(self):
        v = M.evaluate_preflight(_pf(table_column_identity=[
            ["discord_messages", "content", "bytea", "f", "1250000", "1250000"]]),
            pghost="db.x.supabase.co", pgport="5432")
        self.assertFalse(v["green"])

    def test_red_on_missing_table(self):
        v = M.evaluate_preflight(_pf(table_column_identity=[]),
                                 pghost="db.x.supabase.co", pgport="5432")
        self.assertFalse(v["green"])

    def test_already_valid_detected(self):
        v = M.evaluate_preflight(_pf(existing_fts_indexes=[
            [M.INDEX_NAME, "t", "t", "CREATE INDEX ... simple ...", "67000000"],
            ["idx_discord_messages_content_fts", "t", "t", "CREATE INDEX ... english ...", "85000000"]]),
            pghost="db.x.supabase.co", pgport="5432")
        self.assertTrue(v["green"])
        self.assertTrue(v["already_valid"])

    def test_estimate_scales_with_rows(self):
        self.assertLess(M.estimate_index_bytes(1000), M.estimate_index_bytes(1_250_000))
        self.assertGreater(M.estimate_index_bytes(1_250_000), 50_000_000)  # ~109MB


# ---------------------------------------------------------------------------
# EXPLAIN plan parsing — assert index usage (offline, on captured plan text)
# ---------------------------------------------------------------------------

# Captured from the live build (phase1-message-fts-live.json) — the proof.
LIVE_WEBSEARCH_PLAN = """\
Limit (actual time=8.207..10.619 rows=20 loops=1)
  ->  Gather Merge (actual time=8.205..10.614 rows=20 loops=1)
        ->  Sort (actual time=5.128..5.130 rows=15 loops=2)
              ->  Parallel Bitmap Heap Scan on discord_messages m
                    Filter: (NOT is_deleted)
                    ->  Bitmap Index Scan on idx_discord_messages_content_fts_simple (actual time=0.348..0.348 rows=116 loops=1)
Planning Time: 2.806 ms
Execution Time: 10.722 ms"""

BASELINE_SEQ_PLAN = """\
Limit (actual time=0.037..37.977 rows=20 loops=1)
  ->  Seq Scan on discord_messages m
        Filter: ((NOT is_deleted) AND (to_tsvector('simple'::regconfig, COALESCE(content, ''::text)) @@ '''wanvideosampler'''::tsquery))
Execution Time: 37.988 ms"""


class TestExplainPlanParsing(unittest.TestCase):
    def test_detects_simple_index_use(self):
        p = M.parse_explain_plan(LIVE_WEBSEARCH_PLAN)
        self.assertTrue(p["uses_simple_index"])
        self.assertFalse(p["uses_english_index"])
        self.assertFalse(p["is_seq_scan"])

    def test_detects_seq_scan_baseline(self):
        p = M.parse_explain_plan(BASELINE_SEQ_PLAN)
        self.assertTrue(p["is_seq_scan"])
        self.assertFalse(p["uses_simple_index"])
        self.assertFalse(p["uses_english_index"])

    def test_empty_plan_is_not_an_index_use(self):
        p = M.parse_explain_plan("")
        self.assertFalse(p["uses_simple_index"])
        self.assertFalse(p["plan_present"])

    def test_english_index_not_conflated_with_simple(self):
        p = M.parse_explain_plan("Bitmap Index Scan on idx_discord_messages_content_fts")
        self.assertTrue(p["uses_english_index"])
        self.assertFalse(p["uses_simple_index"])


# ---------------------------------------------------------------------------
# Evidence artifacts (offline JSON assertions) — rehearsal + live
# ---------------------------------------------------------------------------

class TestRehearsalEvidence(unittest.TestCase):
    def setUp(self):
        if not REHEARSAL_JSON.exists():
            self.skipTest("rehearsal artifact not captured")
        self.ev = json.loads(REHEARSAL_JSON.read_text())

    def test_verdict_all_pass(self):
        self.assertTrue(self.ev["verdict"]["all_pass"], self.ev["verdict"])

    @unittest.skipUnless(REHEARSAL_JSON.exists(), "rehearsal artifact not captured")
    def test_production_row_count(self):
        # The rehearsal ran at ~1.25M rows (production scale).
        self.assertGreaterEqual(self.ev["seed"]["actual_rows"], 1_000_000)

    @unittest.skipUnless(REHEARSAL_JSON.exists(), "rehearsal artifact not captured")
    def test_online_build_is_valid_and_uses_index(self):
        self.assertEqual(self.ev["online_build"]["indisvalid"], "t")
        for label, p in self.ev["evidence_plans"].items():
            self.assertTrue(p["uses_simple_index"], label)
            self.assertFalse(p["uses_english_index"], label)

    @unittest.skipUnless(REHEARSAL_JSON.exists(), "rehearsal artifact not captured")
    def test_baseline_cannot_use_english_index(self):
        b = self.ev["baseline_no_simple_index"]
        self.assertFalse(b["uses_simple_index"])
        self.assertFalse(b["uses_english_index"])
        self.assertTrue(b["is_seq_scan"])  # falls back to a seq scan, not the english idx

    @unittest.skipUnless(REHEARSAL_JSON.exists(), "rehearsal artifact not captured")
    def test_cancellation_leaves_invalid_index_then_rollback_and_rebuild(self):
        c = self.ev["cancellation_rollback"]
        self.assertEqual(c["indisvalid_after_interrupt"], "f")        # invalid remnant
        self.assertTrue(c["interrupt_left_invalid_index"])
        self.assertEqual(c["rollback"]["index_remains_after_drop"], 0)  # DROP CONCURRENTLY cleans it
        self.assertEqual(c["indisvalid_after_rebuild"], "t")           # recoverable


class TestLiveEvidence(unittest.TestCase):
    def setUp(self):
        self.ev = json.loads(LIVE_JSON.read_text())

    def test_build_succeeded_online(self):
        self.assertEqual(self.ev["build"]["status"], "ok")
        self.assertGreater(self.ev["build"]["elapsed_s"], 0)
        self.assertGreater(len(self.ev["build"]["samples"]), 0)

    def test_index_is_valid_and_reasonable_size(self):
        self.assertEqual(self.ev["evidence"]["index_valid"], "t")
        # Real live index is in the production ballpark (english was 85 MB).
        self.assertGreater(self.ev["evidence"]["index_size_bytes"], 10_000_000)

    def test_representative_queries_use_simple_index_with_real_hits(self):
        for label, p in self.ev["evidence"]["evidence_plans"].items():
            self.assertTrue(p["uses_simple_index"], label)
            self.assertFalse(p["uses_english_index"], label)
            self.assertFalse(p["is_seq_scan"], label)
        for term, n in self.ev["evidence"]["representative_hit_counts"].items():
            self.assertIsNotNone(n, term)
            self.assertGreater(n, 0, term)  # is_deleted=false encoded; real matches

    def test_preflight_was_green_on_session_connection(self):
        self.assertTrue(self.ev["preflight"]["verdict"]["green"])
        self.assertEqual(self.ev["preflight"]["verdict"]["conn_mode"], "session")


# ---------------------------------------------------------------------------
# Redaction boundary (the driver routes everything through verify_access.redact)
# ---------------------------------------------------------------------------

class TestRedactionBoundary(unittest.TestCase):
    def test_secrets_masked_in_output(self):
        for secret in (
            "postgresql://postgres.ujlwuvkrxlvoswwkerdf:hunter2@aws-0-eu-central-1.pooler.supabase.com:5432/postgres",
            'PGPASSWORD="gLunEFoVilWmLxhBiQMyjjYdNU6rkcqz"',
        ):
            out = redact(f"build result: {secret}")
            self.assertNotIn("hunter2", out)
            self.assertNotIn("gLunEFoVilWmLxhBiQMyjjYdNU6rkcqz", out)

    def test_safe_identifiers_preserved(self):
        # Short standalone identifiers survive; only 32+ char opaque runs mask.
        out = redact("ON public.discord_messages USING gin")
        self.assertIn("discord_messages", out)
        self.assertIn("USING", out)


# ---------------------------------------------------------------------------
# Integration: idempotence + cancellation in a throwaway local cluster
# (skipped automatically when PostgreSQL binaries are absent)
# ---------------------------------------------------------------------------

_HAS_PG = all(shutil.which(b) for b in ("initdb", "pg_ctl", "psql", "postgres"))


@unittest.skipUnless(_HAS_PG, "PostgreSQL binaries not available")
class IdempotenceIntegrationTests(unittest.TestCase):
    """Apply the build/drop cycle on a tiny isolated cluster; second ops are no-ops."""

    def test_double_apply_and_double_drop_are_safe(self):
        import importlib
        rehearse = importlib.import_module("rehearse_discord_fts")
        db = rehearse.RehearsalCluster()
        try:
            db.start()
            db.sql_t(M.rehearsal_schema_sql())
            db.sql_t(M.rehearsal_seed_sql(2000))

            def valid() -> str:
                return db.sql_t(
                    f"SELECT indisvalid FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                    f"WHERE c.relname='{M.INDEX_NAME}';").strip().splitlines()[-1]

            # First apply creates a valid index.
            db.sql_t(M.build_statement(), timeout=120)
            self.assertEqual(valid(), "t")
            # Second apply is a no-op (IF NOT EXISTS): still exactly one valid index.
            db.sql_t(M.build_statement(), timeout=120)
            self.assertEqual(valid(), "t")
            n = int(db.sql_t(
                f"SELECT count(*) FROM pg_class WHERE relname='{M.INDEX_NAME}';"
            ).strip().splitlines()[-1])
            self.assertEqual(n, 1)
            # First drop removes it; second drop is a no-op (IF EXISTS), no error.
            db.sql_t(M.rollback_statement(), timeout=120)
            db.sql_t(M.rollback_statement(), timeout=120)
            n2 = int(db.sql_t(
                f"SELECT count(*) FROM pg_class WHERE relname='{M.INDEX_NAME}';"
            ).strip().splitlines()[-1])
            self.assertEqual(n2, 0)
        finally:
            db.stop()
            db.destroy()


@unittest.skipUnless(_HAS_PG, "PostgreSQL binaries not available")
class CancellationIntegrationTests(unittest.TestCase):
    """A cancelled concurrent build leaves an INVALID index; rollback recovers."""

    def test_cancel_then_rollback_then_rebuild(self):
        import importlib
        rehearse = importlib.import_module("rehearse_discord_fts")
        db = rehearse.RehearsalCluster()
        try:
            db.start()
            db.sql_t(M.rehearsal_schema_sql())
            db.sql_t(M.rehearsal_seed_sql(5000))
            canc = rehearse._demonstrate_cancellation(db)
            self.assertTrue(canc["interrupt_left_invalid_index"])
            self.assertEqual(canc["rollback"]["index_remains_after_drop"], 0)
            self.assertEqual(canc["indisvalid_after_rebuild"], "t")
        finally:
            db.stop()
            db.destroy()


if __name__ == "__main__":
    unittest.main()
