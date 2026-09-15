import {
  formatCreatedResponse,
  formatDuplicateResponse,
  formatUnauthorizedResponse,
  isDuplicateConflict,
  parseContributorKey,
  validateContributorKeyHeader,
  validateContributeRequest,
  type AddResourceData,
  type CheckDuplicateInput,
  type ContributeRequest,
  type SubmitDistillationData,
  type ValidationError,
} from "./protocol.ts";

interface ContributorRow {
  id: number;
}

interface DistillationRow {
  id: number;
}

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

async function sha256Hex(input: string): Promise<string> {
  const bytes = new TextEncoder().encode(input);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
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

async function requireContributorId(
  supabaseUrl: string,
  serviceRoleKey: string,
  contributorKey: string,
): Promise<number | null> {
  const hash = await sha256Hex(contributorKey);
  const url = new URL("/rest/v1/contributors", supabaseUrl);
  url.searchParams.set("api_key_hash", `eq.${hash}`);
  url.searchParams.set("revoked_at", "is.null");
  url.searchParams.set("select", "id");
  url.searchParams.set("limit", "1");

  const response = await fetch(url, {
    headers: createServiceHeaders(serviceRoleKey),
  });

  if (!response.ok) {
    throw new Error(`Contributor lookup failed with status ${response.status}`);
  }

  const rows = await response.json() as ContributorRow[];
  return rows[0]?.id ?? null;
}

async function checkSupersedesExists(
  supabaseUrl: string,
  serviceRoleKey: string,
  supersedesId: number,
): Promise<boolean> {
  const url = new URL("/rest/v1/distillations", supabaseUrl);
  url.searchParams.set("id", `eq.${supersedesId}`);
  url.searchParams.set("select", "id");
  url.searchParams.set("limit", "1");

  const response = await fetch(url, {
    headers: createServiceHeaders(serviceRoleKey),
  });

  if (!response.ok) {
    throw new Error(`Supersedes lookup failed with status ${response.status}`);
  }

  const rows = await response.json() as DistillationRow[];
  return rows.length > 0;
}

async function checkDuplicateDistillation(
  supabaseUrl: string,
  serviceRoleKey: string,
  question: string,
  supersedesId: number | null | undefined,
): Promise<CheckDuplicateInput | null> {
  const url = new URL("/rest/v1/rpc/check_duplicate_distillation", supabaseUrl);
  const response = await fetch(url, {
    method: "POST",
    headers: createServiceHeaders(serviceRoleKey, {
      "content-type": "application/json",
    }),
    body: JSON.stringify({
      question_text: question,
      supersedes_id: supersedesId ?? null,
    }),
  });

  if (!response.ok) {
    throw new Error(`Duplicate RPC failed with status ${response.status}`);
  }

  const rows = await response.json() as CheckDuplicateInput[] | CheckDuplicateInput | null;
  if (!rows) return null;
  if (Array.isArray(rows)) {
    return rows[0] ?? null;
  }
  return rows;
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

function uniqueCites(cites: SubmitDistillationData["cites"]): SubmitDistillationData["cites"] {
  const seen = new Set<string>();
  return cites.filter((cite) => {
    const key = `${cite.item_kind}:${cite.item_id}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

async function insertDistillation(
  supabaseUrl: string,
  serviceRoleKey: string,
  contributorId: number,
  data: SubmitDistillationData,
): Promise<Response> {
  const duplicate = await checkDuplicateDistillation(
    supabaseUrl,
    serviceRoleKey,
    data.question,
    data.supersedes_id,
  );
  if (isDuplicateConflict(duplicate, data.supersedes_id)) {
    return jsonResponse(formatDuplicateResponse(duplicate!.existing_id), 409);
  }

  if (data.supersedes_id != null) {
    const exists = await checkSupersedesExists(
      supabaseUrl,
      serviceRoleKey,
      data.supersedes_id,
    );
    if (!exists) {
      return validationResponse("data.supersedes_id does not reference an existing distillation");
    }
  }

  const insertUrl = new URL("/rest/v1/distillations", supabaseUrl);
  const insertResponse = await fetch(insertUrl, {
    method: "POST",
    headers: createServiceHeaders(serviceRoleKey, {
      "content-type": "application/json",
      prefer: "return=representation",
    }),
    body: JSON.stringify({
      question: data.question,
      conditions: data.conditions ?? null,
      answer: data.answer,
      confidence: data.confidence,
      status: "pending",
      author_id: contributorId,
      supersedes_id: data.supersedes_id ?? null,
    }),
  });

  if (!insertResponse.ok) {
    throw new Error(`Distillation insert failed with status ${insertResponse.status}`);
  }

  const distillationRows = await insertResponse.json() as InsertRow[];
  const distillationId = distillationRows[0].id;
  const cites = uniqueCites(data.cites).map((cite) => ({
    distillation_id: distillationId,
    item_kind: cite.item_kind,
    item_id: cite.item_id,
  }));

  const citesUrl = new URL("/rest/v1/distillation_cites", supabaseUrl);
  const citesResponse = await fetch(citesUrl, {
    method: "POST",
    headers: createServiceHeaders(serviceRoleKey, {
      "content-type": "application/json",
      prefer: "return=minimal",
    }),
    body: JSON.stringify(cites),
  });

  if (!citesResponse.ok) {
    throw new Error(`Cites insert failed with status ${citesResponse.status}`);
  }

  return jsonResponse(formatCreatedResponse(distillationId), 201);
}

async function handleRequest(request: Request): Promise<Response> {
  if (request.method !== "POST") {
    return jsonResponse({ error: "method_not_allowed", detail: "POST required" }, 405);
  }

  const contributorKeyHeader = request.headers.get("x-contributor-key");
  const headerError = validateContributorKeyHeader(contributorKeyHeader);
  if (headerError) {
    return validationResponse(headerError.detail);
  }

  const body = await readJsonBody(request);
  if (isValidationError(body)) {
    return validationResponse(body.detail);
  }

  const requestError = validateContributeRequest(body);
  if (requestError) {
    return validationResponse(requestError.detail);
  }

  const parsedKey = parseContributorKey(contributorKeyHeader);
  if (!parsedKey) {
    return validationResponse(`X-Contributor-Key must be 'hm_<64 hex chars>'`);
  }

  const supabaseUrl = getRequiredEnv("SUPABASE_URL");
  const serviceRoleKey = getRequiredEnv("SUPABASE_SERVICE_ROLE_KEY");
  const contributorId = await requireContributorId(supabaseUrl, serviceRoleKey, parsedKey.raw);
  if (!contributorId) {
    return jsonResponse(formatUnauthorizedResponse(), 401);
  }

  const contributeRequest = body as ContributeRequest;
  if (contributeRequest.action === "add_resource") {
    return insertResource(
      supabaseUrl,
      serviceRoleKey,
      contributeRequest.data as AddResourceData,
    );
  }

  return insertDistillation(
    supabaseUrl,
    serviceRoleKey,
    contributorId,
    contributeRequest.data as SubmitDistillationData,
  );
}

Deno.serve(async (request) => {
  try {
    return await handleRequest(request);
  } catch (error) {
    console.error("contribute edge function failed", error);
    return internalErrorResponse();
  }
});
