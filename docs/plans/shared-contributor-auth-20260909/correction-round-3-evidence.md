# Correction-round 3 verification evidence

All checks ran in the isolated Hivemind delivery worktree on 2026-09-09.
Secrets, request tokens, polling secrets, raw contributor keys, database URLs,
and host credentials are intentionally omitted.

| Check | Result |
| --- | --- |
| `python3 -m unittest tests.test_contributor_auth -v` | PASS — 13 tests, including truncated redeem response plus cleanup-failure redaction |
| `python3 scripts/rehearse_contributor_auth.py` | PASS — disposable PostgreSQL cluster created and torn down; safe rollback retained the migration-040 resolver boundary, rejected the revoked legacy key, and preserved legacy/audit rows |
| `deno check supabase/functions/contributor-auth/index.ts supabase/functions/contributor-auth/protocol.ts supabase/functions/contributor-auth/protocol_test.ts` | PASS |
| `deno test --allow-net supabase/functions/contributor-auth/protocol_test.ts supabase/functions/contribute/protocol_test.ts supabase/functions/submit-vibecomfy-rating/protocol_test.ts` | PASS — 20 tests |
| `deno fmt --check supabase/functions/contributor-auth/index.ts supabase/functions/contributor-auth/protocol.ts supabase/functions/contributor-auth/protocol_test.ts` | PASS |
| `python3 -m unittest discover tests/` | PASS — 1,517 tests, 17 skipped |
| `git diff --check` | PASS |

The rehearsal's redacted assertions included anonymous resolver privilege `0`,
service-role resolver privilege `1`, revoked legacy-key status, zero resolver
rows after revocation, and retained legacy contributor/key rows. No live
Supabase, Banodoco, Astrid, deployment, PR, merge, or oracle operation was
performed.
