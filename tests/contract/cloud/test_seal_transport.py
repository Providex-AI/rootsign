"""The cloud transport seals each session it sends, and the store checks it (ADR-016).

The chain is sealed on the client here (ADR-013), so the session is too: the
transport computes the seal over everything it sent and puts it on the close
before the close is sent or spooled. A store recomputes its own seal from what
it holds and never trusts the client's; when they differ, it keeps the
client's beside its own rather than refusing the close (Decision 4). The mock
store implements that rule, so these tests pin both halves.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from rootsign.ingest.schemas import ErrorCode
from rootsign.sdk.client import HttpIngestClient
from rootsign.sdk.spool import read_spool_session
from rootsign.seal import check_seal, jsonl_session_facts
from tests.conftest import make_envelope
from tests.support.mock_ingest_store import MockIngestStore
from tests.support.spool_tree import dead_endpoint

BASE_URL = "https://ingest.example.test/v1"
API_KEY = "sk-contract"


def _session(actions: int = 2) -> list[dict[str, Any]]:
    agent_id, session_id = uuid4(), uuid4()
    envelopes = [make_envelope("SESSION_OPEN", agent_id, session_id, {"objective": "invoice ACME"})]
    envelopes.append(
        make_envelope(
            "DECISION_RECORD",
            agent_id,
            session_id,
            {
                "selected_action": "send_invoice",
                "confidence": 0.9,
                "reasoning_summary": "within policy",
                "reasoning_depth": "summary",
                "timestamp": "2026-11-27T10:00:00+00:00",
            },
        )
    )
    for i in range(actions):
        envelopes.append(
            make_envelope(
                "ACTION_RECORD",
                agent_id,
                session_id,
                {
                    "tool_name": "send_invoice",
                    "input_hash": f"{i:064d}",
                    "output_hash": "b" * 64,
                    "timestamp": f"2026-11-27T10:0{i + 1}:00+00:00",
                    "authorization_status": "auto_authorized",
                },
            )
        )
    envelopes.append(
        make_envelope(
            "SESSION_CLOSE",
            agent_id,
            session_id,
            {"status": "completed", "metadata": {"total_actions": actions}},
        )
    )
    return envelopes


def _send(client: HttpIngestClient, envelopes: list[dict[str, Any]], *, before=None) -> list:
    async def _run():
        responses = []
        for index, envelope in enumerate(envelopes):
            if before is not None:
                before(index, envelope)
            responses.append(await client.handle(envelope))
        await client.close()
        return responses

    return asyncio.run(_run())


def _sent_close_seal(store: MockIngestStore, session_id: str) -> dict:
    (close,) = [
        e
        for e in store.accepted
        if e["event_type"] == "SESSION_CLOSE" and e["session_id"] == session_id
    ]
    return close["payload"]["seal"]


class TestOverTheWire:
    def test_the_close_carries_the_seal_the_store_computes(self):
        store = MockIngestStore()
        envelopes = _session()
        _send(HttpIngestClient(BASE_URL, API_KEY, transport=store.transport()), envelopes)

        session_id = envelopes[0]["session_id"]
        sent = _sent_close_seal(store, session_id)
        assert sent["final_sequence"] == 2
        assert store.seals[session_id] == sent  # agreed, so no client copy kept

    def test_a_record_the_store_refused_leaves_both_seals(self):
        """The client sealed an action the store never kept: the seals differ, both survive."""
        store = MockIngestStore()
        envelopes = _session(actions=3)

        def refuse_the_second_action(index, envelope):
            store.reject_all = (ErrorCode.VALIDATION_ERROR, False) if index == 3 else None

        responses = _send(
            HttpIngestClient(BASE_URL, API_KEY, transport=store.transport()),
            envelopes,
            before=refuse_the_second_action,
        )
        assert responses[3].status == "rejected"

        kept = store.seals[envelopes[0]["session_id"]]
        assert kept["client"]["final_sequence"] == 3
        assert kept["client"]["tail_hash"] != kept["tail_hash"] or (
            kept["client"]["side_digest"] != kept["side_digest"]
        )
        assert {k: v for k, v in kept.items() if k != "client"} != kept["client"]


def _spool_file(tmp_path: Path, session_id: str) -> Path:
    return tmp_path / "spool" / "sessions" / f"{session_id}.jsonl"


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class TestThroughTheSpool:
    def test_a_session_spooled_from_the_start_is_sealed_in_its_file(self, tmp_path):
        envelopes = _session()
        client = HttpIngestClient(
            BASE_URL,
            API_KEY,
            max_retries=1,
            transport=httpx.MockTransport(dead_endpoint),
            spool_dir=str(tmp_path / "spool"),
        )
        _send(client, envelopes)

        records = _records(_spool_file(tmp_path, envelopes[0]["session_id"]))
        close = records[-1]
        facts = jsonl_session_facts(records[:-1], status=close["payload"]["status"])
        check = check_seal(close["payload"]["seal"], facts)
        assert check.sealed, check.detail

    def test_a_mid_session_outage_seals_the_whole_session(self, tmp_path):
        """The spool file holds only the end, but the seal covers every record sent."""
        store = MockIngestStore()
        envelopes = _session(actions=3)

        def sever_after_the_first_action(index, envelope):
            if index == 3:
                store.sever()

        client = HttpIngestClient(
            BASE_URL,
            API_KEY,
            max_retries=1,
            transport=store.transport(),
            spool_dir=str(tmp_path / "spool"),
        )
        _send(client, envelopes, before=sever_after_the_first_action)

        records = _records(_spool_file(tmp_path, envelopes[0]["session_id"]))
        seal = records[-1]["payload"]["seal"]
        assert seal["final_sequence"] == 3
        last_action = [r for r in records if r.get("event_type") == "ACTION_RECORD"][-1]
        assert seal["tail_hash"] == last_action["self_hash"]

    def test_sync_sends_the_spooled_seal_and_the_store_agrees(self, tmp_path):
        """A replay sends the close as spooled; the store, now holding every record, agrees."""
        envelopes = _session()
        recorder = HttpIngestClient(
            BASE_URL,
            API_KEY,
            max_retries=1,
            transport=httpx.MockTransport(dead_endpoint),
            spool_dir=str(tmp_path / "spool"),
        )
        _send(recorder, envelopes)
        spooled = read_spool_session(_spool_file(tmp_path, envelopes[0]["session_id"])).envelopes

        store = MockIngestStore()
        replay = HttpIngestClient(
            BASE_URL,
            API_KEY,
            transport=store.transport(),
            enable_spool=False,
            seal_sessions=False,
        )
        _send(replay, spooled)

        session_id = envelopes[0]["session_id"]
        assert "client" not in store.seals[session_id]
        assert _sent_close_seal(store, session_id) == spooled[-1]["payload"]["seal"]

    def test_a_replay_never_seals_a_close_that_was_spooled_unsealed(self, tmp_path):
        envelopes = _session()
        recorder = HttpIngestClient(
            BASE_URL,
            API_KEY,
            max_retries=1,
            transport=httpx.MockTransport(dead_endpoint),
            spool_dir=str(tmp_path / "spool"),
            seal_sessions=False,
        )
        _send(recorder, envelopes)
        spooled = read_spool_session(_spool_file(tmp_path, envelopes[0]["session_id"])).envelopes
        assert "seal" not in spooled[-1]["payload"]

        store = MockIngestStore()
        _send(
            HttpIngestClient(
                BASE_URL,
                API_KEY,
                transport=store.transport(),
                enable_spool=False,
                seal_sessions=False,
            ),
            spooled,
        )
        assert "seal" not in _sent_close_seal_payload(store, envelopes[0]["session_id"])


def _sent_close_seal_payload(store: MockIngestStore, session_id: str) -> dict:
    (close,) = [
        e
        for e in store.accepted
        if e["event_type"] == "SESSION_CLOSE" and e["session_id"] == session_id
    ]
    return close["payload"]
