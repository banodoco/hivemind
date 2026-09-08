// Active knowledge-model protocol tests.
//
// Run: deno test supabase/functions/contribute/protocol_test.ts

import {
  buildJsonDiff,
  buildTextDiff,
  buildKnowledgeRpcRequest,
  parseKnowledgeReferences,
  validateKnowledgeModelRequest,
} from "./protocol.ts";
import { assertEquals, assertThrows } from "https://deno.land/std@0.208.0/assert/mod.ts";

Deno.test("knowledge protocol keeps decimal IDs as strings and excludes code/escapes", () => {
  assertEquals(parseKnowledgeReferences(
    "[resource:42@7] [message:123@456] [revision:8] [evidence:9] " +
    "[guide](hivemind:resource:42@7) ` [resource:1]` \\[message:2]",
  ), [
    { target_kind: "resource", target_id: "42", target_version_id: "7", labelled: false },
    { target_kind: "message", target_id: "123", target_version_id: "456", labelled: false },
    { target_kind: "revision", target_id: "8", labelled: false },
    { target_kind: "evidence", target_id: "9", labelled: false },
  ]);
});

Deno.test("knowledge protocol rejects versioned revision/evidence tokens", () => {
  assertThrows(() => parseKnowledgeReferences("[revision:8@1]"));
  assertThrows(() => parseKnowledgeReferences("[evidence:9@1]"));
});

Deno.test("knowledge protocol validates a proposal envelope", () => {
  assertEquals(validateKnowledgeModelRequest({
    action: "propose_revision",
    data: {
      idempotency_token: "retry-1", resource_id: "42", base_revision_id: "7",
      kind: "workflow", title: "Guide", body: "candidate",
    },
  }), null);
});

Deno.test("diff helpers retain exact candidate material", () => {
  assertEquals(buildTextDiff("old", "new"), "--- base\n+++ candidate\n-old\n+new\n");
  assertEquals(buildJsonDiff({ nodes: 1 }, { nodes: 2 }), { base: { nodes: 1 }, candidate: { nodes: 2 } });
});

Deno.test("RPC envelopes map action fields and server-side actors safely", () => {
  assertEquals(buildKnowledgeRpcRequest("submit_resource", 17, {
    idempotency_token: "retry-1",
    kind: "guide",
    title: "A guide",
    body: "body",
    metadata: {},
    provenance: {},
  }, [{ target_kind: "resource", target_id: "42" }]), {
    p_submitter: 17,
    p_idempotency_token: "retry-1",
    p_kind: "guide",
    p_title: "A guide",
    p_body: "body",
    p_payload: undefined,
    p_metadata: {},
    p_provenance: {},
    p_origin_source: undefined,
    p_origin_external_id: undefined,
    p_rationale: undefined,
    p_references: [{ target_kind: "resource", target_id: "42" }],
  });
});
