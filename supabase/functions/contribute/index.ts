import {
  buildKnowledgeRpcRequest,
  parseContributorKey,
  parseKnowledgeReferences,
  validateContributorKeyHeader,
  validateKnowledgeModelRequest,
  type KnowledgeModelAction,
  type ValidationError,
} from "./protocol.ts";

const HEADERS = { "content-type": "application/json; charset=utf-8" } as const;
const INTERNAL = { error: "internal", detail: "internal server error" } as const;

function json(body: unknown, status: number): Response { return new Response(JSON.stringify(body), { status, headers: HEADERS }); }
function validation(detail: string): Response { return json({ error: "validation", detail }, 400); }
function env(name: "SUPABASE_URL" | "SUPABASE_SERVICE_ROLE_KEY"): string {
  const value = Deno.env.get(name); if (!value) throw new Error(`Missing required environment variable: ${name}`); return value;
}
function headers(key: string, extra?: HeadersInit): Headers {
  const h = new Headers(extra); h.set("apikey", key); h.set("authorization", `Bearer ${key}`); return h;
}
async function errorBody(response: Response): Promise<{ code?: string; message?: string }> {
  try { return await response.json() as { code?: string; message?: string }; } catch { return {}; }
}
async function contributorId(base: string, serviceKey: string, rawKey: string): Promise<number | null> {
  if (!parseContributorKey(rawKey)) return null;
  // Protected lookup is delegated to the SQL function, which hashes the raw
  // key and reads contributor_keys (never contributors.api_key_hash).
  const response = await fetch(new URL("/rest/v1/rpc/hivemind_resolve_contributor_key", base), {
    method: "POST",
    headers: headers(serviceKey, { "content-type": "application/json" }),
    body: JSON.stringify({ p_key: rawKey }),
  });
  if (!response.ok) return null;
  const rows = await response.json() as Array<{ contributor_id?: number }>;
  return rows[0]?.contributor_id ?? null;
}
async function callRpc(base: string, serviceKey: string, id: number, action: KnowledgeModelAction, data: Record<string, unknown>): Promise<Response> {
  const rpcNames: Record<KnowledgeModelAction, string> = {
    submit_resource: "hivemind_submit_resource", propose_revision: "hivemind_propose_revision",
    decide_revision: "hivemind_decide_revision", mark_canonical: "hivemind_mark_canonical",
    capture_message_snapshot: "hivemind_capture_message_snapshot", submit_evidence: "hivemind_submit_evidence",
  };
  let references: unknown = data.references;
  if ((action === "submit_resource" || action === "propose_revision") && typeof data.body === "string") {
    references = [...(Array.isArray(data.references) ? data.references : []), ...parseKnowledgeReferences(data.body).map((r) => ({ target_kind: r.target_kind, target_id: r.target_id, ...(r.target_version_id ? { target_version_id: r.target_version_id } : {}) }))];
  }
  const request = buildKnowledgeRpcRequest(action, id, data, references);
  const response = await fetch(new URL(`/rest/v1/rpc/${rpcNames[action]}`, base), {
    method: "POST", headers: headers(serviceKey, { "content-type": "application/json" }), body: JSON.stringify(request),
  });
  if (response.ok) return json(await response.json(), action === "decide_revision" || action === "mark_canonical" ? 200 : 201);
  const e = await errorBody(response);
  if (e.code === "42501") return json({ error: "unauthorized" }, 401);
  if (["40001", "55000", "23505"].includes(e.code ?? "")) return json({ error: "conflict", detail: e.message ?? "revision conflict" }, 409);
  if (["22023", "23503", "23514"].includes(e.code ?? "")) return validation(e.message ?? "invalid knowledge-model request");
  throw new Error(`knowledge RPC failed: ${response.status}`);
}

async function handle(request: Request): Promise<Response> {
  if (request.method !== "POST") return json({ error: "method_not_allowed", detail: "POST required" }, 405);
  const contentType = request.headers.get("content-type") ?? "";
  if (!contentType.toLowerCase().startsWith("application/json")) return validation("Content-Type must be application/json");
  const key = request.headers.get("x-contributor-key");
  const keyError = validateContributorKeyHeader(key); if (keyError) return validation(keyError.detail);
  let body: unknown; try { body = await request.json(); } catch { return validation("request body must be valid JSON"); }
  const requestError: ValidationError | null = validateKnowledgeModelRequest(body);
  if (requestError) return validation(requestError.detail);
  const parsed = body as { action: KnowledgeModelAction; data: Record<string, unknown> };
  const base = env("SUPABASE_URL"); const serviceKey = env("SUPABASE_SERVICE_ROLE_KEY");
  const id = await contributorId(base, serviceKey, key!); if (id === null) return json({ error: "unauthorized" }, 401);
  return callRpc(base, serviceKey, id, parsed.action, parsed.data);
}

Deno.serve(async (request) => { try { return await handle(request); } catch (error) { console.error("contribute edge function failed", error); return json(INTERNAL, 500); } });
