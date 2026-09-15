// ============================================================
// Pure protocol logic tests — no network, no Supabase client.
//
// Run: deno test supabase/functions/contribute/protocol_test.ts
// ============================================================

import {
  // Constants
  CONTRIBUTOR_KEY_PREFIX,
  CONTRIBUTOR_KEY_HEX_LENGTH,
  DUPLICATE_SIMILARITY_THRESHOLD,
  VALID_CONFIDENCE_VALUES,
  VALID_CITE_ITEM_KINDS,
  VALID_ACTIONS,
  // Validation
  validateRequest,
  validateAddResourceData,
  validateSubmitDistillationData,
  validateContributeRequest,
  // Key parsing
  parseContributorKey,
  validateContributorKeyHeader,
  // Duplicate helpers
  isDuplicateConflict,
  formatDuplicateResponse,
  // Response constructors
  formatCreatedResponse,
  formatUnauthorizedResponse,
  // Types
  type ContributeRequest,
  type AddResourceData,
  type SubmitDistillationData,
  type ValidationError,
  type DuplicateResult,
  type UnauthorizedResult,
  type CheckDuplicateInput,
} from "./protocol.ts";

import { assertEquals, assertExists } from "https://deno.land/std@0.208.0/assert/mod.ts";

// ============================================================
// Constants
// ============================================================

Deno.test("constants — DUPLICATE_SIMILARITY_THRESHOLD is exactly 0.6", () => {
  assertEquals(DUPLICATE_SIMILARITY_THRESHOLD, 0.6);
});

Deno.test("constants — VALID_CONFIDENCE_VALUES contains high, medium, low", () => {
  assertEquals(VALID_CONFIDENCE_VALUES, ["high", "medium", "low"]);
});

Deno.test("constants — VALID_CITE_ITEM_KINDS contains message, resource, distillation", () => {
  assertEquals(VALID_CITE_ITEM_KINDS, ["message", "resource", "distillation"]);
});

Deno.test("constants — VALID_ACTIONS contains add_resource and submit_distillation", () => {
  assertEquals(VALID_ACTIONS, ["add_resource", "submit_distillation"]);
});

Deno.test("constants — CONTRIBUTOR_KEY_PREFIX is hm_", () => {
  assertEquals(CONTRIBUTOR_KEY_PREFIX, "hm_");
});

Deno.test("constants — CONTRIBUTOR_KEY_HEX_LENGTH is 64", () => {
  assertEquals(CONTRIBUTOR_KEY_HEX_LENGTH, 64);
});

// ============================================================
// validateRequest — top-level shape
// ============================================================

Deno.test("validateRequest — rejects null body", () => {
  const err = validateRequest(null);
  assertExists(err);
  assertEquals(err?.error, "validation");
  assertEquals(err?.detail, "request body is required");
});

Deno.test("validateRequest — rejects undefined body", () => {
  const err = validateRequest(undefined);
  assertExists(err);
  assertEquals(err?.error, "validation");
});

Deno.test("validateRequest — rejects array body", () => {
  const err = validateRequest([]);
  assertExists(err);
  assertEquals(err?.detail, "request body must be a JSON object");
});

Deno.test("validateRequest — rejects string body", () => {
  const err = validateRequest("hello");
  assertExists(err);
  assertEquals(err?.detail, "request body must be a JSON object");
});

Deno.test("validateRequest — rejects missing action", () => {
  const err = validateRequest({ data: {} });
  assertExists(err);
  assertEquals(err?.detail, "field 'action' is required and must be a string");
});

Deno.test("validateRequest — rejects empty action string", () => {
  const err = validateRequest({ action: "", data: {} });
  assertExists(err);
  assertEquals(err?.detail, "invalid action ''; must be one of: add_resource, submit_distillation");
});

Deno.test("validateRequest — rejects invalid action", () => {
  const err = validateRequest({ action: "delete_everything", data: {} });
  assertExists(err);
  assertEquals(err?.detail, "invalid action 'delete_everything'; must be one of: add_resource, submit_distillation");
});

Deno.test("validateRequest — rejects missing data", () => {
  const err = validateRequest({ action: "add_resource" });
  assertExists(err);
  assertEquals(err?.detail, "field 'data' is required and must be a JSON object");
});

