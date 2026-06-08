"""Unit tests for the LangGraph tracer — see ADR-004.

These are framework-version-agnostic. The CI matrix runs the version-specific
contract suite in `tests/contract/langgraph/` separately.

Each test asks for a fresh `sample_tool` via the fixture because `wrap_tool`
mutates the tool in place — re-using one across tests would compound wraps.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

pytest.importorskip("langgraph", reason="LangGraph not installed")
pytest.importorskip("langchain_core", reason="langchain-core not installed")

from langchain_core.tools import tool  # noqa: E402

from rootsign.sdk.context import SessionContext  # noqa: E402
from rootsign.sdk.frameworks.langgraph import LangGraphTracer  # noqa: E402


def _make_sample_tool():
    @tool
    def sample_tool(x: int) -> int:
        """A sample tool that doubles its input."""
        return x * 2

    return sample_tool


@pytest.fixture
def sample_tool():
    return _make_sample_tool()


@pytest.fixture
def mock_client():
    c = AsyncMock()
    c.handle.return_value = MagicMock(
        status="accepted",
        entity_id=uuid4(),
        sequence_number=1,
        self_hash="a" * 64,
    )
    return c


@pytest.fixture
def ctx():
    return SessionContext(agent_id=uuid4())


class TestIsLangchainTool:
    def test_true_for_tool_decorated_function(self, sample_tool):
        from rootsign.sdk.decorator import _is_langchain_tool

        assert _is_langchain_tool(sample_tool) is True

    def test_false_for_plain_function(self):
        from rootsign.sdk.decorator import _is_langchain_tool

        def plain():
            pass

        assert _is_langchain_tool(plain) is False


class TestWrapTool:
    def test_wrap_tool_returns_basetool(self, sample_tool, ctx, mock_client):
        from langchain_core.tools import BaseTool

        wrapped = LangGraphTracer.wrap_tool(sample_tool, ctx=ctx, client=mock_client)
        assert isinstance(wrapped, BaseTool)

    def test_wrap_tool_sets_instrumented_flag(self, sample_tool, ctx, mock_client):
        wrapped = LangGraphTracer.wrap_tool(sample_tool, ctx=ctx, client=mock_client)
        assert wrapped._rootsign_instrumented is True

    def test_wrap_tool_attaches_context(self, sample_tool, ctx, mock_client):
        wrapped = LangGraphTracer.wrap_tool(sample_tool, ctx=ctx, client=mock_client)
        assert wrapped._rootsign_context is ctx

    def test_wrap_tool_preserves_name(self, sample_tool, ctx, mock_client):
        wrapped = LangGraphTracer.wrap_tool(sample_tool, ctx=ctx, client=mock_client)
        assert wrapped.name == "sample_tool"

    def test_wrap_tool_preserves_args_schema(self, sample_tool, ctx, mock_client):
        wrapped = LangGraphTracer.wrap_tool(sample_tool, ctx=ctx, client=mock_client)
        assert wrapped.args_schema is not None
        assert "x" in wrapped.args_schema.model_fields


class TestWrapToolsDoubleWrapGuard:
    def test_double_wrap_skipped(self, sample_tool, ctx, mock_client):
        wrapped_once = LangGraphTracer.wrap_tool(sample_tool, ctx=ctx, client=mock_client)
        result = LangGraphTracer.wrap_tools(
            [wrapped_once], ctx=ctx, client=mock_client
        )
        # Same object, returned unchanged — not re-wrapped.
        assert result[0] is wrapped_once


class TestRunSync:
    """`_run_sync` is the sync-path bridge — see ADR-004."""

    def test_no_running_loop_uses_asyncio_run(self):
        from rootsign.sdk.frameworks.langgraph import _run_sync

        async def coro():
            return "ok-direct"

        # No event loop in this thread → falls through to asyncio.run.
        assert _run_sync(coro()) == "ok-direct"

    async def test_running_loop_dispatches_to_worker_thread(self):
        """Inside an already-running loop, _run_sync must spawn a worker
        thread to avoid the `loop is already running` error."""
        import threading

        from rootsign.sdk.frameworks.langgraph import _run_sync

        caller_thread = threading.get_ident()
        worker_thread_box: dict[str, int] = {}

        async def coro():
            worker_thread_box["tid"] = threading.get_ident()
            return "ok-worker"

        assert _run_sync(coro()) == "ok-worker"
        # Confirm we genuinely went through a separate thread.
        assert worker_thread_box["tid"] != caller_thread

    async def test_worker_thread_propagates_exception(self):
        from rootsign.sdk.frameworks.langgraph import _run_sync

        async def boom():
            raise ValueError("from worker")

        with pytest.raises(ValueError, match="from worker"):
            _run_sync(boom())
