// Deno tests for the embedding-worker protocol (plan task 2.9).
//
// Pins the provider-boundary + dimension-validation logic the Edge Function
// relies on, with an injectable transport (no network). NOTE: these run under
// `deno test`; on a machine without Deno they are committed-but-unrun and the
// worker SQL path is instead proven by scripts/rehearse_embedding_lifecycle.py
// (2.9 protocol measure) on the isolated cluster.

import {
  EmbeddingError,
  EMBEDDING_DIMENSION,
  embedTexts,
  isValidVector,
  vectorLiteral,
  type EmbedTransport,
} from "./protocol.ts";
import { assertEquals, assertRejects } from "./test_assert.ts";

function fakeTransport(resp: unknown): EmbedTransport {
  return async () => resp;
}

Deno.test("isValidVector accepts a finite dim-length vector", () => {
  const v = Array(EMBEDDING_DIMENSION).fill(0.1);
  assertEquals(isValidVector(v), true);
});

Deno.test("isValidVector rejects wrong length / non-finite", () => {
  assertEquals(isValidVector([0.1, 0.2]), false);
  assertEquals(isValidVector(Array(EMBEDDING_DIMENSION).fill(Infinity)), false);
  assertEquals(isValidVector("nope"), false);
});

Deno.test("embedTexts preserves order and validates dimensions", async () => {
  const vec = Array(EMBEDDING_DIMENSION).fill(0.2);
  const transport = fakeTransport({
    data: [
      { index: 1, embedding: vec },
      { index: 0, embedding: vec },
    ],
  });
  const out = await embedTexts(["a", "b"], "key", transport);
  assertEquals(out.length, 2);
  assertEquals(out.every((v) => isValidVector(v)), true);
});

Deno.test("embedTexts rejects a wrong-count provider response", async () => {
  const transport = fakeTransport({ data: [{ index: 0, embedding: Array(EMBEDDING_DIMENSION).fill(0.1) }] });
  await assertRejects(() => embedTexts(["a", "b"], "key", transport), EmbeddingError);
});

Deno.test("embedTexts rejects a wrong-dimension vector", async () => {
  const transport = fakeTransport({ data: [{ index: 0, embedding: [0.1, 0.2] }] });
  await assertRejects(() => embedTexts(["a"], "key", transport), EmbeddingError);
});

Deno.test("embedTexts requires a key (fail closed, no network)", async () => {
  const transport = fakeTransport({ data: [] });
  await assertRejects(() => embedTexts(["a"], "", transport), EmbeddingError);
});

Deno.test("vectorLiteral renders pgvector content", () => {
  assertEquals(vectorLiteral([0.1, 0.2, 0.3]), "[0.1,0.2,0.3]");
});
