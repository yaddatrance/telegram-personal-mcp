"""OAuth-protected Streamable HTTP resource server. Never issues login credentials."""
import asyncio
import json
import re
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
import jwt
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.responses import JSONResponse
from starlette.routing import Route

from .store import UserError

READ_SCOPE = "telegram.read"
SEND_SCOPE = "telegram.send"


def https_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise UserError("Remote URLs must be explicit HTTPS URLs.")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or any(c in value for c in '\r\n"<>')):
        raise UserError("Remote URLs must be HTTPS without userinfo, queries or fragments.")
    return value


@dataclass(frozen=True)
class RemoteConfig:
    issuer: str
    jwks_url: str
    resource_url: str
    owner_subject: str
    telegram_account_id: str
    allowed_client_ids: tuple[str, ...]
    client_id_claim: str = "azp"
    max_token_seconds: int = 3600

    def __post_init__(self):
        for value in (self.issuer, self.jwks_url, self.resource_url):
            https_url(value)
        if urlsplit(self.resource_url).path != "/mcp":
            raise UserError("The canonical resource URL must end in /mcp with no extra path.")
        if not self.owner_subject or len(self.owner_subject) > 256 or not re.fullmatch(r"[1-9][0-9]{0,18}", self.telegram_account_id):
            raise UserError("Configure the exact OAuth owner subject and Telegram account ID.")
        if (not self.allowed_client_ids or len(self.allowed_client_ids) > 5
                or any(not isinstance(c, str) or not c or len(c) > 512 for c in self.allowed_client_ids)):
            raise UserError("Configure one to five exact OAuth client IDs.")
        if self.client_id_claim not in {"azp", "client_id"} or not 60 <= self.max_token_seconds <= 3600:
            raise UserError("Unsupported OAuth claim or access-token lifetime policy.")

    @classmethod
    def read(cls, path):
        try:
            values = json.loads(path.read_text(encoding="utf-8"))
            values["allowed_client_ids"] = tuple(values["allowed_client_ids"])
            return cls(**values)
        except (ValueError, KeyError, TypeError):
            raise UserError("Invalid remote config; fill in the non-secret example with verified issuer/owner values.") from None


class OwnerAccessToken(AccessToken):
    subject: str


