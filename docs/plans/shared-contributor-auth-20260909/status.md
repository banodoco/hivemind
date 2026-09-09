# Status — shared contributor authentication

State: **planning complete; ready for an authorized delivery run**.

Mode: `planning_only`. No implementation, source mutation, worktree,
execution test, review invocation, commit, deployment preparation, or
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

Source census: **complete**. The authoritative delivery source is clean
remote `main` at `e93f7e37bcc49b21b47a715ba6ffc8ce239609f8`,
with remote `main` and `hivemind` agreeing. The original local checkout was
`50ff509240c5582a7335dc71920533b59be7792c`, ahead 3/behind 22 with untracked
`briefing.md` and `handoff-agent-note.txt`; those files are preserved and were not used as product-source evidence.
Reconcile them if relevant before future execution. Astrid's tracked baseline is
`3e1a8c830fcb670b042ea6ce2f8f009ebb2ea91f`; untracked workflow notes/assets
are preserved but not used as product-source evidence.
Banodoco deployed/origin `main` is
`f91eca9c748531957a2740707aee753ba674e17a`; its clean local
`preview/true-union` checkout is `d18eb46557f4d480e040d1a07783edea3b616577`.
Preserve preview and use deployed/origin `main` as the future delivery baseline;
do not overwrite preview. Arca Gidan is `1c1fbadce8868ed3ae806bc8ca28a65ea67a6135`
and remains read-only reference material. The shared Supabase checkout is
read-only identity evidence, not the migration owner. No source was modified or
discarded.

Tests: **NOT RUN**. Review counters: 0 intermediate, 0 final, 0 oracle calls.
No review packet exists because planning mode dispatches no executable review.

Next action: adopt T1's revised frozen source manifest in a newly authorized
delivery run, then implement T2/T3. The intended first review
boundary is after T3 (schema/broker contract, maximum two rounds); the final
integrated boundary is after T8 (maximum three rounds), as declared in
`run.yaml`.

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
is in scope. No product edits, tests, worktrees, commits, deployment, or
production work is authorized in this planning revision.

Planning provenance: Luna High performed the source census, reuse exploration,
and artifact drafting through native subagents; the host adjudicated scope and
corrected stale-source assumptions. These were factual/planning assignments,
not independent implementation reviews. The configured oracle allowance is
for future invoked decisions; no separate oracle model was called in planning.
