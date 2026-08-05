# Phase 0 — Task 0.7 Capacity & Cost Model

**Date:** 2026-07-28
**Task:** 0.7 — Model storage, HNSW memory, provider spend, Edge invocations, and
database compute for 384- and 1536-dimensional candidates.
**Plan:** `docs/architecture/hivemind-hybrid-search-plan.md` (Astrid planning copy)
**Prereqs:** 0.1 access (`phase0-access-audit.md`), 0.2 schema/eligibility
(`phase0-schema-eligibility-map.md`), 0.3 inventory
(`phase0-inventory.{md,json}`).
**Reproduce:** `python3 scripts/capacity_model.py` (human summary);
`python3 scripts/capacity_model.py --results docs/hybrid-search/phase0-capacity-results.json
--assumptions docs/hybrid-search/phase0-capacity-assumptions.json`.
**Tests:** `python3 -m unittest tests.test_capacity_model`.
**Artifacts:** `phase0-capacity-results.json`, `phase0-capacity-assumptions.json`.

This report records **measured corpus facts, dated vendor list prices with
citations, explicit model heuristics with ranges, and pass/fail/conditional
verdicts.** It is modeling only: it reads no source content, calls no provider,
enables no pgvector, creates no index, and starts no backfill. It does **not**
choose the production embedding/chunk contract (task 2.14).

---

## Headline

**384-d is the capacity-viable full-corpus candidate; 1536-d is not.**

| Scenario | `$25` spend | `12 GB` new storage | `$50/mo` recurring |
|---|:-:|:-:|:-:|
| **Pilot, 384-d** | PASS | PASS (0.33 GB) | PASS |
| **Pilot, 1536-d** | PASS | PASS (0.71 GB) | PASS |
| **Full corpus, 384-d** | PASS | **PASS (4.59 GB)** | **CONDITIONAL** |
| **Full corpus, 1536-d** | PASS | **FAIL (16.4 GB)** | CONDITIONAL (storage-blocked) |

- **Initial spend (`$25`)** is a non-constraint at both dimensions. Full-corpus
  embedding cost is **~$0.84** on `text-embedding-3-small` (≈$5.44 on the
  `3-large` quality alternative) — two orders of magnitude under the gate.
- **Storage (`12 GB`)** is the dimension-deciding gate. 384-d full corpus adds
  **~4.6 GB** of vector-table + HNSW (≈2.7× margin under 12 GB, robust across
  the overhead sweep); 1536-d adds **~16.4 GB** and **fails outright**.
- **Monthly recurring (`$50/mo`)** is dominated by the compute add-on needed to
  keep the HNSW index in RAM for warm latency. 384-d full sits **exactly at the
  boundary**: ~$0.10/mo on included (disk-cached) Micro compute, **~$50/mo on a
  Medium add-on** for RAM-resident serving — hence **CONDITIONAL** pending the
  latency gates (tasks 2.16 / 3.10). 1536-d would need a Large/XL add-on
  (~$100–$200/mo) but is already blocked by storage.

This is consistent with the plan's "384-dimensional index is the preferred
starting candidate … 1536-dimensional index remains the quality fallback."

---

## 1. Scenarios

Two corpora × two dimensions = four headline scenarios. The model
(`scripts/capacity_model.py`) is deterministic and byte-reproducible.

- **Pilot** — plan backfill order (task 2.13): all distillations + all external
  resources (prose) + all workflow Python representations + a representative
  **5,000-message** Discord sample.
- **Full eligible corpus** — the production target: **1,245,006 eligible messages**
  (`is_deleted=false`, schema map §5) + all resources + workflow Python +
  distillations. (Plan headline "~1.25M messages"; the 6,987 soft-deleted
  messages are excluded from embedding by the same eligibility rule Phase 1 must
  enforce. Using the `pg_class` `reltuples` 1,248,240 instead moves every figure
  <0.3% and changes no verdict.)

Cohort vector counts (constant across dimensions):

| Cohort | Vectors (pilot) | Vectors (full) |
|---|---:|---:|
| Messages (1 chunk each) | 5,000 | 1,245,006 |
| Resource prose (chunked, ≈512 tok) | 19,313 | 19,313 |
| Workflow Python (code-chunked, ≈512 tok; 222 workflows × ≈76 chunks) | 16,872 | 16,872 |
| Distillations (1 chunk each) | 11 | 11 |
| **Total** | **41,196** | **1,281,202** |

> Workflow Python is the non-trivial resource cohort: 222 workflows averaging
> ~138,983 chars (~34,746 tokens) each generate ~16,872 code chunks at a 512-token
> chunk size — more vectors than resource prose. At 2,048-token chunks this drops
> to ~4,200 (sensitivity sweep below). Messages still dominate the full corpus
> (97% of vectors). Resource-prose chunk counts use the measured body mean and are
> approximate (heavy tail); they are <2% of full-corpus vectors and do not move any
> gate. The 222 Python-bearing workflows are conservatively counted once in prose
> (via the body mean) and once in the Python cohort — a slight over-count that
> only widens the gates' margins.

