"""Pilot embedding coverage + gate evaluator (plan task 2.13).

This is the **pure** half of the task-2.13 pilot rehearsal.  It takes
durable-state *samples* — the expected chunk identity/hash set for every
eligible representation (recomputed with the frozen
:mod:`canonical_representations` + :mod:`chunking` path the backfill uses) and
the *actual* chunk set stored in ``content_embeddings`` — and evaluates the
frozen coverage gates C1–C6 from
``docs/hybrid-search/phase0-workflow-representation-contract.json``.

It deliberately proves coverage from data, never from a constant: every verdict
carries an integer numerator, an integer denominator, and the rate derived from
them.  The DB-collecting half (which builds these samples from a direct/session
``PsqlSession`` — a local throwaway cluster in rehearsal, a real session in an
operator audit) lives in the rehearsal script so this module stays free of the
PostgreSQL adapter (and its import cycle).

Design rules (frozen contract §gates.coverage + the 2.12 handoff):

* **One freshness test.**  A representation is *current* iff the stored
  ``{(chunk_index, representation_hash, chunk_hash)}`` set equals the expected
  set exactly.  That single comparison detects a missing chunk, a mismatched
  hash, **and** a stale tail chunk an older chunker left behind (C5 / no-silent-
  truncation).  It is the same comparison :func:`PostgresBackfillStore.
  representation_current` performs at write time.
* **Eligibility is the source of truth for C6.**  A representation is
  *eligible* iff its item belongs to an enabled source cohort and the
  representation is applicable (safe workflow Python; never quarantined Python).
  C6 counts any *stored* representation that is not eligible+expected — a
  quarantined-Python vector, a deleted-message vector, a rejected-distillation
  vector — and that count must be zero (release blocker).  Quarantined Python is
  therefore modelled as a non-eligible, empty-expected representation sample so
  that any illicit storage is detected here, not hidden.
* **No double indexing.**  Per representation the stored chunk hashes are
  unique; identical body/payload Python yields exactly one ``workflow_python``
  representation whose stored set equals the single authoritative copy (the
  builder structurally strips the duplicate from prose).
* **Bounded, nonsecret vocabulary.**  Only ids / counts / rates / cohort /
  public-state / nonsecret reason codes are emitted — never source bodies,
  Python text, matched secrets, hashes of secrets, DB URLs, or provider keys.

Pure stdlib, offline, dependency-free.  It reuses the frozen representation +
chunker modules verbatim; it does not re-derive canonicalization or chunking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from executors import workflow_representation as wr
from executors.workflow_representation import (
    COHORT_RECOVERABLE,
    COHORT_UNAVAILABLE,
    PUBLIC_STATE_QUARANTINED,
    REP_PROSE,
    REP_WORKFLOW_PYTHON,
)

__all__ = [
    "GATE_C1",
    "GATE_C2",
    "GATE_C3",
    "GATE_C4",
    "GATE_C5",
    "GATE_C6",
    "C1_SOURCES",
    "MESSAGE_SOURCES",
    "ALLOWED_REASON_CODES",
    "ChunkIdentity",
    "RepresentationSample",
    "WorkflowSample",
    "GateVerdict",
    "CoverageReport",
    "evaluate_coverage",
    "rate",
]

# ---------------------------------------------------------------------------
# Frozen gate identities (mirror phase0-workflow-representation-contract.json)
# ---------------------------------------------------------------------------

GATE_C1 = "C1"  # eligible distillations + resource representations coverage == 1.0
GATE_C2 = "C2"  # enabled message-cohort coverage >= 0.95
GATE_C3 = "C3"  # recoverable workflows materialized/versioned/hashed Python == 1.0
GATE_C4 = "C4"  # unavailable rows without an explicit nonsecret reason == 0
GATE_C5 = "C5"  # stale/mismatched representation hashes < 0.001
GATE_C6 = "C6"  # ineligible indexed items == 0 (release blocker)

#: Distillation + resource sources that C1 covers.
C1_SOURCES: frozenset[str] = frozenset(
    {"approved_distillations", "pending_distillations", "distillations", "resources"}
)

#: Message sources that C2 covers (the enabled cohort).
MESSAGE_SOURCES: frozenset[str] = frozenset({"messages"})

#: Bounded, nonsecret reason vocabulary observed across coverage + the backfill.
#: Mirrors ``embedding_backfill`` reason codes and the remediation reason codes;
#: any reason outside this set fails the envelope's vocabulary check.
ALLOWED_REASON_CODES: frozenset[str] = frozenset(
    {
        # backfill engine
        "remediation_failed",
        "transient_provider_error",
        "permanent_provider_error",
        "retries_exhausted",
        "skipped_current",
        "representation_unavailable",
        "quarantined_python_excluded",
        "checkpoint_failed",
        "interrupted",
        "cost_cap_reached",
        # workflow remediation (nonsecret Python-absence reasons)
        "no_source_artifact",
        "corpus_path_unresolvable",
        "conversion_error",
        "exporter_unavailable",
        "secret_quarantine_blocks_public",
        "quarantined",
        "corpus_path_unauthorized",
    }
)

_C5_THRESHOLD = 0.001
_C2_THRESHOLD = 0.95


def rate(numerator: int, denominator: int) -> float:
    """Return ``numerator / denominator`` as a float (1.0 when denominator is 0).

    A zero-denominator gate (no eligible population) is conventionally perfect:
    there is nothing to cover and nothing to violate.
    """

    if denominator <= 0:
        return 1.0
    return numerator / denominator


# ---------------------------------------------------------------------------
# Samples (durable-state views; never carry source text or secret values)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkIdentity:
    """One chunk's frozen identity: positional index + the two frozen hashes."""

    chunk_index: int
    representation_hash: str
    chunk_hash: str


