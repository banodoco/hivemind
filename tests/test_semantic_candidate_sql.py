"""Task 2.15 — semantic candidate SQL tests.

Two layers:

1. **Pure-Python parity tests** (always run, no PostgreSQL). They prove the
   invariants the SQL relies on WITHOUT a cluster, so CI exercises them even
   where pgvector is unavailable:

   * the frozen best-chunk tie-break the SQL mirrors (closest distance; tie ->
     prose before workflow_python; tie -> chunk_index ascending) agrees with
     :func:`executors.chunking.collapse_by_item` / :func:`select_best_chunk` on
     the exact rehearsal fixtures;
   * the result_kind -> entity_type mapping and the generic-vs-concrete
     resource-kind rule (``resource`` is generic; ``workflow``/``article``/...
     are concrete resource kinds);
   * Snowflake-as-text: a JS-unsafe Discord id is carried as a string and
     survives a JSON round trip;
   * the exact fixture cosine distances and their strict ordering (the
     deterministic inputs the SQL rehearsal scores).

2. **Discoverable SQL rehearsal test** (auto-skips when PostgreSQL binaries or
   pgvector are unavailable locally). It wraps
   :mod:`scripts.rehearse_semantic_candidates`, which applies schema/003 +
   schema/020–029 + the new schema/032 in an isolated throwaway cluster and
   proves every task-2.15
   completion signal against live SQL (active-contract filtering, cosine
   distance, best-chunk collapse, eligibility, filters, security posture,
   deterministic rerun).
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from executors import chunking  # noqa: E402
from executors import entity_identity as ei  # noqa: E402
from executors import workflow_representation as wr  # noqa: E402

import lexical_pg  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture vectors (single source of truth — mirrors the rehearsal)
# ---------------------------------------------------------------------------


def _cosine_distance(a: dict[int, float], b: dict[int, float]) -> float:
    """Cosine distance (1 - cosine similarity) over sparse {index: value} maps."""
    keys = set(a) | set(b)
    dot = sum(a.get(k, 0.0) * b.get(k, 0.0) for k in keys)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0.0 or nb == 0.0:
        return 1.0
    return 1.0 - dot / (na * nb)


Q = {0: 1.0}
V0 = {0: 1.0}
V02 = {0: 0.8, 1: 0.6}
V04 = {0: 0.6, 1: 0.8}
V10 = {1: 1.0}


class TestSemanticCandidatePurePython(unittest.TestCase):
    """Invariants the SQL relies on, proven offline."""

    # -- fixture distances + ordering ----------------------------------------
    def test_fixture_distances_and_ordering(self) -> None:
        # Q is a unit vector along component 0; every fixture is a unit vector,
        # so distance == 1 - component_0.
        self.assertAlmostEqual(_cosine_distance(Q, V0), 0.0, places=6)
        self.assertAlmostEqual(_cosine_distance(Q, V02), 0.2, places=6)
        self.assertAlmostEqual(_cosine_distance(Q, V04), 0.4, places=6)
        self.assertAlmostEqual(_cosine_distance(Q, V10), 1.0, places=6)
        # Strict ordering (the deterministic rank order the SQL must produce).
        ds = [_cosine_distance(Q, v) for v in (V0, V02, V04, V10)]
        self.assertEqual(ds, sorted(ds))

    # -- best-chunk tie-break parity with the frozen Python primitive --------
    def _chunk(self, entity: str, item: str, rep: str, idx: int) -> chunking.EmbeddingChunk:
        return chunking.EmbeddingChunk(
            entity_type=entity, item_id=item, representation_type=rep, chunk_index=idx,
            text="", chunk_hash="c", representation_hash="r", method="m",
            token_estimate=1, char_start=-1, char_end=-1, start_line=0, end_line=0,
        )

    def test_closest_chunk_across_representations_matches_frozen_primitive(self) -> None:
        # Resource 5001: prose0(0.4) vs workflow_python0(0.2, closest) vs wp1(1.0).
        # The best chunk is a DIFFERENT representation than the prose chunk.
        scored = [
            (self._chunk("resource", "5001", wr.REP_PROSE, 0), 1.0 - _cosine_distance(Q, V04)),
            (self._chunk("resource", "5001", wr.REP_WORKFLOW_PYTHON, 0), 1.0 - _cosine_distance(Q, V02)),
            (self._chunk("resource", "5001", wr.REP_WORKFLOW_PYTHON, 1), 1.0 - _cosine_distance(Q, V10)),
        ]
        best = chunking.select_best_chunk(scored)
        self.assertIsNotNone(best)
        assert best is not None  # for type checkers
        self.assertEqual(best.representation_type, wr.REP_WORKFLOW_PYTHON)
        self.assertEqual(best.chunk_index, 0)
        # collapse_by_item keeps exactly one row per item.
        collapsed = chunking.collapse_by_item(scored)
        self.assertEqual(list(collapsed), [("resource", "5001")])

    def test_exact_tie_picks_prose_then_chunk_index(self) -> None:
        # Resource 5002: THREE-way exact tie at ~0.2 (prose0 / prose1 / wp0).
        # Frozen tie-break: closest distance; tie -> prose before workflow_python;
        # tie -> chunk_index ascending. Winner = prose chunk 0.
        sim = 1.0 - _cosine_distance(Q, V02)
        scored = [
            (self._chunk("resource", "5002", wr.REP_PROSE, 0), sim),
            (self._chunk("resource", "5002", wr.REP_PROSE, 1), sim),
            (self._chunk("resource", "5002", wr.REP_WORKFLOW_PYTHON, 0), sim),
        ]
        best = chunking.select_best_chunk(scored)
        assert best is not None
        self.assertEqual(best.representation_type, wr.REP_PROSE)
        self.assertEqual(best.chunk_index, 0)
        # The SQL's DISTINCT ON order (distance, prose<python, chunk_index) picks
        # the same row: replicate the key and confirm it is the unique minimum.
        def sql_key(rep: str, idx: int, dist: float):
            return (dist, 0 if rep == wr.REP_PROSE else 1, idx)
        keys = [sql_key(c.representation_type, c.chunk_index, _cosine_distance(Q, V02))
                for c, _ in scored]
        self.assertEqual(keys.index(min(keys)), 0)  # prose0 is first = the winner

    # -- result_kind -> entity_type + generic/concrete resource rule ---------
    def test_kind_to_entity_mapping(self) -> None:
        self.assertEqual(ei.entity_type_for_result_kind("message"), "message")
        self.assertEqual(ei.entity_type_for_result_kind("distillation"), "distillation")
        # 'resource' (generic) and every concrete resource kind map to resource.
        for k in ("resource", "workflow", "article", "transcript", "repo"):
            self.assertEqual(ei.entity_type_for_result_kind(k), "resource", k)
        self.assertTrue(ei.result_kind_is_resource("workflow"))
        self.assertTrue(ei.result_kind_is_resource("resource"))
        self.assertFalse(ei.result_kind_is_resource("message"))
        self.assertFalse(ei.result_kind_is_resource("distillation"))

    def test_generic_vs_concrete_resource_rule(self) -> None:
        # The SQL rule: 'resource' is generic (matches all resource kinds); a
        # concrete requested kind narrows to resources whose source kind equals
        # it. This is the resource-arm predicate
        #   (v_res_generic or r.kind = any(v_res_concrete_kinds))
        # mirrored here.
        def matches(requested: list[str], source_kind: str) -> bool:
            has = len(requested) > 0
            generic = (not has) or "resource" in requested
            concrete = [k for k in requested if k not in ("message", "distillation", "resource")]
            want_res = generic or len(concrete) > 0
            return want_res and (generic or source_kind in concrete)

        # kinds=[] (all): every resource kind matches.
        for kind in ("workflow", "article", "transcript"):
            self.assertTrue(matches([], kind), kind)
        # kinds=['resource'] (generic): every resource kind matches.
        for kind in ("workflow", "article", "transcript"):
            self.assertTrue(matches(["resource"], kind), kind)
        # kinds=['workflow'] (concrete): only workflows match.
        self.assertTrue(matches(["workflow"], "workflow"))
        self.assertFalse(matches(["workflow"], "article"))
        # kinds=['workflow','article']: both match; message-source never matches.
        self.assertTrue(matches(["workflow", "article"], "workflow"))
        self.assertTrue(matches(["workflow", "article"], "article"))
        self.assertFalse(matches(["workflow", "article"], "blog_post"))

    # -- item_ids fail-closed resolution (defect-1 parity) -------------------
    def test_item_ids_resolution_fails_closed_when_ambiguous(self) -> None:
        # Mirror of the SQL resolution block in schema/032: when item_ids are
        # non-empty, they restrict to an entity ONLY when the kinds filter
        # resolves to EXACTLY one entity; otherwise the call FAILS CLOSED
        # (zero rows). 'fail_closed' here corresponds to v_items_entity := NULL
        # in the SQL, which makes every arm's predicate false.
        def resolve(kinds: list[str]) -> str | None:
            has = len(kinds) > 0
            want_msg = (not has) or "message" in kinds
            want_dist = (not has) or "distillation" in kinds
            generic = (not has) or "resource" in kinds
            concrete = [k for k in kinds if k not in ("message", "distillation", "resource")]
            want_res = generic or len(concrete) > 0
            if want_msg and not want_res and not want_dist:
                return "message"
            if want_res and not want_msg and not want_dist:
                return "resource"
            if want_dist and not want_msg and not want_res:
                return "distillation"
            return None  # ambiguous / bare -> fail closed

        # Exactly one entity -> item_ids restrict to it.
        self.assertEqual(resolve(["message"]), "message")
        self.assertEqual(resolve(["workflow"]), "resource")     # concrete resource kind
        self.assertEqual(resolve(["resource"]), "resource")     # generic resource kind
        self.assertEqual(resolve(["distillation"]), "distillation")
        # Bare (no kinds) -> ambiguous across ALL entities -> fail closed.
        self.assertIsNone(resolve([]))
        # Two entities -> ambiguous -> fail closed.
        self.assertIsNone(resolve(["message", "resource"]))
        self.assertIsNone(resolve(["message", "workflow"]))
        self.assertIsNone(resolve(["workflow", "distillation"]))
        # Two concrete resource kinds still resolve to the ONE resource entity.
        self.assertEqual(resolve(["workflow", "article"]), "resource")

    def test_snowflake_is_string_and_js_unsafe(self) -> None:
        snowflake = "9007199254740993"  # 2^53 + 1
        # It is above JS Number.MAX_SAFE_INTEGER (a JSON number would be unsafe).
        self.assertGreater(int(snowflake), 2 ** 53)
        # Hivemind stringifies it; the string survives a JSON round trip.
        self.assertTrue(ei.item_id_survives_json_roundtrip(snowflake))
        self.assertTrue(ei.is_discord_snowflake(snowflake))
        self.assertEqual(ei.stringify_item_id(int(snowflake)), snowflake)
        # Identity key keeps the snowflake as an exact string.
        key = ei.embedding_identity_key("message", snowflake, "prose", 0)
        self.assertEqual(key, ("message", snowflake, "prose", 0))


# ---------------------------------------------------------------------------
# Discoverable SQL rehearsal (skip if no local PostgreSQL / pgvector)
# ---------------------------------------------------------------------------


@unittest.skipUnless(lexical_pg.find_pgbins(), "PostgreSQL binaries not found")
class TestSemanticCandidateSQL(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from rehearse_semantic_candidates import EVIDENCE_PATH, rehearse
        try:
            cls.ev = rehearse(EVIDENCE_PATH)
        except RuntimeError as exc:
            msg = str(exc).lower()
            if "vector" in msg or "extension" in msg or "pgvector" in msg:
                raise unittest.SkipTest(f"pgvector unavailable locally: {exc}")
            raise

    def test_verdict_all_pass(self) -> None:
        self.assertTrue(self.ev["verdict"]["all_pass"], self.ev["verdict"])

    def test_active_contract_filter_excludes_draft_and_superseded(self) -> None:
        checks = self.ev["checks"]
        self.assertTrue(checks["draft_contract_vector_excluded"])
        self.assertTrue(checks["superseded_contract_vector_excluded"])
        self.assertTrue(checks["only_active_contract_ranks"])

    def test_best_chunk_collapse_and_tie_break(self) -> None:
        checks = self.ev["checks"]
        self.assertTrue(checks["multi_chunk_returns_once"])
        self.assertTrue(checks["closest_chunk_across_reps"])
        self.assertTrue(checks["snippet_matches_winning_chunk"])
        self.assertTrue(checks["tie_picks_prose_chunk0"])
        self.assertTrue(checks["tie_resolves_identically_repeated"])
        self.assertTrue(checks["no_entity_duplicates"])

    def test_eligibility_and_filters(self) -> None:
        checks = self.ev["checks"]
        for name in (
            "deleted_message_excluded", "rejected_distillation_excluded",
            "superseded_distillation_excluded", "missing_resource_excluded",
            "generic_resource_matches_all_resources", "concrete_workflow_matches_only_workflows",
            "concrete_article_matches_only_articles", "message_kind_filter",
            "exact_item_ids_snowflake", "snowflake_json_string_roundtrip",
        ):
            self.assertTrue(checks[name], name)

    def test_item_ids_fail_closed(self) -> None:
        # Defect-1: bare / ambiguous item_ids FAIL CLOSED (zero rows) and never
        # leak an unrelated entity. The exact-item-id (unambiguous) path still
        # filters exact text ids, including the JS-unsafe Snowflake.
        checks = self.ev["checks"]
        for name in (
            "bare_item_ids_fail_closed", "bare_item_ids_no_unrelated_entity",
            "ambiguous_item_ids_fail_closed", "ambiguous_item_ids_no_unrelated_entity",
            "exact_item_ids_simple",
        ):
            self.assertTrue(checks[name], name)
        ev = self.ev["evidence"]["item_ids_fail_closed"]
        self.assertEqual(ev["bare_item_ids_returned_count"], 0)
        self.assertEqual(ev["ambiguous_item_ids_returned_count"], 0)
        self.assertFalse(ev["bare_leaked_unrelated_entity_5001"])
        self.assertFalse(ev["ambiguous_leaked_unrelated_entity"])

    def test_unsafe_workflow_python_gated_out(self) -> None:
        # Defect-2: stale/unsafe workflow_python never ranks or leaks a snippet.
        checks = self.ev["checks"]
        for name in (
            "quarantined_workflow_ranks_only_via_prose",
            "unavailable_workflow_does_not_rank",
            "safe_workflow_python_still_wins",
            "unsafe_python_snippet_never_leaked",
            "canonical_accessor_5001_safe",
            "canonical_accessor_5004_not_safe",
            "canonical_accessor_5005_not_safe",
        ):
            self.assertTrue(checks[name], name)
        ev = self.ev["evidence"]["unsafe_workflow_python_gate"]
        # 5004 ranks only via prose; 5005 never ranks; no snippet leaked.
        self.assertTrue(ev["quarantined_5004_present"])
        self.assertEqual(ev["quarantined_5004_winning_representation"], "prose")
        self.assertFalse(ev["unavailable_5005_present"])
        self.assertEqual(ev["safe_5001_winning_representation"], "workflow_python")
        self.assertFalse(ev["stale_unsafe_snippet_leaked"])
        self.assertTrue(ev["canonical_accessor_5001_safe"])
        self.assertFalse(ev["canonical_accessor_5004_safe"])
        self.assertFalse(ev["canonical_accessor_5005_safe"])

    def test_ranking_and_limit(self) -> None:
        checks = self.ev["checks"]
        self.assertTrue(checks["ranks_dense"])
        self.assertTrue(checks["ordering_is_total_order"])
        self.assertTrue(checks["limit_bounded"])
        self.assertTrue(checks["limit_deterministic"])

    def test_security_posture(self) -> None:
        checks = self.ev["checks"]
        for name in (
            "is_stable", "is_security_definer", "search_path_pinned",
            "revoked_from_public", "granted_to_service_role",
            "not_granted_to_anon", "not_granted_to_authenticated",
            "lowpriv_cannot_call", "anon_cannot_call",
            "authenticated_cannot_call", "service_role_can_call",
        ):
            self.assertTrue(checks[name], name)
        # The only EXECUTE grantees are the owner (postgres) + service_role.
        self.assertEqual(
            set(self.ev["evidence"]["security"]["grantees"]), {"postgres", "service_role"}
        )


if __name__ == "__main__":
    unittest.main()
