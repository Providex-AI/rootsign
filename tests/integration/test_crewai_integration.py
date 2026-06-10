"""End-to-end CrewAI integration tests against the real Postgres+Timescale DB.

These tests drive the CrewAITracer through `LocalIngestClient` so the hash
chain in `actions` is actually built and verified by
`crud.action.verify_chain`.

CrewAI's `_run` is synchronous. Calling it from an async test would dispatch
through `_async_bridge._run_sync` into a worker-thread loop — but the
`AsyncSession` from the `db` fixture is bound to the *test's* loop, and
asyncpg refuses cross-loop futures. We therefore drive the same emission
logic via the awaitable shadow `_rootsign_arun` mounted by the tracer.
This mirrors how `tests/integration/test_langgraph_integration.py` uses
`ainvoke` rather than `invoke`. Sync `_run` is the production surface and
is exercised by the contract suite — see ADR-005.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("crewai", reason="CrewAI not installed")

from crewai.tools import tool  # noqa: E402

from rootsign.crud import action as action_crud  # noqa: E402
from rootsign.sdk.client import LocalIngestClient  # noqa: E402
from rootsign.sdk.context import SessionContext  # noqa: E402
from rootsign.sdk.frameworks.crewai import CrewAITracer  # noqa: E402


def _make_tools():
    """Fresh tools per test — `wrap_tool` mutates in place."""

    @tool("Add Numbers")
    def add_numbers(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    @tool("To Uppercase")
    def to_uppercase(text: str) -> str:
        """Convert to uppercase."""
        return text.upper()

    @tool("Count Words")
    def count_words(text: str) -> int:
        """Count words in text."""
        return len(text.split())

    return [add_numbers, to_uppercase, count_words]


class TestCrewAIFullPipeline:
    async def test_three_crewai_tools_produce_valid_chain(
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
                {"objective": "CrewAI integration test"},
            )
        )

        tools = CrewAITracer.wrap_tools(_make_tools(), ctx=ctx, client=client)

        await tools[0]._rootsign_arun(a=10, b=20)
        await tools[1]._rootsign_arun(text="hello world")
        await tools[2]._rootsign_arun(text="one two three four")

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
        assert chain[0].tool_name == "Add Numbers"
        assert [a.sequence_number for a in chain] == [1, 2, 3]

        result = await action_crud.verify_chain(db, session_id=session_id)
        assert result["valid"] is True
        assert result["record_count"] == 3

    async def test_schema_parity_with_langgraph(
        self, db, registered_agent, make_envelope_fixture
    ):
        """CrewAI and LangGraph Action records have identical payload fields."""
        client = LocalIngestClient(db=db)
        session_id = uuid4()
        ctx = SessionContext(
            agent_id=registered_agent.agent_id,
            session_id=session_id,
        )
        await client.handle(
            make_envelope_fixture(
                "SESSION_OPEN", registered_agent.agent_id, session_id, {}
            )
        )

        @tool("Add Numbers")
        def add_numbers(a: int, b: int) -> int:
            """Add."""
            return a + b

        tools = CrewAITracer.wrap_tools([add_numbers], ctx=ctx, client=client)
        await tools[0]._rootsign_arun(a=1, b=2)

        chain = await action_crud.get_session_chain(db, session_id=session_id)
        assert len(chain) == 1
        action = chain[0]
        assert action.tool_name == "Add Numbers"
        assert action.input_hash is not None and len(action.input_hash) == 64
        assert action.self_hash is not None and len(action.self_hash) == 64
        assert action.authorization_status == "auto_authorized"
        assert action.sequence_number == 1
