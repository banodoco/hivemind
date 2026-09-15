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
export const WORKFLOW_SEMANTICS_VERSION = 1;
export const WORKFLOW_SEMANTICS_REQUIRED_SOURCES: readonly string[] = ["vibecomfy", "vibecomfy-external"] as const;
export const WORKFLOW_SEMANTICS_MEDIA_TYPES: readonly string[] = ["image", "video", "audio", "3d", "multi", "unknown"] as const;
export const WORKFLOW_SEMANTICS_TASK_TYPES: readonly string[] = [
  "text_to_image",
  "image_to_image",
  "image_to_video",
  "text_to_video",
  "video_to_video",
  "audio_to_video",
  "controlnet",
  "compositing",
  "inpainting",
  "upscale",
  "other",
  "unknown",
] as const;
export const WORKFLOW_SEMANTICS_CONFIDENCE_VALUES: readonly string[] = ["high", "medium", "low"] as const;
export const WORKFLOW_SEMANTICS_DIRECTION_CONFIDENCE_VALUES: readonly string[] = ["deterministic", "inferred", "llm", "unknown"] as const;

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

  const semanticsError = validateRequiredWorkflowSemantics(data);
  if (semanticsError) {
    return semanticsError;
  }

  return null;
}

export function requiresWorkflowSemantics(data: Record<string, unknown>): boolean {
  return data.kind === "workflow"
    && typeof data.source === "string"
    && (WORKFLOW_SEMANTICS_REQUIRED_SOURCES as readonly string[]).includes(data.source);
}

function hasWorkflowSemanticsBypass(metadata: Record<string, unknown>): boolean {
  const bypass = metadata.workflow_semantics_bypass;
  return bypass === "legacy" || bypass === "backfill" || metadata.workflow_semantics_legacy_bypass === true;
}

function validateRequiredWorkflowSemantics(data: Record<string, unknown>): ValidationError | null {
  if (!requiresWorkflowSemantics(data)) {
    return null;
  }
  const metadata = data.metadata;
  if (metadata === undefined || metadata === null || typeof metadata !== "object" || Array.isArray(metadata)) {
    return { error: "validation", detail: "workflow resources from source 'vibecomfy' or 'vibecomfy-external' require metadata.workflow_semantics" };
  }
  const meta = metadata as Record<string, unknown>;
  if (hasWorkflowSemanticsBypass(meta)) {
    return null;
  }
  if (meta.workflow_semantics_version !== WORKFLOW_SEMANTICS_VERSION) {
    return { error: "validation", detail: "metadata.workflow_semantics_version must be 1 for VibeComfy workflow resources" };
  }
  const semantics = meta.workflow_semantics;
  if (semantics === null || typeof semantics !== "object" || Array.isArray(semantics)) {
    return { error: "validation", detail: "metadata.workflow_semantics must be a JSON object for VibeComfy workflow resources" };
  }
  return validateWorkflowSemanticsObject(semantics as Record<string, unknown>);
}

