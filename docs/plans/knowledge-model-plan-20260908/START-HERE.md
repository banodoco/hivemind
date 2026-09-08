# Hivemind knowledge-model planning handover

This branch contains the existing Hivemind project at the handover base plus a
portable copy of the current planning contract. The plan is planning-only:
implementation, tests, migrations, production cutover, deployment, and corpus
writes have not started.

The handover recipient is explicitly instructed to execute the planned T1–T10
implementation and the configured review stages after pulling this branch. Do
not request a redundant implementation approval. Keep production deployment,
database cutover, corpus writes, and publication outside this handover's scope.

Read in this order:

1. `northstar.md` for the intended end state and anti-patterns.
2. `agent_goal.md` and `status.md` for scope, authorization, counters, and the
   current NOT_RUN state.
3. `plan.md`, `tasklist.md`, and `implementation-criteria.md` for the product
   contract and acceptance proof.
4. `run.yaml` for the declared roles and review budgets. Normal implementation
   workers are GPT-5.6 Luna at high reasoning; justified XHARD work and oracle
   judgment use GPT-5.6 Sol; the final reviewer is GPT-6 Astra. These bindings
   do not authorize implementation by themselves.
5. `review-contract.md` for the portable evidence and packet requirements.

Clone and enter the branch with:

```bash
git clone --branch hivemind https://github.com/banodoco/hivemind.git hivemind
cd hivemind
```

Make the public Megado skill available before coordinating the run:

```bash
git clone https://github.com/peteromallet/poms-skills.git ~/.local/share/poms-skills
mkdir -p ~/.codex/skills
test -e ~/.codex/skills/megado || ln -s ~/.local/share/poms-skills/megado ~/.codex/skills/megado
```

If the skill checkout already exists, update it with
`git -C ~/.local/share/poms-skills pull --ff-only`. The symlink command is
deliberately no-overwrite: an existing user-managed skill is left untouched.
Agents without Codex should read
`~/.local/share/poms-skills/megado/SKILL.md` directly and follow that skill's
role, custody, review, and evidence instructions.

Before implementation, recheck source drift against this branch, inspect the
current local tool prerequisites, and preserve any unrelated dirty work. Before
executing T1, change `run.yaml` from `mode: planning_only` to the accepted
execution mode `mode: delivery`, preserving all role bindings, review stages,
budgets, and counters. Then follow `run.yaml` and execute T1–T10. This is the
activation transition for the explicit recipient instruction, not a new gate.

The original local receipts, raw logs, exploratory briefs, and archives were
deliberately not copied. They are local process history, not implementation
proof. The current status distinguishes historical planning exploration from
future implementation counters and evidence.
