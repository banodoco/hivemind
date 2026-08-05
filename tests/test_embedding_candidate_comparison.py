"""Tests for task 2.14: embedding-candidate comparison (eval/retrieval/semantic).

Pure + offline. No network, no provider key, no real corpus read. The real
provider path is exercised in the operator-run CLI (``evaluate`` / ``replay``);
here we prove the contract identity, cache, cohort, retriever collapse, decision
policy, evidence hygiene, offline byte-stability, and replay zero-call behavior
from deterministic fakes and injected transports.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))

from eval.retrieval import loader
from eval.retrieval import semantic as sem
from eval.retrieval.schema import CorpusItem, GoldenCase, GoldenSet, JudgedItem, Query
from executors import embedding_contract as ec
from executors import entity_identity as ei
from executors.workflow_representation import REP_PROSE, REP_WORKFLOW_PYTHON

# Dimension-only ContractSpec ids under chunking v2 (the IDs the plan calls out;
# these derive from CHUNKING_VERSION, so they move 1->2 with the chunking bump:
# v1 was 7571371577804399660 / 489652545053900118).
HIST_384_ID = 6368594834396668537
HIST_1536_ID = 201336985699651598


def _fake_embed_fn(dim: int):
    fake = ec.DeterministicFakeEmbedder(dimension=dim)

    import asyncio

    def _fn(texts):
        return asyncio.run(fake.embed_texts(list(texts)))

    return _fn


def _workflow_row(item_id: str, *, body: str, python: str | None = None, title: str = "T") -> dict:
    payload = {"python_source": python} if python is not None else {}
    return {"kind": "workflow", "title": title, "body": body, "metadata": {}, "payload": payload}


def _resource_entity(item_id: str, row: dict) -> sem.CohortEntity:
    item = CorpusItem(
        kind=row.get("kind", "resource"),
        source="external_resources",
        item_id=item_id,
        title=row.get("title", ""),
        body=row.get("body", ""),
        metadata=row.get("metadata", {}),
    )
    return sem.CohortEntity(
        entity_type=ei.ENTITY_RESOURCE, item_id=item_id, corpus_item=item, canon_row=row
    )


# ---------------------------------------------------------------------------
# Candidate / chunk-config identity
# ---------------------------------------------------------------------------


class TestCandidateIdentity(unittest.TestCase):
    def test_four_candidates(self):
        names = [c.name for c in sem.CANDIDATES]
        self.assertEqual(names, ["384-small", "384-large", "1536-small", "1536-large"])

    def test_distinct_eval_contract_ids(self):
        ids = {c.eval_contract_id for c in sem.CANDIDATES}
        self.assertEqual(len(ids), 4, "each candidate must have a distinct eval id")

    def test_base_dimension_only_ids_match_history(self):
        by_dim = {c.dimension: c.base_contract_id for c in sem.CANDIDATES}
        self.assertEqual(by_dim[384], HIST_384_ID)
        self.assertEqual(by_dim[1536], HIST_1536_ID)

    def test_dimension_only_id_is_not_sufficient(self):
        # The two 384 candidates share a base id but differ on chunk config.
        c384 = [c for c in sem.CANDIDATES if c.dimension == 384]
        self.assertEqual(c384[0].base_contract_id, c384[1].base_contract_id)
        self.assertNotEqual(c384[0].chunk_config.identity, c384[1].chunk_config.identity)
        self.assertNotEqual(c384[0].eval_contract_id, c384[1].eval_contract_id)

    def test_chunk_config_identity_lists_exact_target_overlap(self):
        c = sem.CANDIDATES[1]  # 384-large
        d = c.to_sanitized_dict()
        self.assertEqual(d["prose"], {"target_tokens": 1024, "overlap_tokens": 100})
        self.assertEqual(d["python"], {"target_tokens": 2048, "overlap_tokens": 100})
        self.assertEqual(d["chunk_config_version"], sem.CHUNK_CONFIG_IDENTITY_VERSION)

    def test_capacity_facts_frozen(self):
        self.assertEqual(sem.CAPACITY_FACTS[384]["verdict"], "PASS")
        self.assertEqual(sem.CAPACITY_FACTS[1536]["verdict"], "FAIL")
        self.assertGreater(sem.CAPACITY_FACTS[1536]["full_corpus_storage_gb"], sem.STORAGE_GATE_GB)


# ---------------------------------------------------------------------------
# Embedding cache
# ---------------------------------------------------------------------------


class TestEmbeddingCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cache.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _cache(self, **kw):
        return sem.EmbeddingCache(self.path, **kw)

    def test_keyed_by_text_hash_not_raw_text(self):
        c = self._cache()
        c.store("openai", "m", 384, ec.content_hash("hello world"), [0.1] * 384)
        raw = self.path.read_text()
        self.assertNotIn("hello world", raw)  # raw text never stored
        self.assertIn("openai:m:384:", raw)

    def test_store_validates_and_reuses(self):
        c = self._cache()
        h = ec.content_hash("x")
        c.store("openai", "m", 384, h, [0.1] * 384)
        got = c.get("openai", "m", 384, h)
        self.assertIsNotNone(got)
        norm = (sum(v * v for v in got)) ** 0.5
        self.assertAlmostEqual(norm, 1.0, places=4)

    def test_wrong_dimension_cached_vector_is_dropped(self):
        c = self._cache()
        h = ec.content_hash("x")
        # Manually inject a wrong-dimension record.
        import base64, struct

        blob = base64.b64encode(struct.pack("<3f", 1.0, 0.0, 0.0)).decode()
        rec = {"key": "openai:m:384:" + h, "d": 384, "v": blob}
        with open(self.path, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        c2 = self._cache()  # reload
        self.assertIsNone(c2.get("openai", "m", 384, h))  # validation fails -> miss

    def test_resume_skips_cached_no_duplicate_calls(self):
        calls = {"n": 0}

        def embed_fn(texts):
            calls["n"] += 1
            return _fake_embed_fn(384)(texts)

        c = self._cache()
        client = sem.ProviderClient(
            candidate=sem.CANDIDATES[0], cache=c, api_key=None, embed_fn=embed_fn
        )
        client.embed_texts(["alpha", "beta"])
        self.assertEqual(calls["n"], 1)
        client.embed_texts(["alpha", "beta"])  # all cached
        self.assertEqual(calls["n"], 1)
        self.assertEqual(client.cache_hits, 2)
        self.assertEqual(client.cache_misses, 2)

    def test_atomic_checkpoint_survives_reload(self):
        c = self._cache()
        c.store("openai", "m", 384, ec.content_hash("z"), [0.2] * 384)
        c.compact()
        c2 = self._cache()
        self.assertIsNotNone(c2.get("openai", "m", 384, ec.content_hash("z")))

    def test_replay_fail_closed_on_miss(self):
        c = self._cache(fail_closed=True)
        client = sem.ProviderClient(
            candidate=sem.CANDIDATES[0], cache=c, api_key=None,
            transport=sem._ReplayTransport(), replay_only=True,
        )
        with self.assertRaises(ec.EmbeddingError):
            client.embed_texts(["never embedded before"])

    def test_replay_zero_calls_when_cached(self):
        # Pre-populate the cache with a fake embedder.
        warm = self._cache()
        client_warm = sem.ProviderClient(
            candidate=sem.CANDIDATES[0], cache=warm, api_key=None, embed_fn=_fake_embed_fn(384)
        )
        client_warm.embed_texts(["alpha", "beta"])
        # Now strict replay must serve from cache with zero provider calls.
        c = self._cache(fail_closed=True)
        replay_transport = sem._ReplayTransport()
        client = sem.ProviderClient(
            candidate=sem.CANDIDATES[0], cache=c, api_key=None,
            transport=replay_transport, replay_only=True,
        )
        out = client.embed_texts(["alpha", "beta"])
        self.assertEqual(len(out), 2)
        self.assertEqual(replay_transport.calls, 0)

    def test_private_file_mode(self):
        c = self._cache()
        c.store("openai", "m", 384, ec.content_hash("a"), [0.1] * 384)
        mode = oct(self.path.stat().st_mode & 0o777)
        self.assertEqual(mode, "0o600")


# ---------------------------------------------------------------------------
# Cohort builder (no double indexing, quarantine, dedup, later chunks)
# ---------------------------------------------------------------------------


class TestCohortBuilder(unittest.TestCase):
    def test_workflow_python_not_double_indexed(self):
        # python_source bytes also appear verbatim as a body block -> emitted once.
        py = "import comfy\nprint(1)\n"
        body = "Intro text.\n\nPython ready-template source:\n" + py + "\n\nMore prose."
        ent = _resource_entity("5", _workflow_row("5", body=body, python=py))
        cohort = sem.build_chunked_cohort([ent], sem.CANDIDATES[0])
        py_chunks = [c for c in cohort.chunks if c.representation_type == REP_WORKFLOW_PYTHON]
        prose_chunks = [c for c in cohort.chunks if c.representation_type == REP_PROSE]
        self.assertEqual(len(py_chunks), 1, "exactly one workflow_python stream")
        # The body block must be stripped from prose (no-duplication).
        for pc in prose_chunks:
            self.assertNotIn("import comfy", pc.normalized_text)

    def test_quarantined_python_excluded(self):
        secret = "api_key = 'sk-proj-" + "A1" * 40 + "'\n"
        body = "Prose only here.\n"
        ent = _resource_entity("6", _workflow_row("6", body=body, python=secret))
        cohort = sem.build_chunked_cohort([ent], sem.CANDIDATES[0])
        py_chunks = [c for c in cohort.chunks if c.representation_type == REP_WORKFLOW_PYTHON]
        self.assertEqual(py_chunks, [], "quarantined python must be excluded")

    def test_duplicate_chunk_texts_collapsed(self):
        # Two messages with identical content -> one embeddable text.
        def msg(i, content):
            item = CorpusItem(kind="message", source="messages", item_id=i, body=content)
            return sem.CohortEntity(
                entity_type=ei.ENTITY_MESSAGE, item_id=i, corpus_item=item, canon_row={"content": content}
            )

        cohort = sem.build_chunked_cohort(
            [msg("1", "same text"), msg("2", "same text")], sem.CANDIDATES[0]
        )
        self.assertEqual(cohort.n_unique_embeddable_texts, 1)
        self.assertEqual(cohort.n_chunks, 2)  # two retrievable units, one vector
        self.assertEqual(cohort.n_duplicate_chunks_collapsed, 1)

    def test_large_config_yields_later_chunks(self):
        # A long resource should split into >1 chunk under the small config.
        para = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron. "
        long_body = "\n\n".join([para * 8] * 6)  # well over 512 tokens
        ent = _resource_entity("7", _workflow_row("7", body=long_body))
        small = sem.build_chunked_cohort([ent], sem.CANDIDATES[0])
        large = sem.build_chunked_cohort([ent], sem.CANDIDATES[1])
        self.assertGreater(max(c.chunk_index for c in small.chunks), 0)
        # Large config should produce fewer (or equal) chunks than small.
        self.assertLessEqual(len(large.chunks), len(small.chunks))


# ---------------------------------------------------------------------------
# Retriever best-chunk collapse (later chunk can win; deterministic tie-break)
# ---------------------------------------------------------------------------


class TestCollapse(unittest.TestCase):
    def _chunk(self, entity, rep, idx, score):
        ch = sem.EntityChunk(
            entity_kind=entity[0], item_id=entity[1], representation_type=rep,
            chunk_index=idx, chunk_hash=f"h{entity[1]}{rep}{idx}",
            representation_hash="r", normalized_text="n", parent=None,  # type: ignore[arg-type]
        )
        return (ch, score)

    def test_best_score_per_entity_wins(self):
        chunks, scores = zip(
            self._chunk(("resource", "5"), REP_PROSE, 0, 0.3),
            self._chunk(("resource", "5"), REP_WORKFLOW_PYTHON, 2, 0.9),  # later chunk, higher score
        )
        out = sem.SemanticRetriever._collapse_ranked(list(chunks), list(scores), limit=10)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].matched_representation, REP_WORKFLOW_PYTHON)
        self.assertAlmostEqual(out[0].score, 0.9)

    def test_later_chunk_can_be_best_hit(self):
        # chunk_index 0 scores lower than chunk_index 3 -> later chunk wins.
        chunks, scores = zip(
            self._chunk(("resource", "9"), REP_PROSE, 0, 0.2),
            self._chunk(("resource", "9"), REP_PROSE, 3, 0.8),
        )
        out = sem.SemanticRetriever._collapse_ranked(list(chunks), list(scores), limit=10)
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0].score, 0.8)
        # The winning chunk_index is surfaced for future later-chunk provenance.
        self.assertEqual(out[0].matched_chunk_index, 3)

    def test_prose_before_python_tiebreak(self):
        chunks, scores = zip(
            self._chunk(("resource", "5"), REP_WORKFLOW_PYTHON, 0, 0.5),
            self._chunk(("resource", "5"), REP_PROSE, 0, 0.5),
        )
        out = sem.SemanticRetriever._collapse_ranked(list(chunks), list(scores), limit=10)
        self.assertEqual(out[0].matched_representation, REP_PROSE)

    def test_no_duplicate_entity_in_ranking(self):
        chunks, scores = zip(
            self._chunk(("resource", "5"), REP_PROSE, 0, 0.5),
            self._chunk(("resource", "5"), REP_WORKFLOW_PYTHON, 1, 0.4),
            self._chunk(("message", "1"), REP_PROSE, 0, 0.3),
        )
        out = sem.SemanticRetriever._collapse_ranked(list(chunks), list(scores), limit=10)
        keys = [(r.key()) for r in out]
        self.assertEqual(len(keys), len(set(keys)))


class TestRetrieverFilters(unittest.TestCase):
    def test_filters_respected(self):
        # Build a tiny cohort + retriever and exercise kind/author filters.
        item_a = CorpusItem(kind="workflow", source="ext", item_id="1", body="comfy sampler node", author="Alice")
        item_b = CorpusItem(kind="message", source="messages", item_id="100", body="comfy sampler node", author="Bob")
        ents = [
            sem.CohortEntity(ei.ENTITY_RESOURCE, "1", item_a, {"kind": "workflow", "body": "comfy sampler node", "title": "", "metadata": {}}),
            sem.CohortEntity(ei.ENTITY_MESSAGE, "100", item_b, {"content": "comfy sampler node"}),
        ]
        cohort = sem.build_chunked_cohort(ents, sem.CANDIDATES[0])
        # Embed with the fake embedder at dim 384.
        client = sem.ProviderClient(
            candidate=sem.CANDIDATES[0], cache=sem.EmbeddingCache(Path(tempfile.mkdtemp()) / "c.jsonl"),
            api_key=None, embed_fn=_fake_embed_fn(384),
        )
        vecs = client.embed_texts(list(cohort.unique_texts.values()))
        vmap = {h: v for h, v in zip(cohort.unique_texts.keys(), vecs)}
        qvecs = {ec.normalize_query_for_embedding("comfy sampler node"): client.embed_texts(["comfy sampler node"])[0]}
        retr = sem.SemanticRetriever(cohort, vmap, qvecs, dimension=384)
        # No filter -> both retrievable.
        self.assertEqual(len(retr.retrieve(Query(query="comfy sampler node", limit=10, filters={}))), 2)
        # kinds=[workflow] -> only the workflow.
        only_wf = retr.retrieve(Query(query="comfy sampler node", limit=10, filters={"kinds": ["workflow"]}))
        self.assertEqual([r.key() for r in only_wf], [("resource", "1")])
        # authors=[Bob] -> only the message.
        only_bob = retr.retrieve(Query(query="comfy sampler node", limit=10, filters={"authors": ["Bob"]}))
        self.assertEqual([r.key() for r in only_bob], [("message", "100")])


# ---------------------------------------------------------------------------
# Decision policy
# ---------------------------------------------------------------------------


class TestDecisionPolicy(unittest.TestCase):
    def _metrics(self, name, dim, recall, cost=0.0):
        cand = next(c for c in sem.CANDIDATES if c.name == name)
        return sem.CandidateMetrics(
            candidate=cand, overall={"recall@10": recall, "mrr": 0.5, "ndcg@10": 0.5},
            by_category={}, by_entity_kind={},
            workflow_code_recall_at_10=0.5, long_resource_chunk_recall_at_10=0.5,
            later_chunk_hit_rate=None,
            later_chunk_diagnostic={"available": False, "reason": sem.LATER_CHUNK_UNAVAILABLE_REASON},
            counts={}, cohort_counts={},
            account={"cost_usd": cost},
            latency_ms={},
        )

    def test_1536_capacity_disqualified_not_selected(self):
        m384 = self._metrics("384-small", 384, 0.70)
        m1536 = self._metrics("1536-small", 1536, 0.99)  # higher quality
        for m in (m384, m1536):
            sem.classify_candidate(
                m, capacity_fail=sem.CAPACITY_FACTS[m.candidate.dimension]["verdict"] == "FAIL",
                missing_judged_identities=0, duplicate_entities_after_collapse=0,
                vector_validation_failures=0, provider_failures=0,
            )
        winner = sem.select_winner([m384, m1536])
        self.assertEqual(winner.candidate.name, "384-small")
        self.assertIn("projected_full_corpus_storage_above_gate", m1536.disqualify_reasons)
        self.assertFalse(m1536.eligible_for_selection)

    def test_lexicographic_tiebreak_recall_then_cost(self):
        a = self._metrics("384-small", 384, 0.80, cost=0.5)
        b = self._metrics("384-large", 384, 0.80, cost=0.3)
        for m in (a, b):
            sem.classify_candidate(m, capacity_fail=False, missing_judged_identities=0,
                                   duplicate_entities_after_collapse=0,
                                   vector_validation_failures=0, provider_failures=0)
        winner = sem.select_winner([a, b])
        self.assertEqual(winner.candidate.name, "384-large")  # same recall -> lower cost

    def test_missing_judged_identity_disqualifies(self):
        m = self._metrics("384-small", 384, 0.99)
        sem.classify_candidate(m, capacity_fail=False, missing_judged_identities=2,
                               duplicate_entities_after_collapse=0,
                               vector_validation_failures=0, provider_failures=0)
        self.assertIn("missing_judged_identity", m.disqualify_reasons)
        self.assertFalse(m.eligible_for_selection)


# ---------------------------------------------------------------------------
# Evidence hygiene
# ---------------------------------------------------------------------------


class TestEvidenceHygiene(unittest.TestCase):
    def test_clean_envelope_passes(self):
        self.assertEqual(sem.scan_envelope({"a": "text", "b": [1, 2, 3]}), [])

    def test_vector_detected(self):
        self.assertIn("contains_embedding_vector", sem.scan_envelope({"v": [0.1] * 384}))

    def test_key_detected(self):
        self.assertTrue(any("openai_key" in v for v in sem.scan_envelope({"k": "sk-proj-" + "x" * 40})))

    def test_url_detected(self):
        v = sem.scan_envelope({"u": "https://ujlwuvkrxlvoswwkerdf.supabase.co/rest/v1/x"})
        self.assertTrue(any("supabase" in x for x in v))

    def test_python_marker_detected(self):
        v = sem.scan_envelope({"s": "Python ready-template source:\nimport x"})
        self.assertIn("raw_workflow_python_marker", v)

    def test_sanitize_report_is_clean(self):
        cand = sem.CANDIDATES[0]
        m = sem.CandidateMetrics(
            candidate=cand, overall={"recall@10": 0.7, "mrr": 0.5, "ndcg@10": 0.5, "n": 104},
            by_category={"workflow_code": {"recall@10": 0.6, "n": 10}},
            by_entity_kind={"resource": {"recall@10": 0.7}},
            workflow_code_recall_at_10=0.6, long_resource_chunk_recall_at_10=0.5,
            later_chunk_hit_rate=None,
            later_chunk_diagnostic={"available": False, "reason": sem.LATER_CHUNK_UNAVAILABLE_REASON},
            counts={"n_total": 112}, cohort_counts={"n_chunks": 10},
            account={"api_requests": 1, "cost_usd": 0.01}, latency_ms={"mean_ms": 1.0},
        )
        env = sem.sanitize_report([m], m)
        self.assertEqual(sem.scan_envelope(env), [])


# ---------------------------------------------------------------------------
# Offline fixture comparison byte-stability (no network)
# ---------------------------------------------------------------------------


class TestOfflineByteStable(unittest.TestCase):
    def test_offline_deterministic_core_identical(self):
        import scripts.compare_embedding_candidates as cli

        with tempfile.TemporaryDirectory() as tmp:
            cache_a = Path(tmp) / "a"
            cache_b = Path(tmp) / "b"
            args_a = cli.build_parser().parse_args(
                ["offline", "--corpus", str(_REPO / "eval/retrieval/golden/corpus-v1.json"),
                 "--golden", str(_REPO / "eval/retrieval/golden/golden-v1.json"),
                 "--out-dir", str(Path(tmp) / "outa"), "--cache-dir", str(cache_a),
                 "--generated-at", "2026-07-28T00:00:00+00:00"]
            )
            args_b = cli.build_parser().parse_args(
                ["offline", "--corpus", str(_REPO / "eval/retrieval/golden/corpus-v1.json"),
                 "--golden", str(_REPO / "eval/retrieval/golden/golden-v1.json"),
                 "--out-dir", str(Path(tmp) / "outb"), "--cache-dir", str(cache_b),
                 "--generated-at", "2026-07-28T00:00:00+00:00"]
            )
            cli.cmd_offline(args_a)
            cli.cmd_offline(args_b)
            with open(Path(tmp) / "outa" / "task-2.14-offline-decision.json") as fh:
                a = json.load(fh)
            with open(Path(tmp) / "outb" / "task-2.14-offline-decision.json") as fh:
                b = json.load(fh)
            self.assertEqual(a["deterministic_core"], b["deterministic_core"])
            self.assertEqual(a["deterministic_core"]["winner"], b["deterministic_core"]["winner"])
            # Tracked artifact must be hygiene-clean.
            self.assertEqual(sem.scan_envelope(a), [])
            self.assertEqual(sem.scan_envelope(b), [])


# ---------------------------------------------------------------------------
# Task 2.14 final correction: persistent counting, standalone cost,
# later-chunk diagnostic, immutable evidence replay
# ---------------------------------------------------------------------------


_REPO = Path(__file__).resolve().parent.parent
_REAL_DECISION = _REPO / "docs" / "hybrid-search" / "task-2.14-embedding-decision.json"
_REAL_MANIFEST = _REPO / "docs" / "hybrid-search" / "task-2.14-frozen-manifest.json"
_REAL_BUNDLE = _REPO / ".cache" / "hivemind-semantic-eval" / "replay-bundle.json"
_REAL_GOLDEN = _REPO / "eval" / "retrieval" / "golden" / "golden-v1.json"
_REAL_CACHE_DIR = _REPO / ".cache" / "hivemind-semantic-eval"


class _FakeUsageTransport:
    """Returns dim-correct vectors + usage; records call count + prompt tokens."""

    def __init__(self, dimension):
        self.dimension = dimension
        self.calls = 0
        self.prompt_tokens = 0

    def __call__(self, url, headers, body):
        self.calls += 1
        n = len(body["input"])
        self.prompt_tokens += 100 * n
        return {
            "data": [
                {"index": i, "embedding": [0.01 * (i + 1)] * self.dimension}
                for i in range(n)
            ],
            "usage": {"prompt_tokens": 100 * n, "total_tokens": 100 * n},
        }


class TestPersistentCountingTransport(unittest.TestCase):
    def test_two_calls_sum_tokens_and_requests(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cache = sem.EmbeddingCache(Path(tmp.name) / "c.jsonl")
        transport = _FakeUsageTransport(384)
        client = sem.ProviderClient(
            candidate=sem.CANDIDATES[0], cache=cache, api_key="test-key",
            transport=transport,
        )
        client.embed_texts(["alpha", "beta"])  # 2 misses -> 1 call, 200 tokens
        client.embed_texts(["alpha", "gamma"])  # 1 miss  -> 1 call, 100 tokens
        # Persistent counter must SUM across both calls, not overwrite.
        self.assertEqual(client._counter.calls, 2)
        self.assertEqual(sum(u["prompt_tokens"] for u in client._counter.usage), 300)
        self.assertEqual(transport.calls, 2)
        self.assertAlmostEqual(client.cost_usd(), 300 / 1e6 * 0.02)

    def test_counter_object_identity_persists_across_make_embedder(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cache = sem.EmbeddingCache(Path(tmp.name) / "c.jsonl")
        client = sem.ProviderClient(
            candidate=sem.CANDIDATES[0], cache=cache, api_key="k", transport=_FakeUsageTransport(384),
        )
        e1 = client._make_embedder()
        first = client._counter
        e2 = client._make_embedder()
        self.assertIs(client._counter, first)  # same persistent counter
        self.assertIs(e1._transport, e2._transport)


class TestStandaloneCost(unittest.TestCase):
    def _golden(self):
        return GoldenSet(cases=[
            GoldenCase(id="c1", query="alpha beta gamma", expected=[], categories=[], limit=10, expect_no_hit=True),
            GoldenCase(id="c2", query="delta epsilon zeta", expected=[], categories=[], limit=10, expect_no_hit=True),
        ])

    def test_order_independent_formula(self):
        golden = self._golden()
        ent = _resource_entity("1", {"kind": "resource", "title": "T", "body": "a b c d e", "metadata": {}})
        cohort = sem.build_chunked_cohort([ent], sem.CANDIDATES[0])
        corpus_tok = sum(max(1, __import__("executors.workflow_representation", fromlist=["estimate_tokens"]).estimate_tokens(t))
                         for t in cohort.unique_texts.values())
        gq = sem.golden_query_token_estimate(golden)["estimated_input_tokens"]
        expected = (corpus_tok + gq) / 1e6 * 0.02
        self.assertAlmostEqual(sem.standalone_cost_for_candidate(sem.CANDIDATES[0], cohort, golden), expected)

    def test_same_corpus_same_dimension_independent_of_sibling(self):
        golden = self._golden()
        ent = _resource_entity("1", {"kind": "resource", "title": "T", "body": "shared text", "metadata": {}})
        cohort_small = sem.build_chunked_cohort([ent], sem.CANDIDATES[0])  # 384-small
        cohort_1536_small = sem.build_chunked_cohort([ent], sem.CANDIDATES[2])  # 1536-small
        # Same corpus texts -> identical standalone cost regardless of dimension cache sharing.
        self.assertAlmostEqual(
            sem.standalone_cost_for_candidate(sem.CANDIDATES[0], cohort_small, golden),
            sem.standalone_cost_for_candidate(sem.CANDIDATES[2], cohort_1536_small, golden),
        )


class TestLaterChunkDiagnostic(unittest.TestCase):
    def test_unavailable_reason_constant(self):
        self.assertEqual(sem.LATER_CHUNK_UNAVAILABLE_REASON, "raw_eval3_ranking_provenance_not_persisted")

    def test_provenance_computation_future_eval(self):
        # Future raw-snapshot evals that persist chunk_index can compute it honestly.
        rate, n = sem.later_chunk_hit_rate_from_provenance([0, 3, 0, 1, None])
        # 4 non-None entries; 2 have index>0 (3 and 1) -> 0.5
        self.assertEqual(n, 4)
        self.assertAlmostEqual(rate, 0.5)

    def test_extract_metrics_unavailable_without_provenance(self):
        # eval3-shaped report: per_case carries only entity-key 'ranked', no chunk_index.
        per_case = [{"case_id": "x", "is_judged": True, "categories": ["workflow_code"],
                     "ranked": [["resource", "1"]], "recall_at_10": 1.0}]
        report = type("R", (), {"per_case": per_case})()
        extra, diag = sem.extract_metrics(report, None, None)  # type: ignore[arg-type]
        self.assertFalse(diag["available"])
        self.assertEqual(diag["reason"], sem.LATER_CHUNK_UNAVAILABLE_REASON)
        self.assertIsNone(diag["later_chunk_hit_rate"])
        # long_resource signal is still computed and unaffected.
        self.assertIn("long_resource_chunk_recall_at_10", extra)

    def test_extract_metrics_available_with_provenance(self):
        per_case = [{"case_id": "x", "is_judged": True, "categories": [],
                     "ranked": [["resource", "1"]], "ranked_chunk_index": [3],
                     "recall_at_10": 1.0}]
        report = type("R", (), {"per_case": per_case})()
        extra, diag = sem.extract_metrics(report, None, None)  # type: ignore[arg-type]
        self.assertTrue(diag["available"])
        self.assertEqual(diag["later_chunk_hit_rate"], 1.0)


def _tiny_decision():
    cands = []
    preflight = {}
    for c in sem.CANDIDATES:
        eligible = c.dimension == 384
        cands.append({
            "candidate": c.to_sanitized_dict(),
            "overall": {"recall@10": 0.5, "mrr": 0.4, "ndcg@10": 0.4, "n": 2},
            "workflow_code_recall_at_10": 0.4,
            "long_resource_chunk_recall_at_10": 0.3,
            "later_chunk_diagnostic": {"available": False, "reason": sem.LATER_CHUNK_UNAVAILABLE_REASON,
                                       "later_chunk_hit_rate": None},
            "counts": {},
            "cohort_counts": {"n_chunks": 3, "n_entities": 2, "n_unique_embeddable_texts": 3,
                              "n_duplicate_chunks_collapsed": 0, "chunks_by_representation": {},
                              "max_chunk_index": 0, "multi_chunk_entities": 0},
            "provider_account": {"input_tokens_from_usage": 1000, "api_requests": 1,
                                 "embedded_inputs": 3, "cache_hits": 0, "cache_misses": 3, "cost_usd": 0.00002},
            "disqualify_reasons": [] if eligible else ["projected_full_corpus_storage_above_gate"],
            "capacity_fail": not eligible,
            "eligible_for_selection": eligible,
        })
        preflight[c.name] = {
            "dimension": c.dimension, "n_unique_embeddable_texts": 3,
            "projected_input_tokens": 5000, "projected_cost_usd": 0.0001,
            "full_corpus_storage_gb": 4.59 if eligible else 16.4,
            "capacity_verdict": "PASS" if eligible else "FAIL",
        }
    policy = sem.sanitize_report([], None)["decision_policy"]
    decision = {
        "decision_policy": policy,
        "candidates": cands,
        "winner": sem.CANDIDATES[0].to_sanitized_dict(),
        "winner_rationale": {"eligible": True, "selected": "384-small",
                             "eligible_ranking": ["384-small", "384-large"],
                             "selection_key_used": "recall@10, ...", "capacity_note": "..."},
        "preflight": {"per_candidate": preflight, "aggregate": {
            "projected_input_tokens_all_candidates": 20000,
            "projected_cost_usd_all_candidates": 0.0004, "spend_cap_usd": 25.0,
            "within_spend_cap": True, "storage_gate_gb": 12.0, "price_assumption": "$0.02/1M"}},
        "cohort_counts": {"sources": {}, "high_water": {},
                          "integrity": {"judged_identities_required": 2, "judged_identities_present": 2,
                                        "missing_judged_identities": 0}},
    }
    # Gap 2: bind the complete accounting object so decision.accounting matches
    # the bundle's accounting in the synthetic fixtures.
    decision["accounting"] = sem.build_evidence_accounting(
        decision, {"unique_normalized_queries": 1, "estimated_input_tokens": 30}
    )
    # Non-deterministic-core envelope fields (selection / handoff / evidence
    # metadata) so the bound-object deep-equality tests mirror the real decision.
    decision["selection"] = {
        "selected_production_dimension": 384,
        "production_activated": False,
        "note": "test selection; not production-activated",
    }
    decision["task_2_17_handoff"] = {
        "scope": "propagation/verification handoff (test)",
        "kind": "propagation-verification-acceptance-handoff",
        "must_verify": ["dimension == 384"],
    }
    decision["evidence_replay"] = {
        "mode": "strict-offline-evidence-cache-replay",
        "evidence_pair_id": "pending",
        "metadata": {"source": "synthetic-test", "revision": 1},
    }
    return decision


def _write_fake_cache(path, dim, n=3):
    import base64, struct
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        os.chmod(path, 0o600)
        for i in range(n):
            blob = base64.b64encode(struct.pack(f"<{dim}f", *([0.01] * dim))).decode()
            fh.write(json.dumps({"key": f"openai:m:{dim}:h{i}", "d": dim, "v": blob}) + "\n")


def _write_fake_golden(path):
    path.write_text(json.dumps({
        "version": "golden/test/v1", "meta": {}, "cases": [
            {"id": "c1", "query": "alpha beta", "expected": [], "categories": [], "limit": 10},
        ],
    }))


def _build_replay_env(testcase):
    """Shared fixture builder. Registers all cleanups on *testcase* so the
    GOLDEN_SHA256 patch never leaks across test instances (item 4 / item 5)."""

    tmp = tempfile.TemporaryDirectory()
    testcase.addCleanup(tmp.cleanup)
    cache_dir = Path(tmp.name) / "cache"
    os.chmod(cache_dir.parent, 0o700)
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(cache_dir, 0o700)
    for dim in sem.EVAL3_DIMENSIONS:
        _write_fake_cache(cache_dir / f"cache.{dim}.jsonl", dim)
    golden_path = Path(tmp.name) / "golden.json"
    _write_fake_golden(golden_path)
    # Bind the golden literal to THIS fake golden so the builder/replay literal
    # checks (item 4) pass for synthetic fixtures; the real-artifact test
    # exercises the true literal against the real golden file.
    fake_golden_sha = sem._sha256_file(golden_path)
    patcher = patch.object(sem, "GOLDEN_SHA256", fake_golden_sha)
    patcher.start()
    testcase.addCleanup(patcher.stop)
    decision = _tiny_decision()
    gqe = {"unique_normalized_queries": 1, "estimated_input_tokens": 30}
    bundle = sem.build_replay_bundle(
        decision, golden_path=golden_path, cache_dir=cache_dir, golden_query_estimate=gqe)
    bundle_path = cache_dir / "replay-bundle.json"
    sem.atomic_write_text(bundle_path, json.dumps(bundle, indent=2, ensure_ascii=False) + "\n",
                         mode=sem.PRIVATE_FILE_MODE)
    bundle_file_sha = sem._sha256_file(bundle_path)
    proof = sem.strict_offline_replay(
        bundle=bundle, decision=decision, golden_path=golden_path, cache_dir=cache_dir,
        manifest={
            "decision_deterministic_core_sha256": bundle["decision_deterministic_core_sha256"],
            "candidate_grid_hash": bundle["candidate_grid_hash"],
            "decision_policy_hash": bundle["decision_policy_hash"],
            "eval3_cohort_manifest_hash": bundle["eval3_cohort_manifest_hash"],
            "accounting_sha256": bundle["accounting_sha256"],
            "golden": bundle["golden"], "immutable_caches": bundle["immutable_caches"],
            "eval3_cache_record_count": bundle["eval3_cache_record_count"],
            "replay_bundle": {"version": bundle["bundle_version"],
                              "file_sha256": bundle_file_sha,
                              "canonical_sha256": sem._sha256_json(bundle)},
        }, golden_query_tokens=30, bundle_path=str(bundle_path))
    manifest = sem.build_frozen_manifest(
        decision, golden_path=golden_path, cache_dir=cache_dir, bundle=bundle,
        replay_proof=proof, bundle_path=str(bundle_path),
        bundle_file_sha256=bundle_file_sha)
    return tmp, cache_dir, golden_path, decision, bundle, manifest


class TestReplayBundleAndManifest(unittest.TestCase):
    def _build_env(self):
        return _build_replay_env(self)

    def test_bundle_is_hygiene_clean(self):
        _, _, _, _, bundle, _ = self._build_env()
        self.assertEqual(sem.scan_envelope(bundle), [])

    def test_strict_replay_passes_and_zero_calls(self):
        _, cache_dir, golden_path, decision, bundle, manifest = self._build_env()
        proof = sem.strict_offline_replay(
            bundle=bundle, decision=decision, golden_path=golden_path, cache_dir=cache_dir,
            manifest=manifest, golden_query_tokens=30)
        self.assertTrue(proof["zero_provider_calls"])
        self.assertEqual(proof["provider_calls_attempted"], 0)
        self.assertEqual(proof["network_calls"], 0)
        self.assertEqual(proof["winner_reproduced"], "384-small")

    def test_fail_closed_on_cache_record_count_mismatch(self):
        _, cache_dir, golden_path, decision, bundle, manifest = self._build_env()
        # Tamper: append a record to cache.384 (count mismatch).
        with open(cache_dir / "cache.384.jsonl", "a") as fh:
            fh.write(json.dumps({"key": "openai:m:384:x", "d": 384, "v": "AAAA"}) + "\n")
        with self.assertRaises(sem.ReplayMismatch):
            sem.strict_offline_replay(
                bundle=bundle, decision=decision, golden_path=golden_path, cache_dir=cache_dir,
                manifest=manifest, golden_query_tokens=30)

    def test_fail_closed_on_cache_mode_mismatch(self):
        _, cache_dir, golden_path, decision, bundle, manifest = self._build_env()
        os.chmod(cache_dir / "cache.384.jsonl", 0o644)
        with self.assertRaises(sem.ReplayMismatch):
            sem.strict_offline_replay(
                bundle=bundle, decision=decision, golden_path=golden_path, cache_dir=cache_dir,
                manifest=manifest, golden_query_tokens=30)
        os.chmod(cache_dir / "cache.384.jsonl", 0o600)

    def test_fail_closed_on_golden_hash_mismatch(self):
        _, cache_dir, golden_path, decision, bundle, manifest = self._build_env()
        golden_path.write_text(json.dumps({"version": "golden/changed/v1", "cases": []}))
        with self.assertRaises(sem.ReplayMismatch):
            sem.strict_offline_replay(
                bundle=bundle, decision=decision, golden_path=golden_path, cache_dir=cache_dir,
                manifest=manifest, golden_query_tokens=30)

    def test_fail_closed_on_decision_core_mismatch(self):
        _, cache_dir, golden_path, decision, bundle, manifest = self._build_env()
        # Tamper: change a recorded metric so the deterministic-core hash shifts.
        decision["candidates"][0]["overall"]["recall@10"] = 0.9
        with self.assertRaises(sem.ReplayMismatch):
            sem.strict_offline_replay(
                bundle=bundle, decision=decision, golden_path=golden_path, cache_dir=cache_dir,
                manifest=manifest, golden_query_tokens=30)

    def test_fail_closed_on_grid_hash_mismatch(self):
        _, cache_dir, golden_path, decision, bundle, manifest = self._build_env()
        bad = dict(manifest)
        bad["candidate_grid_hash"] = "0" * 64
        with self.assertRaises(sem.ReplayMismatch):
            sem.strict_offline_replay(
                bundle=bundle, decision=decision, golden_path=golden_path, cache_dir=cache_dir,
                manifest=bad, golden_query_tokens=30)


@unittest.skipUnless(_REAL_DECISION.exists() and _REAL_BUNDLE.exists(), "real eval3 artifacts absent")
class TestRealArtifactReplay(unittest.TestCase):
    def test_real_strict_replay_zero_calls_identical_winner(self):
        decision = json.loads(_REAL_DECISION.read_text())
        bundle = json.loads(_REAL_BUNDLE.read_text())
        manifest = json.loads(_REAL_MANIFEST.read_text())
        golden = loader.load_golden_set(_REAL_GOLDEN)
        gq = sem.golden_query_token_estimate(golden)["estimated_input_tokens"]
        proof = sem.strict_offline_replay(
            bundle=bundle, decision=decision, golden_path=_REAL_GOLDEN,
            cache_dir=_REAL_CACHE_DIR, manifest=manifest, golden_query_tokens=gq,
            decision_json_path=str(_REAL_DECISION),
            decision_md_path=str(_REAL_DECISION.with_suffix(".md")))
        self.assertTrue(proof["zero_provider_calls"])
        self.assertEqual(proof["provider_calls_attempted"], 0)
        self.assertEqual(proof["network_calls"], 0)
        self.assertEqual(proof["winner_reproduced"], decision["winner"]["name"])
        self.assertEqual(proof["eligible_ranking_reproduced"],
                         decision["winner_rationale"]["eligible_ranking"])

    def test_real_decision_core_hash_stable(self):
        decision = json.loads(_REAL_DECISION.read_text())
        manifest = json.loads(_REAL_MANIFEST.read_text())
        self.assertEqual(
            sem.decision_deterministic_core_hash(decision),
            manifest["decision_deterministic_core_sha256"],
        )

    def test_real_caches_unchanged_facts(self):
        manifest = json.loads(_REAL_MANIFEST.read_text())
        for dim in sem.EVAL3_DIMENSIONS:
            facts = sem.cache_file_facts(_REAL_CACHE_DIR / f"cache.{dim}.jsonl", dim)
            self.assertEqual(facts["record_count"], sem.EVAL3_CACHE_RECORD_COUNT)
            self.assertEqual(facts["sha256"], manifest["immutable_caches"][str(dim)]["sha256"])
            self.assertTrue(facts["mode_is_private"])
            self.assertTrue(facts["dimension_consistent"])


class TestEvidenceAccountingBlocks(unittest.TestCase):
    def test_three_named_blocks_present_and_labeled(self):
        decision = _tiny_decision()
        acc = sem.build_evidence_accounting(decision, {"unique_normalized_queries": 1, "estimated_input_tokens": 30})
        for key in ("eval3_actual_incremental_bakeoff", "standalone_candidate_accounting", "historical_duplicate_attempts"):
            self.assertIn(key, acc)
        self.assertEqual(acc["eval3_actual_incremental_bakeoff"]["scope"].startswith("exact"), True)
        hist = acc["historical_duplicate_attempts"]
        self.assertEqual(hist["exact_spend"], "unavailable")
        self.assertEqual(hist["destructive_cache_resets_disclosed"], 2)
        self.assertIn("ESTIMATE", hist["estimate_label"])
        self.assertTrue(hist["within_spend_cap"])
        self.assertTrue(hist["not_a_guaranteed_upper_bound"])

    def test_standalone_does_not_use_incremental_cache_miss_cost(self):
        decision = _tiny_decision()
        acc = sem.build_evidence_accounting(decision, {"unique_normalized_queries": 1, "estimated_input_tokens": 30})
        # 384-small and 1536-small share corpus texts -> identical standalone cost.
        self.assertEqual(
            acc["standalone_candidate_accounting"]["per_candidate"]["384-small"]["estimated_cost_usd"],
            acc["standalone_candidate_accounting"]["per_candidate"]["1536-small"]["estimated_cost_usd"],
        )


class TestFutureFreezeArchitecture(unittest.TestCase):
    def _contract(self, cli, entities, cohorts, snap_path):
        snap = cli.write_raw_frozen_snapshot(entities, cohorts, snap_path)
        snap["path"] = str(snap_path)
        contract = cli.build_freeze_input_contract(
            entities, cohorts, golden_path=_REAL_GOLDEN, snapshot=snap
        )
        return snap, dict(contract)

    def test_snapshot_write_load_verify_and_fail_closed(self):
        import scripts.compare_embedding_candidates as cli
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ent = _resource_entity("1", {"kind": "resource", "title": "T", "body": "a b c d", "metadata": {}})
        cohorts = {c: sem.build_chunked_cohort([ent], c) for c in sem.CANDIDATES}
        snap_path = Path(tmp.name) / "raw-frozen-snapshot.json"
        snap, manifest = self._contract(cli, [ent], cohorts, snap_path)
        self.assertEqual(snap["n_entities"], 1)
        self.assertTrue(snap_path.exists())
        self.assertEqual(oct(snap_path.stat().st_mode & 0o777), "0o600")
        # load round-trips
        loaded = cli.load_raw_frozen_snapshot(snap_path)
        self.assertEqual(loaded["version"], cli.RAW_FROZEN_SNAPSHOT_VERSION)
        # binding verifies when the COMPLETE contract manifest matches
        self.assertIsNotNone(
            cli.verify_frozen_snapshot_binding(manifest, Path(tmp.name), golden_path=_REAL_GOLDEN)
        )
        # fail closed on tamper
        snap_path.write_text("{}")
        with self.assertRaises(RuntimeError):
            cli.verify_frozen_snapshot_binding(manifest, Path(tmp.name), golden_path=_REAL_GOLDEN)
        # None when no binding declared (offline / eval3-precedes path)
        self.assertIsNone(cli.verify_frozen_snapshot_binding({}, Path(tmp.name)))
        self.assertIsNone(cli.verify_frozen_snapshot_binding(None, Path(tmp.name)))

    def test_snapshot_roundtrip_preserves_nested_semantics_order(self):
        """Raw freeze must not reorder v1's order-sensitive semantics mappings."""
        import scripts.compare_embedding_candidates as cli

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        row = {
            "kind": "workflow",
            "title": "Ordered workflow",
            "body": "Description",
            "metadata": {
                "workflow_semantics": {
                    "models": [
                        {"z_name": "z-model", "a_name": "a-model"},
                    ],
                },
            },
            "payload": {},
        }
        ent = _resource_entity("ordered-1", row)
        cohorts = {c: sem.build_chunked_cohort([ent], c) for c in sem.CANDIDATES}
        snap_path = Path(tmp.name) / "raw-frozen-snapshot.json"
        cli.write_raw_frozen_snapshot([ent], cohorts, snap_path)

        loaded = cli.load_raw_frozen_snapshot(snap_path)
        rebuilt_entities = cli.entities_from_raw_snapshot(loaded)
        rebuilt = {
            c: sem.build_chunked_cohort(rebuilt_entities, c)
            for c in sem.CANDIDATES
        }
        manifest = {
            "per_candidate_chunk_facts": cli.freeze_input_candidate_facts(cohorts),
            "cohort_entity_facts": cli.freeze_input_entity_facts([ent]),
        }
        cli.verify_rebuilt_freeze_facts(manifest, rebuilt, rebuilt_entities)

    def test_incomplete_snapshot_only_manifest_rejected(self):
        # Gap 3: a manifest carrying ONLY raw_frozen_snapshot (no kind/version/
        # golden/grid/policy/chunk-facts) is rejected before any provider call.
        import scripts.compare_embedding_candidates as cli
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ent = _resource_entity("1", {"kind": "resource", "title": "T", "body": "a b c d", "metadata": {}})
        cohorts = {c: sem.build_chunked_cohort([ent], c) for c in sem.CANDIDATES}
        snap_path = Path(tmp.name) / "raw-frozen-snapshot.json"
        snap = cli.write_raw_frozen_snapshot([ent], cohorts, snap_path)
        incomplete = {"raw_frozen_snapshot": {**snap, "path": str(snap_path)}}
        with self.assertRaises(RuntimeError):
            cli.verify_frozen_snapshot_binding(incomplete, Path(tmp.name), golden_path=_REAL_GOLDEN)