@dataclass(frozen=True)
class RepresentationSample:
    """Expected-vs-actual coverage for one (entity, item, representation).

    ``expected`` is the chunk identity set the frozen builder+chunker produces
    for this representation (empty when the representation must not exist — e.g.
    quarantined workflow Python).  ``stored`` is the chunk identity set currently
    in ``content_embeddings``.  ``eligible`` is True iff the item belongs to an
    enabled source cohort AND the representation is applicable (safe Python, not
    quarantined).  Comparing the two sets as sets is the freshness test.
    """

    entity_type: str
    item_id: str
    representation_type: str
    source: str
    eligible: bool
    expected: tuple[ChunkIdentity, ...] = ()
    stored: tuple[ChunkIdentity, ...] = ()

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.entity_type, self.item_id, self.representation_type)

    @property
    def expected_set(self) -> set[tuple[int, str, str]]:
        return {
            (c.chunk_index, c.representation_hash, c.chunk_hash)
            for c in self.expected
        }

    @property
    def stored_set(self) -> set[tuple[int, str, str]]:
        return {
            (c.chunk_index, c.representation_hash, c.chunk_hash)
            for c in self.stored
        }

    @property
    def expected_nonempty(self) -> bool:
        return bool(self.expected)

    @property
    def has_vectors(self) -> bool:
        return bool(self.stored)

    @property
    def current(self) -> bool:
        """Stored identity/hash set equals the expected set exactly.

        Catches a missing chunk, a mismatched hash, and a stale tail chunk left
        by an older chunker (the no-silent-truncation guarantee, C5).
        """

        return self.stored_set == self.expected_set


@dataclass(frozen=True)
class WorkflowSample:
    """Secret-safe workflow facts for the C3/C4 workflow gates.

    Carries only the cohort, the cross-cutting public state, the
    materialized/versioned/hashed flag, and a NONSECRET unavailable reason —
    never the Python text, the matched secret value, or a hash of a secret.
    """

    item_id: str
    cohort: str
    public_state: str
    materialized_versioned_hashed: bool
    unavailable_reason: str | None


# ---------------------------------------------------------------------------
# Verdict + report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateVerdict:
    """One gate's integer evidence + comparator/threshold + pass/fail."""

    id: str
    metric: str
    comparator: str
    threshold: float
    numerator: int
    denominator: int

    @property
    def rate(self) -> float:
        return rate(self.numerator, self.denominator)

    @property
    def passed(self) -> bool:
        if self.comparator == "==":
            if self.threshold == 1.0:
                # Coverage equality: numerator == denominator.  An empty
                # population is conventionally perfect (matches ``rate``); a
                # real pilot guarantees non-empty denominators (asserted by the
                # rehearsal) so this is never a trivial pass in evidence.
                return self.numerator == self.denominator
            # threshold 0 -> a count that must equal zero
            return self.numerator == 0
        if self.comparator == ">=":
            return self.rate >= self.threshold
        if self.comparator == "<":
            return self.rate < self.threshold
        raise ValueError(f"unknown comparator {self.comparator!r}")

    def to_dict(self) -> dict:
        return {
            "metric": self.metric,
            "comparator": self.comparator,
            "threshold": self.threshold,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "rate": round(self.rate, 6),
            "passed": self.passed,
        }