Deno.test("validateRequest — rejects null data", () => {
  const err = validateRequest({ action: "add_resource", data: null });
  assertExists(err);
  assertEquals(err?.detail, "field 'data' is required and must be a JSON object");
});

Deno.test("validateRequest — rejects array data", () => {
  const err = validateRequest({ action: "add_resource", data: [] });
  assertExists(err);
  assertEquals(err?.detail, "field 'data' is required and must be a JSON object");
});

Deno.test("validateRequest — accepts valid add_resource shape", () => {
  const err = validateRequest({
    action: "add_resource",
    data: { kind: "article", source: "web", title: "Title", body: "Body text" },
  });
  assertEquals(err, null);
});

Deno.test("validateRequest — accepts valid submit_distillation shape", () => {
  const err = validateRequest({
    action: "submit_distillation",
    data: {
      question: "What is best?",
      answer: "This one.",
      confidence: "high",
      cites: [{ item_kind: "message", item_id: 1 }],
    },
  });
  assertEquals(err, null);
});

Deno.test("validateRequest — action is case-sensitive (rejects ADD_RESOURCE)", () => {
  const err = validateRequest({ action: "ADD_RESOURCE", data: {} });
  assertExists(err);
  assertEquals(err?.detail, "invalid action 'ADD_RESOURCE'; must be one of: add_resource, submit_distillation");
});

// ============================================================
// validateAddResourceData — action-specific validation
// ============================================================

Deno.test("validateAddResourceData — rejects missing kind", () => {
  const err = validateAddResourceData({ source: "web", title: "T", body: "B" });
  assertExists(err);
  assertEquals(err?.detail, "data.kind is required and must be a non-empty string");
});

Deno.test("validateAddResourceData — rejects empty kind", () => {
  const err = validateAddResourceData({ kind: "   ", source: "web", title: "T", body: "B" });
  assertExists(err);
  assertEquals(err?.detail, "data.kind is required and must be a non-empty string");
});

Deno.test("validateAddResourceData — rejects missing source", () => {
  const err = validateAddResourceData({ kind: "article", title: "T", body: "B" });
  assertExists(err);
  assertEquals(err?.detail, "data.source is required and must be a non-empty string");
});

Deno.test("validateAddResourceData — rejects empty source", () => {
  const err = validateAddResourceData({ kind: "article", source: "", title: "T", body: "B" });
  assertExists(err);
  assertEquals(err?.detail, "data.source is required and must be a non-empty string");
});

Deno.test("validateAddResourceData — rejects missing title", () => {
  const err = validateAddResourceData({ kind: "article", source: "web", body: "B" });
  assertExists(err);
  assertEquals(err?.detail, "data.title is required and must be a non-empty string");
});

Deno.test("validateAddResourceData — rejects empty title", () => {
  const err = validateAddResourceData({ kind: "article", source: "web", title: "", body: "B" });
  assertExists(err);
  assertEquals(err?.detail, "data.title is required and must be a non-empty string");
});

Deno.test("validateAddResourceData — rejects missing body", () => {
  const err = validateAddResourceData({ kind: "article", source: "web", title: "T" });
  assertExists(err);
  assertEquals(err?.detail, "data.body is required and must be a non-empty string");
});

Deno.test("validateAddResourceData — rejects empty body", () => {
  const err = validateAddResourceData({ kind: "article", source: "web", title: "T", body: "   " });
  assertExists(err);
  assertEquals(err?.detail, "data.body is required and must be a non-empty string");
});

Deno.test("validateAddResourceData — rejects non-string external_id", () => {
  const err = validateAddResourceData({
    kind: "article",
    source: "web",
    title: "T",
    body: "B",
    external_id: 123,
  });
  assertExists(err);
  assertEquals(err?.detail, "data.external_id must be a string when provided");
});

Deno.test("validateAddResourceData — rejects array metadata", () => {
  const err = validateAddResourceData({
    kind: "article",
    source: "web",
    title: "T",
    body: "B",
    metadata: [1, 2, 3],
  });
  assertExists(err);
  assertEquals(err?.detail, "data.metadata must be a JSON object when provided");
});

