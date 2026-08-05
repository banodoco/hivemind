"""Offline tests for the Hivemind embedding provider contract (plan task 2.1).

Pure and offline: no network, no database, no provider call, no key read, no
secret printed. These pin the provider interface, the deterministic fake
embedder, the OpenAI embedder's injectable transport + fail-closed credential
gate + wrong-dimension rejection + secret-safe errors, vector validation/L2
normalization, query normalization, the one-source-of-truth content hash, and
the deterministic embedding-contract identity (cross-language parity anchor for
the SQL seeding in schema/021).
"""

from __future__ import annotations

import asyncio
import math
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))

from executors import embedding_contract as ec  # noqa: E402
from executors import workflow_representation as wr  # noqa: E402


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------


class ProviderInterfaceTests(unittest.TestCase):
    def test_fake_satisfies_embedder_protocol(self):
        # runtime_checkable Protocol: structural check.
        self.assertIsInstance(ec.DeterministicFakeEmbedder(dimension=8), ec.Embedder)

    def test_openai_satisfies_embedder_protocol(self):
        self.assertIsInstance(ec.OpenAIEmbedder(api_key="ignored", dimension=8), ec.Embedder)

    def test_embedder_attrs(self):
        fake = ec.DeterministicFakeEmbedder(dimension=16)
        self.assertEqual(fake.model_name, "deterministic-fake")
        self.assertEqual(fake.dimension, 16)


# ---------------------------------------------------------------------------
# Deterministic fake embedder (offline, deterministic)
# ---------------------------------------------------------------------------


class FakeEmbedderTests(unittest.TestCase):
    def test_deterministic_same_input_same_vector(self):
        e = ec.DeterministicFakeEmbedder(dimension=64)
        a = _run(e.embed_texts(["WanVideoSampler with LoRA"]))
        b = _run(e.embed_texts(["WanVideoSampler with LoRA"]))
        self.assertEqual(a, b)
        self.assertEqual(len(a), 1)
        self.assertEqual(len(a[0]), 64)

    def test_preserves_order_and_count(self):
        e = ec.DeterministicFakeEmbedder(dimension=32)
        out = _run(e.embed_texts(["one", "two", "three"]))
        self.assertEqual(len(out), 3)
        self.assertTrue(all(len(v) == 32 for v in out))

    def test_vectors_are_l2_normalized(self):
        e = ec.DeterministicFakeEmbedder(dimension=32)
        out = _run(e.embed_texts(["some tokens here for a vector"]))
        norm = math.sqrt(sum(x * x for x in out[0]))
        self.assertAlmostEqual(norm, 1.0, places=9)

    def test_empty_input_returns_empty(self):
        e = ec.DeterministicFakeEmbedder(dimension=8)
        self.assertEqual(_run(e.embed_texts([])), [])

    def test_different_text_different_vector(self):
        e = ec.DeterministicFakeEmbedder(dimension=64)
        a = _run(e.embed_texts(["alpha"]))[0]
        b = _run(e.embed_texts(["omega"]))[0]
        self.assertNotEqual(a, b)

    def test_rejects_nonpositive_dimension(self):
        with self.assertRaises(ValueError):
            ec.DeterministicFakeEmbedder(dimension=0)


# ---------------------------------------------------------------------------
# Vector validation + L2 normalization
# ---------------------------------------------------------------------------


