"""Async client boundary, with deterministic offline fixtures."""
import asyncio
import io
from datetime import datetime, timezone


class TransportError(Exception):
    def __init__(self, kind, seconds=0, rpc_error=None):
        self.kind = kind
        self.seconds = max(0, int(seconds))
        self.rpc_name = None
        self.rpc_code = None
        self.stage = None
        if rpc_error is not None:
            from telethon import errors
            name = type(rpc_error).__name__
            self.rpc_name = name if getattr(errors, name, None) is type(rpc_error) else "RPCError"
            code = getattr(rpc_error, "code", None)
            self.rpc_code = code if type(code) is int and -9999 <= code <= 9999 else None
        super().__init__("Telegram request: " + kind)

    def diagnostic(self):
        parts = [part for part in (self.stage, self.rpc_name,
                 f"RPC {self.rpc_code}" if self.rpc_code is not None else None) if part]
        return " [" + ", ".join(parts) + "]" if parts else ""


class FakeTransport:
    is_demo = True

    def __init__(self):
        self.calls = []
        self.failure = None
        self.peers = {
            "1001": {"peer_id": "1001", "name": "Alex Morgan", "username": "alex_demo", "kind": "user"},
            "1002": {"peer_id": "1002", "name": "Alex Rivera", "username": "rivera_demo", "kind": "user"},
            "-2001": {"peer_id": "-2001", "name": "Weekend plans", "username": None, "kind": "group"},
        }

    async def identity(self):
        self.peers["9000"] = {"peer_id": "9000", "name": "Ken (demo)", "username": "ken_demo", "kind": "user"}
        return {"account_id": "9000", "name": "Ken (demo)", "username": "ken_demo", "is_bot": False}

    async def list_chats(self, query, limit, offset):
        rows = [dict(p) for p in self.peers.values() if query.casefold() in (p["name"] + " " + (p["username"] or "")).casefold()]
        return {"chats": rows[offset:offset + limit], "next_offset": offset + limit if offset + limit < len(rows) else None,
                "scope": "Synthetic demo dialogs"}

    async def list_contacts(self, query, limit, offset):
        rows = [dict(p) for p in self.peers.values() if p["kind"] == "user" and query.casefold() in (p["name"] + " " + p["username"]).casefold()]
        return {"contacts": rows[offset:offset + limit], "next_offset": offset + limit if offset + limit < len(rows) else None}

    async def resolve(self, peer_id):
        if peer_id not in self.peers:
            raise TransportError("unknown_peer")
        return dict(self.peers[peer_id])

    async def history(self, peer_id, limit, before):
        await self.resolve(peer_id)
        rows = [{"id": i, "peer_id": peer_id, "sender_id": peer_id, "outgoing": False,
                 "text": "Demo conversation — are we still on for Saturday?" if i == 3 else "Synthetic sample message.",
                 "date": "2026-10-02T12:00:00+00:00", "media_type": None} for i in (3, 2, 1)]
        return [r for r in rows if not before or r["id"] < before][:limit]

    async def message(self, peer_id, message_id):
        return next((m for m in await self.history(peer_id, 50, 0) if m["id"] == message_id), None)

    async def send(self, peer_id, text, reply_to, random_id):
        self.calls.append({"peer_id": peer_id, "text": text, "reply_to": reply_to, "random_id": random_id})
        await asyncio.sleep(0)
        if self.failure:
            failure, self.failure = self.failure, None
            raise failure
        return {"message_id": 100 + len(self.calls)}

    async def close(self):
        pass

    async def send_document(self, peer_id, text, reply_to, random_id, attachment, data, progress=None):
        if progress:
            progress("uploading", 0, len(data))
        self.calls.append({"peer_id": peer_id, "text": text, "reply_to": reply_to,
                           "random_id": random_id, "attachment": dict(attachment), "data": bytes(data)})
        await asyncio.sleep(0)
        if self.failure:
            failure, self.failure = self.failure, None
            raise failure
        if progress:
            progress("uploaded", len(data), len(data))
            progress("dispatching", len(data), len(data))
        return {"message_id": 100 + len(self.calls)}


