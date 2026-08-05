"""Contract tests for ``scripts/rehearse_lexical_013.py``.

Test-first gate: this file is the FIRST repository edit in the schema/013
local-proof batch. It MUST be run while ``scripts/rehearse_lexical_013.py`` does
not yet exist, recording the exact red failing count; only then may the rehearsal
script be created.

The contract:

  * the rehearsal module EXISTS and IMPORTS;
  * it REUSES the schema/012 harness (``rehearse_lexical_012`` helpers/fixtures)
    instead of duplicating the whole harness;
  * it APPLIES 013 AFTER 012 (001..012, then 013);
  * it runs the rehearsal and the verdict is an all-pass covering: full canonical
    row byte parity 013 == 012 across representative + adversarial queries;
    cross-boundary negative parity; newest-matching-anchor byte parity;
    quarantine exclusion; the fragment/MV 1..8000 bound; ACL denial (anon /
    authenticated / public cannot select the MV nor execute the private
    candidates function) + service-role RPC works + proacl preserved; rollback to
    012 returns the same rows; double-apply idempotence; the live body satisfies
    the optimization contract (single-pass fragment_anchors anchor CTE present;
    the 012 correlated scalar anchor absent); and an HONEST LOCAL non-regression
    timing comparison with TWO checked cases (dense + sparse), described below;
  * the written verdict JSON is SECRET-SAFE: it contains NO queries, filter
    values, snippets, workflow source, SQL, stderr, credentials, or author /
    channel values — only opaque probe names, counts, booleans, timings, and
    schema object names.

HONEST NON-REGRESSION TIMING CONTRACT (the load-bearing strengthening). The old
contract only asserted that timing FIELDS were present and labelled local-only.
That is gameable: a regression that is 20x slower still "captures" timings. The
strengthened contract requires the rehearsal to PROVE non-regression on TWO
adversarial local shapes, both with warmup, INTERLEAVED body order (013/012
alternated so cache/state cannot favour one body), and medians:

  * DENSE / common — every workflow_python chunk of every matched item contains
    the needle (the worst case for any trigram-first design: selectivity ~100%).
    013 median must NOT exceed 1.25x the 012 median. This is the gate the prior
    single-pass DISTINCT-ON rewrite failed (it was ~20x slower).
  * SPARSE / selective — every matched item has exactly ONE matching chunk among
    many non-matching chunks, and that matching chunk is the OLDEST (forcing 012's
    per-item correlated item_id-index scan to walk every chunk of every matched
    item re-running normalize() as a non-indexed filter). 013 must be FASTER than
    012 (ratio < 1.0) AND the rehearsal must PROVE the actual anchor-selection
    plan (EXPLAIN of the real candidate function call under this corpus+needle)
    is served by the existing ``lexical_documents_python_chunk_trgm_idx`` GIN. A
    bare isolated LIKE-index servability probe with enable_seqscan=off does NOT
    satisfy this — the real plan must use the index.

Both gates are part of ``all_pass``. If either fails, the rehearsal must report
all_pass=False (it must NOT falsify a pass).

These are RED before the rehearsal module exists (file/import/structure fail, and
the cluster-gated run cannot import the module). A correct implementation flips
every test green.
"""

from __future__ import annotations

import importlib
import json
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys_path_added = str(REPO / "scripts")

import sys  # noqa: E402

sys.path.insert(0, sys_path_added)

from lexical_pg import find_pgbins  # noqa: E402

SCRIPT = REPO / "scripts" / "rehearse_lexical_013.py"
SCHEMA_013 = REPO / "schema" / "013_lexical_latency_phase3.sql"


def _import_module():
    try:
        return importlib.import_module("rehearse_lexical_013")
    except Exception:
        return None


