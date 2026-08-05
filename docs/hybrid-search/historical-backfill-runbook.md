# Historical remediation and embedding backfill

These commands are operator-only and default to an audit. They require a
direct/session PostgreSQL URL (ordinary port 5432/session mode); transaction
pooler URLs and transaction-mode settings are refused before any work starts.
Use an environment variable or approved local env-file mechanism for secrets;
never put a URL or provider key in shell history, a report, or this repository.

Audit the workflow representation population first:

```text
python3 scripts/backfill_workflow_representations.py \
  --database-url "$HIVEMIND_DB_URL" --vibecomfy-root "$VIBECOMFY_ROOT" \
  --report-path /secure/operator/workflow-audit.json
```

Then audit the embedding population. The embedding command runs workflow
remediation first in either mode; dry-run does not read or require a provider
key.

```text
python3 scripts/backfill_embeddings.py \
  --database-url "$HIVEMIND_DB_URL" --contract-id "$HIVEMIND_CONTRACT_ID" \
  --source approved_distillations --source pending_distillations --source resources \
  --vibecomfy-root "$VIBECOMFY_ROOT" --report-path /secure/operator/embed-audit.json
```

After reviewing the audit, apply deliberately. The approved env file is read
only for `OPENAI_API_KEY`; it is never printed or written to the report.

```text
python3 scripts/backfill_embeddings.py --apply \
  --database-url "$HIVEMIND_DB_URL" --contract-id "$HIVEMIND_CONTRACT_ID" \
  --source approved_distillations --source pending_distillations --source resources \
  --vibecomfy-root "$VIBECOMFY_ROOT" --provider-env-file /secure/operator/approved.env \
  --batch-items 100 --input-items 64 --max-tokens 8192 --rate-limit 60 \
  --report-path /secure/operator/embed-apply.json
```

On an interruption, retain the reported `run_id` and resume with the same
source/date/contract choices:

```text
python3 scripts/backfill_embeddings.py --apply --resume-run-id RUN_ID \
  --database-url "$HIVEMIND_DB_URL" --contract-id "$HIVEMIND_CONTRACT_ID" \
  --source resources --vibecomfy-root "$VIBECOMFY_ROOT"
```

The workflow-remediation command has its own durable run id. In `--apply`
mode it persists its high-water mark, cursor, aggregate counters, and opaque
retryable row-failure reason before an embedding run is allowed to consume the
reconciled source. Resume it with `--resume-run-id` using the same direct
session and authorized VibeComfy root. Source identity and native artifacts
are preserved; only the canonical payload/body/metadata projection changes.

`content_embeddings` is fixed at `vector(384)`. The backfill rejects any
other `--dimension`, a missing/incompatible contract, or a provider/model
mismatch before a provider request or vector write; it never activates a
contract. `--max-tokens` and `--max-chars` are request batching bounds, not
text truncation controls: a canonical chunk that exceeds a batch bound is sent
intact on its own, so stored and provider text hashes always describe identical
bytes.

Every representation is replaced atomically only after all of its chunks have
been embedded successfully; an item cursor advances only after all of its
applicable prose/Python representations are durable. Freshness compares the
entire stored chunk identity/hash set, so stale tail chunks trigger a complete
replacement. Counters are item-level and checkpointed with each cursor:
eligible, processed, skipped-current, quarantined, unavailable, and failed.

Reports contain only status, opaque IDs/cursors, reason categories, counts,
and elapsed time. `skipped_current` means the stored representation hash is
already current. `quarantined_python_excluded` and
`representation_unavailable` retain safe workflow prose while excluding Python
from public lexical/semantic material. A remediation report’s `failed` count
is retryable; resolve its non-secret reason category before continuing.

`--vibecomfy-root` is required whenever a local `corpus_path` must be read.
The adapter rejects traversal and paths outside that explicit root. It invokes
VibeComfy’s existing parser/converter and never executes stored Python.

For a local-only rehearsal, run:

```text
python3 scripts/rehearse_historical_backfill.py
```

It creates an isolated cluster and is safe to rerun. Do not point either
command at production during development, deploy an Edge Function, activate an
embedding contract, apply remote migrations, or build/enable HNSW as part of
this task.
