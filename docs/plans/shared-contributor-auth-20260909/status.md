# Status — shared contributor authentication

State: **delivery run active; T1 source custody/reconciliation complete; T2/T3
released by D3; implementation in progress**.

Mode: `delivery`. Product changes are being made only in the isolated delivery
worktrees. No product tests, review stages, PR, merge, deployment, or
production operation has occurred.

Estimate: 5–7 focused engineering days, plus 1–2 focused days of integration
contingency for the shared OAuth/broker, static host route, and key migration.

Decision update (2026-09-09): **ADOPTED** the `banodoco-host-findings.md`
recommendation. Banodoco is the browser host: static `/connect/index.html` at
`https://www.banodoco.ai/connect/`, packaged through `deploy/public-files.json`,
with a pinned Supabase browser SDK, shared session identity, `sessionStorage`
request continuity, fixed callback, machine/code display, and explicit approval
POST. Hivemind owns broker identity, key, and race logic; the browser receives
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
branches are recorded in `provenance.md`. Astrid's current auth seam still
pins Hivemind to the old local SHA `50ff509…`, a concrete follow-up for the
later T6 reconciliation after T2/T3.

Tests: **NOT RUN**. Review counters: 0 intermediate, 0 final. Oracle calls: 3
of 3.

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

Next action: implement T2/T3 in the isolated worktrees under D3, preserve the
first review boundary after T3 (schema/broker contract, maximum two rounds),
and preserve the final boundary after T8 (maximum three rounds), as declared
in `run.yaml`.

Key implementation detail: Banodoco currently has no `/connect` route. The
future static page must be allowlisted in `deploy/public-files.json`, use the
pinned browser SDK, preserve request identity in `sessionStorage` through the
fixed callback, show machine/code, and require explicit approval. Declare the
exact Banodoco CORS origin and Supabase redirect allowlist only for a future
authorized environment; live proof is pending that environment. The current
`issue_contributor_key.py` helper generates/prints legacy key SQL;
`_common.resolve_contributor_key` and `edge_post` are reusable seams, but raw
key printing must not enter the broker. Hivemind's active model is immutable
resources/revisions/message snapshots/evidence; no retired presentation model
is in scope. No product edits or product tests have occurred in this delivery
run; no PR, merge, deployment, or production work is authorized by the current
instruction.

Planning provenance: Luna High performed the source census, reuse exploration,
and artifact drafting through native subagents; the host adjudicated scope and
corrected stale-source assumptions. Delivery preflight confirmed Luna, Sol,
and Astra bindings without substitution. These were factual/preflight
assignments, not independent implementation reviews. The configured review
counters remain zero; all three configured oracle calls have been consumed for
D1, D2, and D3.
