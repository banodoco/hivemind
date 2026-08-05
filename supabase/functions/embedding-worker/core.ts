// Hivemind embedding-worker — CORE orchestration (task 2.17 / I6).
//
// Pure importable job orchestration. It NEVER calls Deno.serve and NEVER reads
// Deno.env at module top level: all knobs arrive via WorkerConfig, all I/O via
// the injected Rpc + EmbedTransport transports, and time via now(). index.ts is
// the only place that touches Deno.env / Deno.serve / the service-role key.
//
// Per-job protocol (schema/034 selected-contract surface):
//   1. recover stale leases, then claim a bounded batch (SKIP LOCKED);
//   2. reject any job whose contract_id != the selected literal — fail CLOSED,
//      no payload fetch, no embed call;
//   3. fetch the canonical payload; if !source_available or (workflow_python &&
//      public_state != 'safe') -> ZERO embed calls, finalize with empty chunks
//      (the RPC drops stale vectors atomically);
//   4. else embed ONCE (embedTexts validates count/finite/dimension), then call
//      the source-hash-safe hivemind_finalize_embedding_job (verify lease ->
//      recompute current source -> replace OR drop OR requeue -> complete).
//
// Malformed provider output (wrong count / non-finite / wrong dimension) throws
// before finalize, so the job fails closed with no write. Transient provider
// failures retry boundedly; exhaustion fails the job. Zero remote imports; no
// test-only branches in production paths.

import {
  EmbeddingError,
  type ClaimedJob,
  type EmbeddingChunkRow,
  type EmbedTransport,
  type FinalizeResult,
  type SelectedContractConfig,
  embedTexts,
  isValidVector,
  SELECTED_CONTRACT,
  vectorLiteral,
} from "./protocol.ts";

/** Injectable PostgREST RPC: call a Supabase function by name. */
export type Rpc = <T = unknown>(name: string, params: Record<string, unknown>) => Promise<T>;

/** All worker knobs — built by index.ts from Deno.env; built by tests directly. */
export interface WorkerConfig {
  readonly workerId: string;
  readonly apiKey: string;
  readonly transport: EmbedTransport;
  readonly contract: SelectedContractConfig;
  readonly batchSize: number; // env-clamped 1..64 in index.ts
  readonly leaseSeconds: number; // env-clamped in index.ts
  readonly embedMaxAttempts: number; // bounded retry
  readonly embedBackoffMs: number; // bounded backoff between attempts
  readonly recoverGraceSeconds: number;
  /** Clock for lease/timeout math. */
  readonly now: () => number;
  /** Bounded sleep for retry backoff. Tests inject a no-op. */
  readonly sleep: (ms: number) => Promise<void>;
}

export interface Outcome {
  claimed: number;
  embedded: number;
  dropped: number;
  empty: number;
  completed: number;
  failed: number;
  errors: string[]; // sanitized + bounded
}

export function newOutcome(): Outcome {
  return { claimed: 0, embedded: 0, dropped: 0, empty: 0, completed: 0, failed: 0, errors: [] };
}

// ── bounded / sanitized logging helpers ────────────────────────────────────

export function safeMessage(err: unknown): string {
  // Never persist or return provider/RPC exception text.  The transport and
  // PostgREST layers may include URLs, request bodies, credentials, or source
  // text in an exception.  Keep only bounded reason codes for the job/error
  // boundary; the detailed exception remains intentionally unreachable.
  const raw = err instanceof Error ? err.message : typeof err === "string" ? err : "";
  if (/without a configured key/i.test(raw)) return "provider_key_missing";
  if (/returned \d+ vectors for \d+ inputs|count mismatch|missing 'data'/i.test(raw)) {
    return "provider_invalid_count";
  }
  if (/finite|dimension|vector \d+ (failed|invalid)|invalid vector/i.test(raw)) {
    return "provider_invalid_vector";
  }
  if (/transport|timeout|abort|http \d+/i.test(raw)) return "provider_transport_error";
  if (/contract_mismatch/i.test(raw)) return "contract_mismatch";
  if (/source_changed/i.test(raw)) return "source_changed";
  return "worker_error";
}

