"""Async client boundary, with deterministic offline fixtures."""
import asyncio
from datetime import datetime, timezone


class TransportError(Exception):
    def __init__(self, kind, seconds=0):
        self.kind = kind
        self.seconds = max(0, int(seconds))
        super().__init__("Telegram request: " + kind)


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


class TelethonTransport:
    is_demo = False

    def __init__(self, client):
        self.client = client
        self.known = {}

    async def _call(self, awaitable):
        from telethon import errors
        try:
            return await asyncio.wait_for(awaitable, timeout=30)
        except (errors.FloodWaitError, errors.SlowModeWaitError) as error:
            raise TransportError("rate_limited", error.seconds) from None
        except errors.PeerFloodError:
            raise TransportError("spam_restricted", 86400) from None
        except errors.ForbiddenError:
            raise TransportError("forbidden") from None
        except errors.UnauthorizedError:
            raise TransportError("unauthorized") from None
        except errors.RPCError as error:
            code = error.code
            kind = "unknown" if not isinstance(code, int) or code >= 500 or code in (408, -503) else "rejected"
            raise TransportError(kind) from None
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
        me = await self._call(self.client.get_me())
        if me is None or me.bot:
            raise TransportError("unauthorized")
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

    def _message(self, message, peer_id):
        from telethon import utils
        if not message or not getattr(message, "peer_id", None) or str(utils.get_peer_id(message.peer_id)) != peer_id:
            return None
        return {"id": message.id, "peer_id": peer_id, "sender_id": str(message.sender_id) if message.sender_id else None,
                "outgoing": bool(message.out), "text": (message.message or "")[:8000],
                "date": message.date.isoformat() if message.date else None,
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
            no_webpage=True, send_as=types.InputPeerSelf(),
            reply_to=types.InputReplyToMessage(reply_to_msg_id=reply_to) if reply_to else None)
        # Raw API preserves the durable random_id. No high-level helper generates a new one.
        result = await self._call(self.client(request))
        if isinstance(result, types.UpdateShortSentMessage):
            return {"message_id": result.id}
        for update in getattr(result, "updates", []):
            if isinstance(update, types.UpdateMessageID) and update.random_id == random_id:
                return {"message_id": update.id}
        # A valid response acknowledges acceptance, even if it has no message-ID mapping.
        return {"message_id": None}

    async def close(self):
        await self.client.disconnect()
