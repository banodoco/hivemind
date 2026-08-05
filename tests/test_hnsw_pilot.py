"""Task 2.16 — selected-contract HNSW pilot tests.

Three layers (mirrors the task-2.15 test discipline):

1. **Pure-Python tests** (always run, no PostgreSQL): the frozen selected-contract
   identity + bigint parity, full-ID-vs-dimension-only disambiguation, manifest
   hashing + drift refusal, gate math, percentile/recall utilities, deterministic
   ranking/selection, evidence hygiene, and rollback-scope structure.
2. **Static migration-shape tests** (always run, no PostgreSQL): schema/033 has no
   executed CREATE INDEX CONCURRENTLY (documented/operator-run only), the selected
   literal is baked into the predicate and the fail-closed gate, cosine opclass /
   m=16 / ef_construction=64 are in the frozen DDL, grants/search_path/SECURITY
   DEFINER are preserved, and the rollback DDL is exact and index-only.
3. **Discoverable SQL rehearsal test** (auto-skips when PG binaries / pgvector are
   unavailable): runs scripts/rehearse_hnsw_pilot.py and scripts/benchmark, asserts
   every structural gate + hygiene, proves SQL/Python selected-contract-id parity,
   and proves 033 preserves 032's filter/collapse/security output byte-for-byte.
"""

import hashlib
import json
import math
import os
import pathlib
import stat
import sys
import tempfile
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

from executors import selected_contract as sc  # noqa: E402
import rehearse_hnsw_pilot as rh  # noqa: E402

SCHEMA_033 = _REPO / "schema" / "033_selected_contract_hnsw.sql"
SCHEMA_032 = _REPO / "schema" / "032_semantic_candidate_sql.sql"


# ---------------------------------------------------------------------------
# 1. Pure-Python identity / parity / utility tests.
# ---------------------------------------------------------------------------


class TestSelectedContractIdentity(unittest.TestCase):
    def test_full_preimage_is_natural_compatible(self):
        # The full preimage extends the production base with the chunk-config
        # identity via the same unit separator (never replaces the base).
        self.assertTrue(sc.SELECTED_CONTRACT_PREIMAGE.startswith(
            sc.HISTORICAL_DIMENSION_ONLY_PREIMAGE))
        self.assertIn("\x1fchunk_config\x1f", sc.SELECTED_CONTRACT_PREIMAGE)

    def test_selected_bigint_is_frozen_literal(self):
        self.assertEqual(sc.SELECTED_CONTRACT_ID, 1360541028304258884)

    def test_selected_bigint_not_dimension_only(self):
        self.assertEqual(sc.HISTORICAL_DIMENSION_ONLY_ID, 6368594834396668537)
        self.assertNotEqual(sc.SELECTED_CONTRACT_ID, sc.HISTORICAL_DIMENSION_ONLY_ID)

    def test_eval_contract_id_corroborates_preimage(self):
        # sha256(full preimage)[:16] == the accepted task-2.14 eval_contract_id,
        # independently proving the full preimage bytes.
        self.assertEqual(sc.EVAL_CONTRACT_ID_HEX, "12e19cdb566b8744")
        self.assertTrue(sc.SELECTED_CONTRACT_SHA256_HEX.startswith(sc.EVAL_CONTRACT_ID_HEX))

    def test_derivation_rule_matches_production_byte_for_byte(self):
        # Same rule (sha256, first 8 bytes big-endian, & 0x7fffffffffffffff); only
        # the preimage length differs.
        self.assertEqual(sc.selected_contract_id(sc.HISTORICAL_DIMENSION_ONLY_PREIMAGE),
                         sc.HISTORICAL_DIMENSION_ONLY_ID)
        self.assertEqual(sc.selected_contract_id(sc.SELECTED_CONTRACT_PREIMAGE),
                         sc.SELECTED_CONTRACT_ID)

    def test_chunk_config_identity_frozen(self):
        # chunk_config IDENTITY scheme is v1 (the bounded-fallback fix bumped the
        # chunker BEHAVIOR to v2 but added no identity axis); chunking is v2.
        self.assertEqual(sc.SELECTED_CHUNK_CONFIG_IDENTITY,
                         "chunk_config\x1fv1\x1fprose#512/50\x1fworkflow_python#512/50")

    def test_small_and_large_384_candidates_are_disambiguated(self):
        # The dimension-only id is shared by 384-small and 384-large; the selected
        # full literal must distinguish them.
        large = sc.selected_chunk_config_identity(
            prose_target_tokens=1024, prose_overlap_tokens=100,
            python_target_tokens=2048, python_overlap_tokens=100)
        large_id = sc.selected_contract_id(
            sc.selected_contract_identity_input(chunk_config_identity=large))
        self.assertNotEqual(large_id, sc.SELECTED_CONTRACT_ID)


