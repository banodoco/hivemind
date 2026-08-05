// ============================================================
// Pure protocol logic tests - no network, no Supabase client.
//
// Run: deno test hivemind/supabase/functions/submit-vibecomfy-rating/protocol_test.ts
// ============================================================

import {
  CONTRIBUTOR_KEY_HEX_LENGTH,
  CONTRIBUTOR_KEY_PREFIX,
  DEFAULT_MAX_ZIP_BYTES,
  MAX_COMMENT_LENGTH,
  MAX_PACK_COMMENT_LENGTH,
  MAX_RATING,
  MIN_RATING,
  getMaxZipBytes,
  parseContributorKey,
  validateContributorKeyHeader,
  validateRequest,
  type VibeComfyRatingRequest,
} from "./protocol.ts";

function assertEquals<T>(actual: T, expected: T, message?: string): void {
  if (actual !== expected) {
    throw new Error(message ?? `Expected ${String(expected)}, got ${String(actual)}`);
  }
}

function assert(condition: unknown, message = "Assertion failed"): asserts condition {
  if (!condition) {
    throw new Error(message);
  }
}

function validRequest(overrides: Partial<VibeComfyRatingRequest> = {}): VibeComfyRatingRequest {
  return {
    response_id: "session-1/turn_2",
    session_id: "session-1",
    turn_id: "turn_2",
    rating: 8,
    comment: "useful response",
    pack_shared: false,
    ...overrides,
  };
}

function expectValid(body: unknown, maxZipBytes?: number) {
  const result = validateRequest(body, { maxZipBytes });
  assertEquals(result.error, null);
  assert(result.value !== null);
  return result.value;
}

function expectInvalid(body: unknown, expectedDetail: string, maxZipBytes?: number): void {
  const result = validateRequest(body, { maxZipBytes });
  assert(result.error !== null, "expected validation error");
  assertEquals(result.error.detail, expectedDetail);
  assertEquals(result.value, null);
}

Deno.test("constants expose rating, comment, contributor key, and default ZIP limits", () => {
  assertEquals(MIN_RATING, 1);
  assertEquals(MAX_RATING, 10);
  assertEquals(MAX_COMMENT_LENGTH, 2000);
  assertEquals(MAX_PACK_COMMENT_LENGTH, 2000);
  assertEquals(DEFAULT_MAX_ZIP_BYTES, 10 * 1024 * 1024);
  assertEquals(CONTRIBUTOR_KEY_PREFIX, "hm_");
  assertEquals(CONTRIBUTOR_KEY_HEX_LENGTH, 64);
});

Deno.test("getMaxZipBytes returns configured positive safe integers or default", () => {
  assertEquals(getMaxZipBytes(undefined), DEFAULT_MAX_ZIP_BYTES);
  assertEquals(getMaxZipBytes(null), DEFAULT_MAX_ZIP_BYTES);
  assertEquals(getMaxZipBytes(""), DEFAULT_MAX_ZIP_BYTES);
  assertEquals(getMaxZipBytes("12345"), 12345);
  assertEquals(getMaxZipBytes(2048), 2048);
  assertEquals(getMaxZipBytes("0"), DEFAULT_MAX_ZIP_BYTES);
  assertEquals(getMaxZipBytes("not-a-number"), DEFAULT_MAX_ZIP_BYTES);
});

Deno.test("validateRequest accepts valid metadata-only requests", () => {
  const value = expectValid(validRequest({ comment: null }));
  assertEquals(value.response_id, "session-1/turn_2");
  assertEquals(value.pack_zip_base64, null);
  assertEquals(value.pack_zip_bytes, null);
});

Deno.test("validateRequest accepts valid pack requests and decodes ZIP bytes", () => {
  const value = expectValid(validRequest({
    pack_shared: true,
    pack_comment: "public debug pack",
    pack_zip_base64: "UEsDBAo=",
  }));
  assertEquals(value.pack_shared, true);
  assertEquals(value.pack_zip_bytes?.byteLength, 5);
});

Deno.test("validateRequest rejects non-object bodies", () => {
  expectInvalid(null, "request body is required");
  expectInvalid([], "request body must be a JSON object");
});

