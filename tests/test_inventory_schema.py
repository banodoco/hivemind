"""Tests for ``scripts/inventory_schema.py`` — the Hivemind schema inventory.

Pure and offline: no network, no CLI, no secrets are printed. These tests pin:

  * the inventory SQL is **strictly read-only** (every statement is a SELECT and
    no mutating keyword can start a statement) — the safety boundary for a tool
    that holds a real DB credential;
  * the redactor is applied to subprocess output (never leaks a credential);
  * the derived analysis (opt-out/delete column discovery, missing-table
    detection, SECURITY DEFINER detection, FTS-config extraction) is correct;
  * PROBE-line parsing tolerates psql noise/errors.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

import inventory_schema as inv  # noqa: E402
import verify_access as va  # noqa: E402  (shared redactor)


# ---------------------------------------------------------------------------
# Read-only safety — the hard boundary
# ---------------------------------------------------------------------------


class ReadOnlySqlTests(unittest.TestCase):
    """The inventory SQL must never mutate state, even if bypassed to psql."""

    def setUp(self) -> None:
        self.sql = inv.build_inventory_sql()
        # Split into statements the same way psql would on ';'.
        self.stmts = [s.strip() for s in self.sql.split(";") if s.strip()]

    def test_emits_at_least_one_statement(self):
        self.assertGreater(len(self.stmts), 5)

    def test_every_statement_starts_with_select(self):
        """No INSERT/UPDATE/DELETE/CREATE/ALTER/DROP/GRANT/REVOKE/TRUNCATE."""
        forbidden = {
            "insert", "update", "delete", "create", "alter", "drop",
            "truncate", "grant", "revoke", "merge", "vacuum", "call",
            "copy", "set", "with",
        }
        for stmt in self.stmts:
            first = stmt.split()[0].lower().strip("(")
            self.assertEqual(
                first, "select",
                f"non-SELECT statement in inventory SQL: {stmt[:80]!r}",
            )
            self.assertNotIn(first, forbidden)

    def test_no_mutation_keywords_as_statement_leaders(self):
        # Defensive double-check: even mid-statement, these verbs should only
        # ever appear inside string literals (we assert they are absent entirely
        # from the generated SQL to keep the rule trivially auditable).
        low = self.sql.lower()
        for word in ("insert into", "update ", "delete from", "create ",
                     "alter ", "drop ", "truncate ", "grant ", "revoke ",
                     "create extension", "set search_path"):
            self.assertNotIn(word, low, f"mutation keyword {word!r} present in inventory SQL")

    def test_only_reads_public_and_catalog(self):
        """All probes read public user objects or catalog views — nothing else."""
        # Catalog reads (chosen over information_schema so the limited CLI login
        # role still sees every column and ACL).
        low = self.sql.lower()
        self.assertIn("from pg_extension", low)
        self.assertIn("from pg_attribute", low)
        self.assertIn("from pg_indexes", low)
        self.assertIn("from pg_policies", low)
        self.assertIn("aclexplode", low)
        # Intentionally avoids reading FROM information_schema (privilege-filtered
        # for the login role) and never sets a search_path or config.
        self.assertNotIn("from information_schema", low)
        self.assertNotIn("set search_path", low)
        # User-table reads are limited to the eligibility distributions.
        self.assertIn("from distillations", low)
        self.assertIn("from external_resources", low)


# ---------------------------------------------------------------------------
# PROBE parsing — tolerates psql noise
# ---------------------------------------------------------------------------


class ParseProbesTests(unittest.TestCase):
    def test_parses_labeled_json_lines(self):
        out = (
            "PROBE::extensions::[{\"name\": \"pg_trgm\"}]\n"
            "psql:some_error: permission denied for relation\n"
            "PROBE::relations::[{\"name\": \"distillations\"}]\n"
            "PROBE::workflow_python_coverage::{\"workflows\": 3}\n"
            "\n"
        )
        parsed = inv.parse_probes(out)
        self.assertEqual(parsed["extensions"], [{"name": "pg_trgm"}])
        self.assertEqual(parsed["relations"], [{"name": "distillations"}])
        self.assertEqual(parsed["workflow_python_coverage"], {"workflows": 3})

    def test_null_value_preserved(self):
        parsed = inv.parse_probes("PROBE::triggers::null\n")
        self.assertIsNone(parsed["triggers"])

    def test_malformed_json_captured_not_dropped(self):
        parsed = inv.parse_probes("PROBE::views::not-json\n")
        self.assertEqual(parsed["views"], {"_parse_error": "not-json"})

    def test_non_probe_lines_ignored(self):
        parsed = inv.parse_probes("random line\nPROBE::extensions::[]\n")
        self.assertEqual(parsed, {"extensions": []})


# ---------------------------------------------------------------------------
# Derived analysis (pure functions)
# ---------------------------------------------------------------------------


class DerivedAnalysisTests(unittest.TestCase):
    def test_finds_opt_out_and_delete_columns(self):
        cols = [
            {"table": "message_feed", "column": "author_opted_out"},
            {"table": "message_feed", "column": "content"},
            {"table": "distillations", "column": "deleted_at"},
            {"table": "external_resources", "column": "title"},
        ]
        hits = inv.find_eligibility_columns(cols)
        pairs = {(h["table"], h["column"]) for h in hits}
        self.assertIn(("message_feed", "author_opted_out"), pairs)
        self.assertIn(("distillations", "deleted_at"), pairs)
        self.assertNotIn(("external_resources", "title"), pairs)

    def test_no_eligibility_columns_returns_empty(self):
        self.assertEqual(inv.find_eligibility_columns([{"table": "t", "column": "body"}]), [])
        self.assertEqual(inv.find_eligibility_columns(None), [])

    def test_missing_expected_tables_detected(self):
        rels = [{"name": "message_feed"}, {"name": "distillations"}]
        missing = inv.missing_expected_tables(rels)
        self.assertIn("external_resources", missing)
        self.assertIn("distillation_cites", missing)
        self.assertNotIn("message_feed", missing)

    def test_security_definer_detection(self):
        fns = [
            {"name": "check_duplicate_distillation", "security_definer": False},
            {"name": "hybrid_search", "security_definer": True},
        ]
        sd = inv.security_definer_functions(fns)
        self.assertEqual([f["name"] for f in sd], ["hybrid_search"])

    def test_fts_config_extracted_from_index_def(self):
        idx = [
            {"table": "message_feed", "name": "msg_fts", "def": "CREATE INDEX ... USING gin (to_tsvector('english'::regconfig, content))"},
            {"table": "external_resources", "name": "x_trgm", "def": "USING gin (body gin_trgm_ops)"},
        ]
        fts = inv.fts_config_from_indexes(idx)
        self.assertEqual(fts, [{"table": "message_feed", "index": "msg_fts", "config": "english"}])


# ---------------------------------------------------------------------------
# Redactor integration — output must never leak a credential
# ---------------------------------------------------------------------------


class RedactionTests(unittest.TestCase):
    def test_run_psql_output_is_redacted(self):
        """Whatever psql prints, run_psql must mask secrets before returning."""
        import unittest.mock as mock

        fake_proc = mock.Mock()
        fake_proc.stdout = (
            "PROBE::extensions::[]\n"
            "PGPASSWORD=gLunEFoVilWmLxhBiQMyjjYdNU6rkcqz\n"
            "postgresql://postgres.ujlwuvkrxlvoswwkerdf:hunter2@host:5432/db\n"
        )
        fake_proc.stderr = ""
        with mock.patch("subprocess.run", return_value=fake_proc):
            out = inv.run_psql("SELECT 1;", {"PGPASSWORD": "x"})
        self.assertNotIn("gLunEFoVilWmLxhBiQMyjjYdNU6rkcqz", out)
        self.assertNotIn("hunter2", out)
        # The PROBE line survives.
        self.assertIn("PROBE::extensions::[]", out)

    def test_get_session_env_error_is_redacted(self):
        import unittest.mock as mock

        fake_proc = mock.Mock()
        fake_proc.returncode = 1
        fake_proc.stdout = ""
        fake_proc.stderr = "boom PGPASSWORD=gLunEFoVilWmLxhBiQMyjjYdNU6rkcqz trailing"
        with mock.patch("shutil.which", return_value="/usr/local/bin/supabase"), \
             mock.patch("subprocess.run", return_value=fake_proc):
            with self.assertRaises(inv.ProbeError) as ctx:
                inv.get_session_env()
        self.assertNotIn("gLunEFoVilWmLxhBiQMyjjYdNU6rkcqz", str(ctx.exception))


class PublicCountTests(unittest.TestCase):
    """The eligibility count helper parses PostgREST Content-Range correctly."""

    def test_parses_content_range_total(self):
        import unittest.mock as mock

        fake_resp = mock.MagicMock()
        fake_resp.__enter__.return_value = fake_resp
        fake_resp.__exit__.return_value = False
        fake_resp.headers = {"Content-Range": "0-0/6987"}
        with mock.patch("urllib.request.urlopen", return_value=fake_resp):
            n = inv._postgrest_count("discord_messages", {"is_deleted": "eq.true"})
        self.assertEqual(n, 6987)

    def test_returns_none_when_no_content_range(self):
        import unittest.mock as mock

        fake_resp = mock.MagicMock()
        fake_resp.__enter__.return_value = fake_resp
        fake_resp.__exit__.return_value = False
        fake_resp.headers = {}
        with mock.patch("urllib.request.urlopen", return_value=fake_resp):
            self.assertIsNone(inv._postgrest_count("discord_messages", {}))

    def test_returns_none_on_network_failure(self):
        import unittest.mock as mock

        with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
            self.assertIsNone(inv._postgrest_count("discord_messages", {}))


if __name__ == "__main__":
    unittest.main()
