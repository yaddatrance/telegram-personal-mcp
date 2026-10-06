"""SQLite outbox shared by the MCP process and the owner's local approval UI."""
import hashlib
import json
import secrets
import sqlite3
import time
from pathlib import Path


class UserError(Exception):
    pass


def digest_message(account_id, peer_id, text, reply_to, attachment=None):
    fields = [account_id, peer_id, text, reply_to]
    if attachment is not None:
        fields.append(attachment)
    canonical = json.dumps(fields, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path, mode, clock=time.time):
        self.directory = Path(path).resolve().parent
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.db = sqlite3.connect(str(path), timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS drafts (
                id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, account_id TEXT NOT NULL,
                peer_id TEXT NOT NULL, peer_name TEXT NOT NULL, text TEXT NOT NULL, reply_to INTEGER,
                digest TEXT NOT NULL, random_id INTEGER UNIQUE NOT NULL, status TEXT NOT NULL,
                created REAL NOT NULL, expires REAL NOT NULL, approved_until REAL NOT NULL DEFAULT 0,
                message_id INTEGER, detail TEXT NOT NULL DEFAULT '', retry_at REAL NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS draft_digest ON drafts(digest);
            CREATE TABLE IF NOT EXISTS attempts (draft_id TEXT NOT NULL, created REAL NOT NULL);
        """)
        if "attachment" not in {row[1] for row in self.db.execute("PRAGMA table_info(drafts)")}:
            self.db.execute("ALTER TABLE drafts ADD COLUMN attachment TEXT")
        if "delivery_progress" not in {row[1] for row in self.db.execute("PRAGMA table_info(drafts)")}:
            self.db.execute("ALTER TABLE drafts ADD COLUMN delivery_progress TEXT")
        current = self.meta("mode")
        if current and current != mode:
            self.db.close()
            raise UserError("Demo and live profiles must use separate data directories.")
        with self.db:
            self.set_meta("mode", mode)

    def meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, json.dumps(value)))

    def bind_account(self, account_id):
        current = self.meta("account_id")
        if current and current != account_id:
            raise UserError("This outbox belongs to a different Telegram account. Use a separate profile.")
        with self.db:
            self.set_meta("account_id", account_id)

    def recover(self):
        with self.db:
            self.db.execute("UPDATE drafts SET status='unknown', detail=? WHERE status='sending'",
                            ("Process interrupted during sending. Delivery may have happened. No automatic retry.",))

    def item(self, draft_id):
        if not isinstance(draft_id, str):
            raise UserError("Invalid draft ID.")
        row = self.db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if not row:
            raise UserError("Draft not found.")
        return dict(row)

    def public(self, row):
        # random_id is kept local, stable and immutable; no session/access hashes are returned.
        return {"draft_id": row["id"], "peer_id": row["peer_id"], "peer_name": row["peer_name"],
                "account_id": row["account_id"], "text": row["text"], "reply_to_message_id": row["reply_to"],
                "preview_sha256": row["digest"], "status": row["status"], "detail": row["detail"],
                "created_at": row["created"], "expires_at": row["expires"], "retry_at": row["retry_at"],
                "message_id": row["message_id"],
                "attachment": json.loads(row["attachment"]) if row.get("attachment") else None,
                "delivery_progress": json.loads(row["delivery_progress"]) if row.get("delivery_progress") else None,
                "locally_approved": row["approved_until"] > self.clock() and row["status"] == "prepared"}

    def prepare(self, key, account_id, peer, text, reply_to, attachment=None):
        digest = digest_message(account_id, peer["peer_id"], text, reply_to, attachment)
        with self.db:
            row = self.db.execute("SELECT * FROM drafts WHERE request_key=?", (key,)).fetchone()
            if row:
                if row["digest"] != digest:
                    raise UserError("Idempotency key already belongs to a different recipient, reply, text, or attachment.")
                return self.public(dict(row))
            self.db.execute("UPDATE drafts SET status='expired',detail='Draft expired; prepare a new request.' WHERE status='prepared' AND expires<=?", (self.clock(),))
            duplicate = self.db.execute("""SELECT id FROM drafts WHERE digest=? AND
                (status IN ('prepared','sending','unknown') OR
                 (status IN ('sent','simulated') AND EXISTS
                  (SELECT 1 FROM attempts WHERE draft_id=drafts.id AND attempts.created>?)))""", (digest, self.clock() - 600)).fetchone()
            if duplicate:
                raise UserError("An identical request is pending, recently sent, or uncertain. Check the existing outbox entry.")
            draft_id = secrets.token_hex(16)
            self.db.execute("""INSERT INTO drafts
                (id,request_key,account_id,peer_id,peer_name,text,reply_to,digest,random_id,status,created,expires,attachment)
                VALUES (?,?,?,?,?,?,?,?,?,'prepared',?,?,?)""",
                (draft_id, key, account_id, peer["peer_id"], peer["name"], text, reply_to, digest,
                 secrets.randbelow(2**63 - 1) + 1, self.clock(), self.clock() + 1800,
                 json.dumps(attachment, sort_keys=True) if attachment else None))
            return self.public(self.item(draft_id))

    def approve_local(self, draft_id, expected_digest):
        # This method is intentionally NOT exposed by MCP.
        with self.db:
            row = self.item(draft_id)
            if row["status"] != "prepared" or row["expires"] <= self.clock():
                raise UserError("Only a fresh prepared request can be approved.")
            if not secrets.compare_digest(row["digest"], expected_digest):
                raise UserError("Preview changed; review it again.")
            self.db.execute("UPDATE drafts SET approved_until=? WHERE id=?", (min(row["expires"], self.clock() + 300), draft_id))

    def cancel(self, draft_id):
        with self.db:
            self.item(draft_id)
            self.db.execute("UPDATE drafts SET status='cancelled',approved_until=0,detail='Cancelled locally.' WHERE id=? AND status='prepared'", (draft_id,))
        return self.public(self.item(draft_id))

    def claim(self, draft_id, digest, account_id, require_local_approval=False):
        # Atomic approval consumption and rate check, before contacting Telegram.
        try:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.item(draft_id)
            if row["account_id"] != account_id or not secrets.compare_digest(row["digest"], digest):
                raise UserError("Account or preview hash does not match the prepared request.")
            if row["status"] != "prepared":
                self.db.commit()
                return None
            now = self.clock()
            if row["expires"] <= now:
                raise UserError("Draft expired. Prepare and review a new request.")
            if require_local_approval and row["approved_until"] <= now:
                raise UserError("Local owner approval is required. Open the review window or run the approve command.")
            if self.meta("blocked_until", 0) > now:
                raise UserError("Telegram cooldown is active. No request was sent.")
            minute = self.db.execute("SELECT count(*) FROM attempts WHERE created>?", (now - 60,)).fetchone()[0]
            hour = self.db.execute("SELECT count(*) FROM attempts WHERE created>?", (now - 3600,)).fetchone()[0]
            if minute >= 5 or hour >= 30:
                raise UserError("Local send limit reached (5/minute, 30/hour). Wait and review again.")
            self.db.execute("UPDATE drafts SET status='sending',approved_until=0 WHERE id=?", (draft_id,))
            self.db.execute("INSERT INTO attempts VALUES (?,?)", (draft_id, now))
            self.db.commit()
            return row
        except BaseException:
            self.db.rollback()
            raise

    def finish(self, draft_id, status, detail, message_id=None, cooldown=0):
        with self.db:
            retry_at = self.clock() + cooldown if cooldown else 0
            if cooldown:
                self.set_meta("blocked_until", max(self.meta("blocked_until", 0), retry_at))
            self.db.execute("UPDATE drafts SET status=?,detail=?,message_id=?,retry_at=? WHERE id=?",
                            (status, detail, message_id, retry_at, draft_id))
        return self.public(self.item(draft_id))

    def note_delivery_progress(self, draft_id, stage, uploaded_bytes, total_bytes):
        if stage not in ("uploading", "uploaded", "dispatching") or not (type(uploaded_bytes) is int
                and type(total_bytes) is int and 0 <= uploaded_bytes <= total_bytes):
            raise UserError("Invalid local delivery progress.")
        progress = {"stage": stage, "uploaded_bytes": uploaded_bytes, "total_bytes": total_bytes}
        with self.db:
            self.db.execute("UPDATE drafts SET delivery_progress=? WHERE id=? AND status='sending'",
                            (json.dumps(progress), draft_id))

    def recent(self, limit=20):
        return [self.public(dict(row)) for row in self.db.execute("SELECT * FROM drafts ORDER BY created DESC,rowid DESC LIMIT ?", (limit,))]

    def close(self):
        self.db.close()
