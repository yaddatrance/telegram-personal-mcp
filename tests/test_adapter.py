import asyncio
import importlib.util
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from ken_mcp.transport import TelethonTransport, TransportError


@unittest.skipUnless(importlib.util.find_spec("telethon"), "Install project dependencies to test the Telethon adapter")
class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from telethon import types
        self.client = AsyncMock()
        self.adapter = TelethonTransport(self.client)
        self.adapter.known["1001"] = ({"peer_id": "1001", "name": "Alex", "kind": "user"}, types.InputPeerUser(1001, 123))

    async def test_raw_send_preserves_random_id_and_exact_text(self):
        from telethon import types, functions
        self.client.return_value = types.UpdateShortSentMessage(id=10, pts=1, pts_count=1, date=datetime.now(timezone.utc), out=True)
        result = await self.adapter.send("1001", " Hi 👋 ", 3, 987654321)
        request = self.client.call_args.args[0]
        self.assertIsInstance(request, functions.messages.SendMessageRequest)
        self.assertEqual(request.random_id, 987654321)
        self.assertEqual(request.message, " Hi 👋 ")
        self.assertEqual(request.reply_to.reply_to_msg_id, 3)
        self.assertTrue(request.no_webpage)
        self.assertIsInstance(request.send_as, types.InputPeerSelf)
        self.assertFalse(request.allow_paid_stars)
        self.assertEqual(result["message_id"], 10)

    async def test_rpc_forbidden(self):
        from telethon import errors
        self.client.side_effect = errors.ForbiddenError(request=None, message="PRIVATE")
        with self.assertRaises(TransportError) as raised:
            await self.adapter.send("1001", "Hi", None, 123)
        self.assertEqual(raised.exception.kind, "forbidden")
        self.assertEqual(self.client.call_count, 1)

    async def test_rpc_flood_wait(self):
        from telethon import errors
        self.client.side_effect = errors.FloodWaitError(request=None, capture=123)
        with self.assertRaises(TransportError) as raised:
            await self.adapter.send("1001", "Hi", None, 123)
        self.assertEqual(raised.exception.kind, "rate_limited")
        self.assertEqual(raised.exception.seconds, 123)

    async def test_timeout_and_server_failure_are_uncertain(self):
        from telethon import errors
        for error in (asyncio.TimeoutError(), errors.ServerError(request=None, message="internal")):
            self.client.side_effect = error
            with self.assertRaises(TransportError) as raised:
                await self.adapter.send("1001", "Hi", None, 123)
            self.assertEqual(raised.exception.kind, "unknown")

    async def test_wrong_chat_reply_message_not_accepted(self):
        from telethon import types
        message = types.Message(id=1, peer_id=types.PeerUser(1002), message="Hello", date=datetime.now(timezone.utc))
        self.client.get_messages.return_value = message
        self.assertIsNone(await self.adapter.message("1001", 1))

    async def test_errors_never_include_session_or_request_text(self):
        self.client.side_effect = RuntimeError("secret-session-value and private message")
        with self.assertRaises(TransportError) as raised:
            await self.adapter.send("1001", "Hi", None, 123)
        self.assertNotIn("secret", str(raised.exception))

    async def test_library_retries_disabled_without_connecting(self):
        from ken_mcp.account import make_client
        client = make_client({"api_id": 12345, "api_hash": "0" * 32})
        self.assertEqual(client._request_retries, 0)
        self.assertEqual(client.flood_sleep_threshold, 0)
        self.assertFalse(client._sender._auto_reconnect)
        self.assertFalse(client.is_connected())


if __name__ == "__main__":
    unittest.main()