export function bounded(s: string, n = 300): string {
  return s.length <= n ? s : s.slice(0, n);
}

/** A malformed-provider-output error is permanent (fail closed, no retry). */
function isMalformedOutput(err: unknown): boolean {
  if (!(err instanceof EmbeddingError)) return false;
  return /vector|count|validation|finite|dimension|missing 'data'/i.test(err.message);
}

// ── per-job processing ──────────────────────────────────────────────────────

async function failJob(
  rpc: Rpc,
  job: ClaimedJob,
  config: WorkerConfig,
  outcome: Outcome,
  err: unknown,
  retryable: boolean,
): Promise<void> {
  outcome.failed += 1;
  outcome.errors.push(bounded(safeMessage(err)));
  await rpc("hivemind_fail_embedding_job", {
    p_job_id: job.job_id,
    p_worker_id: config.workerId,
    p_error: bounded(safeMessage(err)),
    p_retryable: retryable,
  }).catch(() => {});
}

async function handleDrop(
  rpc: Rpc,
  job: ClaimedJob,
  config: WorkerConfig,
  outcome: Outcome,
): Promise<void> {
  const contractId = job.contract_id ?? "";
  if (!contractMatches(job, config.contract.selectedContractId)) {
    await failJob(
      rpc,
      job,
      config,
      outcome,
      new EmbeddingError("contract_mismatch"),
      false,
    );
    return;
  }
  if (contractId) {
    await rpc("hivemind_drop_embedding_chunks", {
      p_contract_id: contractId,
      p_entity_type: job.entity_type,
      p_item_id: job.item_id,
      p_representation_type: job.representation_type,
    });
  }
  await rpc("hivemind_complete_embedding_job", {
    p_job_id: job.job_id,
    p_worker_id: config.workerId,
    p_chunks_written: 0,
  });
  outcome.dropped += 1;
  outcome.completed += 1;
}

/** Compare the contract id as a STRING (the selected bigint is > 2^53). */
function contractMatches(job: ClaimedJob, selected: string): boolean {
  const cid = job.contract_id;
  if (cid === null || cid === undefined) return false;
  return String(cid) === selected;
}

function selectedConfigMatches(c: SelectedContractConfig): boolean {
  return c.provider === SELECTED_CONTRACT.provider
    && c.model === SELECTED_CONTRACT.model
    && c.dimension === SELECTED_CONTRACT.dimension
    && c.canonicalizationVersion === SELECTED_CONTRACT.canonicalizationVersion
    && c.chunkingVersion === SELECTED_CONTRACT.chunkingVersion
    && c.chunkConfigVersion === SELECTED_CONTRACT.chunkConfigVersion
    && c.chunkConfigIdentity === SELECTED_CONTRACT.chunkConfigIdentity
    && c.proseTarget === SELECTED_CONTRACT.proseTarget
    && c.proseOverlap === SELECTED_CONTRACT.proseOverlap
    && c.pythonTarget === SELECTED_CONTRACT.pythonTarget
    && c.pythonOverlap === SELECTED_CONTRACT.pythonOverlap
    && c.evalContractIdHex === SELECTED_CONTRACT.evalContractIdHex
    && c.fullConfigSha256 === SELECTED_CONTRACT.fullConfigSha256
    && c.selectedContractId === SELECTED_CONTRACT.selectedContractId;
}

function targetTokensFor(repType: string, c: SelectedContractConfig): number {
  return repType === "workflow_python" ? c.pythonTarget : c.proseTarget;
}
function overlapTokensFor(repType: string, c: SelectedContractConfig): number {
  return repType === "workflow_python" ? c.pythonOverlap : c.proseOverlap;
}

