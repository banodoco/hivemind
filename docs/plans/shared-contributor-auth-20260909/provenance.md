# Handover provenance — shared contributor authentication

This file records the portable handover contract and the exact source and
skill identities inspected while preparing it. The records copied beside this
file are planning evidence. They do not prove that product implementation,
tests, deployment, or production operations occurred.

## Contract

- Project: Banodoco Hivemind (`https://github.com/banodoco/hivemind.git`).
- Recipient: the next Megado coordinator or agent receiving this package.
- Receiving mode: `planning_only`.
- Destination remote: `origin` — `https://github.com/banodoco/hivemind.git`.
- Base branch: `main`.
- Publication visibility: public; publication is limited to this handover
  branch and its selected documentation artifacts.
- Handover branch: `handover/shared-contributor-auth-20260909`.
- Source baseline: `origin/main` at
  `e93f7e37bcc49b21b47a715ba6ffc8ce239609f8`.
- Current source state: the original local `main` was dirty and divergent
  (`50ff509240c5582a7335dc71920533b59be7792c`, 3 commits ahead and 22 behind
  `origin/main`, with untracked `briefing.md` and
  `handoff-agent-note.txt`). Those files are deliberately excluded from the
  source baseline and preserved in the original checkout.

The handover worktree was created cleanly from the recorded remote baseline.
The branch is a full Hivemind repository checkout, while the handover additions
are documentation-only; no product source was changed or selected as a new
artifact. No push has been performed by the preparer; the root coordinator
must audit the selected paths before any publication.

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

The Hivemind, Banodoco, Arca, and poms-skills pins were reverified against
their public `main` refs on 2026-09-09. Astrid's census pin was reverified as
an existing commit and ancestor of its public `main`; its newer tip is
reported above rather than substituted. The shared Supabase project and its
schema/configuration evidence are runtime dependencies. No fetchable public
repository/ref for that evidence was identified in the planning run, so it
remains an explicitly unverified, local-only prerequisite. Before T2/T3 in a
future delivery, T1 must locate and pin the authoritative Supabase
schema/configuration source or record the concrete operator-owned evidence and
its access path; this package does not silently manufacture that missing
dependency.

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
current plan closure and have been rewritten only to replace machine-local
paths with repository URLs or portable descriptions.

The ignored `.otto` run directory, logs, archives, raw receipts, caches,
environment files, credentials, untracked working-tree notes, and private
operational material are intentionally omitted. The original local dirty files
remain preserved in the original checkout and are excluded from the
authoritative source; this package makes no claim about the contents of those
untracked files. No contributor key, Supabase secret, service-role credential,
or authentication receipt is included.

## Receiving checklist

From a clean clone, inspect the branch and then the immutable plan closure:

```bash
git clone https://github.com/banodoco/hivemind.git hivemind
cd hivemind
git fetch --no-tags origin handover/shared-contributor-auth-20260909
git switch --detach 3487d079bce69780d73f6c5511b9f8bf6f8d55dc
sed -n '1,220p' docs/plans/shared-contributor-auth-20260909/START-HERE.md
# Optional parser check; PyYAML is only a handover-validation prerequisite.
python3 -c 'import yaml; yaml.safe_load(open("docs/plans/shared-contributor-auth-20260909/run.yaml"))'
```

The receiving agent must report readiness from this package, retain
`planning_only`, and preserve the role, stage, budget, and counter values in
`run.yaml`. Implementation, tests as product evidence, PR creation, deployment,
merge, and cutover need later explicit authorization.