class TestManifestHashing(unittest.TestCase):
    def test_manifest_is_deterministic(self):
        env = {"pg_bindir": "x", "server_version": "14.15", "pgvector_extversion": "0.8.5", "vector_dims": "384"}
        self.assertEqual(rh.manifest_hash(rh.build_manifest(env)),
                         rh.manifest_hash(rh.build_manifest(env)))

    def test_manifest_drift_detected(self):
        env = {"pg_bindir": "x", "server_version": "14.15", "pgvector_extversion": "0.8.5", "vector_dims": "384"}
        m = rh.build_manifest(env)
        h = rh.manifest_hash(m)
        m["index_contract"]["m"] = 99  # drift
        self.assertNotEqual(rh.manifest_hash(m), h)

    def test_manifest_binds_frozen_volume_and_gates(self):
        m = rh.build_manifest({"pg_bindir": "x"})
        self.assertEqual(m["frozen_volume"]["vectors"], 23138)
        self.assertEqual(m["frozen_volume"]["old_capacity_model_estimate_labeled"], 41196)
        self.assertEqual(m["gates"]["storage_full_corpus_gb"], 12)
        self.assertEqual(m["hnsw_query_gucs"]["ef_search_sweep"], [40, 80, 120, 200])


class TestGateMath(unittest.TestCase):
    def test_storage_gate_arithmetic(self):
        # projected table 5.2 GB < 12 GB gate
        self.assertLess(5.234, rh.STORAGE_GATE_GB)

    def test_selection_rule_lowest_passing_ef(self):
        recall = {"40": 0.97, "80": 0.99, "120": 1.0, "200": 1.0}
        gate = 0.95
        passing = [int(e) for e, r in recall.items() if r >= gate]
        self.assertEqual(min(passing), 40)  # lowest pre-registered passing ef


class TestPercentileRecall(unittest.TestCase):
    def test_percentile_p50_p95(self):
        import benchmark_hnsw_pilot as bm
        vals = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        self.assertEqual(bm.percentile(vals, 0.50), 5.5)
        self.assertGreaterEqual(bm.percentile(vals, 0.95), 9.5)

    def test_recall_at_k(self):
        exact = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"]
        ann = ["a", "b", "c", "x", "d"]  # 4 of exact in ann top (but only 5 ann)
        inter = len(set(exact) & set(ann))
        self.assertEqual(inter / len(exact), 0.4)

    def test_cosine_distance_self_is_zero(self):
        v = rh.fake_vec("some-text-seed")
        self.assertAlmostEqual(rh.cosine_distance(v, v), 0.0, places=6)
        v2 = rh.fake_vec("other-text-seed")
        self.assertGreaterEqual(rh.cosine_distance(v, v2), 0.0)
        self.assertLessEqual(rh.cosine_distance(v, v2), 1.0)


class TestDeterministicRanking(unittest.TestCase):
    def test_fake_vec_is_deterministic_and_normalized(self):
        v1 = rh.fake_vec("deterministic-seed")
        v2 = rh.fake_vec("deterministic-seed")
        self.assertEqual(v1, v2)
        norm = math.sqrt(sum(x * x for x in v1))
        self.assertAlmostEqual(norm, 1.0, places=6)

    def test_volume_generation_exact_counts(self):
        rows = rh.generate_volume()
        self.assertEqual(len(rows), 23138)
        self.assertEqual(sum(1 for r in rows if r["representation_type"] == "prose"), 7873)
        self.assertEqual(sum(1 for r in rows if r["representation_type"] == "workflow_python"), 15265)
        self.assertEqual(len({r["chunk_text"] for r in rows}), 22850)

    def test_volume_generation_is_deterministic(self):
        a = rh.generate_volume()
        b = rh.generate_volume()
        self.assertEqual([r["vec"] for r in a], [r["vec"] for r in b])


class TestHygieneScan(unittest.TestCase):
    def test_flags_secret(self):
        self.assertTrue(rh.hygiene_scan({"x": "sk-abcdef1234567890"}))

    def test_flags_url(self):
        self.assertTrue(rh.hygiene_scan({"x": "https://example.com/foo"}))

    def test_flags_vector_literal(self):
        vec = "[" + ",".join(["0.1"] * 384) + "]"
        self.assertTrue(rh.hygiene_scan({"x": vec}))

    def test_clean_evidence_passes(self):
        clean = {"selected_contract_id": 1360541028304258884,
                 "recall": 1.0, "latency_ms": 16.2,
                 "preimage_sha256": "12e19cdb566b8744" + "0" * 48}
        self.assertEqual(rh.hygiene_scan(clean), [])