Deno.test("validateRequest rejects missing or malformed ids", () => {
  expectInvalid(validRequest({ response_id: "" }), "field 'response_id' is required and must be a non-empty string");
  expectInvalid(validRequest({ session_id: "bad/id" }), "field 'session_id' must be a non-empty URL-safe id");
  expectInvalid(validRequest({ turn_id: "bad id" }), "field 'turn_id' must be a non-empty URL-safe id");
  expectInvalid(validRequest({ response_id: "session-1/wrong-turn" }), "field 'response_id' must be formatted as '<session_id>/<turn_id>'");
});

Deno.test("validateRequest rejects ratings outside 1..10", () => {
  expectInvalid(validRequest({ rating: 0 }), "field 'rating' must be between 1 and 10");
  expectInvalid(validRequest({ rating: 11 }), "field 'rating' must be between 1 and 10");
  expectInvalid(validRequest({ rating: 5.5 }), "field 'rating' is required and must be an integer");
});

Deno.test("validateRequest rejects overlong comments through shared constants", () => {
  expectInvalid(
    validRequest({ comment: "a".repeat(MAX_COMMENT_LENGTH + 1) }),
    `field 'comment' must be at most ${MAX_COMMENT_LENGTH} characters`,
  );
  expectInvalid(
    validRequest({
      pack_shared: true,
      pack_comment: "a".repeat(MAX_PACK_COMMENT_LENGTH + 1),
      pack_zip_base64: "UEsDBAo=",
    }),
    `field 'pack_comment' must be at most ${MAX_PACK_COMMENT_LENGTH} characters`,
  );
});

Deno.test("validateRequest rejects inconsistent pack fields", () => {
  expectInvalid(
    validRequest({ pack_zip_base64: "UEsDBAo=" }),
    "field 'pack_zip_base64' is only allowed when pack_shared is true",
  );
  expectInvalid(
    validRequest({ pack_comment: "not sharing" }),
    "field 'pack_comment' is only allowed when pack_shared is true",
  );
  expectInvalid(
    validRequest({ pack_shared: true }),
    "field 'pack_zip_base64' is required when pack_shared is true",
  );
});

Deno.test("validateRequest rejects malformed base64 ZIP payloads", () => {
  expectInvalid(
    validRequest({ pack_shared: true, pack_zip_base64: "not base64" }),
    "field 'pack_zip_base64' must be valid base64",
  );
  expectInvalid(
    validRequest({ pack_shared: true, pack_zip_base64: "abc" }),
    "field 'pack_zip_base64' must be valid base64",
  );
  expectInvalid(
    validRequest({ pack_shared: true, pack_zip_base64: "aGVsbG8=" }),
    "field 'pack_zip_base64' must decode to a ZIP file",
  );
});

Deno.test("validateRequest rejects ZIP payloads above configured byte limit", () => {
  expectInvalid(
    validRequest({ pack_shared: true, pack_zip_base64: "UEsDBAo=" }),
    "field 'pack_zip_base64' decodes to more than 4 bytes",
    4,
  );
});

Deno.test("validateContributorKeyHeader accepts only strict lowercase hm_<64 hex>", () => {
  const key = `${CONTRIBUTOR_KEY_PREFIX}${"a".repeat(CONTRIBUTOR_KEY_HEX_LENGTH)}`;
  assertEquals(validateContributorKeyHeader(key), null);
  assertEquals(parseContributorKey(key)?.key, "a".repeat(CONTRIBUTOR_KEY_HEX_LENGTH));

  assertEquals(validateContributorKeyHeader(null)?.detail, "X-Contributor-Key header is required");
  assertEquals(validateContributorKeyHeader(`${CONTRIBUTOR_KEY_PREFIX}${"A".repeat(CONTRIBUTOR_KEY_HEX_LENGTH)}`)?.detail, "X-Contributor-Key must be 'hm_<64 lowercase hex chars>'");
  assertEquals(validateContributorKeyHeader(`${CONTRIBUTOR_KEY_PREFIX}${"a".repeat(CONTRIBUTOR_KEY_HEX_LENGTH - 1)}`)?.detail, "X-Contributor-Key must be 'hm_<64 lowercase hex chars>'");
  assertEquals(validateContributorKeyHeader(` ${key}`)?.detail, "X-Contributor-Key must be 'hm_<64 lowercase hex chars>'");
});
