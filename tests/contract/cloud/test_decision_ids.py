"""A decision keeps one id from the cloud client to the store (spec §3.3).

The SDK links each action to the decision before it by the id the decision
came back with. Over the wire that is the store's id. But during an outage the
spool answered, so the spooled action points at the spool's id; and
`rootsign-admin sync` used to drop that id when it rebuilt the envelope, so the
store minted another and the synced action pointed at nothing. The transport
now mints the id itself, as it does an action's, and every store keeps it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx

from rootsign.sdk.client import HttpIngestClient
from rootsign.sdk.jsonl_client import JsonlIngestClient
from rootsign.sdk.spool import read_spool_session
from tests.conftest import make_envelope
from tests.support.mock_ingest_store import MockIngestStore
from tests.support.spool_tree import dead_endpoint

BASE_URL = "https://ingest.example.test/v1"
API_KEY = "sk-contract"

DECISION = {
    "selected_action": "send_invoice",
    "confidence": 0.9,
    "reasoning_summary": "within policy",
    "reasoning_depth": "summary",
    "timestamp": "2026-11-27T10:00:00+00:00",
}


def _record(client: Any) -> tuple[str, str]:
    """Drive a session the way the SDK does; return (session_id, the action's decision_id)."""
    agent_id, session_id = uuid4(), uuid4()

    async def _run() -> str:
        await client.handle(make_envelope("SESSION_OPEN", agent_id, session_id, {"objective": "x"}))
        decision = await client.handle(
            make_envelope("DECISION_RECORD", agent_id, session_id, dict(DECISION))
        )
        # The decorator stashes the returned id as the next action's decision.
        linked = str(decision.entity_id)
        await client.handle(
            make_envelope(
                "ACTION_RECORD",
                agent_id,
                session_id,
                {
                    "tool_name": "send_invoice",
                    "input_hash": "a" * 64,
                    "output_hash": "b" * 64,
                    "timestamp": "2026-11-27T10:01:00+00:00",
                    "decision_id": linked,
                },
            )
        )
        await client.handle(
            make_envelope(
                "SESSION_CLOSE",
                agent_id,
                session_id,
                {"status": "completed", "metadata": {"total_actions": 1}},
            )
        )
        await client.close()
        return linked

    return str(session_id), asyncio.run(_run())


def _stored_decision_ids(store: MockIngestStore, session_id: str) -> list[str]:
    return [
        store.entity_ids[e["event_id"]]
        for e in store.accepted
        if e["event_type"] == "DECISION_RECORD" and e["session_id"] == session_id
    ]


def test_over_the_wire_the_store_keeps_the_clients_id():
    store = MockIngestStore()
    session_id, linked = _record(HttpIngestClient(BASE_URL, API_KEY, transport=store.transport()))

    (sent,) = [e for e in store.accepted if e["event_type"] == "DECISION_RECORD"]
    assert UUID(sent["payload"]["decision_id"])  # minted by the client
    assert _stored_decision_ids(store, session_id) == [linked]


def test_after_an_outage_and_a_sync_the_action_still_points_at_its_decision(tmp_path: Path):
    recorder = HttpIngestClient(
        BASE_URL,
        API_KEY,
        max_retries=1,
        transport=httpx.MockTransport(dead_endpoint),
        spool_dir=str(tmp_path),
    )
    session_id, linked = _record(recorder)
    spooled = read_spool_session(tmp_path / "sessions" / f"{session_id}.jsonl").envelopes

    store = MockIngestStore()
    replay = HttpIngestClient(BASE_URL, API_KEY, transport=store.transport(), enable_spool=False)

    async def _sync():
        for envelope in spooled:
            await replay.handle(envelope)
        await replay.close()

    asyncio.run(_sync())

    (action,) = [e for e in store.accepted if e["event_type"] == "ACTION_RECORD"]
    assert action["payload"]["decision_id"] == linked
    assert _stored_decision_ids(store, session_id) == [linked]


def test_a_spool_file_from_before_this_fix_replays_its_decision_id(tmp_path: Path):
    """Older files hold the id only at the top of the line; replay lifts it into the payload."""
    writer = JsonlIngestClient(data_dir=tmp_path)
    agent_id, session_id = uuid4(), uuid4()

    async def _write() -> str:
        await writer.handle(make_envelope("SESSION_OPEN", agent_id, session_id, {"objective": "x"}))
        response = await writer.handle(
            make_envelope("DECISION_RECORD", agent_id, session_id, dict(DECISION))
        )
        return str(response.entity_id)

    minted = asyncio.run(_write())
    replayed = read_spool_session(tmp_path / "sessions" / f"{session_id}.jsonl").envelopes

    (decision,) = [e for e in replayed if e["event_type"] == "DECISION_RECORD"]
    assert decision["payload"]["decision_id"] == minted
    assert "decision_id" not in decision  # the envelope itself stays within its schema
