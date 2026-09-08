# Canonical Hivemind CLI handover

This branch packages the Hivemind-owned CLI consolidation from base
`abe41fdf72df3bbcfe45087eae64ccf50a1bb809`. It is a handover branch only:
H3 remains planned, with no merge, deploy, global install, personal-launcher
cutover, or corpus write authorized by this branch.

## Completed evidence

- H1 implementation validation: 181 focused upstream tests passed after the
  CLI changes, including CLI routing, search delegation, date bounds, JSON and
  stderr behavior, keyset pagination, get-item IDs, and executor regressions.
- H2 independent overlapping review: 172 upstream tests passed before the CLI
  changes. That count remains historical review evidence and is not a new run
  counter.
- The packaged `hivemind` console entrypoint owns the full human CLI surface;
  `hivemind-search` remains a compatibility entrypoint over the canonical raw
  search path.

## Remaining H3

1. Finish the repo-owned thin CLI executor boundary: accept a structured argv
   list, invoke Hivemind without a shell, and preserve stdout, stderr, exit
   code, timeout, and cancellation semantics.
2. Reconcile the v2 pack and executor manifest contract in Hivemind-owned
   manifests and tests. Astrid owns validated project scope metadata; this
   branch does not invent a `project_optional` field.
3. Port and test the narrow `get_item` message-feed lookup and string-safe
   snowflake IDs in the Hivemind implementation where required.
4. Keep public and secret environment inputs explicit: the anon key is a
   baked-in publishable read key and is not a credential; contributor key
   handling remains secret-required. Astrid owns runtime/source installation
   and passes only declared environment inputs.

Astrid A1–A3 and V1 remain in the original Astrid conversation and checkout;
they are outside this branch. The branch records the stable argv/output/exit
boundary for that future integration but does not perform it.

## Scope audit

Only the six selected implementation files plus this handover document are
added or changed relative to the exact base. The incoming base commit changes
seven executor manifests to wire bounded inputs/routes; it contains no private
credential or local path detected by the handover audit. The selected diff has
no private credential or local path. The public anon key is intentionally
committed and documented as publishable; the contributor key is read from
runtime secret configuration.
