// Pure protocol logic for the single Hivemind knowledge-model write path.

export type KnowledgeModelAction =
  | "submit_resource"
  | "propose_revision"
  | "decide_revision"
  | "mark_canonical"
  | "capture_message_snapshot"
  | "submit_evidence";
export type ActionKind = KnowledgeModelAction;

export interface ValidationError { error: "validation"; detail: string; }
export interface ParsedContributorKey { raw: string; key: string; hash_hex: string; }
export interface KnowledgeReference {
  target_kind: "resource" | "message" | "revision" | "evidence";
  target_id: string;
  target_version_id?: string;
  label?: string;
}
export interface ParsedKnowledgeReference {
  target_kind: KnowledgeReference["target_kind"];
  target_id: string;
  target_version_id?: string;
  labelled: boolean;
}

export const CONTRIBUTOR_KEY_PREFIX = "hm_";
export const CONTRIBUTOR_KEY_HEX_LENGTH = 64;
export const VALID_ACTIONS: readonly ActionKind[] = [
  "submit_resource", "propose_revision", "decide_revision", "mark_canonical",
  "capture_message_snapshot", "submit_evidence",
] as const;
export const WORKFLOW_SEMANTICS_VERSION = 1;
export const WORKFLOW_SEMANTICS_REQUIRED_SOURCES: readonly string[] = ["vibecomfy", "vibecomfy-external"] as const;
export const WORKFLOW_SEMANTICS_MEDIA_TYPES: readonly string[] = ["image", "video", "audio", "3d", "multi", "unknown"] as const;
export const WORKFLOW_SEMANTICS_TASK_TYPES: readonly string[] = [
  "text_to_image", "image_to_image", "image_to_video", "text_to_video", "video_to_video",
  "audio_to_video", "controlnet", "compositing", "inpainting", "upscale", "other", "unknown",
] as const;
export const WORKFLOW_SEMANTICS_CONFIDENCE_VALUES: readonly string[] = ["high", "medium", "low"] as const;
export const WORKFLOW_SEMANTICS_DIRECTION_CONFIDENCE_VALUES: readonly string[] = ["deterministic", "inferred", "llm", "unknown"] as const;

const REFERENCE_RE = /\[(resource|message|revision|evidence):([1-9][0-9]*)(?:@([1-9][0-9]*))?\]/g;
const DESTINATION_RE = /\[[^\]\n]*\]\(hivemind:(resource|message|revision|evidence):([1-9][0-9]*)(?:@([1-9][0-9]*))?\)/g;

function maskReferenceCode(text: string): string {
  const chars = [...text]; let fenced = false; let inline = false;
  let fenceChar = ""; let fenceLength = 0; let inlineLength = 0;
  for (let i = 0; i < chars.length; i++) {
    const delimiter = chars[i];
    if (delimiter === "`" || delimiter === "~") {
      let j = i + 1;
      while (j < chars.length && chars[j] === delimiter) j++;
      const runLength = j - i;
      if (fenced) {
        if (delimiter === fenceChar && runLength >= fenceLength) {
          fenced = false; fenceChar = ""; fenceLength = 0;
        }
        for (let k = i; k < j; k++) chars[k] = " ";
        i = j - 1; continue;
      }
      if (runLength >= 3) {
        fenced = true; fenceChar = delimiter; fenceLength = runLength;
        for (let k = i; k < j; k++) chars[k] = " ";
        i = j - 1; continue;
      }
      if (delimiter === "`") {
        if (!inline) { inline = true; inlineLength = runLength; }
        else if (runLength === inlineLength) { inline = false; inlineLength = 0; }
        for (let k = i; k < j; k++) chars[k] = " ";
        i = j - 1; continue;
      }
    }
    if (fenced) { chars[i] = " "; continue; }
    if (inline) { chars[i] = " "; continue; }
    if (chars[i] === "\\") { chars[i] = " "; if (i + 1 < chars.length) chars[++i] = " "; }
  }
  return chars.join("");
}

export function parseKnowledgeReferences(text: string): ParsedKnowledgeReference[] {
  const masked = maskReferenceCode(text);
  const results: Array<{ index: number; item: ParsedKnowledgeReference }> = [];
  const destinations: Array<[number, number]> = [];
  for (const match of masked.matchAll(DESTINATION_RE)) {
    const kind = match[1] as ParsedKnowledgeReference["target_kind"];
    if ((kind === "revision" || kind === "evidence") && match[3]) throw new Error(`${kind} references cannot carry a version`);
    results.push({ index: match.index ?? 0, item: { target_kind: kind, target_id: match[2], ...(match[3] ? { target_version_id: match[3] } : {}), labelled: true } });
    destinations.push([match.index ?? 0, (match.index ?? 0) + match[0].length]);
  }
  for (const match of masked.matchAll(REFERENCE_RE)) {
    if (destinations.some(([start, end]) => (match.index ?? 0) >= start && (match.index ?? 0) < end)) continue;
    const kind = match[1] as ParsedKnowledgeReference["target_kind"];
    if ((kind === "revision" || kind === "evidence") && match[3]) throw new Error(`${kind} references cannot carry a version`);
    results.push({ index: match.index ?? 0, item: { target_kind: kind, target_id: match[2], ...(match[3] ? { target_version_id: match[3] } : {}), labelled: false } });
  }
  results.sort((a, b) => a.index - b.index);
  const seen = new Set<string>();
  return results.flatMap(({ item }) => { const key = `${item.target_kind}:${item.target_id}@${item.target_version_id ?? ""}`; if (seen.has(key)) return []; seen.add(key); return [item]; });
}

