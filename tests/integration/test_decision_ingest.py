"""Verification test for PRD-19 T4 — DECISION_RECORD ingest wiring.

`_handle_decision_record` was built in Phase 0; this test confirms it
still creates a Decision DB row end-to-end via LocalIngestClient. The
SDK-side `_emit_decision_record` helper (T5) builds the same envelope
shape, so a green run here proves the ingest path is ready to receive
emissions from the new SDK helper.

Note: the envelope payload does NOT carry `decision_id`. The handler
auto-generates it (mirroring how ACTION_RECORD action_ids are assigned)
and returns the new id in `IngestResponse.entity_id`. The SDK helper
reads the id back from the response and stashes it as the pending slot
on SessionContext.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from rootsign.crud import decision as decision_crud
from rootsign.sdk.client import LocalIngestClient
from tests.conftest import make_envelope


class TestDecisionRecordIngest:
    async def test_decision_record_accepted_and_persisted(
        self, clean_db, seeded_agent
    ):
        client = LocalIngestClient(db=clean_db)
        session_id = uuid4()

        await client.handle(
            make_envelope(
                "SESSION_OPEN", seeded_agent.agent_id, session_id, {}
            )
        )

        resp = await client.handle(
            {
                "schema_version": "1.0",
                "sdk_version": "0.1.1",
                "event_type": "DECISION_RECORD",
                "event_id": str(uuid4()),
                "emitted_at": datetime.now(timezone.utc).isoformat(),
                "agent_id": str(seeded_agent.agent_id),
                "session_id": str(session_id),
                "payload": {
                    "selected_action": "test_tool",
                    "reasoning_summary": "Test reasoning.",
                    "confidence": 0.9,
                    "alternatives_considered": [],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "reasoning_depth": "summary",
                    "reasoning_captured": True,
                },
            }
        )
        assert resp.status == "accepted"
        assert resp.entity_id is not None  # handler-assigned decision_id
        await clean_db.commit()

        decisions = await decision_crud.get_by_session(
            clean_db, session_id=session_id
        )
        assert len(decisions) == 1
        assert decisions[0].decision_id == resp.entity_id
        assert decisions[0].selected_action == "test_tool"
        assert decisions[0].reasoning_summary == "Test reasoning."
