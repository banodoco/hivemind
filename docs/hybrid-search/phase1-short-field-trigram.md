# Phase 1 — Task 1.5 Bounded Short-Field Trigram Indexes

**Date:** 2026-07-28
**Task:** 1.5 — Add bounded trigram indexes for high-value short fields such as
resource titles and distillation questions.
**Plan:** `docs/architecture/hivemind-hybrid-search-plan.md` (Astrid planning copy).
**Prereqs:** 1.1 frozen lexical contract (§5: ident arm = exact equality + a
`gin_trgm_ops` similarity path `<%`), 1.4 frozen `hivemind_normalize_identifier`
(IMMUTABLE, ICU; `schema/005`), 1.3 proven live online-build / `SET ROLE postgres`
/ session-mode / redaction path, 0.3 inventory (title p50=49/max=92; question
max=81; 2,759 resources / 11 distillations).
**Tests:** `python3 -m unittest tests.test_short_field_trigram`; full suite green.

This task added the two normalized trigram indexes the frozen exact-identifier
arm needs — `idx_external_resources_title_trgm_norm` on
`external_resources.title` and `idx_distillations_question_trgm_norm` on
`distillations.question`, both over the frozen compact
`hivemind_normalize_identifier` / `gin_trgm_ops` — rehearsed them on a
production-shaped isolated cluster, preflighted the live Hivemind project, applied
the `schema/005` prerequisite safely (it was not yet live), validated SQL/Python
parity on live values, built both indexes concurrently, and captured `EXPLAIN`
evidence that representative normalized `<%`/`%` queries use them. It created
**no candidate SQL, no RPC, no full-message index, and no source-row mutation**.

## Completion signal

> Trigram candidate queries are indexed and remain inside the capacity gate.

**Met.** The two normalized trigram indexes are live and **valid**; the title
index is **used at production scale** by every representative normalized
`<%`/`%` candidate query (Bitmap Index Scan on
`idx_external_resources_title_trgm_norm`, never the raw index, never a seq scan);
combined new storage is **632 kB** (592 kB + 40 kB) — negligible inside the 12 GB
gate (db≈2.33 GB, 6.26 GB free on the 9 GB disk). Cross-variant recall is proven
on live data (`Wan2.2`→841 titles, `FLUX.1`→236). See *Findings* for the honest
11-row distillation nuance.

## Files produced (task-1.5 owned)

| Path | Role |
|---|---|
| `schema/006_short_field_trigram.sql` | **Canonical additive/idempotent migration** — schema/005 guard + table/column guards + `CREATE INDEX CONCURRENTLY IF NOT EXISTS` ×2 + verification SELECT + rollback recipe. Elevation-free (runnable as the owner). |
| `scripts/short_field_trigram.py` | **Pure importable module** — frozen index identities (2 targets), build/rollback SQL, preflight query set + verdict logic, evidence queries (`<%`/`%`, natural + forced), EXPLAIN parser, schema/005-applied check, rehearsal schema/seed, frozen constants. |
| `scripts/rehearse_short_field_trigram.py` | **Isolated-cluster rehearsal harness** — throwaway PG 14 cluster, loads `schema/005`, ~2,759 titles / 11 questions, timed online build, sizes, plans, eligibility proof, cancellation/rollback, teardown. |
| `scripts/live_short_field_trigram.py` | **Live driver** — task-0.1 session-mode preflight + apply `schema/005` (if missing) + parity probe + online build (monitored) + evidence + rollback; all output redacted; `--apply` gated on a green preflight. |
| `tests/test_short_field_trigram.py` | **55 tests** — frozen identity, build/rollback SQL, schema-file consistency, preflight verdict, EXPLAIN parsing, security/eligibility/length-bounds, Unicode/cross-variant, capacity, rehearsal+live evidence, redaction, PG-gated idempotence. |
| `docs/hybrid-search/phase1-short-field-trigram-rehearsal.json` | Rehearsal evidence (machine-readable). |
| `docs/hybrid-search/phase1-short-field-trigram-live.json` | Live preflight + schema/005 apply + parity + build + evidence (machine-readable). |
| `docs/hybrid-search/phase1-short-field-trigram.md` | **This report.** |

No file owned by task 1.2 (`schema/003`), 1.3 (`schema/004`), or 1.4
(`schema/005`) was edited; `schema/005` was **applied** live (deployment, not a
source edit) as the task instructed. The dirty working tree and all concurrent
work were preserved.

## Frozen index identities

