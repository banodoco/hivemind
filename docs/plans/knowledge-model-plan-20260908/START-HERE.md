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

Before implementation, recheck source drift against this branch, inspect the
current local tool prerequisites, and preserve any unrelated dirty work. The
recipient should make the Megado skill available using the public skill source
provided with the handover, then follow `run.yaml` and execute T1–T10. This
handover does not add a new gate.

The original local receipts, raw logs, exploratory briefs, and archives were
deliberately not copied. They are local process history, not implementation
proof. The current status distinguishes historical planning exploration from
future implementation counters and evidence.
