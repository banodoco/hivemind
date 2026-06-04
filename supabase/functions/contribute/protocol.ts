// ============================================================
// Hivemind contribute edge function — pure protocol logic
//
// All functions in this module are PURE: no network, no
// Supabase client, no environment access. They validate,
// parse, decide, and format. The Deno entrypoint (index.ts)
// calls them and handles I/O.
// ============================================================

// ---- Types ----

export type ActionKind = "add_resource" | "submit_distillation";

export type Confidence = "high" | "medium" | "low";

export type CiteItemKind = "message" | "resource" | "distillation";

export interface Cite {
  item_kind: CiteItemKind;
  // bigint in the DB. Discord message ids exceed Number.MAX_SAFE_INTEGER, so
  // ids MUST travel as strings — a JSON number would be silently rounded by
  // JSON.parse before validation could even see it. Numbers are still
  // accepted for small ids but normalized to string.
  item_id: string;
}

export interface AddResourceData {
  kind: string;
  source: string;
  title: string;
  body: string;
  external_id?: string;
  author?: string;
  url?: string;
  metadata?: Record<string, unknown>;
  payload?: Record<string, unknown>;
}

export interface SubmitDistillationData {
  question: string;
  answer: string;
  confidence: Confidence;
  conditions?: string;
  supersedes_id?: number;
  cites: Cite[];
}

export interface ContributeRequest {
  action: ActionKind;
  data: AddResourceData | SubmitDistillationData;
}

export interface ValidationError {
  error: "validation";
  detail: string;
}

export interface DuplicateResult {
  error: "duplicate";
  existing_id: number;
  detail: string;
}

export interface UnauthorizedResult {
  error: "unauthorized";
}

export interface CheckDuplicateInput {
  existing_id: number;
  similarity: number;
}

export interface ParsedContributorKey {
  raw: string;       // the header value as received
  key: string;       // the extracted 64-hex-char key (uppercased, stripped prefix)
  hash_hex: string;  // placeholder — actual hashing is in the entrypoint
}

// ---- Constants ----

export const CONTRIBUTOR_KEY_PREFIX = "hm_";
export const CONTRIBUTOR_KEY_HEX_LENGTH = 64;
export const DUPLICATE_SIMILARITY_THRESHOLD = 0.6;
export const VALID_CONFIDENCE_VALUES: readonly Confidence[] = ["high", "medium", "low"] as const;
export const VALID_CITE_ITEM_KINDS: readonly CiteItemKind[] = ["message", "resource", "distillation"] as const;
export const VALID_ACTIONS: readonly ActionKind[] = ["add_resource", "submit_distillation"] as const;

// ---- Request validation ----

/**
 * Validate the top-level shape of a contribute request body.
 * Returns a ValidationError if anything is wrong, null otherwise.
 */
export function validateRequest(body: unknown): ValidationError | null {
  if (body === null || body === undefined) {
    return { error: "validation", detail: "request body is required" };
  }
  if (typeof body !== "object" || Array.isArray(body)) {
    return { error: "validation", detail: "request body must be a JSON object" };
  }

  const req = body as Record<string, unknown>;

  if (typeof req.action !== "string") {
    return { error: "validation", detail: "field 'action' is required and must be a string" };
  }
  if (!(VALID_ACTIONS as readonly string[]).includes(req.action)) {
    return { error: "validation", detail: `invalid action '${req.action}'; must be one of: ${VALID_ACTIONS.join(", ")}` };
  }

  if (req.data === null || req.data === undefined || typeof req.data !== "object" || Array.isArray(req.data)) {
    return { error: "validation", detail: "field 'data' is required and must be a JSON object" };
  }

  return null; // Top-level shape is valid
}

/**
 * Validate add_resource data fields.
 */
