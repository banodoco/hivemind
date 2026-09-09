# North Star — shared contributor authentication

Hivemind remains a public knowledge reader. Any user or agent can search and
fetch the existing public corpus through the CLI, Astrid, or direct read
surfaces without logging in. Authentication appears only when a caller tries
to contribute through the current resource/revision/message-snapshot/evidence
write surface.

An authenticated Discord account can be connected to an existing Hivemind
contributor identity through a trusted stable mapping or explicit operator
action, and can obtain one or more revocable machine keys. Display-name or
email matching is never an identity proof, and authentication never silently
creates or grants a new editorial identity. Contributions retain each
action's current publication/moderation semantics, contributor attribution,
and editor permissions. Authentication alone never grants editor status or
changes the existing editor decision rules.

The browser flow is a static Banodoco `/connect/index.html` route served at
`https://www.banodoco.ai/connect/`, packaged through `deploy/public-files.json`.
It uses a pinned Supabase browser SDK and the existing shared Supabase session
identity; Arca Gidan is read-only auth/identity reference material and receives
no product changes. The CLI creates a short-lived approval request and keeps
its polling secret locally. The page stores only the opaque request capability
in `sessionStorage`, returns to the fixed `/connect/` callback, completes the
session, and displays the requesting machine and code. A user must explicitly
approve with a POST after login; OAuth completion never auto-approves. Hivemind
owns the schema, identity mapping, broker, key issuance, and race logic. The
browser receives no contributor key or service secret. Requests expire, are
one-use, and are race-safe. Approval records the approved identity only. The
CLI redemption transaction atomically generates, hashes, and returns one key;
plaintext key material is never persisted server-side. Approval GETs have no
side effects. Raw contributor keys never appear in URLs, logs, or
browser-visible state. A successful login attempts to open a browser, always
prints the approval URL, and supports `--no-browser`.

The resulting device key is saved in an owner-only `~/.hivemind/key` file.
`HIVEMIND_CONTRIBUTOR_KEY` remains the higher-precedence environment override.
Status reports structured metadata without secrets. Logout clearly distinguishes
local deletion from server-side revocation.

Avoid adding authentication to reads, redesigning retrieval, introducing a
general IAM system, adding a separate Astrid publishing backend, using a native
Supabase device-grant flow, or performing production operations as part of the
implementation. Do not replace the existing Hivemind GitHub CTA; CLI-generated
URLs suffice. Exact Banodoco CORS and Supabase redirect-allowlist configuration
is declared for a future authorized environment only.
