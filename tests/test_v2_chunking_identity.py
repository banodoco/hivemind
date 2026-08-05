"""Rigorous CHUNKING v2 identity regression tests.

Locks the selected-contract identity in place and GUARDS against the three
identity regressions this surface has seen:

  1. the STALE FULL chunking-v1 contract (chunking_version=1 + chunk_config v1),
     the selected literal *before* the chunker-behavior bump — bigint
     4663917141483337699 / eval id c0b98fedfff837e3;
  2. the INVALID DOUBLE-BUMP contract (chunking_version=2 + chunk_config v2),
     a prior implementation that wrongly bumped the chunk-config IDENTITY scheme
     to v2 as well as the chunker behavior — bigint 8308923303736049826 / eval id
     f34f39b8e12700a2;
  3. the historical dimension-only bigint being used as the *selected* literal
     (7571371577804399660 under chunking v1, 6368594834396668537 under v2 — both
     are shared by 384-small and 384-large and must NEVER be the selecting
     predicate).

The CORRECT selected identity keeps the two version axes distinct:
  * chunker BEHAVIOR is v2 (CHUNKING_VERSION = 2, the bounded oversized
    workflow-Python fallback fix), AND
  * chunk-config IDENTITY scheme is v1 (CHUNK_CONFIG_IDENTITY_VERSION = 1 — the
    fallback fix added no identity axis, so the scheme did not bump).
So the chunk-config identity is ``chunk_config\\x1fv1\\x1fprose#512/50\\x1fworkflow_python#512/50``
even though chunking is v2.

The frozen-literal and SQL-scan assertions are paired with BEHAVIORAL gates (the
bounded oversized workflow-Python fallback, and SQL<->Python identity parity is
exercised in tests.test_hnsw_pilot against a live cluster), so the suite is not
relying on string-only checks for behavioral properties.
"""

from __future__ import annotations

import pathlib
import unittest

from eval.retrieval import semantic as sem
from executors import embedding_contract as ec
from executors import selected_contract as sc
from executors import workflow_representation as wr

REPO = pathlib.Path(__file__).resolve().parents[1]
SCHEMA_033 = REPO / "schema" / "033_selected_contract_hnsw.sql"
SCHEMA_034 = REPO / "schema" / "034_phase2_acceptance_contract.sql"
PROTOCOL_TS = REPO / "supabase" / "functions" / "embedding-worker" / "protocol.ts"

# The CORRECT selected-contract identity: chunking BEHAVIOR v2 + chunk_config
# IDENTITY scheme v1.
CORRECT_SELECTED_BIGINT = 1360541028304258884
CORRECT_EVAL_HEX = "12e19cdb566b8744"
CORRECT_CHUNK_CONFIG_IDENTITY = "chunk_config\x1fv1\x1fprose#512/50\x1fworkflow_python#512/50"
CORRECT_HNSW_INDEX = "content_embeddings_hnsw_c1360541028304258884"

# (a) the STALE FULL chunking-v1 contract (chunking=1 + chunk_config v1): the
#     selected literal before the chunker-behavior bump; superseded by chunking v2.
STALE_CHUNKING_V1_SELECTED_BIGINT = 4663917141483337699
STALE_CHUNKING_V1_EVAL_HEX = "c0b98fedfff837e3"
# (b) the INVALID DOUBLE-BUMP contract (chunking=2 + chunk_config v2): a prior
#     implementation that bumped the identity SCHEME to v2 too; not selected.
INVALID_DOUBLE_BUMP_SELECTED_BIGINT = 8308923303736049826
INVALID_DOUBLE_BUMP_EVAL_HEX = "f34f39b8e12700a2"
INVALID_DOUBLE_BUMP_CHUNK_CONFIG_IDENTITY = "chunk_config\x1fv2\x1fprose#512/50\x1fworkflow_python#512/50"

# Dimension-only bigints (shared by 384-small and 384-large; NEVER a selected
# predicate). A pure function of chunking_version.
DIM_ONLY_BIGINT_CHUNKING_V1 = 7571371577804399660
DIM_ONLY_BIGINT_CHUNKING_V2 = 6368594834396668537


