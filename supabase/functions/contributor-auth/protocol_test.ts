import {
  allowedCorsOrigin,
  CONTRIBUTOR_AUTH_ORIGIN,
  verifiedDiscordUserId,
} from "./protocol.ts";
import { assertEquals } from "https://deno.land/std@0.208.0/assert/mod.ts";

Deno.test("CORS allows only the exact hosted origin and loopback rehearsal origins", () => {
  assertEquals(
    allowedCorsOrigin(CONTRIBUTOR_AUTH_ORIGIN),
    CONTRIBUTOR_AUTH_ORIGIN,
  );
  assertEquals(
    allowedCorsOrigin("http://localhost:8000"),
    "http://localhost:8000",
  );
  assertEquals(
    allowedCorsOrigin("http://127.0.0.1:54321"),
    "http://127.0.0.1:54321",
  );
  assertEquals(allowedCorsOrigin(null), null);
  assertEquals(allowedCorsOrigin("https://banodoco.ai"), null);
  assertEquals(allowedCorsOrigin("https://evil.example"), null);
  assertEquals(allowedCorsOrigin("*"), null);
});

Deno.test("auth admission rejects anonymous, non-Discord, and mismatched identities", () => {
  const id = "11111111-1111-4111-8111-111111111111";
  assertEquals(
    verifiedDiscordUserId({
      id,
      is_anonymous: true,
      identities: [{ provider: "discord" }],
    }),
    null,
  );
  assertEquals(
    verifiedDiscordUserId({
      id,
      identities: [{ provider: "email", user_id: id }],
    }),
    null,
  );
  assertEquals(
    verifiedDiscordUserId({
      id,
      identities: [{
        provider: "discord",
        user_id: "22222222-2222-4222-8222-222222222222",
      }],
    }),
    null,
  );
  assertEquals(
    verifiedDiscordUserId({
      id,
      identities: [{ provider: "discord", user_id: id }],
    }),
    id,
  );
  assertEquals(
    verifiedDiscordUserId({ id, identities: [{ provider: "discord" }] }),
    id,
  );
});
