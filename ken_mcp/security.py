"""Owner-only Windows storage. Secrets never enter MCP inputs or outputs."""
import ctypes
import json
import os
import re
import subprocess
from ctypes import wintypes
from pathlib import Path

from .store import UserError


def default_data_dir(live=False):
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
    return base / "KenTelegramMCP" / ("live" if live else "demo")


def secure_directory(path):
    if os.name != "nt":
        raise UserError("Live credentials currently require Windows DPAPI. Demo works on other platforms.")
    path = Path(path).resolve()
    path.mkdir(parents=True, exist_ok=True)
    # SID-based ACLs work on non-English Windows installations as well.
    creationflags = subprocess.CREATE_NO_WINDOW
    owner = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True,
                           check=True, creationflags=creationflags)
    match = re.search(r"S-1-5-\d+(?:-\d+)+", owner.stdout)
    if not match:
        raise UserError("Could not identify the Windows account for private storage.")
    result = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r",
                             "*{}:(OI)(CI)F".format(match.group()), "*S-1-5-18:(OI)(CI)F"],
                            capture_output=True, creationflags=creationflags)
    if result.returncode:
        raise UserError("Could not restrict the profile directory to this Windows user.")


class Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def dpapi(data, protect=True):
    if os.name != "nt":
        raise UserError("Live credentials require Windows DPAPI.")
    buffer = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    # Flags=1: forbid any system UI prompts; encryption is bound to the current user.
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing))
    if not ok:
        raise UserError("Windows could not protect or unlock the local credentials (Win32 error {}).".format(ctypes.get_last_error()))
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        kernel32.LocalFree(outgoing.pbData)


def save_credentials(directory, values):
    secure_directory(directory)
    path = Path(directory) / "credentials.dpapi"
    temporary = path.with_suffix(".dpapi.tmp")
    temporary.write_bytes(dpapi(json.dumps(values).encode("utf-8")))
    os.replace(temporary, path)


def load_credentials(directory):
    path = Path(directory) / "credentials.dpapi"
    if not path.is_file():
        raise UserError("No local account authorization. The owner must run the login wizard first.")
    try:
        return json.loads(dpapi(path.read_bytes(), protect=False))
    except (ValueError, KeyError):
        raise UserError("Local credentials are invalid. Use the local login wizard after approval.") from None


class ProfileLock:
    def __init__(self, directory):
        self.path = Path(directory) / "server.lock"
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise UserError("This profile is already running. Close its MCP server before starting another.") from None
        return self

    def __exit__(self, *_):
        if self.file:
            self.file.close()
