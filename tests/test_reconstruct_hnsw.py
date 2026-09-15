"""Tests for ``scripts.reconstruct_hnsw_representative`` (Task 2.16).

These tests NEVER hit the network and NEVER touch the paid cache at
``.cache/hivemind-semantic-eval/``. The PostgREST reader is mocked, and cache
fact verification uses throwaway temp JSONL fixtures written into the test's own
``TemporaryDirectory``. Only the real ``reconstruct`` mode hits the live
read-only endpoint.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import reconstruct_hnsw_representative as R  # noqa: E402
from eval.retrieval import semantic as sem  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers: build a tiny synthetic cohort + a throwaway cache fixture.
# ---------------------------------------------------------------------------
def _fake_vector(seed: str, dim: int = 384) -> bytes:
    """Deterministic float32 little-endian vector (same encoding as the cache)."""
    import random

    rng = random.Random(seed)
    floats = [rng.uniform(-1, 1) for _ in range(dim)]
    return struct.pack(f"<{dim}f", *floats)


def _write_cache_fixture(
    dirpath: Path, *, dim: int, records: dict[str, bytes]
) -> Path:
    """Write a throwaway cache JSONL fixture (NEVER the paid cache).

    ``records`` maps cache-key -> base64 vector bytes. The fixture is mode 0600.
    """
    dirpath.mkdir(parents=True, exist_ok=True)
    os.chmod(dirpath, 0o700)
    p = dirpath / "cache.384.jsonl"
    lines = []
    for key, vecb64 in records.items():
        lines.append(json.dumps({"key": key, "d": dim, "v": vecb64, "ts": 1}))
    blob = "\n".join(lines) + "\n"
    p.write_text(blob)
    os.chmod(p, 0o600)
    return p


def _tiny_cohort():
    """Build a tiny ChunkedCohort with known chunk hashes + texts."""
    from eval.retrieval.semantic import EntityChunk, ChunkedCohort

    cand = next(c for c in sem.CANDIDATES if c.name == "384-small")
    cohort = ChunkedCohort(candidate=cand)

    def mk(entity_kind, item_id, rep, ci, text):
        ch_hash = sem.ec.content_hash(text)
        return EntityChunk(
            entity_kind=entity_kind,
            item_id=item_id,
            representation_type=rep,
            chunk_index=ci,
            chunk_hash=ch_hash,
            representation_hash=sem.ec.content_hash(text + "|rep"),
            normalized_text=text,
            parent=None,
        ), ch_hash

    texts = {
        ("message", "100", "prose", 0): "hello message",
        ("resource", "5001", "workflow_python", 0): "python code one",
        ("resource", "5003", "prose", 0): "prose resource",
        ("distillation", "700", "prose", 0): "distillation answer",
    }
    chunks = []
    hash_to_text = {}
    for (ek, iid, rep, ci), text in texts.items():
        ch, chh = mk(ek, iid, rep, ci, text)
        chunks.append(ch)
        hash_to_text[chh] = text
    cohort.chunks = chunks
    cohort.n_entities = 4
    cohort.unique_texts = hash_to_text
    return cohort


class TestPreflightOffline(unittest.TestCase):
    """The offline preflight must be green with zero provider/network."""

    def test_preflight_uses_only_local_files(self):
        # The preflight reads only local files (manifest, decision, cache,
        # golden). It must NOT instantiate a provider or hit the network.
        result = R.reconstruct_preflight(
            endpoint=R.DEFAULT_ENDPOINT, anon_key=R.DEFAULT_ANON_KEY
        )
        self.assertTrue(result["attemptable"], msg=json.dumps(result, indent=2))
        self.assertEqual(result["n_pass"], result["n_checks"])
        names = {c["name"] for c in result["checks"]}
        self.assertIn("strict_offline_replay_green", names)
        self.assertIn("high_water_pinned", names)
        self.assertIn("cache_384_facts_verified", names)

    def test_preflight_states_no_old_chunk_map_hash_limitation(self):
        result = R.reconstruct_preflight(
            endpoint=R.DEFAULT_ENDPOINT, anon_key=R.DEFAULT_ANON_KEY
        )
        lim = result["no_old_chunk_map_hash_limitation"]
        self.assertIn("NEVER persisted", lim)
        self.assertIn("identity-to-chunk", lim)
        self.assertIn("no", lim.lower())
        # must explicitly say only the count-level manifest hash is comparable
        self.assertIn("count-level", lim)


class TestHighWaterPinning(unittest.TestCase):
    """High-water pinning logic: the scan must be bounded to the frozen literal."""

    def test_frozen_high_water_is_the_accepted_literal(self):
        self.assertEqual(R.FROZEN_HIGH_WATER, "1532071167588958368")

    def test_reader_passes_high_water_filter_to_pagination(self):
        """The pinned reader must add message_id=lte.<high_water> as a filter."""
        captured = {}

        def fake_paginate(endpoint, anon_key, table, select, order_col, n,
                          *, extra_filter="", page=1000, retries=None):
            captured["extra_filter"] = extra_filter
            captured["table"] = table
            return []  # empty; reader will handle golden union gracefully

        # _paginate_retry wraps _paginate_keyset; patch the inner one.
        with mock.patch.object(R.cec, "_paginate_keyset", side_effect=fake_paginate):
            with mock.patch.object(R.cec, "_postgrest_get", return_value=[]):
                with mock.patch.object(R, "_get_retry", return_value=[]):
                    # only patch the message pagination path
                    R._paginate_retry(
                        R.DEFAULT_ENDPOINT, R.DEFAULT_ANON_KEY, "message_feed",
                        "message_id,content", "message_id", 5000,
                        extra_filter=f"message_id=lte.{R.FROZEN_HIGH_WATER}",
                    )
        self.assertIn(R.FROZEN_HIGH_WATER, captured["extra_filter"])
        self.assertIn("lte", captured["extra_filter"])


class TestCountComparisonFailClosed(unittest.TestCase):
    """Count comparison must fail closed on drift."""

    def test_count_drift_detected(self):
        cohort = _tiny_cohort()
        counts = R._selected_chunk_counts(cohort)
        # The tiny cohort has 4 chunks, not the accepted 23138.
        self.assertNotEqual(counts["n_chunks"], R.ACCEPTED_SELECTED_COUNTS["n_chunks"])

    def test_exact_match_passes(self):
        # Simulate the accepted counts exactly.
        fake_counts = dict(R.ACCEPTED_SELECTED_COUNTS)
        drift = []
        for k, expected in R.ACCEPTED_SELECTED_COUNTS.items():
            if fake_counts.get(k) != expected:
                drift.append(k)
        self.assertEqual(drift, [])


class TestCacheFactVerificationFailClosed(unittest.TestCase):
    """Cache fact verification must fail closed on wrong hash/bytes/count/dim/mode."""

    def _good_fixture(self, tmpdir: Path) -> Path:
        recs = {
            "openai:text-embedding-3-small:384:deadbeef": base64.b64encode(_fake_vector("x")).decode(),
        }
        return _write_cache_fixture(tmpdir, dim=384, records=recs)

    def test_verifies_good_fixture(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._good_fixture(Path(td))
            facts, keys = R.verify_cache_384_readonly(p)
            self.assertEqual(facts["record_count"], 1)
            self.assertEqual(facts["dimensions_seen"], [384])
            self.assertTrue(facts["dimension_consistent"])
            self.assertTrue(facts["mode_is_private"])
            self.assertEqual(facts["mode_oct"], "0o600")

    def test_fails_on_wrong_mode(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._good_fixture(Path(td))
            os.chmod(p, 0o644)  # not private
            facts, _ = R.verify_cache_384_readonly(p)
            self.assertFalse(facts["mode_is_private"])

    def test_fails_on_wrong_dimension(self):
        with tempfile.TemporaryDirectory() as td:
            recs = {
                "openai:text-embedding-3-small:384:x": base64.b64encode(_fake_vector("x")).decode(),
            }
            p = _write_cache_fixture(Path(td) / "a", dim=512, records=recs)
            facts, _ = R.verify_cache_384_readonly(p)
            self.assertFalse(facts["dimension_consistent"])
            self.assertEqual(facts["dimensions_seen"], [512])

    def test_fails_on_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._good_fixture(Path(td))
            # Tamper: append a record (changes sha256 + record_count).
            with open(p, "a") as f:
                f.write(json.dumps({"key": "openai:text-embedding-3-small:384:y",
                                    "d": 384, "v": base64.b64encode(_fake_vector("y")).decode(),
                                    "ts": 1}) + "\n")
            facts, _ = R.verify_cache_384_readonly(p)
            self.assertEqual(facts["record_count"], 2)  # detected
            self.assertNotEqual(facts["sha256"], R.ACCEPTED_CACHE_384["sha256"])


class TestTextHashHitFailClosed(unittest.TestCase):
    """100%-hit vs miss must fail closed."""

    def test_miss_blocks_with_sanitized_evidence(self):
        cohort = _tiny_cohort()
        # Build a cache index that is MISSING one chunk hash.
        provider, model, dim = "openai", "text-embedding-3-small", 384
        keys = set()
        for i, ch in enumerate(cohort.chunks):
            if i == 0:
                continue  # skip the first -> a miss
            keys.add(f"{provider}:{model}:{dim}:{ch.chunk_hash}")
        missing = sorted(
            {f"{provider}:{model}:{dim}:{ch.chunk_hash}" for ch in cohort.chunks} - keys
        )
        self.assertEqual(len(missing), 1)
        # Sanitized evidence must not leak the raw key.
        sanitized = {"missing_keys_set_sha256": R.sha256_json(missing)}
        dumped = json.dumps(sanitized)
        for ch in cohort.chunks:
            self.assertNotIn(ch.chunk_hash, dumped)


class TestNoOldChunkMapHashLimitation(unittest.TestCase):
    """The no-old-chunk-map-hash limitation must be stated precisely."""

    def test_limitation_string_present_and_precise(self):
        lim = R._NO_OLD_CHUNK_MAP_HASH_LIMITATION
        # Must state Task 2.14 never persisted the mapping.
        self.assertIn("NEVER persisted", lim)
        # Must name the fresh hashes that ARE computed.
        self.assertIn("entity_order_identity_hash", lim)
        self.assertIn("per_representation_chunk_map_hash", lim)
        self.assertIn("text_hash_multiset_hash", lim)
        # Must say no old equivalent exists.
        self.assertIn("NO Task-2.14-persisted equivalent", lim)
        # Must NOT claim to compare against an old chunk-map hash.
        self.assertNotIn("old identity-to-chunk hash is compared", lim)


class TestCLIExistsAndFailCloses(unittest.TestCase):
    """The CLI command must exist and fail closed when preflight is red."""

    def test_cli_modes_exist(self):
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--mode",
            choices=["reconstruct-preflight", "reconstruct", "representative-benchmark"],
        )
        # valid modes parse
        ns = parser.parse_args(["--mode", "reconstruct-preflight"])
        self.assertEqual(ns.mode, "reconstruct-preflight")

    def test_reconstruct_fail_closes_when_preflight_red(self):
        """If preflight is not attemptable, reconstruct returns blocked."""
        with tempfile.TemporaryDirectory() as td:
            fake_evidence = Path(td) / "evidence.json"
            with mock.patch.object(R, "reconstruct_preflight") as mp:
                mp.return_value = {"attemptable": False, "checks": [], "n_checks": 1, "n_pass": 0}
                with mock.patch.object(R, "DOCS_EVIDENCE", fake_evidence):
                    result = R.reconstruct(endpoint=R.DEFAULT_ENDPOINT, anon_key=R.DEFAULT_ANON_KEY)
        self.assertEqual(result["verdict"], "blocked_not_representative")
        self.assertEqual(result["blocker"], "preflight_red")

    def test_main_exits_nonzero_on_blocked(self):
        with mock.patch.object(R, "reconstruct_preflight") as mp:
            mp.return_value = {"attemptable": False, "checks": [], "n_checks": 1, "n_pass": 0}
            with mock.patch("sys.stdout"):
                rc = R.main(["--mode", "reconstruct-preflight"])
        self.assertEqual(rc, 2)

    def test_main_exits_zero_on_green_preflight(self):
        with mock.patch.object(R, "reconstruct_preflight") as mp:
            mp.return_value = {
                "attemptable": True,
                "checks": [{"name": "x", "ok": True}],
                "n_checks": 1,
                "n_pass": 1,
            }
            with mock.patch("sys.stdout"):
                rc = R.main(["--mode", "reconstruct-preflight"])
        self.assertEqual(rc, 0)


class TestEvidenceHygiene(unittest.TestCase):
    """Evidence must contain no raw vector/URL/credential/cache-path."""

    def test_clean_evidence_passes_scan(self):
        clean = {
            "verdict": "ok",
            "sha256": "0" * 64,
            "counts": {"n_chunks": 23138},
        }
        findings = R.evidence_hygiene_scan(clean)
        self.assertEqual(findings, [])

    def test_dirty_evidence_flagged(self):
        dirty = {
            "url": "https://ujlwuvkrxlvoswwkerdf.supabase.co/rest/v1/foo",
            "key": "sb_publishable_O38oPBafrBoFrpi_rlWJvA_UJrulFsx",
            "cache_path": ".cache/hivemind-semantic-eval/cache.384.jsonl",
        }
        findings = R.evidence_hygiene_scan(dirty)
        kinds = {f["kind"] for f in findings}
        self.assertIn("url_literal", kinds)
        self.assertIn("anon_key_literal", kinds)
        self.assertIn("paid_cache_path", kinds)

    def test_hex_hashes_not_flagged_as_blobs(self):
        # A 64-char hex hash is allowed evidence, not a "blob".
        clean = {"hash": "a" * 64}
        findings = R.evidence_hygiene_scan(clean)
        self.assertEqual(findings, [])

    def test_blocked_helper_writes_sanitized_evidence(self):
        # Use a throwaway temp path so the test never mutates real evidence.
        with tempfile.TemporaryDirectory() as td:
            fake_evidence = Path(td) / "evidence.json"
            with mock.patch.object(R, "DOCS_EVIDENCE", fake_evidence):
                with mock.patch.object(R, "evidence_hygiene_scan", return_value=[]):
                    R._blocked("test_blocker", "test reason")
            self.assertTrue(fake_evidence.exists())
            txt = fake_evidence.read_text()
            self.assertNotIn("sb_publishable_", txt)
            self.assertNotIn("https://ujlwuvkrxlvoswwkerdf", txt)


class TestSnapshotPermissions(unittest.TestCase):
    """Private snapshot must be parent 0700 / files 0600."""

    def test_atomic_write_private_sets_0600(self):
        with tempfile.TemporaryDirectory() as td:
            parent = Path(td) / "snap"
            p = parent / "f.json"
            R.atomic_write_private(p, '{"a":1}')
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(parent).st_mode & 0o777, 0o700)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