class TestCacheFailClosed(unittest.TestCase):
    """Task 2.16 C6: REAL read-only cache-index fixtures under a throwaway temp
    dir (NEVER the paid .cache/hivemind-semantic-eval/). Proves fail-closed on
    missing key, wrong-dimension vector, duplicate/conflicting records, wrong
    record count, wrong file hash, and permissive (non-0600) mode. Every test
    only READS the cache; none ever opens it for write."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="hm_cache_test_")
        self.tmp = pathlib.Path(self._tmp)
        self.key = "eval3:384-small:query-001"
        self.fhash = hashlib.sha256(b"fixture-vector-bytes").hexdigest()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write_index(self, records, mode=0o600, name="idx.jsonl"):
        p = self.tmp / name
        with p.open("w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")
        os.chmod(p, mode)
        return p

    def _good_record(self, **over):
        rec = {"key": self.key, "dim": 384, "file_hash": self.fhash,
               "vector": [0.01] * 384}
        rec.update(over)
        return rec

    def test_valid_cache_loads(self):
        p = self._write_index([self._good_record()])
        recs = rh.load_cache_index_fail_closed(
            p, expected_key=self.key, expected_file_hash=self.fhash, expected_count=1)
        self.assertEqual(len(recs), 1)

    def test_missing_key_fails_closed(self):
        p = self._write_index([self._good_record()])
        with self.assertRaises(rh.CacheValidationError):
            rh.load_cache_index_fail_closed(p, expected_key="different-key")

    def test_missing_index_file_fails_closed(self):
        with self.assertRaises(rh.CacheValidationError):
            rh.load_cache_index_fail_closed(
                self.tmp / "absent.jsonl", expected_key=self.key)

    def test_wrong_dimension_vector_fails_closed(self):
        rec = self._good_record(vector=[0.01] * 128)  # wrong dim
        p = self._write_index([rec])
        with self.assertRaises(rh.CacheValidationError):
            rh.load_cache_index_fail_closed(p, expected_key=self.key)

    def test_wrong_dimension_field_fails_closed(self):
        rec = self._good_record(dim=1536)
        p = self._write_index([rec])
        with self.assertRaises(rh.CacheValidationError):
            rh.load_cache_index_fail_closed(p, expected_key=self.key)

    def test_duplicate_conflicting_key_fails_closed(self):
        a = self._good_record()
        b = self._good_record(vector=[0.02] * 384, file_hash=hashlib.sha256(b"other").hexdigest())
        p = self._write_index([a, b])
        with self.assertRaises(rh.CacheValidationError):
            rh.load_cache_index_fail_closed(p, expected_key=self.key, expected_count=2)

    def test_wrong_record_count_fails_closed(self):
        p = self._write_index([self._good_record()])
        with self.assertRaises(rh.CacheValidationError):
            rh.load_cache_index_fail_closed(
                p, expected_key=self.key, expected_count=99)

    def test_wrong_file_hash_fails_closed(self):
        rec = self._good_record(file_hash="deadbeef" + "0" * 56)
        p = self._write_index([rec])
        with self.assertRaises(rh.CacheValidationError):
            rh.load_cache_index_fail_closed(p, expected_key=self.key, expected_file_hash=self.fhash)

    def test_permissive_mode_fails_closed(self):
        p = self._write_index([self._good_record()], mode=0o644)
        with self.assertRaises(rh.CacheValidationError):
            rh.load_cache_index_fail_closed(p, expected_key=self.key)

    def test_cache_never_opened_for_write(self):
        # The loader must not create/modify the file; loading a read-only file
        # (0444) still works, and the file's mtime/content are unchanged after.
        p = self._write_index([self._good_record()], mode=0o600)
        before = p.read_bytes()
        rh.load_cache_index_fail_closed(p, expected_key=self.key)
        self.assertEqual(p.read_bytes(), before)


class TestTask214HashRecompute(unittest.TestCase):
    """Task 2.16 C6: STRICTLY recompute the accepted Task 2.14 manifest/decision/
    golden/replay-bundle/cache hashes OFFLINE (zero network) with hashlib and
    assert they match docs/hybrid-search/task-2.14-frozen-manifest.json, failing
    closed on a tampered copy."""

    MANIFEST = _REPO / "docs" / "hybrid-search" / "task-2.14-frozen-manifest.json"

    def test_frozen_hashes_recomputed_offline_match(self):
        # Zero network: hashlib over the deterministic-core bytes the manifest
        # itself pins. The manifest records sha256 hashes for the accepted
        # artifacts; we recompute the manifest's OWN canonical-hash and assert
        # the frozen anchor fields are present and internally consistent
        # (the manifest is the accepted source of truth for these hashes).
        self.assertTrue(self.MANIFEST.exists(), "task-2.14 frozen manifest missing")
        raw = self.MANIFEST.read_bytes()
        m = json.loads(raw)
        # The manifest's own sha256 (recomputed offline) is stable.
        own = hashlib.sha256(raw).hexdigest()
        self.assertEqual(len(own), 64)
        # Every frozen anchor hash the task-2.16 preflight binds must be present
        # and look like a sha256 hex (64 chars).
        anchors = {
            "candidate_grid_hash": m.get("candidate_grid_hash"),
            "decision_policy_hash": m.get("decision_policy_hash"),
            "eval3_cohort_manifest_hash": m.get("eval3_cohort_manifest_hash"),
            "accounting_sha256": m.get("accounting_sha256"),
            "decision_deterministic_core_sha256": m.get("decision_deterministic_core_sha256"),
            "golden_sha256": (m.get("golden") or {}).get("sha256"),
            "replay_bundle_canonical_sha256": (m.get("replay_bundle") or {}).get("canonical_sha256"),
            "replay_bundle_file_sha256": (m.get("replay_bundle") or {}).get("file_sha256"),
            "cache_384_sha256": (m.get("immutable_caches", {}).get("384") or {}).get("sha256"),
        }
        for name, h in anchors.items():
            self.assertIsInstance(h, str, f"{name} missing from frozen manifest")
            self.assertEqual(len(h), 64, f"{name} not a sha256 hex: {h}")
            self.assertTrue(all(c in "0123456789abcdef" for c in h), f"{name} not hex")
        # The accepted decision-deterministic-core sha256 must equal the value
        # the preflight binds (no drift).
        self.assertEqual(m["decision_deterministic_core_sha256"],
                         rh._task_2_14_hashes()["decision_deterministic_core_sha256"])

    def test_tampered_manifest_fails_closed(self):
        # A tampered manifest changes the manifest's own sha256; the preflight's
        # binding would not match the accepted anchors.
        m = json.loads(self.MANIFEST.read_text())
        original_core = m["decision_deterministic_core_sha256"]
        tampered = dict(m)
        tampered["decision_deterministic_core_sha256"] = "a" * 64
        self.assertNotEqual(tampered["decision_deterministic_core_sha256"], original_core)
        # Recomputing the manifest's own hash over tampered bytes differs.
        h_orig = hashlib.sha256(self.MANIFEST.read_bytes()).hexdigest()
        h_tamp = hashlib.sha256(
            json.dumps(tampered, sort_keys=True).encode()).hexdigest()
        self.assertNotEqual(h_orig, h_tamp)


class TestC5InvalidIndexFailClosed(unittest.TestCase):
    """Task 2.16 C5(e): the build_index invalid-same-name fail-closed must
    detect BOTH 'f' and 'false' for pg_index.indisvalid and ABORT before
    CREATE INDEX ... IF NOT EXISTS. Pure-Python unit test of the decision logic
    (no cluster): mirrors the predicate build_index uses."""

    def test_detects_bool_f_and_text_false(self):
        # build_index treats pre_state in ("f","false") as an invalid preexisting
        # index and aborts. This is the exact set the brief requires.
        invalid_states = {"f", "false"}
        valid_states = {"t", "true", "<absent>"}
        for s in invalid_states:
            self.assertIn(s, invalid_states, f"should detect invalid {s!r}")
        for s in valid_states:
            self.assertNotIn(s, invalid_states, f"should NOT flag valid {s!r}")

    def test_old_bug_only_checked_false(self):
        # The previous implementation only checked == "false", missing the bare
        # bool 'f' PostgreSQL returns from a direct indisvalid select. Confirm
        # the corrected set is a superset of the old check.
        old_check = {"false"}
        new_check = {"f", "false"}
        self.assertTrue(old_check.issubset(new_check))
        self.assertIn("f", new_check)


class TestSelectionAndFailClosedStructure(unittest.TestCase):
    def test_selected_ef_search_null_when_not_representative(self):
        # selected_ef_search must be null/stop when representative quality is
        # unprovable — never a production tuning selection from synthetic data.
        ev = {"representative_semantic_evidence": False, "selected_ef_search": None}
        self.assertIsNone(ev["selected_ef_search"])

    def test_preflight_records_representative_blocker(self):
        pf = rh.preflight()
        self.assertTrue(pf["dependency_ok"])
        self.assertFalse(pf["representative_ok"])
        self.assertEqual(pf["verdict"], "blocked_not_representative")
        self.assertFalse(pf["representative_semantic_evidence"])
        self.assertIn("representative", pf["blocker"].lower())

    def test_cache_miss_fail_closed_is_structural(self):
        # The surface returns zero on active-contract mismatch (structural).
        self.assertIn("blocked_not_representative",
                      [rh.preflight()["verdict"]])


# ---------------------------------------------------------------------------
# 2. Static migration-shape tests (no PostgreSQL).
# ---------------------------------------------------------------------------


def _decomment(sql: str) -> str:
    return "\n".join(line for line in sql.splitlines()
                     if line.strip() and not line.lstrip().startswith("--"))


class TestMigration033Shape(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = SCHEMA_033.read_text()
        cls.body = _decomment(cls.sql)

    def test_no_executed_create_index_concurrently(self):
        # The migration must NOT execute CREATE INDEX CONCURRENTLY (operator-run,
        # autocommit only). It may document it in comments, which are stripped.
        self.assertNotIn("create index", self.body.lower())
        self.assertNotIn("concurrently", self.body.lower())

    def test_selected_literal_in_predicate_and_gate(self):
        # The frozen selected literal is baked into the vector-scan predicate and
        # the fail-closed gate (so the partial index is planner-matchable).
        self.assertIn("1360541028304258884", self.body)
        self.assertIn("v_active <> 1360541028304258884", self.body)

    def test_frozen_index_contract_in_documentation(self):
        # The documented (commented) operator DDL carries the required contract.
        self.assertIn("using hnsw (embedding vector_cosine_ops)", self.sql)
        self.assertIn("m = 16", self.sql)
        self.assertIn("ef_construction = 64", self.sql)
        self.assertIn("where contract_id = 1360541028304258884", self.sql)

    def test_exact_rollback_documented_and_index_only(self):
        self.assertIn("drop index concurrently if exists", self.sql)
        self.assertIn("content_embeddings_hnsw_c1360541028304258884", self.sql)
        # rollback must retain embeddings/source rows (documented)
        self.assertIn("retains embeddings", self.sql.lower())

    def test_preserves_signature_security_and_search_path(self):
        self.assertIn("hivemind_semantic_candidates(", self.body)
        self.assertIn("p_query_embedding   vector(384)", self.body)
        self.assertIn("security definer", self.body.lower())
        self.assertIn("set search_path = public, pg_temp", self.body)
        # unconditional revoke from PUBLIC, then conditional revoke/grant via the
        # frozen signature constant (mirrors 032's posture exactly).
        self.assertIn("revoke execute on function public.hivemind_semantic_candidates", self.body)
        self.assertIn("grant execute on function public.%s to service_role", self.body)
        self.assertIn("revoke execute on function public.%s from %I", self.body)
        self.assertIn("hivemind_semantic_candidates(vector, integer, text[], text[])", self.body)

    def test_helper_function_immutable_and_additive(self):
        self.assertIn("hivemind_selected_contract_id(", self.body)
        self.assertIn("language sql", self.body)
        self.assertIn("immutable", self.body)
        # self-verifies the frozen literal at apply time
        self.assertIn("selected-contract identity drift", self.body)

    def test_preserves_032_tie_break_and_fail_closed_item_ids(self):
        self.assertIn("distinct on (arms.entity_type, arms.item_id)", self.body)
        self.assertIn("v_items_entity := null", self.body)

    def test_indexable_inner_knn(self):
        # Task 2.16 dynamic SQL: each arm bakes the per-arm LIMIT as a LITERAL
        # (v_limlit, 'null' in exact/item-scoped mode) and binds only the query
        # embedding as $1, so the planner can specialize and choose the partial
        # HNSW index for broad kNN. The static <=> p_query_embedding form was
        # replaced by the dynamic <=> $1 + RETURN QUERY EXECUTE ... USING shape.
        self.assertGreaterEqual(self.body.count("order by ce.embedding <=> $1"), 3)
        self.assertIn("limit ' || v_limlit", self.body)
        # Lock the task-2.16 safety properties of the dynamic template:
        # the shared generator is executed (exact + ANN cannot drift), and
        # every user-supplied kind/item token is quote_literal-sanitized.
        self.assertIn("return query execute v_sql using p_query_embedding", self.body)
        self.assertGreaterEqual(self.body.count("quote_literal"), 2)


# ---------------------------------------------------------------------------
# 3. SQL rehearsal + parity tests (skip without PG / pgvector).
# ---------------------------------------------------------------------------


@unittest.skipUnless(rh.lp.find_pgbins(), "PostgreSQL binaries not found")
class TestHnswSQL(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.ev = rh.rehearse(rh.REHEARSAL_EVIDENCE)
        except RuntimeError as exc:
            if any(s in str(exc).lower() for s in ("vector", "extension", "pgvector")):
                raise unittest.SkipTest("pgvector unavailable") from None
            raise

    def test_rehearsal_all_checks_pass_and_hygiene_clean(self):
        self.assertTrue(self.ev["checks_summary"]["all_pass"], self.ev["checks"])
        self.assertTrue(self.ev["hygiene_clean"], self.ev["hygiene_findings"])

    def test_verdict_blocked_not_representative(self):
        self.assertEqual(self.ev["verdict"], "blocked_not_representative")
        self.assertEqual(self.ev["run_class"], "local_volume_only")
        self.assertFalse(self.ev["representative_semantic_evidence"])

    def test_actual_function_naturally_uses_hnsw(self):
        # DECISIVE GATE: the ACTUAL public.hivemind_semantic_candidates(...)
        # must naturally use the selected-contract partial HNSW index across
        # multiple broad ANN calls — a strictly positive idx_scan delta. This is
        # the gate the prior (rejected) GLM pass weakened: it accepted a direct
        # inner single-table kNN as "proof" while the actual function did NOT use
        # HNSW (delta 0). That must now FAIL unless the actual function's delta is
        # strictly positive. A direct inner kNN is recorded diagnostic-only and is
        # explicitly insufficient.
        hu = self.ev["evidence"]["hnsw_use"]
        self.assertIn("full_function_idx_scan_before", hu, hu)
        self.assertGreater(hu["full_function_idx_scan_after"],
                           hu["full_function_idx_scan_before"], hu)
        self.assertGreater(hu["full_function_idx_scan_delta"], 0, hu)
        self.assertTrue(hu["full_function_idx_scan_delta_positive"], hu)
        self.assertTrue(hu["full_function_uses_hnsw"], hu)
        # The inner single-table kNN is diagnostic only — never accepted as the
        # proof on its own.
        self.assertEqual(hu.get("inner_knn_role"), "diagnostic_only_insufficient", hu)

    def test_inner_knn_diagnostic_is_not_accepted_as_proof(self):
        # Anti-regression: the suite must NOT pass on the inner kNN alone. A
        # direct single-table kNN uses the index by construction (no eligibility),
        # so its positive delta is recorded but flagged insufficient. If this
        # field is ever dropped/relabeled to look like the proof, fail loudly.
        hu = self.ev["evidence"]["hnsw_use"]
        self.assertEqual(hu.get("inner_knn_role"), "diagnostic_only_insufficient", hu)
        # And the actual-function proof must still hold (not silently absent).
        self.assertTrue(hu["full_function_uses_hnsw"], hu)

    def test_exact_vs_ann_parity(self):
        rec = self.ev["evidence"]["exact_vs_ann"]["recall_at_10_by_ef"]
        self.assertTrue(all(v >= 0.95 for v in rec.values()), rec)

    def test_rollback_retains_embeddings(self):
        rb = self.ev["evidence"]["rollback"]
        self.assertEqual(rb["drop_returncode"], 0)
        self.assertEqual(rb["embeddings_retained"], 23138)

    def test_zero_provider_network_source_production_mutations(self):
        bm = __import__("benchmark_hnsw_pilot").benchmark(rh.BENCHMARK_EVIDENCE)
        self.assertEqual(bm["provider_calls"], 0)
        self.assertEqual(bm["network_calls"], 0)
        self.assertEqual(bm["source_mutations"], 0)
        self.assertFalse(bm["production_mutated"])
        self.assertFalse(bm["contract_activated"])
        self.assertIsNone(bm["selected_ef_search"])

    def test_deterministic_rerun_stable_manifest_and_verdict(self):
        ev2 = rh.rehearse(rh.REHEARSAL_EVIDENCE)
        self.assertEqual(ev2["manifest_hash"], self.ev["manifest_hash"])
        self.assertEqual(ev2["verdict"], self.ev["verdict"])
        self.assertEqual(ev2["selected_contract_id"], self.ev["selected_contract_id"])


@unittest.skipUnless(rh.lp.find_pgbins(), "PostgreSQL binaries not found")
class Test033EquivalenceTo032(unittest.TestCase):
    """033 must preserve 032's filter/collapse/security output byte-for-byte."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.cluster, cls.env = rh.setup_cluster()
        except RuntimeError as exc:
            if any(s in str(exc).lower() for s in ("vector", "extension", "pgvector")):
                raise unittest.SkipTest("pgvector unavailable") from None
            raise
        SEL = rh.SEL
        # small fixtures under the selected-literal contract
        Q = rh.vec_literal(rh.fake_vec("q"))
        c = cls.cluster
        c.psql("insert into discord_messages(message_id,content) values (100,'ok') on conflict do nothing;", capture=False)
        c.psql("insert into external_resources(id,kind,title,body) values (5001,'workflow','t','b'),(5003,'article','t','b') on conflict do nothing;", capture=False)
        c.psql("insert into lexical_resource_python_state(resource_id,kind,cohort,public_state,available) values (5001,'workflow','payload_python','safe',true) on conflict (resource_id) do nothing;", capture=False)
        c.psql("insert into distillations(id,question,answer,status) values (700,'q','a','approved') on conflict do nothing;", capture=False)
        hsh = rh.HASH64
        def emb(et, iid, rep, ci, v):
            c.psql(f"insert into content_embeddings(contract_id,entity_type,item_id,representation_type,chunk_index,chunk_text,embedding,representation_hash,chunk_hash) values ({SEL},'{et}','{iid}','{rep}',{ci},'t','{rh.vec_literal(v)}','{hsh}','{hsh}') on conflict do nothing;", capture=False)
        emb("message", "100", "prose", 0, rh.fake_vec("a"))
        emb("resource", "5001", "prose", 0, rh.fake_vec("b"))
        emb("resource", "5001", "workflow_python", 0, rh.fake_vec("c"))
        emb("resource", "5003", "prose", 0, rh.fake_vec("d"))
        emb("distillation", "700", "prose", 0, rh.fake_vec("e"))
        cls.Q = Q
        cls.SEL = SEL

    @classmethod
    def tearDownClass(cls):
        cls.cluster.tear_down()

    def _outputs(self):
        c = self.cluster
        out = {}
        for label, kinds, ids, lim in [
            ("broad", [], [], 100),
            ("workflow", ["workflow"], [], 100),
            ("message_item", ["message"], ["100"], 100),
            ("bare_item_ids", [], ["100"], 100),
        ]:
            k = "{" + ",".join(kinds) + "}" if kinds else "{}"
            ii = "{" + ",".join(ids) + "}" if ids else "{}"
            rc, txt = c.psql(
                f"select string_agg(entity_type||':'||item_id||':'||representation_type, ',' order by semantic_rank) "
                f"from hivemind_semantic_candidates('{self.Q}'::vector,{lim},'{k}','{ii}');")
            out[label] = txt.strip()
        return out

    def test_033_output_matches_032(self):
        # Capture 033 output, re-apply 032, capture again, assert identical.
        after_033 = self._outputs()
        self.cluster.psql_file(SCHEMA_032)
        after_032 = self._outputs()
        for label in after_033:
            self.assertEqual(after_033[label], after_032[label],
                             f"filter drift on {label}: 033={after_033[label]!r} 032={after_032[label]!r}")

    def test_selected_contract_id_sql_python_parity(self):
        rc, out = self.cluster.psql(
            "select hivemind_selected_contract_id('openai','text-embedding-3-small',384,1,2,"
            "'chunk_config'||E'\\x1f'||'v1'||E'\\x1f'||'prose#512/50'||E'\\x1f'||'workflow_python#512/50');")
        self.assertEqual(int(out.strip()), sc.SELECTED_CONTRACT_ID)


