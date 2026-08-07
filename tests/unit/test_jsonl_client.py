"""Unit tests for JsonlIngestClient + the verify_session_local updates (ADR-011).

DB-free: the JSONL backend and `verify_session_local` never touch SQLAlchemy.
Covers the chain compute, real IngestResponse, idempotency, all-event-type
append, fsync policy, and the verify-side filtering / legacy / truncation /
duplicate-sequence handling (T2.7).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from rootsign.ingest.schemas import ErrorCode
from rootsign.sdk.chain import verify_session_local
from rootsign.sdk.hashing import compute_payload_hash
from rootsign.sdk.jsonl_client import JsonlIngestClient


def _env(event_type: str, session_id, payload: dict | None = None) -> dict:
    return {
        "schema_version": "1.0",
        "sdk_version": "0.2.0",
        "event_type": event_type,
        "event_id": str(uuid4()),
        "emitted_at": datetime.now(timezone.utc).isoformat(),
        "agent_id": str(uuid4()),
        "session_id": str(session_id),
        "payload": payload or {},
    }


def _action_payload(tool: str, redacted: dict | None = None) -> dict:
    ir = redacted if redacted is not None else {"tool": tool}
    return {
        "tool_name": tool,
        "input_hash": compute_payload_hash(ir),  # so payload-binding verify passes
        "output_hash": "b" * 64,
        "input_redacted": ir,
        "output_redacted": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "authorization_status": "auto_authorized",
        "decision_id": None,
    }


async def _drive_session(client: JsonlIngestClient, session_id, tools=("a", "b", "c")):
    await client.handle(_env("SESSION_OPEN", session_id, {"objective": "x"}))
    responses = []
    for t in tools:
        responses.append(await client.handle(_env("ACTION_RECORD", session_id, _action_payload(t))))
    await client.handle(_env("SESSION_CLOSE", session_id, {"status": "completed"}))
    return responses


def _path(client: JsonlIngestClient, session_id) -> str:
    return str(client._session_path(str(session_id)))


class TestChainAndResponse:
    async def test_session_chain_verifies_valid(self, tmp_path):
        client = JsonlIngestClient(data_dir=tmp_path)
        sid = uuid4()
        await _drive_session(client, sid)
        result = verify_session_local(_path(client, sid))
        assert result.valid is True
        assert result.record_count == 3  # actions only; session events filtered

    async def test_returns_real_ingest_response(self, tmp_path):
        client = JsonlIngestClient(data_dir=tmp_path)
        sid = uuid4()
        responses = await _drive_session(client, sid, tools=("x", "y"))
        assert [r.status for r in responses] == ["accepted", "accepted"]
        assert [r.sequence_number for r in responses] == [1, 2]  # dense, from 1
        assert all(r.entity_id is not None and r.self_hash for r in responses)

    async def test_prev_hash_links_across_actions(self, tmp_path):
        client = JsonlIngestClient(data_dir=tmp_path)
        sid = uuid4()
        await _drive_session(client, sid, tools=("a", "b"))
        actions = [
            json.loads(ln)
            for ln in open(_path(client, sid))
            if json.loads(ln).get("event_type") == "ACTION_RECORD"
        ]
        assert actions[0]["prev_action_hash"] is None
        assert actions[1]["prev_action_hash"] == actions[0]["self_hash"]

    async def test_decision_record_returns_entity_id(self, tmp_path):
        # The decorator stashes response.entity_id as the pending decision_id.
        client = JsonlIngestClient(data_dir=tmp_path)
        sid = uuid4()
        await client.handle(_env("SESSION_OPEN", sid))
        r = await client.handle(_env("DECISION_RECORD", sid, {"selected_action": "go"}))
        assert r.status == "accepted"
        assert r.entity_id is not None


class TestIdempotency:
    async def test_duplicate_event_id_rejected_no_second_line(self, tmp_path):
        client = JsonlIngestClient(data_dir=tmp_path)
        sid = uuid4()
        await client.handle(_env("SESSION_OPEN", sid))
        env = _env("ACTION_RECORD", sid, _action_payload("a"))
        first = await client.handle(env)
        dup = await client.handle(env)  # same event_id
        assert first.status == "accepted"
        assert dup.status == "rejected"
        assert dup.error_code == ErrorCode.DUPLICATE_EVENT
        # Only one action line was written.
        actions = [
            ln for ln in open(_path(client, sid)) if json.loads(ln).get("event_type") == "ACTION_RECORD"
        ]
        assert len(actions) == 1


class TestFsyncPolicy:
    def test_should_fsync_matrix(self, tmp_path):
        chain = JsonlIngestClient(data_dir=tmp_path, fsync="chain")
        assert chain._should_fsync("ACTION_RECORD") is True
        assert chain._should_fsync("APPROVAL_RECORD") is True
        assert chain._should_fsync("SESSION_OPEN") is False
        always = JsonlIngestClient(data_dir=tmp_path, fsync="always")
        assert always._should_fsync("SESSION_OPEN") is True
        never = JsonlIngestClient(data_dir=tmp_path, fsync="never")
        assert never._should_fsync("ACTION_RECORD") is False

    def test_invalid_fsync_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="ROOTSIGN_JSONL_FSYNC"):
            JsonlIngestClient(data_dir=tmp_path, fsync="sometimes")


class TestTamperDetection:
    async def test_mutated_self_hash_is_tampered(self, tmp_path):
        client = JsonlIngestClient(data_dir=tmp_path)
        sid = uuid4()
        await _drive_session(client, sid, tools=("a", "b"))
        path = _path(client, sid)
        lines = open(path).read().splitlines()
        for i, ln in enumerate(lines):
            rec = json.loads(ln)
            if rec.get("event_type") == "ACTION_RECORD":
                rec["tool_name"] = "TAMPERED"  # changes canonical input, not self_hash
                lines[i] = json.dumps(rec)
                break
        open(path, "w").write("\n".join(lines) + "\n")
        result = verify_session_local(path)
        assert result.valid is False
        assert result.first_invalid_sequence == 1


class TestVerifyLocalUpdates:
    async def test_session_with_no_actions_is_valid_empty_chain(self, tmp_path):
        client = JsonlIngestClient(data_dir=tmp_path)
        sid = uuid4()
        await client.handle(_env("SESSION_OPEN", sid))
        await client.handle(_env("SESSION_CLOSE", sid, {"status": "completed"}))
        result = verify_session_local(_path(client, sid))
        assert result.valid is True
        assert result.record_count == 0

    def test_legacy_file_without_event_type_still_verifies(self, tmp_path):
        # Pre-0.2.0 store export: flat action records, no event_type field.
        from rootsign.hashing import compute_action_self_hash

        sid = str(uuid4())
        prev = None
        lines = []
        for seq in (1, 2):
            ir = {"x": seq}
            rec = {
                "session_id": sid,
                "action_id": str(uuid4()),
                "tool_name": "t",
                "input_hash": compute_payload_hash(ir),
                "output_hash": None,
                "prev_action_hash": prev,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "sequence_number": seq,
                "input_redacted": ir,
            }
            rec["self_hash"] = compute_action_self_hash(rec)
            prev = rec["self_hash"]
            lines.append(json.dumps(rec))
        p = tmp_path / "legacy.jsonl"
        p.write_text("\n".join(lines) + "\n")
        result = verify_session_local(str(p))
        assert result.valid is True
        assert result.record_count == 2

    async def test_truncated_final_line_reported(self, tmp_path):
        client = JsonlIngestClient(data_dir=tmp_path)
        sid = uuid4()
        await _drive_session(client, sid, tools=("a",))
        path = _path(client, sid)
        with open(path, "a") as f:
            f.write('{"event_type": "ACTION_RECORD", "sequ')  # crash mid-append
        result = verify_session_local(path)
        assert result.valid is False
        assert "truncated final line" in result.error

    def test_duplicate_sequence_number_tampered(self, tmp_path):
        from rootsign.hashing import compute_action_self_hash

        sid = str(uuid4())
        lines = []
        for _ in range(2):  # two records both claiming sequence_number 1
            rec = {
                "event_type": "ACTION_RECORD",
                "session_id": sid,
                "action_id": str(uuid4()),
                "tool_name": "t",
                "input_hash": "a" * 64,
                "output_hash": None,
                "prev_action_hash": None,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "sequence_number": 1,
            }
            rec["self_hash"] = compute_action_self_hash(rec)
            lines.append(json.dumps(rec))
        p = tmp_path / "dup.jsonl"
        p.write_text("\n".join(lines) + "\n")
        result = verify_session_local(str(p))
        assert result.valid is False
        assert "duplicate sequence_number" in result.error
