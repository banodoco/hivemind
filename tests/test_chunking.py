"""Offline tests for executors.chunking (plan task 2.6).

Pure + offline: no network, DB, provider, or stored-Python execution. Pins the
2.6 completion signal — repeated runs yield identical chunks, and long prose +
generated-Python golden cases can hit a LATER chunk — plus offsets, per-stream
chunk_index, no-silent-truncation, parser fallback, and best-chunk collapse.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))

from executors import canonical_representations as canon  # noqa: E402
from executors import chunking as ch  # noqa: E402
from executors import workflow_representation as wr  # noqa: E402


def _message_rep(content: str, item_id: str = "100") -> canon.CanonicalRepresentation:
    return canon.CanonicalRepresentation(
        entity_type="message",
        item_id=item_id,
        representation_type="prose",
        text=content,
        representation_hash=wr.representation_hash(content),
        available=True,
        public_state="safe",
    )


def _python_rep(source: str, item_id: str = "2580") -> canon.CanonicalRepresentation:
    return canon.CanonicalRepresentation(
        entity_type="resource",
        item_id=item_id,
        representation_type="workflow_python",
        text=source,
        representation_hash=wr.representation_hash(source),
        available=True,
        public_state="safe",
    )


class DeterminismTests(unittest.TestCase):
    def test_prose_repeated_runs_identical(self):
        text = "\n\n".join(f"paragraph {i} about wan video motion amplitude." for i in range(40))
        rep = _message_rep(text)
        a = ch.chunk_representation(rep, target_tokens=64, overlap_tokens=10)
        b = ch.chunk_representation(rep, target_tokens=64, overlap_tokens=10)
        self.assertTrue(a)
        self.assertEqual(
            [(c.chunk_index, c.text, c.chunk_hash, c.char_start, c.char_end) for c in a],
            [(c.chunk_index, c.text, c.chunk_hash, c.char_start, c.char_end) for c in b],
        )

    def test_python_repeated_runs_identical(self):
        src = "\n".join(f"x{i} = {i}" for i in range(200))
        rep = _python_rep(src)
        a = ch.chunk_representation(rep, target_tokens=32, overlap_tokens=8)
        b = ch.chunk_representation(rep, target_tokens=32, overlap_tokens=8)
        self.assertTrue(a)
        self.assertEqual([c.text for c in a], [c.text for c in b])
        self.assertEqual([c.chunk_hash for c in a], [c.chunk_hash for c in b])

    def test_two_prose_configs_differ(self):
        text = "\n\n".join(f"p{i} " * 30 for i in range(30))
        rep = _message_rep(text)
        small = ch.chunk_representation(rep, target_tokens=64, overlap_tokens=8)
        large = ch.chunk_representation(rep, target_tokens=256, overlap_tokens=16)
        self.assertGreater(len(small), len(large))

    def test_two_python_configs_differ(self):
        src = "\n".join(f"v{i} = call_{i}()" for i in range(120))
        rep = _python_rep(src)
        small = ch.chunk_representation(rep, target_tokens=32, overlap_tokens=8)
        large = ch.chunk_representation(rep, target_tokens=256, overlap_tokens=32)
        self.assertGreater(len(small), len(large))

    def test_chunk_texts_match_frozen_chunker(self):
        # 2.6 adds offsets/identity, never alters the frozen chunk texts/hashes.
        text = "\n\n".join(f"line cluster {i} of prose content here." for i in range(20))
        rep = _message_rep(text)
        ours = ch.chunk_representation(rep, target_tokens=64, overlap_tokens=10)
        frozen = wr.chunk_prose(text, target_tokens=64, overlap_tokens=10)
        self.assertEqual([c.text for c in ours], [c.text for c in frozen])
        self.assertEqual([c.chunk_hash for c in ours], [c.hash for c in frozen])
        self.assertEqual([c.method for c in ours], [c.method for c in frozen])


class OffsetTests(unittest.TestCase):
    def test_offsets_within_source_and_monotonic(self):
        text = "\n\n".join(f"paragraph {i} " * 10 for i in range(15))
        rep = _message_rep(text)
        chunks = ch.chunk_representation(rep, target_tokens=64, overlap_tokens=8)
        self.assertGreater(len(chunks), 1)
        norm = wr.normalize_for_hash(text)
        starts = []
        for c in chunks:
            self.assertGreaterEqual(c.char_start, 0, "offset must be located")
            self.assertLessEqual(c.char_start, c.char_end)
            self.assertLessEqual(c.char_end, len(norm))
            # The anchor is the chunk's primary (base-pack) content; an overlap
            # chunk prepends a tail, so the base pack is the chunk's suffix.
            self.assertTrue(
                c.text.endswith(norm[c.char_start:c.char_end]),
                "base-pack anchor must be a suffix of the (possibly overlapped) chunk",
            )
            self.assertGreaterEqual(c.start_line, 1)
            starts.append(c.char_start)
        self.assertEqual(starts, sorted(starts), "char_start must be monotonic")

    def test_offsets_exact_for_non_overlapping_partition(self):
        text = "\n\n".join(f"paragraph {i} " * 10 for i in range(12))
        rep = _message_rep(text)
        chunks = ch.chunk_representation(rep, target_tokens=64, overlap_tokens=0)
        norm = wr.normalize_for_hash(text)
        for c in chunks:
            self.assertEqual(norm[c.char_start:c.char_end], c.text)

    def test_single_chunk_covers_whole_source(self):
        rep = _message_rep("a short message with no need to split.")
        chunks = ch.chunk_representation(rep)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].chunk_index, 0)
        self.assertEqual(chunks[0].char_start, 0)


class CoverageAndFallbackTests(unittest.TestCase):
    def test_no_silent_truncation_on_measured_worst_case(self):
        # Inventory max payload.python_source ~1.4M chars (task 0.3).
        src = "node = 1  # " + ("x" * 1_400_000) + "\n"
        rep = _python_rep(src)
        chunks = ch.chunk_representation(rep, target_tokens=512, overlap_tokens=50)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(ch.coverage_ok(src, chunks))

    def test_python_parser_fallback_for_huge_literal(self):
        huge = 'workflow_json = "' + ("A" * 4000) + '"\n'
        rep = _python_rep(huge)
        chunks = ch.chunk_representation(rep, target_tokens=64, overlap_tokens=16)
        self.assertTrue(chunks)
        self.assertIn("ast_fallback", {c.method for c in chunks})
        self.assertTrue(ch.coverage_ok(huge, chunks))

    def test_python_syntax_error_still_chunked(self):
        rep = _python_rep("def broken(:\n    x =\nplain text line\n")
        chunks = ch.chunk_representation(rep, target_tokens=32, overlap_tokens=8)
        self.assertTrue(chunks)
        self.assertTrue(ch.coverage_ok(rep.text, chunks))

    def test_empty_representation_yields_no_chunks(self):
        self.assertEqual(ch.chunk_representation(_message_rep("   ")), [])


class PerStreamIndexTests(unittest.TestCase):
    def test_workflow_prose_and_python_each_start_at_chunk_zero(self):
        row = {
            "kind": "workflow",
            "title": "Wan I2V",
            "body": "A description of the pipeline.\n\nPython scratchpad source:\nx = 1\n",
            "payload": {"python_source": "x = 1\n"},
            "metadata": {"workflow_semantics": {"media_type": "video"}},
        }
        reps = canon.build_representations(row, "resource", item_id="2580")
        # prose + workflow_python, never a duplicate of the python bytes.
        self.assertEqual({r.representation_type for r in reps}, {"prose", "workflow_python"})
        prose_chunks = ch.chunk_representation(
            next(r for r in reps if r.representation_type == "prose"),
            target_tokens=64,
            overlap_tokens=8,
        )
        py_chunks = ch.chunk_representation(
            next(r for r in reps if r.representation_type == "workflow_python"),
            target_tokens=64,
            overlap_tokens=8,
        )
        self.assertEqual([c.chunk_index for c in prose_chunks], list(range(len(prose_chunks))))
        self.assertEqual([c.chunk_index for c in py_chunks], list(range(len(py_chunks))))
        # Identity is the same item; representation streams are distinct.
        self.assertEqual({c.item_id for c in prose_chunks + py_chunks}, {"2580"})
        self.assertEqual(
            {c.representation_type for c in prose_chunks}, {"prose"}
        )

    def test_quarantined_python_never_chunked(self):
        secret = "token = 'sk-" + "A" * 60 + "'\n"
        row = {
            "kind": "workflow",
            "title": "Bad workflow",
            "body": "desc\n\nPython scratchpad source:\n" + secret,
            "payload": {"python_source": secret},
            "metadata": {},
        }
        reps = canon.build_representations(row, "resource", item_id="999")
        # Quarantined python is excluded: only prose is emitted.
        self.assertEqual({r.representation_type for r in reps}, {"prose"})
        chunks = ch.chunk_representations(reps)
        for c in chunks:
            self.assertNotIn("sk-", c.text)
            self.assertNotIn("workflow_python", c.representation_type)


class BestChunkSelectionTests(unittest.TestCase):
    def _long_prose_with_late_answer(self):
        # The relevant fact is deliberately in a LATER paragraph/chunk.
        filler = "\n\n".join(
            f"Filler paragraph {i} about unrelated comfyui topics and models." for i in range(60)
        )
        text = filler + "\n\nThe block swap is configured in the sampler kwargs region."
        return text

    def test_later_prose_chunk_can_be_best_hit(self):
        rep = _message_rep(self._long_prose_with_late_answer())
        chunks = ch.chunk_representation(rep, target_tokens=64, overlap_tokens=8)
        self.assertGreater(len(chunks), 2)
        # Score only the chunk carrying the target fact; filler scores 0.
        scored = [(c, 1.0 if "block swap" in c.text else 0.0) for c in chunks]
        best = ch.select_best_chunk(scored)
        self.assertIsNotNone(best)
        self.assertIn("block swap", best.text)
        # The winning chunk is NOT the leading chunk — a later chunk can win.
        self.assertGreater(best.chunk_index, 0)

    def test_later_python_chunk_can_be_best_hit(self):
        # Many assignment blocks; the matching symbol is near the end.
        head = "\n".join(f"var_{i} = produce_{i}()" for i in range(120))
        src = head + "\nWanVideoSampler = load_class('WanVideoSampler')\n"
        rep = _python_rep(src)
        chunks = ch.chunk_representation(rep, target_tokens=64, overlap_tokens=8)
        self.assertGreater(len(chunks), 2)
        scored = [(c, 1.0 if "WanVideoSampler" in c.text else 0.0) for c in chunks]
        best = ch.select_best_chunk(scored)
        self.assertIn("WanVideoSampler", best.text)
        self.assertGreater(best.chunk_index, 0)

    def test_collapse_one_best_per_item_across_reps(self):
        row = {
            "kind": "workflow",
            "title": "Workflow with code",
            "body": "description\n\nPython scratchpad source:\nz = 1\n",
            "payload": {"python_source": "z = 1\n"},
            "metadata": {},
        }
        reps = canon.build_representations(row, "resource", item_id="42")
        chunks = ch.chunk_representations(reps, target_tokens=64, overlap_tokens=8)
        # Give the python chunk a higher score; it must win for that item.
        scored = [
            (c, 0.9 if c.representation_type == "workflow_python" else 0.1) for c in chunks
        ]
        collapsed = ch.collapse_by_item(scored)
        self.assertEqual(set(collapsed), {("resource", "42")})
        self.assertEqual(collapsed[("resource", "42")].representation_type, "workflow_python")

    def test_collapse_dedups_multi_chunk_item(self):
        text = self._long_prose_with_late_answer()
        rep = _message_rep(text, item_id="7")
        chunks = ch.chunk_representation(rep, target_tokens=64, overlap_tokens=8)
        scored = [(c, 1.0 / (i + 1)) for i, c in enumerate(chunks)]
        collapsed = ch.collapse_by_item(scored)
        # Many chunks, one item -> exactly one survivor.
        self.assertEqual(set(collapsed), {("message", "7")})


class ManifestTests(unittest.TestCase):
    def test_manifest_has_content_embeddings_shape(self):
        rep = _message_rep("a bounded message body for embedding.")
        chunks = ch.chunk_representation(rep)
        rows = ch.chunk_manifest(chunks, contract_id=123456789)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        for key in (
            "contract_id", "entity_type", "item_id", "representation_type",
            "chunk_index", "chunk_text", "embedding", "representation_hash",
            "chunk_hash",
        ):
            self.assertIn(key, row)
        self.assertEqual(row["contract_id"], 123456789)
        self.assertEqual(row["representation_type"], "prose")
        self.assertEqual(row["chunk_index"], 0)
        self.assertIsNone(row["embedding"])

    def test_manifest_chunk_hash_is_full_chunk_hash_not_bounded_copy(self):
        long_chunk_text = "x" * 5000
        rep = _message_rep(long_chunk_text)
        chunk = ch.chunk_representation(rep)[0]
        rows = ch.chunk_manifest([chunk], contract_id=1, chunk_text_bound=100)
        # Stored text is bounded for defense in depth...
        self.assertLessEqual(len(rows[0]["chunk_text"]), 100)
        # ...but the chunk_hash covers the FULL chunk (identity/reuse unaffected).
        self.assertEqual(rows[0]["chunk_hash"], wr.chunk_hash(chunk.text))


class ConfigTests(unittest.TestCase):
    def test_default_configs_per_representation(self):
        self.assertEqual(ch.config_for_representation("prose").representation_type, "prose")
        self.assertEqual(
            ch.config_for_representation("workflow_python").representation_type,
            "workflow_python",
        )

    def test_overlap_must_be_less_than_target(self):
        with self.assertRaises(ValueError):
            ch.ChunkingConfig("prose", target_tokens=64, overlap_tokens=64)


if __name__ == "__main__":
    unittest.main()