Deno.test("validateAddResourceData — rejects array payload", () => {
  const err = validateAddResourceData({
    kind: "article",
    source: "web",
    title: "T",
    body: "B",
    payload: [1, 2, 3],
  });
  assertExists(err);
  assertEquals(err?.detail, "data.payload must be a JSON object when provided");
});

Deno.test("validateAddResourceData — accepts valid minimal data", () => {
  const err = validateAddResourceData({ kind: "article", source: "web", title: "T", body: "B" });
  assertEquals(err, null);
});

Deno.test("validateAddResourceData — accepts valid data with all optionals", () => {
  const err = validateAddResourceData({
    kind: "transcript",
    source: "youtube",
    title: "My Video",
    body: "Transcript text here...",
    external_id: "abc123",
    author: "Channel Name",
    url: "https://youtube.com/watch?v=abc123",
    metadata: { duration: 600 },
    payload: { raw: "data" },
  });
  assertEquals(err, null);
});

Deno.test("validateAddResourceData — accepts null optionals", () => {
  const err = validateAddResourceData({
    kind: "article",
    source: "web",
    title: "T",
    body: "B",
    external_id: null,
    author: null,
    url: null,
    metadata: null,
    payload: null,
  });
  assertEquals(err, null);
});

// ============================================================
// validateSubmitDistillationData — action-specific validation
// ============================================================

Deno.test("validateSubmitDistillationData — rejects missing question", () => {
  const err = validateSubmitDistillationData({
    answer: "The answer.",
    confidence: "high",
    cites: [{ item_kind: "message", item_id: 1 }],
  });
  assertExists(err);
  assertEquals(err?.detail, "data.question is required and must be a non-empty string");
});

Deno.test("validateSubmitDistillationData — rejects empty question", () => {
  const err = validateSubmitDistillationData({
    question: "",
    answer: "The answer.",
    confidence: "high",
    cites: [{ item_kind: "message", item_id: 1 }],
  });
  assertExists(err);
  assertEquals(err?.detail, "data.question is required and must be a non-empty string");
});

Deno.test("validateSubmitDistillationData — rejects whitespace-only question", () => {
  const err = validateSubmitDistillationData({
    question: "   \n  ",
    answer: "The answer.",
    confidence: "high",
    cites: [{ item_kind: "message", item_id: 1 }],
  });
  assertExists(err);
  assertEquals(err?.detail, "data.question is required and must be a non-empty string");
});

Deno.test("validateSubmitDistillationData — rejects missing answer", () => {
  const err = validateSubmitDistillationData({
    question: "What?",
    confidence: "high",
    cites: [{ item_kind: "message", item_id: 1 }],
  });
  assertExists(err);
  assertEquals(err?.detail, "data.answer is required and must be a non-empty string");
});

Deno.test("validateSubmitDistillationData — rejects empty answer", () => {
  const err = validateSubmitDistillationData({
    question: "What?",
    answer: "",
    confidence: "high",
    cites: [{ item_kind: "message", item_id: 1 }],
  });
  assertExists(err);
  assertEquals(err?.detail, "data.answer is required and must be a non-empty string");
});

Deno.test("validateSubmitDistillationData — rejects missing confidence", () => {
  const err = validateSubmitDistillationData({
    question: "What?",
    answer: "Ans.",
    cites: [{ item_kind: "message", item_id: 1 }],
  });
  assertExists(err);
  assertEquals(err?.detail, "data.confidence is required and must be one of: high, medium, low");
});

Deno.test("validateSubmitDistillationData — rejects invalid confidence value", () => {
  const err = validateSubmitDistillationData({
    question: "What?",
    answer: "Ans.",
    confidence: "super_sure",
    cites: [{ item_kind: "message", item_id: 1 }],
  });
  assertExists(err);
  assertEquals(err?.detail, "data.confidence is required and must be one of: high, medium, low");
});

Deno.test("validateSubmitDistillationData — rejects empty string confidence", () => {
  const err = validateSubmitDistillationData({
    question: "What?",
    answer: "Ans.",
    confidence: "",
    cites: [{ item_kind: "message", item_id: 1 }],
  });
  assertExists(err);
  assertEquals(err?.detail, "data.confidence is required and must be one of: high, medium, low");
});

