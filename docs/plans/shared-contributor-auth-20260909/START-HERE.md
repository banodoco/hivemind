# Megado run: shared contributor authentication

This directory was prepared as a planning-only Megado project. On 2026-09-09
the user explicitly authorized a delivery run; the current run declaration is
now `delivery`, while the original handover remains historical evidence.
Product implementation is currently held at T2/T3 pending the documented
shared-Supabase identity-schema prerequisite.

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

The delivery run preserves the declared roles, review stages, budgets, and
counters. T1 source custody/reconciliation is recorded in
[provenance.md](provenance.md). T2/T3 remain paused until the authoritative
shared-Supabase identity/schema evidence and operator access path are pinned;
the oracle disposition is recorded in the delivery status.
