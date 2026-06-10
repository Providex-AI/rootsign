"""CrewAI contract tests — verify the interception path against the real
CrewAI surface. Mock IngestClient (no DB needed).

The CI matrix runs this file against two CrewAI versions. Locally we exercise
whatever version `uv pip list` reports.

Each test pulls a fresh tool from the factory because `wrap_tool` mutates in
place — see ADR-005.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

pytest.importorskip("crewai", reason="CrewAI not installed")

from crewai.tools import tool  # noqa: E402

from rootsign.ingest.schemas import EventType  # noqa: E402
from rootsign.sdk.context import SessionContext  # noqa: E402
from rootsign.sdk.frameworks.crewai import CrewAITracer, _is_crewai_tool  # noqa: E402


def _make_multiply():
    @tool("Multiply Numbers")
    def multiply(a: int, b: int) -> int:
        """Multiply two integers."""
        return a * b

    return multiply


def _make_always_fails():
    @tool("Always Fails")
    def always_fails(x: str) -> str:
        """Always raises an error."""
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


class TestIsCrewAITool:
    def test_detects_crewai_tool(self, multiply):
        assert _is_crewai_tool(multiply) is True

    def test_rejects_plain_function(self):
        def plain():
            pass

        assert _is_crewai_tool(plain) is False

    def test_rejects_already_instrumented(self, multiply, ctx, mock_client):
        wrapped = CrewAITracer.wrap_tool(multiply, ctx=ctx, client=mock_client)
        assert _is_crewai_tool(wrapped) is False


class TestCrewAIToolMetadata:
    def test_name_preserved(self, multiply, ctx, mock_client):
        original_name = multiply.name
        wrapped = CrewAITracer.wrap_tool(multiply, ctx=ctx, client=mock_client)
        assert wrapped.name == original_name

    def test_description_preserved(self, multiply, ctx, mock_client):
        original_desc = multiply.description
        wrapped = CrewAITracer.wrap_tool(multiply, ctx=ctx, client=mock_client)
        assert wrapped.description == original_desc

    def test_instrumented_flag_set(self, multiply, ctx, mock_client):
        wrapped = CrewAITracer.wrap_tool(multiply, ctx=ctx, client=mock_client)
        assert wrapped._rootsign_instrumented is True


class TestCrewAICallInterception:
    def test_action_record_emitted_on_run(self, multiply, ctx, mock_client):
        wrapped = CrewAITracer.wrap_tool(multiply, ctx=ctx, client=mock_client)
        result = wrapped._run(a=3, b=4)
        assert result == 12
        assert mock_client.handle.called
        envelope = mock_client.handle.call_args[0][0]
        assert envelope["event_type"] == EventType.ACTION_RECORD.value
        assert envelope["payload"]["tool_name"] == "Multiply Numbers"

    def test_input_hash_present_and_valid(self, multiply, ctx, mock_client):
        wrapped = CrewAITracer.wrap_tool(multiply, ctx=ctx, client=mock_client)
        wrapped._run(a=5, b=6)
        payload = mock_client.handle.call_args[0][0]["payload"]
        assert len(payload["input_hash"]) == 64
        assert all(c in "0123456789abcdef" for c in payload["input_hash"])

    def test_input_hash_varies_with_inputs(self, ctx, mock_client):
        """Different inputs → different input_hash. Regression for the
        spec bug where every call hashed empty args/kwargs."""
        t1 = _make_multiply()
        t2 = _make_multiply()
        w1 = CrewAITracer.wrap_tool(t1, ctx=ctx, client=mock_client)
        w2 = CrewAITracer.wrap_tool(t2, ctx=ctx, client=mock_client)
        w1._run(a=1, b=2)
        first_hash = mock_client.handle.call_args[0][0]["payload"]["input_hash"]
        w2._run(a=9, b=9)
        second_hash = mock_client.handle.call_args[0][0]["payload"]["input_hash"]
        assert first_hash != second_hash

    def test_exception_emits_action_with_null_output_hash(
        self, always_fails, ctx, mock_client
    ):
        wrapped = CrewAITracer.wrap_tool(always_fails, ctx=ctx, client=mock_client)
        with pytest.raises(ValueError, match="intentional failure"):
            wrapped._run(x="test")
        assert mock_client.handle.called
        payload = mock_client.handle.call_args[0][0]["payload"]
        assert payload["output_hash"] is None

    def test_action_records_have_parity_with_langgraph(
        self, multiply, ctx, mock_client
    ):
        """ACTION_RECORD from CrewAI must have same payload fields as LangGraph."""
        wrapped = CrewAITracer.wrap_tool(multiply, ctx=ctx, client=mock_client)
        wrapped._run(a=1, b=2)
        payload = mock_client.handle.call_args[0][0]["payload"]
        required_fields = {
            "tool_name",
            "input_hash",
            "output_hash",
            "input_redacted",
            "output_redacted",
            "timestamp",
            "authorization_status",
        }
        assert required_fields.issubset(payload.keys())


class TestWrapTools:
    def test_wrap_tools_preserves_list_length(self, multiply, always_fails, ctx, mock_client):
        tools = [multiply, always_fails]
        wrapped = CrewAITracer.wrap_tools(tools, ctx=ctx, client=mock_client)
        assert len(wrapped) == len(tools)

    def test_all_wrapped_tools_callable(self, multiply, ctx, mock_client):
        wrapped = CrewAITracer.wrap_tools([multiply], ctx=ctx, client=mock_client)
        assert wrapped[0]._run(a=10, b=10) == 100
