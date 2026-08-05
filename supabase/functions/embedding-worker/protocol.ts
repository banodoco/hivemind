// Hivemind embedding-worker protocol (plan task 2.9).
//
// Pure, dependency-free helpers for the bounded `embedding-worker` Edge
// Function: the approved server-side OpenAI call (injectable transport so tests
// never touch the network), strict 384-dimension validation, and the shapes the
// worker surface RPCs (schema/027) exchange. No credential is ever logged: the
// key is read once from the Edge secret store and used only in the Authorization
// header; errors surface only status, never the body.

export const OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings";
export const OPENAI_EMBEDDING_MODEL = "text-embedding-3-small";
export const EMBEDDING_DIMENSION = 384;
export const DEFAULT_BATCH_SIZE = 8;
export const DEFAULT_LEASE_SECONDS = 300;
export const PROVIDER_TIMEOUT_MS = 10_000;

/** A claimed job row from hivemind_claim_embedding_jobs (schema/026 / 034).
 *  Task 2.17: the 034 override returns contract_id as TEXT so a JS consumer
 *  cannot lose precision on the selected bigint (> 2^53). */
export interface ClaimedJob {
  job_id: number;
  entity_type: string;
  item_id: string;
  representation_type: string;
  job_kind: string; // "embed" | "reembed" | "drop"
  contract_id: string | null;
  attempts: number;
}

/** A canonical-chunk row from hivemind_embedding_payload (schema/027 / 034).
 *  Task 2.17: the 034 override adds public_state so the worker can gate
 *  workflow_python on 'safe' before any embed call. */
export interface EmbeddingChunkRow {
  representation_hash: string;
  source_available: boolean;
  public_state: string | null; // 'safe' | 'quarantined' | null
  chunk_index: number | null;
  chunk_text: string | null;
  chunk_hash: string | null;
  method: string | null;
}

/** The frozen selected contract the worker commits against (task 2.17). The
 *  identity is chunking BEHAVIOR v2 + chunk_config IDENTITY scheme v1 — the two
 *  axes are deliberately distinct: the bounded oversized-fallback fix bumped the
 *  chunker behavior but added no config identity axis, so chunkingVersion is 2
 *  while chunkConfigVersion stays 1. No 1536 path exists; selectedContractId is
 *  the bigint-as-string (1360541028304258884) compared as a string to avoid JS
 *  precision loss on a value > 2^53. */
export interface SelectedContractConfig {
  readonly provider: string;
  readonly model: string;
  readonly dimension: 384;
  readonly canonicalizationVersion: 1;
  readonly chunkingVersion: 2;
  readonly chunkConfigVersion: 1;
  readonly chunkConfigIdentity: string;
  readonly proseTarget: 512;
  readonly proseOverlap: 50;
  readonly pythonTarget: 512;
  readonly pythonOverlap: 50;
  readonly evalContractIdHex: string;
  readonly fullConfigSha256: string;
  readonly selectedContractId: string;
}

export const SELECTED_CONTRACT: Readonly<SelectedContractConfig> = Object.freeze({
  provider: "openai",
  model: "text-embedding-3-small",
  dimension: 384,
  canonicalizationVersion: 1,
  chunkingVersion: 2,
  chunkConfigVersion: 1,
  chunkConfigIdentity: "chunk_config\u001fv1\u001fprose#512/50\u001fworkflow_python#512/50",
  proseTarget: 512,
  proseOverlap: 50,
  pythonTarget: 512,
  pythonOverlap: 50,
  evalContractIdHex: "12e19cdb566b8744",
  fullConfigSha256: "12e19cdb566b87445ab2d3563e6cb948f58801f78f8395878fc9e0c2457d5462",
  selectedContractId: "1360541028304258884",
});

/** Result of the source-hash-safe atomic commit (schema/034 finalize RPC). */
export type FinalizeOutcome =
  | "completed"
  | "dropped"
  | "source_changed"
  | "contract_mismatch"
  | "validation_failed"
  | "not_processing";

export interface FinalizeResult {
  chunks_written: number;
  outcome: FinalizeOutcome;
  job_status: string;
}

/** Injectable provider transport: (url, headers, body) -> parsed JSON. */
export type EmbedTransport = (
  url: string,
  headers: Record<string, string>,
  body: Record<string, unknown>,
) => Promise<unknown>;

export class EmbeddingError extends Error {}

/** Return true iff v is a finite number[] of exactly `dim` entries. */
export function isValidVector(v: unknown, dim: number = EMBEDDING_DIMENSION): v is number[] {
  if (!Array.isArray(v) || v.length !== dim) return false;
  return v.every((x) => typeof x === "number" && Number.isFinite(x as number));
}

/**
 * Embed a batch of texts through the approved server-side boundary.
 *
 * Returns one validated 384-d vector per input, preserving order. Throws
 * EmbeddingError (status only, never the body) on any provider/transport
 * failure, wrong count, or a wrong-dimension/non-finite vector — the worker
 * treats that as a fail-closed job retry.
 */
export async function embedTexts(
  texts: string[],
  apiKey: string,
  transport: EmbedTransport,
  opts: { model?: string; dimension?: number } = {},
): Promise<number[][]> {
  if (texts.length === 0) return [];
  if (!apiKey) throw new EmbeddingError("OpenAI embeddings requested without a configured key");
  const model = opts.model ?? OPENAI_EMBEDDING_MODEL;
  const dimension = opts.dimension ?? EMBEDDING_DIMENSION;
  const headers = {
    "authorization": `Bearer ${apiKey}`,
    "content-type": "application/json",
  };
  const body = { model, input: texts, dimensions: dimension };
  let parsed: unknown;
  try {
    parsed = await transport(OPENAI_EMBEDDINGS_URL, headers, body);
  } catch (err) {
    throw new EmbeddingError(`OpenAI embeddings transport error: ${errorName(err)}`);
  }
  const data = (parsed as { data?: unknown })?.data;
  if (!Array.isArray(data)) throw new EmbeddingError("OpenAI embeddings response missing 'data' array");
  const ordered = [...data as { index?: number; embedding?: unknown }[]]
    .sort((a, b) => (a.index ?? 0) - (b.index ?? 0));
  const vectors = ordered.map((item) => item.embedding);
  if (vectors.length !== texts.length) {
    throw new EmbeddingError(`OpenAI returned ${vectors.length} vectors for ${texts.length} inputs`);
  }
  return vectors.map((v, i) => {
    if (!isValidVector(v, dimension)) {
      throw new EmbeddingError(`vector ${i} failed ${dimension}-d finite validation`);
    }
    return v as number[];
  });
}

function errorName(err: unknown): string {
  if (err instanceof Error) return err.name;
  return typeof err === "string" ? err : "unknown";
}

/** Render a vector as the pgvector text literal content_embeddings stores. */
export function vectorLiteral(v: number[]): string {
  return "[" + v.join(",") + "]";
}