function eligibleChunks(job: ClaimedJob, rows: EmbeddingChunkRow[]): EmbeddingChunkRow[] {
  return rows.filter((r) => {
    if (!r.source_available) return false;
    if (!r.chunk_text) return false;
    if (job.representation_type === "workflow_python" && r.public_state !== "safe") return false;
    return true;
  });
}

async function processJob(
  rpc: Rpc,
  job: ClaimedJob,
  config: WorkerConfig,
  outcome: Outcome,
): Promise<void> {
  // 1. DROP job kind.
  if (job.job_kind === "drop") {
    await handleDrop(rpc, job, config, outcome);
    return;
  }

  // 2. Contract gate — fail CLOSED before any payload fetch or embed call.
  if (!contractMatches(job, config.contract.selectedContractId)) {
    await failJob(
      rpc,
      job,
      config,
      outcome,
      new Error(`contract_mismatch: job=${job.contract_id} selected=${config.contract.selectedContractId}`),
      false,
    );
    return;
  }

  // 3. Fetch canonical payload (CURRENT source). 034 column set.
  const rows = await rpc<unknown>("hivemind_embedding_payload", {
    p_entity_type: job.entity_type,
    p_item_id: job.item_id,
    p_representation_type: job.representation_type,
    p_target_tokens: targetTokensFor(job.representation_type, config.contract),
    p_overlap_tokens: overlapTokensFor(job.representation_type, config.contract),
  });
  if (!Array.isArray(rows)) {
    throw new EmbeddingError("payload_invalid_shape");
  }
  // PostgREST returns RETURNS TABLE as a JSON array. Do not coerce an object,
  // null, or a one-row stand-in into a successful empty payload: that could
  // delete good vectors without ever proving the current source.
  const payloadRows = rows as EmbeddingChunkRow[];
  const expectedHash = payloadRows[0]?.representation_hash ?? null;
  const expectedPublic = payloadRows[0]?.public_state ?? null;
  const expectedAvailable = payloadRows.some((r) => r.source_available);

  const eligible = eligibleChunks(job, payloadRows);

  // 4. Quarantine / unavailable -> ZERO embed calls; finalize with empty chunks
  //    so the RPC drops stale vectors atomically (schema/034 defect D2).
  if (eligible.length === 0) {
    const result = await rpc<FinalizeResult>("hivemind_finalize_embedding_job", {
      p_job_id: job.job_id,
      p_worker_id: config.workerId,
      p_chunks: [],
      p_expected_representation_hash: expectedHash,
      p_expected_public_state: expectedPublic,
      p_expected_source_available: expectedAvailable,
    });
    if (result?.outcome === "dropped" || result?.outcome === "completed") outcome.empty += 1;
    outcome.completed += 1;
    return;
  }

  // 5. Embed ONCE with bounded retry. embedTexts validates count/finite/dim and
  //    throws EmbeddingError; a malformed output fails closed BEFORE finalize.
  let vectors: number[][];
  try {
    vectors = await embedWithRetry(eligible.map((c) => c.chunk_text as string), config);
  } catch (err) {
    // Malformed output -> fail closed (non-retryable); exhausted transient ->
    // fail retryable so the job can retry later at the queue level.
    await failJob(rpc, job, config, outcome, err, !isMalformedOutput(err));
    return;
  }
  // Defensive double-check (runs in prod too; no test-only branch).
  if (vectors.length !== eligible.length) {
    await failJob(rpc, job, config, outcome, new EmbeddingError("vector count mismatch"), false);
    return;
  }
  for (let i = 0; i < vectors.length; i++) {
    if (!isValidVector(vectors[i], config.contract.dimension)) {
      await failJob(rpc, job, config, outcome, new EmbeddingError(`vector ${i} invalid`), false);
      return;
    }
  }

  // 6. Assemble + source-hash-safe atomic finalize (single transaction).
  const chunks = eligible.map((c, i) => ({
    chunk_index: c.chunk_index ?? i,
    chunk_text: c.chunk_text,
    embedding: vectorLiteral(vectors[i]),
    chunk_hash: c.chunk_hash,
    representation_hash: c.representation_hash,
    contract_id: job.contract_id,
    entity_type: job.entity_type,
    item_id: job.item_id,
    representation_type: job.representation_type,
  }));
  const result = await rpc<FinalizeResult>("hivemind_finalize_embedding_job", {
    p_job_id: job.job_id,
    p_worker_id: config.workerId,
    p_chunks: chunks,
    p_expected_representation_hash: expectedHash,
    p_expected_public_state: expectedPublic,
    p_expected_source_available: true,
  });
  switch (result?.outcome) {
    case "completed":
      outcome.embedded += typeof result.chunks_written === "number" ? result.chunks_written : chunks.length;
      outcome.completed += 1;
      return;
    case "dropped":
      outcome.empty += 1;
      outcome.completed += 1;
      return;
    case "source_changed":
      // The RPC requeued the job with fresh source; no stale authority retained.
      outcome.completed += 1;
      return;
    case "contract_mismatch":
    case "validation_failed":
      await failJob(rpc, job, config, outcome, new Error(`finalize: ${result.outcome}`), false);
      return;
    case "not_processing":
    default:
      // Lease lost / already handled.
      outcome.completed += 1;
      return;
  }
}

