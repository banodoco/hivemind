"""Tests for the schema/012 search-MV refresh lifecycle + coverage dimension.

Three layers:

1. PURE LOGIC (no DB, no I/O):
   - ``candidate_fn_uses_search_mv``: the schema/012 read-path detector (body
     references the MV) — the predicate that disambiguates an absent MV.
   - ``evaluate_mv_coverage_ok``: fresh / stale / unreadable + the FOUR absent
     cases: (pre-012 + absent => compatible pass) vs (schema-012 + absent =>
     hard failure); plus present stale/unreadable/fresh and string-counter
     normalization.
   - ``refresh_search_mv`` decision logic: absent-MV pre-012 (compatible ok) vs
     absent-MV schema-012 (fail closed); CONCURRENTLY success; CONCURRENTLY-
     failure -> deterministic plain fallback; both-fail (monkeypatch the I/O
     hooks so no DB is touched).

2. SHAPE: the MV counter names in ``COVERAGE_COUNTER_NAMES`` appear as literals
   in ``verify_coverage`` (the ``test_coverage_sql_contains_counters`` pairing),
   ``evaluate_mv_coverage_ok`` consumes them + the read-path flag, and
   ``refresh_search_mv`` branches on ``search_mv_read_path_active``.

3. PG-GATED (``@skipUnless``): on an isolated throwaway cluster —
   - the MV lifecycle (initial population, refresh-after-change, stale
     detection, idempotent rerun, CONCURRENTLY support); and
   - the read-path TRUST distinction: 010/011 only => absent MV is COMPATIBLE;
     012 applied + MV dropped => coverage AND refresh FAIL CLOSED; reapply 012
     => fresh state PASSES.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

_SPEC = importlib.util.spec_from_file_location(
    "live_lexical_refresh", REPO / "scripts" / "live_lexical_refresh.py"
)
assert _SPEC is not None and _SPEC.loader is not None
LLR = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(LLR)

evaluate_mv_coverage_ok = LLR.evaluate_mv_coverage_ok
refresh_search_mv = LLR.refresh_search_mv
candidate_fn_uses_search_mv = LLR.candidate_fn_uses_search_mv
COVERAGE_COUNTER_NAMES = LLR.COVERAGE_COUNTER_NAMES


class _DummyCred:
    """Stand-in for live_lexical_refresh.Cred (no env / network)."""


# ===========================================================================
# Layer 1 — PURE LOGIC
# ===========================================================================
class TestCandidateFnUsesSearchMv(unittest.TestCase):
    def test_references_mv(self) -> None:
        self.assertTrue(candidate_fn_uses_search_mv(
            "select ... from public.lexical_workflow_python_search mv where ..."))

    def test_pre012_body_does_not_reference_mv(self) -> None:
        # schema/008 + schema/010 bodies scan lexical_documents, never the MV.
        self.assertFalse(candidate_fn_uses_search_mv(
            "from public.lexical_documents where representation_type='workflow_python'"))

    def test_empty_and_none(self) -> None:
        self.assertFalse(candidate_fn_uses_search_mv(""))
        self.assertFalse(candidate_fn_uses_search_mv(None))


class TestEvaluateMvCoverageOk(unittest.TestCase):
    def test_fresh_mv_ok(self) -> None:
        ok, reasons = evaluate_mv_coverage_ok(
            {"mv_present": True, "mv_distinct_items": 5, "mv_expected_distinct_items": 5})
        self.assertTrue(ok, reasons)
        self.assertEqual(reasons, [])

    def test_stale_mv_fails(self) -> None:
        ok, reasons = evaluate_mv_coverage_ok(
            {"mv_present": True, "mv_distinct_items": 4, "mv_expected_distinct_items": 5})
        self.assertFalse(ok)
        self.assertTrue(any("mv stale" in r for r in reasons), reasons)

    def test_absent_pre012_read_path_is_compatible(self) -> None:
        # Pre-012 read path active: the MV is not on the live path, so an absent
        # MV is a detected+warned compatible success — NOT a failure.
        ok, reasons = evaluate_mv_coverage_ok(
            {"mv_present": False, "mv_read_path_active": False})
        self.assertTrue(ok)
        self.assertEqual(reasons, [])

    def test_absent_default_read_path_is_compatible(self) -> None:
        # Callers that omit the flag get the historical pre-012 reading.
        ok, reasons = evaluate_mv_coverage_ok({"mv_present": False})
        self.assertTrue(ok)
        self.assertEqual(reasons, [])

    def test_absent_schema012_read_path_fails_closed(self) -> None:
        # The dangerous case: schema/012 is the active read path but its MV is
        # missing. The verifier must FAIL CLOSED, not report success.
        ok, reasons = evaluate_mv_coverage_ok(
            {"mv_present": False, "mv_read_path_active": True})
        self.assertFalse(ok)
        self.assertTrue(reasons, "expected a clear failure reason")

    def test_present_but_unreadable_fails(self) -> None:
        ok, reasons = evaluate_mv_coverage_ok(
            {"mv_present": True, "mv_distinct_items": None, "mv_expected_distinct_items": None})
        self.assertFalse(ok)
        self.assertTrue(any("unreadable" in r for r in reasons), reasons)

    def test_string_counters_normalized(self) -> None:
        ok, reasons = evaluate_mv_coverage_ok(
            {"mv_present": True, "mv_distinct_items": "221", "mv_expected_distinct_items": "221"})
        self.assertTrue(ok, reasons)


class TestRefreshSearchMvLogic(unittest.TestCase):
    """refresh_search_mv decision logic, with the I/O hooks stubbed."""

    def _patch(self, name, value):
        orig = getattr(LLR, name)
        setattr(LLR, name, value)
        self.addCleanup(setattr, LLR, name, orig)

    def test_absent_pre012_read_path_is_compatible_ok(self) -> None:
        self._patch("_mv_present", lambda cred: False)
        self._patch("search_mv_read_path_active", lambda cred: False)
        res = refresh_search_mv(_DummyCred())
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["absent"])

    def test_absent_schema012_read_path_fails_closed(self) -> None:
        # MV absent + schema/012 read path active => the refresh must NOT report
        # success; the fragment arm depends on the missing MV.
        self._patch("_mv_present", lambda cred: False)
        self._patch("search_mv_read_path_active", lambda cred: True)
        res = refresh_search_mv(_DummyCred())
        self.assertFalse(res["ok"], res)
        self.assertTrue(res["absent"])

    def test_concurrently_success(self) -> None:
        self._patch("_mv_present", lambda cred: True)
        calls: list[str] = []
        self._patch("_run_sql_file", lambda cred, sql, timeout: calls.append(sql) or (0, ""))
        res = refresh_search_mv(_DummyCred())
        self.assertTrue(res["ok"])
        self.assertTrue(res["concurrently"])
        self.assertFalse(res["absent"])
        self.assertIn("concurrently", calls[0])

    def test_concurrently_failure_falls_back_to_plain(self) -> None:
        self._patch("_mv_present", lambda cred: True)
        seq = [(1, "concurrent boom"), (0, "")]  # concurrent fails -> plain ok
        self._patch("_run_sql_file", lambda cred, sql, timeout: seq.pop(0))
        res = refresh_search_mv(_DummyCred())
        self.assertTrue(res["ok"])
        self.assertFalse(res["concurrently"])

    def test_both_fail_reports_not_ok(self) -> None:
        self._patch("_mv_present", lambda cred: True)
        self._patch("_run_sql_file", lambda cred, sql, timeout: (1, "nope"))
        res = refresh_search_mv(_DummyCred())
        self.assertFalse(res["ok"])
        self.assertFalse(res["concurrently"])


# ===========================================================================
# Layer 2 — SHAPE
# ===========================================================================
class TestMvCoverageShape(unittest.TestCase):
    def test_counters_in_verify_source(self) -> None:
        src = inspect.getsource(LLR.verify_coverage)
        for name in COVERAGE_COUNTER_NAMES:
            self.assertIn(name, src, f"verify_coverage missing counter literal '{name}'")

    def test_evaluate_consumes_mv_counters(self) -> None:
        src = inspect.getsource(evaluate_mv_coverage_ok)
        for name in ("mv_present", "mv_distinct_items", "mv_expected_distinct_items",
                     "mv_read_path_active"):
            self.assertIn(name, src, f"evaluate_mv_coverage_ok does not read '{name}'")

    def test_refresh_branches_on_read_path(self) -> None:
        src = inspect.getsource(refresh_search_mv)
        # The absent-MV branch must consult the read-path detector (so an absent
        # MV under schema/012 fails closed instead of succeeding).
        self.assertIn("search_mv_read_path_active", src)
        self.assertIn("refresh materialized view concurrently", src)
        self.assertIn("refresh materialized view public.lexical_workflow_python_search", src)


# ===========================================================================
# Layer 3 — PG-GATED (isolated throwaway PostgreSQL)
# ===========================================================================
try:  # pragma: no cover - import guard
    from lexical_pg import find_pgbins  # type: ignore
    _HAS_PG = find_pgbins() is not None
except Exception:  # noqa: BLE001
    _HAS_PG = False


class _ClusterCred:
    """Adapt a LocalCluster to the ``Cred`` surface the refresh helpers expect, so
    the REAL db-touching helpers (``_mv_present``, ``search_mv_read_path_active``,
    ``_mv_coverage``, ``refresh_search_mv``) run against the throwaway cluster
    instead of stubs. Bare ``psql`` (used by ``psql()``/``_run_sql_file``) is
    resolved from the cluster's own ``bindir`` so the matching server version is
    used; auth is ``trust``, so ``cred.refresh()`` is never hit. No production
    endpoint is contacted."""

    def __init__(self, cluster) -> None:
        env = cluster._env()
        env["PATH"] = str(cluster.bindir) + os.pathsep + env.get("PATH", "")
        self.env = env
        self.host = env.get("PGHOST", "")
        self.port = env.get("PGPORT", "")

    def refresh(self) -> None:  # only called on auth failure (never under trust)
        return None


@unittest.skipUnless(_HAS_PG, "PostgreSQL binaries (initdb/pg_ctl/psql) not found")
class TestSearchMvLifecycleCluster(unittest.TestCase):  # pragma: no cover - PG-gated
    @classmethod
    def setUpClass(cls) -> None:
        import rehearse_lexical_candidate as R  # type: ignore
        cls.R = R
        cls.cluster = R.LP.LocalCluster.start()
        try:
            R.reset_schema(cls.cluster)
            R.bootstrap(cls.cluster)  # 001..009
            for name in ("010_lexical_latency_fix.sql",
                         "011_lexical_security_hardening.sql",
                         "012_lexical_latency_phase2.sql"):
                cls.cluster.psql_file(R.SCHEMA_DIR / name)
            # Seed workflow_python docs (resources 20/64 + fillers). 012's apply
            # REFRESHed the MV before these docs existed, so refresh once more.
            R.seed(cls.cluster, n_messages=500)
            cls.cluster.psql("refresh materialized view public.lexical_workflow_python_search;")
        except Exception:
            cls.cluster.tear_down()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.cluster.tear_down()

    # ---- helpers ----
    def _expected(self) -> int:
        rc, out = self.cluster.psql(
            "select count(distinct item_id)::text from public.lexical_documents "
            "where representation_type='workflow_python' and quarantine_state='safe' "
            "and char_length(chunk_text) between 1 and 8000;")
        return int((out or "0").strip() or 0)

    def _mv_count(self) -> int:
        rc, out = self.cluster.psql(
            "select count(*)::text from public.lexical_workflow_python_search;")
        return int((out or "0").strip() or 0)

    def _add_safe_wf_item(self, rid: int, code: str) -> None:
        self.cluster.psql(
            f"insert into public.external_resources (id, kind, source, external_id, title, body) "
            f"overriding system value values ({rid},'workflow','t','x{rid}','New {rid}','d') "
            "on conflict (id) do nothing;", capture=False)
        self.cluster.psql(
            "insert into public.lexical_resource_python_state "
            "(resource_id, kind, cohort, public_state, available) values "
            f"({rid},'workflow','payload_python','safe',true) on conflict (resource_id) "
            "do update set public_state='safe', available=true;", capture=False)
        self.cluster.psql(
            "insert into public.lexical_documents (entity_type, item_id, representation_type, "
            "chunk_index, chunk_text, matched_anchor, representation_hash, chunk_hash, "
            f"quarantine_state) values ('resource','{rid}','workflow_python',0,'{code}',"
            f"'anchor','h{rid}','c{rid}','safe') on conflict do nothing;", capture=False)

    # ---- tests ----
    def test_initial_population_matches_documents(self) -> None:
        self.assertEqual(self._mv_count(), self._expected())
        self.assertGreater(self._mv_count(), 0)

    def test_refresh_concurrently_picks_up_new_document(self) -> None:
        before = self._mv_count()
        self._add_safe_wf_item(55_501, "class BrandNewSamplerNode: pass")
        # Stale before refresh: MV unchanged, expected grew.
        self.assertEqual(self._mv_count(), before)
        self.assertLess(self._mv_count(), self._expected())
        rc, out = self.cluster.psql(
            "refresh materialized view concurrently public.lexical_workflow_python_search;")
        self.assertEqual(rc, 0, out)
        # Now fresh.
        self.assertEqual(self._mv_count(), self._expected())

    def test_stale_detection_when_not_refreshed(self) -> None:
        self._add_safe_wf_item(55_502, "class AnotherNewNode: pass")
        # The SAME SQL _mv_coverage runs:
        rc, out = self.cluster.psql(
            "select (select count(*) from public.lexical_workflow_python_search)::text "
            "|| '|' || (select count(distinct item_id) from public.lexical_documents "
            "where representation_type='workflow_python' and quarantine_state='safe' "
            "and char_length(chunk_text) between 1 and 8000)::text;")
        self.assertEqual(rc, 0, out)
        di, exp = (out.strip().split("|") + ["", ""])[:2]
        self.assertNotEqual(int(di), int(exp))
        cov = {"mv_present": True, "mv_distinct_items": di,
               "mv_expected_distinct_items": exp}
        ok, reasons = evaluate_mv_coverage_ok(cov)
        self.assertFalse(ok)
        self.assertTrue(any("mv stale" in r for r in reasons), reasons)
        # Leave the cluster fresh for siblings.
        self.cluster.psql("refresh materialized view public.lexical_workflow_python_search;")

    def test_idempotent_rerun(self) -> None:
        c0 = self._mv_count()
        self.cluster.psql("refresh materialized view public.lexical_workflow_python_search;")
        self.cluster.psql("refresh materialized view public.lexical_workflow_python_search;")
        self.assertEqual(self._mv_count(), c0)
        self.assertEqual(self._mv_count(), self._expected())

    def test_concurrently_supported_by_unique_index(self) -> None:
        rc, out = self.cluster.psql(
            "select indexname from pg_indexes where tablename='lexical_workflow_python_search';")
        self.assertIn("lexical_workflow_python_search_item_uidx", out)
        rc, out = self.cluster.psql(
            "refresh materialized view concurrently public.lexical_workflow_python_search;")
        self.assertEqual(rc, 0, out)


@unittest.skipUnless(_HAS_PG, "PostgreSQL binaries (initdb/pg_ctl/psql) not found")
class TestSearchMvReadPathCluster(unittest.TestCase):  # pragma: no cover - PG-gated
    """The trust distinction: an absent MV is compatible ONLY under the pre-012
    read path. Under schema/012 (function body references the MV) an absent MV is
    a HARD failure for BOTH coverage and refresh. Drives the real helpers against
    an isolated cluster via ``_ClusterCred``."""

    @classmethod
    def setUpClass(cls) -> None:
        import rehearse_lexical_candidate as R  # type: ignore
        cls.R = R
        cls.cluster = R.LP.LocalCluster.start()
        cls.cred = _ClusterCred(cls.cluster)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.cluster.tear_down()

    # ---- helpers ----
    def _bootstrap_to(self, *migrations: str) -> None:
        """Clean slate -> 001..009, then the named migrations applied in order."""
        self.R.reset_schema(self.cluster)
        self.R.bootstrap(self.cluster)  # 001 + 003..009 (incl. schema/008 fn)
        for name in migrations:
            self.cluster.psql_file(self.R.SCHEMA_DIR / name)

    def _add_safe_wf_item(self, rid: int, code: str = "class WanVideoSampler: pass") -> None:
        self.cluster.psql(
            f"insert into public.external_resources (id, kind, source, external_id, title, body) "
            f"overriding system value values ({rid},'workflow','t','x{rid}','T {rid}','d') "
            "on conflict (id) do nothing;", capture=False)
        self.cluster.psql(
            "insert into public.lexical_resource_python_state "
            "(resource_id, kind, cohort, public_state, available) values "
            f"({rid},'workflow','payload_python','safe',true) on conflict (resource_id) "
            "do update set public_state='safe', available=true;", capture=False)
        self.cluster.psql(
            "insert into public.lexical_documents (entity_type, item_id, representation_type, "
            "chunk_index, chunk_text, matched_anchor, representation_hash, chunk_hash, "
            f"quarantine_state) values ('resource','{rid}','workflow_python',0,'{code}',"
            f"'anchor','h{rid}','c{rid}','safe') on conflict do nothing;", capture=False)

    def _mv_cov(self) -> dict:
        """Build the MV-coverage cov dict from the REAL helpers (DB-backed)."""
        mv = LLR._mv_coverage(self.cred)
        return {"mv_present": mv.get("present"),
                "mv_distinct_items": mv.get("distinct_items"),
                "mv_expected_distinct_items": mv.get("expected_distinct_items"),
                "mv_read_path_active": LLR.search_mv_read_path_active(self.cred)}

    # ---- Phase 1: pre-012 (010/011 applied, 012 NOT) — MV absent is COMPATIBLE ----
    def test_pre012_absent_mv_is_compatible(self) -> None:
        self._bootstrap_to("010_lexical_latency_fix.sql",
                           "011_lexical_security_hardening.sql")
        self._add_safe_wf_item(55_601)
        # The 008/010 candidate fn body does NOT reference the MV.
        self.assertFalse(LLR._mv_present(self.cred))
        self.assertFalse(LLR.search_mv_read_path_active(self.cred))
        # Coverage: absent + pre-012 read path => compatible pass.
        ok, reasons = LLR.evaluate_mv_coverage_ok(self._mv_cov())
        self.assertTrue(ok, reasons)
        # Refresh: absent + pre-012 read path => compatible no-op success.
        res = LLR.refresh_search_mv(self.cred)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["absent"])

    # ---- Phase 2: 012 applied, MV dropped => FAIL CLOSED (coverage + refresh) ----
    def test_012_active_mv_dropped_fails_closed(self) -> None:
        self._bootstrap_to("010_lexical_latency_fix.sql",
                           "011_lexical_security_hardening.sql",
                           "012_lexical_latency_phase2.sql")
        self._add_safe_wf_item(55_602)
        self.cluster.psql("refresh materialized view public.lexical_workflow_python_search;")
        # Sanity: schema/012 read path is live, MV present + fresh.
        self.assertTrue(LLR.search_mv_read_path_active(self.cred))
        self.assertTrue(LLR._mv_present(self.cred))
        ok, _ = LLR.evaluate_mv_coverage_ok(self._mv_cov())
        self.assertTrue(ok)
        # Simulate the dangerous breakage: drop the MV while the schema/012 fn
        # (which reads it) is still installed.
        self.cluster.psql("drop materialized view public.lexical_workflow_python_search;")
        self.assertFalse(LLR._mv_present(self.cred))
        # The fn body STILL references the MV -> read path active, MV missing.
        self.assertTrue(LLR.search_mv_read_path_active(self.cred))
        # Coverage must FAIL CLOSED (not silently pass).
        ok, reasons = LLR.evaluate_mv_coverage_ok(self._mv_cov())
        self.assertFalse(ok)
        self.assertTrue(reasons, "expected a clear failure reason")
        # Refresh must FAIL CLOSED (NOT report success).
        res = LLR.refresh_search_mv(self.cred)
        self.assertFalse(res["ok"], res)
        self.assertTrue(res["absent"])

    # ---- Phase 3: reapply 012 => MV recreated, fresh state PASSES ----
    def test_reapply_012_restores_fresh_pass(self) -> None:
        self._bootstrap_to("010_lexical_latency_fix.sql",
                           "011_lexical_security_hardening.sql",
                           "012_lexical_latency_phase2.sql")
        self._add_safe_wf_item(55_603)
        # Break, then recover by re-applying schema/012 (idempotent: recreates
        # the MV + its indexes + REFRESH, leaves the fn in place).
        self.cluster.psql("drop materialized view public.lexical_workflow_python_search;")
        self.assertFalse(LLR._mv_present(self.cred))
        self.cluster.psql_file(self.R.SCHEMA_DIR / "012_lexical_latency_phase2.sql")
        # Restored: MV present, read path active, fresh => pass.
        self.assertTrue(LLR._mv_present(self.cred))
        self.assertTrue(LLR.search_mv_read_path_active(self.cred))
        ok, reasons = LLR.evaluate_mv_coverage_ok(self._mv_cov())
        self.assertTrue(ok, reasons)
        res = LLR.refresh_search_mv(self.cred)
        self.assertTrue(res["ok"], res)
        self.assertFalse(res["absent"])


if __name__ == "__main__":
    unittest.main()