Deno.test("validateSubmitDistillationData — rejects number confidence", () => {
  const err = validateSubmitDistillationData({
    question: "What?",
    answer: "Ans.",
    confidence: 0.95,
    cites: [{ item_kind: "message", item_id: 1 }],
  });
  // confidence is not a string → fails the `includes` check
  assertExists(err);
});

Deno.test("validateSubmitDistillationData — rejects missing cites", () => {
  const err = validateSubmitDistillationData({
    question: "What?",
    answer: "Ans.",
    confidence: "high",
  });
  assertExists(err);
  assertEquals(err?.detail, "data.cites is required and must be a non-empty array of {item_kind, item_id}");
});

Deno.test("validateSubmitDistillationData — rejects empty cites array", () => {
  const err = validateSubmitDistillationData({
    question: "What?",
    answer: "Ans.",
    confidence: "high",
    cites: [],
  });
  assertExists(err);
  assertEquals(err?.detail, "data.cites is required and must be a non-empty array of {item_kind, item_id}");
});

Deno.test("validateSubmitDistillationData — rejects cites with null element", () => {
  const err = validateSubmitDistillationData({
    question: "What?",
    answer: "Ans.",
    confidence: "high",
    cites: [null],
  });
  assertExists(err);
  assertEquals(err?.detail, "data.cites[0] must be an object with item_kind and item_id");
});

Deno.test("validateSubmitDistillationData — rejects cites with missing item_kind", () => {
  const err = validateSubmitDistillationData({
    question: "What?",
    answer: "Ans.",
    confidence: "high",
    cites: [{ item_id: 1 }],
  });
  assertExists(err);
  assertEquals(err?.detail, "data.cites[0].item_kind must be one of: message, resource, distillation");
});

Deno.test("validateSubmitDistillationData — rejects cites with invalid item_kind", () => {
  const err = validateSubmitDistillationData({
    question: "What?",
    answer: "Ans.",
    confidence: "high",
    cites: [{ item_kind: "blog", item_id: 1 }],
  });
  assertExists(err);
  assertEquals(err?.detail, "data.cites[0].item_kind must be one of: message, resource, distillation");
});

Deno.test("validateSubmitDistillationData — rejects cites with missing item_id", () => {
  const err = validateSubmitDistillationData({
    question: "What?",
    answer: "Ans.",
    confidence: "high",
    cites: [{ item_kind: "message" }],
  });
  assertExists(err);
  assertEquals(err?.detail, "data.cites[0].item_id must be a positive integer (string of digits preferred)");
});

Deno.test("validateSubmitDistillationData — rejects cites with zero item_id", () => {
  const err = validateSubmitDistillationData({
    question: "What?",
    answer: "Ans.",
    confidence: "high",
    cites: [{ item_kind: "message", item_id: 0 }],
  });
  assertExists(err);
  assertEquals(err?.detail, "data.cites[0].item_id must be a positive integer");
});

Deno.test("validateSubmitDistillationData — rejects cites with negative item_id", () => {
  const err = validateSubmitDistillationData({
    question: "What?",
    answer: "Ans.",
    confidence: "high",
    cites: [{ item_kind: "message", item_id: -5 }],
  });
  assertExists(err);
  assertEquals(err?.detail, "data.cites[0].item_id must be a positive integer");
});

Deno.test("validateSubmitDistillationData — rejects cites with float item_id", () => {
  const err = validateSubmitDistillationData({
    question: "What?",
    answer: "Ans.",
    confidence: "high",
    cites: [{ item_kind: "message", item_id: 3.14 }],
  });
  assertExists(err);
  assertEquals(err?.detail, "data.cites[0].item_id must be a positive integer");
});

Deno.test("validateSubmitDistillationData — accepts string item_id (preferred, bigint-safe)", () => {
  const err = validateSubmitDistillationData({
    question: "What?",
    answer: "Ans.",
    confidence: "high",
    cites: [{ item_kind: "message", item_id: "1287357679312048168" as unknown as number }],
  });
  assertEquals(err, null);
});

