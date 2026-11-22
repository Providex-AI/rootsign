"""An agent on the cloud transport keeps its records when the URL answers 404.

End to end against a real local HTTP server that answers every POST the way
the default ROOTSIGN_CLOUD_URL host does while the endpoint is not deployed:
404 with a plain-text body. On main the agent ran, nothing was spooled and
nothing was logged at the default level: every record was lost.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class _NotDeployed(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the test output clean
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        body = b"The deployment could not be found on Vercel.\n\nDEPLOYMENT_NOT_FOUND\n"
        self.send_response(404)
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def not_deployed():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NotDeployed)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()


AGENT = textwrap.dedent(
    """
    import asyncio
    from uuid import UUID

    import rootsign

    rootsign.init(agent="invoice-agent")


    @rootsign.trace()
    async def send_invoice(customer_id: str, amount: float) -> str:
        return "sent"


    async def main() -> None:
        async with rootsign.session(agent_id=UUID("6f1c2d4e-8a9b-4c3d-9e2f-1a2b3c4d5e6f")) as ctx:
            await send_invoice("acme-corp", 1500.0)
        print(ctx.session_id)


    asyncio.run(main())
    """
)


def test_records_reach_the_spool(tmp_path, not_deployed):
    (tmp_path / "agent.py").write_text(AGENT)
    env = {
        **os.environ,
        "ROOTSIGN_BACKEND": "cloud",
        "ROOTSIGN_API_KEY": "test-key",
        "ROOTSIGN_CLOUD_URL": not_deployed,
        "ROOTSIGN_HTTP_MAX_RETRIES": "1",
        "ROOTSIGN_DATA_DIR": str(tmp_path / "data"),
    }
    result = subprocess.run(
        [sys.executable, "agent.py"], cwd=tmp_path, env=env, capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr
    session_id = result.stdout.strip()
    spooled = tmp_path / "data" / "spool" / "sessions" / f"{session_id}.jsonl"
    assert spooled.exists()
    lines = spooled.read_text().splitlines()
    assert len(lines) == 3  # open, the tool call, close
    assert "no ingest endpoint at this URL (HTTP 404)" in result.stderr