# Concrete secret markers that must NEVER appear in the published verdict. These
# are queries, filter values, anchors/snippets, author/channel names, workflow
# titles/source, and python source from the seeded corpus + the function body.
SECRET_MARKERS = [
    # queries / needles
    "WanVideoSampler",
    "CogVideoX",
    "controlnet",
    "upscale model settings",
    "zzuniqueqxx",
    "ksampler",
    "wanvideosampler",
    # rehearsal timing-corpus needles (in-process only; never serialized, but
    # registered so any future leak into the verdict is caught).
    "benchfragneedlexyz",
    "sparsefragneedlexyz",
    # filter values (channels / authors)
    "wan_chatter",
    "QuintForms",
    "NoSuchAuthor_xyz",
    "NoSuchChannel_xyz",
    # anchors / snippets
    "ANCHOR_ZERO",
    "ANCHOR_ONE",
    "ANCHOR_TWO",
    "CB_CHUNK_ZERO",
    "CB_CHUNK_ONE",
    # workflow titles / source
    "WanVideo Image-to-Video",
    "AnchorParityWorkflow",
    "CrossBoundaryWorkflow",
    "Quarantined WanVideo",
    # python source from the seeded SAFE_PY_CHUNK
    "lora_weight=0.8",
    "BackboneSampler",
    "VAEEncode",
    # SQL / shell leakage markers
    "select ",
    "from public.hivemind",
    "create or replace function",
    "psql:",
    "stderr",
    "ON_ERROR_STOP",
    # credentials-shaped
    "sk-",
    "password",
    "postgres://",
]


