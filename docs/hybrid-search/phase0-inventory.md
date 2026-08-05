# Hivemind Corpus Inventory — Task 0.3
**Generated:** 2026-07-28T18:47:36.782242+00:00
**Plan date:** 2026-07-28

## 1. Row Counts

- **unified_feed (derived):** 1,251,010
  - ℹ️ unified_feed total is derived from underlying table counts, not a direct count on the view (which timed out due to join cost). The live view excludes rejected distillations and applies RLS.

### By entity type (unified_feed)
| Entity | Count |
|---|---|
| distillation | 11 |
| message | 1,248,240 |
| transcript | 1 |
| workflow | 2,757 |

### Distillations by status
| Status | Count |
|---|---|
| approved | 0 |
| pending | 11 |
| rejected | 0 |
| superseded | 0 |
| **total** | **11** |

- **distillation_cites:** 33
- **discord_messages (approx, pg_class):** 1,248,240

## 2. Text / Token Length Distributions

### External Resources — Body (characters)
N = 2,759

| Metric | Value |
|---|---|
| min | 1 |
| p50 | 1,612 |
| mean | 12,759 |
| p75 | 1,853 |
| p90 | 2,408 |
| p95 | 13,892 |
| p99 | 345,672 |
| max | 1,417,640 |

### External Resources — Title (characters)
N = 2,759

| Metric | Value |
|---|---|
| min | 1 |
| p50 | 49 |
| mean | 49 |
| p75 | 57 |
| p90 | 62 |
| p95 | 66 |
| p99 | 73 |
| max | 92 |

### External Resources — Body (estimated tokens, chars/4)
N = 2,759

| Metric | Value |
|---|---|
| min | 1 |
| p50 | 403 |
| mean | 3,189 |
| p75 | 463 |
| p90 | 601 |
| p95 | 3,472 |
| p99 | 86,417 |
| max | 354,410 |

### Distillations — Question
N = 11

| Metric | Value |
|---|---|
| min | 49 |
| p50 | 73 |
| mean | 70 |
| p75 | 77 |
| p90 | 81 |
| p95 | 81 |
| p99 | 81 |
| max | 81 |

### Distillations — Answer
N = 11

| Metric | Value |
|---|---|
| min | 208 |
| p50 | 383 |
| mean | 402 |
| p75 | 469 |
| p90 | 514 |
| p95 | 538 |
| p99 | 557 |
| max | 562 |

### Distillations — Conditions
N = 11

| Metric | Value |
|---|---|
| min | 44 |
| p50 | 134 |
| mean | 124 |
| p75 | 143 |
| p90 | 146 |
| p95 | 146 |
| p99 | 146 |
| max | 146 |

### Messages — Body (sample)
N = 5,000

| Metric | Value |
|---|---|
| min | 0 |
| p50 | 46 |
| mean | 77 |
| p75 | 89 |
| p90 | 154 |
| p95 | 237 |
| p99 | 660 |
| max | 1,953 |

### Messages — Body estimated tokens (sample)
N = 5,000

| Metric | Value |
|---|---|
| min | 1 |
| p50 | 11 |
| mean | 19 |
| p75 | 22 |
| p90 | 38 |
| p95 | 59 |
| p99 | 165 |
| max | 488 |

### Message Sampling Methodology
- **Method:** offset-stratified sample via PostgREST unified_feed
- **Sample size:** 5,000
- **Population:** 1,248,240
- **Pages:** 20
- **Stride:** 62412
- **Uncertainty:** Percentiles estimated from a sample, not the full population. True population percentiles may differ, especially in the tails. Methodology is reproducible: re-running with the same seed/offsets produces identical results.

### Long-Resource Distribution (external_resources body)
| Bucket | Count |
|---|---|
| <1 KB | 152 |
| 1-10 KB | 2,450 |
| 10-50 KB | 32 |
| 50-100 KB | 22 |
| 100-500 KB | 95 |
| 500 KB+ | 8 |

## 3. Workflow Representation Cohorts

**Total workflows:** 2,757

### Canonical-source cohorts (plan AD-4)
| Cohort | Count |
|---|---|
| payload_python (non-empty payload.python_source) | 0 |
| body_python only (extractable via delimiter) | 0 |
| both (payload + body Python) | 222 |
| neither (no Python in either location) | 2,535 |

### Aggregated cohorts
- **Any Python source available:** 222
- **No Python source:** 2,535
- **Duplicate body/payload Python:** 222
- **Suspect (credential patterns in payload.python_source):** 222
  - Reason codes: high_entropy_base64=222

### Workflow Python Size Statistics

#### Workflow Prose Body (characters)
N = 2,757

