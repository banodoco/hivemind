"""Tests for the phase-3 lexical latency fix (schema/013, task 1.10/1.11 phase-3).

Two layers, mirroring test_lexical_security_hardening / test_lexical_sql:

1. PURE / SHAPE (no DB): schema/013 exists, is ADDITIVE (replaces ONLY the
   candidate function — no new MV/index/grant/revoke/DDL on any other object),
   preserves schema/012's exact signature + result columns + security posture
   (anon/authenticated/public revoked, service_role untouched), and carries a
   rollback command. It also encodes a SPELLING-AGNOSTIC OPTIMIZATION contract:
   the hot path is an ADAPTIVE dense/sparse design (explicit path markers), the
   sparse matching set is MATERIALIZED and trigram-indexed (its exact inner
   SELECT delimited by begin/end markers so it can be EXPLAINed directly), the
   dense lookup is a correlated/LATERAL bounded form, exact predicates are kept,
   and newest-anchor selection survives — but NO particular ``DISTINCT ON`` or
   ``array_agg`` spelling is mandated. It is NOT the schema/012 per-item
   correlated scalar subquery that re-runs ``normalize(chunk_text)`` as a
   non-indexed filter over every chunk of every matched item.

2. CLUSTER-gated (skip unless PostgreSQL binaries present): apply 001..012 then
   013 on a throwaway cluster and prove, vs a 012 rollback baseline:
     * FULL canonical-row byte parity (entity, item, representation,
       matched_snippet/anchor, lexical_rank, lexical_source, created_at, ORDER,
       global limit) across representative + adversarial + diagnosed-slow-shape
       queries — not just item-id sets;
     * cross-chunk false-positive protection (a needle that exists only across a
       chunk boundary matches in NEITHER 012 nor 013);
     * newest-MATCHING-chunk anchor byte parity (a multi-chunk item where only a
       later chunk matches selects that later chunk's anchor in both);
     * safe-workflow gate + quarantine exclusion (a quarantined workflow never
       ranks even when its chunk would match);
     * 1..8000 chunk-length bound exclusion (an out-of-range chunk never anchors
       nor matches the fragment arm);
     * direct channel/author predicate semantics + empty-resolution behavior
       (an unresolved name resolves to zero messages, never all);
     * ACL: anon/authenticated/public cannot SELECT the MV nor EXECUTE the
       candidates function; service_role RPC still works; proacl unchanged;
     * the live function body satisfies the optimization contract (single-pass
       anchor CTE present; the 012 correlated scalar anchor subquery absent).

These are RED before schema/013 exists / is implemented: the file-existence and
optimization-contract assertions fail (no 013 file; the live body is still
012's per-item correlated anchor). The preservation/parity assertions are
regression guards and pass under a correct 013.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import rehearse_lexical_candidate as R  # noqa: E402
import rehearse_lexical_010 as R10  # noqa: E402
import rehearse_lexical_012 as R12  # noqa: E402
import rehearse_lexical_013 as R13  # noqa: E402
from lexical_pg import find_pgbins  # noqa: E402

SCHEMA_012 = REPO / "schema" / "012_lexical_latency_phase2.sql"
SCHEMA_013 = REPO / "schema" / "013_lexical_latency_phase3.sql"

# The exact schema/012 candidate-function signature (must be preserved verbatim).
SIG = (
    "public.hivemind_lexical_candidates(\n"
    "  p_query           text,\n"
    "  p_candidate_limit int      default 100,\n"
    "  p_kinds           text[]   default '{}',\n"
    "  p_sources         text[]   default '{}',\n"
    "  p_item_ids        text[]   default '{}',\n"
    "  p_since           timestamptz default null,\n"
    "  p_channels        text[]   default '{}',\n"
    "  p_authors         text[]   default '{}',\n"
    "  p_author_optout   boolean  default false,\n"
    "  p_bots_excluded   boolean  default false\n"
    ")"
)

# schema/012's per-item correlated matched_anchor scalar subquery (the phase-3
# optimization target). Present in 012; must be ABSENT in 013.
CORRELATED_ANCHOR_RE = re.compile(
    r"\(\s*select\s+ld\.matched_anchor.*?ld\.item_id\s*=\s*mv\.item_id.*?"
    r"order\s+by\s+ld\.created_at\s+desc.*?limit\s+1\s*\)\s+as\s+matched_snippet",
    re.IGNORECASE | re.DOTALL,
)


# ===========================================================================
# Layer 1 — PURE / SHAPE (no DB)
# ===========================================================================
class TestMigrationShape(unittest.TestCase):
    """Static assertions over schema/013.sql text. No cluster required."""

    def setUp(self) -> None:
        self.assertTrue(SCHEMA_013.exists(), f"missing migration: {SCHEMA_013}")
        self.sql = SCHEMA_013.read_text()
        # Code = SQL with full-line `--` comments and the `comment on function`
        # docstring statement removed. The shape/additivity/optimization
        # contracts are about the actual DDL and function body, not the header
        # commentary or the self-describing COMMENT text (which legitimately
        # mentions the old 012 pattern, the rollback command, and the words
        # "grant"/"revoke"/"create or replace function").
        no_comments = "\n".join(
            line for line in self.sql.splitlines()
            if not line.strip().startswith("--")
        )
        self.code = re.sub(
            r"comment\s+on\s+function\b.*$", "", no_comments, flags=re.IGNORECASE | re.DOTALL
        )

    # ---- existence + additivity ----
    def test_file_exists(self) -> None:
        self.assertTrue(SCHEMA_013.exists())

    def test_replaces_only_candidate_function(self) -> None:
        """013 is additive: the ONLY DDL is one CREATE OR REPLACE FUNCTION of
        the candidate function. No new MV, index, table, grant, revoke, or
        drop touches any object."""
        self.assertEqual(
            len(re.findall(r"create\s+or\s+replace\s+function", self.code, re.I)),
            1,
            "013 must contain exactly one CREATE OR REPLACE FUNCTION",
        )
        self.assertIn("hivemind_lexical_candidates", self.code)
        # No DDL on any other object kind, and no privilege mutation at all.
        for forbidden in [
            r"create\s+materialized\s+view",
            r"create\s+unique\s+index",
            r"create\s+index",
            r"refresh\s+materialized\s+view",
            r"create\s+table",
            r"alter\s+materialized\s+view",
            r"drop\s+",
        ]:
            self.assertNotRegex(
                self.code, forbidden,
                f"013 must be additive — forbidden DDL found: {forbidden}",
            )
        self.assertNotRegex(self.code, r"\bgrant\b", "013 must add no grants")
        self.assertNotRegex(self.code, r"\brevoke\b", "013 must change no ACLs")

    def test_preserves_signature_and_result_columns(self) -> None:
        self.assertIn(SIG, self.code)
        for col in [
            "entity_type        text",
            "item_id            text",
            "representation_type text",
            "matched_snippet    text",
            "lexical_rank       real",
            "lexical_source     text",
            "created_at         timestamptz",
        ]:
            self.assertIn(col, self.code, f"result column block missing: {col}")
        self.assertIn("language plpgsql", self.code)
        self.assertIn("stable", self.code)
        # PL/pgSQL functions are SECURITY INVOKER by default (no explicit
        # keyword). The meaningful check is that 013 does NOT declare SECURITY
        # DEFINER (only the schema/009 RPC is DEFINER).
        self.assertNotRegex(self.code, r"security\s+definer", re.I)

    def test_has_rollback_command(self) -> None:
        self.assertRegex(
            self.sql,
            re.compile(r"rollback.*re-apply\s+schema/012|re-apply\s+schema/012", re.I),
        )

    # ---- OPTIMIZATION CONTRACT (RED before 013 implemented) ----
    # The contract is SPELLING-AGNOSTIC: it does NOT mandate any particular
    # ``DISTINCT ON`` or ``array_agg`` form (either is a valid newest-anchor
    # selection). Instead it requires the STRUCTURAL shape of an adaptive,
    # trigram-indexed hot path, expressed through explicit marker comments the
    # live body MUST carry so the rehearsal can EXPLAIN the exact sparse-match
    # inner statement (an ordinary EXPLAIN of the PL/pgSQL function only yields
    # a Function Scan and is invalid).
    def test_optimization_adaptive_path_markers_present(self) -> None:
        """schema/013 must carry explicit adaptive dense AND sparse path markers
        in the function body (RED against the current unmarked 013)."""
        self.assertIn(R13.DENSE_PATH_MARKER, self.sql,
                      "013 body must carry the adaptive dense-path marker")
        self.assertIn(R13.SPARSE_PATH_MARKER, self.sql,
                      "013 body must carry the adaptive sparse-path marker")

    def test_optimization_sparse_match_inner_markers_present(self) -> None:
        """The exact sparse-match inner SELECT must be delimited by explicit
        begin/end markers so it can be extracted and EXPLAINed directly."""
        self.assertIn(R13.SPARSE_MATCH_BEGIN, self.sql,
                      "013 body must carry the sparse-match begin marker")
        self.assertIn(R13.SPARSE_MATCH_END, self.sql,
                      "013 body must carry the sparse-match end marker")

    def test_optimization_has_materialized_sparse_set(self) -> None:
        """The sparse path's specifically named matching set must be
        MATERIALIZED (the unrelated safe_wf CTE does not qualify)."""
        sparse = self.sql.split(R13.SPARSE_PATH_MARKER, 1)[-1]
        if R13.DENSE_PATH_MARKER in sparse:
            sparse = sparse.split(R13.DENSE_PATH_MARKER, 1)[0]
        self.assertRegex(
            sparse, r"\bsparse_matches\s+as\s+materialized\b",
            "013 sparse path must declare sparse_matches AS MATERIALIZED")

    def test_optimization_has_correlated_or_lateral_dense_lookup(self) -> None:
        """The dense path bounds the per-item anchor lookup via a correlated /
        LATERAL subquery (never an unbounded correlated walk over every chunk)."""
        dense = self.sql.split(R13.DENSE_PATH_MARKER, 1)[-1]
        if R13.SPARSE_PATH_MARKER in dense:
            dense = dense.split(R13.SPARSE_PATH_MARKER, 1)[0]
        self.assertRegex(dense, r"\blateral\b",
                         "013 dense path must use a bounded LATERAL lookup")

    def test_optimization_newest_anchor_selection(self) -> None:
        """Newest-anchor selection survives (order by created_at desc), in either
        a DISTINCT ON or an array_agg spelling."""
        self.assertRegex(self.code, r"order\s+by.*created_at\s+desc",
                         "013 must select the newest-matching anchor")

    def test_optimization_exact_predicates(self) -> None:
        """The exact predicates that make the sparse path trigram-indexed must be
        present (token forms only; never filter values)."""
        for tok in (
            "representation_type = 'workflow_python'",
            "quarantine_state = 'safe'",
            "char_length(ld.chunk_text) between 1 and 8000",
        ):
            self.assertIn(tok, self.code, f"013 must keep the exact predicate: {tok}")

    def test_optimization_no_correlated_scalar_anchor(self) -> None:
        """The 012 per-item correlated matched_anchor scalar subquery must be
        gone from 013's fragment arm."""
        self.assertNotRegex(
            self.code, CORRELATED_ANCHOR_RE,
            "013 must NOT keep 012's per-item correlated matched_anchor subquery",
        )