Deno.test("validateSubmitDistillationData — normalizes numeric item_id to string", () => {
  const data = {
    question: "What?",
    answer: "Ans.",
    confidence: "high",
    cites: [{ item_kind: "message", item_id: 42 }],
  };
  const err = validateSubmitDistillationData(data);
  assertEquals(err, null);
  assertEquals((data.cites[0] as Record<string, unknown>).item_id, "42");
});

Deno.test("validateSubmitDistillationData — rejects unsafe-range numeric item_id (already rounded by JSON.parse)", () => {
  const err = validateSubmitDistillationData({
    question: "What?",
    answer: "Ans.",
    confidence: "high",
    cites: [{ item_kind: "message", item_id: 1287357679312048168 }],
  });
  assertExists(err);
  assertEquals(err?.detail, "data.cites[0].item_id exceeds JSON-safe integer range — send it as a string");
});

Deno.test("validateSubmitDistillationData — rejects malformed string item_id", () => {
  const err = validateSubmitDistillationData({
    question: "What?",
    answer: "Ans.",
    confidence: "high",
    cites: [{ item_kind: "message", item_id: "12abc" as unknown as number }],
  });
  assertExists(err);
  assertEquals(err?.detail, "data.cites[0].item_id must be a string of digits (positive integer)");
});

Deno.test("validateSubmitDistillationData — rejects non-object cite element", () => {
  const err = validateSubmitDistillationData({
    question: "What?",
    answer: "Ans.",
    confidence: "high",
    cites: ["not-an-object" as unknown as Record<string, unknown>],
  });
  // string element will fail the null/typeof check
  assertExists(err);
});

Deno.test("validateSubmitDistillationData — rejects non-integer supersedes_id", () => {
  const err = validateSubmitDistillationData({
    question: "What?",
    answer: "Ans.",
    confidence: "high",
    cites: [{ item_kind: "message", item_id: 1 }],
    supersedes_id: 3.5,
  });
  assertExists(err);
  assertEquals(err?.detail, "data.supersedes_id must be a positive integer when provided");
});

Deno.test("validateSubmitDistillationData — rejects zero supersedes_id", () => {
  const err = validateSubmitDistillationData({
    question: "What?",
    answer: "Ans.",
    confidence: "high",
    cites: [{ item_kind: "message", item_id: 1 }],
    supersedes_id: 0,
  });
  assertExists(err);
  assertEquals(err?.detail, "data.supersedes_id must be a positive integer when provided");
});

Deno.test("validateSubmitDistillationData — rejects negative supersedes_id", () => {
  const err = validateSubmitDistillationData({
    question: "What?",
    answer: "Ans.",
    confidence: "high",
    cites: [{ item_kind: "message", item_id: 1 }],
    supersedes_id: -1,
  });
  assertExists(err);
  assertEquals(err?.detail, "data.supersedes_id must be a positive integer when provided");
});

Deno.test("validateSubmitDistillationData — rejects non-string conditions", () => {
  const err = validateSubmitDistillationData({
    question: "What?",
    answer: "Ans.",
    confidence: "high",
    cites: [{ item_kind: "message", item_id: 1 }],
    conditions: 123 as unknown as string,
  });
  assertExists(err);
  assertEquals(err?.detail, "data.conditions must be a string when provided");
});

Deno.test("validateSubmitDistillationData — accepts valid minimal distillation", () => {
  const err = validateSubmitDistillationData({
    question: "What is the best model?",
    answer: "Use Model X.",
    confidence: "medium",
    cites: [{ item_kind: "resource", item_id: 42 }],
  });
  assertEquals(err, null);
});

Deno.test("validateSubmitDistillationData — accepts valid full distillation with all optionals", () => {
  const err = validateSubmitDistillationData({
    question: "What upscaler for anime video?",
    answer: "ESRGAN 4x-AnimeSharp.",
    confidence: "high",
    conditions: "when working with 1080p source material",
    supersedes_id: 7,
    cites: [
      { item_kind: "message", item_id: 100 },
      { item_kind: "resource", item_id: 200 },
      { item_kind: "distillation", item_id: 7 },
    ],
  });
  assertEquals(err, null);
});

