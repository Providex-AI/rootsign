"""LangGraph contract tests — verify the interception path against the real
LangGraph + langchain_core surface. Mock IngestClient (no DB needed).

The CI matrix runs this file against LangGraph 0.1.x and 0.2.x. The same
tests are exercised locally against whatever version is installed.

Each test pulls a fresh tool from the factory because `wrap_tool` mutates in
place — see ADR-004.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

pytest.importorskip("langgraph", reason="LangGraph not installed")
pytest.importorskip("langchain_core", reason="langchain-core not installed")

from langchain_core.tools import tool  # noqa: E402

from rootsign.ingest.schemas import EventType  # noqa: E402
from rootsign.sdk.context import SessionContext  # noqa: E402
from rootsign.sdk.frameworks.langgraph import LangGraphTracer  # noqa: E402


def _make_multiply():
    @tool
    def multiply(a: int, b: int) -> int:
        """Multiply two integers."""
        return a * b

    return multiply


def _make_always_fails():
    @tool
    def always_fails(x: str) -> str:
        """A tool that always raises."""
        raise ValueError("intentional failure")

    return always_fails


@pytest.fixture
def multiply():
    return _make_multiply()


@pytest.fixture
def always_fails():
    return _make_always_fails()


@pytest.fixture
def mock_client():
    client = AsyncMock()
    client.handle.return_value = MagicMock(
        status="accepted",
        entity_id=uuid4(),
        sequence_number=1,
        self_hash="a" * 64,
    )
    return client


@pytest.fixture
def ctx():
    return SessionContext(agent_id=uuid4())


class TestToolMetadataPreservation:
    def test_tool_name_preserved(self, multiply, ctx, mock_client):
        wrapped = LangGraphTracer.wrap_tool(multiply, ctx=ctx, client=mock_client)
        assert wrapped.name == "multiply"

    def test_tool_description_preserved(self, multiply, ctx, mock_client):
        wrapped = LangGraphTracer.wrap_tool(multiply, ctx=ctx, client=mock_client)
        assert "Multiply" in wrapped.description

    def test_args_schema_preserved(self, multiply, ctx, mock_client):
        wrapped = LangGraphTracer.wrap_tool(multiply, ctx=ctx, client=mock_client)
        assert wrapped.args_schema is not None
        fields = wrapped.args_schema.model_fields
        assert "a" in fields and "b" in fields

    def test_rootsign_instrumented_flag_set(self, multiply, ctx, mock_client):
        wrapped = LangGraphTracer.wrap_tool(multiply, ctx=ctx, client=mock_client)
        assert wrapped._rootsign_instrumented is True


class TestToolCallInterception:
    def test_action_record_emitted_on_invoke(self, multiply, ctx, mock_client):
        wrapped = LangGraphTracer.wrap_tool(multiply, ctx=ctx, client=mock_client)
        result = wrapped.invoke({"a": 3, "b": 4})
        assert result == 12
        assert mock_client.handle.called
        envelope = mock_client.handle.call_args[0][0]
        assert envelope["event_type"] == EventType.ACTION_RECORD.value
        assert envelope["payload"]["tool_name"] == "multiply"

    def test_action_record_has_valid_input_hash(self, multiply, ctx, mock_client):
        wrapped = LangGraphTracer.wrap_tool(multiply, ctx=ctx, client=mock_client)
        wrapped.invoke({"a": 5, "b": 6})
        payload = mock_client.handle.call_args[0][0]["payload"]
        assert len(payload["input_hash"]) == 64
        assert all(c in "0123456789abcdef" for c in payload["input_hash"])

    def test_action_record_has_valid_output_hash(self, multiply, ctx, mock_client):
        wrapped = LangGraphTracer.wrap_tool(multiply, ctx=ctx, client=mock_client)
        wrapped.invoke({"a": 2, "b": 3})
        payload = mock_client.handle.call_args[0][0]["payload"]
        assert len(payload["output_hash"]) == 64

    def test_exception_still_emits_action_record(
        self, always_fails, ctx, mock_client
    ):
        wrapped = LangGraphTracer.wrap_tool(always_fails, ctx=ctx, client=mock_client)
        with pytest.raises(ValueError, match="intentional failure"):
            wrapped.invoke({"x": "test"})
        assert mock_client.handle.called
        payload = mock_client.handle.call_args[0][0]["payload"]
        assert payload["output_hash"] is None

    async def test_action_record_emitted_on_ainvoke(
        self, multiply, ctx, mock_client
    ):
        wrapped = LangGraphTracer.wrap_tool(multiply, ctx=ctx, client=mock_client)
        result = await wrapped.ainvoke({"a": 7, "b": 8})
        assert result == 56
        assert mock_client.handle.called
        envelope = mock_client.handle.call_args[0][0]
        assert envelope["event_type"] == EventType.ACTION_RECORD.value
        assert envelope["payload"]["tool_name"] == "multiply"

    def test_double_wrap_prevention(self, multiply, ctx, mock_client):
        wrapped_once = LangGraphTracer.wrap_tool(multiply, ctx=ctx, client=mock_client)
        tools = LangGraphTracer.wrap_tools([wrapped_once], ctx=ctx, client=mock_client)
        # Should not wrap again — _rootsign_instrumented guard.
        assert tools[0] is wrapped_once


class TestWrapTools:
    def test_wrap_tools_preserves_list_length(self, multiply, always_fails, ctx, mock_client):
        tools = [multiply, always_fails]
        wrapped = LangGraphTracer.wrap_tools(tools, ctx=ctx, client=mock_client)
        assert len(wrapped) == len(tools)

    def test_all_wrapped_tools_callable(self, multiply, ctx, mock_client):
        wrapped = LangGraphTracer.wrap_tools([multiply], ctx=ctx, client=mock_client)
        result = wrapped[0].invoke({"a": 10, "b": 10})
        assert result == 100
