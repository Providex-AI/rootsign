"""S5-TASK 10 — MCP proxy integration (real DB, real hash chain).

Proves that MCP `tools/call` interception produces a valid, ordered Action
hash chain end-to-end: SESSION_OPEN → 3 intercepted tool calls → SESSION_CLOSE,
then `verify_chain` returns VALID with dense sequence numbers.

Why this drives `MCPProxyTracer.intercept_tools_call` directly instead of
through FastAPI's `TestClient`: `TestClient` runs the ASGI app in a separate
anyio portal thread with its own event loop, while `LocalIngestClient` here is
bound to the test loop's `clean_db` AsyncSession. Awaiting that asyncpg-backed
session from the portal loop is a cross-loop violation. The HTTP routing layer
(tools/call vs passthrough) is already covered by the contract tests with a
mock client; this test's job is the real DB → valid chain, so it calls the
interceptor on the test's own loop. Only the upstream `httpx` call is mocked.

`seeded_agent` (committed) per Flag 3 — the session/actions are queried back
after commit.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

pytest.importorskip("fastapi", reason="rootsign[mcp] not installed")
pytest.importorskip("httpx", reason="httpx not installed")

from rootsign.crud import action as action_crud  # noqa: E402
from rootsign.mcp.proxy import MCPProxyTracer  # noqa: E402
from rootsign.sdk.client import LocalIngestClient  # noqa: E402
from rootsign.sdk.context import SessionContext  # noqa: E402
from tests.conftest import make_envelope  # noqa: E402

MOCK_RESPONSE = {
    "jsonrpc": "2.0",
    "result": {"content": [{"type": "text", "text": "done"}], "isError": False},
    "id": 1,
}


@contextmanager
def mock_upstream(json_body):
    resp = MagicMock(json=lambda: json_body, raise_for_status=lambda: None, status_code=200)
    cm = AsyncMock()
    cm.__aenter__.return_value.post = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    with patch("httpx.AsyncClient", return_value=cm):
        yield


class TestMCPProxyIntegration:
    async def test_three_mcp_tool_calls_produce_valid_chain(self, clean_db, seeded_agent):
        client = LocalIngestClient(db=clean_db)
        session_id = uuid4()
        ctx = SessionContext(agent_id=seeded_agent.agent_id, session_id=session_id)

        await client.handle(
            make_envelope(
                "SESSION_OPEN",
                seeded_agent.agent_id,
                session_id,
                {"objective": "MCP proxy integration test"},
            )
        )

        with mock_upstream(MOCK_RESPONSE):
            for i, tool_name in enumerate(["send_email", "query_db", "write_file"]):
                await MCPProxyTracer.intercept_tools_call(
                    request_body={
                        "jsonrpc": "2.0",
                        "method": "tools/call",
                        "params": {"name": tool_name, "arguments": {"x": i}},
                        "id": i + 1,
                    },
                    upstream_url="http://mock/mcp",
                    ctx=ctx,
                    client=client,
                )

        await client.handle(
            make_envelope(
                "SESSION_CLOSE",
                seeded_agent.agent_id,
                session_id,
                {"status": "completed", "metadata": {"total_actions": 3}},
            )
        )
        await clean_db.commit()

        chain = await action_crud.get_session_chain(clean_db, session_id=session_id)
        assert len(chain) == 3
        assert chain[0].tool_name == "send_email"
        assert [a.sequence_number for a in chain] == [1, 2, 3]

        result = await action_crud.verify_chain(clean_db, session_id=session_id)
        assert result["valid"] is True
        assert result["record_count"] == 3
