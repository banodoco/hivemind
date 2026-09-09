# Agent goal — shared contributor authentication

## Objective

Prepare and, in a future authorized delivery run, implement a shared login
path for Hivemind contributors. Public search and read behavior must remain
available without authentication, including through Astrid. Contribution must
require an authenticated contributor key; editor approval must remain a
separate existing permission.

## In scope

- Add `contributors.auth_user_id` and a `contributor_keys` table supporting
  multiple device keys, revocation, timestamps, and safe hashed-key lookup.
- Hivemind owns the canonical schema/RPC/broker logic; add a static
  `/connect/index.html` route to the Banodoco website at
  `https://www.banodoco.ai/connect/`, including its `deploy/public-files.json`
  allowlist entry. Use a pinned Supabase browser SDK and the existing shared
  Supabase session identity; Arca Gidan is read-only auth/identity reference
  material and receives no product changes.
- Migrate existing valid and revoked `contributors.api_key_hash` credentials
  in one direct transaction, preserving contributor IDs, attribution, editor
  status, revocation, and key validity. Deduplicate/conflict-check before the
  copy, then use the new key table for lookup; do not add a parallel dual-read
  compatibility shim.
- Reuse the shared Supabase Discord OAuth identity (`members.auth_user_id`)
  used by Arca Gidan. Link only through a trusted stable mapping or explicit
  operator action; never match on display name or email. Any verified Discord
  Supabase user may become a normal contributor by default; do not invent an
  additional membership gate. Preserve any explicit restrictions actually
  found, never autoedit or receive an automatically created editorial identity.
- Provide a hosted browser approval broker with short-lived, one-use,
  race-safe request state and CLI-held polling secret. Bind browser login to
  CSRF/session state, retain request continuity in `sessionStorage` through a
  fixed `/connect/` callback, rate-limit polling, and make approval GETs
  side-effect free. After login, show the requesting machine and code and
  require an explicit approval POST; OAuth completion never auto-approves.
  Approval records identity only; CLI redemption atomically generates, hashes,
  and returns one key without plaintext server persistence. Handle a lost
  handoff response with clear restart/cleanup semantics and bounded
  retry/new-request behavior rather than unlimited minting. The browser gets no
  contributor key or service secret.
- Add shared CLI login, status, and logout/revoke behavior. Attempt browser
  launch, always print the URL, and support `--no-browser`.
- Save device keys owner-only under `~/.hivemind/key`, preserve environment
  override precedence, and make all contribution-capable executors reuse the
  resolver.
- Update the Astrid Hivemind pack/pin and its documentation to use the shared
  auth flow and the existing contribution backend.
- Prove anonymous reads, authenticated writes, editor separation, migration,
  expiry, replay/race safety, revocation, and no-secret leakage with mocked
  and disposable Supabase fixtures, static packaged-route and OAuth
  callback/state tests, plus a manual OAuth acceptance check when an authorized
  environment exists.

## Explicitly out of scope

No public-read login wall, query or ranking redesign, general IAM or role
system, native Supabase device grant, separate Astrid publishing backend,
device dashboard, OS keychain integration, unrelated Astrid auth/media work,
duplicate migrations or product changes in the Arca Gidan checkout, production
migration, deployment, or operational execution. Do not replace the existing
Hivemind GitHub CTA; the CLI URL is sufficient.

## Source and authority

Source is the Hivemind repository at
`https://github.com/banodoco/hivemind.git`, ref `main`.
The historical planning source pin is the clean remote `main` at
`e93f7e37bcc49b21b47a715ba6ffc8ce239609f8` (remote `main` and `hivemind`
agreed at census time). A recipient must start from the current remote
`main`, record its actual SHA, and reconcile differences before freezing T1.
The original local checkout was
`50ff509240c5582a7335dc71920533b59be7792c`,
ahead 3/behind 22 with untracked `briefing.md` and
`handoff-agent-note.txt`; those untracked files remain preserved and are
excluded from the code baseline, without making any claim about their contents.
The Astrid source census found clean
`main` `3e1a8c830fcb670b042ea6ce2f8f009ebb2ea91f`; untracked workflow
notes/assets remain outside the selected baseline, without a claim about their
contents.
Banodoco website deployed/origin `main` is
`f91eca9c748531957a2740707aee753ba674e17a`; the clean local
`preview/true-union` checkout is `d18eb46557f4d480e040d1a07783edea3b616577`.
Preserve that local preview checkout and choose the deployed/origin `main`
baseline for future delivery; do not overwrite preview with `main`.
Arca Gidan is `main` `1c1fbadce8868ed3ae806bc8ca28a65ea67a6135` and is retained
only as read-only identity/auth reference material, with no product changes.
The shared Supabase checkout is read-only identity evidence, not the migration
owner. The durable direction is
[northstar.md](northstar.md), and the single run declaration is [run.yaml](run.yaml).

The user explicitly authorized this delivery run on 2026-09-09. Product source
mutation and focused validation are permitted within this scope, but PR
creation, merge, deployment, cutover, and production operations remain
separately unauthorized. T2/T3 and their dependents must not start until the
shared Supabase identity-schema prerequisite is resolved with authoritative
source or concrete operator-owned runtime evidence and access path.

## Acceptance contract

Implementation criteria are C1–C9 in [plan.md](plan.md). The source census is
complete; the future run must stop if implementation uncovers a conflicting
admission rule, editor rule, or identity mapping, and the configured oracle
adjudicates that concrete conflict. Arca `is_admin` remains distinct from
Hivemind `contributors.is_editor`. No criterion may be weakened to make a
review pass. Exact Banodoco CORS origin and Supabase redirect-allowlist entries
are declared configuration for a future authorized environment only; this
planning run does not deploy or mutate them.

## Estimate and limits

Estimate: 5–7 focused engineering days for the agreed scope, plus 1–2 focused
days of integration contingency for the shared OAuth/broker, static host route,
and key migration.
That is human-equivalent engineering effort; parallel agent elapsed time may
be shorter or longer and is not evidence of completion. Validation should use
mocked/unit fixtures first, a disposable Supabase-shaped fixture for migration
and authorization, and one manual OAuth acceptance check only when an
authorized environment is supplied.
