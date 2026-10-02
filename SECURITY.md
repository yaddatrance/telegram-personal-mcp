# Security model

This is a single-owner personal Telegram MCP tool. Its default transport is a
fake offline account. Publishing the source grants no access to any account.
Development tests never connect to Telegram or persist a real session.

## Authorization

- Stdio trusts the local MCP host and its operating-system user. Use a private
  host/connection. Do not give another person an unauthenticated stdio bridge.
- HTTP authenticates every MCP request with a pinned external OAuth issuer/JWKS,
  exact audience, owner subject and allowlisted client, plus bounded token
  lifetime. Sending requires `telegram.send` as well as `telegram.read`.
- The owner subject maps to one exact Telegram account ID at startup. This
  implementation is not a multi-tenant account router.
- `user_authorized=true` is a trusted caller assertion about a specific user
  request. It is neither a credential nor independent proof of consent. A
  compromised authorized assistant can misuse its granted account access.
- Exact recipient/text/reply are bound to an immutable preview SHA-256. Incoming
  Telegram text is untrusted; it cannot authorize sends or change instructions.
- Optional strict local approval provides an additional human check. The review
  page binds only loopback, uses a fresh owner key and validates Host/Origin on
  state changes. Never expose it remotely. Agents must not use its owner key.

## Credentials and retained data

Login is a local interactive Windows handoff with hidden input and separate
authorization/persistence prompts. Windows DPAPI encrypts the stored session
and API credentials for the current user; folder ACLs restrict access to that
user and SYSTEM. Same-user malware or an administrator can compromise the
account: DPAPI does not defend against that threat. Memory contains decrypted
credentials while connected. There is no session-export MCP tool.

The outgoing outbox stores text, recipient IDs/names and outcomes in plaintext
SQLite within the private profile. History is returned to the MCP client but
not archived by this app. Your assistant provider, tunnel, OAuth provider and
reverse proxy have their own retention and security policies. Configure those
explicitly; do not enable request-body, Authorization-header or verbose RPC logs.
No raw sensitive exception details are returned by the application CLI.

Use the default live profile under local application data, outside a synced
checkout. Backups can contain private outbox data and encrypted credentials.
Revoke the named session in Telegram Settings > Devices before retiring the
profile. Stop the server before deleting its files. SQLite/WAL and disk backups
mean deleting a draft is not a secure-erasure facility; no purge tool is supplied.

## Delivery safeguards and limits

A transactional claim precedes network dispatch. Durable request keys and
MTProto random IDs prevent a repeated application request from redispatching.
Uncertain outcomes remain blocked; the program does not promise exactly-once
delivery. Do not clear the database or invent keys to bypass uncertainty.
Flood waits, send/read limits, identity binding, exact-peer discovery, bounded
history and disabled automatic retries constrain accidental misuse.

The remote adapter is tested with an in-memory HTTP client and ephemeral test
keys. Live Telegram, a deployed identity provider, HTTPS ingress and the actual
ChatGPT OAuth connection require operator verification. The public listener
must have the edge limits described in docs/REMOTE.md. Authentication and scoped
authorization do not by themselves protect a public endpoint from denial of
service or a compromised owner account.

## Repository hygiene and reporting

`.gitignore` excludes sessions, encrypted credentials, databases, local profiles,
environment files, private-key files and local raw verification logs. The audit
script checks staged/tracked files and optionally all reachable Git history for
sensitive paths, common credential formats, machine-user paths and PNG metadata.
It is a heuristic check, not a guarantee; review staged changes before pushing.
Only synthetic screenshots belong in `evidence/`. Never post a real session,
token, login code, private message or unredacted log in an issue.

Use the repository's private vulnerability reporting feature if available.
Otherwise open a minimal issue asking for a private reporting channel, without
exploit credentials or private data. Revoke exposed Telegram sessions and OAuth
tokens through their providers; deleting a public file does not revoke a secret.