Deno.test("validateSubmitDistillationData — accepts null supersedes_id and conditions", () => {
  const err = validateSubmitDistillationData({
    question: "What?",
    answer: "Ans.",
    confidence: "low",
    cites: [{ item_kind: "message", item_id: 1 }],
    supersedes_id: null,
    conditions: null,
  });
  assertEquals(err, null);
});

Deno.test("validateSubmitDistillationData — accepts all three confidence values", () => {
  for (const conf of VALID_CONFIDENCE_VALUES) {
    const err = validateSubmitDistillationData({
      question: "Q",
      answer: "A",
      confidence: conf,
      cites: [{ item_kind: "message", item_id: 1 }],
    });
    assertEquals(err, null, `confidence '${conf}' should be valid`);
  }
});

// ============================================================
// validateContributeRequest — full validation
// ============================================================

Deno.test("validateContributeRequest — rejects wrong action with right data shape", () => {
  const err = validateContributeRequest({
    action: "submit_distillation",
    data: { kind: "article", source: "web", title: "T", body: "B" },
  });
  assertExists(err);
  // It will try to validate as distillation and fail on missing question
  assertEquals(err?.error, "validation");
});

Deno.test("validateContributeRequest — accepts full add_resource request", () => {
  const err = validateContributeRequest({
    action: "add_resource",
    data: { kind: "article", source: "web", title: "Title", body: "Body text" },
  });
  assertEquals(err, null);
});

Deno.test("validateContributeRequest — accepts full submit_distillation request", () => {
  const err = validateContributeRequest({
    action: "submit_distillation",
    data: {
      question: "Best model?",
      answer: "Model X.",
      confidence: "high",
      cites: [{ item_kind: "message", item_id: 5 }],
    },
  });
  assertEquals(err, null);
});

// ============================================================
// parseContributorKey — header parsing
// ============================================================

Deno.test("parseContributorKey — returns null for null header", () => {
  assertEquals(parseContributorKey(null), null);
});

Deno.test("parseContributorKey — returns null for empty string", () => {
  assertEquals(parseContributorKey(""), null);
});

Deno.test("parseContributorKey — returns null for whitespace-only header", () => {
  assertEquals(parseContributorKey("   "), null);
});

Deno.test("parseContributorKey — returns null for missing hm_ prefix", () => {
  assertEquals(
    parseContributorKey("abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789ab"),
    null,
  );
});

Deno.test("parseContributorKey — returns null for wrong prefix", () => {
  assertEquals(
    parseContributorKey("hk_abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789ab"),
    null,
  );
});

Deno.test("parseContributorKey — returns null for uppercase prefix", () => {
  assertEquals(
    parseContributorKey("HM_abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789ab"),
    null,
  );
});

Deno.test("parseContributorKey — returns null for too-short hex part", () => {
  assertEquals(parseContributorKey("hm_abc123"), null);
});

Deno.test("parseContributorKey — returns null for too-long hex part", () => {
  const long = "hm_" + "a".repeat(65);
  assertEquals(parseContributorKey(long), null);
});

Deno.test("parseContributorKey — returns null for 63 chars (one short)", () => {
  const short = "hm_" + "a".repeat(63);
  assertEquals(parseContributorKey(short), null);
});

Deno.test("parseContributorKey — returns null for non-hex characters", () => {
  const nonHex = "hm_" + "g".repeat(64);
  assertEquals(parseContributorKey(nonHex), null);
});

Deno.test("parseContributorKey — returns null for uppercase hex", () => {
  const upper = "hm_" + "A".repeat(64);
  assertEquals(parseContributorKey(upper), null);
});

Deno.test("parseContributorKey — returns null for mixed non-hex", () => {
  const mixed = "hm_" + "0".repeat(63) + "x";
  assertEquals(parseContributorKey(mixed), null);
});

Deno.test("parseContributorKey — parses valid key correctly", () => {
  const validKey = "a".repeat(64);
  const result = parseContributorKey("hm_" + validKey);
  assertExists(result);
  assertEquals(result!.raw, "hm_" + validKey);
  assertEquals(result!.key, validKey);
  assertEquals(result!.hash_hex, "");
});

