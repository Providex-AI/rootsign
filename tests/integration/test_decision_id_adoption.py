"""The PostgreSQL store keeps a client-minted decision id (spec §3.3).

The ingest handler is the store a hosted backend runs. When a DECISION_RECORD
carries the id the cloud transport minted, the store must keep it, or the
action that points at it dangles. Reusing an id for a different event is a
client bug and is refused, rather than overwriting the first decision.
"""

from __future__ import annotations

from uuid import uuid4

from rootsign.ingest import IdempotencyStore, IngestHandler
from rootsign.models.decision import Decision
from tests.conftest import make_envelope

DECISION = {"selected_action": "send_invoice", "confidence": 0.9, "reasoning_depth": "summary"}


async def _open(handler, agent_id, session_id):
    response = await handler.handle(
        make_envelope("SESSION_OPEN", agent_id, session_id, {"objective": "invoice ACME"})
    )
    assert response.status == "accepted", response


async def test_a_client_minted_id_is_the_stored_id(clean_db, seeded_agent):
    handler = IngestHandler(db=clean_db, idempotency=IdempotencyStore())
    session_id, minted = uuid4(), uuid4()
    await _open(handler, seeded_agent.agent_id, session_id)

    response = await handler.handle(
        make_envelope(
            "DECISION_RECORD",
            seeded_agent.agent_id,
            session_id,
            {**DECISION, "decision_id": str(minted)},
        )
    )

    assert response.status == "accepted", response
    assert response.entity_id == minted
    assert await clean_db.get(Decision, minted) is not None


async def test_without_one_the_store_mints_as_before(clean_db, seeded_agent):
    handler = IngestHandler(db=clean_db, idempotency=IdempotencyStore())
    session_id = uuid4()
    await _open(handler, seeded_agent.agent_id, session_id)

    response = await handler.handle(
        make_envelope("DECISION_RECORD", seeded_agent.agent_id, session_id, dict(DECISION))
    )

    assert response.status == "accepted" and response.entity_id is not None


async def test_an_id_reused_by_another_event_is_refused(clean_db, seeded_agent):
    handler = IngestHandler(db=clean_db, idempotency=IdempotencyStore())
    session_id, minted = uuid4(), uuid4()
    await _open(handler, seeded_agent.agent_id, session_id)
    payload = {**DECISION, "decision_id": str(minted)}
    first = await handler.handle(
        make_envelope("DECISION_RECORD", seeded_agent.agent_id, session_id, payload)
    )
    assert first.status == "accepted"

    second = await handler.handle(
        make_envelope(
            "DECISION_RECORD",
            seeded_agent.agent_id,
            session_id,
            {**payload, "selected_action": "something_else"},
        )
    )

    assert second.status == "rejected"
    assert second.error_code.value == "VALIDATION_ERROR"
    assert "already stored for another event" in second.error_message
    assert (await clean_db.get(Decision, minted)).selected_action == "send_invoice"
