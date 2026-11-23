"""The MCP proxy against a real MCP server and the official MCP client.

The other proxy tests mock the upstream, which is how a proxy that dropped the
client's headers and assumed JSON-only replies passed CI while every real
streamable-HTTP server rejected it with `406 Not Acceptable`. Here nothing on
the wire is mocked:

    MCP ClientSession ──HTTP──▶ create_proxy_app ──HTTP──▶ FastMCP server

all served by uvicorn on loopback ports, with records going to a real
`JsonlIngestClient`. Both FastMCP transport modes run:

* ``stateful-sse``: the default. Replies are SSE streams and the server
  issues an `Mcp-Session-Id` the client must send back.
* ``stateless-json``: `stateless_http=True, json_response=True`. Plain JSON
  replies, no session.

Each run initializes, lists tools, calls one tool through the proxy, and
checks the tool result, the recorded ACTION_RECORD, and a VALID chain.

Each mode also runs with the upstream URL ending in `/mcp/`, which the server
answers with a 307 to `/mcp`. The proxy must follow that itself: relaying it
would hand the client the upstream's real address and let it bypass the
proxy, so its calls would never be recorded.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from uuid import uuid4

import httpx
import pytest

pytest.importorskip("mcp", reason="rootsign[mcp] not installed")
pytest.importorskip("uvicorn", reason="rootsign[mcp] not installed")

import uvicorn  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

import rootsign  # noqa: E402
from rootsign.mcp.proxy import create_proxy_app  # noqa: E402
from rootsign.sdk.chain import verify_session_local  # noqa: E402
from rootsign.sdk.jsonl_client import JsonlIngestClient  # noqa: E402
from rootsign.verdict import Verdict  # noqa: E402

MODES = {
    "stateful-sse": {},
    "stateless-json": {"stateless_http": True, "json_response": True},
}


_ACCEPT = {"Accept": "application/json, text/event-stream"}
_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "0"},
    },
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _billing_server(**settings) -> FastMCP:
    server = FastMCP("billing", **settings)

    @server.tool()
    def send_invoice(customer_id: str, amount: float) -> str:
        """Send an invoice to a customer."""
        return f"invoice sent to {customer_id} for ${amount:,.2f}"

    return server


class _TestServer(uvicorn.Server):
    """A uvicorn server that leaves the process's signal handlers alone.

    sse-starlette ends every SSE stream in the process once it sees "the"
    uvicorn server exiting, and it finds that server through the SIGTERM
    handler `serve()` installs. Stopping one test's server then cut short the
    SSE replies of every server started after it, in later tests too. A test
    server is never stopped by a signal, so it has no reason to install one.
    """

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


@asynccontextmanager
async def _serving(app):
    port = _free_port()
    server = _TestServer(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


@pytest.mark.parametrize("upstream_path", ["/mcp", "/mcp/"], ids=["direct", "redirected"])
@pytest.mark.parametrize("mode", sorted(MODES))
async def test_a_real_client_calls_a_real_server_through_the_proxy(mode, upstream_path, tmp_path):
    upstream_app = _billing_server(**MODES[mode]).streamable_http_app()
    client = JsonlIngestClient(data_dir=tmp_path)

    async with _serving(upstream_app) as upstream_base:
        async with rootsign.session(agent_id=uuid4(), client=client) as ctx:
            proxy = create_proxy_app(
                upstream_url=upstream_base + upstream_path, ctx=ctx, client=client
            )
            async with _serving(proxy) as proxy_base:
                proxy_url = proxy_base + "/mcp"
                async with httpx.AsyncClient() as raw:
                    probe = await raw.post(proxy_url, json=_INITIALIZE, headers=_ACCEPT)
                assert probe.status_code == 200, "a redirect reached the client"
                async with streamablehttp_client(proxy_url) as (read, write, _):
                    async with ClientSession(read, write) as mcp:
                        await mcp.initialize()
                        tools = await mcp.list_tools()
                        result = await mcp.call_tool(
                            "send_invoice", {"customer_id": "acme-corp", "amount": 1500.0}
                        )

    assert [t.name for t in tools.tools] == ["send_invoice"]
    assert not result.isError
    assert result.content[0].text == "invoice sent to acme-corp for $1,500.00"

    session_file = tmp_path / "sessions" / f"{ctx.session_id}.jsonl"
    records = [json.loads(line) for line in session_file.read_text().splitlines()]
    actions = [r for r in records if r["event_type"] == "ACTION_RECORD"]
    assert len(actions) == 1, "exactly one tools/call crossed the proxy"
    [action] = actions
    assert action["tool_name"] == "send_invoice"
    assert action["input_redacted"] == {"customer_id": "acme-corp", "amount": 1500.0}
    reply = action["output_redacted"]["result"]
    assert reply["result"]["content"][0]["text"] == "invoice sent to acme-corp for $1,500.00"

    assert verify_session_local(str(session_file)).verdict is Verdict.VALID


async def test_a_test_servers_shutdown_does_not_end_every_later_stream():
    """sse-starlette keeps one process-wide shutdown flag, `AppStatus.should_exit`.
    A watcher finds "the" uvicorn server through the process's SIGTERM handler
    and, once that server is told to exit, sets the flag, after which every SSE
    stream in the process ends before its first event. A test server that
    installed signal handlers therefore broke the next test's SSE replies:
    `stateful-sse-redirected` runs right after `stateful-sse-direct`, and failed
    in CI whenever the watcher polled before the first test's loop closed."""
    from sse_starlette.sse import AppStatus

    try:
        async with _serving(_billing_server().streamable_http_app()) as base:
            async with httpx.AsyncClient() as raw:
                reply = await raw.post(base + "/mcp", json=_INITIALIZE, headers=_ACCEPT)
            assert reply.status_code == 200  # an SSE reply: the watcher is now running
        await asyncio.sleep(1.2)  # longer than the watcher's 0.5 s poll

        assert AppStatus.should_exit is False
    finally:
        AppStatus.should_exit = False  # never let a regression break later tests
