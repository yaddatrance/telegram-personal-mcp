import asyncio
import hashlib
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
        self.assertIsNone(request.send_as)
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

    async def test_document_upload_and_raw_send_preserve_exact_preview(self):
        from telethon import functions, types
        document = b"Synthetic EPUB fixture"
        async def uploaded(stream, **kwargs):
            self.assertEqual(stream.read(), document)
            callback = kwargs.pop("progress_callback")
            callback(len(document), len(document))
            self.assertEqual(kwargs, {"file_size": len(document), "file_name": "sample.epub"})
            return types.InputFile(id=5, parts=1, name="sample.epub", md5_checksum="synthetic")
        self.client.upload_file.side_effect = uploaded
        self.client.return_value = types.UpdateShortSentMessage(id=12, pts=1, pts_count=1, date=datetime.now(timezone.utc), out=True)
        result = await self.adapter.send_document("1001", "Exact caption", 3, 987654321,
            {"filename": "sample.epub", "mime_type": "application/epub+zip"}, document)
        self.assertEqual(result["message_id"], 12)
        request = self.client.call_args.args[0]
        self.assertIsInstance(request, functions.messages.SendMediaRequest)
        self.assertEqual(request.random_id, 987654321)
        self.assertEqual(request.peer, self.adapter.known["1001"][1])
        self.assertEqual(request.message, "Exact caption")
        self.assertEqual(request.reply_to.reply_to_msg_id, 3)
        self.assertEqual(request.media.mime_type, "application/epub+zip")
        self.assertEqual(request.media.attributes[0].file_name, "sample.epub")
        self.assertTrue(request.media.force_file)
        self.assertIsNone(request.send_as)
        self.assertIsNone(request.allow_paid_stars)
        self.client.upload_file.assert_awaited_once()
        self.assertEqual(self.client.call_count, 1)

    async def test_document_upload_failure_never_calls_send_media(self):
        from telethon import errors
        for error, expected in ((TimeoutError(), "unknown"),
                                (errors.ForbiddenError(None, "PRIVATE", code=403), "forbidden"),
                                (errors.FloodWaitError(None, capture=45), "rate_limited")):
            with self.subTest(error=type(error).__name__):
                self.client.upload_file.reset_mock()
                self.client.upload_file.side_effect = error
                with self.assertRaises(TransportError) as caught:
                    await self.adapter.send_document("1001", "", None, 123,
                        {"filename": "sample.epub", "mime_type": "application/epub+zip"}, b"fixture")
                self.assertEqual(caught.exception.kind, expected)
                self.client.upload_file.assert_awaited_once()
                self.assertEqual(self.client.call_count, 0)

    async def _actual_sdk_upload(self, data, filename, mime_type, reject_part=False):
        from telethon import functions, types
        from telethon.extensions import BinaryReader
        from ken_mcp.account import make_client
        client = make_client({"api_id": 12345, "api_hash": "0" * 32})
        adapter = TelethonTransport(client)
        adapter.known = dict(self.adapter.known)
        parts, sent = [], []

        async def fake_rpc(sender, request, **kwargs):
            # Exercise the real SDK upload and TL serialization, replacing only RPC I/O.
            with BinaryReader(bytes(request)) as reader:
                wire = reader.tgread_object()
            if isinstance(wire, functions.upload.SaveFilePartRequest):
                parts.append(wire)
                return not reject_part
            self.assertIsInstance(wire, functions.messages.SendMediaRequest)
            sent.append(wire)
            self.assertEqual(b"".join(part.bytes for part in parts), data)
            self.assertEqual([part.file_part for part in parts], list(range(len(parts))))
            self.assertEqual({part.file_id for part in parts}, {wire.media.file.id})
            self.assertEqual(wire.media.file.parts, len(parts))
            self.assertEqual(wire.media.file.md5_checksum, hashlib.md5(data).hexdigest())
            self.assertEqual(wire.media.file.name, filename)
            self.assertEqual(wire.media.attributes, [types.DocumentAttributeFilename(filename)])
            self.assertEqual(wire.media.mime_type, mime_type)
            self.assertTrue(wire.media.force_file)
            self.assertIsNone(wire.media.ttl_seconds)
            self.assertIsNone(wire.media.thumb)
            self.assertIsNone(wire.send_as)
            self.assertEqual(wire.random_id, 987654321)
            return types.UpdateShortSentMessage(id=12, pts=1, pts_count=1,
                date=datetime.now(timezone.utc), out=True)

        client._call = AsyncMock(side_effect=fake_rpc)
        try:
            if reject_part:
                with self.assertRaises(TransportError) as raised:
                    await adapter.send_document("1001", "Synthetic caption", None, 987654321,
                        {"filename": filename, "mime_type": mime_type}, data)
                self.assertEqual(raised.exception.stage, "upload")
                self.assertEqual(len(parts), 1)
                self.assertEqual(sent, [])
                self.assertEqual(client._call.await_count, 1)
            else:
                result = await adapter.send_document("1001", "Synthetic caption", None, 987654321,
                    {"filename": filename, "mime_type": mime_type}, data)
                self.assertEqual(result["message_id"], 12)
                self.assertEqual(len(sent), 1)
                self.assertEqual(client._call.await_count, len(parts) + 1)
            self.assertFalse(client.is_connected())
            return parts
        finally:
            await client.disconnect()

    async def test_real_sdk_tiny_text_upload_contains_nonempty_bytes_and_valid_metadata(self):
        parts = await self._actual_sdk_upload(b"Synthetic tiny text document.\n", "sample.txt", "text/plain")
        self.assertEqual(len(parts), 1)
        self.assertGreater(len(parts[0].bytes), 0)

    async def test_real_sdk_multipart_upload_preserves_every_byte_including_tail(self):
        parts = await self._actual_sdk_upload(b"Synthetic file contents\n" * 14000 + b"tail", "sample.epub", "application/epub+zip")
        self.assertGreater(len(parts), 1)
        self.assertTrue(parts[-1].bytes.endswith(b"tail"))

    async def test_real_sdk_rejected_upload_part_never_dispatches_or_retries(self):
        await self._actual_sdk_upload(b"Synthetic fixture", "sample.txt", "text/plain", reject_part=True)

    async def test_document_send_timeout_is_unknown_without_reupload_or_retry(self):
        from telethon import types
        self.client.upload_file.return_value = types.InputFile(5, 1, "sample.epub", "synthetic")
        self.client.side_effect = TimeoutError("PRIVATE")
        with self.assertRaises(TransportError) as caught:
            await self.adapter.send_document("1001", "", None, 123,
                {"filename": "sample.epub", "mime_type": "application/epub+zip"}, b"fixture")
        self.assertEqual(caught.exception.kind, "unknown")
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.client.upload_file.assert_awaited_once()
        self.assertEqual(self.client.call_count, 1)

    async def test_sent_document_metadata_can_be_verified_without_downloading(self):
        from telethon import types
        now = datetime.now(timezone.utc)
        document = types.Document(id=1, access_hash=2, file_reference=b"synthetic", date=now,
            mime_type="application/epub+zip", size=531467, dc_id=2,
            attributes=[types.DocumentAttributeFilename("sample.epub")])
        message = types.Message(id=12, peer_id=types.PeerUser(1001), from_id=types.PeerUser(9000),
            message="Sample book", date=now, out=True, media=types.MessageMediaDocument(document=document))
        self.client.get_messages.return_value = message
        result = await self.adapter.message("1001", 12)
        self.assertEqual(result["attachment"], {"filename": "sample.epub", "size_bytes": 531467, "mime_type": "application/epub+zip"})
        self.assertTrue(result["outgoing"])
        self.assertEqual(result["peer_id"], "1001")
        self.assertNotIn("access_hash", result["attachment"])

    async def test_rpc_diagnostic_contains_only_known_type_code_and_stage(self):
        from telethon import errors
        self.client.upload_file.side_effect = errors.BadRequestError(None, "PRIVATE TOKEN PHONE FILE", code=400)
        with self.assertRaises(TransportError) as caught:
            await self.adapter.send_document("1001", "", None, 123,
                {"filename": "sample.epub", "mime_type": "application/epub+zip"}, b"fixture")
        self.assertEqual(caught.exception.diagnostic(), " [upload, BadRequestError, RPC 400]")
        self.assertNotIn("PRIVATE", caught.exception.diagnostic())

    async def test_partial_upload_and_send_failure_have_distinct_progress(self):
        from telethon import errors, types
        progress = []
        async def partial_upload(stream, **kwargs):
            kwargs['progress_callback'](3, 7)
            raise errors.BadRequestError(None, "PRIVATE", code=400)
        self.client.upload_file.side_effect = partial_upload
        with self.assertRaises(TransportError) as caught:
            await self.adapter.send_document("1001", "", None, 123,
                {"filename": "sample.epub", "mime_type": "application/epub+zip"}, b"fixture",
                lambda *values: progress.append(values))
        self.assertEqual(progress, [("uploading", 0, 7), ("uploading", 3, 7)])
        self.assertEqual(caught.exception.stage, "upload")
        self.assertEqual(self.client.call_count, 0)
        progress.clear()
        self.client.upload_file.side_effect = None
        self.client.upload_file.return_value = types.InputFile(1, 1, "sample.epub", "synthetic")
        self.client.side_effect = errors.BadRequestError(None, "PRIVATE", code=400)
        with self.assertRaises(TransportError) as caught:
            await self.adapter.send_document("1001", "", None, 123,
                {"filename": "sample.epub", "mime_type": "application/epub+zip"}, b"fixture",
                lambda *values: progress.append(values))
        self.assertEqual(progress[-2:], [("uploaded", 7, 7), ("dispatching", 7, 7)])
        self.assertEqual(caught.exception.stage, "send_document")

    async def test_saved_messages_omits_sender_override(self):
        from telethon import types
        self.adapter.known["9000"] = ({"peer_id": "9000", "name": "Owner", "kind": "user"}, types.InputPeerSelf())
        self.client.upload_file.return_value = types.InputFile(1, 1, "sample.txt", "synthetic")
        self.client.return_value = types.UpdateShortSentMessage(id=13, pts=1, pts_count=1, date=datetime.now(timezone.utc), out=True)
        await self.adapter.send_document("9000", "Self test", None, 123,
            {"filename": "sample.txt", "mime_type": "text/plain"}, b"fixture")
        request = self.client.call_args.args[0]
        self.assertIsInstance(request.peer, types.InputPeerSelf)
        self.assertIsNone(request.send_as)

    async def test_group_sender_uses_concrete_verified_owner_peer(self):
        from telethon import types
        self.client.get_me.return_value = types.User(id=9000, is_self=True, first_name="Owner", access_hash=1234)
        await self.adapter.identity()
        self.adapter.known["-2001"] = ({"peer_id": "-2001", "name": "Group", "kind": "group"}, types.InputPeerChat(2001))
        self.client.return_value = types.UpdateShortSentMessage(id=13, pts=1, pts_count=1, date=datetime.now(timezone.utc), out=True)
        await self.adapter.send("-2001", "Test", None, 123)
        request = self.client.call_args.args[0]
        self.assertIsInstance(request.send_as, types.InputPeerUser)
        self.assertEqual(request.send_as.user_id, 9000)


if __name__ == "__main__":
    unittest.main()
