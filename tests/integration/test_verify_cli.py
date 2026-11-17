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
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from typer.testing import CliRunner

from rootsign.config import settings
from rootsign.crud import action as action_crud
from rootsign.ingest import IdempotencyStore, IngestHandler
from rootsign.models.action import Action
from rootsign.sdk.cli import app

# seeded_agent fixture is shared from tests/conftest.py (Sprint 4 §S4-TASK 8).

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
    async def test_valid_session_exits_0(self, clean_db, seeded_agent, patched_cli_session):
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

        result = await asyncio.to_thread(runner.invoke, app, ["verify", str(session_id)])
        assert result.exit_code == 0, result.output
        assert "VALID" in result.output
        assert "2" in result.output

    async def test_tampered_session_exits_1(self, clean_db, seeded_agent, patched_cli_session):
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

        result = await asyncio.to_thread(runner.invoke, app, ["verify", str(session_id)])
        assert result.exit_code == 1, result.output
        assert "TAMPERED" in result.output

    async def test_unknown_session_exits_3_not_valid(self, clean_db, patched_cli_session):
        """A session ID the database has never seen used to report
        `VALID — 0 records` and exit 0, so a mistyped ID in a cron job read as
        a clean audit. It is "nothing to verify": exit 3, one line."""
        unknown = uuid4()

        result = await asyncio.to_thread(runner.invoke, app, ["verify", str(unknown)])

        assert result.exit_code == 3, result.output
        assert f"no session {unknown} in the database" in result.output
        assert "VALID" not in result.output

    async def test_verbose_lists_each_record_and_its_link(
        self, clean_db, seeded_agent, patched_cli_session
    ):
        handler = IngestHandler(db=clean_db, idempotency=IdempotencyStore())
        session_id = uuid4()
        await handler.handle(
            _envelope(
                event_type="SESSION_OPEN",
                agent_id=seeded_agent.agent_id,
                session_id=session_id,
                payload={"objective": "verify cli verbose"},
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
        await clean_db.commit()
        chain = await action_crud.get_session_chain(clean_db, session_id=session_id)

        result = await asyncio.to_thread(runner.invoke, app, ["verify", str(session_id), "-v"])

        assert result.exit_code == 0, result.output
        assert "not yet implemented" not in result.output
        assert "#1  tool_0" in result.output and "#2  tool_1" in result.output
        assert chain[0].self_hash in result.output
        assert f"prev_action_hash  {chain[0].self_hash}" in result.output


class TestVerifyBadInput:
    """audit #11a: `verify <bad-uuid>` must give a clean one-line error, not a
    raw ValueError traceback (matching how `approve` already handles it). Bad
    arguments are "nothing to verify", so they exit 3, never a verdict code."""

    def test_bad_uuid_exits_3_with_clean_message(self):
        # Sync invoke is fine — parsing fails before the CLI's asyncio.run.
        result = runner.invoke(app, ["verify", "not-a-uuid"])
        assert result.exit_code == 3
        assert "not a valid UUID" in result.output
        assert "Traceback" not in result.output
