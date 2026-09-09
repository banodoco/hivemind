# Handover provenance — shared contributor authentication

This file records the portable handover contract and the exact source and
skill identities inspected while preparing it. The records copied beside this
file are planning evidence. They do not prove that product implementation,
tests, deployment, or production operations occurred.

## Contract

- Project: Banodoco Hivemind (`https://github.com/banodoco/hivemind.git`).
- Recipient: the next Megado coordinator or agent receiving this package.
- Receiving mode: `delivery` (explicitly updated by the user on 2026-09-09).
- Destination remote: `origin` — `https://github.com/banodoco/hivemind.git`.
- Base branch: `main`.
- Publication target: none in this run. The implementation is retained in
  isolated local worktrees; PR creation, merge, deployment, and cutover remain
  outside the current authorization.
- Historical source baseline: `origin/main` at
  `e93f7e37bcc49b21b47a715ba6ffc8ce239609f8`.
- Recipient source record: start from the current `origin/main`, record its
  actual SHA in the delivery receipt, and reconcile differences against the
  historical baseline before implementation.
- Current source state: the original local `main` was dirty and divergent
  (`50ff509240c5582a7335dc71920533b59be7792c`, 3 commits ahead and 22 behind
  `origin/main`, with untracked `briefing.md` and
  `handoff-agent-note.txt`). Those files are deliberately excluded from the
  source baseline and preserved in the original checkout.

The package was assembled from clean current-main checkouts based on the
recorded remote baselines. Product implementation is isolated to the delivery
worktrees; the original checkouts remain untouched.

## Delivery source receipt — 2026-09-09

The source was re-cloned into fresh directories and the original checkouts, if
any, were not overwritten. Product mutation is isolated to the worktrees below.
PR creation, merge, deployment, cutover, and production operations remain
outside the current authorization.

| Component | Received `origin/main` SHA | Delivery worktree/branch | Reconciliation against historical pin |
| --- | --- | --- | --- |
| Hivemind | `9253c10cc2ddb6a12e4afeb41d10d94b83bf50f6` | `.otto/worktrees/shared-contributor-auth-20260909-delivery/hivemind` / `otto/shared-contributor-auth-20260909` | Historical `e93f7e37…` was reconciled before implementation. Correction-round implementation commit `0b061df1a46bf935790106ef62aca98cf2b8bba5` contains the Hivemind schema, broker, CLI, rating-writer closure, rehearsal, and v2 pack updates. |
| Banodoco website | `f91eca9c748531957a2740707aee753ba674e17a` | `.otto/worktrees/shared-contributor-auth-20260909-delivery/banodoco-website` / `otto/shared-contributor-auth-20260909` | Matches the historical deployed/origin `main` pin. The separate preview evidence `d18eb465…` remains preserved and is not overwritten. |
| Astrid | `8150c3b70887495f0fae4a55c1ac70085a900550` | `.otto/worktrees/shared-contributor-auth-20260909-delivery/astrid` / `otto/shared-contributor-auth-20260909` | Historical `3e1a8c83…` is an ancestor. Current `main` adds substantial unrelated runtime/docs work; the delivery seam pins Hivemind to `0b061df1a46bf935790106ef62aca98cf2b8bba5` and requires remote publication before remote installation. |
| Arca Gidan | `1c1fbadce8868ed3ae806bc8ca28a65ea67a6135` | read-only checkout | Matches the historical reference pin; no product changes are planned. |
| brain-of-bndc | `bcf8d14964113743ccbbaab793dab1a410469990` | read-only checkout | Its planning record identifies the sibling `banodoco-workspace` repository as the authoritative Supabase migration root. |
| banodoco-workspace | `eedae0b1c90d00b9d7c61ddc64f1cc690d2947cb` | read-only checkout | Authoritative shared identity evidence: `members.auth_user_id`, Discord resolver/trigger/backfill, and separate `public.admins` authority. No changes are planned in this repository. |
| poms-skills | `ef42515942adfb1683cde4b7b2d53d4e56dbe25e` | detached read-only checkout | Matches the required Megado skill snapshot. |

The current source census now includes the authoritative shared Supabase
identity migrations. `brain-of-bndc` explicitly points to the sibling
`banodoco-workspace/supabase/migrations/` root; the workspace checkout pins the
`members.auth_user_id` foreign key/partial uniqueness, Discord identity
resolver and guarded trigger/backfill, and the separate `public.admins`
authority. D1's original evidence gap is superseded by D3. The implementation
uses direct Hivemind contributor binding to shared `auth.users(id)`; a mapped
`members` row is not an admission gate. Arca remains client-side reference
material only and receives no product changes.

Additional current-main reconciliation findings are recorded in the delivery
census. The separate contributor-key-gated
`supabase/functions/submit-vibecomfy-rating/index.ts:261-333` writer was not
one of the six knowledge actions named in the plan; D2 resolved it into the
shared-auth closure. Astrid's current managed-pack admission required schema
v2, so the delivery raises Hivemind's pack contract and pins it to the local
delivery commit. Banodoco's fail-closed `deploy/public-files.json` and
`tools/package-public.py:121-150` required the complete `/connect/` asset
closure, which is now covered by the static page, PKCE flow, and packaging
checks.

Final local delivery receipt (before any remote publication): correction-round
implementation commit `0b061df1a46bf935790106ef62aca98cf2b8bba5` is consumed by
Astrid's local pin. The follow-up documentation receipt commit does not alter
the pinned product code. Banodoco and Astrid remain unpublished delivery
worktrees until a separate authorization.

## Source and dependency pins