export function validateAddResourceData(data: Record<string, unknown>): ValidationError | null {
  if (typeof data.kind !== "string" || data.kind.trim().length === 0) {
    return { error: "validation", detail: "data.kind is required and must be a non-empty string" };
  }
  if (typeof data.source !== "string" || data.source.trim().length === 0) {
    return { error: "validation", detail: "data.source is required and must be a non-empty string" };
  }
  if (typeof data.title !== "string" || data.title.trim().length === 0) {
    return { error: "validation", detail: "data.title is required and must be a non-empty string" };
  }
  if (typeof data.body !== "string" || data.body.trim().length === 0) {
    return { error: "validation", detail: "data.body is required and must be a non-empty string" };
  }

  // Optional fields: validate types if present
  if (data.external_id !== undefined && data.external_id !== null && typeof data.external_id !== "string") {
    return { error: "validation", detail: "data.external_id must be a string when provided" };
  }
  if (data.author !== undefined && data.author !== null && typeof data.author !== "string") {
    return { error: "validation", detail: "data.author must be a string when provided" };
  }
  if (data.url !== undefined && data.url !== null && typeof data.url !== "string") {
    return { error: "validation", detail: "data.url must be a string when provided" };
  }
  if (data.metadata !== undefined && data.metadata !== null && (typeof data.metadata !== "object" || Array.isArray(data.metadata))) {
    return { error: "validation", detail: "data.metadata must be a JSON object when provided" };
  }
  if (data.payload !== undefined && data.payload !== null && (typeof data.payload !== "object" || Array.isArray(data.payload))) {
    return { error: "validation", detail: "data.payload must be a JSON object when provided" };
  }

  return null;
}

/**
 * Validate submit_distillation data fields.
 */
export function validateSubmitDistillationData(data: Record<string, unknown>): ValidationError | null {
  // Required: question
  if (typeof data.question !== "string" || data.question.trim().length === 0) {
    return { error: "validation", detail: "data.question is required and must be a non-empty string" };
  }
  // Required: answer
  if (typeof data.answer !== "string" || data.answer.trim().length === 0) {
    return { error: "validation", detail: "data.answer is required and must be a non-empty string" };
  }
  // Required: confidence
  if (typeof data.confidence !== "string" || !(VALID_CONFIDENCE_VALUES as readonly string[]).includes(data.confidence)) {
    return { error: "validation", detail: `data.confidence is required and must be one of: ${VALID_CONFIDENCE_VALUES.join(", ")}` };
  }
  // Required: cites
  if (!Array.isArray(data.cites) || data.cites.length === 0) {
    return { error: "validation", detail: "data.cites is required and must be a non-empty array of {item_kind, item_id}" };
  }

  // Validate each cite
  for (let i = 0; i < data.cites.length; i++) {
    const cite = data.cites[i];
    if (cite === null || typeof cite !== "object" || Array.isArray(cite)) {
      return { error: "validation", detail: `data.cites[${i}] must be an object with item_kind and item_id` };
    }
    const c = cite as Record<string, unknown>;
    if (typeof c.item_kind !== "string" || !(VALID_CITE_ITEM_KINDS as readonly string[]).includes(c.item_kind)) {
      return { error: "validation", detail: `data.cites[${i}].item_kind must be one of: ${VALID_CITE_ITEM_KINDS.join(", ")}` };
    }
    // item_id: string of digits preferred (bigint-safe); small JSON numbers
    // tolerated. Numbers above MAX_SAFE_INTEGER are rejected outright — by the
    // time JSON.parse hands them to us they have already been rounded.
    if (typeof c.item_id === "string") {
      if (!/^[1-9][0-9]{0,18}$/.test(c.item_id)) {
        return { error: "validation", detail: `data.cites[${i}].item_id must be a string of digits (positive integer)` };
      }
    } else if (typeof c.item_id === "number") {
      if (!Number.isInteger(c.item_id) || c.item_id < 1) {
        return { error: "validation", detail: `data.cites[${i}].item_id must be a positive integer` };
      }
      if (c.item_id > Number.MAX_SAFE_INTEGER) {
        return { error: "validation", detail: `data.cites[${i}].item_id exceeds JSON-safe integer range — send it as a string` };
      }
      c.item_id = String(c.item_id);
    } else {
      return { error: "validation", detail: `data.cites[${i}].item_id must be a positive integer (string of digits preferred)` };
    }
  }

  // Optional fields: validate types
  if (data.conditions !== undefined && data.conditions !== null && typeof data.conditions !== "string") {
    return { error: "validation", detail: "data.conditions must be a string when provided" };
  }
  if (data.supersedes_id !== undefined && data.supersedes_id !== null) {
    if (typeof data.supersedes_id !== "number" || !Number.isFinite(data.supersedes_id) || !Number.isInteger(data.supersedes_id) || data.supersedes_id < 1) {
      return { error: "validation", detail: "data.supersedes_id must be a positive integer when provided" };
    }
  }

  return null;
}

