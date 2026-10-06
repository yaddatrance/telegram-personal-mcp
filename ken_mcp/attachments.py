"""Bounded, explicitly staged local documents; immutable private snapshots."""
import hashlib
import os
import re
import stat
import unicodedata
from pathlib import Path

from .store import UserError


MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_STAGED_BYTES = 250 * 1024 * 1024
MIME_TYPES = {
    ".epub": "application/epub+zip", ".pdf": "application/pdf", ".txt": "text/plain",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def filename(value):
    if (not isinstance(value, str) or not value or len(value) > 180 or value.startswith(".")
            or value[-1] in " ." or any(c in value for c in '/\\:<>"|?*')
            or any(unicodedata.category(c).startswith("C") for c in value)
            or re.fullmatch(r"(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?", value)):
        raise UserError("Use a plain filename from list_attachments, without directories or special characters.")
    if Path(value).suffix.lower() not in MIME_TYPES:
        raise UserError("Unsupported attachment type. Use EPUB, PDF, TXT, PNG, JPG, WEBP, DOCX, XLSX or PPTX.")
    return value


def _regular(info):
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and not getattr(info, "st_file_attributes", 0) & 0x400


def _read_file(root, name):
    """Open once, check the actual handle, then read bounded bytes from that handle."""
    path = root / name
    try:
        before = path.lstat()
        if not _regular(before) or path.resolve(strict=True).parent != root:
            raise UserError("Attachment must be an ordinary file inside the configured inbox; links are not allowed.")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as source:
            current = os.fstat(source.fileno())
            if not _regular(current) or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                raise UserError("Attachment changed while opening. Prepare it again.")
            if os.name == "nt":
                import ctypes
                import msvcrt
                from ctypes import wintypes
                final_path = ctypes.windll.kernel32.GetFinalPathNameByHandleW
                final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
                final_path.restype = wintypes.DWORD
                buffer = ctypes.create_unicode_buffer(32768)
                count = final_path(msvcrt.get_osfhandle(source.fileno()), buffer, len(buffer), 0)
                if not 0 < count < len(buffer):
                    raise UserError("Could not verify the attachment's local file handle.")
                resolved = buffer.value
                if resolved.startswith("\\\\?\\UNC\\"):
                    resolved = "\\\\" + resolved[8:]
                elif resolved.startswith("\\\\?\\"):
                    resolved = resolved[4:]
                if Path(resolved) != path:
                    raise UserError("Attachment path changed while opening. Links are not allowed.")
            if not 0 < current.st_size <= MAX_FILE_BYTES:
                raise UserError("Attachments must contain from 1 byte to 50 MiB.")
            data = source.read(MAX_FILE_BYTES + 1)
            after = os.fstat(source.fileno())
            if len(data) != current.st_size or (after.st_size, after.st_mtime_ns) != (current.st_size, current.st_mtime_ns):
                raise UserError("Attachment changed while reading. Prepare it again.")
            return data
    except (OSError, ValueError, RuntimeError):
        raise UserError("Attachment could not be opened in the configured inbox. Check its filename and local availability.") from None


class AttachmentFiles:
    def __init__(self, inbox, blobs):
        self.inbox = Path(inbox).resolve()
        self.blobs = Path(blobs).resolve()

    def list(self):
        if not self.inbox.is_dir():
            return {"files": [], "max_file_bytes": MAX_FILE_BYTES, "scan_capped": False}
        files = []
        scanned = 0
        with os.scandir(self.inbox) as entries:
            for entry in entries:
                scanned += 1
                if scanned > 200:
                    break
                try:
                    name = filename(entry.name)
                    # Windows DirEntry.stat caches incomplete link metadata (nlink=0).
                    info = Path(entry.path).lstat()
                    if _regular(info) and 0 < info.st_size <= MAX_FILE_BYTES:
                        files.append({"filename": name, "size_bytes": info.st_size,
                                      "mime_type": MIME_TYPES[Path(name).suffix.lower()]})
                except (UserError, OSError):
                    continue
        return {"files": sorted(files, key=lambda f: f["filename"].casefold()),
                "max_file_bytes": MAX_FILE_BYTES, "scan_capped": scanned > 200}

    def snapshot(self, name):
        name = filename(name)
        data = _read_file(self.inbox, name)
        digest = hashlib.sha256(data).hexdigest()
        self.blobs.mkdir(parents=True, exist_ok=True)
        target = self.blobs / (digest + ".blob")
        if target.exists():
            if hashlib.sha256(_read_file(self.blobs, target.name)).hexdigest() != digest:
                raise UserError("The saved attachment snapshot changed. No file was uploaded.")
        else:
            total = sum(p.stat().st_size for p in self.blobs.glob("*.blob"))
            if total + len(data) > MAX_STAGED_BYTES:
                raise UserError("Attachment snapshot storage is full (250 MiB). Remove unneeded snapshots locally after their drafts are finished.")
            try:
                with target.open("xb") as output:
                    output.write(data)
            except FileExistsError:
                if hashlib.sha256(_read_file(self.blobs, target.name)).hexdigest() != digest:
                    raise UserError("The saved attachment snapshot changed. No file was uploaded.") from None
        return {"filename": name, "size_bytes": len(data), "sha256": digest,
                "mime_type": MIME_TYPES[Path(name).suffix.lower()]}

    def verified_bytes(self, attachment):
        name = filename(attachment["filename"])
        digest = attachment["sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise UserError("Invalid attachment snapshot hash.")
        data = _read_file(self.blobs, digest + ".blob")
        if (len(data) != attachment["size_bytes"] or hashlib.sha256(data).hexdigest() != digest
                or attachment["mime_type"] != MIME_TYPES[Path(name).suffix.lower()]):
            raise UserError("The saved attachment snapshot changed. No file was uploaded.")
        return data
