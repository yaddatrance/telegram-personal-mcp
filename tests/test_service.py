import asyncio
import tempfile
import unittest
from pathlib import Path

from ken_mcp.service import Messenger
from ken_mcp.store import Store, UserError
from ken_mcp.transport import FakeTransport, TransportError


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "outbox.sqlite3"
        self.now = 100000.0
        self.store = Store(self.path, "demo", clock=lambda: self.now)
        self.transport = FakeTransport()
        self.service = Messenger(self.store, self.transport, require_local_approval=True)
        await self.service.start()
        await self.service.list_peers()

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def draft(self, text="Hello Alex", key="test-key-00000001", peer="1001", reply=None):
        return await self.service.prepare(peer, text, key, reply)

    def approve(self, draft):
        self.store.approve_local(draft["draft_id"], draft["preview_sha256"])

    async def send(self, draft):
        return await self.service.send(draft["draft_id"], draft["preview_sha256"], "SEND", user_authorized=True)

    async def test_ambiguous_names_return_both_and_never_resolve_names(self):
        result = await self.service.list_peers(query="Alex")
        self.assertEqual(len(result["chats"]), 2)
        with self.assertRaises(UserError):
            await self.draft(peer="Alex")
        self.assertEqual(self.transport.calls, [])

    async def test_unknown_peer_rejected(self):
        with self.assertRaises(UserError):
            await self.draft(peer="99999")

    async def test_exact_text_and_no_hidden_signature(self):
        draft = await self.draft("  Hi <Alex> & 👋\nSaturday?  ")
        self.assertEqual(draft["text"], "  Hi <Alex> & 👋\nSaturday?  ")
        self.assertEqual(self.transport.calls, [])

    async def test_missing_owner_approval_fails(self):
        draft = await self.draft()
        with self.assertRaisesRegex(UserError, "approval"):
            await self.send(draft)
        self.assertEqual(self.transport.calls, [])

    async def test_default_authorized_caller_needs_no_desktop_click(self):
        self.service.require_local_approval = False
        draft = await self.draft()
        self.assertEqual((await self.send(draft))["status"], "simulated")
        self.assertEqual(len(self.transport.calls), 1)

    async def test_missing_user_authorization_fails_even_in_default_mode(self):
        self.service.require_local_approval = False
        draft = await self.draft()
        with self.assertRaisesRegex(UserError, "authorization"):
            await self.service.send(draft["draft_id"], draft["preview_sha256"], "SEND")
        self.assertEqual(self.transport.calls, [])

    async def test_wrong_confirmation_and_hash_fail(self):
        draft = await self.draft()
        self.approve(draft)
        with self.assertRaises(UserError):
            await self.service.send(draft["draft_id"], draft["preview_sha256"], "yes", True)
        with self.assertRaises(UserError):
            await self.service.send(draft["draft_id"], "a" * 64, "SEND", True)
        self.assertEqual(self.transport.calls, [])

    async def test_owner_approval_expires(self):
        draft = await self.draft()
        self.approve(draft)
        self.now += 301
        with self.assertRaises(UserError):
            await self.send(draft)

    async def test_draft_expires(self):
        draft = await self.draft()
        self.now += 1801
        with self.assertRaises(UserError):
            self.approve(draft)

    async def test_duplicate_request_key_reuses_immutable_request(self):
        first = await self.draft()
        second = await self.draft()
        self.assertEqual(first["draft_id"], second["draft_id"])
        with self.assertRaises(UserError):
            await self.draft("Different text")
        with self.assertRaises(UserError):
            await self.draft(peer="1002")

    async def test_new_key_cannot_bypass_identical_message_guard(self):
        await self.draft()
        with self.assertRaises(UserError):
            await self.draft(key="test-key-00000002")

    async def test_double_send_and_concurrent_requests_dispatch_once(self):
        draft = await self.draft()
        self.approve(draft)
        a, b = await asyncio.gather(self.send(draft), self.send(draft))
        self.assertEqual(a["status"], "simulated")
        self.assertEqual(b["status"], "simulated")
        self.assertEqual(len(self.transport.calls), 1)

    async def test_persistent_random_id_and_restart_no_replay(self):
        draft = await self.draft()
        random_id = self.store.item(draft["draft_id"])["random_id"]
        self.approve(draft)
        await self.send(draft)
        self.assertEqual(self.transport.calls[0]["random_id"], random_id)
        self.store.close()
        self.store = Store(self.path, "demo", clock=lambda: self.now)
        self.service = Messenger(self.store, self.transport)
        await self.service.start()
        self.assertEqual((await self.send(draft))["status"], "simulated")
        self.assertEqual(len(self.transport.calls), 1)

    async def test_crash_recovery_is_unknown_and_never_retried(self):
        draft = await self.draft()
        self.approve(draft)
        self.store.claim(draft["draft_id"], draft["preview_sha256"], "9000")
        self.store.recover()
        self.assertEqual((await self.send(draft))["status"], "unknown")
        self.assertEqual(self.transport.calls, [])

    async def test_timeout_unknown_no_blind_retry_or_new_key(self):
        draft = await self.draft()
        self.approve(draft)
        self.transport.failure = TransportError("unknown")
        self.assertEqual((await self.send(draft))["status"], "unknown")
        self.assertEqual((await self.send(draft))["status"], "unknown")
        self.now += 86400
        with self.assertRaises(UserError):
            await self.draft(key="test-key-00000002")
        self.assertEqual(len(self.transport.calls), 1)

    async def test_cancellation_is_uncertain(self):
        draft = await self.draft()
        self.approve(draft)
        self.transport.failure = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.send(draft)
        self.assertEqual(self.store.item(draft["draft_id"])["status"], "unknown")

    async def test_forbidden_403_is_terminal(self):
        draft = await self.draft()
        self.approve(draft)
        self.transport.failure = TransportError("forbidden")
        self.assertEqual((await self.send(draft))["status"], "forbidden")
        await self.send(draft)
        self.assertEqual(len(self.transport.calls), 1)

    async def test_flood_wait_cooldown_persists_and_no_retries(self):
        draft = await self.draft()
        self.approve(draft)
        self.transport.failure = TransportError("rate_limited", 120)
        sent = await self.send(draft)
        self.assertEqual(sent["status"], "rate_limited")
        self.assertEqual(sent["retry_at"], self.now + 120)
        second = await self.draft("Another note", "test-key-00000002")
        self.approve(second)
        with self.assertRaises(UserError):
            await self.send(second)
        self.assertEqual(len(self.transport.calls), 1)
        self.now += 121
        self.assertEqual((await self.send(second))["status"], "simulated")

    async def test_cancelled_draft_never_sends(self):
        draft = await self.draft()
        self.approve(draft)
        self.store.cancel(draft["draft_id"])
        self.assertEqual((await self.send(draft))["status"], "cancelled")
        self.assertEqual(self.transport.calls, [])

    async def test_reply_target_must_exist_in_exact_chat(self):
        with self.assertRaises(UserError):
            await self.draft(reply=999)
        draft = await self.draft(reply=3)
        self.approve(draft)
        await self.send(draft)
        self.assertEqual(self.transport.calls[0]["reply_to"], 3)
        self.assertEqual(self.transport.calls[0]["peer_id"], "1001")

    async def test_history_bounds_and_pagination(self):
        history = await self.service.history("1001", 1, 3)
        self.assertEqual(history["messages"][0]["id"], 2)
        for limit in (0, 51, -1, True):
            with self.assertRaises(UserError):
                await self.service.history("1001", limit)

    async def test_text_bounds(self):
        for text in ("", " ", "x" * 4097, "👋" * 2049, "bad\x00text", "\ud800"):
            with self.assertRaises(UserError):
                await self.draft(text)

    async def test_send_rate_limit(self):
        for i in range(5):
            draft = await self.draft("Note " + str(i), "test-key-number-%02d" % i)
            self.approve(draft)
            await self.send(draft)
        sixth = await self.draft("Sixth", "test-key-number-06")
        self.approve(sixth)
        with self.assertRaisesRegex(UserError, "send limit"):
            await self.send(sixth)
        self.assertEqual(len(self.transport.calls), 5)

    async def test_recent_duplicate_window_starts_at_dispatch(self):
        draft = await self.draft()
        self.now += 1200
        self.approve(draft)
        await self.send(draft)
        with self.assertRaises(UserError):
            await self.draft(key="test-key-00000002")

    async def test_hourly_send_limit_persists(self):
        for i in range(30):
            draft = await self.draft("Hourly " + str(i), "hourly-request-%04d" % i)
            self.approve(draft)
            await self.send(draft)
            self.now += 13
        draft = await self.draft("Over limit", "hourly-request-0031")
        self.approve(draft)
        with self.assertRaisesRegex(UserError, "send limit"):
            await self.send(draft)

    async def test_unauthorized_and_spam_restriction_are_terminal(self):
        for i, kind in enumerate(("unauthorized", "spam_restricted")):
            draft = await self.draft("Status " + kind, "status-request-%04d" % i)
            self.approve(draft)
            self.transport.failure = TransportError(kind, 86400 if kind == "spam_restricted" else 0)
            self.assertEqual((await self.send(draft))["status"], kind)
            self.assertEqual((await self.send(draft))["status"], kind)
        self.assertEqual(len(self.transport.calls), 2)

    async def test_read_rate_limit(self):
        for _ in range(29):
            await self.service.list_peers()
        with self.assertRaisesRegex(UserError, "read limit"):
            await self.service.list_peers()

    async def test_read_only_live_mode_refuses_send(self):
        draft = await self.draft()
        self.approve(draft)
        self.service.enable_sends = False
        with self.assertRaisesRegex(UserError, "disabled"):
            await self.send(draft)

    async def test_separate_accounts_and_modes(self):
        with self.assertRaises(UserError):
            self.store.bind_account("1234")
        with self.assertRaises(UserError):
            Store(self.path, "live")

    async def test_unknown_after_restart_requires_no_access(self):
        draft = await self.draft()
        self.store.finish(draft["draft_id"], "unknown", "Uncertain")
        self.service.discovered.clear()
        self.assertEqual((await self.send(draft))["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