@dataclass(frozen=True)
class CoverageReport:
    """Aggregate C1–C6 verdicts + the release-blocking invariants."""

    gates: dict[str, GateVerdict] = field(default_factory=dict)
    # Counts that must be zero (named, secret-safe invariants).
    quarantined_python_vectors: int = 0
    ineligible_indexed_representations: int = 0
    orphan_vectors: int = 0
    duplicate_chunk_hashes_within_representation: int = 0
    workflow_python_reps_max_per_item: int = 0
    # Bounded vocabulary actually observed (subset check against ALLOWED).
    reason_codes: tuple[str, ...] = ()
    extra: dict = field(default_factory=dict)

    @property
    def all_gates_passed(self) -> bool:
        return all(g.passed for g in self.gates.values())

    @property
    def invariants_clean(self) -> bool:
        return (
            self.quarantined_python_vectors == 0
            and self.ineligible_indexed_representations == 0
            and self.orphan_vectors == 0
            and self.duplicate_chunk_hashes_within_representation == 0
            and self.workflow_python_reps_max_per_item <= 1
        )

    @property
    def reason_vocabulary_bounded(self) -> bool:
        return all(code in ALLOWED_REASON_CODES for code in self.reason_codes)

    def to_envelope(self) -> dict:
        """Return the bounded, secret-free coverage envelope.

        Only gate ids / metrics / integer numerators+denominators / rates /
        verdicts, the named invariants, and the observed reason vocabulary are
        emitted — never source bodies, Python text, matched secrets, hashes of
        secrets, DB URLs, or provider keys.
        """

        return {
            "gates": {gid: g.to_dict() for gid, g in sorted(self.gates.items())},
            "invariants": {
                "quarantined_python_vectors": self.quarantined_python_vectors,
                "ineligible_indexed_representations": self.ineligible_indexed_representations,
                "orphan_vectors": self.orphan_vectors,
                "duplicate_chunk_hashes_within_representation": (
                    self.duplicate_chunk_hashes_within_representation
                ),
                "workflow_python_reps_max_per_item": self.workflow_python_reps_max_per_item,
            },
            "invariants_clean": self.invariants_clean,
            "reason_codes": sorted(set(self.reason_codes)),
            "reason_vocabulary_bounded": self.reason_vocabulary_bounded,
            "all_gates_passed": self.all_gates_passed,
            "extra": dict(self.extra),
        }


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------


def _coverage_gate(samples: Iterable[RepresentationSample]) -> tuple[int, int]:
    """Count (current, eligible+expected) over a representation partition."""

    num = 0
    den = 0
    for s in samples:
        if not (s.eligible and s.expected_nonempty):
            continue
        den += 1
        if s.current:
            num += 1
    return num, den


