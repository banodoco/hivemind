// Hivemind `embedding-worker` Edge Function — THIN ADAPTER (task 2.17 / I6).
//
// All job orchestration lives in core.ts (importable, transport-injected). This
// file only: (a) reads secrets/knobs from Deno.env, (b) builds the production Rpc
// transport (fetch to Supabase /rpc with the service-role key), (c) wires the
// stdlib OpenAI embed transport, (d) runs Deno.serve with auth + safe logs.
//
// Secret boundary unchanged: keys live in the Edge secret store, read via
// Deno.env only here, never logged. Zero remote imports.

import {
  DEFAULT_BATCH_SIZE,
  DEFAULT_LEASE_SECONDS,
  PROVIDER_TIMEOUT_MS,
  SELECTED_CONTRACT,
  EmbeddingError,
  type EmbedTransport,
} from "./protocol.ts";
import { runOnce, safeMessage, bounded, type Outcome, type Rpc, type WorkerConfig } from "./core.ts";

const JSON_HEADERS = { "content-type": "application/json; charset=utf-8" } as const;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: JSON_HEADERS });
}

function requireEnv(name: string): string {
  const v = Deno.env.get(name);
  if (!v) throw new Error(`missing required env ${name}`);
  return v;
}

function clampInt(raw: string | undefined, def: number, lo: number, hi: number): number {
  if (raw === undefined || raw === "") return def;
  const n = Number.parseInt(raw, 10);
  if (!Number.isFinite(n)) return def;
  return Math.max(lo, Math.min(hi, n));
}

function serviceHeaders(serviceRoleKey: string): Record<string, string> {
  return {
    "apikey": serviceRoleKey,
    "authorization": `Bearer ${serviceRoleKey}`,
    "content-type": "application/json",
  };
}

/** Production Rpc: POST to /rest/v1/rpc/<name> with the service-role key. */
function makeRpc(supabaseUrl: string, serviceRoleKey: string): Rpc {
  return async (name: string, params: Record<string, unknown>) => {
    const url = new URL(`/rest/v1/rpc/${name}`, supabaseUrl);
    const resp = await fetch(url, {
      method: "POST",
      headers: serviceHeaders(serviceRoleKey),
      body: JSON.stringify(params),
    });
    if (!resp.ok) {
      let detail: string | undefined;
      try {
        const e = await resp.json() as { message?: string };
        detail = e?.message;
      } catch {
        detail = undefined;
      }
      throw new Error(`rpc ${name} HTTP ${resp.status}${detail ? `: ${detail}` : ""}`);
    }
    return await resp.json();
  };
}

/** Stdlib OpenAI transport used in production (injectable for tests via core). */
const stdioTransport: EmbedTransport = async (url, headers, body) => {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), PROVIDER_TIMEOUT_MS);
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
    if (!resp.ok) throw new EmbeddingError(`OpenAI embeddings HTTP ${resp.status}`);
    return await resp.json();
  } finally {
    clearTimeout(t);
  }
};

function buildConfig(): WorkerConfig {
  return {
    workerId: `edge-${Deno.env.get("DENO_DEPLOYMENT_ID") ?? "manual"}-${Math.floor(Date.now() / 1000)}`,
    apiKey: Deno.env.get("OPENAI_API_KEY") ?? "",
    transport: stdioTransport,
    contract: SELECTED_CONTRACT,
    batchSize: clampInt(Deno.env.get("EMBEDDING_WORKER_BATCH"), DEFAULT_BATCH_SIZE, 1, 64),
    leaseSeconds: clampInt(Deno.env.get("EMBEDDING_WORKER_LEASE_S"), DEFAULT_LEASE_SECONDS, 1, 1800),
    embedMaxAttempts: clampInt(Deno.env.get("EMBEDDING_WORKER_MAX_ATTEMPTS"), 3, 1, 6),
    embedBackoffMs: clampInt(Deno.env.get("EMBEDDING_WORKER_BACKOFF_MS"), 250, 0, 5000),
    recoverGraceSeconds: clampInt(Deno.env.get("EMBEDDING_WORKER_RECOVER_GRACE_S"), 0, 0, 600),
    now: () => Date.now(),
    sleep: (ms) => new Promise<void>((resolve) => setTimeout(resolve, ms)),
  };
}

function authorized(request: Request): boolean {
  const expected = Deno.env.get("EMBEDDING_WORKER_KEY");
  if (!expected) return true; // unset -> callable for operator testing
  return request.headers.get("x-worker-key") === expected;
}

Deno.serve(async (request) => {
  if (request.method !== "POST") {
    return jsonResponse({ error: "method_not_allowed", detail: "POST required" }, 405);
  }
  if (!authorized(request)) {
    return jsonResponse({ error: "forbidden", detail: "x-worker-key mismatch" }, 403);
  }
  try {
    const supabaseUrl = requireEnv("SUPABASE_URL");
    const serviceRoleKey = requireEnv("SUPABASE_SERVICE_ROLE_KEY");
    const config = buildConfig();
    const outcome: Outcome = await runOnce({ rpc: makeRpc(supabaseUrl, serviceRoleKey), config });
    return jsonResponse({ status: "ok", ...outcome });
  } catch (err) {
    console.error("embedding-worker failed", safeMessage(err));
    return jsonResponse({ error: "internal", detail: bounded(safeMessage(err)) }, 500);
  }
});
