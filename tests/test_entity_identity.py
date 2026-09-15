"""Offline tests for the entity_type/result_kind identity + snowflake boundary (task 2.4).

Pure and offline. Pins:
  * result_kind -> entity_type mapping (messages, concrete resource kinds,
    distillations, the workflow/resource alias all agree);
  * exact Discord-snowflake string handling — a >2^53 id survives a JSON
    round-trip as an exact string and is never coerced to a float/number;
  * the embedding/shared-index identity key mirrors the content_embeddings PK.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))

from executors import entity_identity as ei  # noqa: E402


class ResultKindMappingTests(unittest.TestCase):
    def test_message_maps_to_message(self):
        self.assertEqual(ei.entity_type_for_result_kind("message"), ei.ENTITY_MESSAGE)

    def test_distillation_maps_to_distillation(self):
        self.assertEqual(ei.entity_type_for_result_kind("distillation"), ei.ENTITY_DISTILLATION)

    def test_concrete_resource_kinds_map_to_resource(self):
        for kind in ("resource", "workflow", "article", "transcript", "blog_post", "repo", "guide"):
            self.assertEqual(
                ei.entity_type_for_result_kind(kind), ei.ENTITY_RESOURCE,
                msg=f"{kind} should map to resource",
            )

    def test_unknown_future_resource_kind_maps_to_resource(self):
        # A brand-new resource kind needs no identity change.
        self.assertEqual(ei.entity_type_for_result_kind("diffusion_model"), ei.ENTITY_RESOURCE)

    def test_empty_kind_raises(self):
        for bad in ("", "   ", None, 123):
            with self.assertRaises(ValueError):
                ei.entity_type_for_result_kind(bad)

    def test_workflow_and_resource_share_entity_type(self):
        # AD-1: `workflow` is a backwards-compatible alias for `resource`; both
        # resolve to one entity_type so reclassifying never changes identity.
        self.assertEqual(
            ei.entity_type_for_result_kind("workflow"),
            ei.entity_type_for_result_kind("resource"),
        )

    def test_normalize_result_kind_validates(self):
        self.assertEqual(ei.normalize_result_kind("  workflow  "), "workflow")
        with self.assertRaises(ValueError):
            ei.normalize_result_kind("")
        with self.assertRaises(ValueError):
            ei.normalize_result_kind(None)

    def test_result_kind_is_resource(self):
        self.assertTrue(ei.result_kind_is_resource("workflow"))
        self.assertTrue(ei.result_kind_is_resource("article"))
        self.assertFalse(ei.result_kind_is_resource("message"))
        self.assertFalse(ei.result_kind_is_resource("distillation"))
        self.assertFalse(ei.result_kind_is_resource(""))

    def test_entity_types_complete(self):
        self.assertEqual(set(ei.ENTITY_TYPES), {"message", "resource", "distillation"})

    def test_cite_kind_maps_one_to_one(self):
        for cite_kind in ei.CITE_ITEM_KINDS:
            self.assertEqual(ei.entity_type_for_cite_kind(cite_kind), cite_kind)
        with self.assertRaises(ValueError):
            ei.entity_type_for_cite_kind("workflow")  # not a cite kind


class SnowflakeStringTests(unittest.TestCase):
    # A real Discord snowflake (18-19 digits) is well above 2^53.
    SNOWFLAKE = 1234567890123456789
    SAFE_INT = 42

    def test_snowflake_exceeds_json_safe_range(self):
        self.assertTrue(ei.is_discord_snowflake(self.SNOWFLAKE))
        self.assertTrue(ei.is_discord_snowflake(str(self.SNOWFLAKE)))

    def test_small_int_is_not_flagged_as_snowflake(self):
        self.assertFalse(ei.is_discord_snowflake(self.SAFE_INT))
        self.assertFalse(ei.is_discord_snowflake("42"))

    def test_stringify_int_exact(self):
        self.assertEqual(ei.stringify_item_id(self.SNOWFLAKE), "1234567890123456789")
        self.assertEqual(ei.stringify_item_id(self.SAFE_INT), "42")

    def test_stringify_str_passthrough(self):
        self.assertEqual(ei.stringify_item_id("1234567890123456789"), "1234567890123456789")

    def test_stringify_rejects_float(self):
        with self.assertRaises(ValueError):
            ei.stringify_item_id(1234567890123456789.0)

    def test_stringify_rejects_bool_empty(self):
        with self.assertRaises(ValueError):
            ei.stringify_item_id(True)
        with self.assertRaises(ValueError):
            ei.stringify_item_id("   ")

    def test_is_json_safe_integer(self):
        self.assertTrue(ei.is_json_safe_integer(self.SAFE_INT))
        self.assertFalse(ei.is_json_safe_integer(self.SNOWFLAKE))
        self.assertFalse(ei.is_json_safe_integer("42"))
        self.assertFalse(ei.is_json_safe_integer(True))

    def test_snowflake_survives_json_roundtrip_as_string(self):
        self.assertTrue(ei.item_id_survives_json_roundtrip(self.SNOWFLAKE))
        self.assertTrue(ei.item_id_survives_json_roundtrip(str(self.SNOWFLAKE)))

    def test_snowflake_round_trips_through_full_json_pipeline(self):
        # The exact hazard: encode the id as it travels in a result row and
        # decode it back. As a STRING it is exact; as a NUMBER it would round.
        as_string = {"item_id": ei.stringify_item_id(self.SNOWFLAKE)}
        decoded = json.loads(json.dumps(as_string))
        self.assertEqual(decoded["item_id"], "1234567890123456789")
        self.assertIsInstance(decoded["item_id"], str)

    def test_numeric_id_as_json_number_would_round(self):
        # Documents WHY we stringify: the same value as a JSON number loses
        # precision under a JS-class parser. (json in Python preserves big ints,
        # so we assert the boundary condition directly: the int is > 2^53.)
        self.assertGreater(self.SNOWFLAKE, ei.JSON_SAFE_INTEGER_MAX)


class EmbeddingIdentityKeyTests(unittest.TestCase):
    def test_key_shape_mirrors_pk_minus_contract(self):
        key = ei.embedding_identity_key("message", 1234567890123456789, "prose", 0)
        self.assertEqual(key, ("message", "1234567890123456789", "prose", 0))

    def test_key_stringifies_snowflake(self):
        key = ei.embedding_identity_key("resource", 1234567890123456789, "workflow_python", 3)
        self.assertIsInstance(key[1], str)
        self.assertEqual(key[1], "1234567890123456789")

    def test_key_rejects_bad_entity_type(self):
        with self.assertRaises(ValueError):
            ei.embedding_identity_key("workflow", "1", "prose", 0)  # workflow is not an entity_type

    def test_key_rejects_negative_chunk(self):
        with self.assertRaises(ValueError):
            ei.embedding_identity_key("message", "1", "prose", -1)


if __name__ == "__main__":
    unittest.main()
