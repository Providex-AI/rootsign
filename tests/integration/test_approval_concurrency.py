"""Concurrency regression tests for the approval terminal-state guard.

Pre-Phase-2 audit #5: `create_with_chain_link` did a plain SELECT, checked
the terminal guard, then inserted — so under READ COMMITTED two concurrent
resolvers (the `rootsign approve` CLI and the poll-loop timeout writer) could
both pass the guard and write two Approval rows, last-writer-wins on
`Action.authorization_status`. The "human wins ties" invariant (ADR-007)
depends on that guard serialising, which it didn't.

Fix under test:
  1. `SELECT ... FOR UPDATE` on the Action row serialises the resolvers.
  2. A partial unique index `uq_approvals_action_resolution` on
     approvals(action_id) WHERE decision <> 'escalated' is the DB-level
     backstop: a double-insert surfaces as IntegrityError, not two rows.

Real PostgreSQL only (project rule: no mock-based integration tests). Each
concurrent resolver runs on its own AsyncSession → its own connection, so
the FOR UPDATE lock is genuinely contended across transactions.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from rootsign.config import settings
from rootsign.crud.approval import approval as approval_crud
from rootsign.errors import ActionAlreadyResolvedError
from rootsign.ingest import IdempotencyStore, IngestHandler
from rootsign.models.action import Action
from rootsign.models.approval import Approval

# seeded_agent / clean_db shared from tests/conftest.py.


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


async def _seed_pending_action(
    *, clean_db, agent_id: UUID, tool_name: str = "send_invoice"
) -> tuple[UUID, UUID, datetime]:
    """Commit a session + one pending action. Returns (session_id, action_id, action_timestamp)."""
    handler = IngestHandler(db=clean_db, idempotency=IdempotencyStore())
    session_id = uuid4()
    await handler.handle(
        _envelope(
            event_type="SESSION_OPEN",
            agent_id=agent_id,
            session_id=session_id,
            payload={"objective": "concurrency test"},
        )
    )
    response = await handler.handle(
        _envelope(
            event_type="ACTION_RECORD",
            agent_id=agent_id,
            session_id=session_id,
            payload={
                "tool_name": tool_name,
                "input_hash": "a" * 64,
                "output_hash": None,
                "input_redacted": {"tool_name": tool_name, "x": 42},
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "authorization_status": "pending",
            },
        )
    )
    await clean_db.commit()
    action_id = response.entity_id
    assert action_id is not None
    action = (
        await clean_db.execute(select(Action).where(Action.action_id == action_id))
    ).scalar_one()
    return session_id, action_id, action.timestamp


@pytest.fixture
def independent_sessions():
    """Factory bound to TEST_DATABASE_URL via NullPool — each call is a fresh
    connection, so two sessions genuinely contend on the FOR UPDATE lock."""
    engine = create_async_engine(settings.TEST_DATABASE_URL, poolclass=NullPool, future=True)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield factory


async def _resolve(factory, **kwargs):
    """Run one create_with_chain_link on its own committed session.

    Returns the resulting authorization_status on success, or the raised
    exception on failure (so gather can compare the two outcomes)."""
    async with factory() as session:
        try:
            await approval_crud.create_with_chain_link(session, **kwargs)
            await session.commit()
        except BaseException as exc:  # noqa: BLE001 - test needs the exception object
            await session.rollback()
            return exc
    return "ok"


class TestConcurrentResolvers:
    async def test_human_and_timeout_race_leaves_one_winner(
        self, clean_db, seeded_agent, independent_sessions
    ):
        """Two resolvers hit the same pending action at once — exactly one
        wins, one Approval row exists, and the action lands in a terminal
        state. Which one wins is timing-dependent; that exactly one wins is
        not."""
        _, action_id, action_ts = await _seed_pending_action(
            clean_db=clean_db, agent_id=seeded_agent.agent_id
        )

        human = _resolve(
            independent_sessions,
            action_id=action_id,
            action_timestamp=action_ts,
            approver_id="cli:operator",
            approver_type="human",
            context_presented={},
            decision="approved",
        )
        timeout = _resolve(
            independent_sessions,
            action_id=action_id,
            action_timestamp=action_ts,
            approver_id="system:timeout",
            approver_type="timeout_auto_rejected",
            context_presented={},
            decision="rejected",
            decision_reason="No response within window",
        )
        results = await asyncio.gather(human, timeout)

        oks = [r for r in results if r == "ok"]
        losers = [r for r in results if r != "ok"]
        assert len(oks) == 1, f"expected exactly one winner, got {results!r}"
        # The loser lost either at the terminal guard (serialised) or at the
        # unique index (both passed the guard). Both are acceptable proof the
        # double-write was prevented.
        assert isinstance(losers[0], (ActionAlreadyResolvedError, IntegrityError)), (
            f"unexpected loser exception: {losers[0]!r}"
        )

        # Exactly one Approval row for this action.
        count = (
            await clean_db.execute(
                select(func.count()).select_from(Approval).where(Approval.action_id == action_id)
            )
        ).scalar_one()
        assert count == 1

        # Action is terminal, matching the surviving approval's semantics.
        action = (
            await clean_db.execute(select(Action).where(Action.action_id == action_id))
        ).scalar_one()
        assert action.authorization_status in {
            "human_approved",
            "human_rejected",
            "timed_out",
        }


class TestPartialUniqueIndex:
    async def test_duplicate_resolution_rejected_by_index(self, clean_db, seeded_agent):
        """DB-level backstop: two non-escalated approvals for one action
        violate the partial unique index even if the guard were bypassed."""
        session_id, action_id, _ = await _seed_pending_action(
            clean_db=clean_db, agent_id=seeded_agent.agent_id
        )
        clean_db.add(
            Approval(
                approval_id=uuid4(),
                action_id=action_id,
                session_id=session_id,
                approver_id="a@test.com",
                approver_type="human",
                context_presented={},
                decision="approved",
                timestamp=datetime.now(timezone.utc),
            )
        )
        await clean_db.flush()
        clean_db.add(
            Approval(
                approval_id=uuid4(),
                action_id=action_id,
                session_id=session_id,
                approver_id="b@test.com",
                approver_type="human",
                context_presented={},
                decision="rejected",
                timestamp=datetime.now(timezone.utc),
            )
        )
        with pytest.raises(IntegrityError):
            await clean_db.flush()
        await clean_db.rollback()

    async def test_escalated_plus_resolution_allowed(self, clean_db, seeded_agent):
        """The index excludes decision='escalated', so an escalated approval
        plus its resolving child coexist for the same action."""
        session_id, action_id, _ = await _seed_pending_action(
            clean_db=clean_db, agent_id=seeded_agent.agent_id
        )
        escalated = Approval(
            approval_id=uuid4(),
            action_id=action_id,
            session_id=session_id,
            approver_id="tier1@test.com",
            approver_type="human",
            context_presented={},
            decision="escalated",
            timestamp=datetime.now(timezone.utc),
        )
        clean_db.add(escalated)
        await clean_db.flush()
        clean_db.add(
            Approval(
                approval_id=uuid4(),
                action_id=action_id,
                session_id=session_id,
                approver_id="tier2@test.com",
                approver_type="human",
                context_presented={},
                decision="approved",
                parent_approval_id=escalated.approval_id,
                timestamp=datetime.now(timezone.utc),
            )
        )
        # No IntegrityError — one escalated + one resolution is legal.
        await clean_db.flush()
        count = (
            await clean_db.execute(
                select(func.count()).select_from(Approval).where(Approval.action_id == action_id)
            )
        ).scalar_one()
        assert count == 2
