-- ============================================================
-- 011_lexical_security_hardening.sql
-- Hivemind hybrid search — Phase 1, Task 1.12 (security hardening)
--
-- Closes the trust boundary ADDITIVELY. Several Phase-1 lexical
-- objects were left reachable by anon / authenticated after 003/008/009:
--
--   * `hivemind_lexical_candidates` (schema/008) is the canonical
--     candidate SQL. It is STABLE, SECURITY INVOKER, and PUBLIC-executable
--     by Supabase default (no GRANT/REVOKE anywhere). Although it is meant
--     to be called only by the SECURITY DEFINER RPC in 009, nothing stopped
--     anon from running it directly and reading the full ranked identity
--     stream (with matched_anchor snippets) bypassing the RPC's input
--     bounds + hydration.
--
--   * `lexical_documents` and `lexical_resource_python_state` (schema/003)
--     have `revoke all from public` (003:421-422) and RLS enabled with no
--     permissive policy, BUT anon/authenticated still retain Supabase's
--     default table privileges. RLS hides the rows; the privilege itself
--     remains. This migration removes the privilege so the tables are
--     invisible by GRANT, not only by policy (defense in depth: a future
--     permissive policy or a disabled RLS flag would not re-expose them).
--
--   * `hivemind_workflow_python_state(bigint)` (schema/003:306) is a
--     security-sensitive helper: it surfaces a resource's safe/quarantined
--     python cohort state. It is PUBLIC-executable by default. The
--     candidate SQL (008/010) and the RPC reach it internally; because the
--     RPC is SECURITY DEFINER and runs as the owner/service_role, revoking
--     public execute on this helper does NOT break the RPC — it only stops
--     anon from probing individual resource states directly.
--
-- PRESERVED (do NOT touch here — already correct in 009:266-269):
--   * `grant execute on function public.hivemind_lexical_search(...) to
--     service_role` — the sole legitimate read path. The RPC remains
--     revoked from public/anon/authenticated and granted to service_role.
--   * Owner + maintenance access (the migration owner, table owners,
--     VACUUM/ANALYZE via the owning role).
--   * service_role BYPASSRLS table access (it maintains these tables via
--     the refresh job and reads them via the RPC).
--   * Phase-2 (020-029) embedding objects — out of scope, untouched.
--
-- anon / authenticated get NOTHING on the four objects below: no execute,
-- no SELECT/INSERT/UPDATE/DELETE, no TRUNCATE/REFERENCES/TRIGGER. They
-- retain only what they already lacked.
--
-- DDL only — pure REVOKE. Autocommit-safe (no transaction block, no CIC).
-- Idempotent: REVOKE is naturally idempotent; re-running is a no-op.
-- No source rows are read or mutated. Additive (narrows privileges only).
-- ============================================================

-- ------------------------------------------------------------
-- 1. The canonical candidate SQL (schema/008). Revoke direct execute
--    from everyone except the implicit owner. The RPC (009) does not
--    need a grant here — it is SECURITY DEFINER and runs as its owner,
--    so it executes this INVOKER function with the owner's privileges
--    regardless of the caller's grants.
-- ------------------------------------------------------------
revoke execute on function public.hivemind_lexical_candidates(
  text, int, text[], text[], text[], timestamptz, text[], text[], boolean, boolean
) from public, anon, authenticated;

-- ------------------------------------------------------------
-- 2. The two lexical storage tables (schema/003). 003 already did
--    `revoke all from public`; this additionally strips the Supabase
--    default privileges that anon/authenticated still hold. RLS stays
--    enabled (defense in depth: invisible by GRANT AND by policy).
--    service_role bypasses RLS (BYASSRLS) and keeps owner access.
-- ------------------------------------------------------------
revoke all on public.lexical_documents from public, anon, authenticated;
revoke all on public.lexical_resource_python_state from public, anon, authenticated;

-- ------------------------------------------------------------
-- 3. The workflow-python state helper (schema/003:306). It leaks a
--    resource's safe/quarantined cohort; anon should not probe it. The
--    candidate SQL + RPC reach it internally via SECURITY DEFINER /
--    service_role, so revoking public execute does not break the RPC.
-- ------------------------------------------------------------
revoke execute on function public.hivemind_workflow_python_state(bigint)
  from public, anon, authenticated;

-- ------------------------------------------------------------
-- Intent comments (so the trust boundary is self-documenting in \d+).
-- ------------------------------------------------------------
comment on function public.hivemind_lexical_candidates(
  text, int, text[], text[], text[], timestamptz, text[], text[], boolean, boolean
) is
  'Task 1.7/1.12: THE canonical lexical candidate SQL (schema/008/010). STABLE, '
  'SECURITY INVOKER. NOT directly executable by anon/authenticated/public (011): '
  'the ONLY legitimate caller is the SECURITY DEFINER RPC '
  'hivemind_lexical_search (009), which bounds inputs and hydrates the '
  'surviving identities. Owner/service_role retain access.';

comment on table public.lexical_documents is
  'Task 1.2/1.12: chunk-aware lexical documents. Maintained by the service_role '
  'refresh job; read only by the hardened search RPC (009). anon/authenticated/public '
  'have NO privileges (011) AND RLS has no permissive policy (003) — invisible by '
  'GRANT and by policy (defense in depth). service_role bypasses RLS.';

comment on table public.lexical_resource_python_state is
  'Task 1.2/1.12: per-resource authoritative-Python cohort + safe/quarantined state. '
  'anon/authenticated/public have NO privileges (011) AND RLS has no permissive policy '
  '(003). service_role bypasses RLS for maintenance; the RPC reads it via SECURITY DEFINER.';

comment on function public.hivemind_workflow_python_state(bigint) is
  'Task 1.2/1.12: STABLE accessor for the workflow_python eligibility gate. NOT '
  'directly executable by anon/authenticated/public (011): it surfaces a resource''s '
  'safe/quarantined cohort. The candidate SQL + RPC reach it internally via the '
  'SECURITY DEFINER service_role path; revoking public execute does not break the RPC.';