export function buildTextDiff(base: string, candidate: string): string {
  const oldLines = base === "" ? [] : base.split("\n"); const newLines = candidate === "" ? [] : candidate.split("\n");
  return ["--- base", "+++ candidate", ...oldLines.map((line) => `-${line}`), ...newLines.map((line) => `+${line}`), ""].join("\n");
}
export function buildJsonDiff(base: unknown, candidate: unknown): Record<string, unknown> { return { base, candidate }; }

export function parseContributorKey(headerValue: string | null): ParsedContributorKey | null {
  if (!headerValue) return null;
  const trimmed = headerValue.trim(); const key = trimmed.slice(CONTRIBUTOR_KEY_PREFIX.length);
  if (!trimmed.startsWith(CONTRIBUTOR_KEY_PREFIX) || key.length !== CONTRIBUTOR_KEY_HEX_LENGTH || !/^[0-9a-f]{64}$/.test(key)) return null;
  return { raw: trimmed, key, hash_hex: "" };
}
export function validateContributorKeyHeader(headerValue: string | null): ValidationError | null {
  if (!headerValue) return { error: "validation", detail: "X-Contributor-Key header is required" };
  if (!parseContributorKey(headerValue)) return { error: "validation", detail: `X-Contributor-Key must be '${CONTRIBUTOR_KEY_PREFIX}<64 hex chars>'` };
  return null;
}

function positiveDecimal(value: unknown, field: string): ValidationError | null {
  return typeof value === "string" && /^[1-9][0-9]*$/.test(value) ? null : { error: "validation", detail: `${field} must be a positive decimal string` };
}
function optionalObject(value: unknown, field: string): ValidationError | null {
  return value === undefined || value === null || (typeof value === "object" && !Array.isArray(value)) ? null : { error: "validation", detail: `${field} must be a JSON object when provided` };
}
function stringArray(value: unknown, field: string): ValidationError | null {
  return Array.isArray(value) && value.every((item) => typeof item === "string") ? null : { error: "validation", detail: `${field} must be an array of strings` };
}
function workflowSemantics(data: Record<string, unknown>): ValidationError | null {
  if (data.kind !== "workflow" || typeof data.origin_source !== "string" || !(WORKFLOW_SEMANTICS_REQUIRED_SOURCES as readonly string[]).includes(data.origin_source)) return null;
  const metadata = data.metadata;
  if (metadata === null || typeof metadata !== "object" || Array.isArray(metadata)) return { error: "validation", detail: "workflow resources from VibeComfy require metadata.workflow_semantics" };
  const meta = metadata as Record<string, unknown>;
  if (meta.workflow_semantics_bypass === "legacy" || meta.workflow_semantics_bypass === "backfill" || meta.workflow_semantics_legacy_bypass === true) return null;
  if (meta.workflow_semantics_version !== WORKFLOW_SEMANTICS_VERSION) return { error: "validation", detail: "metadata.workflow_semantics_version must be 1 for VibeComfy workflow resources" };
  const semantics = meta.workflow_semantics;
  if (semantics === null || typeof semantics !== "object" || Array.isArray(semantics)) return { error: "validation", detail: "metadata.workflow_semantics must be a JSON object for VibeComfy workflow resources" };
  const s = semantics as Record<string, unknown>;
  if (typeof s.media_type !== "string" || !WORKFLOW_SEMANTICS_MEDIA_TYPES.includes(s.media_type)) return { error: "validation", detail: "metadata.workflow_semantics.media_type is invalid" };
  if (typeof s.task_type !== "string" || !WORKFLOW_SEMANTICS_TASK_TYPES.includes(s.task_type)) return { error: "validation", detail: "metadata.workflow_semantics.task_type is invalid" };
  for (const key of ["model_families", "node_types", "custom_nodes", "models", "searchable_aliases"]) { const err = stringArray(s[key], `metadata.workflow_semantics.${key}`); if (err) return err; }
  if (s.node_class_multiset === null || typeof s.node_class_multiset !== "object" || Array.isArray(s.node_class_multiset)) return { error: "validation", detail: "metadata.workflow_semantics.node_class_multiset must be a JSON object" };
  if (!Array.isArray(s.adapter_directions) || s.evidence === null || typeof s.evidence !== "object" || Array.isArray(s.evidence) || s.promotion_gates === null || typeof s.promotion_gates !== "object" || Array.isArray(s.promotion_gates)) return { error: "validation", detail: "metadata.workflow_semantics evidence, adapter_directions, and promotion_gates are required" };
  return null;
}

