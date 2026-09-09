# Hivemind knowledge-model planning handover

This branch contains the existing Hivemind project at the handover base plus a
portable copy of the current planning contract. The authorized delivery run
implemented and validated T1–T10 locally; production cutover, deployment, and
corpus writes remain out of scope.

The delivery run executed T1–T10 and the configured foundation review. Do not
request another implementation approval. The remaining run gate is the final
holistic review. Keep production deployment, database cutover, corpus writes,
and publication outside this handover's scope.

Read in this order:

1. `northstar.md` for the intended end state and anti-patterns.
2. `agent_goal.md` and `status.md` for scope, authorization, counters, and the
   current delivery/review state.
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

The delivery run rechecked source drift and prerequisites, changed `run.yaml`
to `mode: delivery`, and preserved the declared role bindings, stages, budgets,
and counters. Follow the receipts under `.otto/runs/` and the final review
packet for the current certification state. This activation transition was not
a production deployment or database cutover.

The delivery run stores receipts, review packets, and evidence under
`.otto/runs/knowledge-model-plan-20260908-delivery/`. The current status
distinguishes historical planning exploration from executable implementation
evidence.
