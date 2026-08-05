"""Red tests for Hivemind plan task 2.11 — embedding backfill run orchestration.

These tests pin the SQL API introduced by schema/030_embedding_backfill_runs.sql.
The module imports cleanly even before that migration exists; the run is expected
to be RED until the migration lands. Database behavior uses an isolated throwaway
PostgreSQL cluster via rehearse_embedding_lifecycle.setup_cluster().
"""

import json
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

SCHEMA = REPO / "schema"
MIGRATION = SCHEMA / "030_embedding_backfill_runs.sql"

RPC_NAMES = (
    "hivemind_create_embedding_backfill_run",
    "hivemind_checkpoint_embedding_backfill",
    "hivemind_pause_embedding_backfill_run",
    "hivemind_resume_embedding_backfill_run",
    "hivemind_complete_embedding_backfill_run",
    "hivemind_fail_embedding_backfill_run",
)


def _q(value):
    return "'" + str(value).replace("'", "''") + "'"


def _arr(values):
    return "ARRAY[" + ",".join(_q(v) for v in values) + "]::text[]"


class MigrationTextContract(unittest.TestCase):
    """Static contract over schema/030 source — no database required."""

    @classmethod
    def setUpClass(cls):
        if not MIGRATION.exists():
            raise FileNotFoundError(f"missing embedding backfill migration: {MIGRATION}")
        cls.sql = MIGRATION.read_text()
        cls.low = re.sub(r"\s+", " ", cls.sql.lower())

    def test_tables_are_additive_and_idempotent(self):
        for table in ("embedding_backfill_runs", "embedding_backfill_cursors"):
            self.assertRegex(
                self.low,
                rf"create\s+table\s+if\s+not\s+exists\s+(public\.)?{table}\b",
            )

    def test_run_table_columns(self):
        for needle in (
            "contract_id bigint",
            "version integer",
            "mode text",
            "status text",
            "snapshot text",
            "high_water text",
            "batch_size integer",
            "rate_limit_per_minute integer",
            "cost_cap_usd numeric",
            "total_eligible bigint",
            "total_processed bigint",
            "total_skipped bigint",
            "total_quarantined bigint",
            "total_unavailable bigint",
            "total_failed bigint",
            "created_at timestamptz",
            "updated_at timestamptz",
            "last_error text",
        ):
            self.assertIn(needle, self.low, f"runs: missing {needle}")

    def test_cursor_table_columns(self):
        for needle in (
            "run_id bigint",
            "source text",
            "cursor text",
            "high_water text",
            "eligible_count bigint",
            "processed_count bigint",
            "skipped_count bigint",
            "quarantined_count bigint",
            "unavailable_count bigint",
            "failed_count bigint",
            "last_error text",
            "primary key (run_id, source)",
        ):
            self.assertIn(needle, self.low, f"cursors: missing {needle}")

    def test_contract_foreign_key(self):
        self.assertRegex(self.low, r"contract_id\b[^,]*\breferences\b")
        self.assertRegex(self.low, r"references\s+(public\.)?\w*contract\w*")

    def test_all_six_rpcs_defined(self):
        for name in RPC_NAMES:
            self.assertRegex(
                self.low,
                rf"function\s+(public\.)?{name}\s*\(",
                f"missing function definition: {name}",
            )

    def test_apply_and_rollback_markers(self):
        self.assertIn("apply", self.low)
        self.assertIn("rollback", self.low)

    def test_service_role_grant_and_explicit_revokes(self):
        self.assertRegex(self.low, r"grant[^;]*to[^;]*service_role")
        self.assertRegex(self.low, r"revoke[^;]*from[^;]*anon")
        self.assertRegex(self.low, r"revoke[^;]*from[^;]*authenticated")

    def test_last_error_is_bounded(self):
        self.assertTrue(
            re.search(r"length\s*\(\s*last_error", self.low)
            or re.search(r"(left|substring|substr)\s*\([^)]*last_error", self.low),
            "last_error has no visible bound",
        )


