# Megado run: shared contributor authentication

This directory began as a planning-only Megado project. On 2026-09-09 the user
explicitly authorized a delivery run; the current run declaration is
`delivery`, and the original handover remains historical evidence. The shared
Supabase identity-schema prerequisite was resolved from the read-only
`banodoco-workspace` migrations and the implementation is retained in the
isolated delivery worktrees.

Read in this order:

1. [northstar.md](northstar.md) — the durable behavior and security boundary.
2. [agent_goal.md](agent_goal.md) — the authorized planning contract and
   proposed delivery scope.
3. [plan.md](plan.md) — decisions, criteria, dependencies, and validation.
4. [tasklist.md](tasklist.md) — executable tasks with routing and proof.
5. [run.yaml](run.yaml) — the single role, model, budget, and review declaration.
6. [status.md](status.md) — current state and restart point.

The delivery run adopted the completed source census and froze the clean
current-main source identities. The browser host decision recorded here is
Banodoco's static `/connect/` route at
`https://www.banodoco.ai/connect/`, using the pinned direct Supabase Auth
REST/PKCE browser flow and the shared Supabase session identity. Any verified Discord Supabase user may become
a normal contributor; authentication never grants editor approval authority,
and no additional membership gate is invented without source evidence.

The Banodoco host finding is adopted as of 2026-09-09. The future route must be
packaged through `deploy/public-files.json`, preserve the request in
`sessionStorage` through the fixed callback, display the requesting machine and
code after login, and require an explicit approval POST. Hivemind retains all
broker, identity, key, and race authority; the browser receives no contributor
key or service secret. Arca Gidan is read-only identity/auth reference material
for this plan and receives no product changes. The existing Hivemind GitHub CTA
is retained; CLI-generated URLs are sufficient.

The package is retained as an unpublished local delivery candidate. The current
`origin/main` SHAs, delivery branches, and reconciliations are recorded in
[provenance.md](provenance.md); no PR, merge, deployment, or cutover is part of
this run.

The delivery run preserves the declared roles, review stages, budgets, and
counters. T1 source custody/reconciliation and the T2/T3 oracle decisions are
recorded in [provenance.md](provenance.md) and the delivery status. The final
Astrid review remains the next gate after T8.
