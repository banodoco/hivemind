"""Focused pure tests for the task-2.13 pilot coverage evaluator (offline).

These freeze the C1–C6 gate math and the release-blocking invariants for
:mod:`executors.embedding_coverage` from constructed durable-state samples — no
PostgreSQL, no provider, no network.  Every verdict is shown to be derived from
integer counts and to flip when the durable state changes (coverage is proven
from data, never a constant).
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from executors import embedding_coverage as cov  # noqa: E402
from executors import workflow_representation as wr  # noqa: E402

sys.path.insert(0, str(_REPO / "scripts"))
import rehearse_embedding_pilot as pilot  # noqa: E402


def _chunk(i: int, *, rh: str | None = None, ch: str | None = None) -> cov.ChunkIdentity:
    return cov.ChunkIdentity(i, rh or f"rep{i:064d}", ch or f"chk{i:064d}")


def _rep(
    entity_type: str,
    item_id: str,
    representation_type: str,
    source: str,
    *,
    n_expected: int = 1,
    stored: cov.ChunkIdentity | None | list | None = None,
    eligible: bool = True,
) -> cov.RepresentationSample:
    """Build a representation sample.

    ``stored=None`` means "store exactly the expected set" (current);
    ``stored=[]`` means "store nothing"; a list/tuple is taken literally; a
    single ChunkIdentity means "store just that one".
    """

    expected = tuple(_chunk(i) for i in range(n_expected))
    if stored is None:
        stored_tuple = expected
    elif isinstance(stored, (list, tuple)):
        stored_tuple = tuple(stored)
    else:
        stored_tuple = (stored,)
    return cov.RepresentationSample(
        entity_type, item_id, representation_type, source, eligible, expected, stored_tuple
    )


class GateMathTests(unittest.TestCase):
    """C1–C6 verdicts derive from integer counts and flip with the data."""

    def test_clean_population_passes_every_gate(self):
        reps = [
            _rep("distillation", "1", "prose", "approved_distillations"),
            _rep("distillation", "2", "prose", "pending_distillations"),
            _rep("resource", "10", "prose", "resources"),
            _rep("resource", "11", "prose", "resources"),
            _rep("resource", "11", "workflow_python", "resources"),
            _rep("resource", "14", "prose", "resources"),
            _rep("resource", "14", "workflow_python", "resources"),
            _rep("message", "9007199254740993", "prose", "messages"),
            _rep("message", "9007199254740992", "prose", "messages"),
        ]
        workflows = [
            cov.WorkflowSample("11", "payload_python", "safe", True, None),
            # A recoverable workflow that remediation materialized (C3 denom > 0).
            cov.WorkflowSample("14", "recoverable", "safe", True, None),
        ]
        report = cov.evaluate_coverage(reps, workflows, reason_codes=["skipped_current"])
        for gid in ("C1", "C2", "C3", "C4", "C5", "C6"):
            self.assertTrue(report.gates[gid].passed, gid)
        self.assertTrue(report.all_gates_passed)
        self.assertTrue(report.invariants_clean)
        self.assertTrue(report.reason_vocabulary_bounded)
        # C1/C2/C3/C5 are exactly the eligible population.
        self.assertEqual((report.gates["C1"].numerator, report.gates["C1"].denominator), (7, 7))
        self.assertEqual((report.gates["C2"].numerator, report.gates["C2"].denominator), (2, 2))
        self.assertEqual((report.gates["C3"].numerator, report.gates["C3"].denominator), (1, 1))
        self.assertEqual((report.gates["C5"].numerator, report.gates["C5"].denominator), (0, 9))

    def test_c1_fails_when_an_eligible_representation_is_missing(self):
        reps = [
            _rep("distillation", "1", "prose", "approved_distillations"),
            _rep("resource", "10", "prose", "resources", stored=[]),  # not indexed
        ]
        report = cov.evaluate_coverage(reps, [])
        self.assertFalse(report.gates["C1"].passed)
        self.assertEqual((report.gates["C1"].numerator, report.gates["C1"].denominator), (1, 2))

    def test_c2_boundary_95_percent_passes_and_below_fails(self):
        # 19 of 20 eligible message representations current -> 0.95 -> PASS.
        reps = [_rep("message", str(i), "prose", "messages") for i in range(19)]
        reps.append(_rep("message", "19", "prose", "messages", stored=[]))  # missing
        report = cov.evaluate_coverage(reps, [])
        self.assertEqual(report.gates["C2"].rate, 0.95)
        self.assertTrue(report.gates["C2"].passed)
        # 18 of 20 -> 0.9 -> FAIL.
        reps.append(_rep("message", "20", "prose", "messages", stored=[]))
        report = cov.evaluate_coverage(reps, [])
        self.assertFalse(report.gates["C2"].passed)

    def test_c3_fails_when_a_recoverable_workflow_is_not_materialized(self):
        wf = [cov.WorkflowSample("14", "recoverable", "safe", False, None)]
        report = cov.evaluate_coverage([], wf)
        self.assertFalse(report.gates["C3"].passed)
        self.assertEqual((report.gates["C3"].numerator, report.gates["C3"].denominator), (0, 1))

    def test_c4_fails_when_an_unavailable_workflow_lacks_a_reason(self):
        no_reason = cov.WorkflowSample("15", "unavailable", "safe", False, None)
        with_reason = cov.WorkflowSample(
            "16", "unavailable", "safe", False, "no_source_artifact"
        )
        report = cov.evaluate_coverage([], [no_reason, with_reason])
        # One unavailable row is missing its explicit reason.
        self.assertFalse(report.gates["C4"].passed)
        self.assertEqual((report.gates["C4"].numerator, report.gates["C4"].denominator), (1, 2))
        # Both reasons present -> C4 clean.
        report2 = cov.evaluate_coverage(
            [], [cov.WorkflowSample("15", "unavailable", "safe", False, "conversion_error"), with_reason]
        )
        self.assertTrue(report2.gates["C4"].passed)


class FreshnessAndInvariantsTests(unittest.TestCase):
    """The exact-set freshness test and the no-quirk invariants."""

    def test_current_detects_missing_chunk(self):
        s = _rep("resource", "10", "prose", "resources", n_expected=2, stored=[_chunk(0)])
        self.assertFalse(s.current)

    def test_current_detects_mismatched_hash(self):
        bad = cov.ChunkIdentity(0, "x" * 64, "y" * 64)
        s = _rep("resource", "10", "prose", "resources", stored=[bad])
        self.assertFalse(s.current)

    def test_current_detects_stale_tail_chunk(self):
        # An older chunker left a tail chunk beyond the expected set.
        stale = _rep(
            "resource", "10", "prose", "resources", n_expected=1,
            stored=[_chunk(0), _chunk(1)],
        )
        self.assertFalse(stale.current)

    def test_stale_tail_drives_c5(self):
        reps = [
            _rep("distillation", "1", "prose", "approved_distillations"),
            _rep("resource", "10", "prose", "resources", n_expected=1,
                 stored=[_chunk(0), _chunk(1)]),  # stale tail
        ]
        report = cov.evaluate_coverage(reps, [])
        self.assertFalse(report.gates["C5"].passed)
        self.assertEqual((report.gates["C5"].numerator, report.gates["C5"].denominator), (1, 2))

    def test_quarantined_python_storage_is_c6_and_invariant(self):
        # Quarantined Python must never be stored: model it as a non-eligible,
        # empty-expected representation.  A stored chunk there is a release blocker.
        quarantined = cov.RepresentationSample(
            "resource", "16", "workflow_python", "resources", False, (), [_chunk(0)]
        )
        report = cov.evaluate_coverage([quarantined], [
            cov.WorkflowSample("16", "payload_python", "quarantined", False, "quarantined"),
        ])
        self.assertFalse(report.gates["C6"].passed)
        self.assertEqual(report.quarantined_python_vectors, 1)
        self.assertFalse(report.invariants_clean)

    def test_quarantined_python_with_zero_storage_is_clean(self):
        quarantined = cov.RepresentationSample(
            "resource", "16", "workflow_python", "resources", False, (), ()
        )
        prose = _rep("resource", "16", "prose", "resources")  # safe prose stays eligible
        report = cov.evaluate_coverage([prose, quarantined], [
            cov.WorkflowSample("16", "payload_python", "quarantined", False, "quarantined"),
        ])
        self.assertTrue(report.gates["C6"].passed)
        self.assertEqual(report.quarantined_python_vectors, 0)
        self.assertTrue(report.invariants_clean)

    def test_repeated_chunk_hash_within_a_stream_is_flagged(self):
        dup = cov.RepresentationSample(
            "resource", "13", "workflow_python", "resources", True,
            (_chunk(0), _chunk(1)),
            # Same chunk_hash at two indexes -> identical bytes indexed twice.
            (cov.ChunkIdentity(0, "r0", "DUPHASH"), cov.ChunkIdentity(1, "r1", "DUPHASH")),
        )
        report = cov.evaluate_coverage([dup], [])
        self.assertEqual(report.duplicate_chunk_hashes_within_representation, 1)

    def test_ineligible_message_storage_is_c6(self):
        # A deleted message that nevertheless has stored vectors is ineligible.
        deleted = cov.RepresentationSample(
            "message", "999", "prose", "messages", False, (), [_chunk(0)]
        )
        report = cov.evaluate_coverage([deleted], [])
        self.assertFalse(report.gates["C6"].passed)

    def test_reason_vocabulary_outside_the_bounded_set_is_flagged(self):
        report = cov.evaluate_coverage([], [], reason_codes=["skipped_current", "made_up_reason"])
        self.assertFalse(report.reason_vocabulary_bounded)


class EnvelopeSafetyTests(unittest.TestCase):
    """The emitted envelope stays secret- and URL-free."""

    def test_envelope_carries_no_secret_or_url_substring(self):
        reps = [_rep("distillation", "1", "prose", "approved_distillations")]
        report = cov.evaluate_coverage(reps, [], reason_codes=["skipped_current"])
        envelope = report.to_envelope()
        text = json.dumps(envelope, sort_keys=True)
        # No matched secret values (defense in depth via the frozen scanner).
        self.assertEqual(wr.scan_secrets(text), [])
        for forbidden in ("postgresql://", "password", "OPENAI_API_KEY", "sk-"):
            self.assertNotIn(forbidden, text)
        # Gate structure is present and integer-evidenced.
        self.assertIn("C1", envelope["gates"])
        self.assertEqual(envelope["gates"]["C1"]["numerator"], 1)
        self.assertEqual(envelope["gates"]["C1"]["denominator"], 1)
        self.assertTrue(envelope["gates"]["C1"]["passed"])

    def test_rate_helper_treats_empty_population_as_perfect(self):
        self.assertEqual(cov.rate(0, 0), 1.0)
        self.assertEqual(cov.rate(3, 4), 0.75)


class RequiredPopulationTests(unittest.TestCase):
    """The pilot must prove non-empty denominators for its required gates.

    The generic ``rate(0, 0) == 1.0`` convention makes a coverage-equality gate
    vacuously pass on an empty population; the rehearsal's required-population
    check turns that into an evidenced, non-vacuous pass for the real pilot and
    must fail (and thus fail the rehearsal) when a required cohort is missing.
    """

    def test_clean_pilot_populations_pass_required_check(self):
        reps = [
            _rep("distillation", "1", "prose", "approved_distillations"),
            _rep("resource", "10", "prose", "resources"),
            _rep("message", "9007199254740993", "prose", "messages"),
        ]
        workflows = [
            cov.WorkflowSample("14", "recoverable", "safe", True, None),
            cov.WorkflowSample("15", "unavailable", "safe", False, "no_source_artifact"),
        ]
        report = cov.evaluate_coverage(reps, workflows)
        # The fixture seeds all four required cohorts -> non-empty denominators.
        for gid in pilot.REQUIRED_PILOT_GATES:
            self.assertGreater(report.gates[gid].denominator, 0, gid)
        self.assertTrue(pilot._required_populations_nonempty(report))

    def test_missing_workflows_fail_required_check_despite_vacuous_gate_pass(self):
        # No workflow records: C3/C4 denominators are 0.  Their ==-1.0 / ==-0
        # gate verdicts still PASS vacuously under the generic convention...
        reps = [
            _rep("distillation", "1", "prose", "approved_distillations"),
            _rep("message", "9007199254740993", "prose", "messages"),
        ]
        report = cov.evaluate_coverage(reps, [])  # workflows discarded, as the bug did
        self.assertTrue(report.gates["C3"].passed)  # vacuous 0/0 == 1.0
        self.assertTrue(report.gates["C4"].passed)  # vacuous 0 of 0
        # ...but the required-population evidence check catches the missing cohort.
        self.assertFalse(pilot._required_populations_nonempty(report))
        self.assertEqual(report.gates["C3"].denominator, 0)
        self.assertEqual(report.gates["C4"].denominator, 0)


if __name__ == "__main__":
    unittest.main()