class TestCorrectSelectedIdentityFrozen(unittest.TestCase):
    """The selected-contract identity is exactly: chunking v2 + chunk_config v1."""

    def test_two_distinct_version_axes(self):
        # Chunker BEHAVIOR is v2; chunk-config IDENTITY scheme is v1.
        self.assertEqual(wr.CHUNKING_VERSION, 2)
        self.assertEqual(sc.SELECTED_CHUNKING_VERSION, 2)
        self.assertEqual(sc.CHUNK_CONFIG_IDENTITY_VERSION, 1)
        self.assertEqual(sem.CHUNK_CONFIG_IDENTITY_VERSION, 1)

    def test_chunk_config_identity_is_v1(self):
        self.assertEqual(sc.SELECTED_CHUNK_CONFIG_IDENTITY, CORRECT_CHUNK_CONFIG_IDENTITY)
        self.assertIn("\x1fv1\x1f", sc.SELECTED_CONTRACT_PREIMAGE)
        # ...while the BASE contract in the preimage carries chunking_version=2.
        self.assertIn("384\x1f1\x1f2\x1fchunk_config", sc.SELECTED_CONTRACT_PREIMAGE)

    def test_selected_bigint_and_sha_and_eval_hex(self):
        self.assertEqual(sc.SELECTED_CONTRACT_ID, CORRECT_SELECTED_BIGINT)
        self.assertEqual(sc.SELECTED_CONTRACT_SHA256_HEX,
                         "12e19cdb566b87445ab2d3563e6cb948f58801f78f8395878fc9e0c2457d5462")
        self.assertEqual(sc.EVAL_CONTRACT_ID_HEX, CORRECT_EVAL_HEX)
        # eval hex is the first 16 of the full sha (cross-corroboration).
        self.assertTrue(sc.SELECTED_CONTRACT_SHA256_HEX.startswith(sc.EVAL_CONTRACT_ID_HEX))

    def test_hnsw_index_name_derives_from_selected(self):
        self.assertEqual(CORRECT_HNSW_INDEX, f"content_embeddings_hnsw_c{sc.SELECTED_CONTRACT_ID}")

    def test_eval_identity_matches_selected_identity(self):
        """The eval candidate identity (semantic) is byte-identical to the
        accepted selected-contract identity, so the same preimage hashes to the
        same eval id / selected bigint."""
        small = next(c for c in sem.CANDIDATES if c.name == "384-small")
        self.assertEqual(small.eval_contract_identity_input, sc.SELECTED_CONTRACT_PREIMAGE)
        self.assertEqual(small.eval_contract_id, sc.EVAL_CONTRACT_ID_HEX)


class TestNoStaleOrInvalidIdentity(unittest.TestCase):
    """Guard: the selected literal is neither the stale chunking-v1 FULL contract
    nor the invalid chunk_config-v2 double-bump, and never a dimension-only id."""

    def test_selected_is_not_stale_chunking_v1(self):
        self.assertNotEqual(sc.SELECTED_CONTRACT_ID, STALE_CHUNKING_V1_SELECTED_BIGINT)
        self.assertNotEqual(sc.EVAL_CONTRACT_ID_HEX, STALE_CHUNKING_V1_EVAL_HEX)

    def test_selected_is_not_invalid_double_bump(self):
        self.assertNotEqual(sc.SELECTED_CONTRACT_ID, INVALID_DOUBLE_BUMP_SELECTED_BIGINT)
        self.assertNotEqual(sc.EVAL_CONTRACT_ID_HEX, INVALID_DOUBLE_BUMP_EVAL_HEX)
        self.assertNotEqual(sc.SELECTED_CHUNK_CONFIG_IDENTITY,
                            INVALID_DOUBLE_BUMP_CHUNK_CONFIG_IDENTITY)
        self.assertNotIn("\x1fv2\x1f", sc.SELECTED_CONTRACT_PREIMAGE)

    def test_selected_is_neither_dimension_only_id(self):
        self.assertNotEqual(sc.SELECTED_CONTRACT_ID, DIM_ONLY_BIGINT_CHUNKING_V1)
        self.assertNotEqual(sc.SELECTED_CONTRACT_ID, DIM_ONLY_BIGINT_CHUNKING_V2)

    def test_chunk_config_identity_is_not_the_double_bump_v2(self):
        # The identity scheme must be v1, never the invalid v2 double-bump.
        self.assertNotIn("chunk_config\x1fv2\x1f", sc.SELECTED_CHUNK_CONFIG_IDENTITY)

    def test_historical_dimension_only_id_is_v2_derived(self):
        # The recorded ambiguous dimension-only id moved with the chunking bump.
        self.assertEqual(sc.HISTORICAL_DIMENSION_ONLY_ID, DIM_ONLY_BIGINT_CHUNKING_V2)