def evaluate_coverage(
    representations: Iterable[RepresentationSample],
    workflows: Iterable[WorkflowSample] | None = None,
    *,
    reason_codes: Iterable[str] = (),
    extra: dict | None = None,
) -> CoverageReport:
    """Evaluate C1–C6 (+ release-blocking invariants) over durable-state samples.

    Every verdict is derived from the integer counts in *representations* and
    *workflows*; nothing here is a constant.  Pass/fail follows the frozen
    comparator/threshold per gate.
    """

    reps = list(representations)
    wf = list(workflows or [])

    # C1 — eligible distillation + resource representations covered exactly.
    c1_reps = [r for r in reps if r.source in C1_SOURCES]
    c1_num, c1_den = _coverage_gate(c1_reps)
    # C2 — enabled message-cohort coverage >= 0.95.
    c2_reps = [r for r in reps if r.source in MESSAGE_SOURCES]
    c2_num, c2_den = _coverage_gate(c2_reps)
    # C5 — stale/mismatched representation hashes across every eligible stream.
    c5_num, c5_den = _coverage_gate(reps)
    c5_stale = c5_den - c5_num

    # C3 — recoverable workflows with materialized/versioned/hashed Python.
    recoverable = [w for w in wf if w.cohort == COHORT_RECOVERABLE]
    c3_den = len(recoverable)
    c3_num = sum(1 for w in recoverable if w.materialized_versioned_hashed)

    # C4 — unavailable workflows must each carry an explicit nonsecret reason.
    unavailable = [w for w in wf if w.cohort == COHORT_UNAVAILABLE]
    c4_den = len(unavailable)
    c4_num = sum(
        1 for w in unavailable if not (w.unavailable_reason or "").strip()
    )  # rows MISSING a reason

    # C6 — no stored representation that is not eligible+expected.
    c6_num = sum(
        1
        for r in reps
        if r.has_vectors and not (r.eligible and r.expected_nonempty)
    )

    gates: dict[str, GateVerdict] = {
        GATE_C1: GateVerdict(
            GATE_C1,
            "active_contract_coverage_distillations_and_resources",
            "==",
            1.0,
            c1_num,
            c1_den,
        ),
        GATE_C2: GateVerdict(
            GATE_C2,
            "active_contract_coverage_enabled_message_cohort",
            ">=",
            _C2_THRESHOLD,
            c2_num,
            c2_den,
        ),
        GATE_C3: GateVerdict(
            GATE_C3,
            "recoverable_workflows_with_materialized_versioned_hashed_python",
            "==",
            1.0,
            c3_num,
            c3_den,
        ),
        GATE_C4: GateVerdict(
            GATE_C4,
            "unavailable_rows_without_explicit_non_secret_reason",
            "==",
            0,
            c4_num,
            c4_den,
        ),
        GATE_C5: GateVerdict(
            GATE_C5,
            "stale_or_mismatched_representation_hashes",
            "<",
            _C5_THRESHOLD,
            c5_stale,
            c5_den,
        ),
        GATE_C6: GateVerdict(
            GATE_C6,
            "ineligible_indexed_items",
            "==",
            0,
            c6_num,
            0,  # C6 is a release-blocking count, not a rate
        ),
    }

    # Named invariants (all must be zero / within bound).
    quarantined_python_vectors = sum(
        len(r.stored)
        for r in reps
        if r.entity_type == "resource"
        and r.representation_type == REP_WORKFLOW_PYTHON
        and not r.eligible
    )
    orphan_vectors = sum(
        len(r.stored)
        for r in reps
        if r.has_vectors and r.eligible and not r.expected_nonempty
    )
    # Identical bytes indexed twice within one stream would repeat a chunk_hash.
    # (The frozen builder already strips a duplicate body block so the
    # authoritative Python is emitted once; this is the belt-and-suspenders
    # durability check that no second copy slipped into storage.)
    duplicate_chunk_hashes = 0
    for r in reps:
        if not r.stored:
            continue
        hashes = [c.chunk_hash for c in r.stored]
        if len(hashes) != len(set(hashes)):
            duplicate_chunk_hashes += 1
    # A workflow must have at most one workflow_python representation stream.
    py_reps_per_item: dict[str, int] = {}
    for r in reps:
        if r.entity_type == "resource" and r.representation_type == REP_WORKFLOW_PYTHON:
            py_reps_per_item[r.item_id] = py_reps_per_item.get(r.item_id, 0) + 1
    py_reps_max = max(py_reps_per_item.values(), default=0)

    observed_reasons = sorted({c for c in reason_codes if c})

    return CoverageReport(
        gates=gates,
        quarantined_python_vectors=quarantined_python_vectors,
        ineligible_indexed_representations=c6_num,
        orphan_vectors=orphan_vectors,
        duplicate_chunk_hashes_within_representation=duplicate_chunk_hashes,
        workflow_python_reps_max_per_item=py_reps_max,
        reason_codes=tuple(observed_reasons),
        extra=dict(extra or {}),
    )


# Defense-in-depth: re-export the frozen scanner so callers can assert the
# emitted envelope carries no matched secret value without a second import path.
scan_envelope_for_secrets = wr.scan_secrets
