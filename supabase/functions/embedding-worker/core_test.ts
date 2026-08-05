// OFFLINE tests for the embedding-worker core (task 2.17 / T3).
//
// Fake Rpc transport + deterministic fake embed transport. No network. The only
// non-core import is the local stdlib-only ./test_assert.ts shim, so the suite
// runs under `deno test --no-config` without fetching jsr:/npm modules.
//
// Covers: payload-only (one embed), quarantined (zero embed + finalize drop),
// unavailable (zero embed + finalize drop), wrong contract_id (fail closed,
// zero embed), wrong-count/non-finite/wrong-dimension (fail closed before
// finalize), transient retry then success, permanent/exhaustion, drop job,
// source-change (no stale authority), crash/lease (recover requeues).

import {
  EmbeddingError,
  EMBEDDING_DIMENSION,
  SELECTED_CONTRACT,
  type EmbedTransport,
} from "./protocol.ts";
import { runOnce, type Rpc, type WorkerConfig } from "./core.ts";
import { assertEquals, assertTrue } from "./test_assert.ts";

const SEL = SELECTED_CONTRACT.selectedContractId;
const VEC = Array(EMBEDDING_DIMENSION).fill(0.057);

interface FakeDb {
  calls: { name: string; params: Record<string, unknown> }[];
  finalizeOutcome: string;
  embedFailuresBeforeSuccess: number;
  embedBehavior: "ok" | "wrong_count" | "non_finite" | "wrong_dim" | "throw_transient";
  payloadRowsFor: (rep: string) => any[];
  embedCallCount: number;
  claimedJobs: any[];
}

let nextJobId = 1000;

function buildConfig(over: Partial<WorkerConfig> = {}): WorkerConfig {
  return {
    workerId: "test-worker",
    apiKey: "test-key",
    contract: SELECTED_CONTRACT,
    batchSize: 4,
    leaseSeconds: 60,
    embedMaxAttempts: 3,
    embedBackoffMs: 0,
    recoverGraceSeconds: 0,
    now: () => 0,
    sleep: async () => {},
    ...over,
  } as WorkerConfig;
}

function makeEmbedTransport(db: FakeDb): EmbedTransport {
  return async (_url, _headers, body) => {
    db.embedCallCount += 1;
    if (db.embedFailuresBeforeSuccess > 0) {
      db.embedFailuresBeforeSuccess -= 1;
      throw new Error("transient network error"); // embedTexts wraps as transport EmbeddingError
    }
    if (db.embedBehavior === "throw_transient") throw new Error("transient network error");
    let data: any[];
    switch (db.embedBehavior) {
      case "wrong_count":
        data = [];
        break;
      case "non_finite":
        data = [{ index: 0, embedding: Array(EMBEDDING_DIMENSION).fill(Infinity) }];
        break;
      case "wrong_dim":
        data = [{ index: 0, embedding: [0.1, 0.2] }];
        break;
      default:
        data = (body.input as string[]).map((_, index) => ({ index, embedding: VEC }));
    }
    return { data };
  };
}

function makeRpc(db: FakeDb): Rpc {
  return async <T = unknown>(name: string, params: Record<string, unknown>): Promise<T> => {
    db.calls.push({ name, params });
    switch (name) {
      case "hivemind_recover_stale_leases":
        return 1 as T;
      case "hivemind_claim_embedding_jobs":
        return db.claimedJobs as T;
      case "hivemind_embedding_payload":
        return db.payloadRowsFor(params.p_representation_type as string) as T;
      case "hivemind_finalize_embedding_job":
        return {
          chunks_written: (params.p_chunks as any[]).length,
          outcome: db.finalizeOutcome,
          job_status: db.finalizeOutcome === "source_changed" ? "pending" : "done",
        } as T;
      case "hivemind_fail_embedding_job":
        return { ok: true } as T;
      case "hivemind_complete_embedding_job":
        return { ok: true } as T;
      case "hivemind_drop_embedding_chunks":
        return 1 as T;
      default:
        throw new Error(`unexpected rpc ${name}`);
    }
  };
}

function payloadRow(rep: string, safe: boolean, available: boolean): any {
  return {
    representation_hash: "h",
    source_available: available,
    public_state: rep === "workflow_python" ? (safe ? "safe" : "quarantined") : null,
    chunk_index: 0,
    chunk_text: available ? "alpha beta" : null,
    chunk_hash: "c",
    method: "ast",
  };
}