# ===========================================================================
# Layer 2 — CLUSTER-gated (byte parity / security / optimization body)
# ===========================================================================
@unittest.skipUnless(find_pgbins(), "PostgreSQL binaries (initdb/pg_ctl/psql) not found")
class TestPhase3Cluster(unittest.TestCase):
    """Apply 001..012, then 013, on a throwaway cluster and prove parity +
    security + the optimization body, vs a 012 rollback baseline."""

    @classmethod
    def setUpClass(cls) -> None:
        import lexical_pg as LP
        cls.cluster = LP.LocalCluster.start()
        try:
            c = cls.cluster
            R.reset_schema(c)
            R12.bootstrap_through_011(c)
            cls.counts = R.seed(c, n_messages=8000)
            cls.extra = R10.seed_extra(c)
            cls.adv = R12.seed_adversarial(c)
            R12.apply_migration(c, "012_lexical_latency_phase2.sql")
            R12.refresh_mv(c)
            # Apply 013 if it exists + is implementable; record the live body.
            cls.applied_013 = False
            if SCHEMA_013.exists():
                try:
                    R12.apply_migration(c, "013_lexical_latency_phase3.sql")
                    cls.applied_013 = True
                except Exception:
                    cls.applied_013 = False
            cls.fn_body = cls._live_body(c)
        except Exception:
            cls.cluster.tear_down()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.cluster.tear_down()

    @staticmethod
    def _live_body(c) -> str:
        rc, out = c.psql(
            "select prosrc from pg_proc p join pg_namespace n on n.oid=p.pronamespace "
            "where n.nspname='public' and p.proname='hivemind_lexical_candidates'"
        )
        return out if rc == 0 else ""

    # ---- optimization body (RED before 013 implemented: body is 012's) ----
    def test_013_applied(self) -> None:
        self.assertTrue(self.applied_013, "schema/013 did not apply cleanly after 012")

    def test_body_has_adaptive_path_markers(self) -> None:
        self.assertIn(R13.DENSE_PATH_MARKER, self.fn_body,
                      "live body must carry the adaptive dense-path marker")
        self.assertIn(R13.SPARSE_PATH_MARKER, self.fn_body,
                      "live body must carry the adaptive sparse-path marker")

    def test_body_has_sparse_match_inner_markers(self) -> None:
        self.assertIn(R13.SPARSE_MATCH_BEGIN, self.fn_body,
                      "live body must carry the sparse-match begin marker")
        self.assertIn(R13.SPARSE_MATCH_END, self.fn_body,
                      "live body must carry the sparse-match end marker")

    def test_body_has_materialized_sparse_set(self) -> None:
        sparse = self.fn_body.split(R13.SPARSE_PATH_MARKER, 1)[-1]
        self.assertRegex(
            sparse, r"\bsparse_matches\s+as\s+materialized\b",
            "live body must declare sparse_matches AS MATERIALIZED")

    def test_body_drops_correlated_scalar_anchor(self) -> None:
        self.assertNotRegex(self.fn_body, CORRELATED_ANCHOR_RE,
                            "live body must NOT keep 012's correlated matched_anchor subquery")

    # ---- FULL-row byte parity vs 012 ----
    def _capture(self) -> tuple[dict, dict]:
        c = self.cluster
        queries = [
            {"name": "wan_workflow", "query": "WanVideoSampler", "kinds": ["workflow"]},
            {"name": "cog_workflow", "query": "CogVideoX", "kinds": ["workflow"]},
            {"name": "wan_all", "query": "WanVideoSampler"},
            {"name": "cog_all", "query": "CogVideoX"},
            {"name": "controlnet", "query": "controlnet"},
            {"name": "multi_term", "query": "upscale model settings"},
            {"name": "distillation", "query": "reduce motion strength"},
            {"name": "cog_channel", "query": "CogVideoX", "channels": ["wan_chatter"]},
            {"name": "cog_author", "query": "CogVideoX", "authors": ["QuintForms"]},
            {"name": "wan_single", "query": "WanVideoSampler", "kinds": ["workflow"], "item_ids": ["20"]},
            {"name": "anchor_fix", "query": R12.ANCHOR_NEEDLE, "kinds": ["workflow"]},
            {"name": "no_hit", "query": "zzzznotarealtokenxyz"},
        ]
        s13 = {q["name"]: R12.call_candidates_json(
                   c, q["query"], kinds=q.get("kinds"), channels=q.get("channels"),
                   authors=q.get("authors"), item_ids=q.get("item_ids")) for q in queries}
        R12.apply_migration(c, "012_lexical_latency_phase2.sql")  # rollback body
        R12.refresh_mv(c)
        s12 = {q["name"]: R12.call_candidates_json(
                   c, q["query"], kinds=q.get("kinds"), channels=q.get("channels"),
                   authors=q.get("authors"), item_ids=q.get("item_ids")) for q in queries}
        if SCHEMA_013.exists() and self.applied_013:
            R12.apply_migration(c, "013_lexical_latency_phase3.sql")  # restore 013
        return s13, s12

    def test_full_row_parity_all_queries(self) -> None:
        s13, s12 = self._capture()
        self.assertEqual(set(s13), set(s12))
        diffs = [n for n in s13 if s13[n] != s12[n]]
        self.assertEqual(diffs, [], f"013 != 012 full-row streams for: {diffs}")

    # ---- cross-chunk false-positive protection ----
    def test_cross_boundary_negative_parity(self) -> None:
        c = self.cluster
        item = R12.CROSS_BOUNDARY_ITEM
        ids13 = {r["item_id"] for r in R12.call_candidates(c, R12.CROSS_BOUNDARY_NEEDLE, kinds=["workflow"])}
        self.assertNotIn(item, ids13, "013 must not match a cross-boundary needle")

    # ---- newest-MATCHING-chunk anchor byte parity ----
    def test_newest_matching_anchor_parity(self) -> None:
        c = self.cluster
        item = R12.ANCHOR_ITEM
        stream13 = R12.call_candidates_json(c, R12.ANCHOR_NEEDLE, kinds=["workflow"])
        # The selected anchor must be the newest MATCHING chunk's (ANCHOR_TWO),
        # never the first chunk's. Byte-equal to 012 is proven by full-row parity.
        hit = [r for r in stream13 if r["i"] == item]
        self.assertTrue(hit, "anchor fixture item must surface under 013")
        self.assertEqual(hit[0]["s"], "ANCHOR_TWO",
                         "013 must select the newest MATCHING chunk's anchor")

    # ---- safe-workflow gate + quarantine ----
    def test_quarantined_workflow_excluded(self) -> None:
        c = self.cluster
        wan = R12.call_candidates(c, "WanVideoSampler", kinds=["workflow"])
        self.assertNotIn("7000", {r["item_id"] for r in wan},
                         "quarantined workflow 7000 must never rank under 013")

    # ---- 1..8000 chunk-length bound (enforced at the fragment/MV surface) ----
    def test_out_of_range_chunk_excluded_from_fragment_surface(self) -> None:
        c = self.cluster
        # The 1..8000 bound is enforced at the fragment arm + the MV source WHERE
        # (the workflow_python FTS arm has no length bound, by design, so it is
        # NOT the place to test the bound). Plant a safe workflow whose ONLY
        # workflow_python chunk is >8000 chars; it must be ABSENT from the MV
        # (the fragment/anchor surface) under 013, exactly as under 012.
        c.psql(
            "insert into external_resources (id, kind, source, external_id, title, body, "
            "author, url, metadata) overriding system value values "
            "(7700,'workflow','vibecomfy-external','w7700','OversizeWorkflow','d','agent',null,'{}') "
            "on conflict (id) do nothing;")
        c.psql(
            "insert into lexical_resource_python_state (resource_id, kind, cohort, "
            "public_state, available, body_duplicate, chunk_count) values "
            "(7700,'workflow','payload_python','safe',true,false,1) on conflict (resource_id) "
            "do update set public_state='safe', available=true;")
        overlong = "oversizeneedlexyz " + ("a" * 9000)
        c.psql(
            "insert into lexical_documents (entity_type, item_id, representation_type, "
            "chunk_index, chunk_text, matched_anchor, representation_hash, chunk_hash, "
            "quarantine_state) values "
            "('resource','7700','workflow_python',0,'" + overlong.replace("'", "''") + "',"
            "'ANCH7700','h7700','c7700','safe') on conflict do nothing;")
        R12.refresh_mv(c)
        rc, mv = c.psql(
            "select count(*)::text from public.lexical_workflow_python_search "
            "where item_id='7700';")
        self.assertEqual((mv or "0").strip(), "0",
                         "an out-of-range (>8000) chunk must be excluded from the fragment MV surface")
        # And byte-equal to 012 on the same corpus (the FTS arm may still surface
        # it; the bound contract is about the fragment surface, which both share).
        s13 = R12.call_candidates_json(c, "oversizeneedlexyz", kinds=["workflow"])
        R12.apply_migration(c, "012_lexical_latency_phase2.sql"); R12.refresh_mv(c)
        s12 = R12.call_candidates_json(c, "oversizeneedlexyz", kinds=["workflow"])
        if SCHEMA_013.exists() and self.applied_013:
            R12.apply_migration(c, "013_lexical_latency_phase3.sql")
        self.assertEqual(s13, s12, "013 fragment-surface bound behavior must equal 012")

    # ---- direct channel/author predicate semantics + empty resolution ----
    def test_channel_and_author_filter_semantics(self) -> None:
        c = self.cluster
        planted = str(1_000_000_000_000_000_000 + 17)
        chan = R12.call_candidates(c, "CogVideoX", channels=["wan_chatter"])
        self.assertIn(planted, {r["item_id"] for r in chan},
                      "channel-scoped query must include the planted channel message")
        auth = R12.call_candidates(c, "CogVideoX", authors=["QuintForms"])
        self.assertIn(planted, {r["item_id"] for r in auth},
                      "author-scoped query must include the planted author message")

    def test_unresolved_filter_resolves_to_no_messages(self) -> None:
        c = self.cluster
        unk = R12.call_candidates(c, "CogVideoX", authors=["NoSuchAuthor_xyz"])
        self.assertEqual([r for r in unk if r["entity_type"] == "message"], [],
                         "an unresolved author must yield zero messages, never all")
        unkch = R12.call_candidates(c, "CogVideoX", channels=["NoSuchChannel_xyz"])
        self.assertEqual([r for r in unkch if r["entity_type"] == "message"], [],
                         "an unresolved channel must yield zero messages, never all")

    # ---- ACL / security ----
    def test_mv_and_function_revoked_from_public(self) -> None:
        c = self.cluster
        MV = "public.lexical_workflow_python_search"
        for role in ("anon", "authenticated", "public"):
            rc, res = c.psql(f"select has_table_privilege('{role}','{MV}','SELECT')::text")
            self.assertEqual((rc == 0 and (res or "").strip() == "true"), False,
                             f"{role} must not SELECT the MV")
            rc, res = c.psql(
                "select has_function_privilege('" + role + "','public.hivemind_lexical_candidates("
                "text,int,text[],text[],text[],timestamptz,text[],text[],boolean,boolean)','EXECUTE')::text")
            self.assertEqual((rc == 0 and (res or "").strip() == "true"), False,
                             f"{role} must not EXECUTE the candidates function")

    def test_service_role_rpc_works(self) -> None:
        import json as _json
        c = self.cluster
        rc, out = c.psql(
            "set role service_role; "
            "select public.hivemind_lexical_search('WanVideoSampler',50,'{workflow}','{}','{}',"
            "null,'{}','{}','lexical')::text;")
        c.psql("reset role;")
        self.assertEqual(rc, 0, "service_role RPC must still work under 013")
        env = {}
        if "{" in out:
            env = _json.loads(out[out.find("{"):out.rfind("}") + 1])
        self.assertIn("count", env, "RPC must return a result envelope")

    def test_proacl_unchanged_vs_012(self) -> None:
        c = self.cluster
        proacl_013 = R12.get_proacl(c)
        R12.apply_migration(c, "012_lexical_latency_phase2.sql")
        proacl_012 = R12.get_proacl(c)
        if self.applied_013:
            R12.apply_migration(c, "013_lexical_latency_phase3.sql")
        self.assertEqual(proacl_013, proacl_012,
                         "CREATE OR REPLACE must preserve proacl (013 == 012)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