class TestSchemaFilesUseCorrectSelectedLiteral(unittest.TestCase):
    """The active HNSW/acceptance SQL bakes the CORRECT selected literal (chunking
    v2 + chunk_config v1) and carries neither the stale chunking-v1 FULL contract,
    the invalid chunk_config-v2 double-bump, nor a dimension-only selected predicate."""

    def _body(self, path: pathlib.Path) -> str:
        return path.read_text(encoding="utf-8")

    def test_033_bakes_correct_selected_literal(self):
        body = self._body(SCHEMA_033)
        self.assertIn(str(CORRECT_SELECTED_BIGINT), body)
        self.assertIn(CORRECT_HNSW_INDEX, body)
        self.assertIn(f"contract_id = {CORRECT_SELECTED_BIGINT}", body)
        self.assertIn(f"v_active <> {CORRECT_SELECTED_BIGINT}", body)

    def test_033_has_no_stale_or_invalid_identity(self):
        body = self._body(SCHEMA_033)
        # Neither the stale chunking-v1 selected bigint nor the invalid double-bump
        # bigint may appear at all (not even as a comment).
        self.assertNotIn(str(STALE_CHUNKING_V1_SELECTED_BIGINT), body)
        self.assertNotIn(str(INVALID_DOUBLE_BUMP_SELECTED_BIGINT), body)
        self.assertNotIn(INVALID_DOUBLE_BUMP_EVAL_HEX, body)
        # The self-verify preimage uses chunk_config v1 (chunking is passed as 2).
        self.assertIn("||'v1'||E'\\x1f'||'prose#512/50'", body)
        self.assertNotIn("||'v2'||E'\\x1f'||'prose#512/50'", body)

    def test_033_dimension_only_not_a_selected_predicate(self):
        body = self._body(SCHEMA_033)
        for predicate in (
            f"contract_id = {DIM_ONLY_BIGINT_CHUNKING_V1}",
            f"v_active <> {DIM_ONLY_BIGINT_CHUNKING_V1}",
            f":= {DIM_ONLY_BIGINT_CHUNKING_V1}",
            f"contract_id = {DIM_ONLY_BIGINT_CHUNKING_V2}",
            f"v_active <> {DIM_ONLY_BIGINT_CHUNKING_V2}",
        ):
            self.assertNotIn(predicate, body,
                             f"dimension-only id used as selected predicate: {predicate!r}")

    def test_034_bakes_correct_identity(self):
        body = self._body(SCHEMA_034)
        self.assertIn(str(CORRECT_SELECTED_BIGINT), body)
        self.assertIn(CORRECT_EVAL_HEX, body)
        # The two version axes: chunking behavior v2, chunk-config identity v1.
        self.assertIn("chunking_version = 2", body)
        self.assertIn("chunk_config_version = 1", body)
        # Neither stale contract nor the invalid double-bump may appear.
        self.assertNotIn(str(STALE_CHUNKING_V1_SELECTED_BIGINT), body)
        self.assertNotIn(STALE_CHUNKING_V1_EVAL_HEX, body)
        self.assertNotIn(str(INVALID_DOUBLE_BUMP_SELECTED_BIGINT), body)
        self.assertNotIn(INVALID_DOUBLE_BUMP_EVAL_HEX, body)
        self.assertNotIn("||'v2'||E'\\x1f'||'prose#512/50'", body)


class TestBoundedFallbackBehavioral(unittest.TestCase):
    """BEHAVIORAL gate: the oversized workflow-Python fallback paths are bounded
    with complete coverage (the actual chunker-behavior v2 fix), not a string-only
    claim."""

    OVERLONG = "z" * 6000  # a single line far over the 2048-char budget

    def _max_chunk_and_coverage(self, src: str) -> tuple[int, bool]:
        chunks = wr.chunk_python(src, target_tokens=512, overlap_tokens=50)
        mx = max((len(c.text) for c in chunks), default=0)
        return mx, wr.coverage_ok(src, chunks)

    def test_parser_fallback_oversized_line_is_bounded(self):
        # A SyntaxError source (unparsable) takes the _line_window fallback path;
        # the single over-long line must be windowed, not emitted whole.
        src = "def (\n" + self.OVERLONG
        chunks = wr.chunk_python(src, target_tokens=512, overlap_tokens=50)
        self.assertGreaterEqual(len(chunks), 1)
        self.assertLessEqual(max((len(c.text) for c in chunks), default=0),
                             2048 + 200 + 1, "parser-fallback chunk unbounded")
        self.assertTrue(wr.coverage_ok(src, chunks), "parser-fallback lost coverage")

    def test_ast_numeric_literal_oversized_is_bounded(self):
        # A giant numeric literal trips Python's 4300-digit limit during
        # ast.parse -> SyntaxError -> _line_window; must be bounded + covered.
        src = "x = " + ("1" * 6000) + "\n"
        mx, cov = self._max_chunk_and_coverage(src)
        self.assertLessEqual(mx, 2048 + 200 + 1, f"numeric-literal chunk unbounded: {mx}")
        self.assertTrue(cov, "numeric-literal path lost coverage")

    def test_ast_string_literal_oversized_is_bounded(self):
        # A giant STRING literal parses, hits the AST oversized-literal branch,
        # and is windowed by _fixed_window (then overlap) -> bounded + covered.
        src = "import os\ns = \"" + ("A" * 6000) + "\"\nprint(s)\n"
        mx, cov = self._max_chunk_and_coverage(src)
        self.assertLessEqual(mx, 2048 + 200 + 1, f"string-literal chunk unbounded: {mx}")
        self.assertTrue(cov, "string-literal path lost coverage")

    def test_normal_source_unchanged_single_chunk(self):
        # The fix is a no-op for normal sources: one statement -> one chunk.
        src = "import os\n\ndef f():\n    return 1\n"
        chunks = wr.chunk_python(src, target_tokens=512, overlap_tokens=50)
        self.assertEqual(len(chunks), 1)
        self.assertTrue(wr.coverage_ok(src, chunks))