| Attribute | Title target | Question target |
|---|---|---|
| Schema.table | `public.external_resources` | `public.distillations` |
| Column | `title` (text, not null) | `question` (text, not null) |
| Normalized form | `hivemind_normalize_identifier` (compact) | `hivemind_normalize_identifier` (compact) |
| Op-class | `gin_trgm_ops` | `gin_trgm_ops` |
| Index name | `idx_external_resources_title_trgm_norm` | `idx_distillations_question_trgm_norm` |
| Partial predicate | `char_length(hivemind_normalize_identifier(title)) BETWEEN 1 AND 300` | `status IN ('pending','approved') AND char_length(hivemind_normalize_identifier(question)) BETWEEN 1 AND 300` |
| Eligibility (0.2/§8) | resources: no status column; all rows eligible | distillations: `status IN ('pending','approved')` |

The **compact** form is the frozen choice (task instruction + §5): it collapses
separator/case/Unicode variants (`Wan 2.2` / `Wan2.2` / `wan_2.2` → `wan22`,
`FLUX.1`→`flux1`, `lightx2v_I2V_14B.safetensors`→`lightx2vi2v14bsafetensors`) so a
single trigram similarity passes all of them — the gap FTS cannot bridge
(`to_tsvector('simple','Wan 2.2')` vs `websearch_to_tsquery('simple','Wan2.2')` do
not match; §4). The punctuation-preserving form (`hivemind_normalize_identifier_preserve`)
is the frozen "punctuation-aware trigram path"; it remains an option for task 1.7
and is **not** indexed here. The raw `gin_trgm_ops` indexes from `schema/001`
(`external_resources_title_trgm`, `distillations_question_trgm`) are **retained
additively** — they are un-normalized and serve a different (case/Unicode-
sensitive ILIKE) path; this task adds the normalized form alongside them.

## Exact query rules (frozen, for task 1.7)

The candidate SQL (1.7) consumes these indexes with this **exact** shape
(`scripts/short_field_trigram.py`: `candidate_query_template`):

```sql
SET pg_trgm.word_similarity_threshold = 0.3;     -- frozen WORD_SIMILARITY_THRESHOLD
-- needle <% haystack: the indexed normalized field is the HAYSTACK (right operand)
SELECT id::text AS item_id
  FROM public.external_resources
 WHERE char_length(hivemind_normalize_identifier(title)) BETWEEN 1 AND 300   -- repeat the partial predicate
   AND hivemind_normalize_identifier(:q) <% hivemind_normalize_identifier(title)
 ORDER BY word_similarity(hivemind_normalize_identifier(:q),
                          hivemind_normalize_identifier(title)) DESC NULLS LAST,
          created_at DESC NULLS LAST, id::text ASC                              -- frozen tie-break (§7)
 LIMIT :candidate_limit;
```

| Rule | Value | Why |
|---|---|---|
| **Primary operator** | `<%` (word similarity) | frozen §5. A short identifier (`Wan2.2`→`wan22`) is a *substring* of a longer title; `%` (full similarity) returns LOW for a short needle in a long field, so `<%` is the cross-variant workhorse. |
| Secondary operator | `%` (similarity) | serves near-full-field matches (exact title re-type); structurally index-served (proven). |
| Operator direction | needle `<%` haystack (indexed expr on the right) | the GIN trigram index fires only with the indexed expression as the `<%` haystack — **proven** in rehearsal + live. |
| Thresholds | `pg_trgm.similarity_threshold = 0.3`, `pg_trgm.word_similarity_threshold = 0.3` | pg defaults 0.3/0.6; the word default (0.6) is too strict for SHORT compact identifiers, so both frozen at 0.3. 1.7 sets them per session (deterministic). |
| Rank | `word_similarity(:q, field)` / `similarity(:q, field)` | the matching similarity function for the operator (0..1 lexical_rank for the arm). |
| Tie-break | `lexical_rank DESC NULLS LAST, created_at DESC NULLS LAST, id::text ASC` | frozen §7 (snowflake-safe text id). |
| Empty query | skip the arm (normalize→`''` matches nothing) | protection. |
| Overlong query | skip the arm when `char_length(normalize(:q)) > 300` | `MAX_QUERY_CHARS=300`; a pasted 10 KB blob is neither a meaningful identifier nor cheap to compare. |
| Indexed-value bound | partial predicate `BETWEEN 1 AND 300` | overlong protection + non-empty guard (title max=92 / question max=81 → 300 captures 100% of real data with ~3× margin). |