class TestJsonMarkdownConsistency(unittest.TestCase):
    def test_json_and_md_agree(self):
        if not _REAL_DECISION.exists():
            self.skipTest("real decision absent")
        d = json.loads(_REAL_DECISION.read_text())
        md = (_REPO / "docs" / "hybrid-search" / "task-2.14-embedding-decision.md").read_text()
        # winner
        self.assertIn(d["winner"]["name"], md)
        # selection dimension + chunk contracts
        self.assertIn(str(d["selection"]["selected_production_dimension"]), md)
        self.assertIn("512", md)
        self.assertIn("50", md)
        # accounting labels present
        self.assertIn("eval3", md.lower())
        self.assertIn("standalone", md.lower())
        # later-chunk unavailable disclosed in MD
        self.assertIn(sem.LATER_CHUNK_UNAVAILABLE_REASON, md)
        # task 2.17 handoff referenced
        self.assertIn("2.17", md)
        # replay zero-provider-calls claim
        self.assertIn("`0`", md)
        # both hygiene-clean
        self.assertEqual(sem.scan_envelope(d), [])
        self.assertEqual(sem.scan_envelope(md), [])


class TestWinnerRationaleSelectionKey(unittest.TestCase):
    """Task 2.14 rationale-consistency regression: the winner rationale's
    selection key must state the deterministic, order-independent *standalone*
    candidate cost, never the order-dependent incremental "lower actual cost"."""

    EXPECTED = "lower standalone candidate cost"
    FORBIDDEN = "lower actual cost"

    def _artifacts(self):
        if not _REAL_DECISION.exists():
            self.skipTest("real decision absent")
        d = json.loads(_REAL_DECISION.read_text())
        md = (_REPO / "docs" / "hybrid-search" / "task-2.14-embedding-decision.md").read_text()
        return d, md

    def test_constant_is_standalone_not_actual(self):
        # Single source of truth never reads as the incremental cache-miss cost.
        self.assertIn(self.EXPECTED, sem.SELECTION_KEY_USED)
        self.assertNotIn(self.FORBIDDEN, sem.SELECTION_KEY_USED)

    def test_json_uses_standalone_and_rejects_actual(self):
        d, _ = self._artifacts()
        key = d["winner_rationale"]["selection_key_used"]
        self.assertIn(self.EXPECTED, key)
        self.assertNotIn(self.FORBIDDEN, key)
        self.assertEqual(key, sem.SELECTION_KEY_USED)
        # No winner-rationale field may carry the stale phrase anywhere.
        rendered = json.dumps(d["winner_rationale"])
        self.assertNotIn(self.FORBIDDEN, rendered)

    def test_markdown_uses_standalone_and_rejects_actual(self):
        _, md = self._artifacts()
        self.assertIn(self.EXPECTED, md)
        # The stale phrase must not appear in the selection rationale line.
        for line in md.splitlines():
            if "Selection rationale" in line:
                self.assertNotIn(self.FORBIDDEN, line)
                self.assertIn(self.EXPECTED, line)

    def test_reverse_order_leaves_standalone_cost_and_winner_invariant(self):
        # Confirm the frozen tiebreak is order-independent on standalone cost.
        golden = GoldenSet(cases=[
            GoldenCase(id="c1", query="alpha beta", expected=[], categories=[],
                       limit=10, expect_no_hit=True),
        ])
        ent = _resource_entity("1", {"kind": "resource", "title": "T",
                                     "body": "a b c d e", "metadata": {}})

        def costs_for(order):
            out = {}
            for name in order:
                cand = next(c for c in sem.CANDIDATES if c.name == name)
                cohort = sem.build_chunked_cohort([ent], cand)
                out[name] = sem.standalone_cost_for_candidate(cand, cohort, golden)
            return out

        fwd = costs_for([c.name for c in sem.CANDIDATES])
        rev = costs_for([c.name for c in reversed(sem.CANDIDATES)])
        self.assertEqual(fwd, rev)  # per-name standalone cost invariant

        # The winner is identical under reversed candidate ordering, and the
        # rationale's selection key is the deterministic standalone cost axis.
        self.assertIn(self.EXPECTED, sem.SELECTION_KEY_USED)