| Metric | Value |
|---|---|
| min | 613 |
| p50 | 1,613 |
| mean | 12,769 |
| p75 | 1,853 |
| p90 | 2,411 |
| p95 | 13,898 |
| p99 | 345,703 |
| max | 1,417,640 |

#### Workflow payload.python_source (characters)
N = 222

| Metric | Value |
|---|---|
| min | 3,268 |
| p50 | 95,216 |
| mean | 138,983 |
| p75 | 207,385 |
| p90 | 371,215 |
| p95 | 448,574 |
| p99 | 719,694 |
| max | 1,415,262 |

#### Workflow body-extracted Python (characters)
N = 222

| Metric | Value |
|---|---|
| min | 3,480 |
| p50 | 96,027 |
| mean | 139,843 |
| p75 | 208,392 |
| p90 | 371,980 |
| p95 | 450,192 |
| p99 | 720,821 |
| max | 1,416,828 |

### Python Coverage
- With payload.python_source: 222 / 2,757
- With body-extractable Python: 222 / 2,757

## 4. Table and Index Sizes

| Table | Total | Table Data | Indexes |
|---|---|---|---|
| discord_messages | 1154 MB | 666 MB | 488 MB |
| external_resources | 67 MB | 44 MB | 23 MB |
| distillations | 184 kB | 16 kB | 168 kB |
| vibecomfy_ratings | 64 kB | 16 kB | 48 kB |
| contributors | 48 kB | 16 kB | 32 kB |
| distillation_cites | 32 kB | 16 kB | 16 kB |

### Discord FTS Index
- **Name:** `idx_discord_messages_content_fts`
- **Size:** 85 MB
- **Definition:** `CREATE INDEX idx_discord_messages_content_fts ON public.discord_messages USING gin (to_tsvector('english'::regconfig, content))`

### Existing GIN trigram indexes
- `external_resources_body_trgm`
- `external_resources_title_trgm`
- `distillations_question_trgm`
- `distillations_answer_trgm`

## 5. Methodology and Uncertainty

### row_counts
Counts for external_resources, distillations, and distillation_cites are exact via PostgREST Content-Range header. Discord message count is approximate via pg_class.reltuples (fast). The unified_feed view is too expensive for exact counting (joins message_feed across 1.25M rows with correlated reaction subquery); its row count is derived from underlying table counts.

### text_lengths
Character lengths of the displayable body/text fields. For external_resources and distillations, all rows are measured. For messages, a stratified sample is used (see message_length_method). Estimated tokens use the rough heuristic chars/4 for English text; actual token counts depend on the tokeniser and may differ.

### workflow_representation
All kind=workflow rows from external_resources fetched via PostgREST. Cohorts classified according to plan AD-4 precedence: payload_python (non-empty payload.python_source), body_python (Python block via recognised delimiters in body), both (Python in both locations), neither. Duplicate detection checks whether payload python appears verbatim in body. Suspect scanning uses deterministic regex patterns for credential shapes; matched values are never stored or logged.

### Scope and limitations

- **Row counts:** Exact via PostgREST `Content-Range`.  The `unified_feed`
  view excludes rejected distillations and applies RLS; counts reflect the
  public surface.
- **Message lengths:** Estimated from a stratified sample of 5,000 messages
  fetched via PostgREST.  Full-population percentiles would require ~1,250
  API requests and are not practical for this inventory.  Sample estimates
  may diverge from true population values, especially in the tails (p99, max).
- **Token estimation:** Uses the rough heuristic `chars / 4` for English text.
  Actual token counts depend on the tokeniser (e.g. `text-embedding-3-small`
  uses `cl100k_base`) and may differ by ±30%.
- **Workflow cohorts:** Based on current database state.  `recoverable` rows
  (plan cohort 3) cannot be distinguished from `unavailable` (cohort 4) without
  running the VibeComfy exporter, which requires local filesystem access.
  This inventory reports the observable `payload_python`, `body_python`,
  and `neither` cohorts.  The `recoverable`/`unavailable` split is deferred
  to the workflow representation remediation script (task 0.8/2.12).
- **Secret scanning:** Uses deterministic regex patterns for known credential
  shapes.  It may produce false positives (e.g. high-entropy base64 in normal
  workflow code) and false negatives (obfuscated credentials).  Matched values
  are never stored or logged.  Only reason codes are recorded.
- **Table sizes:** Measured via pg_catalog (psql session-mode).  Sizes reflect
  current on-disk storage including dead tuples; they may not match a fresh
  `pg_dump`.

---
*Report generated at 2026-07-28T18:47:36.782242+00:00*
*Completed at 2026-07-28T18:50:48.217917+00:00*
