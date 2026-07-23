"""Integration tests for `rootsign approve` (Sprint 4 §S4-TASK 7).

End-to-end: seed an Agent + Session + pending Action via IngestHandler,
then drive the CLI via `asyncio.to_thread(runner.invoke, ...)` (Flag 2
Rule B/C) and assert on the resulting DB state.

The `patched_cli_session` fixture rebinds `rootsign.sdk.cli.AsyncSessionLocal`
to the TEST_DATABASE_URL — mirrors the pattern in `test_verify_cli.py`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from typer.testing import CliRunner

from rootsign.config import settings
from rootsign.ingest import IdempotencyStore, IngestHandler
from rootsign.models.action import Action
from rootsign.models.approval import Approval
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


def _pending_action_payload(tool_name: str) -> dict:
    return {
        "tool_name": tool_name,
        "input_hash": "a" * 64,
        "output_hash": None,  # HiTL pending — no output yet
        "input_redacted": {"tool_name": tool_name, "x": 42},
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "authorization_status": "pending",
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


async def _seed_pending_action(
    *, clean_db, agent_id: UUID, tool_name: str = "send_invoice"
) -> tuple[UUID, UUID]:
    """Open a session + insert one pending action. Returns (session_id, action_id)."""
    handler = IngestHandler(db=clean_db, idempotency=IdempotencyStore())
    session_id = uuid4()
    await handler.handle(
        _envelope(
            event_type="SESSION_OPEN",
            agent_id=agent_id,
            session_id=session_id,
            payload={"objective": "approve-cli test"},
        )
    )
    response = await handler.handle(
        _envelope(
            event_type="ACTION_RECORD",
            agent_id=agent_id,
            session_id=session_id,
            payload=_pending_action_payload(tool_name),
        )
    )
    await clean_db.commit()
    assert response.entity_id is not None
    return session_id, response.entity_id


class TestApproveListCLI:
    async def test_list_empty_shows_message(self, clean_db, seeded_agent, patched_cli_session):
        result = await asyncio.to_thread(runner.invoke, app, ["approve", "--list"])
        assert result.exit_code == 0, result.output
        assert "No pending approvals" in result.output

    async def test_list_shows_pending_actions(self, clean_db, seeded_agent, patched_cli_session):
        _, action_id = await _seed_pending_action(
            clean_db=clean_db,
            agent_id=seeded_agent.agent_id,
            tool_name="send_invoice",
        )
        result = await asyncio.to_thread(runner.invoke, app, ["approve", "--list"])
        assert result.exit_code == 0, result.output
        assert "Pending approvals" in result.output
        assert str(action_id) in result.output
        assert "send_invoice" in result.output


class TestApproveAction:
    async def test_approve_flips_status_and_writes_approval(
        self, clean_db, seeded_agent, patched_cli_session
    ):
        session_id, action_id = await _seed_pending_action(
            clean_db=clean_db, agent_id=seeded_agent.agent_id
        )
        result = await asyncio.to_thread(runner.invoke, app, ["approve", str(action_id)])
        assert result.exit_code == 0, result.output
        assert "approved" in result.output

        # Re-read state on a fresh session (CLI's commit lands cross-conn)
        action = (
            await clean_db.execute(select(Action).where(Action.action_id == action_id))
        ).scalar_one()
        assert action.authorization_status == "human_approved"
        approval = (
            await clean_db.execute(select(Approval).where(Approval.action_id == action_id))
        ).scalar_one()
        assert approval.decision == "approved"
        # audit #7a: approver_id now records the actual OS operator under the
        # "cli:" namespace instead of a hardcoded "cli:operator".
        import getpass

        assert approval.approver_id == f"cli:{getpass.getuser()}"
        assert approval.approver_id.startswith("cli:")
        assert approval.approver_type == "human"
        # session_id pulled from the looked-up Action — verifies the
        # CRUDApproval.create_with_chain_link auto-fill works in CLI.
        assert approval.session_id == session_id

    async def test_reject_with_reason_persists_status_and_reason(
        self, clean_db, seeded_agent, patched_cli_session
    ):
        _, action_id = await _seed_pending_action(clean_db=clean_db, agent_id=seeded_agent.agent_id)
        result = await asyncio.to_thread(
            runner.invoke,
            app,
            ["approve", str(action_id), "--reject", "--reason", "Too risky"],
        )
        assert result.exit_code == 0, result.output
        assert "rejected" in result.output

        action = (
            await clean_db.execute(select(Action).where(Action.action_id == action_id))
        ).scalar_one()
        assert action.authorization_status == "human_rejected"
        approval = (
            await clean_db.execute(select(Approval).where(Approval.action_id == action_id))
        ).scalar_one()
        assert approval.decision == "rejected"
        assert approval.decision_reason == "Too risky"


class TestApproveErrors:
    async def test_invalid_uuid_argument(self, clean_db, seeded_agent, patched_cli_session):
        result = await asyncio.to_thread(runner.invoke, app, ["approve", "not-a-uuid"])
        assert result.exit_code == 1
        assert "not a valid UUID" in result.output

    async def test_missing_action_and_no_list_flag(
        self, clean_db, seeded_agent, patched_cli_session
    ):
        result = await asyncio.to_thread(runner.invoke, app, ["approve"])
        assert result.exit_code == 1
        assert "provide an action_id" in result.output

    async def test_action_not_pending_exits_1(self, clean_db, seeded_agent, patched_cli_session):
        """A second approval on the same action must fail — the first
        flipped status away from 'pending', so the SELECT misses."""
        _, action_id = await _seed_pending_action(clean_db=clean_db, agent_id=seeded_agent.agent_id)
        # First approval — succeeds
        result1 = await asyncio.to_thread(runner.invoke, app, ["approve", str(action_id)])
        assert result1.exit_code == 0, result1.output
        # Second — no pending row matches
        result2 = await asyncio.to_thread(runner.invoke, app, ["approve", str(action_id)])
        assert result2.exit_code == 1
        assert "no pending action found" in result2.output

    async def test_unknown_action_id(self, clean_db, seeded_agent, patched_cli_session):
        missing = uuid4()
        result = await asyncio.to_thread(runner.invoke, app, ["approve", str(missing)])
        assert result.exit_code == 1
        assert str(missing) in result.output
