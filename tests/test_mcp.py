import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

from ken_mcp.store import Store


@unittest.skipUnless(importlib.util.find_spec("mcp"), "Install project dependencies for MCP protocol checks")
class MCPIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_mode_user_authorized_send_needs_no_local_click(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        with tempfile.TemporaryDirectory() as directory:
            parameters = StdioServerParameters(command=sys.executable,
                args=["-m", "ken_mcp", "serve", "--data-dir", directory],
                cwd=str(Path(__file__).resolve().parents[1]))
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    identity = await session.call_tool("get_identity", {})
                    self.assertFalse(identity.structuredContent["local_approval_required"])
                    await session.call_tool("list_contacts", {})
                    prepared = await session.call_tool("prepare_message", {"peer_id": "1001", "text": "Ken explicitly requested this note.", "idempotency_key": "default-mode-key-01"})
                    draft = prepared.structuredContent
                    args = {"draft_id": draft["draft_id"], "preview_sha256": draft["preview_sha256"], "confirmation": "SEND", "user_authorized": False}
                    self.assertTrue((await session.call_tool("send_prepared_message", args)).isError)
                    args["user_authorized"] = True
                    sent = await session.call_tool("send_prepared_message", args)
                    self.assertFalse(sent.isError)
                    self.assertEqual(sent.structuredContent["status"], "simulated")
                    status = await session.call_tool("get_send_status", {"draft_id": draft["draft_id"]})
                    self.assertEqual(status.structuredContent["message_id"], sent.structuredContent["message_id"])
                    outbox = await session.call_tool("list_outbox", {"limit": 5})
                    self.assertEqual(len(outbox.structuredContent["requests"]), 1)

    async def test_real_sdk_stdio_handshake_tools_and_confirmed_fake_send(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        with tempfile.TemporaryDirectory() as directory:
            parameters = StdioServerParameters(command=sys.executable,
                args=["-m", "ken_mcp", "serve", "--require-local-approval", "--data-dir", directory],
                cwd=str(Path(__file__).resolve().parents[1]))
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    self.assertEqual(len(tools.tools), 11)
                    names = {tool.name: tool for tool in tools.tools}
                    self.assertNotIn("approve", names)
                    self.assertTrue(names["send_prepared_message"].annotations.destructiveHint)

                    async def call(name, arguments=None):
                        result = await session.call_tool(name, arguments or {})
                        data = result.structuredContent
                        if data is None:
                            data = json.loads(result.content[0].text) if not result.isError else {"error": result.content[0].text}
                        return result, data

                    result, identity = await call("get_identity")
                    self.assertEqual(identity["mode"], "demo")
                    self.assertEqual(identity["account_id"], "9000")
                    _, chats = await call("list_chats", {"query": "Alex"})
                    self.assertEqual(len(chats["chats"]), 2)
                    _, history = await call("read_history", {"peer_id": "1001", "limit": 1})
                    self.assertEqual(len(history["messages"]), 1)
                    _, draft = await call("prepare_reply", {"peer_id": "1001", "reply_to_message_id": 3,
                        "text": "Yes, see you Saturday!", "idempotency_key": "mcp-test-request-001"})
                    send_args = {"draft_id": draft["draft_id"], "preview_sha256": draft["preview_sha256"], "confirmation": "SEND", "user_authorized": True}
                    blocked, _ = await call("send_prepared_message", send_args)
                    self.assertTrue(blocked.isError)
                    # Models cannot do this via MCP. Test simulates an independent owner click.
                    store = Store(Path(directory) / "outbox.sqlite3", "demo")
                    store.approve_local(draft["draft_id"], draft["preview_sha256"])
                    store.close()
                    success, status = await call("send_prepared_message", send_args)
                    self.assertFalse(success.isError)
                    self.assertEqual(status["status"], "simulated")
                    _, repeated = await call("send_prepared_message", send_args)
                    self.assertEqual(repeated["message_id"], status["message_id"])
                    bad, _ = await call("read_history", {"peer_id": "1001", "limit": 51})
                    self.assertTrue(bad.isError)


if __name__ == "__main__":
    unittest.main()