## Rehearsal — production-shaped, isolated cluster (throwaway PG 14.15)

`initdb --auth=trust --no-locale -E UTF8`, temp data dir, **Unix socket only (no
network)**, torn down after. Loads `schema/005` (the frozen prerequisite), seeds
2,759 resource titles + 11 distillation questions (golden identifiers repeated +
realistic filler; deliberate all-separator and overlong rows to exercise the
bounds), builds the raw `schema/001` trigram indexes first, then the two
normalized indexes concurrently.

| Measurement | Value |
|---|---|
| schema/005 loaded; `hivemind_normalize_identifier` provolatile | **`i` (IMMUTABLE)** on a `C`-locale cluster |
| Variant collapse proof | `Wan 2.2 Image` / `Wan2.2` / `wan_2.2` → `wan22image` \| `wan22` \| `wan22` |
| Seed | 2,759 titles / 11 questions (2 rejected; 392 empty-norm + 214 overlong titles excluded by the bound) |
| Online `CREATE INDEX CONCURRENTLY` ×2 | 0.06 s, both `indisvalid=t` |
| Normalized index sizes (rehearsal, conservative-lower) | title 131.1 KB / question 16.4 KB |
| Baseline (no norm index) | Seq Scan; **cannot** use the raw title index for a normalized query |
| Evidence — `<%` natural (title) | Bitmap Index Scan on `idx_external_resources_title_trgm_norm` |
| Evidence — forced (`enable_seqscan=off`, all targets × ops) | **all** use the normalized index (structural usability incl. the 11-row question table) |
| Cross-variant recall | `Wan2.2`→5, `FLUX.1`→2, `WanVideoSampler`→214 (`<%`) |
| Eligibility | 2 rejected questions match the text but are **excluded** by the status partial predicate (arm returns 3 = pending/approved only) |
| Cancellation/rollback | cancelled CIC → `indisvalid=f`; `DROP CONCURRENTLY` → removed (0 remain); rebuild → valid (`t`) |

Rehearsal verdict: **all_pass = True** (13 checks).

## Live preflight (task-0.1 session-mode access, read-only)

Derived the short-lived CLI-login libpq env from `supabase db dump --dry-run`
(credential in child-process memory only, never printed). **Verdict: GREEN** on
every operational gate.

| Gate | Result |
|---|---|
| target identity | `external_resources.title` + `distillations.question` both `text` ✓ |
| existing trigram indexes | raw `external_resources_title_trgm`, `distillations_question_trgm`, `_answer_trgm`, `_body_trgm` present; normalized targets absent ✓ |
| invalid remnants / in-progress builds | none / none ✓ |
| long/locking transactions / relation locks | none / none ✓ |
| storage headroom | db≈2.33 GB, disk 9 GB, **free≈6.26 GB**, need≈0.0033 GB (idx est≈1.1 MB ×3) ✓ |
| connection mode | **session / pooler port 5432** (the 0.1-validated path; 6543 txn pooler would block) ✓ |
| est rows | 2,734 |
| schema/005 prerequisite | **not yet live** (`normalize_fn=f icu_collation=f`) → applied first by `--apply` |

## Live apply (authorized; additive; `SET ROLE postgres`)

Preflight green → `--apply`: applied `schema/005` (deterministic ICU collation +
IMMUTABLE functions + empty alias table; idempotent; no corpus mutation), ran the
read-only SQL/Python parity probe, then built BOTH indexes concurrently
(`SET ROLE postgres; SET lock_timeout='30s'; SET statement_timeout='1800s'; CREATE
INDEX CONCURRENTLY IF NOT EXISTS …` outside any transaction; progress-monitored;
no source-row mutation).

| Measurement | Value |
|---|---|
| schema/005 apply | rc=0; prerequisite confirmed present |
| **SQL/Python parity (live)** | **16 values checked, 0 mismatches** (re-confirms live ICU matches the Python reference) |
| Build | status=**ok**, elapsed **3.25 s**, rc=0 |
| `idx_external_resources_title_trgm_norm` | valid=`t`, **592 kB** |
| `idx_distillations_question_trgm_norm` | valid=`t`, **40 kB** |

### Live EXPLAIN evidence (representative normalized queries)

The title index is **used at production scale** — all four title candidate queries
run a Bitmap Index Scan on `idx_external_resources_title_trgm_norm`, never the raw
index, never a seq scan:

```
title_wordsim_flux / _wan_variant / _symbol (<%)  -> Bitmap Index Scan on idx_external_resources_title_trgm_norm
title_similarity_full (%)                         -> Bitmap Index Scan on idx_external_resources_title_trgm_norm
```

