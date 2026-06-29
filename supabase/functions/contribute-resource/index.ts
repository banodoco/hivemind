import {
  validateAddResourceData,
  type AddResourceData,
  type ValidationError,
} from "../contribute/protocol.ts";

interface InsertRow {
  id: number;
}

interface PostgrestError {
  code?: string;
  details?: string | null;
  hint?: string | null;
  message?: string;
}

const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
} as const;

const INTERNAL_ERROR_BODY = {
  error: "internal",
  detail: "internal server error",
} as const;

function isValidationError(value: unknown): value is ValidationError {
  return !!value
    && typeof value === "object"
    && (value as { error?: string }).error === "validation"
    && typeof (value as { detail?: unknown }).detail === "string";
}

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: JSON_HEADERS,
  });
}

function validationResponse(detail: string): Response {
  return jsonResponse({ error: "validation", detail }, 400);
}

function internalErrorResponse(): Response {
  return jsonResponse(INTERNAL_ERROR_BODY, 500);
}

function getRequiredEnv(name: "SUPABASE_URL" | "SUPABASE_SERVICE_ROLE_KEY"): string {
  const value = Deno.env.get(name);
  if (!value) {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  return value;
}

function createServiceHeaders(serviceRoleKey: string, extra?: HeadersInit): Headers {
  const headers = new Headers(extra);
  headers.set("apikey", serviceRoleKey);
  headers.set("authorization", `Bearer ${serviceRoleKey}`);
  return headers;
}

interface ClientInfo {
  ip: string;
  userAgent: string;
}

function getClientInfo(request: Request, info: Deno.ServeHandlerInfo): ClientInfo {
  // Supabase forwards the original client IP in X-Forwarded-For.
  const forwarded = request.headers.get("x-forwarded-for");
  const ip = forwarded
    ? forwarded.split(",")[0].trim()
    : `${info.remoteAddr.hostname}:${info.remoteAddr.port}`;
  return {
    ip,
    userAgent: request.headers.get("user-agent") ?? "",
  };
}

function annotateResourceData(
  data: AddResourceData,
  clientInfo: ClientInfo,
): AddResourceData {
  const metadata: Record<string, unknown> =
    (data.metadata && typeof data.metadata === "object" && !Array.isArray(data.metadata))
      ? (data.metadata as Record<string, unknown>)
      : {};
  metadata["_submitted_from_ip"] = clientInfo.ip;
  metadata["_submitted_from_user_agent"] = clientInfo.userAgent;
  metadata["_submitted_at"] = new Date().toISOString();
  return { ...data, metadata };
}

async function readJsonBody(request: Request): Promise<unknown | ValidationError> {
  const contentType = request.headers.get("content-type");
  if (!contentType || !contentType.toLowerCase().includes("application/json")) {
    return { error: "validation", detail: "Content-Type must be application/json" };
  }

  try {
    return await request.json();
  } catch {
    return { error: "validation", detail: "request body must be valid JSON" };
  }
}

async function parseError(response: Response): Promise<PostgrestError | null> {
  try {
    return await response.json() as PostgrestError;
  } catch {
    return null;
  }
}

function formatCreatedResponse(id: number): Record<string, unknown> {
  return { id, status: "ok" };
}

function getAllowedSources(): string[] | null {
  const raw = Deno.env.get("ALLOWED_SOURCES");
  if (raw === undefined || raw === null) {
    // Default: only VibeComfy-controlled source namespaces.
    return ["vibecomfy", "vibecomfy-external"];
  }
  const trimmed = raw.trim();
  if (trimmed === "" || trimmed === "*") {
    // Empty string or wildcard disables the allowlist.
    return null;
  }
  return trimmed.split(",").map((s) => s.trim()).filter(Boolean);
}

function isSourceAllowed(source: string, allowedSources: string[] | null): boolean {
  if (allowedSources === null) {
    return true;
  }
  return allowedSources.some((allowed) => source === allowed);
}

async function insertResource(
  supabaseUrl: string,
  serviceRoleKey: string,
  data: AddResourceData,
): Promise<Response> {
  const url = new URL("/rest/v1/external_resources", supabaseUrl);
  const response = await fetch(url, {
    method: "POST",
    headers: createServiceHeaders(serviceRoleKey, {
      "content-type": "application/json",
      prefer: "return=representation",
    }),
    body: JSON.stringify(data),
  });

  if (response.ok) {
    const rows = await response.json() as InsertRow[];
    return jsonResponse(formatCreatedResponse(rows[0].id), 201);
  }

  const error = await parseError(response);
  if (error?.code === "23505") {
    return jsonResponse(
      { error: "duplicate", detail: "resource with this source+external_id already exists" },
      409,
    );
  }

  throw new Error(`Resource insert failed with status ${response.status}`);
}

async function handleRequest(
  request: Request,
  info: Deno.ServeHandlerInfo,
): Promise<Response> {
  if (request.method !== "POST") {
    return jsonResponse({ error: "method_not_allowed", detail: "POST required" }, 405);
  }

  const body = await readJsonBody(request);
  if (isValidationError(body)) {
    return validationResponse(body.detail);
  }

  if (body === null || typeof body !== "object" || Array.isArray(body)) {
    return validationResponse("request body must be a JSON object");
  }

  const req = body as Record<string, unknown>;
  const action = req.action;
  if (action !== "add_resource") {
    return validationResponse("action must be 'add_resource'");
  }

  const data = req.data;
  if (data === null || typeof data !== "object" || Array.isArray(data)) {
    return validationResponse("field 'data' is required and must be a JSON object");
  }

  const dataError = validateAddResourceData(data as Record<string, unknown>);
  if (dataError) {
    return validationResponse(dataError.detail);
  }

  const resourceData = data as AddResourceData;
  const allowedSources = getAllowedSources();
  if (!isSourceAllowed(resourceData.source, allowedSources)) {
    return jsonResponse(
      {
        error: "forbidden",
        detail: `source '${resourceData.source}' is not allowed`,
      },
      403,
    );
  }

  const supabaseUrl = getRequiredEnv("SUPABASE_URL");
  const serviceRoleKey = getRequiredEnv("SUPABASE_SERVICE_ROLE_KEY");
  const clientInfo = getClientInfo(request, info);
  return insertResource(supabaseUrl, serviceRoleKey, annotateResourceData(resourceData, clientInfo));
}

Deno.serve(async (request, info) => {
  try {
    return await handleRequest(request, info);
  } catch (error) {
    console.error("contribute-resource edge function failed", error);
    return internalErrorResponse();
  }
});