class TelethonTransport:
    is_demo = False

    def __init__(self, client):
        self.client = client
        self.known = {}
        self.owner_peer = None

    async def _call(self, awaitable, timeout=30):
        from telethon import errors
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout)
        except (errors.FloodWaitError, errors.SlowModeWaitError) as error:
            raise TransportError("rate_limited", error.seconds, error) from None
        except errors.PeerFloodError as error:
            raise TransportError("spam_restricted", 86400, error) from None
        except errors.ForbiddenError as error:
            raise TransportError("forbidden", rpc_error=error) from None
        except errors.UnauthorizedError as error:
            raise TransportError("unauthorized", rpc_error=error) from None
        except errors.RPCError as error:
            code = error.code
            kind = "unknown" if not isinstance(code, int) or code >= 500 or code in (408, -503) else "rejected"
            raise TransportError(kind, rpc_error=error) from None
        except (TimeoutError, OSError, ConnectionError):
            raise TransportError("unknown") from None
        except asyncio.CancelledError:
            raise
        except Exception:
            raise TransportError("unknown") from None

    def _remember(self, entity):
        from telethon import types, utils
        # V1 supports existing private chats and groups, not broadcast/paid channels.
        if isinstance(entity, types.User):
            if entity.deleted or entity.bot:
                return None
            kind = "user"
            name = " ".join(x for x in (entity.first_name, entity.last_name) if x)
        elif isinstance(entity, types.Chat):
            kind, name = "group", entity.title
        elif isinstance(entity, types.Channel) and entity.megagroup and not entity.broadcast:
            kind, name = "group", entity.title
        else:
            return None
        try:
            input_peer = utils.get_input_peer(entity)
        except (TypeError, ValueError):
            return None
        peer_id = str(utils.get_peer_id(entity))
        public = {"peer_id": peer_id, "name": name, "username": getattr(entity, "username", None), "kind": kind}
        self.known[peer_id] = (public, input_peer)
        return dict(public)

    async def identity(self):
        from telethon import utils
        me = await self._call(self.client.get_me())
        if me is None or me.bot:
            raise TransportError("unauthorized")
        self._remember(me)
        self.owner_peer = utils.get_input_peer(me, allow_self=False)
        return {"account_id": str(me.id), "name": " ".join(x for x in (me.first_name, me.last_name) if x),
                "username": me.username, "is_bot": bool(me.bot)}

    async def list_chats(self, query, limit, offset):
        # Bounded scan; disclose scope rather than imply all account history was searched.
        dialogs = await self._call(self.client.get_dialogs(limit=200))
        rows = []
        for dialog in dialogs:
            peer = self._remember(dialog.entity)
            if peer and query.casefold() in (peer["name"] + " " + (peer["username"] or "")).casefold():
                rows.append(peer)
        return {"chats": rows[offset:offset + limit],
                "next_offset": offset + limit if offset + limit < len(rows) else None,
                "scope": "First 200 dialogs; private chats and groups only", "scan_capped": len(dialogs) >= 200}

    async def list_contacts(self, query, limit, offset):
        from telethon import functions
        result = await self._call(self.client(functions.contacts.GetContactsRequest(hash=0)))
        rows = []
        for user in getattr(result, "users", []):
            peer = self._remember(user)
            if peer and query.casefold() in (peer["name"] + " " + (peer["username"] or "")).casefold():
                rows.append(peer)
        return {"contacts": rows[offset:offset + limit],
                "next_offset": offset + limit if offset + limit < len(rows) else None,
                "scope": "Existing contacts; phone numbers omitted"}

    async def resolve(self, peer_id):
        if peer_id not in self.known:
            raise TransportError("unknown_peer")
        return dict(self.known[peer_id][0])

    def _send_as(self, peer_id):
        # Private chats/Saved Messages reject an explicit InputPeerSelf override.
        # Their natural sender is the authenticated account. For groups, force
        # the owner's concrete peer so an anonymous/channel default cannot apply.
        if self.known[peer_id][0]["kind"] == "user":
            return None
        if self.owner_peer is None:
            raise TransportError("unauthorized")
        return self.owner_peer

    def _message(self, message, peer_id):
        from telethon import types, utils
        if not message or not getattr(message, "peer_id", None) or str(utils.get_peer_id(message.peer_id)) != peer_id:
            return None
        document = getattr(getattr(message, "media", None), "document", None)
        attachment = None
        if isinstance(document, types.Document):
            attachment = {"filename": next((attribute.file_name for attribute in document.attributes
                if isinstance(attribute, types.DocumentAttributeFilename)), None),
                "size_bytes": document.size, "mime_type": document.mime_type}
        return {"id": message.id, "peer_id": peer_id, "sender_id": str(message.sender_id) if message.sender_id else None,
                "outgoing": bool(message.out), "text": (message.message or "")[:8000],
                "date": message.date.isoformat() if message.date else None,
                "attachment": attachment,
                "media_type": type(message.media).__name__ if message.media else None}

    async def history(self, peer_id, limit, before):
        await self.resolve(peer_id)
        messages = await self._call(self.client.get_messages(self.known[peer_id][1], limit=limit, offset_id=before))
        return [row for message in messages if (row := self._message(message, peer_id)) is not None]

    async def message(self, peer_id, message_id):
        await self.resolve(peer_id)
        message = await self._call(self.client.get_messages(self.known[peer_id][1], ids=message_id))
        return self._message(message, peer_id)

    async def send(self, peer_id, text, reply_to, random_id):
        from telethon import functions, types
        await self.resolve(peer_id)
        request = functions.messages.SendMessageRequest(
            peer=self.known[peer_id][1], message=text, random_id=random_id,
            no_webpage=True, send_as=self._send_as(peer_id),
            reply_to=types.InputReplyToMessage(reply_to_msg_id=reply_to) if reply_to else None)
        # Raw API preserves the durable random_id. No high-level helper generates a new one.
        result = await self._call(self.client(request))
        return self._sent_result(result, random_id)

    async def send_document(self, peer_id, text, reply_to, random_id, attachment, data, progress=None):
        from telethon import functions, types
        await self.resolve(peer_id)
        send_as = self._send_as(peer_id)
        def uploaded_progress(sent, total):
            if progress:
                progress("uploading", int(sent), int(total))
        if progress:
            progress("uploading", 0, len(data))
        # Read verified immutable bytes, never pass arbitrary paths/URLs to Telethon.
        with io.BytesIO(data) as document:
            try:
                uploaded = await self._call(self.client.upload_file(document, file_size=len(data),
                    file_name=attachment["filename"], progress_callback=uploaded_progress), timeout=300)
            except TransportError as error:
                error.stage = "upload"
                raise
        if progress:
            progress("uploaded", len(data), len(data))
        media = types.InputMediaUploadedDocument(file=uploaded, mime_type=attachment["mime_type"],
            attributes=[types.DocumentAttributeFilename(attachment["filename"])], force_file=True)
        request = functions.messages.SendMediaRequest(peer=self.known[peer_id][1], media=media,
            message=text, random_id=random_id, send_as=send_as,
            reply_to=types.InputReplyToMessage(reply_to_msg_id=reply_to) if reply_to else None)
        try:
            if progress:
                progress("dispatching", len(data), len(data))
            result = await self._call(self.client(request))
        except TransportError as error:
            error.stage = "send_document"
            raise
        return self._sent_result(result, random_id)

    def _sent_result(self, result, random_id):
        from telethon import types
        if isinstance(result, types.UpdateShortSentMessage):
            return {"message_id": result.id}
        for update in getattr(result, "updates", []):
            if isinstance(update, types.UpdateMessageID) and update.random_id == random_id:
                return {"message_id": update.id}
        # A valid response acknowledges acceptance, even if it has no message-ID mapping.
        return {"message_id": None}

    async def close(self):
        await self.client.disconnect()