function job(rep = "prose", contractId: string = SEL, kind = "embed"): any {
  return {
    job_id: nextJobId++,
    entity_type: "resource", item_id: "1", representation_type: rep, job_kind: kind,
    contract_id: contractId, attempts: 1,
  };
}

Deno.test("payload-only: eligible chunks -> one embed call, finalize completed", async () => {
  const db: FakeDb = {
    calls: [], finalizeOutcome: "completed", embedFailuresBeforeSuccess: 0, embedBehavior: "ok",
    embedCallCount: 0, payloadRowsFor: () => [payloadRow("prose", true, true)], claimedJobs: [job()],
  };
  const out = await runOnce({ rpc: makeRpc(db), config: buildConfig({ transport: makeEmbedTransport(db) }) });
  assertEquals(db.embedCallCount, 1);
  assertEquals(out.embedded, 1); // one payload chunk -> one provider result
  assertEquals(out.completed, 1);
  assertEquals(out.failed, 0);
  const fin = db.calls.find((c) => c.name === "hivemind_finalize_embedding_job");
  assertTrue(!!fin);
  assertEquals((fin!.params.p_chunks as any[]).length, 1);
});

Deno.test("quarantined workflow_python -> zero embeds + finalize([]) dropped", async () => {
  const db: FakeDb = {
    calls: [], finalizeOutcome: "dropped", embedFailuresBeforeSuccess: 0, embedBehavior: "ok",
    embedCallCount: 0, payloadRowsFor: () => [payloadRow("workflow_python", false, true)],
    claimedJobs: [job("workflow_python")],
  };
  const out = await runOnce({ rpc: makeRpc(db), config: buildConfig({ transport: makeEmbedTransport(db) }) });
  assertEquals(db.embedCallCount, 0);
  assertEquals(out.empty, 1);
  const fin = db.calls.find((c) => c.name === "hivemind_finalize_embedding_job");
  assertTrue(!!fin);
  assertEquals((fin!.params.p_chunks as any[]).length, 0);
});

Deno.test("unavailable source -> zero embeds + finalize([]) dropped", async () => {
  const db: FakeDb = {
    calls: [], finalizeOutcome: "dropped", embedFailuresBeforeSuccess: 0, embedBehavior: "ok",
    embedCallCount: 0, payloadRowsFor: () => [payloadRow("prose", true, false)], claimedJobs: [job()],
  };
  const out = await runOnce({ rpc: makeRpc(db), config: buildConfig({ transport: makeEmbedTransport(db) }) });
  assertEquals(db.embedCallCount, 0);
  assertEquals(out.empty, 1);
});

Deno.test("wrong contract_id -> fail CLOSED, zero embed/payload/finalize", async () => {
  const db: FakeDb = {
    calls: [], finalizeOutcome: "completed", embedFailuresBeforeSuccess: 0, embedBehavior: "ok",
    embedCallCount: 0, payloadRowsFor: () => [payloadRow("prose", true, true)],
    claimedJobs: [job("prose", "999999")],
  };
  const out = await runOnce({ rpc: makeRpc(db), config: buildConfig({ transport: makeEmbedTransport(db) }) });
  assertEquals(db.embedCallCount, 0);
  assertEquals(out.failed, 1);
  const fail = db.calls.find((c) => c.name === "hivemind_fail_embedding_job");
  assertTrue(!!fail && fail.params.p_retryable === false);
  assertTrue(!db.calls.some((c) => c.name === "hivemind_finalize_embedding_job"));
  assertTrue(!db.calls.some((c) => c.name === "hivemind_embedding_payload"));
});

for (const behavior of ["wrong_count", "non_finite", "wrong_dim"] as const) {
  Deno.test(`malformed output (${behavior}) -> fail closed before finalize`, async () => {
    const db: FakeDb = {
      calls: [], finalizeOutcome: "completed", embedFailuresBeforeSuccess: 0, embedBehavior: behavior,
      embedCallCount: 0, payloadRowsFor: () => [payloadRow("prose", true, true)], claimedJobs: [job()],
    };
    const out = await runOnce({ rpc: makeRpc(db), config: buildConfig({ transport: makeEmbedTransport(db) }) });
    assertEquals(out.failed, 1);
    const fail = db.calls.find((c) => c.name === "hivemind_fail_embedding_job");
    assertTrue(!!fail && fail.params.p_retryable === false);
    assertTrue(!db.calls.some((c) => c.name === "hivemind_finalize_embedding_job"));
  });
}

