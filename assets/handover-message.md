# Megado handover — shared contributor authentication

This Banodoco Hivemind planning package is prepared for publication on the
public `main` branch:

```text
Project:  https://github.com/banodoco/hivemind.git
Branch:   main
Historical source pin: e93f7e37bcc49b21b47a715ba6ffc8ce239609f8
Mode:     planning_only
```

The selected publication target is `main`, with documentation-only changes.
The historical Hivemind
source pin above is the exact baseline inspected during planning, not a
detached-checkout target. On receipt, start from the current `origin/main`,
record its actual SHA in the delivery receipt, and reconcile any source
differences against the historical pin before implementation. No
implementation branch, deployment, or cutover is authorized by this handover.

The objective is to prepare a future shared login path for Hivemind
contributors while preserving anonymous public reads, current contribution
publication semantics, and separate editor authority. The selected browser
host is Banodoco's static `https://www.banodoco.ai/connect/` route, using a
pinned Supabase browser SDK and the shared Supabase session identity. Hivemind
owns identity mapping, broker state, key issuance, and race handling. Arca
Gidan is read-only auth/identity reference material. The browser must never
receive a contributor key or service secret; no raw key or polling secret may
enter URLs, logs, or persisted non-secret metadata.

The scope is the future T1–T8 plan in
`docs/plans/shared-contributor-auth-20260909/`: identity/key schema and
one-transaction legacy migration; an expiring one-use approval broker; the
Banodoco packaged route and callback/session continuity; CLI
login/status/local logout/server revoke; shared resolver wiring for write
surfaces; Astrid pack/docs reuse; and disposable plus focused validation. It
does not include a read login wall, retrieval redesign, general IAM, a second
publishing backend, native device grants, Arca product changes, production
migration, deployment, or operational execution.

The authoritative state is `planning_only` in
`docs/plans/shared-contributor-auth-20260909/run.yaml`. Preserve all role
bindings, stage boundaries, budgets, and counters exactly:

- coordinator: `gpt-5.6-luna` / medium
- normal worker and normal reviewer: `gpt-5.6-luna` / high and medium
- XHARD worker and reviewer: `gpt-5.6-sol` / high
- oracle and final reviewer: `gpt-6-astra` / medium
- oracle allowance: 3 calls
- review stages: `schema_broker_contract` after T3, completion lens, max 2;
  `final_integrated` after T8, completion-and-strategy lens, max 3
- review counters at handover: 0 intermediate, 0 final, 0 oracle

Source and dependency provenance is pinned in
`docs/plans/shared-contributor-auth-20260909/provenance.md`. The exact
inspected refs are Hivemind `e93f7e37bcc49b21b47a715ba6ffc8ce239609f8`,
Astrid `3e1a8c830fcb670b042ea6ce2f8f009ebb2ea91f`, Banodoco website
`f91eca9c748531957a2740707aee753ba674e17a`, Arca Gidan
`1c1fbadce8868ed3ae806bc8ca28a65ea67a6135`, and poms-skills
`ef42515942adfb1683cde4b7b2d53d4e56dbe25e`. Astrid's current remote tip is
newer (`c2c9bb2ab452b9c813b435278715dffa42e517eb`); the plan remains pinned to
the inspected ancestor and is not silently upgraded. The original dirty local
checkout and its untracked notes are excluded. The additions are
documentation-only and no product source was changed.

The shared Supabase project and schema/configuration evidence are runtime
dependencies. No fetchable public repository/ref for that evidence was
identified in planning, so it remains an explicitly unverified local-only
prerequisite. Before T2/T3, the future T1 census must locate and pin the
authoritative source or record concrete operator-owned evidence and its access
path.

The Megado skill files were read from poms-skills commit
`ef42515942adfb1683cde4b7b2d53d4e56dbe25e`:

- `megado-handover/SKILL.md` blob
  `3355711b782b6fc78ab2e0ca11086a8168667a34`
- `megado/SKILL.md` blob
  `b3a77e556b965ef274913b9736eb460b86495683`

Install that skills snapshot without overwriting existing files:

```bash
git clone https://github.com/peteromallet/poms-skills.git poms-skills
git -C poms-skills fetch --no-tags origin ef42515942adfb1683cde4b7b2d53d4e56dbe25e
git -C poms-skills worktree add --detach ../poms-skills-ef425159 ef42515942adfb1683cde4b7b2d53d4e56dbe25e
```

Direct-read fallback:

```text
https://raw.githubusercontent.com/peteromallet/poms-skills/ef42515942adfb1683cde4b7b2d53d4e56dbe25e/megado-handover/SKILL.md
https://raw.githubusercontent.com/peteromallet/poms-skills/ef42515942adfb1683cde4b7b2d53d4e56dbe25e/megado/SKILL.md
```

Start from the current `main`, record the source you received, and create a
new work branch before implementation:

```bash
git clone https://github.com/banodoco/hivemind.git hivemind
cd hivemind
git switch --create work/shared-contributor-auth-20260909 origin/main
git rev-parse origin/main
sed -n '1,220p' docs/plans/shared-contributor-auth-20260909/START-HERE.md
# Optional parser check; PyYAML is only a handover-validation prerequisite.
python3 -c 'import yaml; yaml.safe_load(open("docs/plans/shared-contributor-auth-20260909/run.yaml"))'
```

Record the `origin/main` SHA, compare it with the historical Hivemind pin in
this message and `provenance.md`, and resolve any relevant differences before
freezing T1. Keep the exact historical source and skill pins as evidence.

The preparer found native Codex tooling and used the configured Luna
planning/factual-audit agents; no product review or oracle call was made. Live
availability of every configured Luna, Sol, and Astra binding was not
independently certified. Before future delivery, verify that prerequisite and
report any missing capability or model; do not substitute silently. The shared
Supabase project was not mutated here. Live OAuth, CORS, redirect allowlist,
database migration, tests as product evidence, and deployment remain pending
an authorized delivery environment.

Selected finish instruction: keep `planning_only`, inspect the package from
current `main`, record the received main SHA, reconcile it with the historical
pins, and report readiness, prerequisites, and any concrete contradiction. A
later explicit authorization may change the single `run.yaml` mode to delivery
while preserving roles, stages, budgets, counters, and boundaries; only then
may the recipient implement and validate the plan. A PR, merge, deploy, or
cutover requires separate explicit authorization.