/**
 * Full validation: top-level shape + action-specific data.
 */
export function validateContributeRequest(body: unknown): ValidationError | null {
  const topError = validateRequest(body);
  if (topError) return topError;

  const req = body as Record<string, unknown>;
  const data = req.data as Record<string, unknown>;
  const action = req.action as ActionKind;

  if (action === "add_resource") {
    return validateAddResourceData(data);
  } else {
    return validateSubmitDistillationData(data);
  }
}

// ---- Contributor key parsing ----

/**
 * Parse the X-Contributor-Key header value.
 * Returns null if the format is invalid (missing prefix, wrong hex length, non-hex chars).
 */
export function parseContributorKey(headerValue: string | null): ParsedContributorKey | null {
  if (!headerValue || typeof headerValue !== "string") {
    return null;
  }

  const trimmed = headerValue.trim();
  if (!trimmed.startsWith(CONTRIBUTOR_KEY_PREFIX)) {
    return null;
  }

  const keyPart = trimmed.slice(CONTRIBUTOR_KEY_PREFIX.length);
  if (keyPart.length !== CONTRIBUTOR_KEY_HEX_LENGTH) {
    return null;
  }

  // Must be exactly 64 lowercase hex characters
  const hexRegex = /^[0-9a-f]{64}$/;
  if (!hexRegex.test(keyPart)) {
    return null;
  }

  return {
    raw: trimmed,
    key: keyPart,
    hash_hex: "", // The entrypoint computes the real hash via SubtleCrypto
  };
}

/**
 * Validate that a contributor key header is present and well-formed.
 * Returns a structured error for the entrypoint to use, or null.
 */
export function validateContributorKeyHeader(headerValue: string | null): ValidationError | null {
  if (!headerValue) {
    return { error: "validation", detail: "X-Contributor-Key header is required" };
  }
  const parsed = parseContributorKey(headerValue);
  if (!parsed) {
    return { error: "validation", detail: `X-Contributor-Key must be '${CONTRIBUTOR_KEY_PREFIX}<64 hex chars>'` };
  }
  return null;
}

// ---- Duplicate decision helpers ----

/**
 * Determine whether a duplicate check result from the RPC
 * constitutes a real duplicate conflict.
 *
 * A conflict exists when:
 *  - A matching row was found (input is not null)
 *  - AND similarity > 0.6 (strictly greater than threshold)
 *  - AND the matching row's id is not the supersedes_id (when provided)
 *
 * Returns true if a conflict exists, false if the submission can proceed.
 */
export function isDuplicateConflict(
  checkResult: CheckDuplicateInput | null,
  supersedesId: number | null | undefined,
): boolean {
  if (!checkResult) return false;
  if (checkResult.similarity <= DUPLICATE_SIMILARITY_THRESHOLD) return false;
  if (supersedesId != null && checkResult.existing_id === supersedesId) return false;
  return true;
}

// ---- Duplicate response formatter ----

/**
 * Format the 409 duplicate response body.
 */
export function formatDuplicateResponse(existingId: number): DuplicateResult {
  return {
    error: "duplicate",
    existing_id: existingId,
    detail: "similar question exists — extend or supersede it",
  };
}

// ---- Response constructors (used by the entrypoint) ----

export function formatCreatedResponse(id: number): { id: number; status: string } {
  return { id, status: "ok" };
}

export function formatUnauthorizedResponse(): UnauthorizedResult {
  return { error: "unauthorized" };
}
