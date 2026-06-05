"""Integration tests for the SDK ingest layer — AC-3.1 through AC-3.14.

Each AC-3.x test maps to one criterion in the Phase 0 Req 0.3 spec. The
fixture pattern mirrors AGENTS.md Task 13: per-test SAVEPOINT rollback via
the `db` fixture, fresh IdempotencyStore per test so caches don't leak.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from rootsign import crud
from rootsign.ingest import IdempotencyStore, IngestHandler
from rootsign.ingest.schemas import ErrorCode
from rootsign.models.action import Action
from rootsign.models.approval import Approval
from rootsign.models.session import AgentSession
from rootsign.schemas import (
    AgentCreate,
    AgentEnvironment,
    AgentFramework,
    AgentRiskTier,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def idempotency():
    return IdempotencyStore()


@pytest_asyncio.fixture
async def handler(db, idempotency):
    return IngestHandler(db=db, idempotency=idempotency)


@pytest_asyncio.fixture
async def registered_agent(db):
    return await crud.agent.create(
        db,
        obj_in=AgentCreate(
            name=f"ingest-agent-{uuid4().hex[:8]}",
            owner="test-team",
            environment=AgentEnvironment.PRODUCTION,
            risk_tier=AgentRiskTier.MEDIUM,
            framework=AgentFramework.LANGGRAPH,
        ),
    )


def envelope(
    *,
    event_type: str,
    agent_id: UUID,
    session_id: UUID,
    payload: dict,
    event_id: UUID | None = None,
    schema_version: str = "1.0",
) -> dict:
    return {
        "schema_version": schema_version,
        "sdk_version": "0.1.0",
        "event_type": event_type,
        "event_id": str(event_id or uuid4()),
        "emitted_at": datetime.now(timezone.utc).isoformat(),
        "agent_id": str(agent_id),
        "session_id": str(session_id),
        "payload": payload,
    }


def action_payload(
    *,
    tool_name: str = "send_email",
    authorization_status: str = "auto_authorized",
) -> dict:
    return {
        "tool_name": tool_name,
        "input_hash": "a" * 64,
        "output_hash": "b" * 64,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "authorization_status": authorization_status,
    }


async def open_session(handler, agent_id: UUID, session_id: UUID) -> None:
    r = await handler.handle(
        envelope(
            event_type="SESSION_OPEN",
            agent_id=agent_id,
            session_id=session_id,
            payload={"objective": "test run"},
        )
    )
    assert r.status == "accepted", r


# ---------------------------------------------------------------------------
# AC-3.1 — SESSION_OPEN creates session with status=RUNNING
# ---------------------------------------------------------------------------


class TestAC31_SessionOpen:
    async def test_creates_session_with_status_running(
        self, handler, registered_agent, db
    ):
        session_id = uuid4()
        r = await handler.handle(
            envelope(
                event_type="SESSION_OPEN",
                agent_id=registered_agent.agent_id,
                session_id=session_id,
                payload={"objective": "kickoff", "user_id": "alice@example.com"},
            )
        )
        assert r.status == "accepted"
        assert r.entity_id == session_id

        session = await db.get(AgentSession, session_id)
        assert session is not None
        assert session.status == "running"
        assert session.action_count == 0
        assert session.start_time is not None

    async def test_session_open_for_unknown_agent_rejected(self, handler):
        bad_agent = uuid4()
        r = await handler.handle(
            envelope(
                event_type="SESSION_OPEN",
                agent_id=bad_agent,
                session_id=uuid4(),
                payload={},
            )
        )
        assert r.status == "rejected"
        assert r.error_code is ErrorCode.UNKNOWN_AGENT
        assert r.retryable is False


# ---------------------------------------------------------------------------
# AC-3.2 — ACTION_RECORD returns sequence_number and self_hash
# ---------------------------------------------------------------------------


class TestAC32_ActionRecordReturnsHashChainFields:
    async def test_response_has_sequence_and_self_hash(
        self, handler, registered_agent
    ):
        session_id = uuid4()
        await open_session(handler, registered_agent.agent_id, session_id)

        r = await handler.handle(
            envelope(
                event_type="ACTION_RECORD",
                agent_id=registered_agent.agent_id,
                session_id=session_id,
                payload=action_payload(),
            )
        )
        assert r.status == "accepted"
        assert r.sequence_number == 1
        assert r.self_hash is not None
        assert len(r.self_hash) == 64
        int(r.self_hash, 16)


# ---------------------------------------------------------------------------
# AC-3.3 — Three sequential ACTION_RECORDs produce correct hash chain
# ---------------------------------------------------------------------------


class TestAC33_HashChainViaIngest:
    async def test_three_actions_chain_correctly(
        self, handler, registered_agent, db
    ):
        session_id = uuid4()
        await open_session(handler, registered_agent.agent_id, session_id)

        responses = []
        for i in range(3):
            r = await handler.handle(
                envelope(
                    event_type="ACTION_RECORD",
                    agent_id=registered_agent.agent_id,
                    session_id=session_id,
                    payload=action_payload(tool_name=f"tool_{i}"),
                )
            )
            assert r.status == "accepted"
            responses.append(r)

        actions = await crud.action.get_session_chain(db, session_id=session_id)
        assert len(actions) == 3
        assert actions[0].prev_action_hash is None
        assert actions[1].prev_action_hash == actions[0].self_hash
        assert actions[2].prev_action_hash == actions[1].self_hash

        verify = await crud.action.verify_chain(db, session_id=session_id)
        assert verify["valid"] is True
        assert verify["record_count"] == 3


# ---------------------------------------------------------------------------
# AC-3.4 — ACTION_RECORD for unknown session_id → SESSION_NOT_FOUND
# ---------------------------------------------------------------------------


class TestAC34_UnknownSessionRejected:
    async def test_action_for_unknown_session(self, handler, registered_agent):
        r = await handler.handle(
            envelope(
                event_type="ACTION_RECORD",
                agent_id=registered_agent.agent_id,
                session_id=uuid4(),  # no SESSION_OPEN
                payload=action_payload(),
            )
        )
        assert r.status == "rejected"
        assert r.error_code is ErrorCode.SESSION_NOT_FOUND
        assert r.retryable is False


# ---------------------------------------------------------------------------
# AC-3.5 — Duplicate event_id → idempotent accepted (only 1 DB record)
# ---------------------------------------------------------------------------


class TestAC35_Idempotency:
    async def test_replay_returns_cached_response_and_does_not_duplicate(
        self, handler, registered_agent, db
    ):
        session_id = uuid4()
        await open_session(handler, registered_agent.agent_id, session_id)

        action_event_id = uuid4()
        env = envelope(
            event_type="ACTION_RECORD",
            agent_id=registered_agent.agent_id,
            session_id=session_id,
            payload=action_payload(),
            event_id=action_event_id,
        )
        r1 = await handler.handle(env)
        r2 = await handler.handle(env)  # exact same envelope

        assert r1.status == "accepted"
        assert r2.status == "accepted"
        # Same self_hash, same sequence_number — cached, not re-computed
        assert r1.self_hash == r2.self_hash
        assert r1.sequence_number == r2.sequence_number
        assert r1.entity_id == r2.entity_id

        # Exactly ONE row in the actions table for this session
        actions = await crud.action.get_session_chain(db, session_id=session_id)
        assert len(actions) == 1


# ---------------------------------------------------------------------------
# AC-3.6 — DECISION_RECORD increments session.decision_count
# ---------------------------------------------------------------------------


class TestAC36_DecisionCountIncrement:
    async def test_one_decision_increments_count(
        self, handler, registered_agent, db
    ):
        session_id = uuid4()
        await open_session(handler, registered_agent.agent_id, session_id)

        r = await handler.handle(
            envelope(
                event_type="DECISION_RECORD",
                agent_id=registered_agent.agent_id,
                session_id=session_id,
                payload={
                    "selected_action": "send_email",
                    "reasoning_summary": "Recipient is verified contact.",
                    "confidence": 0.92,
                    "reasoning_depth": "summary",
                },
            )
        )
        assert r.status == "accepted"

        session = await db.get(AgentSession, session_id)
        await db.refresh(session)
        assert session.decision_count == 1


# ---------------------------------------------------------------------------
# AC-3.7 — APPROVAL_RECORD updates Action.authorization_status atomically
# ---------------------------------------------------------------------------


class TestAC37_ApprovalUpdatesAction:
    async def test_approved_flips_action_to_human_approved(
        self, handler, registered_agent, db
    ):
        session_id = uuid4()
        await open_session(handler, registered_agent.agent_id, session_id)

        # Submit a pending action
        r_action = await handler.handle(
            envelope(
                event_type="ACTION_RECORD",
                agent_id=registered_agent.agent_id,
                session_id=session_id,
                payload=action_payload(authorization_status="pending"),
            )
        )
        action_id = r_action.entity_id

        r_approval = await handler.handle(
            envelope(
                event_type="APPROVAL_RECORD",
                agent_id=registered_agent.agent_id,
                session_id=session_id,
                payload={
                    "action_id": str(action_id),
                    "approver_id": "alice@example.com",
                    "approver_type": "human",
                    "context_presented": {"tool_name": "send_email"},
                    "decision": "approved",
                },
            )
        )
        assert r_approval.status == "accepted"

        action = (
            await db.execute(
                select(Action).where(Action.action_id == action_id)
            )
        ).scalar_one()
        assert action.authorization_status == "human_approved"


# ---------------------------------------------------------------------------
# AC-3.8 — SESSION_CLOSE sets status and end_time
# ---------------------------------------------------------------------------


class TestAC38_SessionClose:
    async def test_close_sets_status_and_end_time(
        self, handler, registered_agent, db
    ):
        session_id = uuid4()
        await open_session(handler, registered_agent.agent_id, session_id)

        r = await handler.handle(
            envelope(
                event_type="SESSION_CLOSE",
                agent_id=registered_agent.agent_id,
                session_id=session_id,
                payload={"status": "completed"},
            )
        )
        assert r.status == "accepted"

        session = await db.get(AgentSession, session_id)
        await db.refresh(session)
        assert session.status == "completed"
        assert session.end_time is not None


# ---------------------------------------------------------------------------
# AC-3.9 — Any entity event after SESSION_CLOSE → SESSION_CLOSED
# ---------------------------------------------------------------------------


class TestAC39_PostCloseEventsRejected:
    async def test_action_after_close_rejected(self, handler, registered_agent):
        session_id = uuid4()
        await open_session(handler, registered_agent.agent_id, session_id)
        await handler.handle(
            envelope(
                event_type="SESSION_CLOSE",
                agent_id=registered_agent.agent_id,
                session_id=session_id,
                payload={"status": "completed"},
            )
        )

        r = await handler.handle(
            envelope(
                event_type="ACTION_RECORD",
                agent_id=registered_agent.agent_id,
                session_id=session_id,
                payload=action_payload(),
            )
        )
        assert r.status == "rejected"
        assert r.error_code is ErrorCode.SESSION_CLOSED
        assert r.retryable is False


# ---------------------------------------------------------------------------
# AC-3.10 — schema_version 2.0 → SCHEMA_VERSION_MISMATCH
# ---------------------------------------------------------------------------


class TestAC310_SchemaVersionMismatch:
    async def test_major_version_mismatch_rejected(
        self, handler, registered_agent
    ):
        r = await handler.handle(
            envelope(
                event_type="SESSION_OPEN",
                agent_id=registered_agent.agent_id,
                session_id=uuid4(),
                payload={},
                schema_version="2.0",
            )
        )
        assert r.status == "rejected"
        assert r.error_code is ErrorCode.SCHEMA_VERSION_MISMATCH
        assert r.retryable is False

    async def test_minor_version_bump_accepted(self, handler, registered_agent):
        # Forward-compatible: store on schema 1.0 accepts envelopes labelled 1.99
        r = await handler.handle(
            envelope(
                event_type="SESSION_OPEN",
                agent_id=registered_agent.agent_id,
                session_id=uuid4(),
                payload={},
                schema_version="1.99",
            )
        )
        assert r.status == "accepted"


# ---------------------------------------------------------------------------
# AC-3.11 — SESSION_CLOSE reconciliation logs warning on action_count mismatch
# ---------------------------------------------------------------------------


class TestAC311_CloseReconciliation:
    async def test_action_count_mismatch_logs_warning_but_accepts(
        self, handler, registered_agent, caplog
    ):
        import logging

        session_id = uuid4()
        await open_session(handler, registered_agent.agent_id, session_id)
        # Record 1 action, then claim 99 on close
        await handler.handle(
            envelope(
                event_type="ACTION_RECORD",
                agent_id=registered_agent.agent_id,
                session_id=session_id,
                payload=action_payload(),
            )
        )

        with caplog.at_level(logging.WARNING, logger="rootsign.ingest"):
            r = await handler.handle(
                envelope(
                    event_type="SESSION_CLOSE",
                    agent_id=registered_agent.agent_id,
                    session_id=session_id,
                    payload={
                        "status": "completed",
                        "metadata": {"total_actions": 99},
                    },
                )
            )
        assert r.status == "accepted"
        assert any(
            "action_count mismatch" in record.message for record in caplog.records
        )


# ---------------------------------------------------------------------------
# AC-3.12 — Full session round-trip < 200ms
# ---------------------------------------------------------------------------


class TestAC312_FullSessionRoundTripPerf:
    async def test_open_5_actions_close_under_200ms(
        self, handler, registered_agent
    ):
        import time

        session_id = uuid4()

        start = time.perf_counter()
        await open_session(handler, registered_agent.agent_id, session_id)
        for i in range(5):
            r = await handler.handle(
                envelope(
                    event_type="ACTION_RECORD",
                    agent_id=registered_agent.agent_id,
                    session_id=session_id,
                    payload=action_payload(tool_name=f"t_{i}"),
                )
            )
            assert r.status == "accepted"
        await handler.handle(
            envelope(
                event_type="SESSION_CLOSE",
                agent_id=registered_agent.agent_id,
                session_id=session_id,
                payload={"status": "completed"},
            )
        )
        elapsed = time.perf_counter() - start
        assert elapsed < 0.2, f"Full round-trip took {elapsed:.3f}s — exceeds 200ms"


# ---------------------------------------------------------------------------
# AC-3.13 — Escalation chain: escalated → approved, parent linkage, final state
# ---------------------------------------------------------------------------


class TestAC313_EscalationChain:
    async def test_escalated_then_approved_links_and_resolves_action(
        self, handler, registered_agent, db
    ):
        session_id = uuid4()
        await open_session(handler, registered_agent.agent_id, session_id)

        # 1. Pending action
        r_action = await handler.handle(
            envelope(
                event_type="ACTION_RECORD",
                agent_id=registered_agent.agent_id,
                session_id=session_id,
                payload=action_payload(authorization_status="pending"),
            )
        )
        action_id = r_action.entity_id

        # 2. First approver escalates
        r_esc = await handler.handle(
            envelope(
                event_type="APPROVAL_RECORD",
                agent_id=registered_agent.agent_id,
                session_id=session_id,
                payload={
                    "action_id": str(action_id),
                    "approver_id": "junior@example.com",
                    "approver_type": "human",
                    "context_presented": {"reason": "needs senior review"},
                    "decision": "escalated",
                },
            )
        )
        assert r_esc.status == "accepted"
        escalated_approval_id = r_esc.entity_id

        # Action stays pending while in flight
        action = (
            await db.execute(select(Action).where(Action.action_id == action_id))
        ).scalar_one()
        assert action.authorization_status == "pending"

        # 3. Senior approves, linking to the escalation
        r_resolve = await handler.handle(
            envelope(
                event_type="APPROVAL_RECORD",
                agent_id=registered_agent.agent_id,
                session_id=session_id,
                payload={
                    "action_id": str(action_id),
                    "approver_id": "senior@example.com",
                    "approver_type": "human",
                    "context_presented": {"reason": "verified, approve"},
                    "decision": "approved",
                    "parent_approval_id": str(escalated_approval_id),
                },
            )
        )
        assert r_resolve.status == "accepted"

        # Action is now resolved
        action_after = (
            await db.execute(select(Action).where(Action.action_id == action_id))
        ).scalar_one()
        assert action_after.authorization_status == "human_approved"

        # Resolving approval has the right parent linkage
        resolver = (
            await db.execute(
                select(Approval).where(Approval.approval_id == r_resolve.entity_id)
            )
        ).scalar_one()
        assert resolver.parent_approval_id == escalated_approval_id

    async def test_chained_escalation_rejected(
        self, handler, registered_agent
    ):
        """2-level enforcement: an `escalated` approval may NOT have parent set."""
        session_id = uuid4()
        await open_session(handler, registered_agent.agent_id, session_id)

        r_action = await handler.handle(
            envelope(
                event_type="ACTION_RECORD",
                agent_id=registered_agent.agent_id,
                session_id=session_id,
                payload=action_payload(authorization_status="pending"),
            )
        )
        action_id = r_action.entity_id

        r_first = await handler.handle(
            envelope(
                event_type="APPROVAL_RECORD",
                agent_id=registered_agent.agent_id,
                session_id=session_id,
                payload={
                    "action_id": str(action_id),
                    "approver_id": "a@example.com",
                    "approver_type": "human",
                    "context_presented": {"k": "v"},
                    "decision": "escalated",
                },
            )
        )
        first_id = r_first.entity_id

        # Second escalation pointing at first — should be rejected
        r_second = await handler.handle(
            envelope(
                event_type="APPROVAL_RECORD",
                agent_id=registered_agent.agent_id,
                session_id=session_id,
                payload={
                    "action_id": str(action_id),
                    "approver_id": "b@example.com",
                    "approver_type": "human",
                    "context_presented": {"k": "v"},
                    "decision": "escalated",
                    "parent_approval_id": str(first_id),
                },
            )
        )
        assert r_second.status == "rejected"
        assert r_second.error_code is ErrorCode.VALIDATION_ERROR

    async def test_approval_parent_not_found(self, handler, registered_agent):
        session_id = uuid4()
        await open_session(handler, registered_agent.agent_id, session_id)
        r_action = await handler.handle(
            envelope(
                event_type="ACTION_RECORD",
                agent_id=registered_agent.agent_id,
                session_id=session_id,
                payload=action_payload(authorization_status="pending"),
            )
        )
        action_id = r_action.entity_id

        r = await handler.handle(
            envelope(
                event_type="APPROVAL_RECORD",
                agent_id=registered_agent.agent_id,
                session_id=session_id,
                payload={
                    "action_id": str(action_id),
                    "approver_id": "a@example.com",
                    "approver_type": "human",
                    "context_presented": {"k": "v"},
                    "decision": "approved",
                    "parent_approval_id": str(uuid4()),  # nonexistent
                },
            )
        )
        assert r.status == "rejected"
        assert r.error_code is ErrorCode.APPROVAL_PARENT_NOT_FOUND


# ---------------------------------------------------------------------------
# AC-3.14 — ACTION_ALREADY_RESOLVED on extra APPROVAL_RECORD after terminal state
# ---------------------------------------------------------------------------


class TestAC314_ActionAlreadyResolved:
    async def test_third_approval_after_resolution_rejected(
        self, handler, registered_agent
    ):
        session_id = uuid4()
        await open_session(handler, registered_agent.agent_id, session_id)
        r_action = await handler.handle(
            envelope(
                event_type="ACTION_RECORD",
                agent_id=registered_agent.agent_id,
                session_id=session_id,
                payload=action_payload(authorization_status="pending"),
            )
        )
        action_id = r_action.entity_id

        # Approve once
        await handler.handle(
            envelope(
                event_type="APPROVAL_RECORD",
                agent_id=registered_agent.agent_id,
                session_id=session_id,
                payload={
                    "action_id": str(action_id),
                    "approver_id": "a@example.com",
                    "approver_type": "human",
                    "context_presented": {"k": "v"},
                    "decision": "approved",
                },
            )
        )
        # Try to approve again — terminal state, should reject
        r = await handler.handle(
            envelope(
                event_type="APPROVAL_RECORD",
                agent_id=registered_agent.agent_id,
                session_id=session_id,
                payload={
                    "action_id": str(action_id),
                    "approver_id": "b@example.com",
                    "approver_type": "human",
                    "context_presented": {"k": "v"},
                    "decision": "approved",
                },
            )
        )
        assert r.status == "rejected"
        assert r.error_code is ErrorCode.ACTION_ALREADY_RESOLVED
        assert r.retryable is False
