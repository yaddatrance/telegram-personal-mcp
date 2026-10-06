# Connect a cloud assistant

The implementation supports local stdio and authenticated Streamable HTTP. It
does not create tunnels, provider accounts, OAuth clients or public endpoints.
Choose a route below; complete account authorization yourself before live use.
No live cloud connection has been exercised as part of the offline test suite.

## Route A: private OpenAI Secure MCP Tunnel

This is the shortest documented route from a private Windows stdio server to a
ChatGPT developer-mode connection. It needs no inbound firewall opening. Follow
the current [official tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
for eligibility, downloads and account setup; availability depends on permissions.

1. In Platform tunnel settings, create a tunnel associated with the intended
   Platform organization and ChatGPT workspace. You need Tunnels Read + Manage
   to create it, and Read + Use to run/select it. Obtain its `tunnel_id` and a
   runtime API key. This is a separate explicit user setup action.
2. Get the current Windows tunnel client from the official
   [latest release](https://github.com/openai/tunnel-client/releases/latest),
   then inspect `tunnel-client help quickstart`. Keep its config outside the repo.
3. Supply `CONTROL_PLANE_API_KEY` only in that local process environment using
   hidden input or a secret manager. Do not paste the key into chat, a command
   literal, shell history, a tracked file or an agent tool call.
4. Start with this **fake-account** profile. Replace placeholders locally and
   quote paths containing spaces according to the tunnel client's command parser:

   ```text
   tunnel-client init --sample sample_mcp_stdio_local --profile telegram-demo --tunnel-id <YOUR_TUNNEL_ID> --mcp-command "C:/path/to/telegram-personal-mcp/.venv/Scripts/python.exe -m ken_mcp serve --data-dir C:/path/to/telegram-personal-mcp/.local/tunnel-demo"
   tunnel-client doctor --profile telegram-demo --explain
   tunnel-client run --profile telegram-demo
   ```

5. In ChatGPT, enable Developer mode under Settings > Security and login. Open
   Plugins, select the plus button, choose Connection: Tunnel and select/paste
   the tunnel ID. Review the thirteen discovered tools. Add that connection to a
   new chat. See the [official connection guide](https://developers.openai.com/plugins/deploy/connect-chatgpt).
6. Verify `get_identity` reports demo account `9000`; list fake chats, prepare a
   synthetic message and explicitly send it. Its status must be `simulated`.
7. Only after the README's local Telegram login handoff, configure a separate
   live profile whose MCP command is the same interpreter plus
   `-m ken_mcp serve --live`. Check the exact account ID. Add `--enable-sends`
   only when live sending is authorized. Keep the tunnel client running while used.

**Trust boundary:** stdio trusts whoever can invoke it through the host/tunnel.
Tunnel organization/workspace permissions are not this app's per-user OAuth
owner check. Use this route only in an owner-private connection. For a shared
workspace or independently authenticated owner, use Route B's HTTP adapter;
OAuth discovery can also travel through a supported tunnel, while its identity
provider must remain reachable by the user's browser and the tunnel host.

This route is private developer-mode access. Publishing this repository does
not publish a ChatGPT plugin or start a tunnel. Marketplace distribution has
separate requirements in the official connection guide.

## Route B: OAuth-protected Streamable HTTP

`serve-http` is a resource server backed by an existing OAuth/OIDC provider.
It does not issue tokens, host a login page, or support static bearer tokens
as a substitute for OAuth. An owner-controlled Windows process keeps the
Telegram session local. Linux live deployment is not supported by DPAPI storage.

### Provider contract

Configure an external authorization server with:

- HTTPS OAuth authorization-server or OIDC discovery metadata, authorization
  code flow, PKCE `S256`, and the exact callback URI shown by ChatGPT's connection
  setup. Copy that URI; do not assume a universal callback. The optional stable
  callback requires the provider's RFC 9207 issuer-identification support.
- A dedicated resource/audience such as `https://telegram.example.invalid/mcp`.
  Honor the OAuth `resource` request in both authorization and token exchange.
- RS256 access tokens (not ID tokens), RSA signing keys at least 2048 bits,
  HTTPS JWKS, and access-token lifetime no greater than one hour.
- Claims `iss`, single-string `aud`, `sub`, numeric `iat`/`exp`, a space-separated
  `scope`, and `azp` (or explicitly configured `client_id`). `nbf`, if present,
  must be valid. Tokens need `telegram.read`; sending additionally needs
  `telegram.send`. The read scope permits local draft preparation/cancellation.
- An exact owner subject and allowlisted client ID. Restrict login/consent to
  that owner at the provider too. Static registered clients, CIMD or DCR may
  be supported by the provider; whichever is used, record the actual client ID
  in this app's allowlist. Arbitrary newly registered clients are not trusted.

Use [OpenAI's authentication requirements](https://developers.openai.com/plugins/build/auth)
to configure the provider and ChatGPT registration. Provider-specific setup is
an operator handoff, not a bundled or preconfigured identity service.

### Resource server setup

Copy `remote-config.example.json` to an ignored `remote-config.json` outside
the published tree. Replace every example value with verified configuration.
The canonical resource must be an HTTPS URL ending exactly in `/mcp` without
query or fragment. Use the provider's exact issuer (including trailing slash,
if applicable), its trusted JWKS URL, your subject and actual OAuth client ID.
`telegram_account_id` must equal `get_identity`'s account ID; use `9000` for demo.
The config contains identifiers, not client secrets or Telegram credentials.

```powershell
# Offline Telegram backend, but actual OAuth verification when called:
.\.venv\Scripts\python.exe -m ken_mcp serve-http --config remote-config.json --data-dir .local\http-demo --port 8766
```

The listener is fixed to **127.0.0.1**. After explicit deployment authorization,
place it behind your trusted HTTPS reverse proxy or approved private HTTP MCP
tunnel. Route `/mcp` and `/.well-known/oauth-protected-resource` (including the
`/mcp` metadata suffix). Preserve Authorization and the canonical Host header.
Do not proxy the review server or expose the Python listener directly on a LAN.
Configure the edge to cap request bodies at 64 KiB, bound request time/rate,
disable body/Authorization logging and restrict access to the intended owner.
The app accepts the configured host and loopback host, and validates supplied
Origin against the canonical origin or `https://chatgpt.com`. Proxy forwarded
headers are not trusted by Uvicorn.

All MCP requests, including initialization and tool listing, require a valid
token. Public metadata advertises the resource, authorization server and both
scopes. Unauthenticated MCP calls return 401 with a metadata challenge. Invalid
issuer/audience/owner/client/signature/expiry fails closed. The configured OAuth
owner maps to exactly one Telegram account. The app never accepts a token's
untrusted `jku` or `x5u` as a key source. JWKS is cached for up to five minutes;
refresh is throttled and unavailable stale keys fail closed.

After the separate local login handoff, switch to `--live` without a demo data
directory. Add `--enable-sends` only when authorized. The optional
`--require-local-approval` flag remains available for either transport.

### Connect and verify

1. Verify HTTPS and metadata from outside the host. Add the canonical `/mcp`
   endpoint using ChatGPT's developer-mode connection settings and OAuth. Supply
   provider client credentials only in the appropriate trusted settings UI if
   that provider requires them; they do not belong in this server's config.
2. Sign in as the configured owner. Discover thirteen tools and verify identity.
   A read-only token can read and prepare drafts but cannot send. Reauthorize
   with `telegram.send` when sending is intended.
3. Exercise demo first. Check the prepared exact peer/text, authorize it, send,
   and confirm `simulated`. Repeat the same send and confirm no second dispatch.
4. For live, perform read-only identity verification first. A real send requires
   a separate specific user instruction. Use that instruction directly once
   unambiguous; this app does not add a redundant desktop confirmation.

If discovery fails, check reachability, the canonical host/resource, OAuth
metadata and provider registration. If authentication fails, check exact issuer,
audience, owner subject, client claim, scopes, clock and token lifetime. Do not
disable authentication or log the token to troubleshoot. A tunnel or proxy outage
does not permit retrying an uncertain send under a new idempotency key.

## What remains for the operator

The offline suite verifies SDK protocol exchange, JWT policies, owner isolation,
scope enforcement and dispatch guards. The operator must supply and validate
actual provider/tunnel access, HTTPS routing, Telegram login, consent, account
identity and the final ChatGPT connection. No real credentials or ready-to-use
public service are shipped with this repository.
