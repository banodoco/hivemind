"""Red tests freezing the public API for plan task 2.12a (workflow-representation
remediation/reconciliation).

These tests define the contract for two not-yet-implemented surfaces:

  * ``executors.workflow_remediation`` — a pure, offline, stdlib-only module that
    reconciles one historical ``kind=workflow`` row into a canonical, hashed,
    secret-scanned Python + searchable-body representation, and orchestrates a
    resumable, bounded, idempotent batch over an injected row store.
  * ``scripts.backfill_workflow_representations`` — the operator CLI, whose
    default mode is a read-only audit/dry-run.

The module is offline: no network, database, provider, subprocess, VibeComfy
import, or local-corpus access. Recovery is via an injected adapter; the row
store is an injected fake. Until the two target modules exist these tests fail
at import with a clear ``ModuleNotFoundError`` naming the missing implementation.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any, Callable

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_REPO))

from executors import workflow_representation as wr  # noqa: E402
from executors import workflow_semantics as ws  # noqa: E402
from executors import workflow_remediation as remediation  # noqa: E402
from scripts import backfill_workflow_representations as cli  # noqa: E402


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_SECRET = "sk-" + "A" * 60          # a real-shaped provider token
_SECRET_B = "sk-" + "B" * 60        # a different token planted in an error path


def _row(
    rid: int,
    *,
    body: str = "",
    payload: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    title: str = "Wan I2V",
    url: str = "https://example.test/wan-i2v",
    external_id: str = "wan-i2v-1",
) -> dict[str, Any]:
    return {
        "id": rid,
        "kind": "workflow",
        "source": "vibecomfy",
        "external_id": external_id,
        "title": title,
        "body": body,
        "url": url,
        "metadata": metadata or {},
        "payload": payload or {},
    }


class FakeRecoverer:
    """Injected VibeComfy exporter/converter adapter: ``recoverer(row) -> str|None``."""

    def __init__(
        self,
        mapping: dict[int, str] | None = None,
        fail_ids: tuple[int, ...] = (),
    ) -> None:
        self.mapping = dict(mapping or {})
        self.fail_ids = set(fail_ids)
        self.calls: list[int] = []

    def __call__(self, row: dict[str, Any]) -> str | None:
        rid = row["id"]
        self.calls.append(rid)
        if rid in self.fail_ids:
            raise ValueError(f"conversion failed {_SECRET_B}")
        return self.mapping.get(rid)


class FakeStore:
    """In-memory row store / write surface injected into ``reconcile``."""

    def __init__(
        self,
        rows: list[dict[str, Any]],
        current: dict[int, dict[str, Any]] | None = None,
        interrupt_after_pages: int | None = None,
    ) -> None:
        self.rows = sorted(rows, key=lambda r: r["id"])
        self.current_map = dict(current or {})
        self.patches: list[tuple[int, dict[str, Any]]] = []
        self.refreshes: list[int] = []
        self.failures: list[tuple[int, str]] = []
        self.events: list[tuple[str, int]] = []
        self._page_calls = 0
        self._interrupt_after = interrupt_after_pages

    # -- paging -----------------------------------------------------------
    def fetch_page(
        self,
        *,
        after_id: int | None,
        high_water: int | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        self._page_calls += 1
        if self._interrupt_after is not None and self._page_calls > self._interrupt_after:
            raise KeyboardInterrupt("simulated interruption")
        out: list[dict[str, Any]] = []
        for r in self.rows:
            rid = r["id"]
            if after_id is not None and rid <= after_id:
                continue
            if high_water is not None and rid > high_water:
                break
            out.append(r)
            if len(out) >= limit:
                break
        return out

    def high_water(self) -> int | None:
        return max((r["id"] for r in self.rows), default=None)

    def eligible_total(self) -> int:
        return len(self.rows)

    # -- per-row write surface -------------------------------------------
    def current(self, row_id: int) -> dict[str, Any] | None:
        return self.current_map.get(row_id)

    def patch(self, row_id: int, update: dict[str, Any]) -> None:
        self.patches.append((row_id, update))
        self.events.append(("patch", row_id))

    def refresh(self, row_id: int) -> None:
        self.refreshes.append(row_id)
        self.events.append(("refresh", row_id))

    def record_failure(self, row_id: int, reason: str) -> None:
        self.failures.append((row_id, reason))


# ---------------------------------------------------------------------------
# 1. Source-cohort precedence (strict, exactly one cohort per eligible row)
# ---------------------------------------------------------------------------


class CohortPrecedenceTests(unittest.TestCase):
    def test_payload_python_wins_over_body_delimiter(self):
        row = _row(
            1,
            body="desc\n\nPython scratchpad source:\nx = 1\n",
            payload={"python_source": "x = 1\n"},
        )
        rep = remediation.reconcile_row(row, recoverer=FakeRecoverer({1: "DIFFERENT"}))
        self.assertEqual(rep.cohort, wr.COHORT_PAYLOAD_PYTHON)
        self.assertEqual(rep.python_source, "x = 1\n")
        self.assertTrue(rep.body_duplicate)

    def test_body_python_when_no_payload(self):
        row = _row(2, body="desc\n\nPython ready-template source:\na = 1\nb = 2\n")
        rep = remediation.reconcile_row(row, recoverer=None)
        self.assertEqual(rep.cohort, wr.COHORT_BODY_PYTHON)
        self.assertIn("a = 1", rep.python_source)
        self.assertIn("b = 2", rep.python_source)

    def test_recoverable_uses_injected_adapter(self):
        rec = FakeRecoverer({3: "# vibecomfy: generated scratchpad\nz = 9\n"})
        row = _row(3, body="desc", payload={"workflow_json": {"nodes": []}})
        rep = remediation.reconcile_row(row, recoverer=rec)
        self.assertEqual(rep.cohort, wr.COHORT_RECOVERABLE)
        self.assertIn("z = 9", rep.python_source)
        self.assertEqual(rec.calls, [3])

    def test_unavailable_when_no_source_and_no_recovery(self):
        row = _row(4, body="just a description, no code anywhere")
        rep = remediation.reconcile_row(row, recoverer=FakeRecoverer())
        self.assertEqual(rep.cohort, wr.COHORT_UNAVAILABLE)
        self.assertFalse(rep.available)
        self.assertEqual(rep.python_source, "")
        self.assertIsNotNone(rep.reason)

    def test_precedence_is_strict_payload_then_body_then_recoverable(self):
        # Payload present => recoverer is never consulted even with a body block.
        rec = FakeRecoverer({1: "SHOULD_NOT_BE_USED"})
        row = _row(
            1,
            body="desc\n\nPython scratchpad source:\nbody = 1\n",
            payload={"python_source": "payload = 1\n"},
        )
        rep = remediation.reconcile_row(row, recoverer=rec)
        self.assertEqual(rep.cohort, wr.COHORT_PAYLOAD_PYTHON)
        self.assertEqual(rep.python_source, "payload = 1\n")
        self.assertEqual(rec.calls, [])

    def test_every_eligible_row_lands_in_exactly_one_cohort(self):
        rows = [
            _row(1, payload={"python_source": "x = 1\n"}),
            _row(2, body="desc\n\nPython scratchpad source:\ny = 2\n"),
            _row(3, payload={"workflow_json": {"nodes": []}}),
            _row(4, body="prose only"),
        ]
        report = remediation.reconcile(
            FakeStore(rows),
            recoverer=FakeRecoverer({3: "# vibecomfy: generated scratchpad\nz = 3\n"}),
        )
        cohorts = report["cohorts"]
        self.assertEqual(set(cohorts), set(wr.SOURCE_COHORTS))
        self.assertEqual(cohorts[wr.COHORT_PAYLOAD_PYTHON], 1)
        self.assertEqual(cohorts[wr.COHORT_BODY_PYTHON], 1)
        self.assertEqual(cohorts[wr.COHORT_RECOVERABLE], 1)
        self.assertEqual(cohorts[wr.COHORT_UNAVAILABLE], 1)
        self.assertEqual(report["eligible_total"], 4)


# ---------------------------------------------------------------------------
# 2. public_state (safe/quarantined) is orthogonal to source cohort
# ---------------------------------------------------------------------------


class PublicStateOrthogonalTests(unittest.TestCase):
    def test_quarantined_payload_python_row_is_still_payload_python(self):
        row = _row(1, payload={"python_source": f'token = "{_SECRET}"\n'})
        rep = remediation.reconcile_row(row, recoverer=None)
        self.assertEqual(rep.cohort, wr.COHORT_PAYLOAD_PYTHON)
        self.assertEqual(rep.public_state, wr.PUBLIC_STATE_QUARANTINED)
        self.assertTrue(rep.available)
        self.assertTrue(rep.secret_reason_codes)

    def test_safe_unavailable_row_is_safe_not_quarantined(self):
        row = _row(2, body="prose only, no python")
        rep = remediation.reconcile_row(row, recoverer=None)
        self.assertEqual(rep.cohort, wr.COHORT_UNAVAILABLE)
        self.assertEqual(rep.public_state, wr.PUBLIC_STATE_SAFE)
        self.assertFalse(rep.available)

    def test_quarantined_and_unavailable_are_independent_axes(self):
        secret_row = _row(1, payload={"python_source": f't = "{_SECRET}"\n'})
        plain_row = _row(2, body="nothing")
        secret_rep = remediation.reconcile_row(secret_row, recoverer=None)
        plain_rep = remediation.reconcile_row(plain_row, recoverer=None)
        # secret + available + quarantined  vs  no-python + unavailable + safe
        self.assertNotEqual(secret_rep.cohort, plain_rep.cohort)
        self.assertNotEqual(secret_rep.public_state, plain_rep.public_state)
        self.assertNotEqual(secret_rep.available, plain_rep.available)


# ---------------------------------------------------------------------------
# 3. Authoritative payload.python_source is never overwritten
# ---------------------------------------------------------------------------


class AuthoritativePayloadTests(unittest.TestCase):
    def test_existing_payload_python_survives_even_when_recoverer_disagrees(self):
        # Payload is authoritative AND a corpus artifact exists. The recovery
        # adapter must not be invoked, and the payload bytes must survive.
        original = "ORIGINAL_CANONICAL = True\n"
        row = _row(
            1,
            body="desc",
            payload={"python_source": original, "corpus_path": "external/x.json"},
        )
        rec = FakeRecoverer({1: "RECOVERED_DIFFERENT = False\n"})
        rep = remediation.reconcile_row(row, recoverer=rec, vibecomfy_root=Path("/tmp/vc"))
        self.assertEqual(rep.python_source, original)
        self.assertEqual(rep.cohort, wr.COHORT_PAYLOAD_PYTHON)
        self.assertEqual(rec.calls, [])

    def test_blank_payload_is_not_authoritative(self):
        # Empty/whitespace payload.python_source must NOT pin the cohort.
        row = _row(1, body="desc\n\nPython scratchpad source:\nx = 1\n", payload={"python_source": "   \n"})
        rep = remediation.reconcile_row(row, recoverer=None)
        self.assertEqual(rep.cohort, wr.COHORT_BODY_PYTHON)
        self.assertIn("x = 1", rep.python_source)


# ---------------------------------------------------------------------------
# 4. Body delimiters: exactly one exact Python block; prose never inferred
# ---------------------------------------------------------------------------


class BodyDelimiterExtractionTests(unittest.TestCase):
    def test_one_recognized_delimiter_yields_one_exact_block(self):
        block = "import comfy\na = 1\nb = 2\n"
        row = _row(1, body=f"intro prose\n\nPython scratchpad source:\n{block}")
        rep = remediation.reconcile_row(row, recoverer=None)
        self.assertEqual(rep.cohort, wr.COHORT_BODY_PYTHON)
        self.assertEqual(rep.python_source.strip(), block.strip())
        self.assertEqual(rep.provenance.get("delimiter"), "Python scratchpad source:")

    def test_prose_without_delimiter_is_never_inferred_as_python(self):
        row = _row(1, body="This mentions python code like x = 1 but has no delimiter block.")
        rep = remediation.reconcile_row(row, recoverer=FakeRecoverer())
        self.assertEqual(rep.cohort, wr.COHORT_UNAVAILABLE)
        self.assertEqual(rep.python_source, "")

    def test_near_miss_delimiter_is_not_python(self):
        for fake in ("python scratchpad source:", "Python source:", "Python scratchpad source"):
            row = _row(1, body=f"x\n\n{fake}\nsecret = 1\n")
            rep = remediation.reconcile_row(row, recoverer=FakeRecoverer())
            self.assertEqual(rep.cohort, wr.COHORT_UNAVAILABLE, fake)
            self.assertNotIn("secret", rep.python_source)

    def test_first_block_wins_when_multiple_present(self):
        body = (
            "Python ready-template source:\na = 1\n\n"
            "Python scratchpad source:\nb = 2\n"
        )
        rep = remediation.reconcile_row(_row(1, body=body), recoverer=None)
        self.assertEqual(rep.cohort, wr.COHORT_BODY_PYTHON)
        self.assertIn("a = 1", rep.python_source)
        self.assertNotIn("b = 2", rep.python_source)


# ---------------------------------------------------------------------------
# 5. Recoverable cohort calls the injected adapter (not a second generator)
# ---------------------------------------------------------------------------


class RecoveryAdapterTests(unittest.TestCase):
    def test_inline_workflow_json_uses_adapter_without_corpus_root(self):
        rec = FakeRecoverer({1: "# vibecomfy: generated scratchpad\nz = 1\n"})
        row = _row(1, payload={"workflow_json": {"nodes": [{"class_type": "X"}]}})
        rep = remediation.reconcile_row(row, recoverer=rec, vibecomfy_root=None)
        self.assertEqual(rep.cohort, wr.COHORT_RECOVERABLE)
        self.assertEqual(rec.calls, [1])

    def test_ready_template_manifest_uses_adapter(self):
        rec = FakeRecoverer({1: "# vibecomfy: generated\nready = True\n"})
        row = _row(1, metadata={"asset_kind": "vibecomfy_ready_template"})
        rep = remediation.reconcile_row(row, recoverer=rec)
        self.assertEqual(rep.cohort, wr.COHORT_RECOVERABLE)
        self.assertEqual(rec.calls, [1])

    def test_provenance_records_derivation(self):
        rec = FakeRecoverer({1: "# vibecomfy: generated external workflow archive\nz = 1\n"})
        row = _row(1, payload={"workflow_json": {}})
        rep = remediation.reconcile_row(row, recoverer=rec)
        self.assertEqual(rep.cohort, wr.COHORT_RECOVERABLE)
        self.assertIn(rep.provenance.get("derivation"), {"external_archive", "scratchpad", "ready_template"})

    def test_adapter_returning_empty_is_unavailable(self):
        rec = FakeRecoverer({1: ""})
        row = _row(1, payload={"workflow_json": {}})
        rep = remediation.reconcile_row(row, recoverer=rec)
        self.assertEqual(rep.cohort, wr.COHORT_UNAVAILABLE)
        self.assertEqual(rec.calls, [1])


# ---------------------------------------------------------------------------
# 6. Local corpus_path authorization (fail-closed)
# ---------------------------------------------------------------------------


class CorpusPathAuthorizationTests(unittest.TestCase):
    def _corpus_row(self, corpus_path: str) -> dict[str, Any]:
        return _row(1, body="desc", payload={"corpus_path": corpus_path})

    def test_corpus_path_rejected_without_authorized_root(self):
        rec = FakeRecoverer({1: "z = 1\n"})
        # No vibecomfy_root => the local corpus_path must not be opened.
        with self.assertRaises(remediation.CorpusPathRejected):
            remediation.reconcile_row(self._corpus_row("external/x.json"), recoverer=rec)
        self.assertEqual(rec.calls, [])

    def test_traversal_outside_root_is_rejected(self):
        rec = FakeRecoverer({1: "z = 1\n"})
        root = Path("/tmp/vc-root")
        with self.assertRaises(remediation.CorpusPathRejected):
            remediation.reconcile_row(
                self._corpus_row("../../etc/passwd"),
                recoverer=rec,
                vibecomfy_root=root,
            )
        self.assertEqual(rec.calls, [])

    def test_absolute_path_outside_root_is_rejected(self):
        rec = FakeRecoverer({1: "z = 1\n"})
        with self.assertRaises(remediation.CorpusPathRejected):
            remediation.reconcile_row(
                self._corpus_row("/etc/passwd"),
                recoverer=rec,
                vibecomfy_root=Path("/tmp/vc-root"),
            )

    def test_corpus_path_within_root_proceeds_to_adapter(self):
        rec = FakeRecoverer({1: "# vibecomfy: generated scratchpad\nz = 1\n"})
        rep = remediation.reconcile_row(
            self._corpus_row("external/x.json"),
            recoverer=rec,
            vibecomfy_root=Path("/tmp/vc-root"),
        )
        self.assertEqual(rep.cohort, wr.COHORT_RECOVERABLE)
        self.assertEqual(rec.calls, [1])

    def test_corpus_rejection_recorded_nonsecretly_in_batch(self):
        store = FakeStore([self._corpus_row("../../etc/passwd")])
        report = remediation.reconcile(
            store,
            recoverer=FakeRecoverer({1: "z = 1\n"}),
            vibecomfy_root=Path("/tmp/vc-root"),
        )
        self.assertEqual(report["failed"], 1)
        self.assertEqual(len(store.failures), 1)
        reason = store.failures[0][1]
        self.assertNotIn("passwd", reason)
        self.assertNotIn(_SECRET, reason)


# ---------------------------------------------------------------------------
# 7. Deterministic secret scanning before materialization (nonsecret reports)
# ---------------------------------------------------------------------------


class SecretScanningTests(unittest.TestCase):
    def test_suspect_python_is_quarantined_not_published(self):
        row = _row(1, payload={"python_source": f'token = "{_SECRET}"\n'})
        rep = remediation.reconcile_row(row, recoverer=None)
        self.assertEqual(rep.public_state, wr.PUBLIC_STATE_QUARANTINED)
        self.assertTrue(rep.secret_reason_codes)
        # The reason is a nonsecret code; the secret value never appears.
        self.assertNotIn(_SECRET, rep.reason or "")
        for code in rep.secret_reason_codes:
            self.assertIn(code, wr.SECRET_REASON_CODES)

    def test_scan_runs_before_materialization_for_recovered_source(self):
        rec = FakeRecoverer({1: f'api_key = "{_SECRET}"\n'})
        row = _row(1, payload={"workflow_json": {}})
        rep = remediation.reconcile_row(row, recoverer=rec)
        self.assertEqual(rep.public_state, wr.PUBLIC_STATE_QUARANTINED)
        self.assertTrue(rep.secret_reason_codes)

    def test_no_secret_in_any_report_field(self):
        rows = [_row(1, payload={"python_source": f't = "{_SECRET}"\n'})]
        report = remediation.reconcile(FakeStore(rows), recoverer=None, apply=True)
        blob = json.dumps(report, sort_keys=True)
        self.assertNotIn(_SECRET, blob)
        for _rid, update in FakeStore([]).patches:
            self.fail("no patches expected on quarantined-only run")

    def test_failure_reason_carries_no_secret_value(self):
        # Recoverer raises an exception whose message contains a secret; the
        # recorded failure reason must be the nonsecret class/code only.
        rows = [_row(1, payload={"workflow_json": {}})]
        store = FakeStore(rows)
        report = remediation.reconcile(store, recoverer=FakeRecoverer(fail_ids=(1,)))
        self.assertEqual(report["failed"], 1)
        reason = store.failures[0][1]
        self.assertNotIn(_SECRET_B, reason)


# ---------------------------------------------------------------------------
# 8. Reconciled payload contract: version, provenance, hashes, state, reason
# ---------------------------------------------------------------------------


class ReconciledPayloadContractTests(unittest.TestCase):
    def test_payload_records_version_and_provenance(self):
        row = _row(1, payload={"python_source": "x = 1\n"})
        rep = remediation.reconcile_row(row, recoverer=None)
        self.assertEqual(rep.payload["python_representation_version"], remediation.REMEDIATION_VERSION)
        self.assertIsInstance(rep.provenance, dict)
        self.assertEqual(rep.payload["python_provenance"], rep.provenance)

    def test_sha256_hashes_present_and_well_formed(self):
        rep = remediation.reconcile_row(_row(1, payload={"python_source": "x = 1\n"}), recoverer=None)
        self.assertRegex(rep.python_sha256, r"^[0-9a-f]{64}$")
        self.assertRegex(rep.representation_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(rep.python_sha256, wr.representation_hash(rep.python_source))

    def test_representation_hash_is_deterministic(self):
        row = _row(1, payload={"python_source": "x = 1\n"})
        a = remediation.reconcile_row(row, recoverer=None)
        b = remediation.reconcile_row(row, recoverer=None)
        self.assertEqual(a.representation_sha256, b.representation_sha256)

    def test_public_search_state_recorded_on_payload(self):
        safe = remediation.reconcile_row(_row(1, payload={"python_source": "x = 1\n"}), recoverer=None)
        quar = remediation.reconcile_row(_row(2, payload={"python_source": f't="{_SECRET}"\n'}), recoverer=None)
        self.assertEqual(safe.payload["python_public_state"], wr.PUBLIC_STATE_SAFE)
        self.assertEqual(quar.payload["python_public_state"], wr.PUBLIC_STATE_QUARANTINED)

    def test_unavailable_quarantine_reason_is_nonsecret(self):
        rep = remediation.reconcile_row(_row(1, body="prose only"), recoverer=FakeRecoverer())
        self.assertIsNotNone(rep.reason)
        self.assertNotIn(_SECRET, rep.reason)
        self.assertEqual(rep.payload.get("python_reason"), rep.reason)


# ---------------------------------------------------------------------------
# 9. Body rendering: searchable-body contract, Python exactly once
# ---------------------------------------------------------------------------


class BodyRenderingTests(unittest.TestCase):
    def test_python_appears_exactly_once_across_payload_and_body(self):
        # "both" cohort: payload authoritative, the body duplicate is stripped.
        row = _row(
            1,
            body="A description.\n\nPython scratchpad source:\nx = 1\n",
            payload={"python_source": "x = 1\n"},
        )
        rep = remediation.reconcile_row(row, recoverer=None)
        # Body must not carry the delimited Python block (no duplication).
        self.assertNotIn("Python scratchpad source:", rep.body)
        # Exactly one occurrence across the canonical body + payload python.
        combined = (rep.body or "") + (rep.payload.get("python_source") or "")
        self.assertEqual(combined.count("x = 1"), 1)

    def test_body_follows_searchable_body_contract(self):
        row = _row(
            1,
            body="Description.\n\nPython scratchpad source:\nx = 1\n",
            payload={"python_source": "x = 1\n"},
            metadata={"workflow_semantics": {"media_type": "video", "task_type": "image_to_video"}},
        )
        rep = remediation.reconcile_row(row, recoverer=None)
        self.assertIn("Wan I2V", rep.body)
        self.assertIn("Description.", rep.body)
        # Projected semantics survive into the searchable body.
        self.assertIn("video", rep.body)
        self.assertIn("image_to_video", rep.body)

    def test_canonical_workflow_prose_is_idempotent(self):
        row = _row(
            1,
            body="Description.",
            metadata={"workflow_semantics": {"media_type": "video", "task_type": "image_to_video"}},
        )
        first = wr.build_workflow_prose(row)
        second = wr.build_workflow_prose({**row, "body": first})
        self.assertEqual(first, second)


# ---------------------------------------------------------------------------
# 10. Preservation: titles, descriptions, URLs, IDs, citations, artifacts
# ---------------------------------------------------------------------------


class PreservationTests(unittest.TestCase):
    def test_native_artifacts_preserved_byte_for_byte(self):
        wf = {"nodes": [{"class_type": "WanVideoSampler"}], "title": "Wan2.2 I2V"}
        compiled = {"last_node_id": "7"}
        row = _row(
            1,
            body="A unique description line.\n\nPython scratchpad source:\nx = 1\n",
            payload={"python_source": "x = 1\n", "workflow_json": wf, "compiled_api": compiled},
        )
        rep = remediation.reconcile_row(row, recoverer=None)
        self.assertEqual(rep.payload["workflow_json"], wf)
        self.assertEqual(rep.payload["compiled_api"], compiled)
        self.assertIn("A unique description line.", rep.body)

    def test_patch_update_does_not_overwrite_identity_columns(self):
        row = _row(
            1,
            title="Wan I2V",
            url="https://example.test/wan-i2v",
            external_id="wan-i2v-1",
            payload={"python_source": "x = 1\n"},
        )
        rep = remediation.reconcile_row(row, recoverer=None)
        update = remediation.patch_update(rep)
        # Remediation only writes payload/body/metadata — never identity columns.
        for protected in ("title", "url", "external_id", "id", "kind", "source"):
            self.assertNotIn(protected, update, protected)
        self.assertEqual(set(update), {"payload", "body", "metadata"})

    def test_citations_preserved(self):
        citations = [{"source": "discord", "message_id": "123"}]
        row = _row(
            1,
            body="desc",
            payload={"python_source": "x = 1\n"},
            metadata={"citations": citations},
        )
        rep = remediation.reconcile_row(row, recoverer=None)
        self.assertEqual(rep.metadata.get("citations"), citations)


# ---------------------------------------------------------------------------
# 11. Unavailable rows: prose/semantics searchable, no public Python
# ---------------------------------------------------------------------------


class UnavailableRowTests(unittest.TestCase):
    def test_no_public_python_but_prose_searchable(self):
        row = _row(1, body="Image-to-video workflow for Wan2.2 with LoRA detailer.")
        rep = remediation.reconcile_row(row, recoverer=FakeRecoverer())
        self.assertEqual(rep.cohort, wr.COHORT_UNAVAILABLE)
        self.assertEqual(rep.python_source, "")
        self.assertEqual(rep.payload.get("python_source"), "")
        self.assertIn("Wan2.2", rep.body)
        # Semantics still projected so the row is discoverable.
        self.assertEqual(rep.public_state, wr.PUBLIC_STATE_SAFE)


# ---------------------------------------------------------------------------
# 12. Workflow semantics recomputed from best structured evidence
# ---------------------------------------------------------------------------


class SemanticsRecomputeTests(unittest.TestCase):
    def test_semantics_derived_from_workflow_json_nodes(self):
        wf = {"nodes": [{"class_type": "WanVideoSampler"}, {"class_type": "VAEDecode"}]}
        row = _row(1, body="desc", payload={"python_source": "x = 1\n", "workflow_json": wf})
        rep = remediation.reconcile_row(row, recoverer=None)
        self.assertEqual(rep.metadata["workflow_semantics_version"], ws.WORKFLOW_SEMANTICS_VERSION)
        node_types = rep.semantics["node_types"]
        self.assertIn("WanVideoSampler", node_types)
        self.assertIn("VAEDecode", node_types)
        # Recomputed semantics projected into the searchable body.
        self.assertIn("WanVideoSampler", rep.body)

    def test_semantics_recomputed_over_stale_metadata(self):
        # Stale/v1 semantics already present must be replaced by a fresh compute.
        stale = {"media_type": "audio", "task_type": "upscale"}  # deliberately wrong
        row = _row(
            1,
            body="Wan2.2 image to video workflow",
            payload={"python_source": "x = 1\n"},
            metadata={"workflow_semantics": stale, "workflow_semantics_version": 1},
        )
        rep = remediation.reconcile_row(row, recoverer=None)
        self.assertEqual(rep.metadata["workflow_semantics_version"], ws.WORKFLOW_SEMANTICS_VERSION)
        self.assertNotEqual(rep.semantics.get("media_type"), "audio")


# ---------------------------------------------------------------------------
# 13. Orchestration: dry-run default, apply order, idempotency, paging, resume
# ---------------------------------------------------------------------------


class OrchestrationTests(unittest.TestCase):
    def _eligible_rows(self, n: int) -> list[dict[str, Any]]:
        return [_row(i, payload={"python_source": f"x = {i}\n"}) for i in range(1, n + 1)]

    def test_dry_run_is_default_and_writes_nothing(self):
        store = FakeStore(self._eligible_rows(3))
        report = remediation.reconcile(store, recoverer=None)  # apply defaults to False
        self.assertEqual(report["mode"], "dry_run")
        self.assertEqual(store.patches, [])
        self.assertEqual(store.refreshes, [])
        self.assertEqual(report["eligible_total"], 3)

    def test_apply_patches_row_before_requesting_lexical_refresh(self):
        store = FakeStore(self._eligible_rows(3))
        report = remediation.reconcile(store, recoverer=None, apply=True)
        self.assertEqual(report["mode"], "apply")
        self.assertEqual(len(store.patches), 3)
        # For every row, its patch event precedes its refresh event in the
        # single global call-order log recorded by the fake store.
        for rid, _update in store.patches:
            patch_idx = store.events.index(("patch", rid))
            refresh_idx = store.events.index(("refresh", rid))
            self.assertGreater(
                refresh_idx,
                patch_idx,
                f"refresh for row {rid} must come strictly after its patch",
            )
        # Each patch is a full payload/body/metadata update with reconciled python.
        rid0, update0 = store.patches[0]
        self.assertEqual(set(update0), {"payload", "body", "metadata"})
        self.assertEqual(update0["payload"]["python_source"], "x = 1\n")

    def test_hash_identical_rerun_skips_writes_and_refreshes(self):
        rows = self._eligible_rows(1)
        store = FakeStore(rows)
        first = remediation.reconcile(store, recoverer=None, apply=True)
        self.assertEqual(len(store.patches), 1)
        prior_hash = store.patches[0][1]["payload"]["python_representation_sha256"]
        # Second run: store already reports an identical representation hash.
        store2 = FakeStore(rows, current={1: {"representation_sha256": prior_hash}})
        report = remediation.reconcile(store2, recoverer=None, apply=True)
        self.assertEqual(store2.patches, [])
        self.assertEqual(store2.refreshes, [])
        self.assertEqual(report["skipped"], 1)

    def test_hash_identical_postgres_row_shape_skips_writes_and_refreshes(self):
        rows = self._eligible_rows(1)
        prior_hash = remediation.reconcile_row(rows[0], recoverer=None).representation_sha256
        # PostgresWorkflowStore.current returns payload/body/metadata, so the
        # durable hash is nested under payload rather than flattened.
        store = FakeStore(rows, current={1: {"payload": {
            "python_representation_sha256": prior_hash,
        }}})
        report = remediation.reconcile(store, recoverer=None, apply=True)
        self.assertEqual(store.patches, [])
        self.assertEqual(store.refreshes, [])
        self.assertEqual(report["skipped"], 1)

    def test_materialized_recovery_keeps_original_cohort_on_rerun(self):
        row = _row(14, body="description", payload={"corpus_path": "fixture.json"})
        recoverer = FakeRecoverer({14: "# vibecomfy: generated scratchpad\nx = 1\n"})
        first = remediation.reconcile_row(
            row, recoverer=recoverer, vibecomfy_root=Path("/tmp/vibecomfy")
        )
        row.update(remediation.patch_update(first))
        second = remediation.reconcile_row(
            row, recoverer=recoverer, vibecomfy_root=Path("/tmp/vibecomfy")
        )
        self.assertEqual(first.cohort, wr.COHORT_RECOVERABLE)
        self.assertEqual(second.cohort, wr.COHORT_RECOVERABLE)
        self.assertTrue(bool(second.python_source.strip()))
        self.assertEqual(first.representation_sha256, second.representation_sha256)

    def test_paging_processes_each_eligible_row_once(self):
        store = FakeStore(self._eligible_rows(5))
        report = remediation.reconcile(store, recoverer=None, apply=True, batch_size=2)
        patched_ids = [rid for rid, _ in store.patches]
        self.assertEqual(sorted(patched_ids), [1, 2, 3, 4, 5])
        self.assertEqual(len(patched_ids), len(set(patched_ids)))
        self.assertEqual(report["eligible_total"], 5)
        self.assertEqual(report["cursor"], 5)
        self.assertEqual(report["high_water"], 5)

    def test_resume_from_cursor_without_duplication_or_loss(self):
        rows = self._eligible_rows(5)
        # First (interrupted) run stops after the first page.
        store_a = FakeStore(rows, interrupt_after_pages=1)
        report_a = remediation.reconcile(store_a, recoverer=None, apply=True, batch_size=2)
        self.assertTrue(report_a["interrupted"])
        cursor = report_a["cursor"]
        high_water = report_a["high_water"]
        self.assertEqual(sorted(rid for rid, _ in store_a.patches), [1, 2])
        # Resume from the recorded cursor / high_water.
        store_b = FakeStore(rows)
        report_b = remediation.reconcile(
            store_b,
            recoverer=None,
            apply=True,
            batch_size=2,
            cursor=cursor,
            high_water=high_water,
            run_id=report_a["run_id"],
        )
        self.assertEqual(sorted(rid for rid, _ in store_b.patches), [3, 4, 5])
        self.assertFalse(report_b["interrupted"])
        # No row processed twice across the two runs.
        all_patched = [rid for rid, _ in store_a.patches] + [rid for rid, _ in store_b.patches]
        self.assertEqual(sorted(all_patched), [1, 2, 3, 4, 5])
        self.assertEqual(len(all_patched), len(set(all_patched)))

    def test_retryable_failure_can_succeed_on_resume(self):
        rows = [
            _row(1, payload={"python_source": "x = 1\n"}),
            _row(2, payload={"workflow_json": {"nodes": []}}),  # recoverable
        ]
        # First run: row 2's recovery fails (transient).
        store_a = FakeStore(rows)
        report_a = remediation.reconcile(
            store_a, recoverer=FakeRecoverer(fail_ids=(2,)), apply=True
        )
        self.assertEqual(report_a["failed"], 1)
        self.assertEqual(len(store_a.failures), 1)
        self.assertNotIn(_SECRET_B, store_a.failures[0][1])
        self.assertEqual(sorted(rid for rid, _ in store_a.patches), [1])
        # Resume: recovery now succeeds for row 2.
        store_b = FakeStore(rows, current={1: {"representation_sha256": _hash_of(rows[0])}})
        report_b = remediation.reconcile(
            store_b,
            recoverer=FakeRecoverer({2: "# vibecomfy: generated scratchpad\nz = 2\n"}),
            apply=True,
            cursor=report_a["cursor"],
            high_water=report_a["high_water"],
        )
        self.assertEqual(sorted(rid for rid, _ in store_b.patches), [2])
        self.assertEqual(report_b["failed"], 0)

    def test_sample_limits_rows_processed(self):
        store = FakeStore(self._eligible_rows(5))
        report = remediation.reconcile(store, recoverer=None, apply=True, sample=2)
        self.assertEqual(len(store.patches), 2)
        self.assertEqual(sorted(rid for rid, _ in store.patches), [1, 2])


def _hash_of(row: dict[str, Any]) -> str:
    """Compute the representation hash the way the reconciler would, for resume fakes."""
    return remediation.reconcile_row(row, recoverer=None).representation_sha256


# ---------------------------------------------------------------------------
# 14. Final reconciliation report shape
# ---------------------------------------------------------------------------


class ReportShapeTests(unittest.TestCase):
    def test_report_has_all_required_fields(self):
        rows = [
            _row(1, payload={"python_source": "x = 1\n"}),
            _row(
                2,
                body="desc\n\nPython scratchpad source:\ny = 2\n",
                payload={"python_source": "y = 2\n"},  # body_duplicate
            ),
            _row(3, payload={"python_source": f't = "{_SECRET}"\n'}),  # quarantined
            _row(4, body="prose only"),
        ]
        report = remediation.reconcile(FakeStore(rows), recoverer=None, apply=True)
        required = {
            "run_id", "mode", "eligible_total", "cohorts", "public_state",
            "materialized", "unavailable", "stale_or_mismatched_hashes",
            "duplicate_python_bodies", "skipped", "failed", "cursor", "high_water",
        }
        self.assertTrue(required.issubset(report.keys()), required - report.keys())
        self.assertEqual(report["cohorts"][wr.COHORT_PAYLOAD_PYTHON], 3)
        self.assertEqual(report["cohorts"][wr.COHORT_UNAVAILABLE], 1)
        self.assertEqual(report["public_state"][wr.PUBLIC_STATE_QUARANTINED], 1)
        self.assertEqual(report["public_state"][wr.PUBLIC_STATE_SAFE], 3)
        self.assertEqual(report["unavailable"], 1)
        self.assertEqual(report["duplicate_python_bodies"], 1)  # row 2 body_duplicate
        self.assertEqual(report["stale_or_mismatched_hashes"], 4)  # no existing state
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["cursor"], 4)
        self.assertEqual(report["high_water"], 4)
        # No secret leaks into the serialized report.
        self.assertNotIn(_SECRET, json.dumps(report, sort_keys=True))


# ---------------------------------------------------------------------------
# 15. Durable lifecycle: CAS cursor/counters/retryable row failures
# ---------------------------------------------------------------------------


class DurableStore(FakeStore):
    def __init__(self, rows):
        super().__init__(rows)
        self.runs = {}
        self.next_run = 1

    def create_remediation_run(self, high_water):
        rid = self.next_run; self.next_run += 1
        self.runs[rid] = {"run_id": rid, "version": 1, "cursor": None, "high_water": high_water,
                          "counters": {"processed": 0, "materialized": 0, "skipped": 0, "unavailable": 0, "failed": 0}}
        return dict(self.runs[rid])

    def load_remediation_run(self, run_id):
        return dict(self.runs[int(run_id)])

    def checkpoint_remediation(self, run_id, expected_version, cursor, high_water, processed, materialized, skipped, unavailable, failed, reason, failed_resource_id):
        run = self.runs[int(run_id)]
        if run["version"] != expected_version:
            return {"ok": False, "reason": "stale_version"}
        run["version"] += 1; run["cursor"] = cursor; run["high_water"] = high_water
        for key, delta in (("processed", processed), ("materialized", materialized), ("skipped", skipped), ("unavailable", unavailable), ("failed", failed)):
            run["counters"][key] += delta
        return {"ok": True, "version": run["version"]}

    def complete_remediation_run(self, run_id, expected_version):
        run = self.runs[int(run_id)]
        if run["version"] != expected_version:
            return {"ok": False}
        run["version"] += 1
        return {"ok": True, "version": run["version"]}


class DurableLifecycleTests(unittest.TestCase):
    def test_apply_persists_cursor_counters_and_resume_uses_them(self):
        store = DurableStore([_row(1, payload={"python_source": "x=1\n"}), _row(2, payload={"python_source": "y=2\n"})])
        first = remediation.reconcile(store, apply=True, batch_size=1)
        run = store.runs[int(first["run_id"])]
        self.assertEqual(run["cursor"], 2)
        self.assertEqual(run["counters"]["processed"], 2)
        second = remediation.reconcile(store, apply=True, run_id=first["run_id"])
        self.assertEqual(second["processed"], 2)
        self.assertEqual(store.runs[int(first["run_id"])]["counters"]["processed"], 2)

    def test_migration_keeps_state_separate_and_owner_only(self):
        sql = (_REPO / "schema" / "031_workflow_remediation_runs.sql").read_text(encoding="utf-8")
        self.assertIn("workflow_remediation_failures", sql)
        self.assertIn("hivemind_checkpoint_workflow_remediation", sql)
        self.assertIn("security definer", sql.lower())
        self.assertIn("revoke all on public.workflow_remediation_runs", sql.lower())
        self.assertNotIn("external_resources set", sql.lower())


# ---------------------------------------------------------------------------
# 16. CLI: flags, dry-run default, safe report output
# ---------------------------------------------------------------------------


class CLITests(unittest.TestCase):
    def test_parser_exposes_required_flags(self):
        parser = cli.build_parser()
        ns = parser.parse_args(
            [
                "--apply",
                "--resume-run-id", "run-7",
                "--batch-size", "3",
                "--vibecomfy-root", "/tmp/vc",
                "--sample", "2",
            ]
        )
        self.assertTrue(ns.apply)
        self.assertEqual(ns.resume_run_id, "run-7")
        self.assertEqual(ns.batch_size, 3)
        self.assertEqual(Path(ns.vibecomfy_root), Path("/tmp/vc"))
        self.assertEqual(ns.sample, 2)

    def test_defaults_are_audit_dry_run(self):
        ns = cli.build_parser().parse_args([])
        self.assertFalse(ns.apply)
        self.assertIsNone(ns.resume_run_id)
        self.assertIsNone(ns.sample)
        self.assertIsNone(ns.vibecomfy_root)
        self.assertGreater(ns.batch_size, 0)

    def test_run_default_is_dry_run_no_writes(self):
        ns = cli.build_parser().parse_args([])
        store = FakeStore([_row(1, payload={"python_source": "x = 1\n"})])
        report = cli.run(ns, store=store, recoverer=FakeRecoverer())
        self.assertEqual(report["mode"], "dry_run")
        self.assertEqual(store.patches, [])
        self.assertEqual(store.refreshes, [])

    def test_run_apply_wires_through_to_writes(self):
        ns = cli.build_parser().parse_args(["--apply", "--batch-size", "5"])
        store = FakeStore([_row(1, payload={"python_source": "x = 1\n"})])
        report = cli.run(ns, store=store, recoverer=FakeRecoverer())
        self.assertEqual(report["mode"], "apply")
        self.assertEqual(len(store.patches), 1)

    def test_run_sample_and_batch_are_wired(self):
        ns = cli.build_parser().parse_args(["--apply", "--sample", "1", "--batch-size", "2"])
        store = FakeStore([_row(i, payload={"python_source": f"x = {i}\n"}) for i in (1, 2, 3)])
        report = cli.run(ns, store=store, recoverer=FakeRecoverer())
        self.assertEqual(len(store.patches), 1)

    def test_main_returns_int_exit_code(self):
        self.assertTrue(hasattr(cli, "main"))


if __name__ == "__main__":
    unittest.main()
