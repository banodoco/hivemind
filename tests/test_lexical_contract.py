"""Tests for the Phase-1 lexical contract (plan task 1.1).

Pins the frozen decisions in ``executors.lexical_contract`` and the on-disk
machine-readable contract ``docs/hybrid-search/phase1-lexical-contract.json``:
the canonical ``'simple'`` regconfig, the weighted tsvector expressions, query
constructors, normalizers, eligibility predicates, ranking inputs, chunk collapse,
the snowflake boundary, and the observed simple-vs-english evidence.

Offline only: no database, no network, no provider.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

import executors.lexical_contract as L  # noqa: E402

CONTRACT_JSON = REPO / "docs" / "hybrid-search" / "phase1-lexical-contract.json"


class TestCanonicalConfig(unittest.TestCase):
    def test_simple_is_the_single_canonical_config(self):
        self.assertEqual(L.LEXICAL_CONFIG, "simple")

    def test_every_representation_uses_simple(self):
        for spec in L.WEIGHTED_SPECS.values():
            self.assertEqual(spec.config, "simple")
            self.assertIn("'simple'::regconfig", spec.expression())

    def test_message_uses_simple_bare_field(self):
        self.assertEqual(L.MESSAGE_BARE_SOURCE, "content")

    def test_english_index_superseded_metadata(self):
        contract = json.loads(CONTRACT_JSON.read_text())
        idx = contract["config_decision"]["superseded_live_index"]
        self.assertEqual(idx["expression"], "to_tsvector('english'::regconfig, content)")
        self.assertFalse(idx["usable_by_canonical_simple_query"])

    def test_simple_vs_english_evidence_recorded(self):
        contract = json.loads(CONTRACT_JSON.read_text())
        ev = contract["simple_vs_english_evidence"]
        self.assertFalse(ev["config_mismatch"]["to_tsvector('english','WanVideoSampler') @@ websearch_to_tsquery('simple','WanVideoSampler')"])
        self.assertEqual(ev["stemming"]["simple_WanVideoSampler"], "wanvideosampler")
        self.assertEqual(ev["stemming"]["english_WanVideoSampler"], "wanvideosampl")
        self.assertFalse(ev["identifier_query_mismatch"]["match"])


class TestWeightedExpressions(unittest.TestCase):
    def test_distillation_weights(self):
        weights = {a.field: a.weight for a in L.DISTILLATION_SPEC.arms}
        self.assertEqual(weights["question"], "A")
        self.assertEqual(weights["conditions"], "B")
        self.assertEqual(weights["answer"], "C")

    def test_resource_prose_weights(self):
        weights = {a.field: a.weight for a in L.RESOURCE_PROSE_SPEC.arms}
        self.assertEqual(weights["title"], "A")
        self.assertEqual(weights["tags_semantics"], "B")
        self.assertEqual(weights["body"], "C")

    def test_workflow_python_is_chunked_uniform_weight(self):
        self.assertTrue(L.WORKFLOW_PYTHON_SPEC.chunked)
        (arm,) = L.WORKFLOW_PYTHON_SPEC.arms
        self.assertEqual(arm.field, "python_chunk")
        self.assertEqual(arm.weight, "C")

    def test_expressions_are_null_safe_and_weighted(self):
        for spec in L.WEIGHTED_SPECS.values():
            expr = spec.expression()
            self.assertIn("coalesce(", expr)
            self.assertIn("setweight(", expr)

    def test_json_spec_expressions_match_module_byte_for_byte(self):
        contract = json.loads(CONTRACT_JSON.read_text())
        mod = {(s["entity_type"], s["representation_type"]): s["expression"] for s in L.summarize()["specs"]}
        for spec in contract["specs"]:
            key = (spec["entity_type"], spec["representation_type"])
            self.assertEqual(spec["expression"], mod[key])


class TestQueryArms(unittest.TestCase):
    def test_default_constructor_is_websearch(self):
        self.assertEqual(L.QUERY_CONSTRUCTOR_DEFAULT, "websearch_to_tsquery")

    def test_phrase_constructor_is_phraseto(self):
        self.assertEqual(L.QUERY_CONSTRUCTOR_PHRASE, "phraseto_tsquery")

    def test_multi_term_natural_query(self):
        arms = {a["arm"] for a in L.query_arms("controlnet settings")}
        self.assertEqual(arms, {"fts", "ident"})

    def test_quoted_phrase_adds_phrase_arm(self):
        arms = {a["arm"] for a in L.query_arms('"block swap"')}
        self.assertEqual(arms, {"fts", "phrase", "ident"})

    def test_single_bare_name_adds_phrase_arm(self):
        arms = {a["arm"] for a in L.query_arms("WanVideoSampler")}
        self.assertEqual(arms, {"fts", "phrase", "ident"})

    def test_spaced_version_name_no_phrase_arm(self):
        arms = {a["arm"] for a in L.query_arms("Wan 2.2")}
        self.assertIn("ident", arms)
        self.assertNotIn("phrase", arms)


class TestNormalization(unittest.TestCase):
    def test_wan_variants_collapse(self):
        for v in ("Wan 2.2", "Wan2.2", "wan_2.2", "WAN 2.2", "wan2.2"):
            self.assertEqual(L.normalize_identifier(v), "wan22", v)

    def test_punctuation_collapses(self):
        self.assertEqual(L.normalize_identifier("FLUX.1"), "flux1")
        self.assertEqual(L.normalize_identifier("LTX-Video"), "ltxvideo")
        self.assertEqual(L.normalize_identifier("model.safetensors"), "modelsafetensors")
        self.assertEqual(L.normalize_identifier("lightx2v_I2V_14B.safetensors"), "lightx2vi2v14bsafetensors")

    def test_query_normalization_nfc_whitespace_no_lowercase(self):
        self.assertEqual(L.normalize_query("  Wan   2.2  "), "Wan 2.2")
        self.assertEqual(L.normalize_query(None), "")

    def test_identifier_none_safe(self):
        self.assertEqual(L.normalize_identifier(None), "")

    def test_aliases_yield_compact_and_spaced(self):
        forms = L.identifier_aliases("Wan 2.2")
        self.assertIn("wan22", forms)


class TestEligibility(unittest.TestCase):
    def test_message_soft_delete_predicate(self):
        self.assertIn("is_deleted = false", L.MESSAGE_ELIGIBILITY.predicates[0])

    def test_message_queries_discord_messages_directly(self):
        self.assertEqual(L.MESSAGE_ELIGIBILITY.base_table, "public.discord_messages")
        self.assertEqual(L.MESSAGE_ELIGIBILITY.identity_column, "message_id::text")

    def test_author_optout_flag_bound(self):
        msg = " ".join(L.MESSAGE_ELIGIBILITY.predicates)
        self.assertIn("author_optout_enabled", msg)
        self.assertIn("allow_content_sharing", msg)

    def test_distillation_status_predicate(self):
        self.assertIn(
            "status IN ('pending', 'approved')",
            " ".join(L.DISTILLATION_ELIGIBILITY.predicates),
        )

    def test_workflow_python_quarantine_gate(self):
        self.assertIn(
            "hivemind_workflow_python_state",
            " ".join(L.WORKFLOW_PYTHON_ELIGIBILITY.predicates),
        )

    def test_optout_policy_decision_recorded(self):
        contract = json.loads(CONTRACT_JSON.read_text())
        self.assertIn("default-OFF", contract["eligibility"]["author_optout_policy"]["decision"])


class TestBoundedCodeDocumentPolicy(unittest.TestCase):
    def test_identity_key_shape(self):
        self.assertEqual(
            L.LEXICAL_DOCUMENT_IDENTITY,
            ("entity_type", "item_id", "representation_type", "chunk_index"),
        )

    def test_pilot_chunk_sizes(self):
        self.assertEqual(L.WORKFLOW_PYTHON_CHUNK_TARGET_TOKENS, 512)
        self.assertEqual(L.WORKFLOW_PYTHON_CHUNK_OVERLAP_TOKENS, 50)

    def test_no_silent_truncation_flag(self):
        contract = json.loads(CONTRACT_JSON.read_text())
        self.assertTrue(contract["bounded_workflow_code_policy"]["no_silent_truncation"])

    def test_collapse_to_one_identity(self):
        self.assertIn("exactly one", L.CHUNK_COLLAPSE_RULE.one_identity)

    def test_measured_python_sizes_recorded(self):
        contract = json.loads(CONTRACT_JSON.read_text())
        sizes = contract["bounded_workflow_code_policy"]["measured_sizes_0_3"]
        self.assertEqual(sizes["payload_python_max_chars"], 1415262)
        self.assertGreater(sizes["approx_chunks_at_512tok_max"], 70)


class TestRankingInputs(unittest.TestCase):
    def test_ts_rank_normalization_32(self):
        self.assertEqual(L.TS_RANK_NORMALIZATION, 32)

    def test_lexical_rank_uses_frozen_flag(self):
        self.assertEqual(L.LEXICAL_RANK_SQL, "ts_rank(tsv, tsq, 32)")

    def test_rrf_k_60(self):
        self.assertEqual(L.RRF_K, 60)

    def test_tie_break_deterministic(self):
        for needle in ("lexical_rank DESC", "created_at DESC", "item_id ASC"):
            self.assertIn(needle, L.TIE_BREAK)


class TestSnowflakeBoundary(unittest.TestCase):
    def test_snowflake_columns_cast_to_text(self):
        for col in L.SNOWFLAKE_TEXT_COLUMNS:
            self.assertIn("::text", col)

    def test_item_ids_are_text(self):
        self.assertTrue(L.ITEM_IDS_ARE_TEXT)


class TestGoldenFixtureFreeze(unittest.TestCase):
    def test_golden_freeze_recorded(self):
        contract = json.loads(CONTRACT_JSON.read_text())
        gf = contract["golden_fixture_freeze"]
        self.assertEqual(gf["n_cases"], 112)
        self.assertEqual(gf["n_judged"], 104)
        self.assertEqual(gf["n_no_hit"], 8)
        self.assertEqual(gf["version"], "golden/2026-07-28/v1")

    def test_blocking_gates_recorded(self):
        contract = json.loads(CONTRACT_JSON.read_text())
        gates = contract["golden_fixture_freeze"]["blocking_gates_for_1_10_1_11"]
        self.assertIn(">=0.95", gates["exact_identifier_recall_at_10"])
        self.assertIn(">=0.95", gates["workflow_code_exact_match_recall_at_10"])

    def test_all_required_categories_present(self):
        contract = json.loads(CONTRACT_JSON.read_text())
        cats = set(contract["golden_fixture_freeze"]["acceptance_categories"])
        required = {
            "exact_name", "workflow_code", "multi_term", "paraphrase", "settings",
            "named_author", "channel_scoped", "time_scoped", "cross_source",
            "best_is_distillation", "best_is_message", "best_is_resource",
            "long_resource_chunk", "workflow_only", "single_workflow",
            "spelling_variant", "no_hit", "workflow_python_evidence",
            "pending_status", "timeout_prone", "selective_filter", "code_fragment",
            "snowflake",
        }
        self.assertEqual(required & cats, required)


class TestPostHocLocked(unittest.TestCase):
    def test_contract_is_post_hoc_locked(self):
        contract = json.loads(CONTRACT_JSON.read_text())
        self.assertTrue(contract["post_hoc_locked"])


class TestPumpernickelAttribution(unittest.TestCase):
    def test_ported_and_runtime_dependency_none(self):
        contract = json.loads(CONTRACT_JSON.read_text())
        attr = contract["pumpernickel_attribution"]
        self.assertIn("websearch_to_tsquery('simple')", attr["ported"])
        self.assertIn("ts_rank(...,32)", attr["ported"])
        self.assertFalse(attr["runtime_dependency"])


if __name__ == "__main__":
    unittest.main()