Representative hit counts (cross-variant recall on **live** data): `Wan2.2`→**841**
titles, `FLUX.1`→**236** titles, `best upscale model`→0 distillations (none exist;
correct).

## Exact live object state (post-build)

| Index | Schema.Table | Expression | valid | partial predicate | size |
|---|---|---|---|---|---|
| `idx_external_resources_title_trgm_norm` | `public.external_resources` | `gin(hivemind_normalize_identifier(title) gin_trgm_ops)` | t | `char_length(...) BETWEEN 1 AND 300` | **592 kB** |
| `idx_distillations_question_trgm_norm` | `public.distillations` | `gin(hivemind_normalize_identifier(question) gin_trgm_ops)` | t | `status IN ('pending','approved') AND char_length(...) BETWEEN 1 AND 300` | **40 kB** |

Combined new storage **632 kB** — comfortably inside the 12 GB capacity envelope
(0.7); the 1.3 `idx_discord_messages_content_fts_simple` (67 MB) and the raw
`schema/001` trigram indexes are untouched.

## Rollback readiness

One block, online, outside a transaction (idempotent — `IF EXISTS` makes re-runs
safe; rehearsed in the cancellation path and available as the live `--rollback`).
Leaves `schema/005` and the raw `schema/001` trigram indexes in place:

```sql
DROP INDEX CONCURRENTLY IF EXISTS public.idx_external_resources_title_trgm_norm;
DROP INDEX CONCURRENTLY IF EXISTS public.idx_distillations_question_trgm_norm;
```

```
python3 scripts/live_short_field_trigram.py --rollback   # SET ROLE postgres; DROP INDEX CONCURRENTLY IF EXISTS … (both)
```

## Capacity result

The two normalized trigram indexes add **632 kB** live (592 kB + 40 kB). Against
the plan's `12 GB` new-storage gate and the 9 GB live disk (6.26 GB free), this is
a **PASS with ~4 orders of magnitude of margin** — these are the corpus's smallest
tables (2,759 resources + 11 distillations; inventory 0.3) and the indexed fields
are short (title max=92, question max=81). The capacity model (0.7) is unaffected;
no gate is approached.

## Security / redaction

- **Eligibility is structural.** The distillation index's partial predicate is the
  frozen `status IN ('pending','approved')` (§8), so a rejected/superseded
  distillation can never surface via this arm even before the RPC eligibility
  layer (proven: 2 rejected questions match the text but are excluded). Resources
  have no status column (all eligible, 0.2 §5).
- **Overlong/empty protection.** Both indexes' partial predicates bound the
  normalized value to `BETWEEN 1 AND 300`; the candidate query short-circuits on
  empty or overlong (>300) normalized queries.
- **Read-only evidence** runs as `SET ROLE postgres` (the tables are externally
  owned; the 1.3-proven path); the 1.7 RPC will instead run as the service role
  with eligibility encoded in SQL.
- Every human-facing line from the live driver + rehearsal harness is routed
  through the task-0.1 `redact()` boundary (connection strings, JWTs, Supabase/
  contributor keys, `PGPASSWORD=…`, 32+ char opaque tokens masked). The CLI-login
  credential lives only in child-process env; no credential or connection string
  is printed, logged, or committed. Parity probe reads sampled titles/questions
  but never prints raw values (only the parity verdict).

## Findings and notes

1. **`<%` (word similarity) is the primary operator, not `%`.** Observed: a short
   identifier needle (`Wan2.2`→`wan22`) against a long title (`wan22image…`)
   yields LOW `%` similarity (~0.1, below 0.3 → 0 hits) because `%` is whole-string.
   `<%` (is the needle a similar *word/substring* of the field?) returns the hit.
   This is exactly why the frozen contract §5 names `<%` for the ident similarity
   path. `%` is retained as a secondary operator for near-full-title matches and
   is structurally index-served (proven). 1.7 should prefer `<%` for identifier
   arms and use `%` only for full-field re-types.
2. **At the current 11-row distillation count the question index is not
   planner-used.** With 11 rows the planner correctly seq-scans (natural) or uses
   `distillations_status_idx` (forced, `enable_seqscan=off`) and evaluates `<%` as
   a filter over 11 rows (Execution Time 0.36–0.41 ms) rather than the trigram
   index. The question index is **valid and eligibility-correct** (proven
   structurally servable in the isolated rehearsal), and becomes planner-relevant
   as distillations grow (the flywheel expects many). This is correct PostgreSQL
   behavior on a tiny table, not a defect; flagged honestly so 1.7/1.11 do not
   assume question-index index-use at the current scale.