export function validateKnowledgeModelRequest(body: unknown): ValidationError | null {
  if (body === null || typeof body !== "object" || Array.isArray(body)) return { error: "validation", detail: "request body must be a JSON object" };
  const request = body as Record<string, unknown>;
  if (typeof request.action !== "string" || !VALID_ACTIONS.includes(request.action as KnowledgeModelAction)) return { error: "validation", detail: `action must be one of: ${VALID_ACTIONS.join(", ")}` };
  if (request.data === null || typeof request.data !== "object" || Array.isArray(request.data)) return { error: "validation", detail: "data must be a JSON object" };
  const data = request.data as Record<string, unknown>;
  if (typeof data.idempotency_token !== "string" || data.idempotency_token.trim() === "") return { error: "validation", detail: "data.idempotency_token is required" };
  if (request.action === "submit_resource") {
    for (const field of ["kind", "title", "body"]) if (typeof data[field] !== "string" || (data[field] as string).trim() === "") return { error: "validation", detail: `data.${field} is required and must be a non-empty string` };
    for (const field of ["metadata", "provenance", "payload"]) { const err = optionalObject(data[field], `data.${field}`); if (err) return err; }
    const semanticsError = workflowSemantics(data); if (semanticsError) return semanticsError;
    if (data.references !== undefined && !Array.isArray(data.references)) return { error: "validation", detail: "data.references must be an array" };
    try { parseKnowledgeReferences(data.body as string); } catch (error) { return { error: "validation", detail: error instanceof Error ? error.message : "invalid typed reference" }; }
  } else if (request.action === "propose_revision") {
    for (const field of ["resource_id", "base_revision_id"]) { const err = positiveDecimal(data[field], `data.${field}`); if (err) return err; }
    for (const field of ["kind", "title", "body"]) if (typeof data[field] !== "string" || (data[field] as string).trim() === "") return { error: "validation", detail: `data.${field} is required and must be a non-empty string` };
    try { parseKnowledgeReferences(data.body as string); } catch (error) { return { error: "validation", detail: error instanceof Error ? error.message : "invalid typed reference" }; }
  } else if (request.action === "decide_revision") {
    const err = positiveDecimal(data.revision_id, "data.revision_id"); if (err) return err;
    if (!["accepted", "rejected", "withdrawn"].includes(String(data.decision))) return { error: "validation", detail: "data.decision must be accepted, rejected, or withdrawn" };
  } else if (request.action === "mark_canonical") {
    const err = positiveDecimal(data.resource_id, "data.resource_id"); if (err) return err;
    if (typeof data.canonical !== "boolean") return { error: "validation", detail: "data.canonical must be a boolean" };
  } else if (request.action === "capture_message_snapshot") {
    const err = positiveDecimal(data.message_id, "data.message_id"); if (err) return err;
    if (typeof data.content !== "string") return { error: "validation", detail: "data.content must be a string" };
    const metadataError = optionalObject(data.source_metadata, "data.source_metadata"); if (metadataError) return metadataError;
  } else {
    for (const field of ["claim", "reported_result"]) if (typeof data[field] !== "string" || (data[field] as string).trim() === "") return { error: "validation", detail: `data.${field} is required and must be a non-empty string` };
    if (data.basis !== "reported" && data.basis !== "observed") return { error: "validation", detail: "data.basis must be reported or observed" };
    if (!Array.isArray(data.subjects) || data.subjects.length === 0) return { error: "validation", detail: "data.subjects must be a non-empty array" };
  }
  return null;
}

export function buildKnowledgeRpcRequest(action: KnowledgeModelAction, contributorId: number, data: Record<string, unknown>, derivedReferences?: unknown): Record<string, unknown> {
  const fields: Record<KnowledgeModelAction, string[]> = {
    submit_resource: ["p_submitter", "p_idempotency_token", "p_kind", "p_title", "p_body", "p_payload", "p_metadata", "p_provenance", "p_origin_source", "p_origin_external_id", "p_rationale", "p_references"],
    propose_revision: ["p_submitter", "p_idempotency_token", "p_resource_id", "p_base_revision_id", "p_kind", "p_title", "p_body", "p_payload", "p_metadata", "p_provenance", "p_rationale", "p_references"],
    decide_revision: ["p_editor", "p_idempotency_token", "p_revision_id", "p_decision", "p_reason"],
    mark_canonical: ["p_editor", "p_idempotency_token", "p_resource_id", "p_canonical"],
    capture_message_snapshot: ["p_submitter", "p_idempotency_token", "p_message_id", "p_content", "p_source_metadata", "p_original_author_id", "p_original_author_name", "p_observed_at"],
    submit_evidence: ["p_submitter", "p_idempotency_token", "p_claim", "p_conditions", "p_reported_result", "p_basis", "p_subjects", "p_sources", "p_supersedes_evidence_id"],
  };
  const request: Record<string, unknown> = {};
  for (const field of fields[action]) {
    if (field === "p_submitter" || field === "p_editor") request[field] = contributorId;
    else if (field === "p_idempotency_token") request[field] = data.idempotency_token;
    else if (field === "p_references") request[field] = derivedReferences ?? data.references ?? [];
    else request[field] = data[field.slice(2)];
  }
  return request;
}
