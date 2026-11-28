"""The JSONL writer seals each session it opened (ADR-016).

The default backend writes the seal on the `SESSION_CLOSE` line, computed from
the lines it wrote for that session, read back. These tests read the file the
way the offline verifier will (`rootsign.seal.jsonl_session_facts`) and check
the seal against it: an honest session is sealed, and each edit the seal
covers shows up.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

from rootsign.sdk.jsonl_client import JsonlIngestClient
from rootsign.seal import SealOutcome, check_seal, jsonl_session_facts
from tests.conftest import make_envelope
from tests.support.docs_pages import run_script


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _check(path: Path, edit=None):
    """Check the close line's seal against the file, after an optional edit to its records."""
    records = _lines(path)
    close = records[-1]
    assert close["event_type"] == "SESSION_CLOSE"
    body = records[:-1]
    if edit is not None:
        body = edit(body)
    facts = jsonl_session_facts(body, status=close["payload"]["status"])
    return check_seal(close["payload"].get("seal"), facts)


SCRIPT = """\
import asyncio, rootsign
rootsign.init(agent="seal-writer", risk_tier="high")

@rootsign.trace()
async def pay(amount: float) -> str:
    return "paid"

async def main():
    async with rootsign.session(objective="pay two invoices") as ctx:
        await ctx.record_decision(selected_action="pay", confidence=0.9, reasoning_summary="ok")
        await pay(10.0)
        await pay(20.0)

asyncio.run(main())
"""


class TestASessionTheWriterOpened:
    def test_the_close_line_carries_a_seal_that_checks(self, tmp_path):
        run = run_script(SCRIPT, tmp_path, extra_env={"ROOTSIGN_CAPTURE_DECISIONS": "true"})
        path = run.only_session()

        check = _check(path)
        assert check.sealed, check.detail
        seal = _lines(path)[-1]["payload"]["seal"]
        assert seal["version"] == 1 and seal["final_sequence"] == 2

    def test_each_covered_edit_is_detected(self, tmp_path):
        run = run_script(SCRIPT, tmp_path, extra_env={"ROOTSIGN_CAPTURE_DECISIONS": "true"})
        path = run.only_session()

        def cut_last(records):
            return [r for r in records if r.get("sequence_number") != 2]

        def edit_objective(records):
            return [
                {**r, "payload": {**r["payload"], "objective": "pay one"}}
                if r["event_type"] == "SESSION_OPEN"
                else r
                for r in records
            ]

        def edit_decision(records):
            return [
                {**r, "payload": {**r["payload"], "reasoning_summary": "approved by the CFO"}}
                if r["event_type"] == "DECISION_RECORD"
                else r
                for r in records
            ]

        assert _check(path, cut_last).outcome is SealOutcome.MISSING_AT_END
        assert _check(path, edit_objective).outcome is SealOutcome.OPEN_EDITED
        assert _check(path, edit_decision).outcome is SealOutcome.SIDE_EDITED


def _drive(client: JsonlIngestClient, envelopes: list[dict]) -> None:
    async def _run():
        for envelope in envelopes:
            response = await client.handle(envelope)
            assert response.status == "accepted", response

    asyncio.run(_run())


def _session(close_payload: dict | None = None, *, open_=True, approval=True) -> list[dict]:
    agent_id, session_id = uuid4(), uuid4()
    envelopes = []
    if open_:
        envelopes.append(
            make_envelope("SESSION_OPEN", agent_id, session_id, {"objective": "pay supplier"})
        )
    action = {
        "tool_name": "wire_transfer",
        "input_hash": "a" * 64,
        "output_hash": None,
        "timestamp": "2026-11-27T10:00:00+00:00",
        "authorization_status": "pending",
    }
    envelopes.append(make_envelope("ACTION_RECORD", agent_id, session_id, action))
    if approval:
        envelopes.append(
            make_envelope(
                "APPROVAL_RECORD",
                agent_id,
                session_id,
                {
                    "action_id": str(uuid4()),
                    "approver_id": "tty:operator",
                    "approver_type": "human",
                    "context_presented": {"tool_name": "wire_transfer"},
                    "decision": "rejected",
                    "decision_reason": "over the mandate",
                    "timestamp": "2026-11-27T10:00:05+00:00",
                },
            )
        )
    envelopes.append(
        make_envelope(
            "SESSION_CLOSE",
            agent_id,
            session_id,
            close_payload or {"status": "completed", "metadata": {"total_actions": 1}},
        )
    )
    return envelopes


def _path(client: JsonlIngestClient, envelopes: list[dict]) -> Path:
    return client._session_path(envelopes[0]["session_id"])


class TestWhatTheWriterSeals:
    def test_an_approval_is_covered(self, tmp_path):
        client = JsonlIngestClient(data_dir=tmp_path)
        envelopes = _session()
        _drive(client, envelopes)

        def approve_instead(records):
            return [
                {**r, "payload": {**r["payload"], "decision": "approved"}}
                if r["event_type"] == "APPROVAL_RECORD"
                else r
                for r in records
            ]

        assert _check(_path(client, envelopes)).sealed
        assert _check(_path(client, envelopes), approve_instead).outcome is SealOutcome.SIDE_EDITED

    def test_the_sdks_count_is_the_length_it_seals(self, tmp_path):
        """One action written, three counted: the seal says three, and the file is short."""
        client = JsonlIngestClient(data_dir=tmp_path)
        envelopes = _session({"status": "failed", "metadata": {"total_actions": 3}})
        _drive(client, envelopes)

        check = _check(_path(client, envelopes))
        assert check.outcome is SealOutcome.MISSING_AT_END and check.missing == (2, 3)

    def test_a_close_that_arrives_sealed_keeps_its_seal(self, tmp_path):
        """A spooled cloud close: the transport sealed it over the whole session."""
        client = JsonlIngestClient(data_dir=tmp_path)
        theirs = {"version": 1, "final_sequence": 9, "seal_hash": "f" * 64}
        envelopes = _session({"status": "completed", "seal": theirs})
        _drive(client, envelopes)

        assert _lines(_path(client, envelopes))[-1]["payload"]["seal"] == theirs

    def test_a_session_it_never_saw_open_is_not_sealed(self, tmp_path):
        """Sealing part of a session would vouch for records the writer never held."""
        client = JsonlIngestClient(data_dir=tmp_path)
        envelopes = _session(open_=False)
        _drive(client, envelopes)

        assert "seal" not in _lines(_path(client, envelopes))[-1]["payload"]

    def test_as_a_spool_it_never_seals(self, tmp_path):
        client = JsonlIngestClient(data_dir=tmp_path, seal_sessions=False)
        envelopes = _session()
        _drive(client, envelopes)

        assert "seal" not in _lines(_path(client, envelopes))[-1]["payload"]

    def test_the_callers_close_payload_is_not_modified(self, tmp_path):
        """A buffered or retrying caller may still hold the envelope."""
        client = JsonlIngestClient(data_dir=tmp_path)
        envelopes = _session()
        _drive(client, envelopes)

        assert "seal" not in envelopes[-1]["payload"]
        assert "seal" in _lines(_path(client, envelopes))[-1]["payload"]