class BackfillDBTests(unittest.TestCase):
    """Database behavior on an isolated cluster. Skips only when local
    PostgreSQL/pgvector is genuinely unavailable — never because schema/030 is
    missing."""

    cluster = None
    meta = None
    source_snapshot = None

    @classmethod
    def setUpClass(cls):
        try:
            from rehearse_embedding_lifecycle import setup_cluster
        except Exception as exc:  # pragma: no cover - harness not importable
            raise unittest.SkipTest(f"lifecycle harness unavailable: {exc}")
        try:
            cls.cluster, cls.meta = setup_cluster()
        except unittest.SkipTest:
            raise
        except Exception as exc:
            msg = str(exc).lower()
            if any(k in msg for k in ("postgres", "pgvector", "initdb", "pg_config", "cluster", "server")):
                raise unittest.SkipTest(f"postgres/pgvector unavailable: {exc}")
            raise
        # Snapshot corpus tables BEFORE applying 030 to prove the migration is
        # read-only with respect to source data. tear_down always runs.
        try:
            cls.source_snapshot = cls._snapshot_sources()
            cls.cluster.psql_file(MIGRATION)
        except Exception:
            cls.cluster.tear_down()
            raise

    @classmethod
    def tearDownClass(cls):
        if cls.cluster is not None:
            cls.cluster.tear_down()

    # -- helpers ---------------------------------------------------------

    def _copy(self, sql):
        rc, out = self.cluster.psql("COPY (" + sql + ") TO STDOUT;")
        self.assertEqual(rc, 0, out)
        return out

    def _one(self, sql):
        out = self._copy(sql).strip()
        self.assertNotEqual(out, "", f"empty result for: {sql}")
        return out

    def _json(self, sql):
        return json.loads(self._one(sql))

    def _rpc(self, call):
        return json.loads(self._one("SELECT (" + call + ")::text"))

    def _count(self, table, where="true"):
        return int(self._one(f"SELECT count(*)::text FROM {table} WHERE {where}"))

    def _role_exists(self, role):
        return self._one(
            f"SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname={_q(role)})::text"
        ) == "t"

    def _run(self, run_id):
        return self._json(
            f"SELECT to_jsonb(r)::text FROM embedding_backfill_runs r WHERE run_id={run_id}"
        )

    def _cursor(self, run_id, source):
        return self._json(
            f"SELECT to_jsonb(c)::text FROM embedding_backfill_cursors c "
            f"WHERE run_id={run_id} AND source={_q(source)}"
        )

    def _create_run(self, sources=("messages", "resources"), mode="rebuild"):
        cid = self.meta["active_contract_id"]
        call = (
            f"SELECT hivemind_create_embedding_backfill_run("
            f"{cid}, {_q(mode)}, {_arr(sources)})"
        )
        return int(self._one(call))

    def _checkpoint(self, run_id, source, expected_version, cursor, **kw):
        order = ("high_water", "processed", "skipped", "quarantined",
                 "unavailable", "failed", "eligible", "last_error")
        vals = []
        for key in order:
            v = kw.get(key)
            if v is None:
                vals.append("NULL")
            elif isinstance(v, str):
                vals.append(_q(v))
            else:
                vals.append(str(v))
        call = (
            "SELECT hivemind_checkpoint_embedding_backfill("
            + ",".join(
                [str(run_id), _q(source), str(expected_version), _q(cursor)] + vals
            )
            + ")"
        )
        return self._rpc(call)

    @classmethod
    def _snapshot_sources(cls):
        rc, out = cls.cluster.psql(
            "COPY (SELECT relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind='r' AND n.nspname='public' ORDER BY relname) TO STDOUT;"
        )
        assert rc == 0, out
        snap = {}
        for tbl in [t.strip() for t in out.splitlines() if t.strip()]:
            rc, h = cls.cluster.psql(
                f"COPY (SELECT coalesce(md5(string_agg(md5(t::text), ',' ORDER BY t::text)), md5('')) "
                f"FROM {tbl} t) TO STDOUT;"
            )
            assert rc == 0, h
            rc2, c = cls.cluster.psql(
                f"COPY (SELECT count(*)::text FROM {tbl}) TO STDOUT;"
            )
            assert rc2 == 0, c
            snap[tbl] = (int(c.strip()), h.strip())
        return snap

    def _hash_table(self, tbl):
        rc, h = self.cluster.psql(
            f"COPY (SELECT coalesce(md5(string_agg(md5(t::text), ',' ORDER BY t::text)), md5('')) "
            f"FROM {tbl} t) TO STDOUT;"
        )
        self.assertEqual(rc, 0, h)
        rc2, c = self.cluster.psql(f"COPY (SELECT count(*)::text FROM {tbl}) TO STDOUT;")
        self.assertEqual(rc2, 0, c)
        return (int(c.strip()), h.strip())

    # -- migration -------------------------------------------------------

    def test_migration_applies_twice(self):
        # setUpClass applied once; a second apply must be a no-op (idempotent).
        self.cluster.psql_file(MIGRATION)

    # -- create ----------------------------------------------------------

    def test_create_persists_run_and_per_source_cursors(self):
        run_id = self._create_run(sources=("messages", "resources"))
        self.assertGreater(run_id, 0)
        self.assertEqual(self._count("embedding_backfill_runs", f"run_id={run_id}"), 1)
        self.assertEqual(self._count("embedding_backfill_cursors", f"run_id={run_id}"), 2)
        run = self._run(run_id)
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["version"], 1)
        for src in ("messages", "resources"):
            cur = self._cursor(run_id, src)
            self.assertEqual(cur["processed_count"], 0)
            self.assertIsNone(cur["cursor"])

    # -- checkpoint ------------------------------------------------------

    def test_checkpoint_advances_cursor_and_counters(self):
        run_id = self._create_run(sources=("messages",))
        res = self._checkpoint(
            run_id, "messages", 1, "cur-1",
            processed=5, skipped=2, failed=1, eligible=10,
        )
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["version"], 2)
        cur = self._cursor(run_id, "messages")
        self.assertEqual(cur["cursor"], "cur-1")
        self.assertEqual(cur["processed_count"], 5)
        self.assertEqual(cur["skipped_count"], 2)
        self.assertEqual(cur["failed_count"], 1)
        run = self._run(run_id)
        self.assertEqual(run["version"], 2)
        self.assertEqual(run["total_processed"], 5)
        self.assertEqual(run["total_skipped"], 2)
        self.assertEqual(run["total_failed"], 1)

    def test_subsequent_checkpoint_accumulates_at_cursor(self):
        run_id = self._create_run(sources=("messages",))
        self._checkpoint(run_id, "messages", 1, "cur-1", processed=5, eligible=10)
        res = self._checkpoint(run_id, "messages", 2, "cur-2", processed=3, eligible=2)
        self.assertTrue(res["ok"], res)
        cur = self._cursor(run_id, "messages")
        self.assertEqual(cur["cursor"], "cur-2")
        self.assertEqual(cur["processed_count"], 8)
        self.assertEqual(cur["eligible_count"], 12)
        self.assertEqual(self._run(run_id)["total_processed"], 8)

    def test_stale_expected_version_loses_cas(self):
        run_id = self._create_run(sources=("messages",))
        self._checkpoint(run_id, "messages", 1, "cur-1", processed=5)
        res = self._checkpoint(run_id, "messages", 1, "cur-stale", processed=99)
        self.assertFalse(res["ok"], res)
        cur = self._cursor(run_id, "messages")
        self.assertEqual(cur["cursor"], "cur-1")
        self.assertEqual(cur["processed_count"], 5)
        self.assertEqual(self._run(run_id)["version"], 2)

    # -- pause / resume --------------------------------------------------

    def test_pause_resume_is_legal(self):
        run_id = self._create_run(sources=("messages",))
        paused = self._rpc(f"SELECT hivemind_pause_embedding_backfill_run({run_id}, 1)")
        self.assertTrue(paused["ok"], paused)
        self.assertEqual(self._run(run_id)["status"], "paused")
        resumed = self._rpc(
            f"SELECT hivemind_resume_embedding_backfill_run({run_id}, {paused['version']})"
        )
        self.assertTrue(resumed["ok"], resumed)
        self.assertEqual(self._run(run_id)["status"], "running")

    # -- terminal --------------------------------------------------------

    def test_completed_run_is_terminal(self):
        run_id = self._create_run(sources=("messages",))
        done = self._rpc(f"SELECT hivemind_complete_embedding_backfill_run({run_id}, 1)")
        self.assertTrue(done["ok"], done)
        self.assertEqual(self._run(run_id)["status"], "completed")
        # Correct CAS version, but terminal status blocks further progress.
        after = self._checkpoint(run_id, "messages", done["version"], "after", processed=1)
        self.assertFalse(after["ok"], after)
        self.assertEqual(self._cursor(run_id, "messages")["processed_count"], 0)

    def test_failed_run_is_terminal(self):
        run_id = self._create_run(sources=("messages",))
        err = self._rpc(
            f"SELECT hivemind_fail_embedding_backfill_run({run_id}, 1, {_q('boom')})"
        )
        self.assertTrue(err["ok"], err)
        run = self._run(run_id)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["last_error"], "boom")
        after = self._checkpoint(run_id, "messages", err["version"], "after", processed=1)
        self.assertFalse(after["ok"], after)

    # -- data fidelity ---------------------------------------------------

    def test_snowflake_cursor_round_trips_as_text(self):
        run_id = self._create_run(sources=("messages",))
        snowflake = "9223372036854775000"
        self._checkpoint(run_id, "messages", 1, snowflake)
        self.assertEqual(self._cursor(run_id, "messages")["cursor"], snowflake)

    def test_raw_secret_error_is_not_stored(self):
        run_id = self._create_run(sources=("messages",))
        secret = "Authorization: Bearer sk-secret-DO-NOT-STORE"
        self._checkpoint(run_id, "messages", 1, "cur-1", failed=1, last_error=secret)
        stored = self._cursor(run_id, "messages")["last_error"]
        self.assertIsNotNone(stored)
        self.assertNotIn("sk-secret", stored)
        self.assertNotEqual(stored, secret)
        self.assertLessEqual(len(stored), 500)

    def test_source_tables_unchanged(self):
        for tbl, (count, digest) in self.source_snapshot.items():
            self.assertEqual(
                self._hash_table(tbl), (count, digest),
                f"source table {tbl} mutated by backfill run",
            )

    # -- privileges ------------------------------------------------------

    def test_anon_and_authenticated_have_no_privileges(self):
        for tbl in ("embedding_backfill_runs", "embedding_backfill_cursors"):
            for role in ("anon", "authenticated"):
                if not self._role_exists(role):
                    continue
                for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    got = self._one(
                        f"SELECT coalesce(has_table_privilege('{role}', {_q(tbl)}, {_q(priv)}), false)::text"
                    )
                    self.assertEqual(got, "f", f"{role} has {priv} on {tbl}")
        leaked = self._count(
            "information_schema.routine_privileges",
            "routine_name IN (" + ",".join(_q(n) for n in RPC_NAMES) + ") "
            "AND grantee IN ('anon','authenticated')",
        )
        self.assertEqual(leaked, 0)
        # service_role retains access.
        if self._role_exists("service_role"):
            self.assertEqual(
                self._one(
                    "SELECT coalesce(has_table_privilege('service_role', "
                    "'embedding_backfill_runs', 'SELECT'), false)::text"
                ),
                "t",
            )


if __name__ == "__main__":
    unittest.main()
