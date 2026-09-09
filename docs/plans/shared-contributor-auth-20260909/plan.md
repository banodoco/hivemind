# Implementation plan — shared contributor authentication

## Current contract and decisions

The authoritative delivery source is PR4's immutable knowledge model: public
reads expose messages and accepted current resource revisions, while the
single `supabase/functions/contribute` edge function accepts
`submit_resource`, `propose_revision`, `decide_revision`, `mark_canonical`,
`capture_message_snapshot`, and `submit_evidence`. It authenticates the
current `X-Contributor-Key` before calling the schema RPCs. Resources and
revisions/evidence have distinct existing publication semantics; the auth
work preserves them exactly and does not reintroduce a retired presentation
model. Search/query design remains unchanged.

The authoritative shared identity evidence is in `banodoco-workspace`, not the
Arca application checkout. Its current main is pinned in `provenance.md` and
provides the nullable, unique `members.auth_user_id` mapping, Discord resolver
and trigger/backfill plumbing, plus a separate `public.admins` authority. The
Hivemind contributor principal is intentionally narrower: it binds directly to
the shared Supabase `auth.users.id`, and a mapped `members` row is not an
admission requirement. `members.auth_user_id` is an optional Discord/profile
association only. Do not substitute the retired `ag_user_identities` path.
Banodoco's static page uses a pinned, dependency-free Supabase Auth REST/PKCE
browser flow and the existing shared Supabase session identity; Arca's implementation is read-only reference
material and receives no product changes. Hivemind owns its contributor
identity and adds the direct auth UUID to `contributors`. Existing contributor
rows and IDs remain stable. A `contributor_keys` table holds one hash per
device key, with contributor link, label/created timestamps, last-use metadata,
and `revoked_at`;
raw keys are never stored. Existing valid and revoked legacy hashes are
deduplicated/conflict-checked, then copied in one direct transaction preserving
their state. After migration, new lookup uses `contributor_keys`; no parallel
dual-read compatibility shim is planned. An unlinked legacy row/key remains
available for audit and status inspection as `claim_pending`, but cannot
authenticate a write until an operator binds the contributor row to a verified
Auth identity.

The completed source census found the current editor representation in
`contributors.is_editor` and the schema functions `decide_revision` and
`mark_canonical` requiring an active editor. Arca `is_admin` is a separate
authority and must not be treated as Hivemind editor status. The default
admission ruling is that any verified Discord Supabase user may become a normal
contributor; no additional membership gate is invented. A contributor is
linked only through a trusted stable mapping or explicit operator action.
Never match on display name or email, and never auto-create/grant a new
editorial identity from login alone. Login does not change existing editor
permission checks or publication semantics.

Hivemind owns the schema, RPC, identity mapping, and broker edge logic. The
hosted `/connect/index.html` route is served by Banodoco at
`https://www.banodoco.ai/connect/` and is included through
`deploy/public-files.json`; it uses a pinned Supabase browser SDK. The page
stores the opaque request capability in `sessionStorage`, returns to the fixed
`/connect/` callback, and restores the shared Supabase session. A CLI creates a
request containing an opaque short-lived request identifier and a high-entropy
polling secret. Only the approval URL capability is sent to the browser; the
CLI secret stays local and is never put in a URL or log. After Discord OAuth,
the page displays the requesting machine and code and requires an explicit
approval POST; OAuth completion never auto-approves. The browser receives no
contributor key or service secret. The broker validates the server-side user
and binds browser session/CSRF state before resolving `auth_user_id`. Approval
GETs are side-effect free and approval records only the approved identity. CLI
redemption atomically consumes that approval, generates one random key, stores
only its hash, and returns the plaintext once. Polling is rate-limited and
bounded, rejects expired/consumed/revoked state, and uses a conditional update
or transaction to make concurrent redemption race-safe. If the handoff response
is lost, the client receives clear cleanup/restart semantics with bounded retry
or a new request; polling cannot mint unlimited credentials. Key material is
never logged or persisted server-side in plaintext.

The broker allows only the exact `https://www.banodoco.ai` origin (plus
loopback for disposable rehearsal) and the fixed Supabase redirect callback is
`https://www.banodoco.ai/connect/`. These are checked-in delivery defaults;
live Supabase redirect/CORS configuration still requires a separately
authorized environment change. Keep the existing Hivemind GitHub CTA; the
CLI-generated URL is sufficient.

