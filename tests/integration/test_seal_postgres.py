"""The PostgreSQL store seals each session at close (ADR-016), in migration 0007's column.

The ingest handler computes the seal from what the store holds, through the
same reader `verify_session` uses (`rootsign.crud.seal.session_facts`), and
stores it in `sessions.seal`. A client seal that differs is kept beside the
store's under `client` (Decision 4). And `rootsign approve` cannot add an
approval to a session that has closed, because that would change the records
the seal already covers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from rootsign.crud.approval import approval as approval_crud
from rootsign.crud.seal import session_facts
from rootsign.errors import SessionClosedError
from rootsign.ingest import IdempotencyStore, IngestHandler
from rootsign.models.action import Action
from rootsign.models.session import AgentSession
from rootsign.seal import SealOutcome, check_seal, compute_seal
from tests.conftest import make_envelope


async def _ok(handler, envelope):
    response = await handler.handle(envelope)
    assert response.status == "accepted", response
    return response


async def _session_with_side_records(handler, agent_id, *, close: dict | None = None):
    """Open, a decision, a held action approved over ingest, an ordinary action, close."""
    session_id = uuid4()
    await _ok(handler, make_envelope("SESSION_OPEN", agent_id, session_id, {"objective": "pay"}))
    decision = await _ok(
        handler,
        make_envelope(
            "DECISION_RECORD",
            agent_id,
            session_id,
            {"selected_action": "wire_transfer", "confidence": 0.8, "reasoning_depth": "summary"},
        ),
    )
    held = await _ok(
        handler,
        make_envelope(
            "ACTION_RECORD",
            agent_id,
            session_id,
            {
                "tool_name": "wire_transfer",
                "input_hash": "a" * 64,
                "output_hash": None,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "authorization_status": "pending",
                "decision_id": str(decision.entity_id),
            },
        ),
    )
    await _ok(
        handler,
        make_envelope(
            "APPROVAL_RECORD",
            agent_id,
            session_id,
            {
                "action_id": str(held.entity_id),
                "approver_id": "cli:operator",
                "approver_type": "human",
                "context_presented": {"tool_name": "wire_transfer"},
                "decision": "approved",
                "decision_reason": "verified",
            },
        ),
    )
    await _ok(
        handler,
        make_envelope(
            "ACTION_RECORD",
            agent_id,
            session_id,
            {
                "tool_name": "log_payment",
                "input_hash": "c" * 64,
                "output_hash": "d" * 64,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        ),
    )
    payload = close or {"status": "completed", "metadata": {"total_actions": 2}}
    await _ok(handler, make_envelope("SESSION_CLOSE", agent_id, session_id, payload))
    return session_id


async def test_a_closed_session_is_sealed_and_checks(clean_db, seeded_agent):
    handler = IngestHandler(db=clean_db, idempotency=IdempotencyStore())
    session_id = await _session_with_side_records(handler, seeded_agent.agent_id)
    await clean_db.commit()

    session = await clean_db.get(AgentSession, session_id)
    assert session.seal["version"] == 1 and session.seal["final_sequence"] == 2
    facts = await session_facts(clean_db, session, status=session.status)
    check = check_seal(session.seal, facts)
    assert check.sealed, check.detail


async def test_an_edited_status_after_close_is_detected(clean_db, seeded_agent):
    """What `verify` will read: a store row changed after its seal was written."""
    handler = IngestHandler(db=clean_db, idempotency=IdempotencyStore())
    session_id = await _session_with_side_records(handler, seeded_agent.agent_id)
    await clean_db.commit()

    session = await clean_db.get(AgentSession, session_id)
    actions = (await clean_db.execute(Action.__table__.select())).all()
    held = next(a for a in actions if a.tool_name == "wire_transfer")
    await clean_db.execute(
        Action.__table__.update()
        .where(Action.action_id == held.action_id, Action.timestamp == held.timestamp)
        .values(authorization_status="human_rejected")
    )
    await clean_db.commit()

    facts = await session_facts(clean_db, session, status=session.status)
    assert check_seal(session.seal, facts).outcome is SealOutcome.SIDE_EDITED


async def test_a_client_seal_that_differs_is_kept_beside_the_stores(clean_db, seeded_agent):
    handler = IngestHandler(db=clean_db, idempotency=IdempotencyStore())
    theirs = {"version": 1, "final_sequence": 2, "seal_hash": "f" * 64}
    session_id = await _session_with_side_records(
        handler,
        seeded_agent.agent_id,
        close={"status": "completed", "metadata": {"total_actions": 2}, "seal": theirs},
    )
    await clean_db.commit()

    stored = (await clean_db.get(AgentSession, session_id)).seal
    assert stored["client"] == theirs
    assert stored["seal_hash"] != theirs["seal_hash"]


async def test_a_client_seal_that_agrees_is_not_duplicated(clean_db, seeded_agent):
    handler = IngestHandler(db=clean_db, idempotency=IdempotencyStore())
    session_id = await _session_with_side_records(handler, seeded_agent.agent_id)
    await clean_db.commit()
    session = await clean_db.get(AgentSession, session_id)
    agreed = compute_seal(
        await session_facts(clean_db, session, status="completed"), final_sequence=2
    )

    assert {k: v for k, v in session.seal.items() if k != "client"} == agreed
    assert "client" not in session.seal


async def test_an_action_of_a_closed_session_cannot_be_approved(clean_db, seeded_agent):
    """A held call cancelled inside its session leaves a pending action behind."""
    handler = IngestHandler(db=clean_db, idempotency=IdempotencyStore())
    session_id = uuid4()
    agent_id = seeded_agent.agent_id
    await _ok(handler, make_envelope("SESSION_OPEN", agent_id, session_id, {"objective": "pay"}))
    held = await _ok(
        handler,
        make_envelope(
            "ACTION_RECORD",
            agent_id,
            session_id,
            {
                "tool_name": "wire_transfer",
                "input_hash": "a" * 64,
                "output_hash": None,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "authorization_status": "pending",
            },
        ),
    )
    await _ok(
        handler,
        make_envelope(
            "SESSION_CLOSE",
            agent_id,
            session_id,
            {"status": "failed", "metadata": {"total_actions": 1}},
        ),
    )
    await clean_db.commit()
    action = (
        await clean_db.execute(Action.__table__.select().where(Action.action_id == held.entity_id))
    ).one()

    with pytest.raises(SessionClosedError, match="is failed, so no call is waiting"):
        await approval_crud.create_with_chain_link(
            clean_db,
            action_id=action.action_id,
            action_timestamp=action.timestamp,
            approver_id="cli:operator",
            approver_type="human",
            context_presented={},
            decision="approved",
        )