export function validateWorkflowSemanticsObject(semantics: Record<string, unknown>): ValidationError | null {
  if (typeof semantics.media_type !== "string" || !(WORKFLOW_SEMANTICS_MEDIA_TYPES as readonly string[]).includes(semantics.media_type)) {
    return { error: "validation", detail: `metadata.workflow_semantics.media_type must be one of: ${WORKFLOW_SEMANTICS_MEDIA_TYPES.join(", ")}` };
  }
  if (typeof semantics.task_type !== "string" || !(WORKFLOW_SEMANTICS_TASK_TYPES as readonly string[]).includes(semantics.task_type)) {
    return { error: "validation", detail: `metadata.workflow_semantics.task_type must be one of: ${WORKFLOW_SEMANTICS_TASK_TYPES.join(", ")}` };
  }
  for (const key of ["model_families", "node_types", "custom_nodes", "models", "searchable_aliases"]) {
    const err = validateStringArray(semantics[key], `metadata.workflow_semantics.${key}`);
    if (err) return err;
  }
  if (semantics.node_class_multiset === null || typeof semantics.node_class_multiset !== "object" || Array.isArray(semantics.node_class_multiset)) {
    return { error: "validation", detail: "metadata.workflow_semantics.node_class_multiset must be a JSON object" };
  }
  for (const [key, value] of Object.entries(semantics.node_class_multiset as Record<string, unknown>)) {
    if (!key || typeof value !== "number" || !Number.isInteger(value) || value < 1) {
      return { error: "validation", detail: "metadata.workflow_semantics.node_class_multiset values must be positive integers" };
    }
  }
  const adapterError = validateAdapterDirections(semantics.adapter_directions);
  if (adapterError) return adapterError;
  const evidenceError = validateSemanticsEvidence(semantics.evidence);
  if (evidenceError) return evidenceError;
  const gateError = validatePromotionGates(semantics.promotion_gates);
  if (gateError) return gateError;
  return null;
}

function validateStringArray(value: unknown, path: string): ValidationError | null {
  if (!Array.isArray(value)) {
    return { error: "validation", detail: `${path} must be an array of strings` };
  }
  if (!value.every((item) => typeof item === "string")) {
    return { error: "validation", detail: `${path} must be an array of strings` };
  }
  return null;
}

function validateAdapterDirections(value: unknown): ValidationError | null {
  if (!Array.isArray(value)) {
    return { error: "validation", detail: "metadata.workflow_semantics.adapter_directions must be an array" };
  }
  for (let i = 0; i < value.length; i++) {
    const item = value[i];
    if (item === null || typeof item !== "object" || Array.isArray(item)) {
      return { error: "validation", detail: `metadata.workflow_semantics.adapter_directions[${i}] must be an object` };
    }
    const direction = item as Record<string, unknown>;
    const fromError = validateStringArray(direction.from, `metadata.workflow_semantics.adapter_directions[${i}].from`);
    if (fromError) return fromError;
    if (typeof direction.to !== "string" || direction.to.length === 0) {
      return { error: "validation", detail: `metadata.workflow_semantics.adapter_directions[${i}].to must be a non-empty string` };
    }
    if (typeof direction.confidence !== "string" || !(WORKFLOW_SEMANTICS_DIRECTION_CONFIDENCE_VALUES as readonly string[]).includes(direction.confidence)) {
      return { error: "validation", detail: `metadata.workflow_semantics.adapter_directions[${i}].confidence must be one of: ${WORKFLOW_SEMANTICS_DIRECTION_CONFIDENCE_VALUES.join(", ")}` };
    }
  }
  return null;
}

function validateSemanticsEvidence(value: unknown): ValidationError | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return { error: "validation", detail: "metadata.workflow_semantics.evidence must be a JSON object" };
  }
  const evidence = value as Record<string, unknown>;
  const derivedError = validateStringArray(evidence.derived_from, "metadata.workflow_semantics.evidence.derived_from");
  if (derivedError) return derivedError;
  if (typeof evidence.confidence !== "string" || !(WORKFLOW_SEMANTICS_CONFIDENCE_VALUES as readonly string[]).includes(evidence.confidence)) {
    return { error: "validation", detail: `metadata.workflow_semantics.evidence.confidence must be one of: ${WORKFLOW_SEMANTICS_CONFIDENCE_VALUES.join(", ")}` };
  }
  return null;
}

function validatePromotionGates(value: unknown): ValidationError | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return { error: "validation", detail: "metadata.workflow_semantics.promotion_gates must be a JSON object" };
  }
  const gates = value as Record<string, unknown>;
  for (const key of ["has_workflow_json", "has_compiled_api", "has_python_source", "parseable_workflow"]) {
    if (typeof gates[key] !== "boolean") {
      return { error: "validation", detail: `metadata.workflow_semantics.promotion_gates.${key} must be a boolean` };
    }
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