The CLI should expose a shared command surface such as `hivemind auth login`,
`hivemind auth status`, and `hivemind auth logout` with an explicit local
delete versus server revoke operation. Login attempts to open the browser,
always prints the URL, and supports `--no-browser`. Status is structured and
redacts key values. The resolver continues to prefer a non-empty
`HIVEMIND_CONTRIBUTOR_KEY` over `~/.hivemind/key`; writes use owner-only file
permissions and atomic replacement. Astrid contribution and ingestion
executors call the same resolver and surface a deterministic login-required
error without interrupting reads. Reuse the existing `hivemind.cli:main`,
`_common.resolve_contributor_key`, `edge_post`, and all four current writers;
do not repurpose admin-switch-session/impersonate-user or unrelated content
approval-request infrastructure.

## Delivery reconciliation — D2 (2026-09-09)

The separate `submit-vibecomfy-rating` edge writer is included in the shared
authentication acceptance closure because it directly validates
`contributors.api_key_hash`/`revoked_at`. It remains a separate endpoint with
its existing rating semantics, payloads, attribution, and upload behavior; it
is not a seventh knowledge-model action. T5/C7/C8 evidence must cover its
migrated-key lookup, revocation, missing-key errors, and no-secret behavior.

Astrid's strict managed external-pack admission makes Hivemind pack-v2
compatibility and a retrievable immutable Hivemind pin explicit T6 deliverables.
The received v1 `pack.yaml` and unavailable `50ff509…` pin are not acceptable
installation evidence. This reconciliation does not release D1's prerequisite
gate for authoritative shared-Supabase identity-schema evidence.

## Implementation criteria

These IDs are the implementation contract for the future run and are used by
the declared review stages in `run.yaml`.

### C1 — Anonymous public read preservation

Search, get-item, media refresh/read surfaces, and their Astrid entrypoints
work with only the existing public anon configuration and no contributor key.
No read command invokes login or emits a login prompt. Accepted current
resource revisions, messages, evidence, and all existing RLS/visibility rules
remain unchanged. Proof: mocked HTTP tests and the affected public-read
regression suite show no auth dependency.

### C2 — Identity and key schema migration

`contributors.auth_user_id` links directly to `auth.users(id)` with a unique
constraint. A mapped `members` row is not required for admission.
`contributor_keys` supports
multiple device keys, hashed lookup, revocation, and useful lifecycle
timestamps. A direct transactional migration deduplicates/conflict-checks and
copies both valid and revoked legacy hashes, then the new lookup path is used;
there is no dual-read shim. Existing contributor IDs, attribution, editor
fields/status, and legacy key rows continue to be retained. An unlinked legacy
key is `claim_pending` and cannot authenticate a write; after an explicit
operator claim, the same key can authenticate under its preserved contributor.
Proof: SQL fixture migration checks counts, hashes, claim-pending blocking,
post-claim resolution, revoked behavior, foreign keys, and uniqueness.

### C3 — Secure broker exchange

The hosted approval flow uses expiring, one-use, race-safe request state and a
CLI-held polling secret. Server-side user validation, CSRF/session binding,
poll rate limits, and side-effect-free approval GETs are enforced. Approval
records identity only; CLI redemption atomically generates and hashes one key,
returning plaintext once without server persistence. Replay, expiry, concurrent
redemption, wrong-secret, polling-after-consume, and lost-response/retry cases
are rejected or resolved with bounded explicit cleanup. Proof: deterministic
protocol/unit tests, packaged-route and OAuth callback/state tests, plus a
disposable Supabase-shaped integration fixture.

### C4 — Admission and editor separation

Any verified Discord Supabase user can be mapped/admitted as a normal
contributor by default and can submit under each current action's
publication/moderation path. Trusted stable mapping or explicit operator
action is required; display-name/email matching and automatic editorial
identity grants are forbidden. Login does not change the existing
`is_editor`-based decision permissions or make an Arca admin a Hivemind editor.
Proof: verified-user, unmapped-user, non-editor, editor, and action-semantics
fixtures; any explicit discovered restriction has an oracle disposition.

### C5 — Shared CLI credential UX

CLI login/status/logout/revoke commands use the broker and shared resolver.
Login attempts browser opening, always prints the URL, supports `--no-browser`,
and saves a key atomically owner-only. Environment override precedence is
preserved. Structured status redacts secrets. Local logout deletes only the
local key; revoke performs an explicit server-side revocation and reports the
distinct result. Proof: mocked broker and filesystem tests, including failure
and interrupted-write cases.

