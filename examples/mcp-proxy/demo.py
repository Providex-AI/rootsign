"""RootSign's MCP proxy between an MCP client and an MCP server, in one script.

    pip install 'rootsign[mcp]'
    python demo.py

Three parts, all real and all on your machine:

1. An ordinary MCP server, built with FastMCP from the official MCP Python SDK.
   It knows nothing about RootSign.
2. RootSign's proxy in front of it. Every `tools/call` that passes through is
   recorded on the hash chain, then forwarded unchanged.
3. An ordinary MCP client, the "agent", pointed at the proxy instead of the
   server. That URL is the only thing an agent changes.

They run in one process so the demo needs no setup; in production each is its
own service. No database, no API key, no network beyond 127.0.0.1. Records
land in `~/.rootsign/sessions/<session_id>.jsonl` (ADR-011).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import uvicorn
from mcp import ClientSession
from mcp.server.fastmcp import FastMCP

import rootsign
from rootsign.mcp.proxy import create_proxy_app

try:  # mcp >= 1.24
    from mcp.client.streamable_http import streamable_http_client as connect
except ImportError:  # older mcp
    from mcp.client.streamable_http import streamablehttp_client as connect

SERVER_PORT = 8001
PROXY_PORT = 8000

# 1. An ordinary MCP server.
billing = FastMCP("billing", log_level="WARNING")


@billing.tool()
def send_invoice(customer_id: str, amount: float) -> str:
    """Send an invoice to a customer."""
    return f"invoice sent to {customer_id} for ${amount:,.2f}"


@billing.tool()
def log_payment(customer_id: str, amount: float) -> str:
    """Record a payment in the ledger."""
    return f"payment of ${amount:,.2f} recorded for {customer_id}"


rootsign.init(agent="mcp-billing-agent", risk_tier="high")


async def serve(app, port: int):
    """Start an ASGI app with uvicorn on 127.0.0.1 and wait until it is up."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    return server, task


async def stop(server, task) -> None:
    server.should_exit = True
    await task


async def run_agent(url: str) -> None:
    """3. An ordinary MCP client, pointed at the proxy."""
    async with connect(url) as (read, write, _):
        async with ClientSession(read, write) as mcp:
            await mcp.initialize()
            for name in ("send_invoice", "log_payment"):
                result = await mcp.call_tool(name, {"customer_id": "acme-corp", "amount": 1500.0})
                print(f"tool:  {result.content[0].text}")


async def main() -> None:
    server = await serve(billing.streamable_http_app(), SERVER_PORT)

    # 2. The proxy runs inside a session: every tools/call through it lands on
    # this session's chain until the proxy stops.
    async with rootsign.session(objective="invoice acme-corp over MCP") as ctx:
        proxy = await serve(
            create_proxy_app(upstream_url=f"http://127.0.0.1:{SERVER_PORT}/mcp"), PROXY_PORT
        )
        await run_agent(f"http://127.0.0.1:{PROXY_PORT}/mcp")
        await stop(*proxy)

    await stop(*server)

    data_dir = Path(os.environ.get("ROOTSIGN_DATA_DIR", "~/.rootsign")).expanduser()
    target = data_dir / "sessions" / f"{ctx.session_id}.jsonl"
    print(f"\n{ctx.current_sequence} actions recorded → {target}")
    print("\nVerify the chain:")
    print(f"    rootsign verify --local {target}")


if __name__ == "__main__":
    asyncio.run(main())
