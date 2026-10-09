"""A tiny read-only status page for the agent, so the dashboard can show the device side."""

import json
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class SyncStatus:
    """What the sync thread is up to. Written by that thread, read by the status page."""

    def __init__(self):
        self.online: bool | None = None  # None until the first upload attempt
        self.last_error = ""
        self.last_synced_at: str | None = None
        self._retry_at = 0.0

    def delivered(self, when: str) -> None:
        self.online, self.last_error, self.last_synced_at = True, "", when
        self._retry_at = 0.0

    def failed(self, detail: str, retry_in: float) -> None:
        self.online, self.last_error = False, detail
        self._retry_at = time.monotonic() + retry_in

    def retry_in(self) -> float:
        return round(max(0.0, self._retry_at - time.monotonic()), 1)


def serve_status(port: int, payload: Callable[[], dict]) -> ThreadingHTTPServer:
    """Serve `payload()` as JSON at /status on localhost, from a background thread."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] != "/status":
                self.send_error(404)
                return
            body = json.dumps(payload()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            # The dashboard is served from the server's port, so this is a cross-origin read.
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, name="status", daemon=True).start()
    return server