# ===========================================================================
# Layer 1 — PURE / SHAPE (no DB). RED before the rehearsal module exists.
# ===========================================================================
class TestRehearsalModuleContract(unittest.TestCase):
    """Static + import contract. No cluster required."""

    def test_module_file_exists(self) -> None:
        self.assertTrue(SCRIPT.exists(), f"missing rehearsal script: {SCRIPT}")

    def test_module_importable(self) -> None:
        self.assertIsNotNone(_import_module(), "rehearse_lexical_013 must import")

    def test_reuses_012_harness_not_duplicating(self) -> None:
        m = _import_module()
        if m is None:
            self.fail("rehearse_lexical_013 not importable")
        src = SCRIPT.read_text()
        # Must lean on the 012 harness, not re-spawn a parallel one.
        self.assertIn("rehearse_lexical_012 as R12", src,
                      "must reuse rehearse_lexical_012 as R12")
        self.assertIn("R12.bootstrap_through_011", src,
                      "must reuse R12.bootstrap_through_011 rather than re-deriving bootstrap")
        self.assertIn("R12.seed_adversarial", src,
                      "must reuse R12.seed_adversarial rather than re-seeding adversarial fixtures")
        # The byte-parity capture is the 012 module's load-bearing helper.
        self.assertIn("R12.call_candidates_json", src,
                      "must reuse R12.call_candidates_json for full canonical-row capture")

    def test_applies_013_after_012(self) -> None:
        m = _import_module()
        if m is None:
            self.fail("rehearse_lexical_013 not importable")
        src = SCRIPT.read_text()
        self.assertIn("012_lexical_latency_phase2.sql", src,
                      "must apply schema/012 as the pre-013 baseline")
        self.assertIn("013_lexical_latency_phase3.sql", src,
                      "must apply schema/013 on top of 012")
        # The 012 apply must precede the 013 apply in the actual CODE, not the
        # module docstring (whose first line legitimately names schema/013). Strip
        # the leading module docstring (after the shebang) before comparing
        # positions so the heuristic reflects the real apply order.
        code = src
        start = code.find('"""')
        if start != -1:
            end = code.find('"""', start + 3)
            if end != -1:
                code = code[end + 3:]
        self.assertLess(code.index("012_lexical_latency_phase2.sql"),
                        code.index("013_lexical_latency_phase3.sql"),
                        "013 must be applied AFTER 012")

    def test_exposes_verdict_path_and_entrypoints(self) -> None:
        m = _import_module()
        if m is None:
            self.fail("rehearse_lexical_013 not importable")
        self.assertTrue(hasattr(m, "VERDICT_PATH"), "must expose VERDICT_PATH")
        self.assertTrue(callable(getattr(m, "rehearse", None)), "must expose rehearse()")
        self.assertTrue(callable(getattr(m, "main", None)), "must expose main()")

    # ---- benchmark-only changes (static proof of the contract) ----
    def test_timing_corpus_timestamps_are_monotonic_no_modulo(self) -> None:
        """_seed_timing_corpus must build created_at from a valid base timestamptz
        plus a monotonic chunk_index*interval expression — NEVER a modulo/wrapped
        calendar date."""
        m = _import_module()
        if m is None:
            self.fail("rehearse_lexical_013 not importable")
        src = SCRIPT.read_text()
        # The helper source (from its def to the next top-level def).
        start = src.index("def _seed_timing_corpus")
        end = src.index("\ndef ", start + 1)
        body = src[start:end]
        # No modulo / wrapped day-of-month formatting.
        self.assertNotRegex(body, r"%\s*28|%\s*30|%\s*31|\bmod\b",
                            "timestamps must not use modulo day wrapping")
        self.assertNotIn("2026-02-{", body,
                         "timestamps must not be Python-formatted wrapped calendar dates")
        # A valid base timestamptz plus a chunk_index*interval expression is used.
        self.assertIn("interval '1 second'", body,
                      "timestamps must use chunk_index * interval '1 second'")
        self.assertIn("TIMING_BASE_TS", body,
                      "timestamps must reference the base timestamptz constant")
        # The constant itself is a valid timestamptz literal.
        self.assertTrue(
            m.TIMING_BASE_TS.lower().startswith("timestamp '"),
            "TIMING_BASE_TS must be a valid timestamptz literal")

    def test_analyze_is_checked(self) -> None:
        """The rehearsal must run checked ANALYZE on both surfaces after the
        timing-corpus insert + MV refresh, and record analyzed in the evidence."""
        import inspect
        m = _import_module()
        if m is None:
            self.fail("rehearse_lexical_013 not importable")
        self.assertTrue(callable(getattr(m, "_analyze_checked", None)),
                        "must expose a checked ANALYZE helper")
        helper = inspect.getsource(m._analyze_checked)
        self.assertIn("public.lexical_documents", helper,
                      "must run checked ANALYZE on lexical_documents")
        self.assertIn("public.lexical_workflow_python_search", helper,
                      "must run checked ANALYZE on the MV")
        self.assertIn("ANALYZE", helper,
                      "must issue an ANALYZE statement")
        src = SCRIPT.read_text()
        # _seed_timing_corpus returns an analyzed flag consumed by the evidence.
        start = src.index("def _seed_timing_corpus")
        end = src.index("\ndef ", start + 1)
        self.assertIn("analyzed", src[start:end],
                      "timing corpus must record analyzed in its return value")

    def test_symmetric_function_only_body_switching(self) -> None:
        """012 and 013 application must be symmetric during timing: a function-
        only 012 helper, and _median_interleaved uses it, warms both bodies,
        interleaves, and never refreshes the MV per switch."""
        m = _import_module()
        if m is None:
            self.fail("rehearse_lexical_013 not importable")
        self.assertTrue(callable(getattr(m, "apply_012_function_only", None)),
                        "must expose apply_012_function_only")
        self.assertTrue(callable(getattr(m, "_extract_function_statement_012", None)),
                        "must expose the 012 function-statement extractor")
        # The extractor must isolate ONLY the candidate function (not the MV /
        # index / refresh / revoke DDL from 012).
        sql = m._extract_function_statement_012()
        self.assertIn("create or replace function public.hivemind_lexical_candidates", sql,
                      "extractor must return the candidate function statement")
        self.assertFalse(
            re.search(r"create\s+materialized\s+view|create\s+index|refresh\s+materialized",
                      sql, re.I),
            "extractor must NOT carry 012 MV/index/refresh DDL")
        self.assertTrue(sql.rstrip().endswith("$$;"),
                        "extractor must return a single self-contained statement")

        src = SCRIPT.read_text()
        start = src.index("def _median_interleaved")
        end = src.index("\ndef ", start + 1)
        body = src[start:end]
        self.assertIn("apply_012_function_only(cluster)", body,
                      "_median_interleaved must switch 012 via the function-only helper")
        self.assertIn("apply_013(cluster)", body,
                       "_median_interleaved must switch 013 (already function-only)")
        self.assertNotIn("refresh_mv(cluster)", body,
                         "_median_interleaved must NOT refresh the MV per body switch")

    def test_real_plan_rejects_top_level_function_scan(self) -> None:
        """An ordinary EXPLAIN of the PL/pgSQL function only returns a Function
        Scan; the rehearsal must NOT accept that as plan evidence."""
        m = _import_module()
        if m is None:
            self.fail("rehearse_lexical_013 not importable")
        self.assertTrue(callable(getattr(m, "_real_plan_uses_trgm", None)))
        src = SCRIPT.read_text()
        start = src.index("def _real_plan_uses_trgm")
        end = src.index("\ndef ", start + 1)
        body = src[start:end]
        self.assertIn("Function Scan", body,
                      "_real_plan_uses_trgm must explicitly reject a top-level Function Scan")
        # Must NOT EXPLAIN the wrapper function call.
        self.assertNotIn("EXPLAIN (COSTS OFF) select * from public.hivemind_lexical_candidates",
                         body,
                         "must not EXPLAIN the wrapper function (yields only Function Scan)")

    def test_real_plan_uses_no_forced_or_bare_like_probe(self) -> None:
        """The exact-inner-plan proof must use neither enable_seqscan=off nor a
        bare-LIKE substitute."""
        import inspect
        m = _import_module()
        if m is None:
            self.fail("rehearse_lexical_013 not importable")
        src = inspect.getsource(m._real_plan_uses_trgm)
        # Strip the docstring so prose mentions of the forbidden knob do not
        # masquerade as executable usage.
        first = src.find('"""')
        if first != -1:
            second = src.find('"""', first + 3)
            if second != -1:
                src = src[second + 3:]
        self.assertNotRegex(src, r"enable_seqscan\s*=\s*('?'?)?off|set\s+enable_seqscan",
                            "must NOT disable seqscan to force an index")
        self.assertIn("bare_like_substitute_used", src,
                      "must report whether a bare-LIKE substitute was used")
        # The structured result shape.
        import inspect
        src_text = inspect.getsource(m._real_plan_uses_trgm)
        for key in ("method", "uses_trgm_index", "reason"):
            self.assertIn(key, src_text,
                          f"_real_plan_uses_trgm must expose structured evidence key {key}")
        self.assertIn("extracted_exact_inner_statement", src_text,
                      "method must be extracted_exact_inner_statement")
        self.assertIn("missing_inner_plan_markers", src_text,
                      "unmarked body must yield reason missing_inner_plan_markers")

    def test_real_plan_corpus_has_natural_planner_scale(self) -> None:
        """The exact unforced plan must run at a scale where the selective
        trigram index is naturally preferable to a tiny-table sequential scan."""
        import inspect
        m = _import_module()
        if m is None:
            self.fail("rehearse_lexical_013 not importable")
        self.assertGreaterEqual(
            getattr(m, "TIMING_INDEX_DECOY_ROWS", 0), 100_000)
        self.assertTrue(callable(getattr(m, "_seed_index_scale_decoys", None)))
        helper = inspect.getsource(m._seed_index_scale_decoys).lower()
        self.assertIn("generate_series", helper)
        self.assertNotIn("enable_seqscan", helper)

    def test_security_exposes_unsuffixed_no_execute_aliases(self) -> None:
        """security_proof must expose unsuffixed anon/authenticated
        no_execute_candidates aliases from the proven _after_012 booleans."""
        m = _import_module()
        if m is None:
            self.fail("rehearse_lexical_013 not importable")
        src = SCRIPT.read_text()
        self.assertIn("anon_no_execute_candidates", src,
                      "must expose unsuffixed anon_no_execute_candidates alias")
        self.assertIn("authenticated_no_execute_candidates", src,
                      "must expose unsuffixed authenticated_no_execute_candidates alias")
        self.assertIn("anon_no_execute_candidates_after_012", src,
                      "must keep the proven _after_012 boolean")


