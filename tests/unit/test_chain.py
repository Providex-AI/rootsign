"""Unit tests for `rootsign.sdk.chain` — see Sprint 3 §3.2.

Pure-Python tests; no DB. The DB-backed path of `verify_session` is covered
by the verify CLI integration test.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from rootsign.sdk.chain import VerifyResult, verify_session_local
from rootsign.sdk.hashing import compute_payload_hash


class TestVerifyResultSummary:
    def test_valid_summary(self):
        r = VerifyResult(valid=True, record_count=47, session_id=uuid4())
        s = r.summary
        assert "VALID" in s
        assert "47" in s

    def test_tampered_summary_includes_sequence(self):
        r = VerifyResult(
            valid=False,
            record_count=10,
            session_id=uuid4(),
            first_invalid_sequence=5,
            error="self_hash mismatch",
        )
        s = r.summary
        assert "TAMPERED" in s
        assert "#5" in s
        assert "self_hash mismatch" in s


def _canonical(record: dict) -> dict:
    return {
        "action_id": record.get("action_id"),
        "session_id": record.get("session_id"),
        "tool_name": record.get("tool_name"),
        "input_hash": record.get("input_hash"),
        "output_hash": record.get("output_hash"),
        "prev_action_hash": record.get("prev_action_hash"),
        "timestamp": record.get("timestamp"),
        "sequence_number": record.get("sequence_number"),
    }


def _make_chain(n: int, session_id: str) -> list[dict]:
    """Build a canonical JSONL chain of *n* records with valid self_hashes."""
    records: list[dict] = []
    prev: str | None = None
    for i in range(1, n + 1):
        rec: dict = {
            "action_id": str(uuid4()),
            "session_id": session_id,
            "tool_name": f"tool_{i}",
            "input_hash": "a" * 64,
            "output_hash": "b" * 64,
            "prev_action_hash": prev,
            "timestamp": f"2026-05-01T00:00:0{i}+00:00",
            "sequence_number": i,
        }
        rec["self_hash"] = compute_payload_hash(_canonical(rec))
        records.append(rec)
        prev = rec["self_hash"]
    return records


def _write_jsonl(tmp_path, records: list[dict]):
    p = tmp_path / "session.jsonl"
    with open(p, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return p


class TestVerifySessionLocal:
    def test_valid_chain_returns_valid(self, tmp_path):
        session_id = str(uuid4())
        records = _make_chain(3, session_id)
        path = _write_jsonl(tmp_path, records)

        result = verify_session_local(str(path))
        assert result.valid is True
        assert result.record_count == 3
        assert result.session_id == session_id

    def test_corrupted_self_hash_detected(self, tmp_path):
        session_id = str(uuid4())
        records = _make_chain(3, session_id)
        records[1]["self_hash"] = "c" * 64
        path = _write_jsonl(tmp_path, records)

        result = verify_session_local(str(path))
        assert result.valid is False
        assert result.first_invalid_sequence == 2
        assert "self_hash" in (result.error or "")

    def test_broken_prev_hash_detected(self, tmp_path):
        session_id = str(uuid4())
        records = _make_chain(3, session_id)
        # Break the prev chain at #2 (and recompute its self_hash so the
        # self-hash check passes; the prev_action_hash check is what fires).
        records[1]["prev_action_hash"] = "d" * 64
        records[1]["self_hash"] = compute_payload_hash(_canonical(records[1]))
        path = _write_jsonl(tmp_path, records)

        result = verify_session_local(str(path))
        assert result.valid is False
        assert result.first_invalid_sequence == 2
        assert "prev_action_hash" in (result.error or "")

    def test_empty_file_returns_invalid(self, tmp_path):
        path = tmp_path / "empty.jsonl"
        path.write_text("")
        result = verify_session_local(str(path))
        assert result.valid is False
        assert result.record_count == 0

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            verify_session_local(str(tmp_path / "does_not_exist.jsonl"))

    def test_out_of_order_records_are_sorted_before_verify(self, tmp_path):
        session_id = str(uuid4())
        records = _make_chain(3, session_id)
        shuffled = [records[2], records[0], records[1]]
        path = _write_jsonl(tmp_path, shuffled)

        result = verify_session_local(str(path))
        assert result.valid is True
        assert result.record_count == 3
