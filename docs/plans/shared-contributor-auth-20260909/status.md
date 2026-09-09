# Status — shared contributor authentication

State: **delivery implementation complete locally; awaiting final Astrid
integrated review**.

Correction-round contract: unlinked legacy contributor rows and key hashes are
retained for audit and report `claim_pending`; they cannot authenticate writes
until an explicit operator claim binds a verified Supabase Auth identity.

Mode: `delivery`. Product changes are retained only in the isolated delivery
worktrees. No PR, merge, deployment, cutover, or production operation has
occurred.

Estimate: 5–7 focused engineering days, plus 1–2 focused days of integration
contingency for the shared OAuth/broker, static host route, and key migration.

Decision update (2026-09-09): **ADOPTED** the `banodoco-host-findings.md`
recommendation. Banodoco is the browser host: static `/connect/index.html` at
`https://www.banodoco.ai/connect/`, packaged through `deploy/public-files.json`,
with dependency-free direct Supabase Auth REST/PKCE, shared session identity,
`sessionStorage` request continuity, fixed callback, machine/code display, and
explicit approval POST. Hivemind owns broker identity, key, and race logic; the browser receives
no contributor key or service secret. Arca is read-only auth/identity reference
material and receives no product changes. The existing Hivemind GitHub CTA
remains; CLI URLs are sufficient. This is direct user steering; no additional
oracle call was made.

Source census: **complete**. The historical planning source pin is clean
remote `main` at `e93f7e37bcc49b21b47a715ba6ffc8ce239609f8`,
with remote `main` and `hivemind` agreeing at census time. A recipient must
record the actual current `origin/main` SHA and reconcile differences before
freezing T1. The original local checkout was
`50ff509240c5582a7335dc71920533b59be7792c`, ahead 3/behind 22 with untracked
`briefing.md` and `handoff-agent-note.txt`; those untracked files remain
preserved and are excluded from the authoritative baseline, without a claim
about their contents. Reconcile them if relevant before future execution.
Astrid's tracked baseline is
`3e1a8c830fcb670b042ea6ce2f8f009ebb2ea91f`; untracked workflow notes/assets
remain outside the selected baseline, without a claim about their contents.
Banodoco deployed/origin `main` is
`f91eca9c748531957a2740707aee753ba674e17a`; its clean local
`preview/true-union` checkout is `d18eb46557f4d480e040d1a07783edea3b616577`.
Preserve preview and use deployed/origin `main` as the future delivery baseline;
do not overwrite preview. Arca Gidan is `1c1fbadce8868ed3ae806bc8ca28a65ea67a6135`
and remains read-only reference material. The shared Supabase checkout is
read-only identity evidence, not the migration owner. No original checkout
source was modified or discarded. The received current SHAs and worktree
branches are recorded in `provenance.md`. Astrid's auth seam was reconciled
during T6 to the final local Hivemind delivery SHA recorded in `provenance.md`;
remote publication is still required before Astrid can install it from GitHub.

Tests: focused correction-round checks are recorded in
`correction-round-3-evidence.md`; final integrated review pending.
Review counters: 2 intermediate, 0 final. Oracle calls: 3 of 3.

Oracle decision D1 (Astra, 2026-09-09): **BLOCKED at the time**. The supplied repositories
show Arca's client-side `members.auth_user_id` write path and Hivemind's
contributors/editor rules, but no authoritative shared-Supabase schema or
operator-owned evidence/access path. Do not implement T2/T3 from inferred
semantics. Independent T1 work may continue.

Additional T1 findings resolved by D2/D3: the separate contributor-key-gated
VibeComfy rating writer is outside the plan's six-action inventory, and Astrid
requires a schema-v2 external pack while received Hivemind is schema-v1 with
an unavailable `50ff509…` default pin. Banodoco's static packaging is
fail-closed and has no existing browser-Supabase seam. These findings are
recorded in the delivery census. D2 (Astra, 2026-09-09) adopts inclusion of
the rating writer in shared-auth closure and makes a retrievable v2-compatible
Hivemind pin an explicit T6 deliverable. D3 (Astra, 2026-09-09) adopts direct
contributor binding to shared `auth.users.id`, rejects a required `members`
admission gate, and releases T2/T3.

Next action: run the declared final integrated review after T8 with the
configured Astra final reviewer, then record the verdict and local delivery
SHAs. Preserve the review limits and all role/stage/budget/counter values in
`run.yaml`.

Key implementation detail: Banodoco's `/connect/` route is now allowlisted in
`deploy/public-files.json` and uses the dependency-free direct PKCE/Auth REST
flow against the pinned Hivemind Supabase project. It preserves request
identity in `sessionStorage` through the fixed callback, shows machine/code,
and requires explicit approval. The exact origin and redirect defaults are
checked in, while live environment validation remains pending authorization.
The current
`issue_contributor_key.py` helper generates/prints legacy key SQL;
`_common.resolve_contributor_key` and `edge_post` are reusable seams, but raw
key printing must not enter the broker. Hivemind's active model is immutable
resources/revisions/message snapshots/evidence; no retired presentation model
is in scope. Product edits and local verification are recorded in
`correction-round-3-evidence.md`; no PR, merge, deployment, or production work
is authorized by the current instruction.

Planning provenance: Luna High performed the source census, reuse exploration,
and artifact drafting through native subagents; the host adjudicated scope and
corrected stale-source assumptions. Delivery preflight confirmed Luna, Sol,
and Astra bindings without substitution. The first review boundary consumed
both configured rounds; the final Astra review remains required. All three
configured oracle calls have been consumed for D1, D2, and D3.
