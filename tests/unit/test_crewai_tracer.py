"""Unit tests for the CrewAI tracer — see ADR-005.

Framework-version-agnostic. The CI matrix runs the version-specific contract
suite in `tests/contract/crewai/` separately.

Each test asks for a fresh tool via the factory because `wrap_tool` mutates
the tool in place — re-using one across tests would compound wraps.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

pytest.importorskip("crewai", reason="CrewAI not installed")

from crewai.tools import tool  # noqa: E402

from rootsign.sdk.context import SessionContext  # noqa: E402
from rootsign.sdk.frameworks.crewai import CrewAITracer, _is_crewai_tool  # noqa: E402


def _make_double_tool():
    @tool("Double")
    def double(x: int) -> int:
        """Double the input."""
        return x * 2

    return double


@pytest.fixture
def double_tool():
    return _make_double_tool()


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


class TestIsCrewAITool:
    def test_detects_crewai_tool(self, double_tool):
        assert _is_crewai_tool(double_tool) is True

    def test_rejects_plain_function(self):
        def plain():
            pass

        assert _is_crewai_tool(plain) is False

    def test_rejects_already_instrumented(self, double_tool, ctx, mock_client):
        wrapped = CrewAITracer.wrap_tool(double_tool, ctx=ctx, client=mock_client)
        assert _is_crewai_tool(wrapped) is False

    def test_langchain_tool_also_matches_shape(self):
        """LangChain StructuredTool has .name (str) and ._run (callable).

        Duck-typing intentionally matches it — disambiguation is the
        decorator's job (LangChain check runs first). This test locks
        the documented behaviour so a future "tighten the check" doesn't
        silently change semantics.
        """
        pytest.importorskip("langchain_core")
        from langchain_core.tools import tool as lc_tool

        @lc_tool
        def lc_test(x: int) -> int:
            """LC test."""
            return x

        assert _is_crewai_tool(lc_test) is True


class TestCrewAITracerMetadata:
    def test_name_preserved(self, double_tool, ctx, mock_client):
        original_name = double_tool.name
        wrapped = CrewAITracer.wrap_tool(double_tool, ctx=ctx, client=mock_client)
        assert wrapped.name == original_name

    def test_description_preserved(self, double_tool, ctx, mock_client):
        original_desc = double_tool.description
        wrapped = CrewAITracer.wrap_tool(double_tool, ctx=ctx, client=mock_client)
        assert wrapped.description == original_desc

    def test_args_schema_preserved(self, double_tool, ctx, mock_client):
        original_schema = double_tool.args_schema
        wrapped = CrewAITracer.wrap_tool(double_tool, ctx=ctx, client=mock_client)
        assert wrapped.args_schema is original_schema

    def test_instrumented_flag_set(self, double_tool, ctx, mock_client):
        wrapped = CrewAITracer.wrap_tool(double_tool, ctx=ctx, client=mock_client)
        assert wrapped._rootsign_instrumented is True

    def test_context_attached(self, double_tool, ctx, mock_client):
        wrapped = CrewAITracer.wrap_tool(double_tool, ctx=ctx, client=mock_client)
        assert wrapped._rootsign_context is ctx


class TestCrewAITracerWrapTools:
    def test_wrap_tools_list_length_preserved(self, ctx, mock_client):
        tools = [_make_double_tool(), _make_double_tool()]
        wrapped = CrewAITracer.wrap_tools(tools, ctx=ctx, client=mock_client)
        assert len(wrapped) == 2

    def test_wrap_tools_double_wrap_skipped(self, ctx, mock_client):
        t = _make_double_tool()
        once = CrewAITracer.wrap_tools([t], ctx=ctx, client=mock_client)[0]
        run_after_first = once._run
        twice = CrewAITracer.wrap_tools([once], ctx=ctx, client=mock_client)[0]
        # Same identity; ._run NOT re-wrapped a second time.
        assert twice is once
        assert twice._run is run_after_first

    def test_wrap_tools_rejects_missing_ctx(self, mock_client):
        with pytest.raises(TypeError):
            CrewAITracer.wrap_tools([_make_double_tool()], client=mock_client)


class TestCrewAITracerDecoratorRouting:
    def test_at_trace_routes_crewai_tool_to_crewai_tracer(
        self, double_tool, ctx, mock_client
    ):
        from rootsign.sdk.decorator import trace

        wrapped = trace(ingest_client=mock_client, session_context=ctx)(double_tool)
        assert wrapped._rootsign_instrumented is True
        # CrewAI path leaves ._run callable; LangGraph would not (it mounts
        # .invoke/.ainvoke instead). Tools coming out of the CrewAI tracer
        # therefore still have ._run as the primary entry point.
        assert hasattr(wrapped, "_run")

    def test_at_trace_routes_langchain_tool_to_langgraph_tracer(
        self, ctx, mock_client
    ):
        """Load-bearing for ADR-005: LangChain check MUST win.

        LangChain's StructuredTool satisfies both `_is_langchain_tool` AND
        `_is_crewai_tool`. If routing order ever flips, this test fails.
        """
        pytest.importorskip("langchain_core")
        from langchain_core.tools import tool as lc_tool

        from rootsign.sdk.decorator import trace

        @lc_tool
        def lc_test(x: int) -> int:
            """LC test."""
            return x

        wrapped = trace(ingest_client=mock_client, session_context=ctx)(lc_test)
        assert wrapped._rootsign_instrumented is True
        # LangGraph tracer replaces .invoke / .ainvoke — not ._run — and the
        # original Pydantic model carries an `invoke` attribute regardless.
        # The discriminating evidence: ._run on a LangChain tool is the raw
        # one, not our traced_run closure. Inspect via __closure__: our
        # traced_run captures the name "captured_args" in its closure cells.
        run_cells = getattr(wrapped._run, "__closure__", None) or ()
        captured_names = {cell.cell_contents for cell in run_cells if hasattr(cell, "cell_contents")}
        # CrewAITracer's traced_run closes over `original_run` callable +
        # `tool_name` str. If we accidentally routed to CrewAITracer, ._run
        # would be a CLOSURE rather than the bound method on the
        # StructuredTool. The simplest invariant: traced LangChain tools
        # still have the *bound method* shape for ._run — they don't go
        # through CrewAITracer.
        # Direct invariant check: traced LangChain tools have a re-mounted
        # .invoke whose qualname comes from langgraph tracer.
        assert wrapped.invoke.__name__ == "traced_invoke"
        # And ._run was not touched by either tracer.
        assert "captured_args" not in captured_names