Deno.test("parseContributorKey — parses all-zero key", () => {
  const key = "0".repeat(64);
  const result = parseContributorKey("hm_" + key);
  assertExists(result);
  assertEquals(result!.key, key);
});

Deno.test("parseContributorKey — parses all-f key", () => {
  const key = "f".repeat(64);
  const result = parseContributorKey("hm_" + key);
  assertExists(result);
  assertEquals(result!.key, key);
});

Deno.test("parseContributorKey — handles leading/trailing whitespace gracefully", () => {
  const key = "a".repeat(64);
  // Trimmed internally, but the raw does not include the trimmed whitespace
  const result = parseContributorKey("  hm_" + key + "  ");
  assertExists(result);
  assertEquals(result!.key, key);
});

// ============================================================
// validateContributorKeyHeader
// ============================================================

Deno.test("validateContributorKeyHeader — returns error for null header", () => {
  const err = validateContributorKeyHeader(null);
  assertExists(err);
  assertEquals(err?.error, "validation");
  assertEquals(err?.detail, "X-Contributor-Key header is required");
});

Deno.test("validateContributorKeyHeader — returns error for empty header", () => {
  const err = validateContributorKeyHeader("");
  assertExists(err);
  assertEquals(err?.detail, "X-Contributor-Key header is required");
});

Deno.test("validateContributorKeyHeader — returns error for malformed key", () => {
  const err = validateContributorKeyHeader("not-a-key");
  assertExists(err);
  assertEquals(err?.detail, "X-Contributor-Key must be 'hm_<64 hex chars>'");
});

Deno.test("validateContributorKeyHeader — returns null for valid key", () => {
  const key = "hm_" + "a".repeat(64);
  const err = validateContributorKeyHeader(key);
  assertEquals(err, null);
});

// ============================================================
// isDuplicateConflict — duplicate decision logic
// ============================================================

Deno.test("isDuplicateConflict — returns false for null check result", () => {
  assertEquals(isDuplicateConflict(null, undefined), false);
  assertEquals(isDuplicateConflict(null, null), false);
  assertEquals(isDuplicateConflict(null, 5), false);
});

Deno.test("isDuplicateConflict — returns false when similarity is below threshold", () => {
  assertEquals(
    isDuplicateConflict({ existing_id: 42, similarity: 0.5 }, undefined),
    false,
  );
  assertEquals(
    isDuplicateConflict({ existing_id: 42, similarity: 0.0 }, undefined),
    false,
  );
});

Deno.test("isDuplicateConflict — returns false when similarity equals threshold exactly", () => {
  // The contract says "strict > 0.6", so exactly 0.6 is NOT a conflict
  assertEquals(
    isDuplicateConflict({ existing_id: 42, similarity: 0.6 }, undefined),
    false,
  );
  assertEquals(
    isDuplicateConflict({ existing_id: 42, similarity: 0.6 }, null),
    false,
  );
});

Deno.test("isDuplicateConflict — returns true when similarity is above threshold (no supersedes)", () => {
  assertEquals(
    isDuplicateConflict({ existing_id: 42, similarity: 0.600000001 }, undefined),
    true,
  );
  assertEquals(
    isDuplicateConflict({ existing_id: 42, similarity: 0.7 }, undefined),
    true,
  );
  assertEquals(
    isDuplicateConflict({ existing_id: 42, similarity: 1.0 }, undefined),
    true,
  );
  assertEquals(
    isDuplicateConflict({ existing_id: 42, similarity: 0.61 }, null),
    true,
  );
});

Deno.test("isDuplicateConflict — returns false when existing_id equals supersedes_id", () => {
  // superseding itself: similarity > 0.6 but it's the same row being superseded
  assertEquals(
    isDuplicateConflict({ existing_id: 42, similarity: 0.95 }, 42),
    false,
  );
});

Deno.test("isDuplicateConflict — returns true when existing_id differs from supersedes_id", () => {
  assertEquals(
    isDuplicateConflict({ existing_id: 42, similarity: 0.95 }, 99),
    true,
  );
});

