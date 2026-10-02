import asyncio
import dataclasses
import tempfile
import time
import unittest
from pathlib import Path

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from ken_mcp.remote import OAuthVerifier, RemoteConfig, build_http_app
from ken_mcp.service import Messenger
from ken_mcp.store import Store, UserError
from ken_mcp.transport import FakeTransport, TransportError


class RemoteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "outbox.sqlite3", "demo")
        self.transport = FakeTransport()
        self.service = Messenger(self.store, self.transport)
        await self.service.start()
        self.config = RemoteConfig(issuer="https://issuer.example.invalid/", jwks_url="https://issuer.example.invalid/jwks",
            resource_url="https://telegram.example.invalid/mcp", owner_subject="synthetic-owner", telegram_account_id="9000",
            allowed_client_ids=("synthetic-chatgpt-client",))
        # Ephemeral test signing key, never saved or used as a real connector credential.
        self.signing = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.key = jwt.algorithms.RSAAlgorithm.to_jwk(self.signing.public_key(), as_dict=True)
        self.key.update(kid="test-key", use="sig", alg="RS256")
        self.load_count = 0
        async def load():
            self.load_count += 1
            return {"keys": [self.key]}
        self.verifier = OAuthVerifier(self.config, jwks_loader=load)
        self.app = build_http_app(self.service, self.config, self.verifier)
        # AnyIO task groups must enter and exit in the same task. unittest runs
        # asyncSetUp/asyncTearDown in separate tasks, so own the lifespan here.
        self.ready = asyncio.Event()
        self.stop = asyncio.Event()
        async def lifespan():
            async with self.app.router.lifespan_context(self.app):
                self.ready.set()
                await self.stop.wait()
        self.lifespan_task = asyncio.create_task(lifespan())
        await self.ready.wait()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://telegram.example.invalid")
        self.rpc_id = 0

    async def asyncTearDown(self):
        await self.client.aclose()
        self.stop.set()
        await self.lifespan_task
        self.store.close()
        self.directory.cleanup()

    def token(self, **changes):
        now = int(time.time())
        claims = {"iss": self.config.issuer, "aud": self.config.resource_url, "sub": "synthetic-owner",
                  "azp": "synthetic-chatgpt-client", "iat": now, "exp": now + 600,
                  "scope": "telegram.read telegram.send"}
        claims.update(changes)
        return jwt.encode(claims, self.signing, algorithm="RS256", headers={"kid": "test-key", "typ": "at+jwt"})

    async def rpc(self, method, params=None, token=None, headers=None):
        self.rpc_id += 1
        merged = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}
        if token:
            merged["Authorization"] = "Bearer " + token
        merged.update(headers or {})
        return await self.client.post("/mcp", headers=merged,
            json={"jsonrpc": "2.0", "id": self.rpc_id, "method": method, "params": params or {}})

    async def call(self, name, arguments=None, token=None, headers=None):
        return await self.rpc("tools/call", {"name": name, "arguments": arguments or {}}, token or self.token(), headers)

    async def prepare(self):
        await self.call("list_chats")
        response = await self.call("prepare_message", {"peer_id": "1001", "text": "Authorized synthetic note.", "idempotency_key": "http-test-key-0001"})
        return response.json()["result"]["structuredContent"]

    def send_args(self, draft):
        return {"draft_id": draft["draft_id"], "preview_sha256": draft["preview_sha256"], "confirmation": "SEND", "user_authorized": True}

    async def test_discovery_metadata_and_unauthorized_challenge(self):
        for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
            result = await self.client.get(path)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["resource"], self.config.resource_url)
            self.assertEqual(result.json()["scopes_supported"], ["telegram.read", "telegram.send"])
        result = await self.rpc("tools/list")
        self.assertEqual(result.status_code, 401)
        self.assertIn("resource_metadata=", result.headers["www-authenticate"])

    async def test_authenticated_initialize_and_security_metadata(self):
        result = await self.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "offline-test", "version": "1"}}, self.token())
        self.assertEqual(result.status_code, 200)
        self.assertIn("serverInfo", result.json()["result"])
        tools = (await self.rpc("tools/list", token=self.token())).json()["result"]["tools"]
        self.assertEqual(len(tools), 11)
        send = next(tool for tool in tools if tool["name"] == "send_prepared_message")
        self.assertEqual(send["securitySchemes"][0]["scopes"], ["telegram.read", "telegram.send"])
        self.assertEqual(send["_meta"]["securitySchemes"], send["securitySchemes"])

    async def test_wrong_issuer_audience_subject_client_and_time_rejected(self):
        now = int(time.time())
        for override in ({"iss": "https://evil.example/"}, {"aud": "another-api"},
            {"aud": [self.config.resource_url, "another-api"]}, {"sub": "other-person"},
            {"azp": "other-client"}, {"exp": now - 60}, {"nbf": now + 600},
            {"iat": now + 100}, {"exp": now + 7200}, {"scope": "telegram.send"},
            {"client_id": "conflicting-client"}):
            with self.subTest(override=override):
                response = await self.rpc("tools/list", token=self.token(**override))
                self.assertEqual(response.status_code, 401)
        self.assertEqual(self.service.discovered, set())
        self.assertEqual(self.transport.calls, [])

    async def test_invalid_signature_none_algorithm_and_untrusted_key_url(self):
        wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        claims = jwt.decode(self.token(), options={"verify_signature": False})
        forged = jwt.encode(claims, wrong_key, algorithm="RS256", headers={"kid": "test-key", "jku": "https://evil.example/keys"})
        unsigned = jwt.encode(claims, key="", algorithm="none", headers={"kid": "test-key"})
        for token in (forged, unsigned, "malformed", "x" * 17000):
            self.assertEqual((await self.rpc("tools/list", token=token)).status_code, 401)
        self.assertEqual(self.load_count, 1)

    async def test_read_token_cannot_send_and_full_scope_needs_no_desktop_click(self):
        draft = await self.prepare()
        args = self.send_args(draft)
        denied = await self.call("send_prepared_message", args, token=self.token(scope="telegram.read"))
        self.assertTrue(denied.json()["result"]["isError"])
        self.assertEqual(self.transport.calls, [])
        sent = await self.call("send_prepared_message", args)
        self.assertEqual(sent.json()["result"]["structuredContent"]["status"], "simulated")

    async def test_other_owner_cannot_read_outbox_or_use_known_draft(self):
        draft = await self.prepare()
        other = self.token(sub="other-person")
        for name, args in (("list_outbox", {}), ("get_send_status", {"draft_id": draft["draft_id"]}), ("send_prepared_message", self.send_args(draft))):
            response = await self.call(name, args, token=other, headers={"Mcp-Session-Id": "stolen-session"})
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.transport.calls, [])

    async def test_recipient_hash_and_authorization_cannot_be_swapped(self):
        draft = await self.prepare()
        args = self.send_args(draft)
        for overrides in ({"preview_sha256": "a" * 64}, {"user_authorized": False}, {"confirmation": "maybe"}):
            denied = await self.call("send_prepared_message", {**args, **overrides})
            self.assertTrue(denied.json()["result"]["isError"])
        unknown = await self.call("prepare_message", {"peer_id": "9999", "text": "No", "idempotency_key": "http-test-key-0002"})
        self.assertTrue(unknown.json()["result"]["isError"])
        self.assertEqual(self.transport.calls, [])

    async def test_concurrent_http_calls_dispatch_once(self):
        draft = await self.prepare()
        a, b = await asyncio.gather(self.call("send_prepared_message", self.send_args(draft)), self.call("send_prepared_message", self.send_args(draft)))
        self.assertEqual(a.json()["result"]["structuredContent"]["message_id"], b.json()["result"]["structuredContent"]["message_id"])
        self.assertEqual(len(self.transport.calls), 1)

    async def test_uncertain_delivery_never_replayed_over_http(self):
        draft = await self.prepare()
        self.transport.failure = TransportError("unknown")
        for _ in range(2):
            result = await self.call("send_prepared_message", self.send_args(draft))
            self.assertEqual(result.json()["result"]["structuredContent"]["status"], "unknown")
        self.assertEqual(len(self.transport.calls), 1)

    async def test_untrusted_origin_and_host_rejected(self):
        for headers in ({"Host": "evil.example"}, {"Origin": "https://evil.example"}):
            response = await self.call("get_identity", headers=headers)
            self.assertIn(response.status_code, (400, 403, 421))

    async def test_invalid_owner_mapping_and_insecure_config_refused(self):
        with self.assertRaises(UserError):
            build_http_app(self.service, dataclasses.replace(self.config, telegram_account_id="9001"))
        for field, value in (("issuer", "http://insecure.example"), ("jwks_url", "https://user:password@example.com/jwks"), ("resource_url", "https://example.com/wrong")):
            with self.assertRaises(UserError):
                dataclasses.replace(self.config, **{field: value})

    async def test_jwks_failure_fails_closed_without_secret_output(self):
        async def fail():
            raise RuntimeError("synthetic-secret-that-must-not-escape")
        verifier = OAuthVerifier(self.config, jwks_loader=fail)
        self.assertIsNone(await verifier.verify_token(self.token()))


if __name__ == "__main__":
    unittest.main()
