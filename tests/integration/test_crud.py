"""Integration tests against a real PostgreSQL + TimescaleDB instance.

Covers AC-2.3 through AC-2.12. Each test runs inside a SAVEPOINT that is
rolled back on teardown (see tests/conftest.py).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from providex import crud
from providex.models.action import Action
from providex.models.session import ProvidexSession
from providex.schemas import (
    ActionAuthorizationStatus,
    ActionCreate,
    AgentCreate,
    AgentEnvironment,
    AgentFramework,
    AgentRiskTier,
    ApprovalCreate,
    ApprovalDecision,
    ApproverType,
    DecisionCreate,
    SessionCreate,
    SessionStatus,
)

pytestmark = pytest.mark.integration


async def _make_agent(db) -> "Agent":  # noqa: F821
    return await crud.agent.create(
        db,
        obj_in=AgentCreate(
            name=f"agent-{uuid4().hex[:8]}",
            owner="platform-team",
            environment=AgentEnvironment.PRODUCTION,
            risk_tier=AgentRiskTier.HIGH,
            framework=AgentFramework.LANGGRAPH,
        ),
    )


async def _make_session(db, agent_id: UUID) -> ProvidexSession:
    return await crud.session.create(
        db,
        obj_in=SessionCreate(agent_id=agent_id, status=SessionStatus.RUNNING),
    )


def _action_create(session_id: UUID, **overrides) -> ActionCreate:
    kwargs = {
        "session_id": session_id,
        "tool_name": "send_email",
        "input_hash": "a" * 64,
        "output_hash": "b" * 64,
        "authorization_status": ActionAuthorizationStatus.AUTO_AUTHORIZED,
    }
    kwargs.update(overrides)
    return ActionCreate(**kwargs)


class TestRoundTrip:
    # AC-2.7
    async def test_agent_round_trip(self, db: AsyncSession):
        created = await _make_agent(db)
        fetched = await crud.agent.get(db, created.agent_id)
        assert fetched is not None
        assert fetched.name == created.name
        assert fetched.environment == "production"
        assert fetched.risk_tier == "high"
        assert fetched.framework == "langgraph"
        assert fetched.is_active is True

    async def test_session_round_trip(self, db: AsyncSession):
        agent = await _make_agent(db)
        s = await _make_session(db, agent.agent_id)
        fetched = await crud.session.get(db, s.session_id)
        assert fetched is not None
        assert fetched.agent_id == agent.agent_id
        assert fetched.status == "running"
        assert fetched.action_count == 0

    async def test_approval_context_preserved_deep_equality(self, db: AsyncSession):
        # AC-1.8
        agent = await _make_agent(db)
        s = await _make_session(db, agent.agent_id)
        # We need a real action_id for FK semantics — but Approval has no SQL FK
        # to Action so we can use any UUID. Use a fresh one.
        fake_action_id = uuid4()
        context = {
            "prompt": "Send email to user X?",
            "risk_signals": ["external_recipient", "first_time"],
            "agent_metadata": {"version": "1.2", "model": "gpt-4o"},
            "nested": {"deeply": {"nested": [1, 2, {"key": "value"}]}},
        }
        a = await crud.approval.create(
            db,
            obj_in=ApprovalCreate(
                action_id=fake_action_id,
                session_id=s.session_id,
                approver_id="alice@example.com",
                approver_type=ApproverType.HUMAN,
                context_presented=context,
                decision=ApprovalDecision.APPROVED,
            ),
        )
        fetched = await crud.approval.get(db, a.approval_id)
        assert fetched is not None
        assert fetched.context_presented == context


class TestForeignKeys:
    # AC-2.3
    async def test_action_with_unknown_session_id_fails(self, db: AsyncSession):
        bogus_session_id = uuid4()
        # Try to insert an Action directly with no matching session row.
        a = Action(
            action_id=uuid4(),
            session_id=bogus_session_id,
            tool_name="send_email",
            input_hash="a" * 64,
            self_hash="b" * 64,
            timestamp=datetime.now(timezone.utc),
            authorization_status="auto_authorized",
            sequence_number=1,
        )
        db.add(a)
        with pytest.raises(IntegrityError):
            await db.flush()


class TestHashChain:
    # AC-2.4
    async def test_three_sequential_actions_chain_correctly(self, db: AsyncSession):
        agent = await _make_agent(db)
        s = await _make_session(db, agent.agent_id)

        a1 = await crud.action.create_with_hash(
            db, obj_in=_action_create(s.session_id, tool_name="t1"), session_obj=s
        )
        a2 = await crud.action.create_with_hash(
            db, obj_in=_action_create(s.session_id, tool_name="t2"), session_obj=s
        )
        a3 = await crud.action.create_with_hash(
            db, obj_in=_action_create(s.session_id, tool_name="t3"), session_obj=s
        )

        assert a1.prev_action_hash is None
        assert a1.sequence_number == 1
        assert a2.prev_action_hash == a1.self_hash
        assert a2.sequence_number == 2
        assert a3.prev_action_hash == a2.self_hash
        assert a3.sequence_number == 3

        # Session denormalized counters updated atomically.
        await db.refresh(s)
        assert s.action_count == 3
        assert s.chain_head_hash == a1.self_hash
        assert s.chain_tail_hash == a3.self_hash

    # AC-2.5
    async def test_verify_chain_unmodified_10_actions(self, db: AsyncSession):
        agent = await _make_agent(db)
        s = await _make_session(db, agent.agent_id)
        for i in range(10):
            await crud.action.create_with_hash(
                db,
                obj_in=_action_create(s.session_id, tool_name=f"tool_{i}"),
                session_obj=s,
            )
        result = await crud.action.verify_chain(db, session_id=s.session_id)
        assert result["valid"] is True
        assert result["record_count"] == 10
        assert result["first_invalid_sequence"] is None
        assert result["error"] is None

    # AC-2.6
    async def test_verify_chain_detects_self_hash_corruption(self, db: AsyncSession):
        agent = await _make_agent(db)
        s = await _make_session(db, agent.agent_id)
        a1 = await crud.action.create_with_hash(
            db, obj_in=_action_create(s.session_id, tool_name="t1"), session_obj=s
        )
        await crud.action.create_with_hash(
            db, obj_in=_action_create(s.session_id, tool_name="t2"), session_obj=s
        )

        # Corrupt the stored self_hash on action 1.
        await db.execute(
            update(Action)
            .where(Action.action_id == a1.action_id)
            .values(self_hash="0" * 64)
        )
        await db.flush()

        result = await crud.action.verify_chain(db, session_id=s.session_id)
        assert result["valid"] is False
        assert result["first_invalid_sequence"] == 1
        assert "self_hash mismatch" in result["error"]

    async def test_verify_chain_empty_session_is_valid(self, db: AsyncSession):
        agent = await _make_agent(db)
        s = await _make_session(db, agent.agent_id)
        result = await crud.action.verify_chain(db, session_id=s.session_id)
        assert result == {
            "valid": True,
            "record_count": 0,
            "first_invalid_sequence": None,
            "error": None,
        }

    # AC-2.11
    async def test_get_session_chain_orders_by_sequence_number(
        self, db: AsyncSession
    ):
        agent = await _make_agent(db)
        s = await _make_session(db, agent.agent_id)

        # Insert with deliberately non-monotonic timestamps to prove ordering is
        # by sequence_number, not by time.
        base_ts = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
        ts_order = [
            base_ts + timedelta(seconds=5),  # action 1 — latest timestamp
            base_ts + timedelta(seconds=0),  # action 2 — earliest timestamp
            base_ts + timedelta(seconds=3),
        ]
        for i, ts in enumerate(ts_order, start=1):
            await crud.action.create_with_hash(
                db,
                obj_in=_action_create(s.session_id, tool_name=f"t{i}", timestamp=ts),
                session_obj=s,
            )

        chain = await crud.action.get_session_chain(db, session_id=s.session_id)
        assert [a.sequence_number for a in chain] == [1, 2, 3]
        assert [a.tool_name for a in chain] == ["t1", "t2", "t3"]


class TestHypertable:
    # AC-2.2
    async def test_actions_is_hypertable(self, db: AsyncSession):
        result = await db.execute(
            text(
                "SELECT hypertable_name FROM timescaledb_information.hypertables "
                "WHERE hypertable_name = 'actions'"
            )
        )
        assert result.scalar_one_or_none() == "actions"


class TestConcurrentInserts:
    """AC-2.12 — concurrent inserts must produce unique, contiguous sequence_numbers.

    SAVEPOINT-based test isolation can't span concurrent connections, so this
    test uses `clean_db` which commits and then truncates on teardown.
    """

    async def test_five_concurrent_inserts_unique_sequence_numbers(
        self, clean_db: AsyncSession, test_engine
    ):
        agent = await _make_agent(clean_db)
        s = await _make_session(clean_db, agent.agent_id)
        await clean_db.commit()
        session_id = s.session_id

        factory = async_sessionmaker(
            test_engine, class_=AsyncSession, expire_on_commit=False
        )

        async def writer(i: int):
            async with factory() as worker_db:
                async with worker_db.begin():
                    await crud.action.create_with_hash(
                        worker_db,
                        obj_in=_action_create(session_id, tool_name=f"concurrent_{i}"),
                    )

        await asyncio.gather(*(writer(i) for i in range(5)))

        async with factory() as reader:
            chain = await crud.action.get_session_chain(reader, session_id=session_id)
            seqs = sorted(a.sequence_number for a in chain)
            assert seqs == [1, 2, 3, 4, 5], (
                f"Expected contiguous 1..5, got {seqs}"
            )
            verify = await crud.action.verify_chain(reader, session_id=session_id)
            assert verify["valid"] is True, verify

    async def test_session_action_count_constraint(self, db: AsyncSession):
        agent = await _make_agent(db)
        s = await _make_session(db, agent.agent_id)
        with pytest.raises(IntegrityError):
            await db.execute(
                update(ProvidexSession)
                .where(ProvidexSession.session_id == s.session_id)
                .values(action_count=-1)
            )
            await db.flush()
