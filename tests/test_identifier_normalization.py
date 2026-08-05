"""Tests for the Phase-1 identifier-normalization contract (plan task 1.4).

Pins the frozen rules in ``executors.identifier_normalization`` and the on-disk
machine-readable fixture corpus
``eval/retrieval/fixtures/identifier-normalization-v1.json``: the compact and
punctuation-preserving forms, the casefold policy (NFC + ``str.lower()``, not
``casefold()``), the documented non-equivalences (confusables, ligatures,
fullwidth, ß, İ), the alias representation (provenance/version/collision/
priority/safe-update), and the no-silent-natural-language-rewrite guarantee.

Offline only: no database, no network, no provider. The SQL/Python byte-for-byte
parity and the IMMUTABLE/index-suitability proof live in
``scripts/validate_identifier_normalization.py`` (opt-in isolated cluster); their
output is recorded in the task-1.4 report.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

import executors.identifier_normalization as I  # noqa: E402
import executors.lexical_contract as L  # noqa: E402

CORPUS_JSON = REPO / "eval" / "retrieval" / "fixtures" / "identifier-normalization-v1.json"


class TestFrozenContractMetadata(unittest.TestCase):
    def test_versions_frozen(self):
        self.assertEqual(I.IDENTIFIER_NORMALIZATION_VERSION, 1)
        self.assertEqual(I.IDENTIFIER_ALIAS_VERSION, 1)

    def test_casefold_policy_is_lower_not_casefold(self):
        self.assertIn("str.lower()", I.CASEFOLD_POLICY)
        self.assertIn("not str.casefold()", I.CASEFOLD_POLICY)

    def test_reexports_are_same_objects_as_lexical_contract(self):
        # Single source of truth: the task-1.4 helpers ARE the frozen 1.1 helpers.
        self.assertIs(I.normalize_identifier, L.normalize_identifier)
        self.assertIs(I.normalize_query, L.normalize_query)
        self.assertIs(I.identifier_aliases, L.identifier_aliases)


class TestCompactForm(unittest.TestCase):
    def test_dotted_versioned_collapse(self):
        for v in ("Wan 2.2", "Wan2.2", "wan_2.2", "WAN 2.2", "wan2.2"):
            self.assertEqual(I.normalize_identifier(v), "wan22", v)
        self.assertEqual(I.normalize_identifier("FLUX.1"), "flux1")
        self.assertEqual(I.normalize_identifier("flux.1"), "flux1")
        self.assertEqual(I.normalize_identifier("flux1"), "flux1")

    def test_hyphenated_and_filename(self):
        self.assertEqual(I.normalize_identifier("LTX-Video"), "ltxvideo")
        self.assertEqual(I.normalize_identifier("ltx-2-19b-ic-lora-detailer"),
                         "ltx219bicloradetailer")
        self.assertEqual(I.normalize_identifier(".safetensors"), "safetensors")
        self.assertEqual(I.normalize_identifier(".gguf"), "gguf")
        self.assertEqual(I.normalize_identifier("lightx2v_I2V_14B.safetensors"),
                         "lightx2vi2v14bsafetensors")
        self.assertEqual(I.normalize_identifier("wan2.2_animate_14B_bf16"),
                         "wan22animate14bbf16")

    def test_python_symbols_and_code_fragments(self):
        self.assertEqual(I.normalize_identifier("WanVideoSampler"), "wanvideosampler")
        self.assertEqual(I.normalize_identifier("IPAdapterFaceIDKolors"),
                         "ipadapterfaceidkolors")
        self.assertEqual(I.normalize_identifier("from wanmodules import WanVideoSampler"),
                         "fromwanmodulesimportwanvideosampler")
        self.assertEqual(I.normalize_identifier("class WanVideoSamplerBlockSwap"),
                         "classwanvideosamplerblockswap")

    def test_keyword_argument_keeps_equals_drops_underscore(self):
        self.assertEqual(I.normalize_identifier("force_clip_output=False"),
                         "forceclipoutput=false")
        self.assertEqual(I.normalize_identifier("steps=20"), "steps=20")
        self.assertEqual(I.normalize_identifier("seed=12345"), "seed=12345")

    def test_backslash_is_a_separator(self):
        self.assertEqual(I.normalize_identifier("C:\\Users\\flux"), "cusersflux")
        self.assertEqual(I.normalize_identifier("a\\b/c.d-e_f"), "abcdef")

    def test_code_punctuation_kept(self):
        # = + # % are NOT separators; () [] {} _ . - are.
        self.assertEqual(I.normalize_identifier("key=value"), "key=value")
        self.assertEqual(I.normalize_identifier("a+b"), "a+b")
        self.assertEqual(I.normalize_identifier("#flux"), "#flux")
        self.assertEqual(I.normalize_identifier("100%"), "100%")
        self.assertEqual(I.normalize_identifier("(grouped)"), "grouped")
        self.assertEqual(I.normalize_identifier("arr[0]"), "arr0")

    def test_null_and_empty(self):
        self.assertEqual(I.normalize_identifier(None), "")
        self.assertEqual(I.normalize_identifier(""), "")
        self.assertEqual(I.normalize_identifier("   "), "")
        self.assertEqual(I.normalize_identifier("._-/"), "")

    def test_casefold_is_nfc_lower(self):
        self.assertEqual(I.normalize_identifier("ÜBER"), "über")
        self.assertEqual(I.normalize_identifier("İSTANBUL"), "i̇stanbul")  # İ -> i + U+0307
        self.assertEqual(I.normalize_identifier("NAÏVE"), "naïve")


class TestPreserveForm(unittest.TestCase):
    def test_preserves_punctuation_collapses_whitespace(self):
        self.assertEqual(I.normalize_identifier_preserve("  Wan   2.2  "), "wan 2.2")
        self.assertEqual(I.normalize_identifier_preserve("FLUX.1"), "flux.1")
        self.assertEqual(I.normalize_identifier_preserve("lightx2v_I2V_14B"),
                         "lightx2v_i2v_14b")
        self.assertEqual(I.normalize_identifier_preserve("LTX-Video"), "ltx-video")
        self.assertEqual(I.normalize_identifier_preserve("models/checkpoints/x.safetensors"),
                         "models/checkpoints/x.safetensors")

    def test_preserve_keeps_punctuation_including_backslash(self):
        # The preserve form keeps ALL punctuation (incl. backslash) and only
        # collapses whitespace; the compact form is what drops separators.
        self.assertEqual(I.normalize_identifier_preserve("C:\\Users\\flux"), "c:\\users\\flux")
        self.assertEqual(I.normalize_identifier("C:\\Users\\flux"), "cusersflux")

    def test_preserve_null_safe(self):
        self.assertEqual(I.normalize_identifier_preserve(None), "")
        self.assertEqual(I.normalize_identifier_preserve("   "), "")


class TestFormsParity(unittest.TestCase):
    def test_identifier_forms_equals_lexical_contract_aliases(self):
        for s in ("Wan 2.2", "FLUX.1", "lightx2v_I2V_14B.safetensors",
                  "control net", "动漫视频", "WanVideoSampler", "key=value"):
            self.assertEqual(tuple(I.identifier_forms(s)), L.identifier_aliases(s), s)

    def test_forms_order_compact_then_preserve(self):
        forms = I.identifier_forms("Wan 2.2")
        self.assertEqual(forms[0], "wan22")
        self.assertIn("wan 2.2", forms)

    def test_forms_dedup_when_compact_equals_preserve(self):
        # Single-token ASCII: compact == preserve -> one form.
        forms = I.identifier_forms("WanVideoSampler")
        self.assertEqual(forms, ("wanvideosampler",))


class TestDistinctForms(unittest.TestCase):
    """Characters/forms that intentionally remain distinct (documented)."""

    def test_eszett_not_folded_to_ss(self):
        self.assertEqual(I.normalize_identifier("ß"), "ß")
        self.assertNotEqual(I.normalize_identifier("groß"), I.normalize_identifier("gross"))

    def test_ligatures_not_expanded(self):
        self.assertEqual(I.normalize_identifier("ﬁle"), "ﬁle")
        self.assertNotEqual(I.normalize_identifier("ﬁle"), I.normalize_identifier("file"))

    def test_fullwidth_not_folded(self):
        # NFC (not NFKC) keeps fullwidth distinct from ASCII.
        self.assertNotEqual(I.normalize_identifier("ＡＢＣ"), I.normalize_identifier("ABC"))

    def test_confusable_homoglyphs_not_folded(self):
        latin = I.normalize_identifier("a")     # U+0061
        cyrillic = I.normalize_identifier("а")  # U+0430
        greek = I.normalize_identifier("α")     # U+03B1
        self.assertEqual(len({latin, cyrillic, greek}), 3, "three distinct homoglyph compact keys")
        self.assertEqual(I.DISTINCT_CHARS.get("a/а/α")[:9], "Homoglyph")

    def test_distinct_chars_documented(self):
        for key in ("ß", "İ", "ﬁﬂﬀﬃﬄ", "Ａ-Ｚａ-ｚ０-９", "a/а/α"):
            self.assertIn(key, I.DISTINCT_CHARS)


class TestMalformedAndLengthBounds(unittest.TestCase):
    def test_none_and_empty(self):
        for v in (None, "", "   ", "\t\n", "._-/"):
            self.assertEqual(I.normalize_identifier(v), "", repr(v))

    def test_long_token_stable_and_idempotent(self):
        s = "WanVideoSampler_" * 1000  # 15,000 chars
        once = I.normalize_identifier(s)
        twice = I.normalize_identifier(once)
        self.assertEqual(once, twice)
        self.assertEqual(len(once), len(s.lower().replace("_", "")))

    def test_minimal_nonempty(self):
        self.assertEqual(I.normalize_identifier("A1"), "a1")


class TestAliasRegistryRegistration(unittest.TestCase):
    def _reg(self):
        r = I.AliasRegistry()
        r.register(canonical_kind="resource", canonical_id="2537",
                   canonical_name="ControlNet", alias_text="control net",
                   provenance=I.PROV_WORKFLOW_SEARCHABLE_ALIASES, provenance_detail="wf:2537")
        r.register(canonical_kind="resource", canonical_id="2537",
                   canonical_name="ControlNet", alias_text="controlnet",
                   provenance=I.PROV_CURATED)
        return r

    def test_register_derives_compact_and_preserve(self):
        r = self._reg()
        e = [x for x in r.entries if x.alias_text == "control net"][0]
        self.assertEqual(e.alias_compact, "controlnet")
        self.assertEqual(e.alias_preserve, "control net")
        self.assertEqual(e.canonical_key, "controlnet")
        self.assertEqual(e.alias_version, I.IDENTIFIER_ALIAS_VERSION)
        self.assertTrue(e.live)

    def test_register_is_idempotent_on_logical_key(self):
        r = I.AliasRegistry()
        r.register(canonical_kind="resource", canonical_id="1", canonical_name="X",
                   alias_text="x", provenance=I.PROV_CURATED)
        r.register(canonical_kind="resource", canonical_id="1", canonical_name="X",
                   alias_text="x", provenance=I.PROV_CURATED)
        self.assertEqual(len(r.entries), 1)

    def test_register_validates_entity_provenance_and_identity(self):
        r = I.AliasRegistry()
        with self.assertRaises(I.AliasValidationError):
            r.register(canonical_kind="bogus", canonical_id="1", canonical_name="X",
                       alias_text="x", provenance=I.PROV_CURATED)
        with self.assertRaises(I.AliasValidationError):
            r.register(canonical_kind="resource", canonical_id="1", canonical_name="X",
                       alias_text="x", provenance="bogus")
        with self.assertRaises(I.AliasValidationError):
            r.register(canonical_kind="resource", canonical_id="", canonical_name="X",
                       alias_text="x", provenance=I.PROV_CURATED)
        with self.assertRaises(I.AliasValidationError):
            r.register(canonical_kind="resource", canonical_id="1", canonical_name="X",
                       alias_text="...", provenance=I.PROV_CURATED)  # empty compact
        with self.assertRaises(I.AliasValidationError):
            r.register(canonical_kind="resource", canonical_id="1", canonical_name="._-",
                       alias_text="x", provenance=I.PROV_CURATED)  # empty canonical key

    def test_register_canonical_forms_auto_derives(self):
        r = I.AliasRegistry()
        r.register_canonical_forms(canonical_kind="resource", canonical_id="42",
                                   canonical_name="Wan 2.2")
        compacts = {e.alias_compact for e in r.entries}
        self.assertIn("wan22", compacts)
        self.assertTrue(all(e.provenance == I.PROV_DERIVED_CANONICAL for e in r.entries))


class TestAliasCollisions(unittest.TestCase):
    def test_collision_reported_not_merged(self):
        r = I.AliasRegistry()
        r.register(canonical_kind="resource", canonical_id="2537", canonical_name="ControlNet",
                   alias_text="controlnet", provenance=I.PROV_CURATED)
        r.register(canonical_kind="resource", canonical_id="9999", canonical_name="Control Mesh",
                   alias_text="controlnet", provenance=I.PROV_CURATED)
        coll = r.collisions()
        self.assertIn("controlnet", coll)
        identities = {e.identity for e in coll["controlnet"]}
        self.assertEqual(identities, {"resource:2537", "resource:9999"})
        # Both remain valid candidates (not silently dropped).
        self.assertEqual(len(r.resolve_alias_candidates("controlnet")), 2)

    def test_no_collision_within_one_identity(self):
        r = I.AliasRegistry()
        r.register(canonical_kind="resource", canonical_id="1", canonical_name="ControlNet",
                   alias_text="control net", provenance=I.PROV_WORKFLOW_SEARCHABLE_ALIASES)
        r.register(canonical_kind="resource", canonical_id="1", canonical_name="ControlNet",
                   alias_text="controlnet", provenance=I.PROV_CURATED)
        self.assertEqual(r.collisions(), {})

    def test_resolution_is_deterministic_priority_then_identity(self):
        r = I.AliasRegistry()
        # Same compact, same priority (curated=100) -> tie broken by identity ASC.
        r.register(canonical_kind="resource", canonical_id="9999", canonical_name="B",
                   alias_text="shared", provenance=I.PROV_CURATED)
        r.register(canonical_kind="resource", canonical_id="1111", canonical_name="A",
                   alias_text="shared", provenance=I.PROV_CURATED)
        # Lower priority appears last.
        r.register(canonical_kind="resource", canonical_id="5555", canonical_name="C",
                   alias_text="shared", provenance=I.PROV_DERIVED_CANONICAL)
        order = [e.canonical_id for e in r.resolve_alias_candidates("shared")]
        self.assertEqual(order, ["1111", "9999", "5555"])


class TestNoSilentNLRewrite(unittest.TestCase):
    def test_expand_returns_identity_strings_not_text(self):
        r = I.AliasRegistry()
        r.register(canonical_kind="resource", canonical_id="2537", canonical_name="ControlNet",
                   alias_text="controlnet", provenance=I.PROV_CURATED)
        exp = r.expand_query_identifiers("ControlNet")
        self.assertEqual(exp, {"resource:2537"})
        # Every element is a "kind:id" identity string — never a prose/FTS rewrite.
        self.assertTrue(all(":" in s and " " not in s for s in exp))

    def test_expand_only_fires_for_registered_alias_keys(self):
        r = I.AliasRegistry()
        # No aliases registered -> nothing is synthesized from arbitrary NL.
        self.assertEqual(r.expand_query_identifiers("some long natural language question"), set())
        self.assertEqual(r.expand_query_identifiers(""), set())

    def test_aliases_never_relabel_identity(self):
        r = I.AliasRegistry()
        r.register(canonical_kind="resource", canonical_id="2537", canonical_name="ControlNet",
                   alias_text="cn", provenance=I.PROV_CURATED)
        for e in r.entries:
            # An alias expands TO the canonical identity; it never changes it.
            self.assertEqual(e.canonical_id, "2537")


class TestProvenancePriority(unittest.TestCase):
    def test_priority_ordering(self):
        self.assertGreater(I.provenance_priority(I.PROV_CURATED),
                           I.provenance_priority(I.PROV_WORKFLOW_SEARCHABLE_ALIASES))
        self.assertGreater(I.provenance_priority(I.PROV_WORKFLOW_SEARCHABLE_ALIASES),
                           I.provenance_priority(I.PROV_NODE_CLASS))
        self.assertGreater(I.provenance_priority(I.PROV_NODE_CLASS),
                           I.provenance_priority(I.PROV_MODEL_FILENAME))
        self.assertGreater(I.provenance_priority(I.PROV_MODEL_FILENAME),
                           I.provenance_priority(I.PROV_DERIVED_CANONICAL))
        self.assertEqual(I.provenance_priority("unknown"), 0)

    def test_provenance_vocabulary_frozen(self):
        self.assertEqual(set(I.PROVENANCE_VOCABULARY),
                         set(I.PROVENANCE_PRIORITY))


class TestCorpusSelfConsistency(unittest.TestCase):
    def setUp(self):
        self.corpus = json.loads(CORPUS_JSON.read_text())

    def test_corpus_metadata(self):
        self.assertTrue(self.corpus["post_hoc_locked"])
        self.assertEqual(self.corpus["normalization_version"], I.IDENTIFIER_NORMALIZATION_VERSION)
        self.assertEqual(self.corpus["alias_version"], I.IDENTIFIER_ALIAS_VERSION)
        self.assertEqual(self.corpus["casefold_policy"], I.CASEFOLD_POLICY)

    def test_module_reproduces_every_fixture(self):
        for fx in self.corpus["fixtures"]:
            self.assertEqual(I.normalize_identifier(fx["input"]),
                             fx["expected_compact"], fx["id"])
            self.assertEqual(I.normalize_identifier_preserve(fx["input"]),
                             fx["expected_preserve"], fx["id"])

    def test_completion_signal_forms_all_present(self):
        required = {"dotted", "versioned", "hyphenated", "filename",
                    "python_symbol", "keyword_argument", "alias"}
        present = {f for fx in self.corpus["fixtures"] for f in fx["forms"]}
        self.assertEqual(required & present, required)

    def test_accept_fixtures_identifier_forms_match_aliases(self):
        for fx in self.corpus["fixtures"]:
            if fx["parity_class"] != "accept":
                continue
            if not isinstance(fx["input"], str):
                # identifier_forms/aliases parity is a STRING guarantee; None is the
                # NULL boundary (see TestNullAndMalformedAndLength). The frozen
                # identifier_aliases stringifies None -> "None" -> ("none",); our
                # identifier_forms correctly yields () for the absent/NULL case.
                continue
            self.assertEqual(tuple(I.identifier_forms(fx["input"])),
                             L.identifier_aliases(fx["input"]), fx["id"])

    def test_distinct_fixtures_are_genuinely_distinct(self):
        confusables = [fx for fx in self.corpus["fixtures"] if "confusable" in fx["forms"]]
        compacts = {fx["expected_compact"] for fx in confusables}
        self.assertEqual(len(compacts), len(confusables))


if __name__ == "__main__":
    unittest.main()
