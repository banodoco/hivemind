// ============================================================
// VibeComfy rating edge function - pure protocol logic
//
// This module does not read environment variables or perform I/O. The
// entrypoint supplies configured limits and handles Supabase writes.
// ============================================================

export interface VibeComfyRatingRequest {
  response_id: string;
  session_id: string;
  turn_id: string;
  rating: number;
  comment?: string | null;
  pack_shared: boolean;
  pack_comment?: string | null;
  pack_zip_base64?: string | null;
}

export interface ValidatedVibeComfyRatingRequest {
  response_id: string;
  session_id: string;
  turn_id: string;
  rating: number;
  comment: string | null;
  pack_shared: boolean;
  pack_comment: string | null;
  pack_zip_base64: string | null;
  pack_zip_bytes: Uint8Array | null;
}

export interface ValidationError {
  error: "validation";
  detail: string;
}

export interface ParsedContributorKey {
  raw: string;
  key: string;
  hash_hex: string;
}

export interface ValidateRequestOptions {
  maxZipBytes?: number | null;
}

export const CONTRIBUTOR_KEY_PREFIX = "hm_";
export const CONTRIBUTOR_KEY_HEX_LENGTH = 64;
export const RESPONSE_ID_SEPARATOR = "/";
export const MIN_RATING = 1;
export const MAX_RATING = 10;
export const MAX_COMMENT_LENGTH = 2000;
export const MAX_PACK_COMMENT_LENGTH = 2000;
export const DEFAULT_MAX_ZIP_BYTES = 10 * 1024 * 1024;

const ID_PART_REGEX = /^[A-Za-z0-9._~-]+$/;
const BASE64_REGEX = /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;

export function getMaxZipBytes(configuredValue: string | number | null | undefined): number {
  if (configuredValue === null || configuredValue === undefined || configuredValue === "") {
    return DEFAULT_MAX_ZIP_BYTES;
  }

  const parsed = typeof configuredValue === "number" ? configuredValue : Number(configuredValue);
  if (!Number.isSafeInteger(parsed) || parsed < 1) {
    return DEFAULT_MAX_ZIP_BYTES;
  }
  return parsed;
}

export function parseContributorKey(headerValue: string | null): ParsedContributorKey | null {
  if (typeof headerValue !== "string") {
    return null;
  }

  if (!headerValue.startsWith(CONTRIBUTOR_KEY_PREFIX)) {
    return null;
  }

  const keyPart = headerValue.slice(CONTRIBUTOR_KEY_PREFIX.length);
  if (!/^[0-9a-f]{64}$/.test(keyPart)) {
    return null;
  }

  return {
    raw: headerValue,
    key: keyPart,
    hash_hex: "",
  };
}

export function validateContributorKeyHeader(headerValue: string | null): ValidationError | null {
  if (!headerValue) {
    return { error: "validation", detail: "X-Contributor-Key header is required" };
  }
  if (!parseContributorKey(headerValue)) {
    return { error: "validation", detail: "X-Contributor-Key must be 'hm_<64 lowercase hex chars>'" };
  }
  return null;
}

