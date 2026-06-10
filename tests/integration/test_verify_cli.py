"""Integration tests for `rootsign verify`.

Driving Typer's CliRunner from an async test is delicate: the CLI calls
`asyncio.run(...)` internally, which refuses to run inside an already-
running loop. We therefore dispatch `runner.invoke(...)` via
`asyncio.to_thread` so the CLI's own `asyncio.run` lands in a worker
thread with no pre-existing loop.

A separate engine bound to `TEST_DATABASE_URL` is monkeypatched onto
`rootsign.sdk.cli.AsyncSessionLocal` so the CLI reads the *test* database
(`rootsign_test`), not the development database the production factory
points at.

We seed the chain via the IngestHandler directly (no LangGraph tracer),
because the tracer's worker-thread bridge fights asyncpg's loop affinity
when the test loop and the tracer loop differ. Tracer correctness is
covered by `tests/integration/test_langgraph_integration.py` /
`test_crewai_integration.py`; this file's job is the CLI surface.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from typer.testing import CliRunner

from rootsign import crud
from rootsign.config import settings
from rootsign.crud import action as action_crud
from rootsign.ingest import IdempotencyStore, IngestHandler
from rootsign.models.action import Action
from rootsign.schemas import (
    AgentCreate,
    AgentEnvironment,
    AgentFramework,
    AgentRiskTier,
)
from rootsign.sdk.cli import app

runner = CliRunner()


def _envelope(*, event_type: str, agent_id: UUID, session_id: UUID, payload: dict) -> dict:
    return {
        "schema_version": "1.0",
        "sdk_version": "0.1.0",
        "event_type": event_type,
        "event_id": str(uuid4()),
        "emitted_at": datetime.now(timezone.utc).isoformat(),
        "agent_id": str(agent_id),
        "session_id": str(session_id),
        "payload": payload,
    }


def _action_payload(tool_name: str) -> dict:
    return {
        "tool_name": tool_name,
        "input_hash": "a" * 64,
        "output_hash": "b" * 64,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "authorization_status": "auto_authorized",
    }


@pytest_asyncio.fixture
async def seeded_agent(clean_db):
    """Create an agent inside the *clean_db* connection so subsequent
    Action FKs in that same connection resolve cleanly. Using `db`'s
    transactional fixture would leave the agent invisible to clean_db's
    connection."""
    return await crud.agent.create(
        clean_db,
        obj_in=AgentCreate(
            name=f"verify-cli-{uuid4().hex[:8]}",
            owner="test-team",
            environment=AgentEnvironment.PRODUCTION,
            risk_tier=AgentRiskTier.MEDIUM,
            framework=AgentFramework.LANGGRAPH,
        ),
    )


@pytest.fixture
def patched_cli_session(monkeypatch):
    """Bind the CLI's session factory to TEST_DATABASE_URL."""
    test_engine_local = create_async_engine(
        settings.TEST_DATABASE_URL, poolclass=NullPool, future=True
    )
    factory = async_sessionmaker(bind=test_engine_local, expire_on_commit=False)
    monkeypatch.setattr("rootsign.sdk.cli.AsyncSessionLocal", factory)
    yield factory


class TestVerifyCLI:
    async def test_valid_session_exits_0(
        self, clean_db, seeded_agent, patched_cli_session
    ):
        handler = IngestHandler(db=clean_db, idempotency=IdempotencyStore())
        session_id = uuid4()
        await handler.handle(
            _envelope(
                event_type="SESSION_OPEN",
                agent_id=seeded_agent.agent_id,
                session_id=session_id,
                payload={"objective": "verify cli"},
            )
        )
        for i in range(2):
            await handler.handle(
                _envelope(
                    event_type="ACTION_RECORD",
                    agent_id=seeded_agent.agent_id,
                    session_id=session_id,
                    payload=_action_payload(f"tool_{i}"),
                )
            )
        await handler.handle(
            _envelope(
                event_type="SESSION_CLOSE",
                agent_id=seeded_agent.agent_id,
                session_id=session_id,
                payload={"status": "completed"},
            )
        )
        await clean_db.commit()

        result = await asyncio.to_thread(
            runner.invoke, app, ["verify", str(session_id)]
        )
        assert result.exit_code == 0, result.output
        assert "VALID" in result.output
        assert "2" in result.output

    async def test_tampered_session_exits_1(
        self, clean_db, seeded_agent, patched_cli_session
    ):
        handler = IngestHandler(db=clean_db, idempotency=IdempotencyStore())
        session_id = uuid4()
        await handler.handle(
            _envelope(
                event_type="SESSION_OPEN",
                agent_id=seeded_agent.agent_id,
                session_id=session_id,
                payload={"objective": "verify cli tamper"},
            )
        )
        await handler.handle(
            _envelope(
                event_type="ACTION_RECORD",
                agent_id=seeded_agent.agent_id,
                session_id=session_id,
                payload=_action_payload("tool_to_tamper"),
            )
        )
        await clean_db.commit()

        chain = await action_crud.get_session_chain(clean_db, session_id=session_id)
        assert len(chain) == 1
        await clean_db.execute(
            update(Action)
            .where(Action.action_id == chain[0].action_id)
            .values(self_hash="corrupted_hash_value_" + "0" * 43)
        )
        await clean_db.commit()

        result = await asyncio.to_thread(
            runner.invoke, app, ["verify", str(session_id)]
        )
        assert result.exit_code == 1, result.output
        assert "TAMPERED" in result.output
