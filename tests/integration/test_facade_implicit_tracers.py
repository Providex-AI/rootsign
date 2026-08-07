"""All three tracers work with implicit context (ADR-012 exit criterion 6).

LangGraph, CrewAI, and the MCP proxy get their `(ctx, client)` from the ambient
`rootsign.session()` — no `ctx=`/`client=` kwargs anywhere. Runs on the JSONL
backend against `tmp_path`, so the whole file is DB-free and each case ends in a
real `verify --local` verdict.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import rootsign
from rootsign.sdk import facade

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _jsonl_facade_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ROOTSIGN_BACKEND", "jsonl")
    monkeypatch.setenv("ROOTSIGN_DATA_DIR", str(tmp_path))
    facade._reset_init_config()
    yield
    facade._reset_init_config()


def _verify(tmp_path, session_id):
    return rootsign.verify_session_local(
        str(tmp_path / "sessions" / f"{session_id}.jsonl")
    )


@pytest.mark.asyncio
async def test_langgraph_wrap_tools_without_ctx_or_client(tmp_path):
    pytest.importorskip("langchain_core", reason="langchain-core not installed")
    from langchain_core.tools import tool

    @tool
    def multiply(a: int, b: int) -> int:
        """Multiply two integers."""
        return a * b

    rootsign.init(agent="implicit-langgraph")

    async with rootsign.session(objective="multiply things") as ctx:
        (wrapped,) = rootsign.wrap_tools([multiply])
        assert await wrapped.ainvoke({"a": 3, "b": 4}) == 12

    result = _verify(tmp_path, ctx.session_id)
    assert result.valid is True
    assert result.record_count == 1


@pytest.mark.asyncio
async def test_crewai_wrap_tools_without_ctx_or_client(tmp_path):
    pytest.importorskip("crewai", reason="crewai not installed")
    from crewai.tools import BaseTool

    class Double(BaseTool):
        name: str = "double"
        description: str = "Double an integer."

        def _run(self, value: int) -> int:
            return value * 2

    rootsign.init(agent="implicit-crewai")

    async with rootsign.session(objective="double things") as ctx:
        (wrapped,) = rootsign.wrap_crewai_tools([Double()])
        # `_rootsign_arun` is the awaitable shadow of `_run` (Sprint 3) — the
        # async-test entry point that doesn't cross an event-loop boundary.
        assert await wrapped._rootsign_arun(value=21) == 42

    result = _verify(tmp_path, ctx.session_id)
    assert result.valid is True
    assert result.record_count == 1


@contextmanager
def _mock_upstream(json_body):
    resp = MagicMock(json=lambda: json_body, raise_for_status=lambda: None, status_code=200)
    cm = AsyncMock()
    cm.__aenter__.return_value.post = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    with patch("httpx.AsyncClient", return_value=cm):
        yield


@pytest.mark.asyncio
async def test_mcp_proxy_intercept_without_ctx_or_client(tmp_path):
    pytest.importorskip("httpx", reason="httpx not installed")
    from rootsign.mcp.proxy import MCPProxyTracer

    upstream = {
        "jsonrpc": "2.0",
        "result": {"content": [{"type": "text", "text": "done"}], "isError": False},
        "id": 1,
    }

    rootsign.init(agent="implicit-mcp")

    async with rootsign.session(objective="proxy some calls") as ctx:
        with _mock_upstream(upstream):
            for i, tool_name in enumerate(["send_email", "query_db"]):
                response = await MCPProxyTracer.intercept_tools_call(
                    request_body={
                        "jsonrpc": "2.0",
                        "method": "tools/call",
                        "params": {"name": tool_name, "arguments": {"x": i}},
                        "id": i + 1,
                    },
                    upstream_url="http://mock/mcp",
                )
                assert response == upstream

    result = _verify(tmp_path, ctx.session_id)
    assert result.valid is True
    assert result.record_count == 2
