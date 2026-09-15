import {
  getMaxZipBytes,
  parseContributorKey,
  validateContributorKeyHeader,
  validateRequest,
  type ValidatedVibeComfyRatingRequest,
  type ValidationError,
} from "./protocol.ts";

interface ContributorRow {
  id: number;
}

interface InsertRow {
  id: string;
}

interface PostgrestError {
  code?: string;
  details?: string | null;
  hint?: string | null;
  message?: string;
}

const REPORTS_BUCKET = "vibecomfy-reports";

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

function unauthorizedResponse(): Response {
  return jsonResponse({ error: "unauthorized" }, 401);
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

async function requireContributorKeyHash(
  supabaseUrl: string,
  serviceRoleKey: string,
  contributorKey: string,
): Promise<string | null> {
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
  return rows[0]?.id ? hash : null;
}

function reportObjectPath(now = new Date(), id = crypto.randomUUID()): string {
  const year = String(now.getUTCFullYear()).padStart(4, "0");
  const month = String(now.getUTCMonth() + 1).padStart(2, "0");
  const day = String(now.getUTCDate()).padStart(2, "0");
  return `${year}/${month}/${day}/${id}.zip`;
}

function publicReportUrl(supabaseUrl: string, path: string): string {
  const url = new URL(`/storage/v1/object/public/${REPORTS_BUCKET}/${path}`, supabaseUrl);
  return url.toString();
}

interface ReportPack {
  url: string;
  path: string;
}

async function uploadReportPack(
  supabaseUrl: string,
  serviceRoleKey: string,
  request: ValidatedVibeComfyRatingRequest,
): Promise<ReportPack | null> {
  if (!request.pack_shared || request.pack_zip_bytes === null) {
    return null;
  }

  const path = reportObjectPath();
  const url = new URL(`/storage/v1/object/${REPORTS_BUCKET}/${path}`, supabaseUrl);
  const response = await fetch(url, {
    method: "POST",
    headers: createServiceHeaders(serviceRoleKey, {
      "content-type": "application/zip",
      "x-upsert": "false",
    }),
    body: request.pack_zip_bytes,
  });

  if (!response.ok) {
    throw new Error(`Report pack upload failed with status ${response.status}`);
  }

  return { url: publicReportUrl(supabaseUrl, path), path };
}

async function deleteReportPack(
  supabaseUrl: string,
  serviceRoleKey: string,
  path: string,
): Promise<void> {
  const url = new URL(`/storage/v1/object/${REPORTS_BUCKET}/${path}`, supabaseUrl);
  await fetch(url, {
    method: "DELETE",
    headers: createServiceHeaders(serviceRoleKey),
  });
}

function extractMetadata(rawBody: unknown): Record<string, unknown> | undefined {
  if (rawBody === null || typeof rawBody !== "object" || Array.isArray(rawBody)) {
    return undefined;
  }
  const metadata = (rawBody as Record<string, unknown>).metadata;
  if (metadata === undefined || metadata === null) {
    return undefined;
  }
  if (typeof metadata !== "object" || Array.isArray(metadata)) {
    return undefined;
  }
  return metadata as Record<string, unknown>;
}

function extractClientCreatedAt(rawBody: unknown): string | undefined {
  if (rawBody === null || typeof rawBody !== "object" || Array.isArray(rawBody)) {
    return undefined;
  }
  const value = (rawBody as Record<string, unknown>).client_created_at;
  if (typeof value !== "string") {
    return undefined;
  }
  return value;
}

async function insertRating(
  supabaseUrl: string,
  serviceRoleKey: string,
  data: ValidatedVibeComfyRatingRequest,
  contributorKeyHash: string,
  report: ReportPack | null,
  rawBody: unknown,
): Promise<Response> {
  const url = new URL("/rest/v1/vibecomfy_ratings", supabaseUrl);
  const row: Record<string, unknown> = {
    response_id: data.response_id,
    session_id: data.session_id,
    turn_id: data.turn_id,
    rating: data.rating,
    comment: data.comment,
    pack_shared: data.pack_shared,
    pack_comment: data.pack_comment,
    report_url: report?.url ?? null,
    report_path: report?.path ?? null,
    contributor_key_hash: contributorKeyHash,
    metadata: extractMetadata(rawBody) ?? {},
    client_created_at: extractClientCreatedAt(rawBody) ?? null,
  };

  const response = await fetch(url, {
    method: "POST",
    headers: createServiceHeaders(serviceRoleKey, {
      "content-type": "application/json",
      prefer: "return=representation",
    }),
    body: JSON.stringify(row),
  });

  if (response.ok) {
    const rows = await response.json() as InsertRow[];
    return jsonResponse({
      ok: true,
      id: rows[0]?.id ?? null,
      report_url: report?.url ?? null,
    }, 201);
  }

  const error = await parseError(response);
  if (error?.code === "23514") {
    return validationResponse(error.message ?? "rating payload violates database constraints");
  }

  throw new Error(`Rating insert failed with status ${response.status}`);
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

  const maxZipBytes = getMaxZipBytes(Deno.env.get("VIBECOMFY_RATING_MAX_ZIP_BYTES"));
  const validationResult = validateRequest(body, { maxZipBytes });
  if (validationResult.error) {
    return validationResponse(validationResult.error.detail);
  }

  const parsedKey = parseContributorKey(contributorKeyHeader);
  if (!parsedKey) {
    return validationResponse("X-Contributor-Key must be 'hm_<64 lowercase hex chars>'");
  }

  const supabaseUrl = getRequiredEnv("SUPABASE_URL");
  const serviceRoleKey = getRequiredEnv("SUPABASE_SERVICE_ROLE_KEY");
  const contributorKeyHash = await requireContributorKeyHash(
    supabaseUrl,
    serviceRoleKey,
    parsedKey.raw,
  );
  if (!contributorKeyHash) {
    return unauthorizedResponse();
  }

  const report = await uploadReportPack(
    supabaseUrl,
    serviceRoleKey,
    validationResult.value,
  );

  try {
    return await insertRating(
      supabaseUrl,
      serviceRoleKey,
      validationResult.value,
      contributorKeyHash,
      report,
      body,
    );
  } catch (error) {
    if (report?.path) {
      try {
        await deleteReportPack(supabaseUrl, serviceRoleKey, report.path);
      } catch (cleanupError) {
        console.error("Failed to clean up uploaded report pack after DB failure", cleanupError);
      }
    }
    throw error;
  }
}

Deno.serve(async (request) => {
  try {
    return await handleRequest(request);
  } catch (error) {
    console.error("submit-vibecomfy-rating edge function failed", error);
    return internalErrorResponse();
  }
});
