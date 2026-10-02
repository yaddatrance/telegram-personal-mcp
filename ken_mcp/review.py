"""Independent local owner approval, never a Telegram transport or MCP endpoint."""
import json
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .store import Store, UserError


def make_review_server(store, port=8765, owner_key=None):
    key = owner_key or secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, status, body, content_type="application/json"):
            if content_type == "application/json":
                body = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def allowed(self, mutation=False):
            host = "127.0.0.1:{}".format(self.server.server_port)
            if self.headers.get("Host") != host:
                self.respond(403, {"error": "Invalid local host."})
                return False
            if self.path.startswith("/api/"):
                supplied = self.headers.get("X-Owner-Key", "")
                if not secrets.compare_digest(supplied, key):
                    self.respond(401, {"error": "Open the private owner link printed in your local terminal."})
                    return False
            if mutation and (self.headers.get("Origin") != "http://" + host
                             or self.headers.get("Content-Type") != "application/json"):
                self.respond(403, {"error": "Same-origin JSON requests required."})
                return False
            return True

        def do_GET(self):
            if not self.allowed():
                return
            assets = {"/": ("review.html", "text/html; charset=utf-8"),
                      "/review.js": ("review.js", "application/javascript; charset=utf-8"),
                      "/review.css": ("review.css", "text/css; charset=utf-8")}
            if self.path in assets:
                name, content_type = assets[self.path]
                self.respond(200, Path(__file__).with_name(name).read_bytes(), content_type)
            elif self.path == "/api/state":
                self.respond(200, {"mode": store.meta("mode"), "account_id": store.meta("account_id"),
                                  "require_local_approval": store.meta("require_local_approval", False), "requests": store.recent(50)})
            else:
                self.respond(404, {"error": "Not found."})

        def do_POST(self):
            if not self.allowed(mutation=True):
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise UserError("Invalid request size.")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise UserError("Invalid request body.")
                if self.path == "/api/approve":
                    if data.get("confirmation") != "APPROVE":
                        raise UserError("Explicit approval is required.")
                    store.approve_local(data.get("draft_id"), data.get("preview_sha256", ""))
                    self.respond(200, {"approved": True})
                elif self.path == "/api/cancel":
                    self.respond(200, store.cancel(data.get("draft_id")))
                else:
                    self.respond(404, {"error": "Not found."})
            except (UserError, ValueError, TypeError):
                self.respond(400, {"error": "Request rejected. Refresh and review the exact draft again."})

    server = HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 1
    return server, key


def run_review(directory, live, port, open_browser=False):
    store = Store(directory / "outbox.sqlite3", "live" if live else "demo")
    server, key = make_review_server(store, port)
    url = "http://127.0.0.1:{}/#{}".format(server.server_port, key)
    print("Private owner review link (do not share):\n" + url)
    print("Approvals expire after five minutes. Ctrl+C closes the review window server.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()
