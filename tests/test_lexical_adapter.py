"""Offline unit tests for the lexical retrieval adapter (Phase 1, tasks 1.7–1.11).

These test the deterministic offline model of the lexical candidate SQL
(``eval.retrieval.adapters.LexicalAdapter``) directly — the FTS AND-match, the
identifier-containment bands (mirroring schema/008), the full filter set
(kinds/source/date/channel/author/item_ids, including the workflow↔resource
alias), deterministic ordering, the global limit, no-hit behavior, and
Snowflake-safe string item ids. No PostgreSQL, no network.
"""

from __future__ import annotations

import unittest
from typing import Any

from eval.retrieval.adapters import (
    LexicalAdapter,
    lexical_norm_identifier,
    lexical_passes_filters,
    lexical_simple_tokens,
)
from eval.retrieval.schema import Corpus, CorpusItem, Query


def _item(kind: str, item_id: str, title: str = "", body: str = "",
          source: str = "banodoco-discord", author: str | None = None,
          context: str | None = None, created_at: str | None = "2026-01-01T00:00:00Z",
          status: str | None = None) -> CorpusItem:
    return CorpusItem(kind=kind, source=source, item_id=item_id, title=title,
                      body=body, author=author, context=context, created_at=created_at,
                      status=status)


def _run(items: list[CorpusItem], query: str, limit: int = 20,
         filters: dict[str, Any] | None = None):
    return LexicalAdapter(Corpus(items=items)).retrieve(
        Query(query=query, limit=limit, filters=filters or {})
    )


class LexicalMatchingTests(unittest.TestCase):
    def test_fts_and_match_finds_multi_term_legacy_misses(self) -> None:
        # Legacy ILIKE needs the contiguous substring "upscale model settings";
        # lexical FTS AND-matches the tokens in any order/position.
        items = [
            _item("article", "900", "Guide to upscale models",
                  "Settings for the upscale model appear here with samplers.",
                  source="hivemind"),
            _item("message", "111", body="model settings for upscale pipelines"),
        ]
        res = _run(items, "upscale model settings")
        ids = {(r.kind, r.item_id) for r in res}
        self.assertIn(("article", "900"), ids)
        self.assertIn(("message", "111"), ids)

    def test_ident_containment_bands_title_above_body(self) -> None:
        items = [
            _item("workflow", "20", title="WanVideoSampler node demo",
                  body="some prose", source="vibecomfy-external"),
            _item("message", "1", body="I use WanVideoSampler daily"),
        ]
        res = _run(items, "WanVideoSampler")
        # Title band (0.95) ranks the workflow first; message prose (0.90) after.
        self.assertEqual(res[0].item_id, "20")
        self.assertEqual(res[0].matched_representation, "prose")
        self.assertEqual(res[1].item_id, "1")
        self.assertEqual(res[1].matched_representation, "prose")

    def test_workflow_python_representation_carried(self) -> None:
        items = [_item("workflow", "20", title="demo",
                       body="class WanVideoSampler: pass", source="vibecomfy-external")]
        res = _run(items, "WanVideoSampler")
        self.assertEqual(res[0].matched_representation, "workflow_python")

    def test_spaced_form_bridged_by_compact_normalization(self) -> None:
        # "FLUX 1" body -> flux1 ; a "FLUX.1" query -> flux1 (task-1.6 bridge).
        self.assertEqual(lexical_norm_identifier("FLUX 1"), "flux1")
        self.assertEqual(lexical_norm_identifier("FLUX.1"), "flux1")
        items = [_item("message", "7", body="FLUX 1 is my go-to model")]
        res = _run(items, "FLUX.1")
        self.assertEqual([r.item_id for r in res], ["7"])

    def test_fts_requires_all_terms(self) -> None:
        items = [_item("article", "1", body="upscale model only two of three")]
        # 'upscale model settings' -> settings missing -> no FTS, no ident.
        res = _run(items, "upscale model settings")
        self.assertEqual(res, [])

    def test_no_hit_returns_empty(self) -> None:
        items = [_item("message", "1", body="hello world")]
        self.assertEqual(_run(items, "zzznotarealtoken"), [])


class LexicalFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.items = [
            _item("workflow", "20", title="WanVideoSampler",
                  body="nodes: WanVideoSampler", source="vibecomfy-external",
                  created_at="2026-07-20T00:00:00Z"),
            _item("message", "100", body="WanVideoSampler in wan_chatter",
                  author="QuintForms", context="wan_chatter",
                  created_at="2026-07-25T00:00:00Z"),
            _item("message", "101", body="WanVideoSampler in general",
                  author="buggz", context="general",
                  created_at="2026-07-28T00:00:00Z"),
            _item("distillation", "1", title="WanVideoSampler?",
                  body="answer", source="hivemind"),
        ]

    def test_kinds_workflow_alias_matches_resource(self) -> None:
        res = _run(self.items, "WanVideoSampler", filters={"kinds": ["workflow"]})
        ids = {(r.kind, r.item_id) for r in res}
        self.assertIn(("workflow", "20"), ids)
        # No messages/distillations under kinds=[workflow].
        self.assertFalse(any(k == "message" for k, _ in ids))

    def test_channel_filter_honored(self) -> None:
        res = _run(self.items, "WanVideoSampler", filters={"channels": ["wan_chatter"]})
        ids = {r.item_id for r in res}
        self.assertIn("100", ids)
        self.assertNotIn("101", ids)  # different channel

    def test_author_filter_honored(self) -> None:
        res = _run(self.items, "WanVideoSampler", filters={"authors": ["QuintForms"]})
        ids = {r.item_id for r in res}
        self.assertEqual(ids, {"100"})

    def test_since_filter_honored(self) -> None:
        res = _run(self.items, "WanVideoSampler", filters={"since": "2026-07-26"})
        ids = {r.item_id for r in res}
        self.assertIn("101", ids)
        self.assertNotIn("100", ids)

    def test_item_ids_filter_honored(self) -> None:
        res = _run(self.items, "WanVideoSampler",
                   filters={"kinds": ["message"], "item_ids": ["101"]})
        self.assertEqual([r.item_id for r in res], ["101"])

    def test_source_filter_honored(self) -> None:
        res = _run(self.items, "WanVideoSampler", filters={"sources": ["vibecomfy-external"]})
        self.assertEqual([r.item_id for r in res], ["20"])

    def test_passes_filters_helper(self) -> None:
        it = _item("workflow", "20", author="x", context="c")
        self.assertTrue(lexical_passes_filters(it, {"kinds": ["resource"]}))
        self.assertTrue(lexical_passes_filters(it, {"kinds": ["workflow"]}))
        self.assertFalse(lexical_passes_filters(it, {"kinds": ["message"]}))
        self.assertFalse(lexical_passes_filters(it, {"channels": ["other"]}))


class LexicalOrderLimitTests(unittest.TestCase):
    def test_deterministic_order(self) -> None:
        items = [_item("message", str(i), body="controlnet config")
                 for i in range(50)]
        a = [r.item_id for r in _run(items, "controlnet")]
        b = [r.item_id for r in _run(items, "controlnet")]
        self.assertEqual(a, b)

    def test_global_limit_enforced(self) -> None:
        items = [_item("message", str(i), body="controlnet config")
                 for i in range(50)]
        res = _run(items, "controlnet", limit=5)
        self.assertEqual(len(res), 5)

    def test_snowflake_item_id_preserved_as_string(self) -> None:
        snowflake = "1234567890123456789"  # > 2^53
        items = [_item("message", snowflake, body="WanVideoSampler")]
        res = _run(items, "WanVideoSampler")
        self.assertEqual(res[0].item_id, snowflake)
        self.assertIsInstance(res[0].item_id, str)
        # key() is the snowflake-safe (entity_kind, item_id) tuple.
        self.assertEqual(res[0].key(), ("message", snowflake))


class LexicalTokenizationTests(unittest.TestCase):
    def test_simple_tokens(self) -> None:
        self.assertEqual(lexical_simple_tokens("FLUX.1 dev model"),
                         ["flux", "1", "dev", "model"])

    def test_norm_identifier_strips_separators(self) -> None:
        self.assertEqual(lexical_norm_identifier("Wan 2.2"), "wan22")
        self.assertEqual(lexical_norm_identifier("lightx2v_I2V_14B"), "lightx2vi2v14b")


if __name__ == "__main__":
    unittest.main()
