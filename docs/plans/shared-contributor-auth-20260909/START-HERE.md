# Megado run: shared contributor authentication

This directory is a planning-only Megado project. It is ready to hand to a
delivery coordinator, but it contains no product edits, implementation tests,
deployment, or production-operation preparation.

Read in this order:

1. [northstar.md](northstar.md) — the durable behavior and security boundary.
2. [agent_goal.md](agent_goal.md) — the authorized planning contract and
   proposed delivery scope.
3. [plan.md](plan.md) — decisions, criteria, dependencies, and validation.
4. [tasklist.md](tasklist.md) — executable tasks with routing and proof.
5. [run.yaml](run.yaml) — the single role, model, budget, and review declaration.
6. [status.md](status.md) — current state and restart point.

The future delivery run must first adopt the completed source census and freeze
the clean Hivemind PR4 and Banodoco source identities. The browser host decision
recorded here is Banodoco's static `/connect/` route at
`https://www.banodoco.ai/connect/`, using a pinned Supabase browser SDK and the
shared Supabase session identity. Any verified Discord Supabase user may become
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

The package is selected for publication on `main`. Before implementation, clone the current
`origin/main`, record its actual SHA in the delivery receipt, create a new work
branch from it, and reconcile any differences with the historical source pins
in [provenance.md](provenance.md).

Run mode is deliberately `planning_only`. To execute, a coordinator must
create a new authorized delivery run or receive explicit authorization to
change this run's mode; that change is outside this preparation request.