All entries below have fetchable public URLs. The source census intentionally
retains the exact refs used by the plan even where a dependency's current
`main` has advanced; do not silently upgrade the plan's evidence.

| Component | Fetchable URL | Exact inspected commit | Use in this plan |
| --- | --- | --- | --- |
| Hivemind | `https://github.com/banodoco/hivemind.git` | `e93f7e37bcc49b21b47a715ba6ffc8ce239609f8` | Delivery baseline; remote `main` and `hivemind` agreed at census time. |
| Astrid | `https://github.com/peteromallet/Astrid.git` | `3e1a8c830fcb670b042ea6ce2f8f009ebb2ea91f` | Read-only pack/source census. This commit is an ancestor of the inspected current remote `main` (`c2c9bb2ab452b9c813b435278715dffa42e517eb`); the plan remains pinned to the census commit. |
| Banodoco website | `https://github.com/banodoco/Banodoco-website.git` | `f91eca9c748531957a2740707aee753ba674e17a` | Deployed/origin `main` baseline for future delivery. Preserved preview evidence is `d18eb46557f4d480e040d1a07783edea3b616577`. |
| Arca Gidan | `https://github.com/banodoco/arca-gidan.git` | `1c1fbadce8868ed3ae806bc8ca28a65ea67a6135` | Read-only identity/auth reference; no product changes. |
| poms-skills | `https://github.com/peteromallet/poms-skills.git` | `ef42515942adfb1683cde4b7b2d53d4e56dbe25e` | Skill source inspected for this handover and canonical Megado instructions. |

The Hivemind, Banodoco, Arca, brain-of-bndc, banodoco-workspace, and poms-skills pins were reverified against
their public `main` refs on 2026-09-09. Astrid's census pin was reverified as
an existing commit and ancestor of its public `main`; its newer tip is
reported above rather than substituted. The shared Supabase project and its
schema/configuration evidence are runtime dependencies. The authoritative
public source is now pinned to `banodoco-workspace` `eedae0b1…`;
`brain-of-bndc` `bcf8d149…` provides the cross-repository provenance. D3
releases T2/T3 with direct `auth.users(id)` binding and no required member-row
admission.

## Skill provenance and dependency install

The inspected files came from poms-skills commit
`ef42515942adfb1683cde4b7b2d53d4e56dbe25e`:

- `megado-handover/SKILL.md`, blob
  `3355711b782b6fc78ab2e0ca11086a8168667a34`.
- `megado/SKILL.md`, blob
  `b3a77e556b965ef274913b9736eb460b86495683`.

Install the pinned skills without overwriting an existing checkout:

```bash
git clone https://github.com/peteromallet/poms-skills.git poms-skills
git -C poms-skills fetch --no-tags origin ef42515942adfb1683cde4b7b2d53d4e56dbe25e
git -C poms-skills worktree add --detach ../poms-skills-ef425159 ef42515942adfb1683cde4b7b2d53d4e56dbe25e
```

If cloning is unavailable, directly read the pinned files:

```text
https://raw.githubusercontent.com/peteromallet/poms-skills/ef42515942adfb1683cde4b7b2d53d4e56dbe25e/megado-handover/SKILL.md
https://raw.githubusercontent.com/peteromallet/poms-skills/ef42515942adfb1683cde4b7b2d53d4e56dbe25e/megado/SKILL.md
```

The native agent capability required by the run is the normal Megado worker
and reviewer route. The declared model bindings are retained exactly in
`run.yaml`: Luna for coordinator/normal worker/normal reviewer, Sol for the
XHARD slots, Astra for oracle/final reviewer. Availability of each model and
native capability is a recipient prerequisite; this handover does not silently
substitute a model. The preparer found the native Codex tooling available and
used the configured Luna planning/factual-audit agents; no product review or
oracle call was made. Live availability of every configured Luna, Sol, and
Astra binding was not independently certified, so the recipient must verify
that prerequisite and report any missing model rather than substitute silently.

## Included and omitted records

Included under this directory are `START-HERE.md`, `northstar.md`,
`agent_goal.md`, `plan.md`, `tasklist.md`, `run.yaml`, `status.md`, and the
adopted `banodoco-host-findings.md`, plus this provenance record. They form the
current planning package and have been rewritten only to replace machine-local
paths with repository URLs or portable descriptions.

The ignored `.otto` run directory, logs, archives, raw receipts, caches,
environment files, credentials, untracked working-tree notes, and private
operational material are intentionally omitted. The original local dirty files
remain preserved in the original checkout and are excluded from the
authoritative source; this package makes no claim about the contents of those
untracked files. No contributor key, Supabase secret, service-role credential,
or authentication receipt is included.

## Receiving checklist

From a clean clone, inspect current `main` and create a work branch:

```bash
git clone https://github.com/banodoco/hivemind.git hivemind
cd hivemind
git switch --create work/shared-contributor-auth-20260909 origin/main
git rev-parse origin/main
sed -n '1,220p' docs/plans/shared-contributor-auth-20260909/START-HERE.md
# Optional parser check; PyYAML is only a handover-validation prerequisite.
python3 -c 'import yaml; yaml.safe_load(open("docs/plans/shared-contributor-auth-20260909/run.yaml"))'
```

Record the `origin/main` SHA, compare it with the historical source pin above,
and reconcile any relevant differences before implementation.

The receiving agent must report readiness from this package, retain
`planning_only`, and preserve the role, stage, budget, and counter values in
`run.yaml`. Implementation, tests as product evidence, PR creation, deployment,
merge, and cutover need later explicit authorization.