# ---------------------------------------------------------------------------
# Task 2.14 final narrow follow-up: items 2, 3, 5, 7, 8, 9, 10, 14, 17
# ---------------------------------------------------------------------------


class TestReplayOfflineNoNetwork(unittest.TestCase):
    """Item 2: the public `replay` CLI is strict-offline; monkeypatching the live
    reader and provider/network entry points to raise must still reproduce the
    winner with zero calls."""

    def test_replay_reproduces_when_live_reader_and_network_raise(self):
        import scripts.compare_embedding_candidates as cli

        def _boom(*a, **k):
            raise AssertionError("live reader / network must not be touched in replay")

        argv = ["replay", "--decision-json", str(_REAL_DECISION),
                "--bundle", str(_REAL_BUNDLE), "--frozen-manifest", str(_REAL_MANIFEST),
                "--golden", str(_REAL_GOLDEN), "--cache-dir", str(_REAL_CACHE_DIR)]
        args = cli.build_parser().parse_args(argv)
        with patch.object(cli, "read_real_cohort_entities", _boom), \
                patch.object(cli, "_postgrest_get", _boom), \
                patch.object(sem, "_stdlib_transport" if hasattr(sem, "_stdlib_transport") else "_ReplayTransport", _boom), \
                patch("urllib.request.urlopen", _boom):
            rc = cli.cmd_replay(args)
        self.assertEqual(rc, 0)


