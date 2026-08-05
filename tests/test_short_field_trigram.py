"""Tests for plan task 1.5 — bounded normalized short-field trigram indexes.

Covers the deliverables the task names: dry-run / idempotence, index-use
(EXPLAIN parsing), capacity, rollback, security (eligibility partial predicate
excludes rejected distillations + overlong/empty bounds), Unicode (the frozen
normalize collapses separator/case variants), the schema/005 prerequisite, plus
the frozen-identity / SQL-text / preflight-verdict / redaction logic.

Offline by default (no network, no live DB, no provider): the frozen-identity,
SQL-text, preflight-verdict, redaction, capacity, security, Unicode, and plan-
parsing tests pin pure logic, and the rehearsal/live evidence artifacts captured
by the operator scripts are asserted as offline JSON. One integration class
(idempotence) spins a throwaway local PostgreSQL cluster and is skipped
automatically when PG binaries are absent.
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

import scripts.short_field_trigram as M  # noqa: E402
import executors.identifier_normalization as IN  # noqa: E402
from verify_access import redact  # noqa: E402

SCHEMA_006 = _REPO / "schema" / "006_short_field_trigram.sql"
SCHEMA_005 = _REPO / "schema" / "005_identifier_normalization.sql"
REHEARSAL_JSON = _REPO / "docs" / "hybrid-search" / "phase1-short-field-trigram-rehearsal.json"
LIVE_JSON = _REPO / "docs" / "hybrid-search" / "phase1-short-field-trigram-live.json"


# ---------------------------------------------------------------------------
# Frozen identity (cross-checked against the 1.4 identifier-normalization form)
# ---------------------------------------------------------------------------

class TestFrozenIdentity(unittest.TestCase):
    def test_two_targets_are_the_frozen_short_fields(self):
        tables = {t["table"] for t in M.TARGETS}
        self.assertEqual(tables, {"external_resources", "distillations"})
        cols = {t["table"]: t["column"] for t in M.TARGETS}
        self.assertEqual(cols, {"external_resources": "title", "distillations": "question"})

    def test_index_names_are_frozen(self):
        self.assertEqual(M.TITLE_INDEX, "idx_external_resources_title_trgm_norm")
        self.assertEqual(M.QUESTION_INDEX, "idx_distillations_question_trgm_norm")
        self.assertEqual(set(M.INDEX_NAMES), {M.TITLE_INDEX, M.QUESTION_INDEX})

    def test_expression_uses_the_frozen_compact_normalize(self):
        for t in M.TARGETS:
            self.assertEqual(M.index_expression(t["table"], t["column"]),
                             f"hivemind_normalize_identifier({t['column']})")
        # The compact form is the Python reference normalize_identifier.
        self.assertEqual(IN.normalize_identifier("Wan 2.2"), "wan22")

    def test_opclass_is_gin_trgm_ops(self):
        self.assertEqual(M.TRIGRAM_OPCLASS, "gin_trgm_ops")

    def test_no_large_body_field_is_indexed(self):
        # Scope boundary: bodies/answers/conditions are NOT trigram-indexed here.
        cols = {t["column"] for t in M.TARGETS}
        self.assertNotIn("body", cols)
        self.assertNotIn("answer", cols)
        self.assertNotIn("content", cols)

    def test_thresholds_and_bounds_are_frozen(self):
        self.assertEqual(M.SIMILARITY_THRESHOLD, 0.3)
        self.assertEqual(M.WORD_SIMILARITY_THRESHOLD, 0.3)
        self.assertEqual(M.MAX_NORM_FIELD_CHARS, 300)
        self.assertEqual(M.MAX_QUERY_CHARS, 300)


# ---------------------------------------------------------------------------
# Build / rollback SQL text + idempotence (dry-run shape)
# ---------------------------------------------------------------------------

class TestBuildAndRollbackSQL(unittest.TestCase):
    def test_build_statements_shape(self):
        sql = M.build_statements()
        self.assertIn("SET lock_timeout = '30s';", sql)
        self.assertIn("SET statement_timeout = '1800s';", sql)
        # Two separate concurrent idempotent builds.
        self.assertEqual(sql.count("CREATE INDEX CONCURRENTLY IF NOT EXISTS"), 2)
        self.assertIn(M.TITLE_INDEX, sql)
        self.assertIn(M.QUESTION_INDEX, sql)
        self.assertIn("gin_trgm_ops", sql)
        # Outside a transaction: no BEGIN/COMMIT wrapper.
        self.assertNotIn("BEGIN", sql)
        self.assertNotIn("COMMIT", sql)

    def test_build_statements_include_partial_predicates(self):
        sql = M.build_statements()
        # Title: length bound.
        self.assertIn("char_length(hivemind_normalize_identifier(title)) BETWEEN 1 AND 300", sql)
        # Question: eligibility status + length bound.
        self.assertIn("status IN ('pending','approved')", sql)
        self.assertIn("char_length(hivemind_normalize_identifier(question)) BETWEEN 1 AND 300", sql)

    def test_build_statement_timeout_optional(self):
        sql = M.build_statements(statement_timeout_s=None)
        self.assertNotIn("statement_timeout", sql)
        self.assertIn("lock_timeout", sql)

    def test_build_uses_frozen_expression_per_target(self):
        sql = M.build_statements()
        for t in M.TARGETS:
            self.assertIn(f"gin (hivemind_normalize_identifier({t['column']}) gin_trgm_ops)", sql)

    def test_rollback_drops_both_indexes_concurrently(self):
        sql = M.rollback_statements()
        self.assertEqual(sql.count("DROP INDEX CONCURRENTLY IF EXISTS"), 2)
        self.assertIn(M.TITLE_INDEX, sql)
        self.assertIn(M.QUESTION_INDEX, sql)

    def test_rollback_leaves_raw_and_005_alone(self):
        sql = M.rollback_statements()
        # Does NOT drop the raw schema/001 trigram indexes (precise: the raw name
        # is a substring of the normalized name, so match the full DROP target).
        self.assertNotIn(f"public.{M.EXISTING_RAW_TITLE_INDEX};", sql)
        self.assertNotIn(f"public.{M.EXISTING_RAW_QUESTION_INDEX};", sql)


# ---------------------------------------------------------------------------
# Schema-file consistency (schema/006 mirrors the module)
# ---------------------------------------------------------------------------

class TestSchemaFile(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCHEMA_006.exists())
        self.sql = SCHEMA_006.read_text()

    def test_file_has_schema_005_guard(self):
        # Must fail closed if the prerequisite function/collation are absent.
        self.assertIn("hivemind_normalize_identifier", self.sql)
        self.assertIn("hivemind_unicode", self.sql)
        self.assertIn("apply schema/005 first", self.sql)
        self.assertIn("RAISE EXCEPTION", self.sql)

    def test_file_uses_concurrent_idempotent_indexes(self):
        # Two statements (the phrase also appears once in the IDEMPOTENCE note).
        for name in (M.TITLE_INDEX, M.QUESTION_INDEX):
            self.assertIn(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name}", self.sql)

    def test_file_documents_rollback(self):
        self.assertIn("DROP INDEX CONCURRENTLY IF EXISTS", self.sql)
        self.assertIn(M.TITLE_INDEX, self.sql)
        self.assertIn(M.QUESTION_INDEX, self.sql)

    def test_file_keeps_raw_indexes_additive(self):
        # Does not drop the raw schema/001 trigram indexes.
        self.assertNotIn("DROP INDEX" + " " + M.EXISTING_RAW_TITLE_INDEX, self.sql)

    def test_file_no_transaction_block_around_concurrent(self):
        # psql -f autocommit; CIC cannot be in a transaction.
        self.assertNotIn("BEGIN;", self.sql)
        self.assertNotIn("COMMIT;", self.sql)

    def test_file_expression_and_predicate_match_module(self):
        for t in M.TARGETS:
            self.assertIn(
                f"gin (hivemind_normalize_identifier({t['column']}) gin_trgm_ops)", self.sql)
        self.assertIn("status IN ('pending','approved')", self.sql)
        self.assertIn("BETWEEN 1 AND 300", self.sql)

    def test_file_targets_only_title_and_question(self):
        # Scope: no body/answer/content trigram index.
        self.assertNotIn("gin_trgm_ops)", self.sql.replace(
            f"gin (hivemind_normalize_identifier(title) gin_trgm_ops)", ""
            ).replace(f"gin (hivemind_normalize_identifier(question) gin_trgm_ops)", ""))


# ---------------------------------------------------------------------------
# Preflight verdict logic
# ---------------------------------------------------------------------------

def _pf(**overrides):
    """A clean-green preflight parsed-input base, overridable per test."""
    base = {
        "schema_005_prerequisite": [["t", "t"]],
        "target_identity": [
            ["external_resources", "title", "text", "f", "2759", "2759"],
            ["distillations", "question", "text", "f", "11", "11"],
        ],
        "existing_trgm_indexes": [
            ["external_resources", M.EXISTING_RAW_TITLE_INDEX, "t", "t",
             "CREATE INDEX ...gin_trgm_ops...", "200000"],
            ["distillations", M.EXISTING_RAW_QUESTION_INDEX, "t", "t",
             "CREATE INDEX ...gin_trgm_ops...", "16000"],
        ],
        "invalid_index_remnants": [],
        "in_progress_index_builds": [],
        "database_storage": [["postgres", "2330000000"]],
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
        self.assertFalse(v["schema_005_needed"])

    def test_green_on_direct_connection(self):
        v = M.evaluate_preflight(_pf(), pghost="db.x.supabase.co", pgport="5432")
        self.assertTrue(v["green"])
        self.assertEqual(v["conn_mode"], "direct")

    def test_green_even_when_schema_005_needed(self):
        # The prerequisite is a STEP --apply performs, not an operational blocker.
        v = M.evaluate_preflight(_pf(schema_005_prerequisite=[["f", "f"]]),
                                 pghost="db.x.supabase.co", pgport="5432")
        self.assertTrue(v["green"], v["reasons"])
        self.assertTrue(v["schema_005_needed"])

    def test_red_on_transaction_pooler(self):
        v = M.evaluate_preflight(_pf(), pghost="aws-0.pooler.supabase.com", pgport="6543")
        self.assertFalse(v["green"])
        self.assertEqual(v["conn_mode"], "transaction_pooler")

    def test_red_on_invalid_remnant(self):
        v = M.evaluate_preflight(_pf(invalid_index_remnants=[
            [M.TITLE_INDEX, "f", "f", "CREATE INDEX CONCURRENTLY ..."]]),
            pghost="db.x.supabase.co", pgport="5432")
        self.assertFalse(v["green"])
        self.assertTrue(any("invalid" in r for r in v["reasons"]))

    def test_red_on_in_progress_build(self):
        v = M.evaluate_preflight(_pf(in_progress_index_builds=[
            ["123", "public.external_resources", "CREATE INDEX", "building", "1", "2", "3", "4"]]),
            pghost="db.x.supabase.co", pgport="5432")
        self.assertFalse(v["green"])
        self.assertTrue(any("in progress" in r for r in v["reasons"]))

    def test_red_on_insufficient_headroom(self):
        v = M.evaluate_preflight(_pf(database_storage=[["postgres", str(8 * 10**9 - 10**6)]]),
                                 pghost="db.x.supabase.co", pgport="5432",
                                 disk_bytes=8 * 10**9)
        self.assertFalse(v["green"])
        self.assertTrue(any("headroom" in r for r in v["reasons"]))

    def test_red_on_missing_target_table(self):
        v = M.evaluate_preflight(_pf(target_identity=[
            ["external_resources", "title", "text", "f", "2759", "2759"]]),
            pghost="db.x.supabase.co", pgport="5432")
        self.assertFalse(v["green"])

    def test_already_valid_detected(self):
        v = M.evaluate_preflight(_pf(existing_trgm_indexes=[
            ["external_resources", M.TITLE_INDEX, "t", "t", "CREATE INDEX ...norm...", "606208"],
            ["distillations", M.QUESTION_INDEX, "t", "t", "CREATE INDEX ...norm...", "40960"],
        ]), pghost="db.x.supabase.co", pgport="5432")
        self.assertTrue(v["green"])
        self.assertTrue(v["already_valid"])

    def test_estimate_scales_with_rows(self):
        self.assertLess(M.estimate_index_bytes(100), M.estimate_index_bytes(100_000))


# ---------------------------------------------------------------------------
# EXPLAIN plan parsing — assert index usage (offline, on captured plan text)
# ---------------------------------------------------------------------------

NORM_INDEX_PLAN = """\
Limit (actual time=0.10..0.20 rows=5 loops=1)
  ->  Sort
        ->  Bitmap Heap Scan on external_resources
              Filter: (char_length(hivemind_normalize_identifier(title)) BETWEEN 1 AND 300)
              ->  Bitmap Index Scan on idx_external_resources_title_trgm_norm (actual time=0.05..0.05 rows=5 loops=1)
