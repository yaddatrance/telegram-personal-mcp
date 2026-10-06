import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import audit_public_tree as audit


class PublicationAuditTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.git("init", "--quiet")
        (self.root / "README.md").write_text("Synthetic project\n", encoding="utf-8")
        self.git("add", "README.md")

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, stderr=subprocess.DEVNULL)

    def run_audit(self, *args):
        output = io.StringIO()
        with patch.object(audit, "ROOT", self.root), patch("sys.argv", ["audit", *args]), contextlib.redirect_stdout(output):
            result = audit.main()
        return result, output.getvalue()

    def test_working_copy_checks_modified_and_new_files_without_echoing_content(self):
        secret = "ghp_" + "A" * 36  # Synthetic credential-shaped fixture.
        (self.root / "README.md").write_text(secret, encoding="utf-8")
        (self.root / "private.session").write_text("synthetic", encoding="utf-8")
        result, output = self.run_audit("--working-tree")
        self.assertEqual(result, 1)
        self.assertIn("GitHub credential: README.md", output)
        self.assertIn("private runtime path: private.session", output)
        self.assertNotIn(secret, output)
        self.assertEqual(self.run_audit()[0], 0)  # Index bytes are still clean.

    def test_ignored_private_files_are_excluded_but_tracked_private_files_are_rejected(self):
        (self.root / ".gitignore").write_text(".local/\n*.epub\n", encoding="utf-8")
        (self.root / ".local").mkdir()
        (self.root / ".local" / "credentials.dpapi").write_bytes(b"synthetic")
        (self.root / "sample.epub").write_bytes(b"synthetic")
        self.assertEqual(self.run_audit("--working-tree")[0], 0)
        self.git("add", "--force", "sample.epub")
        for args in ((), ("--working-tree",)):
            result, output = self.run_audit(*args)
            self.assertEqual(result, 1)
            self.assertIn("private runtime path: sample.epub", output)

    def test_working_copy_handles_deleted_tracked_files(self):
        (self.root / "README.md").unlink()
        (self.root / "replacement.txt").write_text("Synthetic replacement", encoding="utf-8")
        result, output = self.run_audit("--working-tree")
        self.assertEqual(result, 0)
        self.assertIn("1 working-tree/history blob checks", output)

    def test_history_rejects_private_files_deleted_from_current_tree(self):
        (self.root / "sample.epub").write_bytes(b"synthetic")
        self.git("add", "sample.epub")
        self.git("-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid",
                 "commit", "--quiet", "-m", "Synthetic history fixture")
        self.git("rm", "--quiet", "sample.epub")
        self.assertEqual(self.run_audit("--working-tree")[0], 0)
        result, output = self.run_audit("--working-tree", "--history")
        self.assertEqual(result, 1)
        self.assertIn("private runtime path: sample.epub", output)

    def test_attachment_snapshot_paths_are_rejected(self):
        for name in ("attachments/sample.txt", "attachment-blobs/example.blob", "sample.epub"):
            with self.subTest(name=name):
                self.assertIn("private runtime path", audit.inspect_blob(name, b"synthetic"))


if __name__ == "__main__":
    unittest.main()