---

## 2. Storage, memory, spend, compute — by scenario

### Full eligible corpus @ 384-d (the recommended path)

| Quantity | Central | Range / note |
|---|---:|---|
| Raw vector payload | 1.83 GiB | matches plan "~1.9 GB" |
| Heap (`content_embeddings` table) | 2.11 GiB | vector(384)=1,544 B + ~200 B row/chunk_text |
| HNSW index | 2.02 GiB | low 1.96 / high 2.14 (graph-overhead sweep) |
| Secondary indexes | 0.15 GB | PK + hash + active-contract lookup |
| **New storage (vec-table + HNSW + 2nd idx)** | **4.59 GB** | low 4.53 / high 4.72 |
| Total DB after (corpus tables only) | 5.47 GiB | **under the 8 GB Pro disk** → $0 extra storage |
| HNSW build `maintenance_work_mem` to fit graph | ~2.0 GiB | build is slower (but correct) below this |
| Backfill cost | $0.84 (3-small) | $5.44 on 3-large quality alt |
| RAM-resident serving tier | **Medium (4 GB, +$50/mo)** | disk-cached on included Micro = $0 extra |
| Monthly steady-state | **$0.10** (disk-cached) / **$50.10** (RAM-resident) | compute-dominated |

### Full eligible corpus @ 1536-d (quality fallback — gated out)

| Quantity | Central | Range / note |
|---|---:|---|
| Raw vector payload | 7.33 GiB | matches plan "~7.7 GB" |
| Heap | 7.61 GiB | vector(1536)=6,152 B + ~200 B |
| HNSW index | 7.52 GiB | low 7.46 / high 7.64 |
| **New storage** | **16.40 GB** | **> 12 GB → gate FAIL** (robust: low 16.33 / high 16.53) |
| Total DB after (corpus only) | 16.47 GiB | +9.68 GB over the 8 GB disk → +$1.21/mo storage |
| Backfill cost | $0.84 (3-small) | per-token price is dim-independent |
| RAM-resident serving tier | XL (16 GB, +$200/mo) | Large (8 GB, +$100) barely fits |
| Monthly steady-state | $1.31 (disk-cached) / $201.31 (RAM-resident) | blocked by storage anyway |

### Pilot (both dimensions)

| Quantity | 384-d | 1536-d |
|---|---:|---:|
| New storage | 0.33 GB | 0.71 GB |
| Total DB after (corpus only) | 1.50 GiB | 1.85 GiB |
| Backfill cost (3-small) | $0.37 | $0.37 |
| RAM-resident tier | Micro (included) | Micro (included) |
| Monthly | $0.10 | $0.10 |

The pilot clears every gate at both dimensions with >10× storage margin, so the
dimension comparison (task 2.14) can run on real HNSW without capacity risk.

---

## 3. The three fixed gates — verdicts and reasoning

### `$25` initial embedding spend — **PASS (both dims, both scales)**

Full-corpus backfill embeds ≈41.8M input tokens (≈23.7M messages + ≈9.7M
resources + ≈8.5M workflow Python) → **$0.84** at `text-embedding-3-small`'s
$0.02/M. Even the `3-large` quality path ($0.13/M) is $5.44. Pilot is $0.37.
Robustly under $25 with ~30–300× headroom; the gate is a non-constraint.

### `12 GB` projected vector-table + HNSW storage — **decides the dimension**

Plan stop condition (authoritative): *"Full-corpus backfill does not begin if
projected vector-table plus HNSW storage exceeds `12 GB`."* The task wording
("≥12 GB headroom after the index") is a looser restatement of the same gate;
both readings agree on the verdict, reported here together:

- **384-d full: PASS.** New storage 4.59 GB central (4.53–4.72 across the
  graph-overhead sweep) — **2.6–2.7× margin**. Total DB ≈5.5 GiB, **inside the
  8 GB Pro disk** ($0 extra), leaving ~2.4 GiB headroom on the included tier.
- **1536-d full: FAIL.** New storage 16.4 GB central (16.33–16.53) — **37%
  over** the gate. Total DB ≈16.5 GiB, +9.7 GB over the included disk.

> The 12 GB verdict is **insensitive to the model's main heuristic** (HNSW graph
> overhead, which pgvector does not publish). At 1536-d the stored vector copy
> dominates the index, so the overhead sweep moves total storage by <1.5% — the
> FAIL is structural, not an estimation artifact. At 384-d the PASS holds across
> the entire sweep. (Tests pin this: `SensitivityTests`.)

### `$50/month` recurring incremental — **CONDITIONAL for 384-d full**