class VectorValidationTests(unittest.TestCase):
    def test_dimension_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            ec.normalize_vector([0.1, 0.2, 0.3], dimension=4)

    def test_nan_rejected(self):
        with self.assertRaises(ValueError):
            ec.normalize_vector([float("nan"), 0.5, 0.5], dimension=3)

    def test_inf_rejected(self):
        with self.assertRaises(ValueError):
            ec.normalize_vector([float("inf"), 0.5, 0.5], dimension=3)

    def test_all_zero_rejected(self):
        with self.assertRaises(ValueError):
            ec.normalize_vector([0.0, 0.0, 0.0], dimension=3)

    def test_l2_normalized(self):
        out = ec.normalize_vector([3.0, 4.0], dimension=2)
        self.assertAlmostEqual(out[0], 0.6, places=9)
        self.assertAlmostEqual(out[1], 0.8, places=9)
        self.assertAlmostEqual(sum(x * x for x in out), 1.0, places=9)

    def test_validate_vectors_batch(self):
        out = ec.validate_vectors([[3.0, 4.0], [1.0, 0.0]], dimension=2)
        self.assertEqual(len(out), 2)
        for v in out:
            self.assertAlmostEqual(sum(x * x for x in v), 1.0, places=9)


# ---------------------------------------------------------------------------
# Query normalization
# ---------------------------------------------------------------------------


class QueryNormalizationTests(unittest.TestCase):
    def test_collapses_internal_whitespace(self):
        self.assertEqual(ec.normalize_query_for_embedding("wan   2.2\tvideo"), "wan 2.2 video")

    def test_strips_and_collapses(self):
        self.assertEqual(ec.normalize_query_for_embedding("   WanVideoSampler  "), "WanVideoSampler")

    def test_blank_returns_empty(self):
        self.assertEqual(ec.normalize_query_for_embedding("   "), "")
        self.assertEqual(ec.normalize_query_for_embedding(""), "")

    def test_none_returns_empty(self):
        self.assertEqual(ec.normalize_query_for_embedding(None), "")

    def test_deterministic_cache_key(self):
        # Identical semantic content -> identical normalized key.
        a = ec.normalize_query_for_embedding("block swap\n\nconfiguration")
        b = ec.normalize_query_for_embedding("block swap configuration")
        self.assertEqual(a, b)


# ---------------------------------------------------------------------------
# One-source-of-truth content hash
# ---------------------------------------------------------------------------


class ContentHashTests(unittest.TestCase):
    def test_content_hash_equals_frozen_representation_hash(self):
        text = "Wan 2.2 image-to-video sampler"
        self.assertEqual(ec.content_hash(text), wr.representation_hash(text))

    def test_canonical_content_hash_normalizes(self):
        # CRLF vs LF must hash identically.
        self.assertEqual(
            ec.canonical_content_hash("line one\r\nline two"),
            ec.canonical_content_hash("line one\nline two"),
        )

    def test_chunk_hash_alias_consistent(self):
        self.assertEqual(ec.chunk_hash("a chunk"), wr.chunk_hash("a chunk"))


# ---------------------------------------------------------------------------
# OpenAI embedder: injectable transport + credential gate + safety
# ---------------------------------------------------------------------------