# ===========================================================================
# Layer 2 — CLUSTER-gated (run the rehearsal + assert verdict + secret-safety).
# ===========================================================================
@unittest.skipUnless(find_pgbins(), "PostgreSQL binaries (initdb/pg_ctl/psql) not found")
class TestRehearsalRun(unittest.TestCase):
    """Run rehearse_lexical_013.rehearse() once and assert the full verdict."""

    @classmethod
    def setUpClass(cls) -> None:
        m = _import_module()
        if m is None:
            raise AssertionError("rehearse_lexical_013 not importable")
        cls.mod = m
        cls.verdict = m.rehearse()

    def test_rehearse_all_pass(self) -> None:
        self.assertTrue(self.verdict.get("applied_ok"), "schema/013 must apply cleanly after 012")
        self.assertTrue(self.verdict.get("all_pass"),
                        f"rehearsal all_pass must be True; keys={sorted(self.verdict)}")

    def test_full_row_parity_and_adversarial_proofs(self) -> None:
        v = self.verdict
        # Full canonical-row byte parity 013 == 012 across all parity queries.
        self.assertTrue(v.get("parity_all"), "full-row parity 013 == 012 must hold for all queries")
        # No single parity query diffed.
        diffs = [n for n, p in v.get("parity", {}).items() if not p.get("identical")]
        self.assertEqual(diffs, [], f"013 != 012 full-row streams for: {diffs}")
        # Cross-boundary negative parity.
        self.assertTrue(v.get("cross_ok"), "cross-boundary needle must match in NEITHER 012 nor 013")
        # Newest-matching-anchor byte parity.
        self.assertTrue(v.get("anchor_ok"), "newest-matching-anchor selection must equal 012")
        # Quarantine exclusion.
        self.assertTrue(v.get("security", {}).get("quarantined_zero_candidates"),
                        "quarantined workflow must contribute zero candidates")
        # Fragment/MV 1..8000 bound.
        self.assertTrue(v.get("fragment_bound_ok"),
                        "an out-of-range (>8000) chunk must be excluded from the fragment surface")

    def test_security_acl_proofs(self) -> None:
        sec = self.verdict.get("security", {})
        for key in ("anon_cannot_select_mv", "authenticated_cannot_select_mv",
                    "public_cannot_select_mv", "anon_select_errors",
                    "authenticated_select_errors", "anon_no_execute_candidates",
                    "authenticated_no_execute_candidates",
                    "service_role_rpc_ok"):
            self.assertTrue(sec.get(key), f"security proof {key} must be True")
        self.assertGreater(sec.get("service_role_rpc_workflow_results", 0), 0,
                           "service-role RPC must return workflow results")

    def test_rollback_and_idempotence_and_grants(self) -> None:
        v = self.verdict
        self.assertTrue(v.get("rollback_ok"), "rollback to 012 must restore the same rows")
        self.assertTrue(v.get("rollback_streams_equal"),
                        "012-on-rollback stream must equal the pre-rollback 013 stream")
        self.assertTrue(v.get("idempotent_ok"), "applying 013 twice must be idempotent + correct")
        self.assertTrue(v.get("grants_preserved"),
                        "CREATE OR REPLACE must preserve proacl (013 == 012)")

    def test_hot_path_body_shape(self) -> None:
        """The live body keeps the SPELLING-AGNOSTIC optimization contract:
        adaptive dense + sparse path markers, a MATERIALIZED sparse set, a
        correlated/LATERAL bounded dense lookup, exact predicates, newest-anchor
        selection, and NO 012 correlated scalar anchor. (The real-plan trigram
        proof is asserted under the sparse timing case below — a forced bare
        probe is NOT accepted here.)"""
        v = self.verdict
        plan = v.get("hot_path_plan", {})
        self.assertTrue(plan.get("has_dense_path_marker"),
                        "the live body must carry the adaptive dense-path marker")
        self.assertTrue(plan.get("has_sparse_path_marker"),
                        "the live body must carry the adaptive sparse-path marker")
        self.assertTrue(plan.get("has_materialized_sparse_set"),
                        "the sparse_matches set in the sparse path must be MATERIALIZED")
        self.assertTrue(plan.get("has_correlated_or_lateral_dense"),
                        "the dense lookup must be correlated/LATERAL bounded")
        self.assertTrue(plan.get("has_newest_anchor_selection"),
                        "newest-anchor selection (created_at desc) must survive")
        self.assertTrue(plan.get("has_exact_predicates"),
                        "the exact sparse-path predicates must be present")
        self.assertFalse(plan.get("has_correlated_scalar_anchor"),
                         "the hot path must NOT contain 012's correlated scalar anchor")
        # The rehearsal must NOT rely on a forced bare-LIKE probe as the hot-path
        # proof; it must EXPLAIN the extracted exact inner statement. The forced
        # probe may be present only as a non-gating informational field.
        self.assertIsNone(plan.get("bare_like_probe_is_gate"),
                          "a forced bare-LIKE probe must never be the gating hot-path proof")

    def test_local_timing_honest_non_regression(self) -> None:
        """Both adversarial local timing cases are captured with warmup,
        interleaved body order, and medians, run against ANALYZEd statistics,
        with SYMMETRIC function-only body switching (no MV refresh per switch),
        and BOTH non-regression gates pass."""
        timing = self.verdict.get("local_timing", {})
        self.assertTrue(timing.get("local_only_label"),
                        "timing must be explicitly labelled local-only")
        self.assertFalse(timing.get("claims_production_gate", False),
                         "must NOT claim the 750ms production gate passes")
        self.assertTrue(timing.get("interleaved"),
                        "timing must interleave 013/012 bodies (no cache-favouring order)")

        for case in ("dense", "sparse"):
            c = timing.get(case, {})
            # ANALYZE was run checked before timing.
            self.assertTrue(c.get("analyzed"), f"{case}: ANALYZE must be checked before timing")
            # Body switching is symmetric + function-only; no MV refresh per switch.
            self.assertEqual(c.get("body_switch"), "function_only",
                             f"{case}: body switching must be function-only (symmetric)")
            self.assertFalse(c.get("mv_refresh_per_switch", True),
                             f"{case}: must NOT refresh the MV per body switch")

        dense = timing.get("dense", {})
        self.assertIn(dense.get("matched_chunks_per_item"), ("all", "every_chunk"),
                      "dense case must be the every-chunk-matches shape")
        self.assertIsNotNone(dense.get("median_ms_012"), "dense: 012 median missing")
        self.assertIsNotNone(dense.get("median_ms_013"), "dense: 013 median missing")
        self.assertLessEqual(dense.get("ratio_013_over_012", 1e9), 1.25,
                             f"dense: 013 must be <=1.25x 012 (got "
                             f"{dense.get('ratio_013_over_012')})")
        self.assertTrue(dense.get("gate_pass"), "dense non-regression gate must pass")

        sparse = timing.get("sparse", {})
        self.assertEqual(sparse.get("matched_chunks_per_item"), 1,
                         "sparse case must have exactly one matching chunk per item")
        self.assertEqual(sparse.get("matching_chunk_position"), "oldest",
                         "sparse matching chunk must be the OLDEST (forces 012 to walk all chunks)")
        self.assertIsNotNone(sparse.get("median_ms_012"), "sparse: 012 median missing")
        self.assertIsNotNone(sparse.get("median_ms_013"), "sparse: 013 median missing")
        self.assertLess(sparse.get("ratio_013_over_012", 1e9), 1.0,
                        f"sparse: 013 must be FASTER than 012 (got "
                        f"{sparse.get('ratio_013_over_012')})")

        # Structured exact-inner-plan evidence (NOT a top-level Function Scan and
        # NOT a forced/bare-LIKE probe). The trigram GIN must appear in the
        # EXPLAIN of the extracted exact sparse-match inner statement.
        ev = sparse.get("trgm_evidence", {})
        self.assertEqual(ev.get("method"), "extracted_exact_inner_statement",
                         "sparse trigram proof must use the extracted exact inner statement")
        self.assertIn(ev.get("reason"), (
            "trgm_index_in_inner_plan", "trgm_index_absent_from_inner_plan",
            "missing_inner_plan_markers", "empty_inner_statement",
            "explain_failed", "top_level_function_scan"),
            "sparse trigram evidence must carry a secret-safe reason")
        self.assertFalse(ev.get("bare_like_substitute_used", True),
                         "sparse trigram proof must NEVER use a bare-LIKE substitute")
        self.assertNotEqual(ev.get("reason"), "top_level_function_scan",
                            "a top-level Function Scan must NEVER be accepted as the plan proof")
        self.assertTrue(sparse.get("trgm_index_in_real_plan"),
                        "sparse: the extracted exact inner-statement plan must use "
                        "lexical_documents_python_chunk_trgm_idx (not a forced probe)")
        self.assertTrue(sparse.get("gate_pass"), "sparse non-regression gate must pass")

    def test_verdict_secret_safe(self) -> None:
        path = self.mod.VERDICT_PATH
        self.assertTrue(Path(path).exists(), f"verdict JSON must be written to {path}")
        text = Path(path).read_text(encoding="utf-8")
        leaked = [m for m in SECRET_MARKERS if m.lower() in text.lower()]
        self.assertEqual(leaked, [], f"verdict JSON leaked secret markers: {leaked}")
        # The parsed JSON must be a dict (never a bare error / traceback dump).
        parsed = json.loads(text)
        self.assertIsInstance(parsed, dict)


if __name__ == "__main__":
    unittest.main(verbosity=2)