Plan stop condition: *"pauses if observed search-related infra cost is projected
to add more than `$50/month`."* Recurring cost decomposes as:

| Component | 384-d full | 1536-d full |
|---|---:|---:|
| Extra DB storage (beyond 8 GB) | $0.00 (fits in included) | $1.21/mo |
| Compute add-on (to keep HNSW in RAM) | **$0 (Micro) … $50 (Medium)** | $0 (Micro) … $200 (XL) |
| Edge Function invocations (250K searches + worker cron) | $0.00 (under 2M) | $0.00 |
| Query embeddings (250K/mo × 20 tok) | $0.10 | $0.10 |

The single live lever is **compute**: the 2.0 GiB 384-d HNSW index can be served
disk-cached on the included Micro instance (≈$0.10/mo, *capacity* PASS), but
meeting the plan's warm **p95 ≤ 1.0 s** gate most likely needs the index
RAM-resident on a **Medium (4 GB) add-on at +$50/mo** — exactly on the boundary.
Hence **CONDITIONAL**: the capacity question is settled, the latency question is
not, and latency is gated to tasks **2.16** (HNSW tuning) and **3.10** (Phase-3
load test). 1536-d would need Large/XL (+$100–$200/mo) but is already
storage-blocked.

---

## 4. Cited assumptions (prices, technical facts, timestamps)

All prices are **vendor list prices** retrieved 2026-07-28 from primary
documentation (full citations in `phase0-capacity-assumptions.json → sources`):

| Fact | Value | Source | Retrieved |
|---|---|---|---|
| `text-embedding-3-small` | **$0.02 / 1M input tokens** | `developers.openai.com/api/docs/pricing` | 2026-07-28 |
| `text-embedding-3-large` | $0.13 / 1M input tokens (1536-d quality alt) | `developers.openai.com/api/docs/pricing` | 2026-07-28 |
| `dimensions` param | 3-small native 1536-d; 3-large native 3072-d; both shorten (256 shown) → 384-d = 3-small(dims=384), 1536-d = 3-small native | `developers.openai.com/api/docs/guides/embeddings` | 2026-07-28 |
| Supabase Pro | $25/mo; **8 GB disk** included then $0.125/GB (General Purpose) | `supabase.com/pricing` | 2026-07-28 |
| Supabase compute | Pro includes $10 credit = 1 Micro (1 GB); Small $15/2 GB, **Medium $60/4 GB**, Large $110/8 GB, XL $210/16 GB, 2XL $410/32 GB | `supabase.com/pricing` | 2026-07-28 |
| Edge Functions | **2,000,000 invocations included** on Pro, then $2 / 1M | `supabase.com/pricing` | 2026-07-28 |
| pgvector vector(D) on-disk | **`4*D + 8` bytes** per value | `github.com/pgvector/pgvector` | 2026-07-28 |
| pgvector HNSW | defaults `m=16`, `ef_construction=64`, `ef_search=40`; builds faster when graph fits `maintenance_work_mem`; index **need not** be RAM-resident for correct queries | `github.com/pgvector/pgvector` | 2026-07-28 |

**Measured corpus facts** (facts, not assumptions — not swept; from 0.2/0.3,
2026-07-28): eligible messages 1,245,006; resources 2,759 (2,757 workflows);
workflows with Python 222; distillations 11; message est-tokens mean 19;
resource-body est-tokens mean 3,189; workflow-Python est-tokens mean 34,746.

These are **dated list prices, not a budget guarantee** — the plan itself says to
recalculate against the provider's current pricing before backfill.

---

## 5. Model heuristics (not vendor-published) — explicit ranges

pgvector does not publish an HNSW index-size formula, so the index model is a
**documented lower bound plus a graph-overhead heuristic**, swept over a range:

- **HNSW index** = stored copy of every vector (`4D+8` B, documented) **+ graph
  overhead** of 100/150/250 B per node (low/central/high), derived from `m=16`
  (≈2×16 base-layer links + upper layers + page headers). The verdict is
  evaluated at central **and** high; the 12 GB result is robust to this range.
- **Heap row overhead** ≈180–240 B (central 200): bigint `contract_id`, the
  text identity/hash columns, ints, timestamptz, tuple header. Messages store no
  `chunk_text` (chunk 0 = the row); resource/workflow rows store ~1 KB snippet.
- **Chunking**: prose 512 tok, Python 512 tok, 10% overlap (sweep 256–2,048).
- **RAM safety factor** 1.25 (index shares RAM with `shared_buffers` + OS).
- **Secondary indexes** ≈0.15 GB (composite PK, two sha-256 hashes, contract
  lookup) — largely dimension-independent.

---

## 6. Uncertainties and mitigations