class OpenAIEmbedderTests(unittest.TestCase):
    def _fake_transport(self, vectors, *, dim):
        """Return a transport that yields the given vectors regardless of input."""
        captured = {}

        def transport(url, headers, body):
            captured["url"] = url
            captured["has_auth"] = "Authorization" in (headers or {})
            captured["body_model"] = body.get("model")
            captured["body_dim"] = body.get("dimensions")
            captured["n_inputs"] = len(body.get("input", []))
            data = [
                {"index": i, "embedding": vec}
                for i, vec in enumerate(vectors)
            ]
            # Shuffle to prove the embedder re-sorts by index.
            import random  # deterministic within the test process
            data = list(data)
            random.Random(0).shuffle(data)
            return {"data": data}

        return transport, captured

    def test_embeds_through_injected_transport_and_sorts_by_index(self):
        dim = 4
        vecs = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]]
        transport, cap = self._fake_transport(vecs, dim=dim)
        e = ec.OpenAIEmbedder(api_key="sk-test-not-real", dimension=dim, transport=transport)
        out = _run(e.embed_texts(["a", "b"]))
        self.assertEqual(out, [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]])
        # The Authorization header was set from the injected key (never printed).
        self.assertTrue(cap["has_auth"])
        self.assertEqual(cap["body_dim"], dim)
        self.assertEqual(cap["n_inputs"], 2)

    def test_no_credential_fails_closed(self):
        e = ec.OpenAIEmbedder(api_key=None, dimension=4)  # no env key in tests
        with self.assertRaises(ec.EmbeddingError):
            _run(e.embed_texts(["a"]))

    def test_missing_data_array_raises(self):
        def transport(url, headers, body):
            return {"error": "something"}
        e = ec.OpenAIEmbedder(api_key="sk-test-not-real", dimension=4, transport=transport)
        with self.assertRaises(ec.EmbeddingError):
            _run(e.embed_texts(["a"]))

    def test_count_mismatch_raises(self):
        def transport(url, headers, body):
            return {"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0]}]}
        e = ec.OpenAIEmbedder(api_key="sk-test-not-real", dimension=4, transport=transport)
        with self.assertRaises(ec.EmbeddingError):
            _run(e.embed_texts(["a", "b"]))  # asked for 2, got 1

    def test_wrong_dimension_from_provider_rejected(self):
        def transport(url, headers, body):
            # Provider returns 3-d vectors into a 4-d contract.
            return {"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}]}
        e = ec.OpenAIEmbedder(api_key="sk-test-not-real", dimension=4, transport=transport)
        with self.assertRaises(ValueError):
            _run(e.embed_texts(["a"]))

    def test_empty_input_no_transport_call(self):
        called = {"n": 0}

        def transport(url, headers, body):
            called["n"] += 1
            return {"data": []}
        e = ec.OpenAIEmbedder(api_key="sk-test-not-real", dimension=4, transport=transport)
        self.assertEqual(_run(e.embed_texts([])), [])
        self.assertEqual(called["n"], 0)

    def test_has_credential_does_not_leak_value(self):
        e = ec.OpenAIEmbedder(api_key="sk-test-not-real", dimension=4)
        self.assertTrue(e.has_credential())
        # The public object state must not expose the key string.
        import inspect
        src = inspect.getsource(type(e))
        self.assertNotIn("sk-test-not-real", repr(e))

    def test_stdlib_transport_wraps_http_error_secret_free(self):
        # Monkeypatch urlopen to raise a constructed HTTPError (no network).
        import io
        import urllib.error
        import urllib.request
        err = urllib.error.HTTPError(
            url="https://api.openai.invalid/v1/embeddings",
            code=401,
            msg="Unauthorized",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b'{"error":{"message":"secret-ish-body"}}'),
        )
        orig = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(err)
        try:
            raised = False
            try:
                ec._stdlib_transport(
                    "https://api.openai.invalid/v1/embeddings",
                    {"Authorization": "Bearer x"},
                    {"model": "x", "input": ["a"], "dimensions": 4},
                )
            except ec.EmbeddingError as exc:
                raised = True
                msg = str(exc)
                self.assertIn("401", msg)  # status surfaced
                # The response body (could carry key context) is never surfaced.
                self.assertNotIn("secret-ish-body", msg)
            self.assertTrue(raised)
        finally:
            urllib.request.urlopen = orig

    def test_stdlib_transport_wraps_url_error_secret_free(self):
        import urllib.error
        import urllib.request
        err = urllib.error.URLError("transient dns failure")
        orig = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(err)
        try:
            with self.assertRaises(ec.EmbeddingError):
                ec._stdlib_transport(
                    "https://api.openai.invalid/v1/embeddings",
                    {},
                    {"model": "x", "input": ["a"], "dimensions": 4},
                )
        finally:
            urllib.request.urlopen = orig


# ---------------------------------------------------------------------------
# Embedding contract identity (cross-language parity anchor)
# ---------------------------------------------------------------------------


class ContractIdentityTests(unittest.TestCase):
    def test_id_is_deterministic_and_stable(self):
        spec = ec.ContractSpec(provider="openai", model="text-embedding-3-small", dimension=384)
        self.assertEqual(spec.id, spec.id)
        self.assertEqual(contract_id_ref(), spec.id)

    def test_identical_specs_share_id(self):
        a = ec.ContractSpec(provider="openai", model="m", dimension=384)
        b = ec.ContractSpec(provider="openai", model="m", dimension=384)
        self.assertEqual(a.id, b.id)

    def test_different_dimension_different_id(self):
        a = ec.ContractSpec(provider="openai", model="m", dimension=384)
        b = ec.ContractSpec(provider="openai", model="m", dimension=1536)
        self.assertNotEqual(a.id, b.id)

    def test_different_canon_version_different_id(self):
        a = ec.ContractSpec(provider="openai", model="m", dimension=384, canonicalization_version=1)
        b = ec.ContractSpec(provider="openai", model="m", dimension=384, canonicalization_version=2)
        self.assertNotEqual(a.id, b.id)

    def test_id_is_positive_bigint(self):
        spec = ec.ContractSpec(provider="openai", model="m", dimension=384)
        self.assertGreater(spec.id, 0)
        self.assertLess(spec.id, 2 ** 63)

    def test_identity_input_uses_unit_separator(self):
        spec = ec.ContractSpec(provider="openai", model="m", dimension=384)
        self.assertEqual(
            spec.identity_input,
            "openai\x1fm\x1f384\x1f1\x1f2",
        )

    def test_mapping_input_matches_dataclass_input(self):
        spec = ec.ContractSpec(provider="openai", model="m", dimension=384)
        mapping = {
            "provider": "openai", "model": "m", "dimension": 384,
            "canonicalization_version": 1, "chunking_version": 2,
        }
        self.assertEqual(ec.contract_identity_input(spec), ec.contract_identity_input(mapping))
        self.assertEqual(ec.contract_id(spec), ec.contract_id(mapping))

    def test_field_order_collision_resistance(self):
        # "ab"+"c" vs "a"+"bc" must not collide: the separator prevents it.
        a = ec.ContractSpec(provider="ab", model="c", dimension=8)
        b = ec.ContractSpec(provider="a", model="bc", dimension=8)
        self.assertNotEqual(a.id, b.id)

    def test_pilot_specs_cover_both_dimensions(self):
        specs = ec.pilot_contract_specs()
        dims = sorted(s.dimension for s in specs)
        self.assertEqual(dims, [384, 1536])
        for s in specs:
            self.assertEqual(s.provider, "openai")
            self.assertEqual(s.model, ec.DEFAULT_OPENAI_EMBEDDING_MODEL)

    def test_contract_roundtrip(self):
        spec = ec.ContractSpec(provider="openai", model="m", dimension=384)
        contract = ec.EmbeddingContract.from_spec(spec, status=ec.CONTRACT_STATUS_DRAFT)
        self.assertEqual(contract.id, spec.id)
        self.assertEqual(contract.status, ec.CONTRACT_STATUS_DRAFT)
        self.assertEqual(contract.to_spec(), spec)


def contract_id_ref() -> int:
    """Independent recompute of the 384-d pilot contract id (parity anchor)."""
    import hashlib
    preimage = "openai\x1ftext-embedding-3-small\x1f384\x1f1\x1f2".encode("utf-8")
    digest = hashlib.sha256(preimage).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF


# ---------------------------------------------------------------------------
# Best-effort query cache
# ---------------------------------------------------------------------------


class QueryCacheTests(unittest.TestCase):
    def setUp(self):
        ec._cache_clear()

    def test_store_then_lookup(self):
        ec._cache_store("deterministic-fake", "q", [0.1, 0.2])
        self.assertEqual(ec._cache_lookup("deterministic-fake", "q"), [0.1, 0.2])

    def test_miss_returns_none(self):
        self.assertIsNone(ec._cache_lookup("deterministic-fake", "absent"))

    def test_clear(self):
        ec._cache_store("deterministic-fake", "q", [0.1])
        ec._cache_clear()
        self.assertIsNone(ec._cache_lookup("deterministic-fake", "q"))


if __name__ == "__main__":
    unittest.main()