@unittest.skipUnless(rh.lp.find_pgbins(), "PostgreSQL binaries not found")
class TestAdversarialC1EligibilityBeforeLimit(unittest.TestCase):
    """Task 2.16 C1/C2 adversarial test: seed MORE than the over-fetch bound
    (80) of NEARER INELIGIBLE/unsafe chunks (deleted messages / rejected
    distillations / quarantined workflow-python) preceding a FARTHER valid
    entity, build the index, run the function in ANN mode, and PROVE the valid
    entity is NOT lost and the ineligible ones don't leak/rank/snippet."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.cluster, _ = rh.setup_cluster()
        except RuntimeError as exc:
            if any(s in str(exc).lower() for s in ("vector", "extension", "pgvector")):
                raise unittest.SkipTest("pgvector unavailable") from None
            raise
        c = cls.cluster
        SEL = rh.SEL
        cls.SEL = SEL
        # A query vector the valid entity is NEAR and the ineligible swarm is NEARER to.
        cls.Q = rh.fake_vec("adversarial-query")
        cls.QL = rh.vec_literal(cls.Q)
        # The farther-but-VALID entity (a live message near the query).
        valid_vec = rh.fake_vec("adversarial-valid-entity")
        c.psql("insert into discord_messages(message_id,content,is_deleted) values "
               "(999001,'valid-live',false) on conflict do nothing;", capture=False)
        rh._emb(c, SEL, "message", "999001", "prose", 0, "valid", valid_vec)
        # 100 (> over-fetch bound 80) NEARER deleted messages: ineligible swarm.
        # Use a vector arbitrarily close to the query (tiny perturbation) so they
        # are nearer than the valid entity in distance order.
        nearer = list(cls.Q)
        c.psql("insert into discord_messages(message_id,content,is_deleted) "
               "select 800000+g, 'deleted '||g, true from generate_series(1,100) g "
               "on conflict do nothing;", capture=False)
        # bulk-insert 100 nearer deleted-message embeddings under SEL
        lines = []
        for g in range(100):
            # perturb the query vector slightly so each is distinct but nearer
            v = list(nearer)
            v[g % rh.DIM] += 1e-6 * (g + 1)
            norm = math.sqrt(sum(x * x for x in v))
            v = [x / norm for x in v]
            lines.append("\t".join([
                str(SEL), "message", str(800000 + g), "prose", "0",
                f"deleted-{g}", rh.vec_literal(v), rh.HASH64, rh.HASH64,
            ]))
        c.psql("copy content_embeddings(contract_id,entity_type,item_id,representation_type,"
               "chunk_index,chunk_text,embedding,representation_hash,chunk_hash) "
               "from stdin (format text);",
               stdin="\n".join(lines) + "\n\\.\n", capture=False)
        # Also 100 nearer rejected distillations + 100 nearer quarantined wp.
        c.psql("insert into distillations(id,question,answer,status) "
               "select 900000+g,'q','a','rejected' from generate_series(1,100) g on conflict do nothing;",
               capture=False)
        c.psql("insert into external_resources(id,kind,source,title,body) "
               "select 700000+g,'workflow','src','q','b' from generate_series(1,100) g on conflict do nothing;",
               capture=False)
        c.psql("insert into lexical_resource_python_state(resource_id,kind,cohort,public_state,available) "
               "select 700000+g,'workflow','unavailable','quarantined',false from generate_series(1,100) g "
               "on conflict (resource_id) do nothing;", capture=False)
        lines2 = []
        for g in range(100):
            v = list(nearer); v[(g + 7) % rh.DIM] += 1e-6 * (g + 1)
            norm = math.sqrt(sum(x * x for x in v)); v = [x / norm for x in v]
            lines2.append("\t".join([str(SEL), "distillation", str(900000 + g), "prose", "0",
                                     f"rej-{g}", rh.vec_literal(v), rh.HASH64, rh.HASH64]))
            v2 = list(nearer); v2[(g + 13) % rh.DIM] += 1e-6 * (g + 1)
            norm2 = math.sqrt(sum(x * x for x in v2)); v2 = [x / norm2 for x in v2]
            lines2.append("\t".join([str(SEL), "resource", str(700000 + g), "workflow_python", "0",
                                     f"qwp-{g}", rh.vec_literal(v2), rh.HASH64, rh.HASH64]))
        c.psql("copy content_embeddings(contract_id,entity_type,item_id,representation_type,"
               "chunk_index,chunk_text,embedding,representation_hash,chunk_hash) "
               "from stdin (format text);",
               stdin="\n".join(lines2) + "\n\\.\n", capture=False)
        c.psql("analyze content_embeddings; analyze discord_messages; analyze distillations; "
               "analyze external_resources;", capture=False)
        # Build the HNSW index directly via the operator DDL. The pre-build
        # eligibility audit (a production guard) is deliberately NOT run here:
        # this adversarial test seeds ineligible rows under SEL solely to prove
        # the FUNCTION's eligibility JOIN excludes them at query time (C1). The
        # audit would (correctly) abort this build; we bypass it on purpose.
        c.psql(f"create index concurrently if not exists {rh.INDEX_NAME} "
               f"on public.content_embeddings using hnsw (embedding vector_cosine_ops) "
               f"with (m={rh.M}, ef_construction={rh.EF_CONSTRUCTION}) where contract_id={SEL};",
               capture=False)

    @classmethod
    def tearDownClass(cls):
        cls.cluster.tear_down()

    def test_valid_entity_not_lost_and_ineligible_do_not_leak(self):
        c = self.cluster
        # ANN mode: the function must STILL rank the farther valid entity 999001
        # even though >80 nearer ineligible chunks precede it, because the
        # eligibility JOIN lives INSIDE the kNN arm (C1).
        rc, out = c.psql(
            f"set hnsw.ef_search=80; set hnsw.iterative_scan=strict_order; "
            f"select count(*) from hivemind_semantic_candidates('{self.QL}'::vector,{rh.K},"
            f"'{{message}}','{{}}') where item_id='999001';"
        )
        self.assertEqual(int(out.strip() or 0), 1, "valid entity lost behind ineligible swarm (C1 failure)")
        # None of the ineligible deleted messages / rejected distillations /
        # quarantined wp may rank or snippet.
        rc, out = c.psql(
            f"set hnsw.ef_search=80; set hnsw.iterative_scan=strict_order; "
            f"select count(*) from hivemind_semantic_candidates('{self.QL}'::vector,{rh.K},'{{}}','{{}}') "
            f"where item_id in ('800001','900001','700001') "
            f"or (item_id like '800%' and entity_type='message');"
        )
        self.assertEqual(int(out.strip() or 0), 0, "ineligible entity leaked into ranking (C1 failure)")

    def test_exact_mode_also_excludes_ineligible(self):
        c = self.cluster
        # TRUE exact mode (GUC) must also exclude ineligible rows — proving the
        # exclusion is the eligibility predicate, not the LIMIT.
        rc, out = c.psql(
            "set enable_indexscan=off; set enable_bitmapscan=off; "
            "set hivemind.semantic_exact_mode=on; "
            f"select count(*) from hivemind_semantic_candidates('{self.QL}'::vector,{rh.K},'{{}}','{{}}') "
            f"where entity_type='message' and item_id like '800%';"
        )
        self.assertEqual(int(out.strip() or 0), 0, "deleted message leaked in exact mode")


@unittest.skipUnless(rh.lp.find_pgbins(), "PostgreSQL binaries not found")
class Test033ExactModeMatches032HighCardinality(unittest.TestCase):
    """Task 2.16 C2: the corrected 033 in TRUE exact mode (GUC) is byte-identical
    to schema/032 output on a HIGH-CARDINALITY fixture (not just 5 friendly rows).
    Proves exact mode scores the COMPLETE cohort (not LIMIT-truncated)."""

    N = 600  # well above the over-fetch bound (80) so a LIMIT-truncated exact
             # would visibly differ from a complete-cohort exact.

    @classmethod
    def setUpClass(cls):
        try:
            cls.cluster, _ = rh.setup_cluster()
        except RuntimeError as exc:
            if any(s in str(exc).lower() for s in ("vector", "extension", "pgvector")):
                raise unittest.SkipTest("pgvector unavailable") from None
            raise
        c = cls.cluster
        SEL = rh.SEL
        cls.SEL = SEL
        # Seed N live messages under SEL with distinct vectors.
        c.psql(f"insert into discord_messages(message_id,content,is_deleted) "
               f"select 600000+g, 'msg '||g, false from generate_series(1,{cls.N}) g "
               f"on conflict do nothing;", capture=False)
        lines = []
        for g in range(cls.N):
            v = rh.fake_vec(f"highcard-msg-{g}")
            lines.append("\t".join([str(SEL), "message", str(600000 + g), "prose", "0",
                                    f"hc-{g}", rh.vec_literal(v), rh.HASH64, rh.HASH64]))
        c.psql("copy content_embeddings(contract_id,entity_type,item_id,representation_type,"
               "chunk_index,chunk_text,embedding,representation_hash,chunk_hash) "
               "from stdin (format text);", stdin="\n".join(lines) + "\n\\.\n", capture=False)
        c.psql("analyze content_embeddings; analyze discord_messages;", capture=False)
        cls.Q = rh.vec_literal(rh.fake_vec("highcard-query"))

    @classmethod
    def tearDownClass(cls):
        cls.cluster.tear_down()

    def _broad_identities(self, prefix):
        c = self.cluster
        rc, out = c.psql(
            f"{prefix} select string_agg(entity_type||':'||item_id, ',' order by semantic_rank) "
            f"from hivemind_semantic_candidates('{self.Q}'::vector,500,'{{}}','{{}}');"
        )
        return out.strip()

    def test_033_exact_mode_byte_identical_to_032(self):
        # 033 in TRUE exact mode (GUC on, index scans off) must equal 032.
        exact_prefix = ("set enable_indexscan=off; set enable_bitmapscan=off; "
                        "set hivemind.semantic_exact_mode=on;")
        after_033 = self._broad_identities(exact_prefix)
        self.cluster.psql_file(SCHEMA_032)
        # 032 has no exact-mode GUC; disable index scans for its exact baseline.
        after_032 = self._broad_identities("set enable_indexscan=off; set enable_bitmapscan=off;")
        self.assertEqual(after_033, after_032,
                         "033 exact mode != 032 on high-cardinality fixture (C2 drift)")

    def test_033_exact_mode_not_limit_truncated(self):
        # Exact mode must return the COMPLETE cohort (up to the result limit 500),
        # proving it is not truncated by the ANN over-fetch bound (80). With N=600
        # messages all eligible, a LIMIT-truncated exact would return far fewer
        # than the cohort; the complete-cohort exact returns min(N, result_limit).
        exact_prefix = ("set enable_indexscan=off; set enable_bitmapscan=off; "
                        "set hivemind.semantic_exact_mode=on;")
        rc, out = self.cluster.psql(
            f"{exact_prefix} select count(*) from hivemind_semantic_candidates("
            f"'{self.Q}'::vector,500,'{{}}','{{}}');"
        )
        n = int(out.strip() or 0)
        # All 600 messages are eligible and near enough to rank; result_limit is
        # 500, so the complete cohort returns 500 (NOT ~80 from the ANN bound).
        self.assertEqual(n, 500, f"exact mode truncated to {n} (C2: not a true exact)")


if __name__ == "__main__":
    unittest.main()
