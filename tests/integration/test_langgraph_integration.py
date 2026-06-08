"""End-to-end LangGraph integration tests against the real Postgres+Timescale DB.

These tests drive the LangGraphTracer through `LocalIngestClient` so the
hash chain in `actions` is actually built and verified by
`crud.action.verify_chain`. They use `ainvoke` (not `invoke`) because the
test is async — sharing the test loop's `AsyncSession` across a worker
thread (the `_run_sync` fallback) would cross loop boundaries and confuse
asyncpg. See ADR-004 § "Sync-path correctness — running event loop".
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("langgraph", reason="LangGraph not installed")
pytest.importorskip("langchain_core", reason="langchain-core not installed")

from langchain_core.tools import tool  # noqa: E402

from rootsign.crud import action as action_crud  # noqa: E402
from rootsign.sdk.client import LocalIngestClient  # noqa: E402
from rootsign.sdk.context import SessionContext  # noqa: E402
from rootsign.sdk.frameworks.langgraph import LangGraphTracer  # noqa: E402


def _make_tools():
    """Return a fresh set of tools per test — `wrap_tool` mutates in place."""

    @tool
    def add_numbers(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    @tool
    def to_uppercase(text: str) -> str:
        """Convert text to uppercase."""
        return text.upper()

    @tool
    def reverse_string(text: str) -> str:
        """Reverse a string."""
        return text[::-1]

    return [add_numbers, to_uppercase, reverse_string]


class TestLangGraphFullPipeline:
    async def test_three_tool_calls_produce_correct_chain(
        self, db, registered_agent, make_envelope_fixture
    ):
        client = LocalIngestClient(db=db)
        session_id = uuid4()
        ctx = SessionContext(
            agent_id=registered_agent.agent_id,
            session_id=session_id,
        )

        # Open session.
        await client.handle(
            make_envelope_fixture(
                "SESSION_OPEN",
                registered_agent.agent_id,
                session_id,
                {"objective": "LangGraph integration test"},
            )
        )

        tools = LangGraphTracer.wrap_tools(_make_tools(), ctx=ctx, client=client)

        await tools[0].ainvoke({"a": 3, "b": 4})
        await tools[1].ainvoke({"text": "hello"})
        await tools[2].ainvoke({"text": "world"})

        # Close session.
        await client.handle(
            make_envelope_fixture(
                "SESSION_CLOSE",
                registered_agent.agent_id,
                session_id,
                {"status": "completed", "metadata": {"total_actions": 3}},
            )
        )

        chain = await action_crud.get_session_chain(db, session_id=session_id)
        assert len(chain) == 3
        assert chain[0].tool_name == "add_numbers"
        assert chain[1].tool_name == "to_uppercase"
        assert chain[2].tool_name == "reverse_string"
        assert [a.sequence_number for a in chain] == [1, 2, 3]

        result = await action_crud.verify_chain(db, session_id=session_id)
        assert result["valid"] is True
        assert result["record_count"] == 3

    async def test_exception_does_not_break_chain(
        self, db, registered_agent, make_envelope_fixture
    ):
        client = LocalIngestClient(db=db)
        session_id = uuid4()
        ctx = SessionContext(
            agent_id=registered_agent.agent_id,
            session_id=session_id,
        )

        await client.handle(
            make_envelope_fixture(
                "SESSION_OPEN",
                registered_agent.agent_id,
                session_id,
                {"objective": "LangGraph exception test"},
            )
        )

        @tool
        def broken_tool(x: int) -> int:
            """Always fails."""
            raise RuntimeError("tool failure")

        wrapped = LangGraphTracer.wrap_tool(broken_tool, ctx=ctx, client=client)
        with pytest.raises(RuntimeError, match="tool failure"):
            await wrapped.ainvoke({"x": 1})

        chain = await action_crud.get_session_chain(db, session_id=session_id)
        assert len(chain) == 1
        assert chain[0].output_hash is None  # null for failed tool

        result = await action_crud.verify_chain(db, session_id=session_id)
        assert result["valid"] is True
