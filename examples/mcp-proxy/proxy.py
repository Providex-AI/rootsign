"""Run RootSign's MCP proxy in front of an existing MCP server.

    pip install 'rootsign[mcp]'
    MCP_UPSTREAM_URL=http://127.0.0.1:8001/mcp python proxy.py

Then point your agent's MCP client at http://127.0.0.1:8000/mcp instead of
the server. Every `tools/call` is recorded on one session's hash chain, from
start-up until you stop the proxy with Ctrl+C, and the session ID is printed
when it stops. `demo.py` next to this file runs the same proxy with a server
and a client in one process.
"""

from __future__ import annotations

import asyncio
import os

import uvicorn

import rootsign
from rootsign.mcp.proxy import create_proxy_app

UPSTREAM_URL = os.environ.get("MCP_UPSTREAM_URL", "http://127.0.0.1:8001/mcp")

rootsign.init(agent="mcp-proxied-agent", risk_tier="high")


async def main() -> None:
    async with rootsign.session(objective="proxy MCP tool calls") as ctx:
        app = create_proxy_app(upstream_url=UPSTREAM_URL)
        config = uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="warning")
        await uvicorn.Server(config).serve()
    print(f"session: {ctx.session_id} ({ctx.current_sequence} actions)")


if __name__ == "__main__":
    asyncio.run(main())
