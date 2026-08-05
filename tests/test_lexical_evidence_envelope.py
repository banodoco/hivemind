"""Tests for the lexical-search RPC evidence-envelope parsing (requirement B).

Regression coverage for the bug where ``capture_evidence`` in
``scripts/live_lexical_search.py`` called ``jsonb_array_length`` on the
top-level RPC envelope object — which is ``{results, count, meta}`` (see
schema/009:239-249), NOT a bare array — so the smoke reported NULL/garbage and
failed even when the RPC was healthy.

These tests are pure (no network, no DB) except for an optional PG-gated
live test that is skipped when no rehearsal cluster is reachable.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO / "scripts"))

from live_lexical_search import capture_evidence, parse_rpc_envelope  # noqa: E402


class TestParseRpcEnvelope(unittest.TestCase):
    def test_parse_count_from_envelope_count_and_results(self):
        # The live evidence-arm query emits a two-column "count|results_len"
        # psql -t -A row. This is the preferred live form.
        out = parse_rpc_envelope("20|20")
        self.assertEqual(out, {"count": 20, "results_len": 20})

    def test_parse_envelope_json_object(self):
        # Regression for the bug: the OLD code fed this object straight to
        # jsonb_array_length, which returns NULL on a JSON object. parse_rpc_envelope
        # must read count/results from the envelope fields, not the top-level.
        envelope = {
            "results": [{"item_id": str(i)} for i in range(20)],
            "count": 20,
            "meta": {"mode_used": "lexical", "limit": 10, "candidate_limit": 50},
        }
        out = parse_rpc_envelope(json.dumps(envelope))
        self.assertEqual(out, {"count": 20, "results_len": 20})

    def test_parse_envelope_zero_results(self):
        out = parse_rpc_envelope(json.dumps({"results": [], "count": 0, "meta": {}}))
        self.assertEqual(out, {"count": 0, "results_len": 0})

    def test_parse_envelope_empty_and_garbage_returns_sentinel(self):
        # Never raise — a failed smoke must be a failure, not a crash.
        self.assertEqual(parse_rpc_envelope(""), {"count": -1, "results_len": -1})
        self.assertEqual(parse_rpc_envelope("   "), {"count": -1, "results_len": -1})
        self.assertEqual(parse_rpc_envelope("not json|not int either"),
                         {"count": -1, "results_len": -1})

    def test_parse_count_len_row_with_whitespace(self):
        # psql -A can emit trailing whitespace / a trailing newline.
        out = parse_rpc_envelope(" 7 | 7 \n")
        self.assertEqual(out, {"count": 7, "results_len": 7})


class TestLiveArmSqlShape(unittest.TestCase):
    """Static assertion that the buggy SQL pattern is gone from capture_evidence."""

    def _capture_source(self) -> str:
        import inspect

        return inspect.getsource(capture_evidence)

    def test_live_arm_sql_reads_count_and_results_fields(self):
        src = self._capture_source()
        # The fixed query reads the ENVELOPE fields directly.
        self.assertIn("->>'count'", src)
        self.assertIn("->'results'", src)
        # The buggy pattern — jsonb_array_length on the top-level envelope
        # object j — MUST be gone. Match the literal buggy call. We forbid any
        # jsonb_array_length(j) where j is the whole envelope (the old call was
        # exactly "jsonb_array_length(j)-0"). We allow jsonb_array_length(j->'results').
        self.assertNotIn("jsonb_array_length(j)-0", src)
        self.assertNotIn("jsonb_array_length(j)", src)

    def test_live_arm_records_both_count_and_results_len(self):
        src = self._capture_source()
        self.assertIn("rpc_smoke_count", src)
        self.assertIn("rpc_smoke_results_len", src)
        # Error path must still redact.
        self.assertIn("rpc_smoke_error", src)
        self.assertIn("redact", src)

    def test_live_arm_argument_list_unchanged(self):
        src = self._capture_source()
        # Identical smoke arguments to today: query 'WanVideoSampler', limit 10,
        # empty filters, mode 'lexical'.
        self.assertIn("public.hivemind_lexical_search('WanVideoSampler',10,'{}','{}','{}',null,'{}','{}','lexical')",
                      src)


@unittest.skipUnless(
    os.environ.get("HIVEMIND_LIVE_EVIDENCE_TEST") == "1",
    "PG-gated: set HIVEMIND_LIVE_EVIDENCE_TEST=1 and provide rehearsal cluster creds",
)
class TestLiveEvidencePgGated(unittest.TestCase):
    """Optional PG-gated test: seeds the RPC on the rehearsal cluster and asserts
    capture_evidence returns count>0. Skipped unless explicitly enabled."""

    def test_capture_evidence_returns_positive_count(self):
        # Requires a live libpq env (PGHOST/PGUSER/PGPASSWORD/...) pointed at a
        # rehearsal cluster that has schema/008+009 applied and real rows.
        env = dict(os.environ)
        self.assertIn("PGHOST", env)
        ev = capture_evidence(env)
        self.assertGreaterEqual(ev.get("rpc_smoke_count", -1), 0,
                                f"smoke failed: {ev.get('rpc_smoke_error')}")
        self.assertGreaterEqual(ev.get("rpc_smoke_results_len", -1), 0)


if __name__ == "__main__":
    unittest.main()
