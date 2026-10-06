"""Heuristic publication gate. Prints finding categories/paths, never matched secrets."""
import argparse
import re
import stat
import struct
import subprocess
import sys
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "private-key material": rb"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----",
    "GitHub credential": rb"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})",
    "API credential": rb"sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}",
    "Telegram bot credential": rb"\b[0-9]{7,12}:[A-Za-z0-9_-]{30,}\b",
    "serialized Telegram session": rb"[\"']1[A-Za-z0-9_=-]{300,}[\"']",
    "JWT credential": rb"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{20,}",
    "machine user path": rb"(?i)[a-z]:[\\/]{1,2}Users[\\/]{1,2}[^\s\"'<>]+",
    "local package URL": rb"file:///[A-Za-z]:/",
}


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, stderr=subprocess.DEVNULL)


def private_path(name):
    path = PurePosixPath(name)
    parts = {p.lower() for p in path.parts}
    lower = name.lower()
    return (bool(parts & {".local", ".venv", "archive", "__pycache__", "node_modules", "attachments", "attachment-blobs"})
            or (path.name.startswith(".env") and path.name != ".env.example")
            or any(marker in lower for marker in (".session", ".sqlite", ".dpapi", "credentials", "library-uploads"))
            or path.suffix.lower() in {".db", ".key", ".pem", ".pfx", ".p12", ".log", ".blob", ".epub"}
            or lower in {"remote-config.json", "evidence/test-results.txt", "evidence/verification.json"})


def inspect_blob(name, data):
    findings = []
    if private_path(name):
        findings.append("private runtime path")
    if len(data) > 2 * 1024 * 1024:
        findings.append("unexpected large file")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        position = 8
        while position + 12 <= len(data):
            size = struct.unpack(">I", data[position:position + 4])[0]
            chunk = data[position + 4:position + 8]
            if chunk in {b"tEXt", b"zTXt", b"iTXt", b"eXIf"}:
                findings.append("image metadata requires review")
            position += size + 12
        if position != len(data):
            findings.append("unexpected PNG trailing data")
        return findings
    if b"\x00" in data:
        findings.append("unexpected binary file")
    for label, pattern in PATTERNS.items():
        if re.search(pattern, data):
            findings.append(label)
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--working-tree", action="store_true",
                        help="Inspect current tracked and unignored new files instead of the Git index")
    args = parser.parse_args()
    try:
        if args.working_tree:
            names = git("ls-files", "--cached", "--others", "--exclude-standard", "-z").decode().split("\x00")
            blobs = []
            for name in sorted(set(names) - {""}):
                path = ROOT / name
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    continue  # A tracked deletion is absent from the proposed tree.
                if (not stat.S_ISREG(info.st_mode)
                        or getattr(info, "st_file_attributes", 0) & 0x400):
                    raise ValueError("Working-tree links and special files require manual review")
                blobs.append((name, path.read_bytes()))
        else:
            # Publishing still defaults to the exact Git index bytes.
            names = git("ls-files", "-z").decode().split("\x00")
            blobs = [(name, git("show", ":" + name)) for name in names if name]
        if not blobs:
            raise ValueError("No public files to inspect")
        if args.history:
            for line in git("rev-list", "--objects", "--all").decode().splitlines():
                oid, _, name = line.partition(" ")
                if name and git("cat-file", "-t", oid).strip() == b"blob":
                    blobs.append((name, git("cat-file", "blob", oid)))
    except (OSError, subprocess.CalledProcessError, ValueError):
        print("Audit could not read the selected Git files/history. Check the checkout and file types.")
        return 2
    findings = set()
    for name, data in blobs:
        findings.update((name, category) for category in inspect_blob(name, data))
    for name, category in sorted(findings):
        print(f"FAIL {category}: {name}")
    if findings:
        return 1
    selected = "working-tree" if args.working_tree else "staged"
    print(f"PASS: {len(blobs)} {selected}/history blob checks; no flagged paths, common credentials or image metadata.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
