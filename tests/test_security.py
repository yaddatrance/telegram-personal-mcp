import json
import os
import queue
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from ken_mcp.review import make_review_server
from ken_mcp.security import ProfileLock, dpapi
from ken_mcp.store import Store, UserError


class SecurityTests(unittest.TestCase):
    def test_single_instance_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            with ProfileLock(directory):
                with self.assertRaises(UserError):
                    with ProfileLock(directory):
                        pass
            with ProfileLock(directory):
                pass

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_dpapi_roundtrip_only_synthetic_data(self):
        raw = b"synthetic-test-data-not-a-real-session"
        encrypted = dpapi(raw)
        self.assertNotIn(raw, encrypted)
        self.assertEqual(dpapi(encrypted, False), raw)

    def test_no_credentials_default(self):
        from ken_mcp.security import load_credentials
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(UserError):
                load_credentials(directory)
            self.assertEqual(list(Path(directory).iterdir()), [])


class ReviewHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.events = queue.Queue()
        self.key = "synthetic-test-owner-key"
        def start():
            store = Store(Path(self.temp.name) / "outbox.sqlite3", "demo")
            store.bind_account("9000")
            item = store.prepare("request-key-00001", "9000", {"peer_id": "1001", "name": "Alex"}, "Hello", None)
            server, _ = make_review_server(store, port=0, owner_key=self.key)
            self.events.put((server, item))
            server.serve_forever(poll_interval=0.05)
            server.server_close()
            store.close()
        self.thread = threading.Thread(target=start)
        self.thread.start()
        self.server, self.item = self.events.get(timeout=5)
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(5)
        self.temp.cleanup()

    def request(self, path="/api/state", body=None, owner=True, origin=True, host=None):
        headers = {}
        if owner:
            headers["X-Owner-Key"] = self.key
        if origin:
            headers["Origin"] = self.base
        if host:
            headers["Host"] = host
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers

    def test_unauthorized_cannot_read_or_approve(self):
        self.assertEqual(self.request(owner=False)[0], 401)
        self.assertEqual(self.request("/api/approve", {}, owner=False)[0], 401)

    def test_dns_rebinding_host_rejected(self):
        self.assertEqual(self.request(host="attacker.example")[0], 403)

    def test_csrf_missing_origin_rejected(self):
        self.assertEqual(self.request("/api/approve", {}, origin=False)[0], 403)

    def test_review_approve_and_cancel(self):
        payload = {"draft_id": self.item["draft_id"], "preview_sha256": self.item["preview_sha256"], "confirmation": "APPROVE"}
        self.assertEqual(self.request("/api/approve", payload)[0], 200)
        state = json.loads(self.request()[1])
        self.assertTrue(state["requests"][0]["locally_approved"])
        self.assertEqual(self.request("/api/cancel", payload)[0], 200)
        self.assertEqual(json.loads(self.request()[1])["requests"][0]["status"], "cancelled")

    def test_wrong_hash_and_confirmation_rejected(self):
        payload = {"draft_id": self.item["draft_id"], "preview_sha256": "wrong", "confirmation": "APPROVE"}
        self.assertEqual(self.request("/api/approve", payload)[0], 400)
        payload["preview_sha256"] = self.item["preview_sha256"]
        payload["confirmation"] = "no"
        self.assertEqual(self.request("/api/approve", payload)[0], 400)

    def test_static_headers_and_no_arbitrary_file_reads(self):
        status, _, headers = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.request("/../credentials.dpapi")[0], 404)


if __name__ == "__main__":
    unittest.main()
