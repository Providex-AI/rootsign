"""Integration tests for PRD-19 — opt-in Decision capture end-to-end.

Each test drives the SDK through the LangGraphTracer wrap path with a real
LocalIngestClient against PostgreSQL+TimescaleDB. Uses `seeded_agent`
(commits) per Sprint 3 Flag 3 — the decorator path reads/writes across
asyncio.Lock boundaries that SAVEPOINT rollback cannot serve.
"""

from __future__ import annotations

import importlib
from uuid import uuid4

import pytest

from rootsign.crud import action as action_crud
from rootsign.crud import decision as decision_crud
from rootsign.sdk import config as cfg
from rootsign.sdk.client import LocalIngestClient
from rootsign.sdk.context import SessionContext
from tests.conftest import make_envelope

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")
from langchain_core.tools import tool  # noqa: E402

from rootsign.sdk.frameworks.langgraph import LangGraphTracer  # noqa: E402


def _make_tools():
    """Per-test tool factory.

    Module-level `@tool` definitions cache their underlying ainvoke to
    whichever test's event loop saw them first. Subsequent tests in
    different loops then trip asyncpg's "Future attached to a different
    loop" error. Same pattern as `test_show_hn_quickstart.py::
    _make_quickstart_tools` and `test_langgraph_integration.py::_make_tools`.
    """

    @tool
    def process_payment(amount: float) -> str:
        """Process a payment."""
        return "processed"

    return [process_payment]


class TestDecisionCaptureDisabled:
    async def test_no_decision_records_when_flag_off(
        self, clean_db, seeded_agent, monkeypatch
    ):
        monkeypatch.setenv("ROOTSIGN_CAPTURE_DECISIONS", "false")
        importlib.reload(cfg)

        client = LocalIngestClient(db=clean_db)
        session_id = uuid4()
        ctx = SessionContext(
            agent_id=seeded_agent.agent_id, session_id=session_id
        )
        await client.handle(
            make_envelope(
                "SESSION_OPEN", seeded_agent.agent_id, session_id, {}
            )
        )
        tools = LangGraphTracer.wrap_tools(
            _make_tools(), ctx=ctx, client=client
        )
        await tools[0].ainvoke({"amount": 100.0})
        await clean_db.commit()

        chain = await action_crud.get_session_chain(
            clean_db, session_id=session_id
        )
        assert len(chain) == 1
        assert chain[0].decision_id is None  # not populated when off

        decisions = await decision_crud.get_by_session(
            clean_db, session_id=session_id
        )
        assert len(decisions) == 0


class TestDecisionCaptureEnabled:
    async def test_decision_record_created_and_linked(
        self, clean_db, seeded_agent, monkeypatch
    ):
        monkeypatch.setenv("ROOTSIGN_CAPTURE_DECISIONS", "true")
        monkeypatch.setenv("ROOTSIGN_REASONING_DEPTH", "summary")
        importlib.reload(cfg)

        client = LocalIngestClient(db=clean_db)
        session_id = uuid4()
        ctx = SessionContext(
            agent_id=seeded_agent.agent_id, session_id=session_id
        )
        await client.handle(
            make_envelope(
                "SESSION_OPEN", seeded_agent.agent_id, session_id, {}
            )
        )

        decision_id = await ctx.record_decision(
            selected_action="process_payment",
            reasoning_summary="Amount within policy limit; auto-approved.",
            confidence=0.97,
            ingest_client=client,
        )
        assert decision_id is not None

        tools = LangGraphTracer.wrap_tools(
            _make_tools(), ctx=ctx, client=client
        )
        await tools[0].ainvoke({"amount": 100.0})
        await clean_db.commit()

        chain = await action_crud.get_session_chain(
            clean_db, session_id=session_id
        )
        assert len(chain) == 1
        assert chain[0].decision_id == decision_id

        decisions = await decision_crud.get_by_session(
            clean_db, session_id=session_id
        )
        assert len(decisions) == 1
        assert decisions[0].selected_action == "process_payment"
        assert (
            decisions[0].reasoning_summary
            == "Amount within policy limit; auto-approved."
        )

        result = await action_crud.verify_chain(
            clean_db, session_id=session_id
        )
        assert result["valid"] is True
        assert result["record_count"] == 1

    async def test_pending_decision_id_cleared_after_one_action(
        self, clean_db, seeded_agent, monkeypatch
    ):
        monkeypatch.setenv("ROOTSIGN_CAPTURE_DECISIONS", "true")
        importlib.reload(cfg)

        client = LocalIngestClient(db=clean_db)
        session_id = uuid4()
        ctx = SessionContext(
            agent_id=seeded_agent.agent_id, session_id=session_id
        )
        await client.handle(
            make_envelope(
                "SESSION_OPEN", seeded_agent.agent_id, session_id, {}
            )
        )

        await ctx.record_decision(
            selected_action="process_payment",
            ingest_client=client,
        )
        tools = LangGraphTracer.wrap_tools(
            _make_tools() + _make_tools(), ctx=ctx, client=client
        )
        await tools[0].ainvoke({"amount": 100.0})  # consumes decision_id
        await tools[1].ainvoke({"amount": 200.0})  # no decision_id
        await clean_db.commit()

        chain = await action_crud.get_session_chain(
            clean_db, session_id=session_id
        )
        assert chain[0].decision_id is not None  # first action: linked
        assert chain[1].decision_id is None  # second action: not linked

    async def test_minimal_depth_stores_no_reasoning(
        self, clean_db, seeded_agent, monkeypatch
    ):
        monkeypatch.setenv("ROOTSIGN_CAPTURE_DECISIONS", "true")
        monkeypatch.setenv("ROOTSIGN_REASONING_DEPTH", "minimal")
        importlib.reload(cfg)

        client = LocalIngestClient(db=clean_db)
        session_id = uuid4()
        ctx = SessionContext(
            agent_id=seeded_agent.agent_id, session_id=session_id
        )
        await client.handle(
            make_envelope(
                "SESSION_OPEN", seeded_agent.agent_id, session_id, {}
            )
        )
        await ctx.record_decision(
            selected_action="process_payment",
            reasoning_summary="This should NOT be stored at minimal depth.",
            ingest_client=client,
        )
        await clean_db.commit()

        decisions = await decision_crud.get_by_session(
            clean_db, session_id=session_id
        )
        assert decisions[0].reasoning_summary is None
        assert decisions[0].selected_action == "process_payment"
