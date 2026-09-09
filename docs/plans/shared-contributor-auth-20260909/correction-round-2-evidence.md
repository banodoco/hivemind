# Correction-round 2 verification evidence

All checks below ran in the isolated Hivemind delivery worktree on 2026-09-09.
Secrets, request tokens, polling secrets, raw contributor keys, database URLs,
and host credentials are intentionally omitted.

| Check | Result |
| --- | --- |
| `python3 -m unittest tests.test_contributor_auth -v` | PASS — 11 tests |
| `deno check supabase/functions/contributor-auth/index.ts supabase/functions/contributor-auth/protocol.ts supabase/functions/contributor-auth/protocol_test.ts` | PASS |
| `deno test --allow-net supabase/functions/contributor-auth/protocol_test.ts supabase/functions/contribute/protocol_test.ts supabase/functions/submit-vibecomfy-rating/protocol_test.ts` | PASS — 20 tests |
| `python3 scripts/rehearse_contributor_auth.py` | PASS — disposable PostgreSQL cluster created and torn down; claim-pending, post-claim resolution, concurrent redemption, cleanup, replay, rate-limit, expiry, and per-key revoke assertions passed |
| `deno fmt --check supabase/functions/contributor-auth/index.ts supabase/functions/contributor-auth/protocol.ts supabase/functions/contributor-auth/protocol_test.ts` | PASS |
| `python3 -m unittest tests.test_contributor_auth tests.test_contribute` | PASS — 18 tests |
| `python3 -m unittest discover tests/` | PASS — 1,515 tests, 17 skipped, 157.768s |

The disposable rehearsal confirms that unlinked legacy rows/keys remain
auditable but do not resolve for writes, that an issued key is durably linked
to its consumed request, and that poll-secret-bound cleanup revokes that key
without issuing a replacement. No live Supabase, Banodoco, Astrid, deployment,
PR, merge, or oracle operation was performed.
