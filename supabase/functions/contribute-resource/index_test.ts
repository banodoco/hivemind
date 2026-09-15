// Smoke tests for the anonymous resource-contribution edge function.
// Run: deno test supabase/functions/contribute-resource/index_test.ts

import { assertEquals, assertExists } from "https://deno.land/std@0.208.0/assert/mod.ts";
import { validateAddResourceData } from "../contribute/protocol.ts";

function workflowSemantics() {
  return {
    workflow_semantics_version: 1,
    workflow_semantics: {
      media_type: "video",
      task_type: "image_to_video",
      model_families: ["ltx"],
      adapter_directions: [
        { from: ["image"], to: "video", confidence: "deterministic" },
      ],
      node_types: ["LoadImage"],
      node_class_multiset: { LoadImage: 1 },
      custom_nodes: [],
      models: ["ltxv.safetensors"],
      searchable_aliases: ["ltx", "i2v"],
      evidence: {
        derived_from: ["workflow_json", "summary"],
        confidence: "high",
      },
      promotion_gates: {
        has_workflow_json: true,
        has_compiled_api: false,
        has_python_source: false,
        parseable_workflow: true,
      },
    },
  };
}

Deno.test("validateAddResourceData accepts valid workflow payload", () => {
  const err = validateAddResourceData({
    kind: "workflow",
    source: "vibecomfy-external",
    title: "Test workflow",
    body: "Description: test.",
    external_id: "vibecomfy:external_workflow:abc",
    url: "file://external_workflows/corpus/abc.json",
    metadata: { summary: { title: "Test workflow" }, ...workflowSemantics() },
    payload: { corpus_path: "external_workflows/corpus/abc.json" },
  });
  assertEquals(err, null);
});

Deno.test("validateAddResourceData rejects VibeComfy workflow without semantics", () => {
  const err = validateAddResourceData({
    kind: "workflow",
    source: "vibecomfy-external",
    title: "Test workflow",
    body: "Description: test.",
    metadata: { summary: { title: "Test workflow" } },
  });
  assertExists(err);
  assertEquals(
    err?.detail,
    "metadata.workflow_semantics_version must be 1 for VibeComfy workflow resources",
  );
});

Deno.test("validateAddResourceData accepts explicit legacy semantics bypass", () => {
  const err = validateAddResourceData({
    kind: "workflow",
    source: "vibecomfy",
    title: "Legacy workflow",
    body: "Description: legacy.",
    metadata: { workflow_semantics_bypass: "legacy" },
  });
  assertEquals(err, null);
});

Deno.test("validateAddResourceData rejects missing title", () => {
  const err = validateAddResourceData({
    kind: "workflow",
    source: "vibecomfy-external",
    body: "Description: test.",
  });
  assertExists(err);
  assertEquals(err?.error, "validation");
  assertEquals(err?.detail, "data.title is required and must be a non-empty string");
});

Deno.test("validateAddResourceData rejects disallowed metadata type", () => {
  const err = validateAddResourceData({
    kind: "workflow",
    source: "vibecomfy-external",
    title: "T",
    body: "B",
    metadata: "not-an-object",
  });
  assertExists(err);
  assertEquals(err?.detail, "data.metadata must be a JSON object when provided");
});
