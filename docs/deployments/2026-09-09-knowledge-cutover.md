# Knowledge model production cutover receipt

Date: 2026-09-09  
Project: `ujlwuvkrxlvoswwkerdf`  
Change: merged Hivemind PR4 knowledge model and delivery path

## Database

The operator wrapper applied the additive prerequisites in this order:

`020`, `021`, `022`, `023`, `024`, `025`, `026`, `027`, `029`, `030`, `031`, `032`, `033`, `034`.

Migration `028` was intentionally skipped because the embedding worker URL/key and cron contract are not configured. Migrations `038` and `039`, followed by the legacy conversion, ran in one locked transaction. The wrapper uses `SET ROLE postgres` and `SET search_path = public, extensions`; the latter is required because the production `pgcrypto` extension is installed in `extensions`.

The committed cutover assertions and post-cutover read-only query reported:

- 4,285 resources and 4,285 resource revisions
- 33 knowledge references
- 11 pending knowledge revisions
- resource revision kinds: 4,272 workflows, 1 transcript, 1 `n` resource
- no legacy active relations (`external_resources`, `distillations`, `distillation_cites`, `unified_feed`, or legacy lexical surfaces)
- A final read-only check saw 8,557 pending embedding jobs while live message writers continued to enqueue work; no worker was running. The queue is intentionally idle.
- zero active embedding contracts and zero stored content embeddings; worker/backfill remains intentionally idle

The pre-cutover schema backup and affected-data custom dump are retained privately under `.otto/deployments/20260909/` and are excluded from Git. Their checksums are recorded in the private operator log; the files preserve ACLs and grants.

## Edge functions and smoke checks

- Deployed the matching `contribute` function successfully; it is active at the new deployment version.
- Retired the stale `contribute-resource` function after the database cutover and new endpoint deployment.
- Public resource search returned workflow results through the new `resources`/revision path.
- Current and pinned resource retrieval passed through `get_item`.
- An unauthenticated POST to `contribute` returned HTTP 401.
- A rollback-only privileged RPC smoke exercised submit, approve, propose, approve, and evidence submission with an exact revision pin; it passed and left zero evidence rows and contributor 1 not editor.

No embedding worker or cron schedule was deployed. The queue is therefore expected to remain pending until a separately authorized worker rollout and contract activation.