async function embedWithRetry(texts: string[], config: WorkerConfig): Promise<number[][]> {
  let lastErr: unknown;
  for (let attempt = 1; attempt <= config.embedMaxAttempts; attempt++) {
    try {
      return await embedTexts(texts, config.apiKey, config.transport, {
        model: config.contract.model,
        dimension: config.contract.dimension,
      });
    } catch (err) {
      lastErr = err;
      // Malformed output is permanent — propagate immediately (fail closed).
      if (isMalformedOutput(err)) throw err;
      if (attempt < config.embedMaxAttempts) {
        await config.sleep(config.embedBackoffMs * attempt);
      }
    }
  }
  throw lastErr instanceof Error ? lastErr : new EmbeddingError("embed exhausted retries");
}

// ── top-level orchestration ─────────────────────────────────────────────────

export interface RunDeps {
  rpc: Rpc;
  config: WorkerConfig;
}

/** Run one bounded invocation: recover stale leases, claim, process each job. */
export async function runOnce(deps: RunDeps): Promise<Outcome> {
  const { rpc, config } = deps;
  const outcome = newOutcome();

  // Do not even claim a job if the worker was configured with a partial or
  // mismatched identity surface. This is a configuration failure, not a
  // provider failure, and therefore cannot mutate queue state.
  if (!selectedConfigMatches(config.contract)) {
    outcome.failed = 1;
    outcome.errors.push("contract_config_mismatch");
    return outcome;
  }

  // Best-effort stale-lease recovery: a job left 'processing' by a crashed prior
  // invocation is requeued to 'pending' here (verified against schema/026: claim
  // selects status='pending'; recover flips only rows past lease_expires_at, so
  // a still-in-flight job in the same batch is never re-claimed).
  await rpc("hivemind_recover_stale_leases", { p_grace_seconds: config.recoverGraceSeconds })
    .catch(() => {});

  const claimed = await rpc<unknown>("hivemind_claim_embedding_jobs", {
    p_worker_id: config.workerId,
    p_batch_size: config.batchSize,
    p_lease_seconds: config.leaseSeconds,
  });
  if (!Array.isArray(claimed)) throw new EmbeddingError("claim_invalid_shape");
  const jobs = claimed as ClaimedJob[];
  outcome.claimed = jobs.length;

  for (const job of jobs) {
    try {
      await processJob(rpc, job, config, outcome);
    } catch (err) {
      outcome.failed += 1;
      outcome.errors.push(bounded(safeMessage(err)));
      const retryable = !isMalformedOutput(err);
      await rpc("hivemind_fail_embedding_job", {
        p_job_id: job.job_id,
        p_worker_id: config.workerId,
        p_error: bounded(safeMessage(err)),
        p_retryable: retryable,
      }).catch(() => {});
    }
  }
  return outcome;
}