3. **Rehearsal sizes are conservative-lower, like 1.3.** Synthetic rehearsal
   titles under-represent live lexeme diversity, so the rehearsal title index
   (131 KB) is smaller than the live one (592 kB). The **live measurement is
   authoritative**; the rehearsal's value is proving mechanics and scale (loads
   `schema/005`, online build, locks, cancellation/rollback, index usage,
   eligibility), not the byte size.
4. **`schema/005` applied live here (first time).** Task 1.4 proved the contract
   only on an isolated cluster; 1.5 is the first task to apply it to the live
   project (idempotent DDL; the `identifier_aliases` table is empty — no aliases
   registered yet, which is why no trigram index on `alias_compact` was added: a
   trigram index on an empty table is pointless now; the btree on `alias_compact`
   from `schema/005` already serves exact equality). SQL/Python parity on live
   values: 0/16 mismatches.
5. **`SET ROLE postgres` elevation** is used for DDL + evidence, identical to 1.3:
   the CLI login role is a *member* of `postgres` (the owner) but the
   `CREATE INDEX` ownership check is not satisfied by inherited privileges
   (`must be owner of table`). The canonical `schema/006` stays elevation-free so
   it is runnable as the owner directly (e.g. from the SQL editor).

## Reproduce

```bash
python3 scripts/live_short_field_trigram.py --preflight        # read-only preflight
python3 scripts/live_short_field_trigram.py --dry-run          # SQL it would run + preflight
python3 scripts/live_short_field_trigram.py --evidence         # capture plans (indexes must exist)
python3 scripts/live_short_field_trigram.py --apply            # apply 005(if needed)+build (preflight green)
python3 scripts/live_short_field_trigram.py --rollback         # drop both
python3 scripts/rehearse_short_field_trigram.py                # isolated rehearsal (~2,759 titles)
python3 -m unittest tests.test_short_field_trigram -v          # this task's tests (55)
python3 -m unittest discover tests/                            # whole repo (821 green)
```

## Gaps / out of scope (per task boundary)

- **No full-message exact-identifier path (1.6).** Message bodies are explicitly
  NOT trigram-indexed here (large field; the raw `external_resources_body_trgm`
  and the `discord_messages` FTS indexes are untouched). The very-long-token
  finding from 1.3 (tokens >2047 chars are not FTS-discoverable) belongs to 1.6.
- **No multi-arm candidate SQL / RPC (1.7).** This task provides the indexes and
  the frozen exact query rules + thresholds + tie-break; it does not implement the
  fts/phrase/ident/code-fragment merge.
- **No trigram index on `identifier_aliases.alias_compact`.** The alias table is
  empty (no aliases registered); a trigram index on it is pointless now. Its
  btree (exact equality) from `schema/005` is sufficient until 1.7/backfill
  populate aliases, at which point a trigram index there can be added.
- **No `preserve`-form trigram index.** The compact form is indexed per the task
  instruction + §5; the punctuation-preserving "punctuation-aware trigram path"
  remains a documented 1.7 option (not needed for the cross-variant gate).

## Boundary

The only live mutations were: (1) the additive, idempotent application of
`schema/005` (collation + IMMUTABLE functions + empty alias table), and (2) the
additive, online creation of the two normalized trigram indexes (plus read-only
verification/EXPLAIN). No source row was changed; no RPC, candidate SQL, Edge
function, message/resource-body index, `schema/003/004/005` source file, or
pgvector/provider path was touched. The dirty Hivemind working tree and all
concurrent task work were preserved.

## Next tasks (dependency-safe — do not start here)

Task 1.5 unblocks the exact-identifier candidate path:

- **1.7** — Lexical candidate SQL. **Must use the identical indexed expression**
  `hivemind_normalize_identifier(<field>)`, repeat each partial predicate, prefer
  `<%` (word similarity) for identifier arms with the frozen 0.3 thresholds, and
  apply the frozen tie-break; merge fts/phrase/ident/code-fragment arms.
- **1.6** — Full-message exact-identifier path (message trigram vs normalized side
  index; the 12 GB gate + 1.3 finding-2 long tokens are inputs).
- **1.10 / 1.11** — SQL-plan tests + the lexical gate (exact-identifier Recall@10
  ≥ 0.95); the question-index planner-use threshold is a function of distillation
  count (finding 2).
