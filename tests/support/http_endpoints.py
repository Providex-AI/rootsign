"""Real HTTP endpoints on 127.0.0.1 for tests that run RootSign in a subprocess.

`mock_ingest()` serves `tests.support.mock_ingest_store.MockIngestStore` (the
spec-checking store the cloud contract suite uses) over HTTP, so a script or
`rootsign-admin sync` in another process talks to it like a real endpoint.
`POST /_sever` makes it answer 503 until `POST /_restore`, which lets a script
cut the endpoint part-way through its own session.

`not_deployed()` answers every POST the way the default ROOTSIGN_CLOUD_URL host
does while the hosted endpoint is not live: 404 with a plain-text body.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from tests.support.mock_ingest_store import MockIngestStore


@contextmanager
def _serve(handler: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


class _Quiet(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep test output clean
        pass

    def _reply(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def mock_ingest() -> Iterator[tuple[str, MockIngestStore]]:
    """Yield (`ROOTSIGN_CLOUD_URL` value, the store behind it)."""
    store = MockIngestStore()
    state = {"down": False}

    class Handler(_Quiet):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("content-length", 0)))
            if self.path in ("/_sever", "/_restore"):
                state["down"] = self.path == "/_sever"
                self._reply(200, b"{}")
            elif state["down"]:
                self._reply(503, b"{}")
            else:
                response = store(httpx.Request("POST", f"http://mock{self.path}", content=body))
                self._reply(response.status_code, response.content)

    with _serve(Handler) as base:
        yield f"{base}/v1", store


@contextmanager
def not_deployed() -> Iterator[str]:
    """Yield a `ROOTSIGN_CLOUD_URL` value whose host answers 404, not deployed."""

    class Handler(_Quiet):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length", 0)))
            body = b"The deployment could not be found on Vercel.\n\nDEPLOYMENT_NOT_FOUND\n"
            self._reply(404, body, "text/plain; charset=utf-8")

    with _serve(Handler) as base:
        yield f"{base}/v1"