Deno.test("transient embed failure then success -> completed", async () => {
  const db: FakeDb = {
    calls: [], finalizeOutcome: "completed", embedFailuresBeforeSuccess: 2, embedBehavior: "ok",
    embedCallCount: 0, payloadRowsFor: () => [payloadRow("prose", true, true)], claimedJobs: [job()],
  };
  const out = await runOnce({
    rpc: makeRpc(db), config: buildConfig({ transport: makeEmbedTransport(db), embedMaxAttempts: 3 }),
  });
  assertEquals(db.embedCallCount, 3); // 2 transient + 1 success
  assertEquals(out.completed, 1);
  assertEquals(out.failed, 0);
});

Deno.test("permanent/exhausted transient -> fail retryable (queue-level retry)", async () => {
  const db: FakeDb = {
    calls: [], finalizeOutcome: "completed", embedFailuresBeforeSuccess: 0, embedBehavior: "throw_transient",
    embedCallCount: 0, payloadRowsFor: () => [payloadRow("prose", true, true)], claimedJobs: [job()],
  };
  const out = await runOnce({
    rpc: makeRpc(db), config: buildConfig({ transport: makeEmbedTransport(db), embedMaxAttempts: 2 }),
  });
  assertEquals(db.embedCallCount, 2);
  assertEquals(out.failed, 1);
  const fail = db.calls.find((c) => c.name === "hivemind_fail_embedding_job");
  assertTrue(!!fail && fail.params.p_retryable === true);
});

Deno.test("drop job -> drop chunks then complete", async () => {
  const db: FakeDb = {
    calls: [], finalizeOutcome: "completed", embedFailuresBeforeSuccess: 0, embedBehavior: "ok",
    embedCallCount: 0, payloadRowsFor: () => [], claimedJobs: [job("prose", SEL, "drop")],
  };
  const out = await runOnce({ rpc: makeRpc(db), config: buildConfig({ transport: makeEmbedTransport(db) }) });
  assertEquals(db.embedCallCount, 0);
  assertEquals(out.dropped, 1);
  assertTrue(db.calls.some((c) => c.name === "hivemind_drop_embedding_chunks"));
  assertTrue(db.calls.some((c) => c.name === "hivemind_complete_embedding_job"));
});

Deno.test("source-change: finalize returns source_changed, no stale authority", async () => {
  const db: FakeDb = {
    calls: [], finalizeOutcome: "source_changed", embedFailuresBeforeSuccess: 0, embedBehavior: "ok",
    embedCallCount: 0, payloadRowsFor: () => [payloadRow("prose", true, true)], claimedJobs: [job()],
  };
  const out = await runOnce({ rpc: makeRpc(db), config: buildConfig({ transport: makeEmbedTransport(db) }) });
  assertEquals(db.embedCallCount, 1); // did embed, but commit detected newer source
  assertEquals(out.completed, 1);
  assertEquals(out.failed, 0);
  assertEquals(out.embedded, 0);
  assertTrue(!db.calls.some((c) => c.name === "hivemind_drop_embedding_chunks"));
  assertTrue(!db.calls.some((c) => c.name === "hivemind_fail_embedding_job"));
});

Deno.test("crash/lease: empty claim still runs recover_stale_leases", async () => {
  const db: FakeDb = {
    calls: [], finalizeOutcome: "completed", embedFailuresBeforeSuccess: 0, embedBehavior: "ok",
    embedCallCount: 0, payloadRowsFor: () => [], claimedJobs: [],
  };
  const out = await runOnce({ rpc: makeRpc(db), config: buildConfig({ transport: makeEmbedTransport(db) }) });
  assertEquals(out.claimed, 0);
  assertEquals(db.embedCallCount, 0);
  assertTrue(db.calls.some((c) => c.name === "hivemind_recover_stale_leases"));
});

// Keep an EmbeddingError reference so the import is exercised (and the type is
// part of the worker-core contract under test).
void EmbeddingError;
