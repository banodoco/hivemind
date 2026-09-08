# Portable review and evidence contract

This planning handover uses the review stages and criteria in `run.yaml` and
`implementation-criteria.md`. It does not create additional review stages or
authorize implementation.

For each scheduled review, preserve a frozen candidate/source identity, the
North Star and goal, the exact task and criterion scope, relevant diffs, and
test or rehearsal receipts. Map each criterion to expected behavior, command,
result, and evidence path. Missing evidence remains `MISSING`; inaccessible or
stale artifacts cannot produce `PASS`.

Completion review checks actual implementation and executable proof. The final
review also checks integration, simplicity, and alignment with the North Star.
Correction rounds cover affected criteria and dependencies only. Reviewers are
independent leaves and do not widen scope or invent requirements. Historical
planning exploration and model recommendations are context, not implementation
evidence.

Required delivery distinction: source availability, installed/runtime readiness,
and remote service availability must be reported separately. A skill link,
successful harness start, or planning receipt is not proof that the Hivemind
product behavior works. Live search/get-item validation remains read-only.