class TestDimensionOnlyIdIsDerivedFromChunking(unittest.TestCase):
    """The dimension-only id is a pure function of chunking_version (the subtle
    axis the task warns about); bumping chunking moves it, and the selected
    literal must always differ from it."""

    def test_v2_base_contract_id_matches_dim_only_constant(self):
        spec = ec.ContractSpec(provider="openai", model="text-embedding-3-small", dimension=384)
        self.assertEqual(spec.id, DIM_ONLY_BIGINT_CHUNKING_V2)
        self.assertEqual(spec.id, sc.HISTORICAL_DIMENSION_ONLY_ID)

    def test_v1_chunking_base_contract_id_is_the_old_dim_only(self):
        spec_v1 = ec.ContractSpec(
            provider="openai", model="text-embedding-3-small", dimension=384,
            canonicalization_version=1, chunking_version=1)
        self.assertEqual(spec_v1.id, DIM_ONLY_BIGINT_CHUNKING_V1)


class TestWorkerProtocolFrozenIdentity(unittest.TestCase):
    """The Deno embedding-worker's frozen SELECTED_CONTRACT (protocol.ts) must
    carry the SAME identity as the Python/SQL selected contract — chunking
    BEHAVIOR v2 + chunk_config IDENTITY scheme v1 — never the invalid
    chunk_config-v2 double-bump, never the stale chunking-v1 contract.

    This is the static contract test that pins the worker's TypeScript literal
    to the canonical Python identity, so a future double-bump (chunk_config v2,
    bigint 8308923303736049826, eval hex f34f39b8e12700a2) cannot silently
    reappear in the edge function."""

    # protocol.ts writes the unit separator as the literal TS escape text — the
    # six characters backslash, u, 0, 0, 1, f — so build the expected text with
    # chr(92) and avoid placing a literal backslash in this source.
    _SEP = chr(92) + "u001f"

    def _body(self) -> str:
        self.assertTrue(PROTOCOL_TS.exists(), f"missing {PROTOCOL_TS}")
        return PROTOCOL_TS.read_text(encoding="utf-8")

    def test_chunking_behavior_stays_v2(self):
        # The behavior bump (bounded oversized-fallback fix) must not regress.
        self.assertIn("chunkingVersion: 2,", self._body())

    def test_chunk_config_version_is_v1_not_v2(self):
        body = self._body()
        self.assertIn("chunkConfigVersion: 1,", body)
        self.assertNotIn("chunkConfigVersion: 2", body)

    def test_chunk_config_identity_is_v1_not_v2(self):
        identity_v1 = (
            'chunkConfigIdentity: "chunk_config' + self._SEP + 'v1' + self._SEP
            + 'prose#512/50' + self._SEP + 'workflow_python#512/50",'
        )
        identity_v2 = "chunk_config" + self._SEP + "v2"
        body = self._body()
        self.assertIn(identity_v1, body)
        # The invalid double-bump v2 identity must not be active anywhere.
        self.assertNotIn(identity_v2, body)

    def test_eval_contract_hex_matches_selected(self):
        body = self._body()
        self.assertIn(f'evalContractIdHex: "{CORRECT_EVAL_HEX}"', body)
        # The invalid double-bump eval hex must not be active.
        self.assertNotIn(INVALID_DOUBLE_BUMP_EVAL_HEX, body)

    def test_full_config_sha256_matches_selected(self):
        self.assertIn(
            'fullConfigSha256: "'
            '12e19cdb566b87445ab2d3563e6cb948f58801f78f8395878fc9e0c2457d5462"',
            self._body())

    def test_selected_contract_id_matches_selected_bigint(self):
        body = self._body()
        self.assertIn(f'selectedContractId: "{CORRECT_SELECTED_BIGINT}"', body)
        # The invalid double-bump selected bigint must not be active anywhere.
        self.assertNotIn(str(INVALID_DOUBLE_BUMP_SELECTED_BIGINT), body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
