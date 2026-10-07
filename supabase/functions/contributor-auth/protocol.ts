export const CONTRIBUTOR_AUTH_ORIGIN = "https://www.banodoco.ai";
const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const LOCAL_ORIGIN_RE =
  /^https?:\/\/(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?$/i;

/**
 * Return an explicitly permitted browser origin.  Same-origin requests have
 * no Origin header and therefore need no CORS response header.  Loopback is
 * intentionally limited to local rehearsal; wildcard CORS is never allowed.
 */
export function allowedCorsOrigin(origin: string | null): string | null {
  if (!origin) return null;
  if (origin === CONTRIBUTOR_AUTH_ORIGIN || LOCAL_ORIGIN_RE.test(origin)) {
    return origin;
  }
  return null;
}

export interface SupabaseAuthIdentity {
  provider?: unknown;
  user_id?: unknown;
}

export interface SupabaseAuthUser {
  id?: unknown;
  is_anonymous?: unknown;
  identities?: unknown;
}

/**
 * `/auth/v1/user` has already verified the bearer token.  Admission still
 * requires a non-anonymous user carrying a Discord identity; members/profile
 * rows are deliberately not consulted here.
 */
export function verifiedDiscordUserId(value: unknown): string | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const user = value as SupabaseAuthUser;
  if (
    user.is_anonymous === true || typeof user.id !== "string" ||
    !UUID_RE.test(user.id)
  ) return null;
  if (!Array.isArray(user.identities)) return null;
  const discord = user.identities.some((candidate) => {
    if (
      candidate === null || typeof candidate !== "object" ||
      Array.isArray(candidate)
    ) return false;
    const identity = candidate as SupabaseAuthIdentity;
    return identity.provider === "discord" &&
      (identity.user_id === undefined || identity.user_id === user.id);
  });
  return discord ? user.id : null;
}