### C6 — Astrid reuse and pin update

The Astrid Hivemind pack uses the shared resolver/auth command contract, keeps
all reads public, and uses the existing contribution edge function. The old
Hivemind pin/docs are updated only where required to consume the new contract;
no parallel publishing backend is introduced. Proof: pack manifest/runtime
contract checks and an executor-level contribution/read fixture.

### C7 — Contribution compatibility and error boundary

The existing contribution payloads, key format, attribution, idempotency,
reference validation, immutable resource/revision/evidence rules, and each
current publication/decision rule continue to work unchanged. Missing,
expired, or revoked credentials produce a structured login-required/
unauthorized result with the login command and never block an unrelated read.
Proof: current protocol tests plus new key-state/error fixtures.

### C8 — Secret and race regression coverage

Tests prove no raw key or polling secret enters URLs, logs, structured output,
or persisted non-secret metadata; two redemptions cannot mint two keys for one
approval; replay, rate limits, lost-response cleanup, and revocation are
enforced across all write surfaces. Proof: redaction assertions and
concurrent/disposable database fixture results.

### C9 — Documentation and operational boundary

CLI, Astrid, and pack documentation explain public reads, contribution login,
editor separation, local-vs-server logout, env/file precedence, browser URL
handling, and the no-login read invariant. No production operation is claimed
or run by the implementation. Proof: docs review tied to the runtime contract,
static packaged-route and OAuth callback/state checks, and a manual OAuth
acceptance checklist marked with its actual environment; live proof is pending
an authorized environment.

## Work sequence and dependencies

The source census is complete. The historical Hivemind planning pin is PR4
`e93f7e3`; start future delivery from the current remote `main`, record its
actual SHA, and reconcile differences before freezing T1. Keep the exact
historical Astrid
`3e1a8c8`, Banodoco deployed/origin `main` `f91eca9c`, and read-only
shared-Supabase and Arca identity evidence. Preserve Banodoco's clean local
`preview/true-union` `d18eb465` checkout; future delivery uses the deployed/
origin `main` baseline and must not overwrite preview. Freeze those identities
before schema design. Then lock the direct transactional legacy-key migration
and broker protocol together, because the broker cannot safely issue a key until
trusted identity admission is defined. Implement the Hivemind schema/RPC/edge
boundary and fixtures, the Banodoco static `/connect/index.html` route and
allowlist packaging, the shared CLI resolver/auth commands, then Astrid/docs.
Run focused tests as each seam lands, including packaged-route and OAuth
callback/state tests, a disposable migration/auth rehearsal at integration, and
the broad affected suite once on the final candidate.

## Uncertainties requiring an owner

  - The authoritative workspace migration source is pinned in the delivery
  receipt. Hivemind's direct auth UUID binding must remain independent of the
  optional Discord/profile member mapping.
- The Banodoco static callback route must restore the shared Supabase session
  with the pinned browser SDK, retain request continuity through
  `sessionStorage`, show machine/code, and require explicit approval. The exact
  `https://www.banodoco.ai` CORS origin and fixed Supabase redirect allowlist
  entry are configuration for a future authorized environment only; no deploy
  or live mutation is authorized here. Arca is read-only reference material and
  receives no product changes.
- The legacy hash migration must complete as one deduplicated/conflict-checked
  transaction, then use `contributor_keys` lookup. No dual-read compatibility
  branch is permitted.
- Logout UX must make local deletion and server revocation two explicit
  operations; the delivery task owns the exact flags/command wording.

## Future validation approach

Use stdlib Python unit tests for resolver, file modes, redaction, URL/browser
behavior, and HTTP error mapping; static packaged-route and OAuth callback/state
tests; Deno protocol tests for edge-function validation and key-state rules; and
a disposable Supabase-shaped PostgreSQL
fixture for migration, trusted identity mapping, atomic consume, revocation,
rate limiting, lost-response cleanup, and editor separation. Manual OAuth
acceptance is limited to one supplied hosted environment and records the
URL/browser/result without credentials. If staging/deployment is unauthorized,
code completion does not require a live smoke test; document that live proof is
pending instead. All evidence must identify command,
fixture/environment, result, and digest; no future review may infer proof from
a worker summary.
