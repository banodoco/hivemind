"""Tests for the full-message exact-identifier path (hybrid-search task 1.6).

Offline tests (always run): the frozen CHOICE + chosen index expression + partial
predicate, the candidate-query contract shape, arm gating, schema/007 identity
consistency, build/rollback SQL shape, preflight verdict logic, the EXPLAIN
parser, and the rejected-alternative extraction (required families + frozen
fixture corpus self-consistency).

PG-gated tests (run when a local PostgreSQL 14 is available + HIVEMIND_EVAL_CLUSTER=1):
schema/007 loads on an isolated throwaway cluster, the chosen index builds valid,
a candidate query USES it (Bitmap Index Scan) and excludes soft-deleted rows, and
the rejected-alternative SQL extraction agrees byte-for-byte with the Python
reference on every fixture (the decision-record parity proof).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "scripts"), str(REPO_ROOT / "executors")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import scripts.message_identifier_index as M  # noqa: E402
from executors import message_identifier_index as REF  # noqa: E402

FIXTURES = REPO_ROOT / "eval" / "retrieval" / "fixtures" / "message-identifier-v1.json"
SCHEMA = (REPO_ROOT / "schema" / "007_message_identifier_index.sql").read_text()

PG_AVAILABLE = all(shutil.which(b) for b in ("initdb", "pg_ctl", "psql"))
CLUSTER_OK = PG_AVAILABLE and os.environ.get("HIVEMIND_EVAL_CLUSTER") == "1"


class ChosenDesignContract(unittest.TestCase):
    def test_choice_is_full_message_trigram(self):
        self.assertEqual(REF.CHOICE, "normalized_full_message_trigram_length_bounded")

    def test_version_is_at_least_v3(self):
        # v3 = corrected exact/variant bridge (normalized containment, not equality)
        self.assertGreaterEqual(REF.MESSAGE_IDENTIFIER_INDEX_VERSION, 3)

    def test_index_expression_frozen(self):
        self.assertEqual(REF.INDEX_EXPRESSION, "hivemind_normalize_identifier(content)")
        self.assertEqual(REF.INDEX_OPCLASS, "gin_trgm_ops")

    def test_partial_predicate_has_eligibility_and_length_bound(self):
        self.assertIn("is_deleted = false", REF.PARTIAL_PREDICATE)
        self.assertIn("char_length(content)", REF.PARTIAL_PREDICATE)
        self.assertIn("BETWEEN", REF.PARTIAL_PREDICATE)
        self.assertGreater(REF.CONTENT_LENGTH_MAX, 2047)  # captures long-token bodies

    def test_storage_gate_constant(self):
        self.assertEqual(REF.STORAGE_GATE_GB, 12.0)


class CandidateQueryContract(unittest.TestCase):
    def test_candidate_query_shape(self):
        cq = M.candidate_query_sql(requested_limit=20)
        self.assertIn("hivemind_normalize_identifier(m.content)", cq)
        # PRIMARY path is index-supported exact normalized CONTAINMENT (LIKE), NOT
        # the permissive <% fuzzy path (demoted to an optional bounded fallback).
        self.assertIn("LIKE '%' || q.k || '%'", cq)
        self.assertNotIn("<%", cq)
        self.assertIn("is_deleted = false", cq)
        self.assertIn(f"char_length(m.content) BETWEEN {REF.CONTENT_LENGTH_MIN} AND {REF.CONTENT_LENGTH_MAX}", cq)
        self.assertIn("message_id::text", cq)
        self.assertIn("LIMIT", cq)
        # repeat the partial predicate (index-match requirement)
        self.assertEqual(cq.count("is_deleted = false"), 1)

    def test_candidate_limit(self):
        self.assertEqual(M.candidate_limit(20), 100)
        self.assertEqual(M.candidate_limit(1), REF.CANDIDATE_MULTIPLIER)
        self.assertLessEqual(M.candidate_limit(10000), REF.CANDIDATE_LIMIT_CAP)
        self.assertEqual(M.candidate_limit(0), 0)

    def test_arm_should_fire_gating(self):
        self.assertTrue(M.arm_should_fire("FLUX.1"))
        self.assertTrue(M.arm_should_fire("wan2.2"))
        self.assertFalse(M.arm_should_fire(""))
        self.assertFalse(M.arm_should_fire("   "))
        self.assertFalse(M.arm_should_fire(None))
        self.assertFalse(M.arm_should_fire("x" * (REF.MAX_QUERY_CHARS + 1)))

    def test_normalize_query_key(self):
        self.assertEqual(M.normalize_query_key("FLUX.1"), "flux1")
        self.assertEqual(M.normalize_query_key("Wan2.2"), "wan22")
        self.assertEqual(M.normalize_query_key(""), "")
        self.assertEqual(M.normalize_query_key(None), "")

    def test_tie_break_is_snowflake_safe(self):
        self.assertIn("message_id::text", REF.TIE_BREAK)


class SchemaIdentityConsistency(unittest.TestCase):
    def test_schema_has_chosen_index(self):
        self.assertIn(REF.INDEX_NAME, SCHEMA)
        self.assertIn(REF.INDEX_EXPRESSION, SCHEMA)
        self.assertIn("gin_trgm_ops", SCHEMA)
        self.assertIn("is_deleted = false", SCHEMA)
        self.assertIn("char_length(content)", SCHEMA)

    def test_schema_is_index_only_no_side_table(self):
        # the pivot removed the rejected side index
        self.assertNotIn("message_identifiers", SCHEMA)
        self.assertNotIn("hivemind_extract_message_identifiers", SCHEMA)
        self.assertNotIn("create trigger", SCHEMA.lower())

    def test_schema_has_guard_and_rollback(self):
        self.assertIn("prerequisite guard", SCHEMA)
        self.assertIn("DROP INDEX CONCURRENTLY", SCHEMA)
        self.assertIn("CREATE INDEX CONCURRENTLY", SCHEMA)


class BuildRollbackSql(unittest.TestCase):
    def test_build_statement_shape(self):
        b = M.build_statement(lock_timeout_s=30, statement_timeout_s=3600)
        self.assertIn("CREATE INDEX CONCURRENTLY IF NOT EXISTS", b)
        self.assertIn(REF.INDEX_NAME, b)
        self.assertIn(REF.INDEX_EXPRESSION, b)
        self.assertIn("lock_timeout", b)

    def test_rollback_statement_shape(self):
        r = M.rollback_statement()
        self.assertIn("DROP INDEX", r)
        self.assertIn(REF.INDEX_NAME, r)


class PreflightLogic(unittest.TestCase):
    def _pf(self, parsed, host="session.supabase.com", port="5432"):
        return M.evaluate_preflight(parsed, pghost=host, pgport=port)

    def test_green_on_clean_state(self):
        parsed = {
            "source_table_shape": [["content", "text"], ["is_deleted", "boolean"], ["message_id", "bigint"]],
            "prereq_005_present": [["t"]],
            "target_index_state": [],
            "est_rows": [["1250000"]],
            "n_eligible": [["1240000"]],
            "db_size_bytes": [["2300000000"]],
            "invalid_indexes": [],
            "long_txns": [["0"]],
            "relation_locks": [["0"]],
        }
        v = self._pf(parsed)
        self.assertTrue(v["green"], msg=v["reasons"])
        self.assertTrue(v["checks_by_name"] if False else all(c["pass"] for c in v["checks"]))

    def test_red_on_wrong_content_type(self):
        parsed = {"source_table_shape": [["content", "bytea"]], "prereq_005_present": [["t"]],
                  "target_index_state": [], "est_rows": [["100"]], "n_eligible": [["100"]],
                  "db_size_bytes": [["1000"]], "invalid_indexes": [], "long_txns": [["0"]],
                  "relation_locks": [["0"]]}
        self.assertFalse(self._pf(parsed)["green"])

    def test_red_on_missing_005(self):
        parsed = {"source_table_shape": [["content", "text"], ["is_deleted", "boolean"]],
                  "prereq_005_present": [["f"]], "target_index_state": [], "est_rows": [["100"]],
                  "n_eligible": [["100"]], "db_size_bytes": [["1000"]], "invalid_indexes": [],
                  "long_txns": [["0"]], "relation_locks": [["0"]]}
        self.assertFalse(self._pf(parsed)["green"])

    def test_red_on_pooler_txn_mode(self):
        parsed = {"source_table_shape": [["content", "text"], ["is_deleted", "boolean"]],
                  "prereq_005_present": [["t"]], "target_index_state": [], "est_rows": [["100"]],
                  "n_eligible": [["100"]], "db_size_bytes": [["1000"]], "invalid_indexes": [],
                  "long_txns": [["0"]], "relation_locks": [["0"]]}
        self.assertFalse(self._pf(parsed, port="6543")["green"])

    def test_red_on_invalid_remnant(self):
        parsed = {"source_table_shape": [["content", "text"], ["is_deleted", "boolean"]],
                  "prereq_005_present": [["t"]], "target_index_state": [["f", "t", "1000"]],
                  "est_rows": [["100"]], "n_eligible": [["100"]], "db_size_bytes": [["1000"]],
                  "invalid_indexes": [["idx_discord_messages_identifier_trgm"]],
                  "long_txns": [["0"]], "relation_locks": [["0"]]}
        self.assertFalse(self._pf(parsed)["green"])

    def test_storage_gate_estimation(self):
        parsed = {"source_table_shape": [["content", "text"], ["is_deleted", "boolean"]],
                  "prereq_005_present": [["t"]], "target_index_state": [], "est_rows": [["1250000"]],
                  "n_eligible": [["1240000"]], "db_size_bytes": [["1000000000"]],
                  "invalid_indexes": [], "long_txns": [["0"]], "relation_locks": [["0"]]}
        v = self._pf(parsed)
        self.assertLess(v["est_index_bytes"], REF.STORAGE_GATE_GB * 1e9)


class PreflightQueryShape(unittest.TestCase):
    """Regression for the defect-1 preflight SQL error.

    The canonical ``target_index_state`` preflight query must call
    ``pg_relation_size`` with a regclass/oid argument (``c.oid``), NOT a
    ``name``-typed ``c.relname``: ``pg_relation_size`` has NO ``name`` overload,
    so the old form errored on PG14 ("No function matches the given name and
    argument types"), returned empty rows, and the verdict falsely reported the
    index absent. This shape check runs offline and would have caught it.
    """

    def test_target_index_state_uses_oid_not_name(self):
        q = dict(M.preflight_queries())["target_index_state"]
        self.assertIn("pg_relation_size(c.oid)", q)
        self.assertNotIn("pg_relation_size(c.relname)", q)
        self.assertNotIn("pg_relation_size(c.relname::", q)

    def test_target_index_state_scoped_to_public_namespace(self):
        # match the fully-qualified index identity, not any same-named relation
        q = dict(M.preflight_queries())["target_index_state"]
        self.assertIn("nspname", q)
        self.assertIn(REF.INDEX_NAME, q)


class ExplainParser(unittest.TestCase):
    def test_detects_bitmap_index_scan(self):
        plan = "Bitmap Index Scan on idx_discord_messages_identifier_trgm (...)"
        p = M.parse_explain_plan(plan)
        self.assertTrue(p["uses_index_scan"])
        self.assertTrue(p["uses_identifier_index"])
        self.assertFalse(p["is_seq_scan"])

    def test_detects_seq_scan(self):
        plan = "Seq Scan on discord_messages (...)"
        p = M.parse_explain_plan(plan)
        self.assertTrue(p["is_seq_scan"])
        self.assertFalse(p["uses_index_scan"])


class RejectedAlternativeExtraction(unittest.TestCase):
    """The rejected side-index grammar, kept as the decision-record reference."""

    def test_required_families_one_compact_key(self):
        cases = {
            "FLUX.1": "flux1", "Wan2.2": "wan22", "wan_2.2": "wan22",
            "LTX-Video": "ltxvideo", "ltx-2-19b-ic-lora-detailer": "ltx219bicloradetailer",
            "lightx2v_I2V_14B.safetensors": "lightx2vi2v14bsafetensors",
            "WanVideoSampler": "wanvideosampler",
            "force_clip_output=False": "forceclipoutput=false",
        }
        for raw, want in cases.items():
            self.assertIn(want, REF.extract_message_identifiers(raw), msg=raw)

    def test_pure_digits_and_short_runs_dropped(self):
        out = REF.extract_message_identifiers("1024 512 2.2 7 v a == ...")
        self.assertNotIn("1024", out)  # pure digit
        self.assertNotIn("512", out)
        self.assertNotIn("22", out)    # 2.2 -> 22, len 2
        # 'v1' len2 dropped; but 'v155' (from v1.5.5) kept elsewhere
        for k in out:
            self.assertGreaterEqual(len(k), REF.MIN_TERM_CHARS)

    def test_url_components_extracted(self):
        out = REF.extract_message_identifiers("https://huggingface.co/u/flux.1-dev.safetensors")
        self.assertIn("flux1devsafetensors", out)
        self.assertIn("huggingfaceco", out)

    def test_adversarial_sql_inert(self):
        out = REF.extract_message_identifiers("DROP TABLE unified_feed; -- inert")
        # nothing executed; only ASCII identifier runs extracted
        self.assertIn("drop", out)
        self.assertIn("unifiedfeed", out)

    def test_fixture_corpus_self_consistent(self):
        fx = json.loads(FIXTURES.read_text())
        for f in fx["fixtures"]:
            got = sorted(REF.extract_message_identifiers(f["content"]).keys())
            self.assertEqual(got, sorted(f["expected_compacts"]), msg=f["name"])

    def test_term_cap_respected(self):
        # a pathological body of many distinct identifiers is capped
        body = " ".join(f"id{i}x" for i in range(2000))
        out = REF.extract_message_identifiers(body)
        self.assertLessEqual(len(out), REF.MAX_TERMS_PER_MESSAGE)


# ---------------------------------------------------------------------------
# PG-gated integration (isolated throwaway cluster; only with HIVEMIND_EVAL_CLUSTER=1)
# ---------------------------------------------------------------------------

@unittest.skipUnless(CLUSTER_OK, "needs local PG14 + HIVEMIND_EVAL_CLUSTER=1")
class ClusterIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="mi_test_cluster_"))
        cls.datadir = cls.root / "data"
        cls.env = {**os.environ, "PGHOST": str(cls.root), "PGPORT": "55471",
                   "PGUSER": "postgres", "PGDATABASE": "postgres"}
        subprocess.run(["initdb", "-D", str(cls.datadir), "-U", "postgres", "-A", "trust",
                        "--no-locale", "-E", "UTF8"], env=cls.env, capture_output=True,
                       text=True, timeout=120, check=True)
        opts = f"-c listen_addresses='' -c unix_socket_directories='{cls.root}' -p 55471"
        subprocess.run(["pg_ctl", "-D", str(cls.datadir), "-l", str(cls.root / "pg.log"),
                        "-o", opts, "-w", "start"], env=cls.env, capture_output=True,
                       text=True, timeout=120, check=True)
        cls._sql(M.prereq_schema_sql_text())
        cls._sql("create extension if not exists pg_trgm;")
        cls._sql(f"create table {M.SOURCE_TABLE} (message_id bigint primary key, content text, "
                 f"is_deleted boolean not null default false, created_at timestamptz not null default now(), "
                 f"channel_id bigint, author_id bigint, guild_id bigint);")
        cls._sql(f"insert into {M.SOURCE_TABLE}(message_id, content, is_deleted) values "
                 f"(1,'FLUX.1 dev lora',false),(2,'wan2.2 WanVideoSampler',false),(3,'controlnet',true);")

    @classmethod
    def _sql(cls, stmt, timeout=180):
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as tf:
            tf.write(stmt); s = tf.name
        try:
            return subprocess.run(["psql", "-X", "-q", "-t", "-A", "-P", "pager=off",
                                   "-v", "ON_ERROR_STOP=1", "-f", s], env=cls.env,
                                  capture_output=True, text=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL)
        finally:
            os.unlink(s)

    @classmethod
    def _sqlt(cls, stmt, timeout=180):
        r = cls._sql(stmt, timeout=timeout)
        if r.returncode != 0:
            raise AssertionError(r.stderr or r.stdout)
        return r.stdout

    @classmethod
    def tearDownClass(cls):
        if (cls.datadir / "postmaster.pid").exists():
            subprocess.run(["pg_ctl", "-D", str(cls.datadir), "-m", "fast", "-w", "stop"],
                           env=cls.env, capture_output=True, text=True, timeout=60)
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_schema_loads_and_index_valid(self):
        self.assertEqual(self._sql(M.schema_sql_text()).returncode, 0)
        valid = self._sqlt(f"SELECT indisvalid FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                           f"WHERE c.relname='{REF.INDEX_NAME}';").strip().splitlines()[-1]
        self.assertIn(valid, ("t", "true"))

    def test_candidate_query_uses_index_and_excludes_deleted(self):
        self._sql(M.schema_sql_text())  # idempotent
        # enable_seqscan=off forces the index (a 3-row fixture table seq-scans
        # naturally; the production-scale index-use proof is the 1.25M rehearsal).
        # The PRIMARY containment predicate (LIKE) is structurally index-served
        # (the production-scale natural index choice is proven in the rehearsal).
        plan = self._sqlt(
            f"SET enable_seqscan = off;\n"
            f"EXPLAIN WITH q AS (SELECT public.hivemind_normalize_identifier('FLUX.1') AS k) "
            f"SELECT m.message_id::text FROM {M.SOURCE_TABLE} m, q "
            f"WHERE m.is_deleted=false AND char_length(m.content) BETWEEN {REF.CONTENT_LENGTH_MIN} AND {REF.CONTENT_LENGTH_MAX} "
            f"AND public.hivemind_normalize_identifier(m.content) LIKE '%' || q.k || '%' LIMIT 10;")
        self.assertIn("bitmap index scan", plan.lower())
        self.assertIn(REF.INDEX_NAME, plan.lower())

    def test_preflight_target_index_state_detects_valid_index(self):
        """Regression (cluster) for defect 1: the canonical target_index_state
        preflight query must NOT error ('No function matches the given name and
        argument types...') and must detect the already-built VALID index, so
        evaluate_preflight reports already_valid=True rather than 'absent'.
        Mirrors the live driver's preflight readback path."""
        self._sql(M.schema_sql_text())  # builds the chosen index valid
        q = dict(M.preflight_queries())["target_index_state"]
        rows = [ln.split("|") for ln in self._sqlt(q).splitlines() if ln.strip()]
        self.assertTrue(rows, "target_index_state returned no rows (query errored?)")
        self.assertIn(rows[0][0], ("t", "true"))
        self.assertIn(rows[0][1], ("t", "true"))
        parsed = {
            "source_table_shape": [["content", "text"], ["is_deleted", "boolean"], ["message_id", "bigint"]],
            "prereq_005_present": [["t"]], "target_index_state": rows,
            "est_rows": [["100"]], "n_eligible": [["100"]], "db_size_bytes": [["1000"]],
            "invalid_indexes": [], "long_txns": [["0"]], "relation_locks": [["0"]],
        }
        verdict = M.evaluate_preflight(parsed)
        self.assertTrue(verdict["already_valid"])
        ti_check = next(c for c in verdict["checks"]
                        if c["name"] == "target_index_absent_or_valid")
        self.assertTrue(ti_check["pass"])
        self.assertNotIn("absent", ti_check["detail"])

    def test_rejected_alt_sql_python_parity(self):
        fx = json.loads(FIXTURES.read_text())
        for f in fx["fixtures"]:
            content = f["content"]
            py = set(REF.extract_message_identifiers(content).keys())
            out = self._sqlt(
                "SELECT distinct on (norm.compact) norm.compact FROM ("
                " SELECT public.hivemind_normalize_identifier((rm)[1]) AS compact, rn"
                "   FROM regexp_matches($pc$" + content + "$pc$, '[A-Za-z0-9_.=-]+', 'g') WITH ORDINALITY AS r(rm, rn)"
                ") norm WHERE char_length(norm.compact) BETWEEN 3 AND 100 AND norm.compact ~ '[A-Za-z]'"
                " ORDER BY norm.compact, norm.rn LIMIT 256;")
            sq = set(l.strip() for l in out.splitlines() if l.strip())
            self.assertEqual(py, sq, msg=f["name"])

    def test_rollback_drops_index(self):
        self._sql(M.schema_sql_text())
        self.assertEqual(self._sql(M.rollback_statement(concurrently=False)).returncode, 0)
        n = self._sqlt("SELECT count(*) FROM pg_class WHERE relname='" + REF.INDEX_NAME + "';").strip().splitlines()[-1]
        self.assertEqual(n, "0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