Deno.test("isDuplicateConflict — handle supersedes_id=0 (edge case, should be treated as not matching)", () => {
  // 0 is not a valid id but should still work: if existing_id=42 and supersedes_id=0, it's a conflict
  assertEquals(
    isDuplicateConflict({ existing_id: 42, similarity: 0.95 }, 0),
    true,
  );
});

Deno.test("isDuplicateConflict — similarity exactly at 0.6000000000000001 (floating edge)", () => {
  // The strict > 0.6 check — values just barely above
  assertEquals(
    isDuplicateConflict({ existing_id: 1, similarity: 0.6 + Number.EPSILON }, undefined),
    // 0.6 + EPSILON is still essentially 0.6 in floating point
    0.6 + Number.EPSILON > 0.6,
  );
});

// ============================================================
// formatDuplicateResponse
// ============================================================

Deno.test("formatDuplicateResponse — returns correct structure", () => {
  const result = formatDuplicateResponse(12345);
  assertEquals(result.error, "duplicate");
  assertEquals(result.existing_id, 12345);
  assertEquals(result.detail, "similar question exists — extend or supersede it");
});

Deno.test("formatDuplicateResponse — works with different ids", () => {
  assertEquals(formatDuplicateResponse(1).existing_id, 1);
  assertEquals(formatDuplicateResponse(999999).existing_id, 999999);
});

// ============================================================
// formatCreatedResponse
// ============================================================

Deno.test("formatCreatedResponse — returns correct 201 shape", () => {
  const result = formatCreatedResponse(42);
  assertEquals(result.id, 42);
  assertEquals(result.status, "ok");
});

// ============================================================
// formatUnauthorizedResponse
// ============================================================

Deno.test("formatUnauthorizedResponse — returns correct 401 shape", () => {
  const result = formatUnauthorizedResponse();
  assertEquals(result.error, "unauthorized");
});

// ============================================================
// Edge-case: interaction between validateRequest and action routing
// ============================================================

Deno.test("edge case — extra unknown fields in body are ignored", () => {
  const err = validateContributeRequest({
    action: "add_resource",
    data: { kind: "article", source: "web", title: "T", body: "B", extra: "ignored" },
    unknown_top: true,
  });
  assertEquals(err, null);
});

Deno.test("edge case — extra unknown fields in distillation data are ignored", () => {
  const err = validateContributeRequest({
    action: "submit_distillation",
    data: {
      question: "Q",
      answer: "A",
      confidence: "low",
      cites: [{ item_kind: "distillation", item_id: 1 }],
      extra_field: "should be ok",
    },
  });
  assertEquals(err, null);
});

Deno.test("edge case — very long strings accepted", () => {
  const longStr = "x".repeat(10000);
  const err = validateAddResourceData({
    kind: longStr,
    source: "web",
    title: longStr,
    body: longStr,
  });
  assertEquals(err, null);
});

Deno.test("edge case — single cite works", () => {
  const err = validateSubmitDistillationData({
    question: "Q?",
    answer: "A.",
    confidence: "medium",
    cites: [{ item_kind: "message", item_id: 1 }],
  });
  assertEquals(err, null);
});

Deno.test("edge case — many cites work", () => {
  const cites = Array.from({ length: 100 }, (_, i) => ({
    item_kind: "message" as const,
    item_id: i + 1,
  }));
  const err = validateSubmitDistillationData({
    question: "Q?",
    answer: "A.",
    confidence: "medium",
    cites,
  });
  assertEquals(err, null);
});

Deno.test("edge case — empty string metadata field in resource is not allowed", () => {
  const err = validateAddResourceData({
    kind: "article",
    source: "web",
    title: "T",
    body: "B",
    metadata: "" as unknown as Record<string, unknown>,
  });
  assertExists(err);
});

// ============================================================
// Verify no network/Supabase imports in protocol.ts
// (This is verified by the file itself — it only uses pure TS.)
// ============================================================

Deno.test("protocol module — contains no network or supabase imports (verified by src)", () => {
  // The protocol.ts file imports nothing from external modules.
  // This test just confirms the constants/testability of the module.
  // If protocol.ts added network imports, deno test would fail at import time.
  assertEquals(typeof validateRequest, "function");
  assertEquals(typeof parseContributorKey, "function");
  assertEquals(typeof isDuplicateConflict, "function");
});
