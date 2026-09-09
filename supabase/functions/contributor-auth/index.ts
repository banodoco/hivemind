interface RpcError { code?: string; message?: string; }

const HEADERS = { "content-type": "application/json; charset=utf-8" } as const;
const INTERNAL = { error: "internal", detail: "internal server error" } as const;
const DEFAULT_CONNECT_URL = "https://www.banodoco.ai/connect/";
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: HEADERS });
}

function requiredEnv(name: "SUPABASE_URL" | "SUPABASE_SERVICE_ROLE_KEY"): string {
  const value = Deno.env.get(name);
  if (!value) throw new Error(`Missing required environment variable: ${name}`);
  return value;
}

function serviceHeaders(key: string, extra?: HeadersInit): Headers {
  const headers = new Headers(extra);
  headers.set("apikey", key);
  headers.set("authorization", `Bearer ${key}`);
  return headers;
}

async function errorBody(response: Response): Promise<RpcError> {
  try { return await response.json() as RpcError; } catch { return {}; }
}

async function rpc(base: string, key: string, name: string, args: Record<string, unknown>): Promise<unknown> {
  const response = await fetch(new URL(`/rest/v1/rpc/${name}`, base), {
    method: "POST",
    headers: serviceHeaders(key, { "content-type": "application/json" }),
    body: JSON.stringify(args),
  });
  if (response.ok) return await response.json();
  const error = await errorBody(response);
  const failure = new Error(error.message ?? `broker RPC failed with status ${response.status}`);
  (failure as Error & { code?: string }).code = error.code;
  throw failure;
}

function brokerError(error: unknown): Response {
  const code = error instanceof Error ? (error as Error & { code?: string }).code : undefined;
  if (code === "42501") return json({ error: "unauthorized" }, 401);
  if (code === "55P03") return json({ error: "rate_limited" }, 429);
  if (code === "23505" || code === "40001") return json({ error: "conflict" }, 409);
  if (code === "22023") return json({ error: "validation", detail: error instanceof Error ? error.message : "invalid broker request" }, 400);
  return json(INTERNAL, 500);
}

async function body(request: Request): Promise<Record<string, unknown> | null> {
  try {
    const value = await request.json();
    return value !== null && typeof value === "object" && !Array.isArray(value)
      ? value as Record<string, unknown> : null;
  } catch { return null; }
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function approvalUrl(requestToken: string, approvalCode: string): string {
  const base = Deno.env.get("HIVEMIND_AUTH_CONNECT_URL") || DEFAULT_CONNECT_URL;
  const url = new URL(base);
  url.searchParams.set("request", requestToken);
  // Keep the broker's pairing code distinct from Supabase's OAuth callback
  // `code` parameter. The static Banodoco callback must survive both values.
  url.searchParams.set("approval_code", approvalCode);
  return url.toString();
}

async function authenticatedUserId(request: Request, base: string, serviceKey: string): Promise<string | null> {
  const authorization = request.headers.get("authorization");
  if (!authorization?.startsWith("Bearer ")) return null;
  const response = await fetch(new URL("/auth/v1/user", base), {
    headers: { apikey: serviceKey, authorization },
  });
  if (!response.ok) return null;
  const user = await response.json() as { id?: unknown };
  return typeof user.id === "string" && UUID_RE.test(user.id) ? user.id : null;
}

async function handle(request: Request): Promise<Response> {
  const base = requiredEnv("SUPABASE_URL");
  const serviceKey = requiredEnv("SUPABASE_SERVICE_ROLE_KEY");

  // GET is intentionally a side-effect-free request preview for the hosted
  // browser page. The CLI polling secret is never accepted in a URL.
  if (request.method === "GET") {
    const url = new URL(request.url);
    const requestToken = url.searchParams.get("request");
    if (!requestToken) return json({ error: "validation", detail: "request is required" }, 400);
    try {
      return json(await rpc(base, serviceKey, "hivemind_auth_get_request", { p_request_token: requestToken }));
    } catch (error) {
      return brokerError(error);
    }
  }
  if (request.method !== "POST") return json({ error: "method_not_allowed", detail: "GET or POST required" }, 405);
  const input = await body(request);
  if (!input) return json({ error: "validation", detail: "request body must be a JSON object" }, 400);
  const action = text(input.action);
  try {
    if (action === "create") {
      const requestToken = text(input.request_token);
      const pollSecret = text(input.poll_secret);
      const approvalCode = text(input.approval_code);
      const machineLabel = text(input.machine_label);
      if (!requestToken || requestToken.length < 32 || !pollSecret || pollSecret.length < 32 ||
          !approvalCode || approvalCode.length < 8 || !machineLabel) {
        return json({ error: "validation", detail: "request_token, poll_secret, approval_code, and machine_label are required" }, 400);
      }
      const created = await rpc(base, serviceKey, "hivemind_auth_create_request", {
        p_request_token: requestToken, p_poll_secret: pollSecret,
        p_approval_code: approvalCode, p_machine_label: machineLabel,
        p_ttl_seconds: typeof input.ttl_seconds === "number" ? input.ttl_seconds : 600,
      }) as Record<string, unknown>;
      return json({ ...created, approval_url: approvalUrl(requestToken, approvalCode) }, 201);
    }
    if (action === "poll") {
      const requestToken = text(input.request_token); const pollSecret = text(input.poll_secret);
      if (!requestToken || !pollSecret) return json({ error: "validation", detail: "request_token and poll_secret are required" }, 400);
      return json(await rpc(base, serviceKey, "hivemind_auth_poll_request", { p_request_token: requestToken, p_poll_secret: pollSecret }));
    }
    if (action === "approve") {
      const requestToken = text(input.request_token); const approvalCode = text(input.approval_code);
      const userId = await authenticatedUserId(request, base, serviceKey);
      if (!requestToken || !approvalCode || !userId) return json({ error: "unauthorized" }, 401);
      return json(await rpc(base, serviceKey, "hivemind_auth_approve_request", {
        p_request_token: requestToken, p_approval_code: approvalCode, p_auth_user_id: userId,
      }));
    }
    if (action === "redeem") {
      const requestToken = text(input.request_token); const pollSecret = text(input.poll_secret);
      if (!requestToken || !pollSecret) return json({ error: "validation", detail: "request_token and poll_secret are required" }, 400);
      return json(await rpc(base, serviceKey, "hivemind_auth_redeem_request", { p_request_token: requestToken, p_poll_secret: pollSecret }));
    }
    if (action === "revoke" || action === "status") {
      const key = text(input.key);
      if (!key) return json({ error: "validation", detail: "key is required" }, 400);
      return json(await rpc(base, serviceKey, action === "revoke" ? "hivemind_auth_revoke_key" : "hivemind_auth_key_status", { p_key: key }));
    }
    if (action === "revoke_request") {
      const requestToken = text(input.request_token); const pollSecret = text(input.poll_secret);
      if (!requestToken || !pollSecret) return json({ error: "validation", detail: "request_token and poll_secret are required" }, 400);
      return json(await rpc(base, serviceKey, "hivemind_auth_revoke_request", { p_request_token: requestToken, p_poll_secret: pollSecret }));
    }
    return json({ error: "validation", detail: "action must be create, poll, approve, redeem, revoke, revoke_request, or status" }, 400);
  } catch (error) {
    return brokerError(error);
  }
}

Deno.serve(async (request) => {
  try { return await handle(request); }
  catch (error) { console.error("contributor-auth edge function failed", error); return json(INTERNAL, 500); }
});
