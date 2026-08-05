"""Tests for the lexical security-hardening migration (schema/011, task 1.12).

Two layers:

1. PURE / SHAPE tests (no DB): assert ``schema/011`` contains the three
   REVOKE statements (candidate fn + both tables) targeting public/anon/
   authenticated, does NOT revoke from service_role, and does NOT touch
   ``hivemind_lexical_search``'s existing service_role grant.

2. CLUSTER-gated tests (skip unless PostgreSQL binaries are present): apply
   migrations 001..010, then 011, and prove on an isolated throwaway cluster
   that anon/authenticated are blocked on both functions and both tables
   (read + write), while service_role can still call the hardened RPC and
   eligibility (rejected distillation / soft-deleted message / quarantined
   workflow_python) still excludes the ineligible rows from RPC results.

The cluster is a throwaway ``initdb --auth=trust`` instance on an ephemeral
port; torn down in ``tearDownClass``. No Docker, no network, no production
mutation. Mirrors the seeding/role pattern of ``test_lexical_candidate_sql``.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import rehearse_lexical_candidate as R  # noqa: E402
from lexical_pg import find_pgbins  # noqa: E402

SCHEMA_011 = REPO / "schema" / "011_lexical_security_hardening.sql"
MIGRATIONS_011 = list(R.MIGRATIONS) + ["010_lexical_latency_fix.sql", "011_lexical_security_hardening.sql"]

# Supabase default that the bare initdb cluster does NOT replicate: anon,
# authenticated, and service_role all have USAGE on schema public so they can
# resolve `public.<object>`. The per-object privilege is what 011 narrows;
# schema USAGE is the ambient default restored here so the privilege checks
# are what actually decide access (not an unrelated schema-resolution failure).
GRANT_SUPABASE_DEFAULTS = (
    "grant usage on schema public to anon, authenticated, service_role;"
)


# ===========================================================================
# Layer 1 — PURE / SHAPE (no DB)
# ===========================================================================
class TestMigrationShape(unittest.TestCase):
    """Static assertions over schema/011.sql text. No cluster required."""

    def setUp(self) -> None:
        self.assertTrue(SCHEMA_011.exists(), f"missing migration: {SCHEMA_011}")
        self.sql = SCHEMA_011.read_text()

    def _revoke_statements(self) -> list[str]:
        """Return each REVOKE statement as one normalized (whitespace-collapsed)
        lowercase string, so multi-line REVOKEs are inspected as a unit.

        Robust to SQL line comments (``-- ...``) and statement-spanning
        REVOKEs: walk the file, strip ``--`` comments, accumulate text until
        a terminating ``;``, and keep only statements whose first token is
        ``revoke``.
        """
        stmts: list[str] = []
        buf: list[str] = []
        for raw in self.sql.splitlines():
            # Strip line comments (none of our statements embed '--' in a
            # string literal, so a naive split is safe here).
            code = raw.split("--", 1)[0]
            buf.append(code)
            if ";" in code:
                full = " ".join(" ".join(buf).split()).lower().strip()
                # full may contain multiple statements if several share a line;
                # take the leading statement(s) starting with 'revoke'.
                for piece in full.split(";"):
                    piece = piece.strip()
                    if piece.startswith("revoke"):
                        stmts.append(piece)
                buf = []
        return stmts

    def _statements_starting_with(self, *prefixes: str) -> list[str]:
        """Generalized form of _revoke_statements: return each SQL statement
        (whitespace-collapsed, lowercase) whose first token is one of
        *prefixes*. Used to scan both REVOKE and GRANT statements without
        matching comment prose that merely mentions the words."""
        stmts: list[str] = []
        buf: list[str] = []
        for raw in self.sql.splitlines():
            code = raw.split("--", 1)[0]
            buf.append(code)
            if ";" in code:
                full = " ".join(" ".join(buf).split()).lower().strip()
                for piece in full.split(";"):
                    piece = piece.strip()
                    if piece.startswith(prefixes):
                        stmts.append(piece)
                buf = []
        return stmts

    def _has_revoke(self, object_sql_fragment: str, roles: list[str]) -> None:
        stmts = self._revoke_statements()
        self.assertTrue(stmts, "migration must contain REVOKE statements")
        matches = [s for s in stmts if object_sql_fragment in s]
        self.assertEqual(
            len(matches), 1,
            f"expected exactly one REVOKE referencing {object_sql_fragment!r}, "
            f"got {len(matches)}: {matches}",
        )
        stmt = matches[0]
        for role in roles:
            self.assertIn(role, stmt,
                          f"REVOKE on {object_sql_fragment!r} must target role {role!r}: {stmt}")

    def test_revoke_candidate_function(self) -> None:
        self._has_revoke(
            "hivemind_lexical_candidates", ["public", "anon", "authenticated"]
        )

    def test_revoke_lexical_documents_table(self) -> None:
        self._has_revoke(
            "lexical_documents", ["public", "anon", "authenticated"]
        )

    def test_revoke_python_state_table(self) -> None:
        self._has_revoke(
            "lexical_resource_python_state", ["public", "anon", "authenticated"]
        )

    def test_revoke_workflow_python_state_helper(self) -> None:
        self._has_revoke(
            "hivemind_workflow_python_state", ["public", "anon", "authenticated"]
        )

    def test_does_not_revoke_from_service_role(self) -> None:
        # service_role must keep its access; 011 must not revoke from it.
        for stmt in self._revoke_statements():
            self.assertNotIn(
                "service_role", stmt,
                f"011 must NOT revoke from service_role: {stmt!r}",
            )

    def test_does_not_touch_lexical_search_grant(self) -> None:
        # The hardened RPC's service_role grant (009:268) must be untouched.
        # 011 must neither revoke nor grant on hivemind_lexical_search.
        # Inspect ACTUAL statements only (leading token revoke/grant), not
        # comment prose that merely mentions the words.
        touched = [s for s in self._statements_starting_with("revoke", "grant")
                   if "hivemind_lexical_search" in s]
        self.assertEqual(
            touched, [],
            f"011 must not issue any revoke/grant on hivemind_lexical_search: {touched}",
        )

    def test_is_ddl_only_no_transaction_or_cic(self) -> None:
        low = self.sql.lower()
        # No CREATE INDEX CONCURRENTLY (forbidden in a txn / autocommit-only);
        # no BEGIN/COMMIT wrapper (autocommit-safe).
        self.assertNotIn("create index concurrently", low)
        self.assertNotIn("begin;", low)
        self.assertNotIn("commit;", low)


# ===========================================================================
# Layer 2 — CLUSTER-gated (isolated throwaway PostgreSQL)
# ===========================================================================
@unittest.skipUnless(find_pgbins(), "PostgreSQL binaries (initdb/pg_ctl/psql) not found")
class TestSecurityHardeningCluster(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cluster = R.LP.LocalCluster.start()
        try:
            R.reset_schema(cls.cluster)
            # bootstrap() applies 001 + 003..009 (the candidate suite's
            # migration set). We then apply 010 + 011 explicitly.
            R.bootstrap(cls.cluster)
            for name in ("010_lexical_latency_fix.sql", "011_lexical_security_hardening.sql"):
                cls.cluster.psql_file(R.SCHEMA_DIR / name)
            # Restore the Supabase-default schema USAGE the bare cluster lacks
            # so per-object privilege (the thing 011 narrows) is what decides.
            cls.cluster.psql(GRANT_SUPABASE_DEFAULTS, capture=False)
            # Seed AFTER all migrations so tables/columns exist. A modest seed
            # is enough for security + eligibility assertions.
            R.seed(cls.cluster, n_messages=4000)
        except Exception:
            cls.cluster.tear_down()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.cluster.tear_down()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _as_role_errors(self, role: str, sql: str) -> bool:
        """Run `SET ROLE <role>; <sql>` and return True if it ERRORs."""
        # ON_ERROR_STOP + a single -c with two statements: the SET ROLE scopes
        # the second statement to that role for this session. We use a wrapper
        # so the SET ROLE applies to the probe.
        wrapped = f"set role {role}; {sql}"
        rc, _ = self.cluster.psql(wrapped)
        # Always reset so later tests aren't accidentally scoped.
        self.cluster.psql("reset role;")
        return rc != 0

    def _as_role(self, role: str, sql: str) -> tuple[int, str]:
        rc, out = self.cluster.psql(f"set role {role}; {sql}")
        self.cluster.psql("reset role;")
        return rc, out

    # ------------------------------------------------------------------
    # Idempotence: re-applying 011 is a no-op.
    # ------------------------------------------------------------------
    def test_migration_idempotent(self) -> None:
        self.cluster.psql_file(SCHEMA_011)

    # ------------------------------------------------------------------
    # anon / authenticated CANNOT execute hivemind_lexical_candidates.
    # ------------------------------------------------------------------
    def test_anon_blocked_from_candidate_fn(self) -> None:
        sql = ("select * from public.hivemind_lexical_candidates("
               "'x', 1) limit 1;")
        self.assertTrue(self._as_role_errors("anon", sql),
                        "anon must ERROR calling hivemind_lexical_candidates")

    def test_authenticated_blocked_from_candidate_fn(self) -> None:
        sql = ("select * from public.hivemind_lexical_candidates("
               "'x', 1) limit 1;")
        self.assertTrue(self._as_role_errors("authenticated", sql),
                        "authenticated must ERROR calling hivemind_lexical_candidates")

    # ------------------------------------------------------------------
    # anon / authenticated CANNOT read or write the two lexical tables.
    # ------------------------------------------------------------------
    def test_anon_blocked_read_lexical_documents(self) -> None:
        self.assertTrue(self._as_role_errors("anon", "select count(*) from public.lexical_documents;"),
                        "anon must ERROR reading lexical_documents")

    def test_anon_blocked_read_python_state(self) -> None:
        self.assertTrue(self._as_role_errors("anon", "select count(*) from public.lexical_resource_python_state;"),
                        "anon must ERROR reading lexical_resource_python_state")

    def test_anon_blocked_write_lexical_documents(self) -> None:
        sql = ("insert into public.lexical_documents "
               "(entity_type, item_id, representation_type, chunk_index, "
               "chunk_text, representation_hash, chunk_hash) values "
               "('resource','9999','workflow_python',0,'x','h','h');")
        self.assertTrue(self._as_role_errors("anon", sql),
                        "anon must ERROR writing lexical_documents")

    def test_anon_blocked_write_python_state(self) -> None:
        sql = ("insert into public.lexical_resource_python_state "
               "(resource_id, kind, cohort, public_state, available) values "
               "(999999,'workflow','payload_python','safe',true);")
        self.assertTrue(self._as_role_errors("anon", sql),
                        "anon must ERROR writing lexical_resource_python_state")

    def test_authenticated_blocked_read_lexical_documents(self) -> None:
        self.assertTrue(self._as_role_errors("authenticated", "select count(*) from public.lexical_documents;"),
                        "authenticated must ERROR reading lexical_documents")

    def test_authenticated_blocked_read_python_state(self) -> None:
        self.assertTrue(self._as_role_errors("authenticated", "select count(*) from public.lexical_resource_python_state;"),
                        "authenticated must ERROR reading lexical_resource_python_state")

    # ------------------------------------------------------------------
    # anon CANNOT execute the workflow_python_state helper directly.
    # ------------------------------------------------------------------
    def test_anon_blocked_from_workflow_python_state_helper(self) -> None:
        self.assertTrue(self._as_role_errors("anon", "select public.hivemind_workflow_python_state(20);"),
                        "anon must ERROR calling hivemind_workflow_python_state")

    def test_authenticated_blocked_from_workflow_python_state_helper(self) -> None:
        self.assertTrue(self._as_role_errors("authenticated",
                        "select public.hivemind_workflow_python_state(20);"),
                        "authenticated must ERROR calling hivemind_workflow_python_state")

    # ------------------------------------------------------------------
    # The helper is a PRIVATE internal routine: NO role (including
    # service_role) can call it DIRECTLY after 011. It is reached ONLY via the
    # SECURITY DEFINER RPC (owner postgres). service_role calling it directly
    # must be DENIED — defense in depth.
    # ------------------------------------------------------------------
    def test_service_role_blocked_from_helper_directly(self) -> None:
        self.assertTrue(self._as_role_errors("service_role",
                        "select public.hivemind_workflow_python_state(20);"),
                        "direct service_role call to workflow_python_state must be DENIED; "
                        "the helper is reached only via the SECURITY DEFINER RPC")

    # ------------------------------------------------------------------
    # service_role CAN call the hardened RPC (the legitimate read path).
    # ------------------------------------------------------------------
    def test_service_role_can_call_rpc(self) -> None:
        # SET ROLE service_role, call the RPC. It should succeed (rc==0) and
        # return a valid json envelope with a 'count' field.
        sql = ("select public.hivemind_lexical_search('WanVideoSampler',1,"
               "'{}','{}','{}',null,'{}','{}','lexical')::text;")
        rc, out = self._as_role("service_role", sql)
        self.cluster.psql("reset role;")
        self.assertEqual(rc, 0, f"service_role RPC call must succeed; got rc={rc} out={out[:200]}")
        start = out.find("{")
        end = out.rfind("}")
        self.assertGreaterEqual(start, 0, f"no JSON in RPC output: {out[:200]!r}")
        envelope = json.loads(out[start:end + 1])
        self.assertIn("count", envelope, f"RPC envelope missing 'count': {envelope}")
        self.assertIn("results", envelope, f"RPC envelope missing 'results': {envelope}")

    # ------------------------------------------------------------------
    # Eligibility is preserved (RPC reaches the helper via SECURITY DEFINER):
    # rejected distillation / soft-deleted message / quarantined
    # workflow_python never appear in RPC results.
    # ------------------------------------------------------------------
    def _rpc_result_ids(self, query: str, **kw):
        # Call as the cluster superuser (postgres) — the RPC is SECURITY
        # DEFINER and the owner; service_role also works. Use the existing
        # helper which runs as the connection user (postgres).
        resp = R.call_rpc(self.cluster, query, **kw)
        return resp

    def test_rejected_distillation_absent_from_rpc(self) -> None:
        # Seed a rejected distillation whose terms would otherwise match.
        self.cluster.psql(
            "insert into public.distillations (id, question, conditions, answer, "
            "confidence, status, author_id) overriding system value values "
            "(777,'How do I reduce motion strength rejected','x','y','low','rejected',1) "
            "on conflict do nothing;"
        )
        resp = self._rpc_result_ids("reduce motion strength")
        ids = {r["item_id"] for r in resp["results"] if r["kind"] == "distillation"}
        self.assertNotIn("777", ids,
                         f"rejected distillation 777 must not appear: {ids}")

    def test_softdeleted_message_absent_from_rpc(self) -> None:
        # The candidate seed soft-deletes messages where i % 200 == 0; i=0 is
        # the first such message and its body is a planted 'sampler settings'
        # template -> it would match 'sampler' if eligibility were off.
        deleted_msg = str(1_000_000_000_000_000_000 + 0)
        resp = self._rpc_result_ids("sampler settings for video")
        ids = {r["item_id"] for r in resp["results"] if r["kind"] == "message"}
        self.assertNotIn(deleted_msg, ids,
                         f"soft-deleted message {deleted_msg} must not appear: {ids}")

    def test_quarantined_workflow_python_absent_from_rpc(self) -> None:
        # Quarantine excludes only the WORKFLOW_PYTHON representation of a
        # resource, NOT the resource's prose (a quarantined workflow may still
        # legitimately match a prose arm — that is expected, not a leak). The
        # security-relevant guarantee is twofold:
        #   (a) STRUCTURAL: a quarantined resource has ZERO workflow_python
        #       lexical_documents rows (schema/003 CHECK forbids
        #       representation_type='workflow_python' with quarantine_state<>
        #       'safe', and the refresh never writes a doc for quarantined code).
        #   (b) GATE: even if a stray safe doc somehow existed, the candidate
        #       SQL's safe_wf CTE (010) requires
        #       hivemind_workflow_python_state(id)='safe', so a quarantined
        #       resource's code can never rank via the workflow_python arms.
        # Plant a quarantined workflow whose prose DOES mention the query term
        # (so prose may surface it) but whose code is quarantined.
        self.cluster.psql(
            "insert into public.external_resources (id, kind, source, external_id, "
            "title, body, author, url, metadata) overriding system value values "
            "(31337,'workflow','vibecomfy-external','w31337','QuarantinedCredentialWorkflow',"
            "'Workflow prose mentions WanVideoSampler but its code is quarantined',"
            "'agent',null,'{}') on conflict do nothing;"
        )
        self.cluster.psql(
            "insert into public.lexical_resource_python_state "
            "(resource_id, kind, cohort, public_state, available, chunk_count) values "
            "(31337,'workflow','payload_python','quarantined',false,0) "
            "on conflict (resource_id) do update set public_state='quarantined', available=false;"
        )
        # (a) The helper reports quarantined (RPC path intact after 011 revoke).
        rc, out = self.cluster.psql("select public.hivemind_workflow_python_state(31337);")
        self.assertEqual(rc, 0)
        self.assertIn("quarantined", out.strip().lower(),
                      f"workflow_python_state(31337) should be quarantined: {out!r}")
        # (b) Structural: zero workflow_python lexical_documents for 31337.
        rc, out = self.cluster.psql(
            "select count(*) from public.lexical_documents "
            "where item_id='31337' and representation_type='workflow_python';"
        )
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.strip(), "0",
                         f"quarantined resource 31337 must have 0 workflow_python docs: {out!r}")
        # (c) The candidate SQL's workflow_python arms never emit 31337. Call
        #     the candidates fn directly (as superuser/owner) and assert that
        #     NO row for 31337 carries representation_type='workflow_python'.
        rc, out = self.cluster.psql(
            "select representation_type from public.hivemind_lexical_candidates("
            "'WanVideoSampler', 500, '{workflow}','{}','{}',null,'{}','{}',false,false) "
            "where item_id='31337';"
        )
        self.assertEqual(rc, 0, out)
        reps = {ln.strip() for ln in out.splitlines() if ln.strip()}
        self.assertNotIn(
            "workflow_python", reps,
            f"quarantined resource 31337 must never rank via workflow_python; "
            f"got representations={reps}",
        )


if __name__ == "__main__":
    unittest.main()