export function validateRequest(
  body: unknown,
  options: ValidateRequestOptions = {},
): { value: ValidatedVibeComfyRatingRequest; error: null } | { value: null; error: ValidationError } {
  if (body === null || body === undefined) {
    return invalid("request body is required");
  }
  if (typeof body !== "object" || Array.isArray(body)) {
    return invalid("request body must be a JSON object");
  }

  const req = body as Record<string, unknown>;
  const responseId = readRequiredString(req, "response_id");
  if (responseId.error) return invalid(responseId.error);

  const sessionId = readRequiredString(req, "session_id");
  if (sessionId.error) return invalid(sessionId.error);

  const turnId = readRequiredString(req, "turn_id");
  if (turnId.error) return invalid(turnId.error);

  const idError = validateIds(responseId.value, sessionId.value, turnId.value);
  if (idError) return invalid(idError);

  if (typeof req.rating !== "number" || !Number.isInteger(req.rating)) {
    return invalid("field 'rating' is required and must be an integer");
  }
  if (req.rating < MIN_RATING || req.rating > MAX_RATING) {
    return invalid(`field 'rating' must be between ${MIN_RATING} and ${MAX_RATING}`);
  }

  const comment = readOptionalString(req, "comment", MAX_COMMENT_LENGTH);
  if (comment.error) return invalid(comment.error);

  if (typeof req.pack_shared !== "boolean") {
    return invalid("field 'pack_shared' is required and must be a boolean");
  }

  const packComment = readOptionalString(req, "pack_comment", MAX_PACK_COMMENT_LENGTH);
  if (packComment.error) return invalid(packComment.error);

  const packZip = readOptionalString(req, "pack_zip_base64", Number.MAX_SAFE_INTEGER);
  if (packZip.error) return invalid(packZip.error);

  if (!req.pack_shared) {
    if (packZip.value !== null) {
      return invalid("field 'pack_zip_base64' is only allowed when pack_shared is true");
    }
    if (packComment.value !== null) {
      return invalid("field 'pack_comment' is only allowed when pack_shared is true");
    }
  } else if (packZip.value === null || packZip.value.length === 0) {
    return invalid("field 'pack_zip_base64' is required when pack_shared is true");
  }

  const maxZipBytes = options.maxZipBytes ?? DEFAULT_MAX_ZIP_BYTES;
  if (!Number.isSafeInteger(maxZipBytes) || maxZipBytes < 1) {
    return invalid("configured max ZIP byte limit must be a positive safe integer");
  }

  let packZipBytes: Uint8Array | null = null;
  if (packZip.value !== null) {
    const decoded = decodeStrictBase64(packZip.value);
    if (decoded === null) {
      return invalid("field 'pack_zip_base64' must be valid base64");
    }
    if (decoded.byteLength > maxZipBytes) {
      return invalid(`field 'pack_zip_base64' decodes to more than ${maxZipBytes} bytes`);
    }
    if (!hasZipSignature(decoded)) {
      return invalid("field 'pack_zip_base64' must decode to a ZIP file");
    }
    packZipBytes = decoded;
  }

  return {
    value: {
      response_id: responseId.value,
      session_id: sessionId.value,
      turn_id: turnId.value,
      rating: req.rating,
      comment: comment.value,
      pack_shared: req.pack_shared,
      pack_comment: packComment.value,
      pack_zip_base64: packZip.value,
      pack_zip_bytes: packZipBytes,
    },
    error: null,
  };
}

function validateIds(responseId: string, sessionId: string, turnId: string): string | null {
  if (!ID_PART_REGEX.test(sessionId)) {
    return "field 'session_id' must be a non-empty URL-safe id";
  }
  if (!ID_PART_REGEX.test(turnId)) {
    return "field 'turn_id' must be a non-empty URL-safe id";
  }
  const expectedResponseId = `${sessionId}${RESPONSE_ID_SEPARATOR}${turnId}`;
  if (responseId !== expectedResponseId) {
    return "field 'response_id' must be formatted as '<session_id>/<turn_id>'";
  }
  return null;
}

function readRequiredString(req: Record<string, unknown>, field: string): { value: string; error: null } | { value: null; error: string } {
  const value = req[field];
  if (typeof value !== "string" || value.length === 0) {
    return { value: null, error: `field '${field}' is required and must be a non-empty string` };
  }
  return { value, error: null };
}

function readOptionalString(
  req: Record<string, unknown>,
  field: string,
  maxLength: number,
): { value: string | null; error: null } | { value: null; error: string } {
  const value = req[field];
  if (value === undefined || value === null) {
    return { value: null, error: null };
  }
  if (typeof value !== "string") {
    return { value: null, error: `field '${field}' must be a string when provided` };
  }
  if (value.length > maxLength) {
    return { value: null, error: `field '${field}' must be at most ${maxLength} characters` };
  }
  return { value, error: null };
}

function decodeStrictBase64(value: string): Uint8Array | null {
  if (value.length === 0 || value.length % 4 !== 0 || !BASE64_REGEX.test(value)) {
    return null;
  }

  try {
    const binary = atob(value);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) {
      bytes[i] = binary.charCodeAt(i);
    }
    return bytes;
  } catch {
    return null;
  }
}

function hasZipSignature(bytes: Uint8Array): boolean {
  if (bytes.byteLength < 4 || bytes[0] !== 0x50 || bytes[1] !== 0x4b) {
    return false;
  }
  const signature = `${bytes[2]},${bytes[3]}`;
  return signature === "3,4" || signature === "5,6" || signature === "7,8";
}

function invalid(detail: string): { value: null; error: ValidationError } {
  return { value: null, error: { error: "validation", detail } };
}