class OAuthVerifier(TokenVerifier):
    def __init__(self, config, jwks_loader=None):
        self.config = config
        self.loader = jwks_loader or self._load_jwks
        self.keys = {}
        self.loaded_at = 0
        self.last_attempt = float("-inf")
        self.lock = asyncio.Lock()

    async def _load_jwks(self):
        # Only this configured URL can be fetched; token-provided jku/x5u are ignored.
        async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", self.config.jwks_url) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 65536:
                        raise ValueError("JWKS response too large")
        return json.loads(data)

    async def _key(self, kid):
        async with self.lock:
            now = time.monotonic()
            refresh = now - self.loaded_at > 300 or kid not in self.keys
            if refresh and now - self.last_attempt >= 30:
                self.last_attempt = now
                document = await self.loader()
                rows = document.get("keys", [])
                if not 1 <= len(rows) <= 20:
                    raise ValueError("Invalid key set")
                keys = {}
                for row in rows:
                    if row.get("kty") == "RSA" and row.get("use", "sig") == "sig" and row.get("alg", "RS256") == "RS256":
                        if not isinstance(row.get("kid"), str) or row["kid"] in keys:
                            raise ValueError("Invalid key identifier")
                        key = jwt.PyJWK.from_dict(row, algorithm="RS256").key
                        if key.key_size < 2048:
                            raise ValueError("Weak signing key")
                        keys[row["kid"]] = key
                self.keys, self.loaded_at = keys, now
            if now - self.loaded_at > 300:
                raise ValueError("No fresh signing keys")
            return self.keys[kid]

    async def verify_token(self, token):
        try:
            if not isinstance(token, str) or len(token) > 16384:
                return None
            header = jwt.get_unverified_header(token)
            if (header.get("alg") != "RS256" or header.get("crit")
                    or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", header.get("kid", ""))):
                return None
            key = await self._key(header["kid"])
            claims = jwt.decode(token, key, algorithms=["RS256"], issuer=self.config.issuer,
                                audience=self.config.resource_url, leeway=5,
                                options={"require": ["iss", "aud", "exp", "iat", "sub", "scope", self.config.client_id_claim],
                                         "strict_aud": True})
            if not isinstance(claims["sub"], str) or not secrets.compare_digest(claims["sub"], self.config.owner_subject):
                return None
            if any(type(claims[name]) not in (int, float) for name in ("exp", "iat")):
                return None
            if not 0 < claims["exp"] - claims["iat"] <= self.config.max_token_seconds:
                return None
            client_id = claims[self.config.client_id_claim]
            if client_id not in self.config.allowed_client_ids:
                return None
            if claims.get("azp") and claims.get("client_id") and claims["azp"] != claims["client_id"]:
                return None
            if not isinstance(claims["scope"], str):
                return None
            scopes = claims["scope"].split()
            if READ_SCOPE not in scopes:
                return None
            return OwnerAccessToken(token=token, subject=claims["sub"], client_id=client_id,
                                    scopes=scopes, expires_at=int(claims["exp"]), resource=self.config.resource_url)
        except Exception:
            # Do not emit tokens, subject claims, key material, request details or exceptions.
            return None


class OAuthFastMCP(FastMCP):
    async def list_tools(self):
        tools = await super().list_tools()
        # ChatGPT expects securitySchemes at top level; mirror in _meta for older clients.
        for tool in tools:
            schemes = (tool.meta or {}).get("securitySchemes")
            if schemes:
                tool.securitySchemes = schemes
        return tools


def build_http_app(service, config, verifier=None):
    from .server import create_server
    if service.account["account_id"] != config.telegram_account_id:
        raise UserError("The OAuth owner mapping does not match this Telegram account.")
    verifier = verifier or OAuthVerifier(config)

    def authorize(sending=False):
        token = get_access_token()
        if not isinstance(token, OwnerAccessToken) or token.subject != config.owner_subject:
            raise ToolError("Authenticated owner access is required.")
        if sending and SEND_SCOPE not in token.scopes:
            raise ToolError("Missing telegram.send scope. Reauthorize through OAuth before sending.")

    parsed = urlsplit(config.resource_url)
    origin = parsed.scheme + "://" + parsed.netloc
    server = create_server(service, authorize=authorize, server_class=OAuthFastMCP,
        token_verifier=verifier, stateless_http=True, json_response=True,
        auth=AuthSettings(issuer_url=AnyHttpUrl(config.issuer), resource_server_url=AnyHttpUrl(config.resource_url), required_scopes=[READ_SCOPE]),
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
            allowed_hosts=[parsed.netloc, "127.0.0.1:*"], allowed_origins=[origin, "https://chatgpt.com"]))
    app = server.streamable_http_app()

    async def metadata(request):
        return JSONResponse({"resource": config.resource_url, "authorization_servers": [config.issuer],
            "scopes_supported": [READ_SCOPE, SEND_SCOPE], "bearer_methods_supported": ["header"],
            "resource_name": "Telegram Personal MCP"}, headers={"Cache-Control": "no-store"})

    # Explicit path and root aliases; preserve exact resource audience for OAuth discovery.
    app.router.routes.insert(0, Route("/.well-known/oauth-protected-resource/mcp", metadata, methods=["GET"]))
    app.router.routes.insert(0, Route("/.well-known/oauth-protected-resource", metadata, methods=["GET"]))
    return app


async def serve_http(directory, live, enable_sends, require_local_approval, config, port, attachment_dir=None):
    import uvicorn
    from .account import connect_existing
    from .security import ProfileLock
    from .service import Messenger
    from .store import Store
    from .transport import FakeTransport
    with ProfileLock(directory):
        transport = await connect_existing(directory) if live else FakeTransport()
        store = Store(directory / "outbox.sqlite3", "live" if live else "demo")
        try:
            service = Messenger(store, transport, enable_sends, require_local_approval, attachment_dir)
            await service.start()
            app = build_http_app(service, config)
            await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                access_log=False, log_level="critical", proxy_headers=False,
                timeout_keep_alive=5, limit_concurrency=20)).serve()
        finally:
            await transport.close()
            store.close()
