import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ken_mcp.attachments import AttachmentFiles, MAX_FILE_BYTES
from ken_mcp.service import Messenger
from ken_mcp.store import Store, UserError, digest_message
from ken_mcp.transport import FakeTransport, TransportError


class AttachmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.inbox = self.directory / "attachments"
        self.inbox.mkdir()
        self.data = b"Synthetic EPUB bytes; no actual book or personal data."
        (self.inbox / "sample.epub").write_bytes(self.data)
        self.store = Store(self.directory / "outbox.sqlite3", "demo")
        self.addCleanup(self.store.close)
        self.transport = FakeTransport()
        self.service = Messenger(self.store, self.transport)
        await self.service.start()
        await self.service.list_peers()

    async def draft(self, name="sample.epub", peer="1001", key="attachment-key-0001", caption="", reply=None):
        return await self.service.prepare(peer, caption, key, reply, name)

    async def send(self, draft):
        return await self.service.send(draft["draft_id"], draft["preview_sha256"], "SEND", True)

    async def test_preview_metadata_bound_to_peer_caption_reply_and_bytes(self):
        draft = await self.draft(caption="For Saturday", reply=3)
        attachment = draft["attachment"]
        self.assertEqual(attachment, {"filename": "sample.epub", "size_bytes": len(self.data),
            "sha256": hashlib.sha256(self.data).hexdigest(), "mime_type": "application/epub+zip"})
        self.assertEqual(draft["preview_sha256"], digest_message("9000", "1001", "For Saturday", 3, attachment))
        self.assertEqual(self.transport.calls, [])
        for peer, caption, reply in (("1002", "For Saturday", 3), ("1001", "Different", 3), ("1001", "For Saturday", 2)):
            self.assertNotEqual(draft["preview_sha256"], digest_message("9000", peer, caption, reply, attachment))

    async def test_unknown_recipient_rejected_before_file_read(self):
        with patch.object(self.service.attachments, "snapshot") as snapshot:
            with self.assertRaisesRegex(UserError, "Unknown peer"):
                await self.draft(peer="99999")
            snapshot.assert_not_called()

    async def test_caption_and_reply_bounds(self):
        with self.assertRaisesRegex(UserError, "1024"):
            await self.draft(caption="x" * 1025)
        with self.assertRaisesRegex(UserError, "does not exist"):
            await self.draft(reply=999)
        with self.assertRaises(UserError):
            await self.draft(caption="bad\x00caption")

    async def test_path_and_url_inputs_are_rejected(self):
        for name in ("../outside.epub", "..\\outside.epub", str(self.inbox / "sample.epub"),
                     "https://example.invalid/file.epub", "sample.epub:secret", "NUL.epub", "a\x00.epub", ".env", "script.exe"):
            with self.subTest(filename=name), self.assertRaises(UserError):
                await self.draft(name=name)
        self.assertEqual(self.transport.calls, [])

    async def test_hard_links_are_rejected(self):
        outside = self.directory / "outside.epub"
        outside.write_bytes(b"outside")
        os.link(outside, self.inbox / "linked.epub")
        with self.assertRaisesRegex(UserError, "links"):
            await self.draft(name="linked.epub")
        self.assertNotIn("linked.epub", [f["filename"] for f in self.service.attachments.list()["files"]])

    async def test_symlinks_are_rejected(self):
        try:
            (self.inbox / "linked.epub").symlink_to(self.inbox / "sample.epub")
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows does not grant this process symlink creation; hard-link and handle checks run.")
            raise
        with self.assertRaisesRegex(UserError, "links"):
            await self.draft(name="linked.epub")

    async def test_missing_empty_and_oversize_files_rejected(self):
        with self.assertRaises(UserError):
            await self.draft(name="missing.epub")
        (self.inbox / "empty.epub").write_bytes(b"")
        with self.assertRaisesRegex(UserError, "50 MiB"):
            await self.draft(name="empty.epub")
        with (self.inbox / "huge.epub").open("wb") as file:
            file.truncate(MAX_FILE_BYTES + 1)
        with self.assertRaisesRegex(UserError, "50 MiB"):
            await self.draft(name="huge.epub")

    async def test_only_inbox_metadata_is_listed(self):
        (self.directory / "outside.pdf").write_bytes(b"not staged")
        (self.inbox / "secret.session").write_bytes(b"not supported")
        listed = self.service.attachments.list()
        self.assertEqual([f["filename"] for f in listed["files"]], ["sample.epub"])
        self.assertNotIn("Synthetic", json.dumps(listed))
        self.assertNotIn(str(self.directory), json.dumps(listed))

    async def test_source_change_cannot_change_prepared_bytes(self):
        draft = await self.draft()
        (self.inbox / "sample.epub").write_bytes(b"replacement")
        self.assertEqual((await self.send(draft))["status"], "simulated")
        self.assertEqual(self.transport.calls[0]["data"], self.data)
        self.assertEqual(self.transport.calls[0]["peer_id"], "1001")

    async def test_snapshot_tamper_blocked_before_attempt_or_upload(self):
        draft = await self.draft()
        blob = self.service.attachments.blobs / (draft["attachment"]["sha256"] + ".blob")
        blob.write_bytes(b"tampered")
        with self.assertRaisesRegex(UserError, "snapshot changed"):
            await self.send(draft)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM attempts").fetchone()[0], 0)

    async def test_preview_metadata_tamper_and_wrong_hash_rejected(self):
        draft = await self.draft()
        with self.assertRaisesRegex(UserError, "hash"):
            await self.service.send(draft["draft_id"], "a" * 64, "SEND", True)
        modified = dict(draft["attachment"], filename="renamed.epub")
        with self.store.db:
            self.store.db.execute("UPDATE drafts SET attachment=? WHERE id=?", (json.dumps(modified), draft["draft_id"]))
        with self.assertRaisesRegex(UserError, "content changed"):
            await self.send(draft)
        self.assertEqual(self.transport.calls, [])

    async def test_readonly_and_owner_authorization_before_file_upload(self):
        draft = await self.draft()
        with self.assertRaisesRegex(UserError, "authorization"):
            await self.service.send(draft["draft_id"], draft["preview_sha256"], "SEND", False)
        self.service.enable_sends = False
        with self.assertRaisesRegex(UserError, "disabled"):
            await self.send(draft)
        self.assertEqual(self.transport.calls, [])

    async def test_local_approval_optional_policy_applies_to_files(self):
        self.service.require_local_approval = True
        draft = await self.draft()
        with self.assertRaisesRegex(UserError, "approval"):
            await self.send(draft)
        self.store.approve_local(draft["draft_id"], draft["preview_sha256"])
        self.assertEqual((await self.send(draft))["status"], "simulated")

    async def test_same_key_reuses_preview_and_changed_file_or_peer_conflicts(self):
        draft = await self.draft()
        self.assertEqual(draft["draft_id"], (await self.draft())["draft_id"])
        with self.assertRaisesRegex(UserError, "Idempotency"):
            await self.draft(peer="1002")
        (self.inbox / "sample.epub").write_bytes(b"replacement")
        with self.assertRaisesRegex(UserError, "Idempotency"):
            await self.draft()

    async def test_duplicate_and_concurrent_sends_upload_once(self):
        draft = await self.draft()
        results = await asyncio.gather(*(self.send(draft) for _ in range(3)))
        self.assertEqual(len(self.transport.calls), 1)
        self.assertTrue(all(r["message_id"] == results[0]["message_id"] for r in results))
        with self.assertRaisesRegex(UserError, "identical"):
            await self.draft(key="attachment-other-key")

    async def test_timeout_unknown_never_retries_or_accepts_new_key(self):
        draft = await self.draft()
        self.transport.failure = TransportError("unknown")
        self.assertEqual((await self.send(draft))["status"], "unknown")
        self.assertEqual((await self.send(draft))["status"], "unknown")
        self.assertEqual(len(self.transport.calls), 1)
        with self.assertRaisesRegex(UserError, "identical"):
            await self.draft(key="attachment-other-key")

    async def test_forbidden_and_flood_wait_are_terminal(self):
        for index, failure in enumerate((TransportError("forbidden"), TransportError("rate_limited", 45))):
            draft = await self.draft(key=f"attachment-key-{index:04}", caption=str(index))
            self.transport.failure = failure
            self.assertEqual((await self.send(draft))["status"], failure.kind)
            count = len(self.transport.calls)
            self.assertEqual((await self.send(draft))["status"], failure.kind)
            self.assertEqual(len(self.transport.calls), count)
        self.assertGreater(self.store.meta("blocked_until"), self.store.clock())

    async def test_crash_recovery_preserves_snapshot_and_durable_random_id(self):
        draft = await self.draft()
        row = self.store.claim(draft["draft_id"], draft["preview_sha256"], "9000")
        random_id = row["random_id"]
        self.store.close()
        self.store = Store(self.directory / "outbox.sqlite3", "demo")
        self.addCleanup(self.store.close)
        self.service = Messenger(self.store, self.transport)
        await self.service.start()
        self.assertEqual((await self.send(draft))["status"], "unknown")
        self.assertEqual(self.store.item(draft["draft_id"])["random_id"], random_id)
        self.assertEqual(self.transport.calls, [])

    async def test_restart_sends_saved_snapshot_when_source_is_gone(self):
        draft = await self.draft()
        self.store.close()
        (self.inbox / "sample.epub").unlink()
        self.store = Store(self.directory / "outbox.sqlite3", "demo")
        self.addCleanup(self.store.close)
        self.service = Messenger(self.store, self.transport)
        await self.service.start()
        await self.service.list_peers()
        self.assertEqual((await self.send(draft))["status"], "simulated")
        self.assertEqual(self.transport.calls[0]["data"], self.data)

    async def test_cancelled_upload_is_unknown_and_never_retried(self):
        draft = await self.draft()
        self.transport.failure = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.send(draft)
        self.assertEqual((await self.send(draft))["status"], "unknown")
        self.assertEqual(len(self.transport.calls), 1)

    async def test_cancelled_draft_does_not_upload(self):
        draft = await self.draft()
        self.store.cancel(draft["draft_id"])
        self.assertEqual((await self.send(draft))["status"], "cancelled")
        self.assertEqual(self.transport.calls, [])

    async def test_snapshot_quota_prevents_unbounded_copying(self):
        with patch("ken_mcp.attachments.MAX_STAGED_BYTES", 1):
            with self.assertRaisesRegex(UserError, "storage is full"):
                await self.draft()

    async def test_saved_messages_uses_verified_owner_without_contact_lookup(self):
        service = Messenger(self.store, self.transport)
        await service.start()
        draft = await service.prepare("9000", "Self test", "self-attachment-key-01", attachment_filename="sample.epub")
        sent = await service.send(draft["draft_id"], draft["preview_sha256"], "SEND", True)
        self.assertEqual(sent["status"], "simulated")
        self.assertEqual(self.transport.calls[0]["peer_id"], "9000")

    async def test_delivery_progress_survives_restart_without_file_contents(self):
        draft = await self.draft()
        sent = await self.send(draft)
        self.assertEqual(sent["delivery_progress"], {"stage": "dispatching", "uploaded_bytes": len(self.data), "total_bytes": len(self.data)})
        self.store.close()
        self.store = Store(self.directory / "outbox.sqlite3", "demo")
        self.addCleanup(self.store.close)
        self.assertEqual(self.store.public(self.store.item(draft["draft_id"]))["delivery_progress"], sent["delivery_progress"])

    async def test_rejected_upload_retains_sanitized_diagnostic_and_progress(self):
        from telethon import errors
        failure = TransportError("rejected", rpc_error=errors.BadRequestError(None, "PRIVATE", code=400))
        failure.stage = "upload"
        self.transport.failure = failure
        draft = await self.draft()
        rejected = await self.send(draft)
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["delivery_progress"]["uploaded_bytes"], 0)
        self.assertIn("upload, BadRequestError, RPC 400", rejected["detail"])
        self.assertNotIn("PRIVATE", rejected["detail"])


class AttachmentMigrationTests(unittest.TestCase):
    def test_old_text_database_upgrades_without_changing_existing_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.sqlite3"
            old = Store(path, "demo")
            preview = old.prepare("existing-text-key-01", "9000", {"peer_id": "1001", "name": "Alex"}, "Hello", None)
            old.db.execute("ALTER TABLE drafts DROP COLUMN attachment")
            old.db.commit()
            old.close()
            upgraded = Store(path, "demo")
            try:
                row = upgraded.public(upgraded.item(preview["draft_id"]))
                self.assertEqual(row["preview_sha256"], preview["preview_sha256"])
                self.assertIsNone(row["attachment"])
            finally:
                upgraded.close()