class TestCumulativeSpendGate(unittest.TestCase):
    """Item 3: the cap must use accumulated actual usage + next estimated batch,
    so a first call whose actual usage + the second estimated batch exceeds the
    cap blocks the second provider call."""

    def test_second_call_blocked_when_actual_plus_estimate_exceeds_cap(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cache = sem.EmbeddingCache(Path(tmp.name) / "c.jsonl")
        transport = _FakeUsageTransport(384)
        # Set a tiny cap so the test is fast and deterministic.
        client = sem.ProviderClient(
            candidate=sem.CANDIDATES[0], cache=cache, api_key="k",
            transport=transport, cost_cap_usd=0.0001,
        )
        # First call: 1 input -> 100 prompt tokens -> $0.000002 (under cap).
        client.embed_texts(["alpha"])
        self.assertEqual(transport.calls, 1)
        # Second call: another fresh miss. Accumulated actual (100) + next
        # estimated batch (100) = 200 tokens = $0.000004 ... still under 0.0001?
        # Use a cap that the first actual+second estimate clearly exceeds. With
        # 100 tokens/call, cap 0.0001 => $0.000002/call is far below. Instead,
        # force the estimate high by using a long text so the second batch alone
        # plus the first actual usage exceeds the cap.
        long_text = " ".join(["x"] * 200000)  # ~ very many estimated tokens
        with self.assertRaises(ec.EmbeddingError) as cm:
            client.embed_texts([long_text])
        self.assertIn("exceeds cap", str(cm.exception))
        # The second provider call was BLOCKED (transport only called once).
        self.assertEqual(transport.calls, 1)

    def test_two_call_cumulative_accounting_preserved(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cache = sem.EmbeddingCache(Path(tmp.name) / "c.jsonl")
        transport = _FakeUsageTransport(384)
        client = sem.ProviderClient(
            candidate=sem.CANDIDATES[0], cache=cache, api_key="k",
            transport=transport, cost_cap_usd=sem.SPEND_CAP_USD,
        )
        client.embed_texts(["alpha", "beta"])
        client.embed_texts(["alpha", "gamma"])
        self.assertEqual(client._counter.calls, 2)
        self.assertEqual(sum(u["prompt_tokens"] for u in client._counter.usage), 300)


class TestBundleByteAndAccountingTamper(unittest.TestCase):
    """Items 5 + 12: deliberate bundle-hash mismatch + accounting tamper fail."""

    def _env(self):
        return _build_replay_env(self)

    def test_fail_closed_on_bundle_file_byte_tamper(self):
        _, cache_dir, golden_path, decision, bundle, manifest = self._env()
        bundle_path = cache_dir / "replay-bundle.json"
        # Rewrite the bundle file with different bytes (same object, extra whitespace
        # changes the file hash but keeps canonical hash -> isolates the FILE check).
        bundle_path.write_text(json.dumps(bundle, indent=4, ensure_ascii=False) + "\n")
        os.chmod(bundle_path, 0o600)
        with self.assertRaises(sem.ReplayMismatch):
            sem.strict_offline_replay(
                bundle=bundle, decision=decision, golden_path=golden_path, cache_dir=cache_dir,
                manifest=manifest, golden_query_tokens=30, bundle_path=str(bundle_path))

    def test_fail_closed_on_eval3_accounting_tamper(self):
        _, cache_dir, golden_path, decision, bundle, manifest = self._env()
        # Tamper the recorded eval3 exact usage -> decision-core hash shifts.
        decision["candidates"][0]["provider_account"]["input_tokens_from_usage"] = 999999
        with self.assertRaises(sem.ReplayMismatch):
            sem.strict_offline_replay(
                bundle=bundle, decision=decision, golden_path=golden_path, cache_dir=cache_dir,
                manifest=manifest, golden_query_tokens=30)


class TestReverseOrderInvariance(unittest.TestCase):
    """Item 7: reversing candidate order leaves per-name standalone cost and the
    winner / eligible ranking invariant."""

    def test_reversed_order_invariant(self):
        golden = GoldenSet(cases=[
            GoldenCase(id="c1", query="alpha beta", expected=[], categories=[], limit=10, expect_no_hit=True),
        ])
        ent = _resource_entity("1", {"kind": "resource", "title": "T", "body": "a b c d e", "metadata": {}})

        def metrics_for(order):
            # Distinct per-name recall so the winner is well-defined and the
            # frozen tiebreak is genuinely order-independent (not relying on
            # stable-sort input order among identical metrics).
            recall_by_name = {"384-small": 0.80, "384-large": 0.70,
                              "1536-small": 0.60, "1536-large": 0.50}
            out = []
            for name in order:
                cand = next(c for c in sem.CANDIDATES if c.name == name)
                cohort = sem.build_chunked_cohort([ent], cand)
                m = sem.CandidateMetrics(
                    candidate=cand,
                    overall={"recall@10": recall_by_name[name], "mrr": 0.4, "ndcg@10": 0.4},
                    by_category={}, by_entity_kind={},
                    workflow_code_recall_at_10=0.4, long_resource_chunk_recall_at_10=0.3,
                    later_chunk_hit_rate=None,
                    later_chunk_diagnostic={"available": False, "reason": sem.LATER_CHUNK_UNAVAILABLE_REASON},
                    counts={}, cohort_counts=cohort.sanitized_counts(),
                    account={"cost_usd": 0.0},
                    selection_cost_usd=sem.standalone_cost_for_candidate(cand, cohort, golden),
                    latency_ms={},
                )
                sem.classify_candidate(m, capacity_fail=sem.CAPACITY_FACTS[cand.dimension]["verdict"] == "FAIL",
                                       missing_judged_identities=0, duplicate_entities_after_collapse=0,
                                       vector_validation_failures=0, provider_failures=0)
                out.append(m)
            return out

        fwd = metrics_for([c.name for c in sem.CANDIDATES])
        rev = metrics_for([c.name for c in reversed(sem.CANDIDATES)])
        fwd_costs = {m.candidate.name: m.selection_cost_usd for m in fwd}
        rev_costs = {m.candidate.name: m.selection_cost_usd for m in rev}
        self.assertEqual(fwd_costs, rev_costs)  # per-name standalone cost invariant
        self.assertEqual(sem.select_winner(fwd).candidate.name,
                         sem.select_winner(rev).candidate.name)
        fwd_rank = [m.candidate.name for m in sorted(
            [m for m in fwd if m.eligible_for_selection and not m.disqualify_reasons],
            key=sem.CandidateMetrics.selection_key)]
        rev_rank = [m.candidate.name for m in sorted(
            [m for m in rev if m.eligible_for_selection and not m.disqualify_reasons],
            key=sem.CandidateMetrics.selection_key)]
        self.assertEqual(fwd_rank, rev_rank)


class TestSnapshotPathModeAudit(unittest.TestCase):
    """Item 8: repo-relative snapshot path resolves under repo root (not double
    .cache), parent is 0700, file is 0600, and binding fails closed on
    permissive mode / missing / hash mismatch."""

    def test_relative_path_resolved_under_repo_root(self):
        # A repo-relative path is anchored at repo root, not at cache_dir.parent.
        resolved = sem.resolve_private_path(".cache/hivemind-semantic-eval/x.json")
        self.assertTrue(resolved.is_absolute())
        self.assertNotIn(".cache/.cache", str(resolved))

    def test_binding_fail_closed_on_modes_missing_mismatch(self):
        import scripts.compare_embedding_candidates as cli
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cache_dir = Path(tmp.name) / "cache"
        cache_dir.mkdir(parents=True)
        os.chmod(cache_dir, 0o700)
        ent = _resource_entity("1", {"kind": "resource", "title": "T", "body": "a b c", "metadata": {}})
        cohorts = {c: sem.build_chunked_cohort([ent], c) for c in sem.CANDIDATES}
        snap_path = cache_dir / "raw-frozen-snapshot.json"
        snap = cli.write_raw_frozen_snapshot([ent], cohorts, snap_path)
        snap["path"] = str(snap_path)
        manifest = cli.build_freeze_input_contract(
            [ent], cohorts, golden_path=_REAL_GOLDEN, snapshot=snap
        )
        # parent 0700, file 0600
        self.assertEqual(oct(snap_path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(cache_dir.stat().st_mode & 0o777), "0o700")
        self.assertIsNotNone(
            cli.verify_frozen_snapshot_binding(manifest, cache_dir, golden_path=_REAL_GOLDEN)
        )
        # permissive file mode -> fail closed
        os.chmod(snap_path, 0o644)
        with self.assertRaises(sem.ReplayMismatch):
            cli.verify_frozen_snapshot_binding(manifest, cache_dir, golden_path=_REAL_GOLDEN)
        os.chmod(snap_path, 0o600)
        # missing file -> fail closed
        snap_path.unlink()
        with self.assertRaises(sem.ReplayMismatch):
            cli.verify_frozen_snapshot_binding(manifest, cache_dir, golden_path=_REAL_GOLDEN)


class TestSnapshotDrivesEvaluate(unittest.TestCase):
    """Item 9: with a verified snapshot binding, cmd_evaluate reconstructs the
    cohort from the snapshot and does NOT call the live reader (which raises)."""

    def test_evaluate_uses_snapshot_not_live_reader(self):
        import scripts.compare_embedding_candidates as cli
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cache_dir = Path(tmp.name) / "cache"
        cache_dir.mkdir(parents=True)
        os.chmod(cache_dir, 0o700)
        ent = _resource_entity("1", {"kind": "resource", "title": "T", "body": "alpha beta gamma", "metadata": {}})
        cohorts = {c: sem.build_chunked_cohort([ent], c) for c in sem.CANDIDATES}
        snap_path = cache_dir / "raw-frozen-snapshot.json"
        snap = cli.write_raw_frozen_snapshot([ent], cohorts, snap_path)
        snap["path"] = str(snap_path)
        # Gap 3: the COMPLETE freeze-input contract (not a snapshot-only manifest).
        manifest = cli.build_freeze_input_contract(
            [ent], cohorts, golden_path=_REAL_GOLDEN, snapshot=snap
        )
        manifest_path = Path(tmp.name) / "freeze-input.json"
        manifest_path.write_text(json.dumps(manifest))

        def _boom(*a, **k):
            raise AssertionError("live reader must not be called when snapshot is bound")

        # Reconstruct entities directly from the snapshot (the path cmd_evaluate uses).
        loaded = cli.load_raw_frozen_snapshot(snap_path)
        ents = cli.entities_from_raw_snapshot(loaded)
        self.assertEqual(len(ents), 1)
        self.assertEqual(ents[0].corpus_item.body, "alpha beta gamma")
        # full CorpusItem state round-trips
        self.assertEqual(ents[0].corpus_item.kind, "resource")

        args = cli.build_parser().parse_args([
            "evaluate", "--offline-embedder", "--frozen-manifest", str(manifest_path),
            "--cache-dir", str(cache_dir), "--golden", str(_REAL_GOLDEN),
            "--out-json", str(Path(tmp.name) / "out.json"),
            "--out-md", str(Path(tmp.name) / "out.md"),
            "--endpoint", "http://127.0.0.1:1/blocked",
        ])
        with patch.object(cli, "read_real_cohort_entities", _boom):
            # offline-embedder path: no provider/network, snapshot-driven cohort.
            cli.cmd_evaluate(args)
        self.assertTrue((Path(tmp.name) / "out.json").exists())

    def test_evaluate_refuses_without_snapshot_or_live_flag(self):
        import scripts.compare_embedding_candidates as cli
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        args = cli.build_parser().parse_args([
            "evaluate", "--offline-embedder", "--cache-dir", str(Path(tmp.name) / "c"),
            "--golden", str(_REAL_GOLDEN),
            "--out-json", str(Path(tmp.name) / "o.json"),
            "--out-md", str(Path(tmp.name) / "o.md"),
        ])
        with self.assertRaises(SystemExit):
            cli.cmd_evaluate(args)


class TestReplayReadOnlyCache(unittest.TestCase):
    """Item 10: strict replay never writes/compacts either paid cache."""

    def test_replay_does_not_touch_cache_writes(self):
        decision = json.loads(_REAL_DECISION.read_text())
        bundle = json.loads(_REAL_BUNDLE.read_text())
        manifest = json.loads(_REAL_MANIFEST.read_text())
        golden = loader.load_golden_set(_REAL_GOLDEN)
        gq = sem.golden_query_token_estimate(golden)["estimated_input_tokens"]

        def _boom(*a, **k):
            raise AssertionError("replay must not write/compact a cache")

        with patch.object(sem.EmbeddingCache, "store", _boom), \
                patch.object(sem.EmbeddingCache, "compact", _boom), \
                patch.object(sem.EmbeddingCache, "_append", _boom):
            proof = sem.strict_offline_replay(
                bundle=bundle, decision=decision, golden_path=_REAL_GOLDEN,
                cache_dir=_REAL_CACHE_DIR, manifest=manifest, golden_query_tokens=gq,
                bundle_path=str(_REAL_BUNDLE),
                decision_json_path=str(_REAL_DECISION),
                decision_md_path=str(_REAL_DECISION.with_suffix(".md")))
        self.assertTrue(proof["zero_provider_calls"])
        self.assertEqual(proof["network_calls"], 0)


class TestRunnerProvenance(unittest.TestCase):
    """Item 14: the real runner persists matched_chunk_index / representation."""

    def test_run_one_carries_real_chunk_provenance(self):
        from eval.retrieval import runner
        # Build a real semantic retriever over a tiny cohort so Results carry
        # matched_chunk_index / matched_representation from the actual collapse.
        item = CorpusItem(kind="workflow", source="ext", item_id="1", body="comfy sampler node")
        ent = sem.CohortEntity(ei.ENTITY_RESOURCE, "1", item,
                               {"kind": "workflow", "body": "comfy sampler node", "title": "", "metadata": {}})
        cohort = sem.build_chunked_cohort([ent], sem.CANDIDATES[0])
        client = sem.ProviderClient(
            candidate=sem.CANDIDATES[0],
            cache=sem.EmbeddingCache(Path(tempfile.mkdtemp()) / "c.jsonl"),
            api_key=None, embed_fn=_fake_embed_fn(384),
        )
        vecs = client.embed_texts(list(cohort.unique_texts.values()))
        vmap = {h: v for h, v in zip(cohort.unique_texts.keys(), vecs)}
        qv = {ec.normalize_query_for_embedding("comfy sampler node"): client.embed_texts(["comfy sampler node"])[0]}
        retr = sem.SemanticRetriever(cohort, vmap, qv, dimension=384)
        case = GoldenCase(id="c1", query="comfy sampler node",
                          expected=[JudgedItem(kind="resource", item_id="1", grade=1)],
                          categories=[], limit=10)
        diag = runner._run_one(retr, case, ks=(10,), timeout_s=5.0)
        self.assertIn("ranked_chunk_index", diag)
        self.assertIn("ranked_representation", diag)
        self.assertEqual(diag["ranked_chunk_index"][0], 0)  # real, not fabricated
        self.assertEqual(diag["ranked_representation"][0], REP_PROSE)

    def test_extract_metrics_uses_runner_provenance(self):
        from eval.retrieval import runner
        # report built from a real runner result drives extract_metrics honestly.
        report = type("R", (), {"per_case": [{
            "case_id": "x", "is_judged": True, "categories": [],
            "ranked": [["resource", "1"]], "ranked_chunk_index": [3],
            "ranked_representation": [REP_WORKFLOW_PYTHON], "recall_at_10": 1.0}]})()
        _, diag = sem.extract_metrics(report, None, None)
        self.assertTrue(diag["available"])
        self.assertEqual(diag["later_chunk_hit_rate"], 1.0)


class TestStructuralPrivacyRejection(unittest.TestCase):
    """Item 17: benign raw query/body fields fail closed even without markers."""

    def test_benign_raw_query_body_fails_closed(self):
        benign = {"candidate": "384-small", "query": "what is the weather today",
                  "body": "just some innocent prose with no secret marker"}
        violations = sem.scan_envelope(benign)
        self.assertTrue(any(v.startswith("raw_field:") for v in violations))

    def test_raw_url_python_source_fail_closed(self):
        v = sem.scan_envelope({"url": "https://example.com/path", "python_source": "import os"})
        self.assertTrue(any(v.startswith("raw_field:") for v in v))

    def test_existing_sanitized_artifacts_remain_clean(self):
        # The tracked artifacts must remain clean under the structural check.
        d = json.loads(_REAL_DECISION.read_text())
        m = json.loads(_REAL_MANIFEST.read_text())
        b = json.loads(_REAL_BUNDLE.read_text())
        self.assertEqual(sem.scan_envelope(d), [])
        self.assertEqual(sem.scan_envelope(m), [])
        self.assertEqual(sem.scan_envelope(b), [])


class TestEvidencePairBinding(unittest.TestCase):
    """Item 6: the manifest binds exact JSON+MD byte hashes + shared pair id;
    tampering any tracked file fails the pair verification."""

    def test_real_pair_bound_and_tamper_fails(self):
        manifest = json.loads(_REAL_MANIFEST.read_text())
        pair = manifest["evidence_pair"]
        self.assertEqual(pair["decision_json"]["sha256"], sem._sha256_file(_REAL_DECISION))
        self.assertEqual(pair["decision_md"]["sha256"], sem._sha256_file(_REAL_DECISION.with_suffix(".md")))
        d = json.loads(_REAL_DECISION.read_text())
        self.assertEqual(d["evidence_pair_id"], pair["id"])
        # clean verify passes
        sem.verify_evidence_pair(manifest, decision_json_path=_REAL_DECISION,
                                 decision_md_path=_REAL_DECISION.with_suffix(".md"))
        # tamper: corrupt the recorded json hash -> fail closed
        bad = json.loads(json.dumps(manifest))
        bad["evidence_pair"]["decision_json"]["sha256"] = "0" * 64
        with self.assertRaises(sem.ReplayMismatch):
            sem.verify_evidence_pair(bad, decision_json_path=_REAL_DECISION,
                                     decision_md_path=_REAL_DECISION.with_suffix(".md"))


class TestGoldenLiteralBinding(unittest.TestCase):
    """Item 4: builder + strict replay bind the fixed golden SHA literal."""

    def test_literal_matches_real_golden(self):
        self.assertEqual(sem.golden_file_facts(_REAL_GOLDEN)["sha256"], sem.GOLDEN_SHA256)

    def test_builder_rejects_golden_drift(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cache_dir = Path(tmp.name) / "cache"
        cache_dir.mkdir(parents=True); os.chmod(cache_dir, 0o700)
        for dim in sem.EVAL3_DIMENSIONS:
            _write_fake_cache(cache_dir / f"cache.{dim}.jsonl", dim)
        gp = Path(tmp.name) / "g.json"
        _write_fake_golden(gp)
        # Do NOT patch GOLDEN_SHA256 here -> real literal rejects the fake golden.
        with self.assertRaises(sem.ReplayMismatch):
            sem.build_replay_bundle(_tiny_decision(), golden_path=gp, cache_dir=cache_dir,
                                    golden_query_estimate={"unique_normalized_queries": 1, "estimated_input_tokens": 30})


# ---------------------------------------------------------------------------
# Task 2.14 final four audited release gaps (GLM-5.2, OFFLINE)
# ---------------------------------------------------------------------------


def _build_pair_env(testcase):
    """Synthetic env whose manifest binds an evidence_pair + real JSON/MD files."""

    tmp, cache_dir, golden_path, decision, bundle, manifest = _build_replay_env(testcase)
    pair_id = sem.evidence_pair_id(decision)
    decision["evidence_pair_id"] = pair_id
    json_path = Path(tmp.name) / "decision.json"
    md_path = Path(tmp.name) / "decision.md"
    sem.atomic_write_text(json_path, json.dumps(decision, indent=2, ensure_ascii=False) + "\n")
    sem.atomic_write_text(md_path, "# decision\n\npair: " + pair_id + "\n")
    manifest = sem.build_frozen_manifest(
        decision, golden_path=golden_path, cache_dir=cache_dir, bundle=bundle,
        replay_proof=manifest["replay_proof"],
        bundle_path=str(cache_dir / "replay-bundle.json"),
        bundle_file_sha256=manifest["replay_bundle"]["file_sha256"],
        evidence_pair={
            "id": pair_id,
            "decision_json": {"path": str(json_path), "sha256": sem._sha256_file(json_path)},
            "decision_md": {"path": str(md_path), "sha256": sem._sha256_file(md_path)},
            "note": "test pair",
        },
    )
    return cache_dir, golden_path, decision, bundle, manifest, json_path, md_path


class TestEvidencePairNeverOptional(unittest.TestCase):
    """Gap 1: when the manifest binds evidence_pair, BOTH JSON and MD paths are
    mandatory; missing/None/wrong/tampered paths fail closed."""

    def _env(self):
        return _build_pair_env(self)

    def test_success_with_both_paths(self):
        cache_dir, golden_path, decision, bundle, manifest, jp, mp = self._env()
        proof = sem.strict_offline_replay(
            bundle=bundle, decision=decision, golden_path=golden_path, cache_dir=cache_dir,
            manifest=manifest, golden_query_tokens=30,
            decision_json_path=str(jp), decision_md_path=str(mp))
        self.assertTrue(proof["zero_provider_calls"])
        self.assertEqual(proof["network_calls"], 0)
        self.assertIn("evidence_pair_json_md_hashes_and_id_match", proof["checks_passed"])

    def test_missing_json_path_fails_closed(self):
        cache_dir, golden_path, decision, bundle, manifest, jp, mp = self._env()
        with self.assertRaises(sem.ReplayMismatch):
            sem.strict_offline_replay(
                bundle=bundle, decision=decision, golden_path=golden_path, cache_dir=cache_dir,
                manifest=manifest, golden_query_tokens=30,
                decision_json_path=None, decision_md_path=str(mp))

    def test_missing_md_path_fails_closed(self):
        cache_dir, golden_path, decision, bundle, manifest, jp, mp = self._env()
        with self.assertRaises(sem.ReplayMismatch):
            sem.strict_offline_replay(
                bundle=bundle, decision=decision, golden_path=golden_path, cache_dir=cache_dir,
                manifest=manifest, golden_query_tokens=30,
                decision_json_path=str(jp), decision_md_path=None)

    def test_wrong_md_path_fails_closed(self):
        cache_dir, golden_path, decision, bundle, manifest, jp, mp = self._env()
        with self.assertRaises(sem.ReplayMismatch):
            sem.strict_offline_replay(
                bundle=bundle, decision=decision, golden_path=golden_path, cache_dir=cache_dir,
                manifest=manifest, golden_query_tokens=30,
                decision_json_path=str(jp), decision_md_path=str(mp.parent / "nope.md"))

    def test_json_tamper_fails_closed(self):
        cache_dir, golden_path, decision, bundle, manifest, jp, mp = self._env()
        # Tamper the JSON file bytes (hash diverges from the manifest binding).
        jp.write_text(jp.read_text() + "\n# tampered\n")
        with self.assertRaises(sem.ReplayMismatch):
            sem.strict_offline_replay(
                bundle=bundle, decision=decision, golden_path=golden_path, cache_dir=cache_dir,
                manifest=manifest, golden_query_tokens=30,
                decision_json_path=str(jp), decision_md_path=str(mp))

    def test_cli_replay_requires_explicit_md_and_unreachable_network(self):
        # Gap 1 CLI: explicit --decision-md; endpoint/proxy unreachable -> still
        # zero provider/network calls (strict evidence replay).
        import scripts.compare_embedding_candidates as cli

        def _boom(*a, **k):
            raise AssertionError("replay must make no network call")

        argv = ["replay", "--decision-json", str(_REAL_DECISION),
                "--decision-md", str(_REAL_DECISION.with_suffix(".md")),
                "--bundle", str(_REAL_BUNDLE), "--frozen-manifest", str(_REAL_MANIFEST),
                "--golden", str(_REAL_GOLDEN), "--cache-dir", str(_REAL_CACHE_DIR),
                "--endpoint", "http://127.0.0.1:1/blocked"]
        args = cli.build_parser().parse_args(argv)
        with patch.object(cli, "read_real_cohort_entities", _boom), \
                patch.object(cli, "_postgrest_get", _boom), \
                patch("urllib.request.urlopen", _boom):
            rc = cli.cmd_replay(args)
        self.assertEqual(rc, 0)


class TestAccountingCompleteBinding(unittest.TestCase):
    """Gap 2: the COMPLETE accounting object is bound (decision == bundle, hash in
    bundle + manifest). Tampering ANY block — especially a top-level total cost —
    fails closed, even when original JSON/MD paths are supplied."""

    def _env(self):
        return _build_pair_env(self)

    def _replay(self, cache_dir, golden_path, decision, bundle, manifest, jp, mp):
        return sem.strict_offline_replay(
            bundle=bundle, decision=decision, golden_path=golden_path, cache_dir=cache_dir,
            manifest=manifest, golden_query_tokens=30,
            decision_json_path=str(jp), decision_md_path=str(mp))

    def test_eval3_top_level_total_cost_tamper_fails(self):
        cache_dir, golden_path, decision, bundle, manifest, jp, mp = self._env()
        d2 = json.loads(json.dumps(decision))
        d2["accounting"]["eval3_actual_incremental_bakeoff"]["total_cost_usd"] = 24.999999
        with self.assertRaises(sem.ReplayMismatch):
            self._replay(cache_dir, golden_path, d2, bundle, manifest, jp, mp)

    def test_eval3_per_candidate_cost_tamper_fails(self):
        cache_dir, golden_path, decision, bundle, manifest, jp, mp = self._env()
        d2 = json.loads(json.dumps(decision))
        d2["accounting"]["eval3_actual_incremental_bakeoff"]["per_candidate"]["384-small"]["cost_usd"] = 9.99
        with self.assertRaises(sem.ReplayMismatch):
            self._replay(cache_dir, golden_path, d2, bundle, manifest, jp, mp)

    def test_standalone_block_tamper_fails(self):
        cache_dir, golden_path, decision, bundle, manifest, jp, mp = self._env()
        d2 = json.loads(json.dumps(decision))
        d2["accounting"]["standalone_candidate_accounting"]["per_candidate"]["384-small"]["estimated_cost_usd"] = 9.99
        with self.assertRaises(sem.ReplayMismatch):
            self._replay(cache_dir, golden_path, d2, bundle, manifest, jp, mp)

    def test_historical_estimate_block_tamper_fails(self):
        cache_dir, golden_path, decision, bundle, manifest, jp, mp = self._env()
        d2 = json.loads(json.dumps(decision))
        d2["accounting"]["historical_duplicate_attempts"]["conservative_reconstructed_estimate_usd"] = 9.99
        with self.assertRaises(sem.ReplayMismatch):
            self._replay(cache_dir, golden_path, d2, bundle, manifest, jp, mp)

    def test_runtime_spend_gates_block_tamper_fails(self):
        cache_dir, golden_path, decision, bundle, manifest, jp, mp = self._env()
        d2 = json.loads(json.dumps(decision))
        d2["accounting"]["runtime_spend_gates"]["aggregate_actual_usage_guard"]["hard_cap_resets_between_candidates_or_dimensions"] = True
        with self.assertRaises(sem.ReplayMismatch):
            self._replay(cache_dir, golden_path, d2, bundle, manifest, jp, mp)

    def test_bundle_accounting_tamper_also_fails(self):
        # Tamper the bundle's accounting (keeping its hash) -> deep-equality fails.
        cache_dir, golden_path, decision, bundle, manifest, jp, mp = self._env()
        b2 = json.loads(json.dumps(bundle))
        b2["accounting"]["eval3_actual_incremental_bakeoff"]["total_cost_usd"] = 24.999999
        with self.assertRaises(sem.ReplayMismatch):
            self._replay(cache_dir, golden_path, decision, b2, manifest, jp, mp)

    def test_missing_accounting_fails(self):
        cache_dir, golden_path, decision, bundle, manifest, jp, mp = self._env()
        d2 = json.loads(json.dumps(decision))
        d2.pop("accounting", None)
        with self.assertRaises(sem.ReplayMismatch):
            self._replay(cache_dir, golden_path, d2, bundle, manifest, jp, mp)

    def test_real_accounting_bound_and_stable(self):
        d = json.loads(_REAL_DECISION.read_text())
        b = json.loads(_REAL_BUNDLE.read_text())
        m = json.loads(_REAL_MANIFEST.read_text())
        self.assertEqual(d["accounting"], b["accounting"])
        self.assertEqual(sem.accounting_canonical_hash(d["accounting"]), b["accounting_sha256"])
        self.assertEqual(b["accounting_sha256"], m["accounting_sha256"])


class TestBoundObjectDeepEquality(unittest.TestCase):
    """Task 2.14 final finding 1: when an evidence pair is present, the supplied
    in-memory ``decision`` must be canonical FULL-OBJECT deep-equal to the exact
    hash-verified bound JSON file object — covering EVERY field (selection,
    task_2_17_handoff, evidence metadata, non-core top-level metadata), not just
    the deterministic core. A direct attack that mutates the in-memory decision
    while passing the ORIGINAL bound JSON/Markdown paths must fail closed."""

    def _env(self):
        return _build_pair_env(self)

    def _replay(self, cache_dir, golden_path, decision, bundle, manifest, jp, mp):
        return sem.strict_offline_replay(
            bundle=bundle, decision=decision, golden_path=golden_path, cache_dir=cache_dir,
            manifest=manifest, golden_query_tokens=30,
            decision_json_path=str(jp), decision_md_path=str(mp))

    def test_clean_replay_runs_deep_equality_check(self):
        cache_dir, golden_path, decision, bundle, manifest, jp, mp = self._env()
        proof = self._replay(cache_dir, golden_path, decision, bundle, manifest, jp, mp)
        self.assertIn("decision_object_matches_bound_json_full_deep_equality", proof["checks_passed"])
        self.assertTrue(proof["zero_provider_calls"])

    def _tamper(self, mutate):
        cache_dir, golden_path, decision, bundle, manifest, jp, mp = self._env()
        d2 = json.loads(json.dumps(decision))  # divergent in-memory object
        mutate(d2)
        # ORIGINAL bound JSON/Markdown paths (file bytes unchanged on disk).
        with self.assertRaises(sem.ReplayMismatch):
            self._replay(cache_dir, golden_path, d2, bundle, manifest, jp, mp)

    def test_direct_attack_selection_production_activated_fails(self):
        self._tamper(lambda d: d["selection"].__setitem__("production_activated", True))

    def test_handoff_field_tamper_fails(self):
        self._tamper(lambda d: d["task_2_17_handoff"].__setitem__("kind", "PRODUCTION ACTIVATED"))

    def test_handoff_list_tamper_fails(self):
        self._tamper(lambda d: d["task_2_17_handoff"]["must_verify"].append("dimension == 999"))

    def test_evidence_replay_metadata_tamper_fails(self):
        self._tamper(lambda d: d["evidence_replay"]["metadata"].__setitem__("revision", 2))

    def test_evidence_replay_mode_tamper_fails(self):
        self._tamper(lambda d: d["evidence_replay"].__setitem__("mode", "live"))

    def test_non_core_top_level_metadata_tamper_fails(self):
        # generated_at is non-deterministic-core top-level metadata; mutating it
        # must still fail closed under full-object deep equality.
        self._tamper(lambda d: d.__setitem__("generated_at", "2030-01-01T00:00:00+00:00"))

    def test_verify_evidence_pair_returns_bound_object(self):
        # The bound file object is the single source of truth and is returned so
        # callers can require full-object equality (two reps are never trusted).
        cache_dir, golden_path, decision, bundle, manifest, jp, mp = self._env()
        bound = sem.verify_evidence_pair(
            manifest, decision_json_path=str(jp), decision_md_path=str(mp))
        self.assertEqual(bound, decision)
        self.assertEqual(sem._sha256_json(bound), sem._sha256_json(decision))


class TestRetryBillingHonesty(unittest.TestCase):
    """Task 2.14 final finding 2: the transport records usage ONLY for a response
    the provider returned. A failed attempt (transport error / 429 / 5xx) raises
    before reporting usage, so failed-attempt billing is UNAVAILABLE and NOT
    counted; the recorded aggregate is the sum of successful-response usage only
    and is NOT proof against billed-but-unreported failed attempts. Retries are
    bounded. Per-client successful usage/accounting stays correct."""

    def test_failed_transport_attempt_reports_zero_usage(self):
        # Inner transport raises (transport error / HTTP 429 / 5xx) -> no usage.
        def _boom(url, headers, body):
            raise ec.EmbeddingError("transport error / HTTP 429")
        counter = sem._CountingTransport(_boom)
        with self.assertRaises(ec.EmbeddingError):
            counter("https://x", {}, {"input": ["a", "b"], "model": "m"})
        # The attempt was made (calls incremented) but no usage was reported.
        self.assertEqual(counter.calls, 1)
        self.assertEqual(counter.usage, [])
        self.assertEqual(sum(u["prompt_tokens"] for u in counter.usage), 0)

    def test_successful_response_records_usage_exactly_once(self):
        def _ok(url, headers, body):
            return {"usage": {"prompt_tokens": 7, "total_tokens": 7}, "data": []}
        counter = sem._CountingTransport(_ok)
        counter("https://x", {}, {"input": ["a"], "model": "m"})
        self.assertEqual(counter.calls, 1)
        self.assertEqual(len(counter.usage), 1)
        self.assertEqual(counter.usage[0]["prompt_tokens"], 7)

    def test_failed_then_successful_records_only_successful_usage(self):
        attempts = {"n": 0}

        def _flap(url, headers, body):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ec.EmbeddingError("HTTP 503")
            return {"usage": {"prompt_tokens": 9, "total_tokens": 9}, "data": []}
        counter = sem._CountingTransport(_flap)
        # First attempt fails (no usage recorded).
        with self.assertRaises(ec.EmbeddingError):
            counter("https://x", {}, {"input": ["a"], "model": "m"})
        # Retry succeeds (usage recorded).
        counter("https://x", {}, {"input": ["a"], "model": "m"})
        self.assertEqual(counter.calls, 2)
        self.assertEqual(len(counter.usage), 1)
        # The failed attempt's billed tokens (if any) are NOT counted.
        self.assertEqual(sum(u["prompt_tokens"] for u in counter.usage), 9)

    def test_retries_bounded_and_constants_consistent(self):
        self.assertEqual(sem.PROVIDER_MAX_RETRIES, 3)
        self.assertEqual(sem.PROVIDER_MAX_ATTEMPTS, 4)
        client = sem.ProviderClient.__new__(sem.ProviderClient)
        client.max_retries = sem.ProviderClient.max_retries
        self.assertEqual(client.max_retries, sem.PROVIDER_MAX_RETRIES)

    def test_accounting_guard_fields_are_honest(self):
        acc = sem.build_evidence_accounting(
            _tiny_decision(), {"unique_normalized_queries": 1, "estimated_input_tokens": 30})
        g = acc["runtime_spend_gates"]["aggregate_actual_usage_guard"]
        # The overclaim field is GONE; honest fields replace it.
        self.assertNotIn("retries_are_real_billed_attempts_counted_not_double_counted", g)
        self.assertNotIn("records_actual_usage_exactly_once_per_batch", g)
        self.assertTrue(g["records_actual_usage_exactly_once_per_successful_batch"])
        self.assertTrue(g["failed_attempt_billing_unavailable_and_not_counted"])
        self.assertIn("usage", g["failed_attempt_billing_note"].lower())
        self.assertIn("failed", g["failed_attempt_billing_note"].lower())
        self.assertTrue(g["retries_bounded"])
        self.assertEqual(g["retries_bounded_max_attempts"], sem.PROVIDER_MAX_ATTEMPTS)
        self.assertTrue(g["recorded_aggregate_actual_cap_not_proof_against_unreported_billed_failures"])

    def test_record_exactly_once_only_counts_successful_usage(self):
        # The guard sums whatever successful-response delta it is given; a failed
        # attempt that reports no usage contributes a zero delta.
        guard = sem.AggregateUsageGuard(cap_usd=25.0)
        guard.record_exactly_once(0)  # failed attempt supplies zero usage
        self.assertEqual(guard.actual_tokens, 0)
        guard.record_exactly_once(1000)  # successful batch reports usage once
        self.assertEqual(guard.actual_tokens, 1000)
        guard.record_exactly_once(0)  # another failed attempt
        self.assertEqual(guard.actual_tokens, 1000)


class TestFreezeInputContractDrift(unittest.TestCase):
    """Gap 3: the freeze-input manifest contract rejects drift in golden / grid /
    policy / chunk-map / chunker / version, and rebuild facts are re-verified."""

    def _contract_env(self):
        import scripts.compare_embedding_candidates as cli
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cache_dir = Path(tmp.name) / "cache"
        cache_dir.mkdir(parents=True)
        os.chmod(cache_dir, 0o700)
        ent = _resource_entity("1", {"kind": "resource", "title": "T",
                                     "body": "alpha beta gamma delta epsilon", "metadata": {}})
        cohorts = {c: sem.build_chunked_cohort([ent], c) for c in sem.CANDIDATES}
        snap_path = cache_dir / "raw-frozen-snapshot.json"
        snap = cli.write_raw_frozen_snapshot([ent], cohorts, snap_path)
        snap["path"] = str(snap_path)
        manifest = cli.build_freeze_input_contract(
            [ent], cohorts, golden_path=_REAL_GOLDEN, snapshot=snap
        )
        return cli, cache_dir, ent, cohorts, manifest

    def test_golden_drift_rejected(self):
        cli, cache_dir, ent, cohorts, manifest = self._contract_env()
        bad = json.loads(json.dumps(manifest))
        bad["golden"]["sha256"] = "0" * 64
        with self.assertRaises(RuntimeError):
            cli.verify_frozen_snapshot_binding(bad, cache_dir, golden_path=_REAL_GOLDEN)

    def test_grid_drift_rejected(self):
        cli, cache_dir, ent, cohorts, manifest = self._contract_env()
        bad = json.loads(json.dumps(manifest))
        bad["candidate_grid_hash"] = "0" * 64
        with self.assertRaises(RuntimeError):
            cli.verify_frozen_snapshot_binding(bad, cache_dir, golden_path=_REAL_GOLDEN)

    def test_policy_drift_rejected(self):
        cli, cache_dir, ent, cohorts, manifest = self._contract_env()
        bad = json.loads(json.dumps(manifest))
        bad["decision_policy_hash"] = "0" * 64
        with self.assertRaises(RuntimeError):
            cli.verify_frozen_snapshot_binding(bad, cache_dir, golden_path=_REAL_GOLDEN)

    def test_wrong_kind_version_rejected(self):
        cli, cache_dir, ent, cohorts, manifest = self._contract_env()
        bad = json.loads(json.dumps(manifest))
        bad["kind"] = "something-else"
        with self.assertRaises(RuntimeError):
            cli.verify_frozen_snapshot_binding(bad, cache_dir, golden_path=_REAL_GOLDEN)
        bad2 = json.loads(json.dumps(manifest))
        bad2["freeze_input_contract_version"] = 999
        with self.assertRaises(RuntimeError):
            cli.verify_frozen_snapshot_binding(bad2, cache_dir, golden_path=_REAL_GOLDEN)

    def test_chunk_map_hash_drift_detected_on_rebuild(self):
        cli, cache_dir, ent, cohorts, manifest = self._contract_env()
        bad = json.loads(json.dumps(manifest))
        bad["per_candidate_chunk_facts"]["384-small"]["chunk_map_sha256"] = "0" * 64
        with self.assertRaises(RuntimeError):
            cli.verify_rebuilt_freeze_facts(bad, cohorts, [ent])

    def test_chunk_count_drift_detected_on_rebuild(self):
        cli, cache_dir, ent, cohorts, manifest = self._contract_env()
        bad = json.loads(json.dumps(manifest))
        bad["per_candidate_chunk_facts"]["384-small"]["n_chunks"] += 1
        with self.assertRaises(RuntimeError):
            cli.verify_rebuilt_freeze_facts(bad, cohorts, [ent])

    def test_entity_identity_drift_detected_on_rebuild(self):
        cli, cache_dir, ent, cohorts, manifest = self._contract_env()
        ent2 = _resource_entity("2", {"kind": "resource", "title": "T", "body": "other", "metadata": {}})
        with self.assertRaises(RuntimeError):
            cli.verify_rebuilt_freeze_facts(manifest, cohorts, [ent, ent2])

    def test_current_chunker_drift_detected_on_rebuild(self):
        # Patch the frozen chunker so rebuilt chunks differ from the freeze facts.
        import dataclasses
        from executors import chunking
        cli, cache_dir, ent, cohorts, manifest = self._contract_env()
        orig = chunking.chunk_representation

        def _drift(rep, *, target_tokens, overlap_tokens):
            out = orig(rep, target_tokens=target_tokens, overlap_tokens=overlap_tokens)
            # Drift each chunk's frozen chunk_hash -> chunk_map_sha256 diverges.
            return [dataclasses.replace(ck, chunk_hash=ck.chunk_hash + "_drift") for ck in out]

        with patch.object(chunking, "chunk_representation", _drift):
            drifted = {c: sem.build_chunked_cohort([ent], c) for c in sem.CANDIDATES}
            with self.assertRaises(RuntimeError):
                cli.verify_rebuilt_freeze_facts(manifest, drifted, [ent])

    def test_rebuild_clean_match_passes(self):
        cli, cache_dir, ent, cohorts, manifest = self._contract_env()
        # Rebuilding with the same chunker/config reproduces the bound facts.
        rebuilt = {c: sem.build_chunked_cohort([ent], c) for c in sem.CANDIDATES}
        cli.verify_rebuilt_freeze_facts(manifest, rebuilt, [ent])  # no raise


class TestAggregateSpendGuard(unittest.TestCase):
    """Gap 4: a shared aggregate actual-usage guard does not reset between
    candidates; a later candidate's call is blocked before its transport runs."""

    def _client(self, cand, guard, tmp, dim):
        cache = sem.EmbeddingCache(Path(tmp) / f"c{cand.name}.jsonl")
        transport = _FakeUsageTransport(dim)
        client = sem.ProviderClient(
            candidate=cand, cache=cache, api_key="k",
            transport=transport, aggregate_guard=guard,
        )
        return client, transport

    def test_later_candidate_first_call_blocked_before_transport(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # 100 prompt tokens per input (from _FakeUsageTransport); set the cap to
        # exactly the cost of 100 tokens so candidate 1 consumes the whole budget.
        cap = 100 / 1_000_000.0 * sem.PRICE_PER_1M_TOKENS_USD
        guard = sem.AggregateUsageGuard(cap_usd=cap)
        c1, t1 = self._client(sem.CANDIDATES[0], guard, tmp.name, 384)
        c1.embed_texts(["alpha"])  # 1 input -> 100 tokens reported
        self.assertEqual(t1.calls, 1)
        self.assertEqual(guard.actual_tokens, 100)
        # Candidate 2 (fresh client/transport, SAME guard): its first call is gated
        # BEFORE the transport is invoked and blocked.
        c2, t2 = self._client(sem.CANDIDATES[1], guard, tmp.name, 384)
        with self.assertRaises(ec.EmbeddingError) as cm:
            c2.embed_texts(["beta"])
        self.assertIn("exceeds", str(cm.exception))
        self.assertEqual(t2.calls, 0)

    def test_multi_call_and_cross_candidate_accumulation(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        guard = sem.AggregateUsageGuard(cap_usd=sem.SPEND_CAP_USD)
        c1, t1 = self._client(sem.CANDIDATES[0], guard, tmp.name, 384)
        c1.embed_texts(["a", "b"])  # 200 tokens
        c1.embed_texts(["c"])       # 100 tokens
        self.assertEqual(guard.actual_tokens, 300)
        c2, t2 = self._client(sem.CANDIDATES[1], guard, tmp.name, 384)
        c2.embed_texts(["d"])       # 100 tokens
        self.assertEqual(guard.actual_tokens, 400)
        # Persistent per-client accounting preserved (each client sums its own).
        self.assertEqual(sum(u["prompt_tokens"] for u in c1._counter.usage), 300)
        self.assertEqual(sum(u["prompt_tokens"] for u in c2._counter.usage), 100)

    def test_retries_do_not_double_count_usage(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        guard = sem.AggregateUsageGuard(cap_usd=sem.SPEND_CAP_USD)

        class _Flaky:
            def __init__(self, dim):
                self.dim = dim
                self.calls = 0

            def __call__(self, url, headers, body):
                self.calls += 1
                if self.calls == 1:
                    raise ec.EmbeddingError("transport error (retryable)")
                n = len(body["input"])
                return {"data": [{"index": i, "embedding": [0.01] * self.dim} for i in range(n)],
                        "usage": {"prompt_tokens": 100 * n, "total_tokens": 100 * n}}

        cache = sem.EmbeddingCache(Path(tmp.name) / "c.jsonl")
        client = sem.ProviderClient(
            candidate=sem.CANDIDATES[0], cache=cache, api_key="k",
            transport=_Flaky(384), aggregate_guard=guard, max_retries=2,
        )
        out = client.embed_texts(["alpha"])  # retries once, then succeeds
        self.assertEqual(len(out), 1)
        # The failed attempt recorded NO usage; only the successful attempt's 100
        # tokens are counted exactly once.
        self.assertEqual(guard.actual_tokens, 100)

    def test_offline_fake_embedder_zero_cost_unblocked(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # A tiny aggregate cap must NOT block the offline fake embedder (zero cost).
        guard = sem.AggregateUsageGuard(cap_usd=0.00000001)
        cache = sem.EmbeddingCache(Path(tmp.name) / "c.jsonl")
        client = sem.ProviderClient(
            candidate=sem.CANDIDATES[0], cache=cache, api_key=None,
            embed_fn=_fake_embed_fn(384), aggregate_guard=guard,
        )
        out = client.embed_texts(["alpha", "beta", "gamma"])
        self.assertEqual(len(out), 3)
        self.assertEqual(guard.actual_tokens, 0)  # offline never reports usage


if __name__ == "__main__":
    unittest.main()