1. **Full-project DB size is unmeasured.** Task 0.3 measured the *corpus tables*
   (~1.28 GB, of which `discord_messages` is 1.15 GB). The live project has 537
   relations including the larger Banodoco/Astrid app, so the true current DB
   total is larger and the "8 GB included" headroom is lower than the corpus-only
   figure suggests. **Measure the full DB size before any index build.** The 12 GB
   gate is on *new* storage and is unaffected.
2. **Message token mean is a sample.** The 19-token mean comes from the 5,000-msg
   stratified sample. At the plan's conservative 50-/100-token assumptions the
   full-corpus spend is still only $1.25/$2.50 — spend is a non-constraint across
   the whole plausible range.
3. **HNSW graph overhead is heuristic.** Bounded by the sweep; the 12 GB verdict
   is insensitive to it (§3).
4. **`$50/mo` hinges on latency, not capacity.** Whether the 384-d index needs
   the Medium add-on is deferred to tasks 2.16 / 3.10. If Small (2 GB) serves the
   2.0 GiB index from OS cache at acceptable p95, the recurring cost drops to
   ~$0–$5/mo (clean PASS).
5. **Mitigations that could change a 1536 verdict** (not assumed, flagged for
   task 0.8 / 2.14 if 1536 quality is ever required):
   - **`halfvec`** halves vector storage (2 B/dim): a 1536-d index would drop
     toward ~8 GB new storage — near, but still likely over, the 12 GB gate for
     the full message corpus.
   - **Binary quantization** (1 bit/dim, 32× smaller) with re-ranking makes
     1536-d storage trivial but trades recall for the re-ranking pass.
   - **Cohort gating**: index distillations + resources + workflow Python + a
     message subset at 1536-d (the pilot shape) clears all gates; only the *full
     1.25M-message* 1536-d index is storage-blocked.

---

## 7. Reproducibility and tests

- **Tool:** `scripts/capacity_model.py` — pure, deterministic, offline. Writes
  `--results` and `--assumptions` JSON; prints a human summary. Re-running
  produces byte-identical scenario numbers (the only time-derived field is the
  output `generated_at`).
- **Tests:** `tests/test_capacity_model.py` — 28 tests pin: the
  `4D+8`/`4D·N`/chunk/embedding-cost formulas, GiB-vs-GB unit separation,
  scenario determinism across runs, monotonicity (1536 > 384 on every storage
  component; full > pilot), the plan's headline raw-vector figures
  (384→~1.9 GB, 1536→~7.7 GB), gate-boundary inclusivity, the four expected
  gate verdicts, tier selection, and that the 12 GB verdict holds across the
  HNSW-overhead sweep.
- **Boundary:** no database object created/altered/dropped; no pgvector enabled;
  no index built; no provider called; no secret read, printed, or committed; no
  source content read; the production dimension/chunk contract not chosen (task
  2.14). The dirty Hivemind working tree and the concurrent 0.4/0.5
  evaluation/inventory work were preserved untouched.

---

## Completion signal (0.7)

> Capacity report evaluates the `$25`, `12 GB`, and `$50/month` gates.

**Met.** This report and `scripts/capacity_model.py` evaluate all three fixed
gates for 384- and 1536-dimensional candidates across the pilot and full
eligible corpus, with dated primary-source citations, measured inputs, explicit
heuristic ranges, sensitivity sweeps, and pass/fail/conditional verdicts. The
verdicts: `$25` spend **PASS** everywhere; `12 GB` storage **PASS for 384-d
full (4.59 GB) and both pilots, FAIL for 1536-d full (16.4 GB)**; `$50/mo`
**PASS for pilots and CONDITIONAL for 384-d full** (disk-cached ≈$0 vs
RAM-resident Medium ≈$50, latency-gated to 2.16/3.10), with 1536-d full already
storage-blocked.

## Next tasks (dependency-safe — do not start here)

Task 0.7 has no blockers. It unblocks (per the plan graph):

- **0.8** — Freeze the workflow representation/embedding/chunk contracts. This
  model's workflow-Python cohort sizing (222 workflows × ~76 chunks at 512 tok)
  and the halfvec/binary-quantization/cohort levers are explicit inputs.
- **1.6** — Select the message exact-identifier path; the message-trigram-index
  storage cost should be checked against the same 12 GB envelope.
- **2.2** — Enable pgvector in staging (no capacity surprise: 384-d pilot is
  <1 GB; build `maintenance_work_mem` ~2 GiB for a 384-d full HNSW).
- **2.16 / 3.10** — Resolve the 384-d `$50/mo` **CONDITIONAL**: measure warm p95
  on included-Micro vs Medium compute and pick the tier.
- **5.1 / 5.2** — Cohort planning and the preflight: measure the **full-project
  DB size** before the production backfill/index build.

Do **not** choose the production dimension/chunk contract, enable pgvector,
create indexes, call providers, or start backfills from this task.