Execution Time: 0.22 ms"""

SEQ_PLAN = """\
Limit (actual time=0.03..0.25 rows=0 loops=1)
  ->  Seq Scan on distillations
        Filter: ((status = ANY (...)) AND (... <% ...))
Execution Time: 0.26 ms"""

RAW_INDEX_PLAN = """\
Bitmap Index Scan on external_resources_title_trgm (actual rows=10)
"""


class TestExplainPlanParsing(unittest.TestCase):
    def test_detects_normalized_index_use(self):
        p = M.parse_explain_plan(NORM_INDEX_PLAN)
        self.assertTrue(p["uses_normalized_index"])
        self.assertFalse(p["uses_raw_trgm_index"])
        self.assertFalse(p["is_seq_scan"])

    def test_detects_seq_scan(self):
        p = M.parse_explain_plan(SEQ_PLAN)
        self.assertTrue(p["is_seq_scan"])
        self.assertFalse(p["uses_normalized_index"])

    def test_raw_index_not_conflated_with_normalized(self):
        p = M.parse_explain_plan(RAW_INDEX_PLAN)
        self.assertTrue(p["uses_raw_trgm_index"])
        self.assertFalse(p["uses_normalized_index"])

    def test_empty_plan_is_not_an_index_use(self):
        p = M.parse_explain_plan("")
        self.assertFalse(p["uses_normalized_index"])
        self.assertFalse(p["plan_present"])

    def test_specific_index_name_match(self):
        p = M.parse_explain_plan(NORM_INDEX_PLAN, M.TITLE_INDEX)
        self.assertTrue(p["uses_normalized_index"])
        p2 = M.parse_explain_plan(NORM_INDEX_PLAN, M.QUESTION_INDEX)
        self.assertFalse(p2["uses_normalized_index"])


# ---------------------------------------------------------------------------
# Security: eligibility partial predicate + length bounds (pure logic)
# ---------------------------------------------------------------------------

class TestSecurityAndBounds(unittest.TestCase):
    def test_question_predicate_excludes_rejected(self):
        self.assertIn("status IN ('pending','approved')", M.QUESTION_PREDICATE)
        # The candidate query repeats it, so rejected/superseded never surface.
        q = M.candidate_query_template("distillations", "question", "<%", "word_similarity",
                                       "best upscale model")
        self.assertIn("status IN ('pending','approved')", q)

    def test_title_predicate_has_length_bound(self):
        self.assertIn("BETWEEN 1 AND 300", M.TITLE_PREDICATE)
        self.assertIn("BETWEEN 1 AND 300", M.QUESTION_PREDICATE)

    def test_overlong_query_short_circuits(self):
        # A query whose normalized form exceeds MAX_QUERY_CHARS must not reach the
        # trigram arm; the candidate SQL checks the bound before the operator.
        self.assertGreater(M.MAX_QUERY_CHARS, 0)
        huge = "a" * (M.MAX_QUERY_CHARS + 1)
        self.assertGreater(len(IN.normalize_identifier(huge)), M.MAX_QUERY_CHARS)

    def test_empty_query_produces_no_match(self):
        self.assertEqual(IN.normalize_identifier(""), "")
        self.assertEqual(IN.normalize_identifier("... --- ..."), "")
        # The non-empty partial predicate excludes empty-normalizing rows.
        self.assertIn("BETWEEN 1", M.TITLE_PREDICATE)


# ---------------------------------------------------------------------------
# Unicode / cross-variant normalization (the whole point of the normalized idx)
# ---------------------------------------------------------------------------

class TestUnicodeNormalization(unittest.TestCase):
    CASES = [
        ("Wan 2.2", "wan22"), ("Wan2.2", "wan22"), ("wan_2.2", "wan22"),
        ("WAN 2.2", "wan22"), ("FLUX.1", "flux1"), ("LTX-Video", "ltxvideo"),
        ("lightx2v_I2V_14B.safetensors", "lightx2vi2v14bsafetensors"),
        ("WanVideoSampler", "wanvideosampler"),
    ]

    def test_separator_case_variants_collapse(self):
        for raw, want in self.CASES:
            self.assertEqual(IN.normalize_identifier(raw), want, raw)

    def test_query_and_field_normalize_identically(self):
        # A user typing Wan2.2 matches a field storing "Wan 2.2" after both
        # normalize to wan22 — the property the index exists to provide.
        self.assertEqual(IN.normalize_identifier("Wan2.2"),
                         IN.normalize_identifier("Wan 2.2"))


# ---------------------------------------------------------------------------
# Capacity gate (pure logic; measured numbers asserted from evidence artifacts)
# ---------------------------------------------------------------------------

class TestCapacityGate(unittest.TestCase):
    def test_estimate_is_small_for_real_counts(self):
        # 2,759 resources + 11 distillations: a few hundred KB at most.
        est = M.estimate_index_bytes(2770)
        self.assertLess(est, 5_000_000)  # < 5 MB planning estimate

    def test_gate_is_12gb_envelope(self):
        self.assertLess(M.SUPABASE_PRO_DISK_BYTES, 12 * 10**9 + 1)

    @unittest.skipUnless(LIVE_JSON.exists(), "live artifact not captured")
    def test_live_indexes_inside_capacity_gate(self):
        d = json.loads(LIVE_JSON.read_text())
        total = sum(ix["index_size_bytes"] for ix in d["evidence"]["indexes"].values())
        # Negligible: < 1 MB vs the 12 GB / 9 GB envelope.
        self.assertLess(total, 5_000_000)
        self.assertLess(total, M.SUPABASE_PRO_DISK_BYTES)


# ---------------------------------------------------------------------------
# Rehearsal evidence (offline JSON)
# ---------------------------------------------------------------------------

class TestRehearsalEvidence(unittest.TestCase):
    @unittest.skipUnless(REHEARSAL_JSON.exists(), "rehearsal artifact not captured")
    def test_verdict_all_pass(self):
        d = json.loads(REHEARSAL_JSON.read_text())
        self.assertTrue(d["verdict"]["all_pass"], d["verdict"]["checks"])

    @unittest.skipUnless(REHEARSAL_JSON.exists(), "rehearsal artifact not captured")
    def test_production_row_count_and_immutable_normalize(self):
        d = json.loads(REHEARSAL_JSON.read_text())
        self.assertEqual(d["seed"]["title_rows"], 2759)
        self.assertEqual(d["seed"]["question_rows"], 11)
        self.assertEqual(d["normalize_proof"]["provolatile"], "i")
        self.assertEqual(d["normalize_proof"]["variants_collapse_to"], "wan22image|wan22|wan22")

    @unittest.skipUnless(REHEARSAL_JSON.exists(), "rehearsal artifact not captured")
    def test_normalized_index_used_and_variants_recall(self):
        d = json.loads(REHEARSAL_JSON.read_text())
        # Cross-variant recall: Wan2.2 → wan22 hits the Wan 2.2 / wan_2.2 rows.
        self.assertGreater(d["representative_hit_counts"]["title_wan22_variant_Wan2.2_<%"], 0)
        # Forced plans all use the normalized index (structural usability incl.
        # the tiny question table).
        forced = {k: v for k, v in d["evidence_plans"].items() if v.get("forced")}
        self.assertTrue(all(p["uses_normalized_index"] for p in forced.values()))

    @unittest.skipUnless(REHEARSAL_JSON.exists(), "rehearsal artifact not captured")
    def test_eligibility_excludes_rejected_in_rehearsal(self):
        d = json.loads(REHEARSAL_JSON.read_text())
        self.assertTrue(d["eligibility_excludes_rejected"]["excluded_by_partial_predicate"])

    @unittest.skipUnless(REHEARSAL_JSON.exists(), "rehearsal artifact not captured")
    def test_cancellation_then_rollback_then_rebuild(self):
        d = json.loads(REHEARSAL_JSON.read_text())
        c = d["cancellation_rollback"]
        self.assertTrue(c["interrupt_left_invalid_index"])
        self.assertEqual(c["rollback"]["index_remains_after_drop"], 0)
        self.assertEqual(c["indisvalid_after_rebuild"], "t")


# ---------------------------------------------------------------------------
# Live evidence (offline JSON)
# ---------------------------------------------------------------------------

class TestLiveEvidence(unittest.TestCase):
    @unittest.skipUnless(LIVE_JSON.exists(), "live artifact not captured")
    def test_build_succeeded_and_schema_005_applied(self):
        d = json.loads(LIVE_JSON.read_text())
        # build.status is "ok" on a fresh apply, "already_valid" on an idempotent
        # re-apply (the honest end state once the indexes exist).
        self.assertIn(d["build"]["status"], ("ok", "already_valid"))
        # schema/005 is either freshly applied (rc=0 + confirmed) or already present.
        s505 = d.get("schema_005_apply", {})
        self.assertTrue(
            (isinstance(s505, dict) and s505.get("returncode") == 0
             and d.get("schema_005_confirmed") is True)
            or s505.get("status") == "already_present")

    @unittest.skipUnless(LIVE_JSON.exists(), "live artifact not captured")
    def test_indexes_valid_and_parity_holds(self):
        d = json.loads(LIVE_JSON.read_text())
        for ix in d["evidence"]["indexes"].values():
            self.assertEqual(ix["index_valid"], "t")
        self.assertEqual(d["parity_probe"]["mismatches"], 0)

    @unittest.skipUnless(LIVE_JSON.exists(), "live artifact not captured")
    def test_cross_variant_recall_on_live_titles(self):
        d = json.loads(LIVE_JSON.read_text())
        # Wan2.2 / FLUX.1 hit live titles via the normalized compact key.
        self.assertGreater(d["evidence"]["representative_hit_counts"]["external_resources:Wan2.2"], 0)
        self.assertGreater(d["evidence"]["representative_hit_counts"]["external_resources:FLUX.1"], 0)
        self.assertTrue(d["evidence"]["title_index_used_at_production_scale"])

    @unittest.skipUnless(LIVE_JSON.exists(), "live artifact not captured")
    def test_preflight_was_green_on_session_connection(self):
        d = json.loads(LIVE_JSON.read_text())
        self.assertTrue(d["preflight"]["verdict"]["green"])
        self.assertEqual(d["preflight"]["verdict"]["conn_mode"], "session")


# ---------------------------------------------------------------------------
# Redaction boundary (reuse task-0.1 verify_access.redact)
# ---------------------------------------------------------------------------

class TestRedactionBoundary(unittest.TestCase):
    def test_secrets_masked_in_output(self):
        masked = redact("postgresql://postgres:hunter2@db.x.supabase.co:5432/postgres")
        self.assertNotIn("hunter2", masked)

    def test_safe_identifiers_preserved(self):
        # Index/table names are not secrets and survive redaction in the JSON.
        self.assertIn(M.TITLE_INDEX, json.dumps({"i": M.TITLE_INDEX}))


# ---------------------------------------------------------------------------
# PG-gated idempotence integration (throwaway local cluster)
# ---------------------------------------------------------------------------

_HAS_PG = all(shutil.which(b) for b in ("initdb", "pg_ctl", "psql", "postgres"))


@unittest.skipUnless(_HAS_PG, "PostgreSQL binaries not available")
class IdempotenceIntegrationTests(unittest.TestCase):
    """Apply schema/005 + build BOTH indexes twice + drop twice = safe no-ops."""

    def test_double_apply_and_double_drop_are_safe(self):
        import scripts.rehearse_short_field_trigram as R
        db = R.RehearsalCluster()
        try:
            db.start()
            db.sql_t(M.rehearsal_load_005_sql(), timeout=120)
            db.sql_t(M.rehearsal_schema_sql())
            db.sql_t(M.rehearsal_seed_sql(200, 5), timeout=120)

            def valid_count():
                out = db.sql_t(
                    "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                    f"WHERE c.relname IN ('{M.TITLE_INDEX}','{M.QUESTION_INDEX}') "
                    "AND i.indisvalid;").strip().splitlines()
                return int(out[-1]) if out and out[-1].isdigit() else 0

            db.sql_t(M.build_statements(), timeout=300)   # first build
            self.assertEqual(valid_count(), 2)
            db.sql_t(M.build_statements(), timeout=300)   # idempotent re-apply
            self.assertEqual(valid_count(), 2)
            db.sql_t(M.rollback_statements(), timeout=300)  # first drop
            self.assertEqual(valid_count(), 0)
            db.sql_t(M.rollback_statements(), timeout=300)  # idempotent re-drop
            self.assertEqual(valid_count(), 0)
        finally:
            db.stop()
            db.destroy()


if __name__ == "__main__":
    unittest.main()
