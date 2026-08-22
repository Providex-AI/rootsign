"""Unit tests for `rootsign.sdk.chain` — see Sprint 3 §3.2.

Pure-Python tests; no DB. The DB-backed path of `verify_session` is covered
by the verify CLI integration test.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

# Fixtures pin self_hash with the FROZEN canonical function
# (`rootsign.hashing.compute_action_self_hash`) — NOT
# `rootsign.sdk.hashing.compute_payload_hash`. The previous version of
# this file used compute_payload_hash, which silently let the local
# verifier drift from the store-side formula (audit finding #8). The
# rule: a verifier test fixture must be built with the same FROZEN
# function the store uses — see memory
# `feedback_canonical_hash_never_reimplemented`.
from rootsign.hashing import compute_action_self_hash
from rootsign.sdk.chain import VerifyResult, verify_session_local
from rootsign.verdict import Verdict


class TestVerifyResultSummary:
    def test_valid_summary(self):
        r = VerifyResult(verdict=Verdict.VALID, record_count=47, session_id=uuid4())
        s = r.summary
        assert "VALID" in s
        assert "47" in s

    def test_tampered_summary_includes_sequence(self):
        r = VerifyResult(
            verdict=Verdict.TAMPERED,
            record_count=10,
            session_id=uuid4(),
            first_invalid_sequence=5,
            error="self_hash mismatch",
        )
        s = r.summary
        assert "TAMPERED" in s
        assert "#5" in s
        assert "self_hash mismatch" in s


def _make_chain(n: int, session_id: str) -> list[dict]:
    """Build a canonical JSONL chain of *n* records with valid self_hashes.

    Record #1 deliberately has `prev_action_hash=None` to exercise the
    None-coercion path in `compute_action_self_hash` — this is the
    exact shape that any genuine store export produces and was the
    case that broke local verify pre-audit-fix.
    """
    records: list[dict] = []
    prev: str | None = None
    for i in range(1, n + 1):
        rec: dict = {
            "action_id": str(uuid4()),
            "session_id": session_id,
            "tool_name": f"tool_{i}",
            "input_hash": "a" * 64,
            "output_hash": "b" * 64,
            "prev_action_hash": prev,  # None on record #1
            "timestamp": f"2026-05-01T00:00:0{i}+00:00",
            "sequence_number": i,
        }
        # FROZEN canonical function — same one the store uses.
        rec["self_hash"] = compute_action_self_hash(rec)
        records.append(rec)
        prev = rec["self_hash"]
    return records


def _write_jsonl(tmp_path, records: list[dict]):
    p = tmp_path / "session.jsonl"
    with open(p, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return p


class TestIncompleteVerdict:
    """Gaps are their own verdict now (ADR-013 Decision 4b, T2.4b).

    Before this, a missing record surfaced as TAMPERED — record N+1's
    `prev_action_hash` names a record that is not there, which is
    indistinguishable from an alteration unless you look at the sequence set.
    """

    def test_a_missing_record_is_incomplete_not_tampered(self, tmp_path):
        session_id = str(uuid4())
        records = _make_chain(4, session_id)
        del records[1]  # sequence 2 never made it to disk
        path = _write_jsonl(tmp_path, records)

        result = verify_session_local(str(path))

        assert result.verdict is Verdict.INCOMPLETE
        assert result.valid is False  # the property still reads false
        assert result.missing_ranges == [(2, 2)]
        assert result.record_count == 3
        assert "missing" in (result.error or "")

    def test_a_run_of_lost_records_reports_one_range(self, tmp_path):
        session_id = str(uuid4())
        records = _make_chain(6, session_id)
        del records[1:4]  # sequences 2, 3, 4 — a spool that was down a while
        path = _write_jsonl(tmp_path, records)

        result = verify_session_local(str(path))

        assert result.verdict is Verdict.INCOMPLETE
        assert result.missing_ranges == [(2, 4)]
        assert "2-4" in result.summary

    def test_a_missing_first_record_is_incomplete(self, tmp_path):
        session_id = str(uuid4())
        records = _make_chain(3, session_id)
        del records[0]
        path = _write_jsonl(tmp_path, records)

        result = verify_session_local(str(path))

        assert result.verdict is Verdict.INCOMPLETE
        assert result.missing_ranges == [(1, 1)]

    def test_tampering_after_a_gap_is_still_caught(self, tmp_path):
        """The reason a gap-explained break re-anchors instead of returning.

        If the verifier stopped at the gap, one dropped record would mask every
        alteration after it — a cheap way to launder a rewritten log.
        """
        session_id = str(uuid4())
        records = _make_chain(5, session_id)
        del records[1]  # gap at sequence 2
        records[2]["tool_name"] = "ATTACKER_REWROTE_THIS"  # sequence 4, hashed field
        path = _write_jsonl(tmp_path, records)

        result = verify_session_local(str(path))

        assert result.verdict is Verdict.TAMPERED  # worst verdict wins
        assert result.first_invalid_sequence == 4
        assert result.missing_ranges == [(2, 2)]  # ...and the gap is still reported
        assert "also missing sequence 2" in result.summary

    def test_a_clean_chain_is_still_valid(self, tmp_path):
        session_id = str(uuid4())
        path = _write_jsonl(tmp_path, _make_chain(3, session_id))

        result = verify_session_local(str(path))

        assert result.verdict is Verdict.VALID
        assert result.valid is True
        assert result.missing_ranges == []

    def test_an_alteration_without_gaps_is_tampered(self, tmp_path):
        session_id = str(uuid4())
        records = _make_chain(3, session_id)
        records[1]["self_hash"] = "c" * 64
        path = _write_jsonl(tmp_path, records)

        result = verify_session_local(str(path))

        assert result.verdict is Verdict.TAMPERED
        assert result.missing_ranges == []

    def test_duplicate_sequences_stay_tampered(self, tmp_path):
        session_id = str(uuid4())
        records = _make_chain(3, session_id)
        records[2]["sequence_number"] = 2
        path = _write_jsonl(tmp_path, records)

        result = verify_session_local(str(path))

        assert result.verdict is Verdict.TAMPERED
        assert "duplicate" in (result.error or "")

    def test_the_incomplete_summary_names_the_range_and_the_count(self, tmp_path):
        session_id = str(uuid4())
        records = _make_chain(5, session_id)
        del records[1:3]
        path = _write_jsonl(tmp_path, records)

        summary = verify_session_local(str(path)).summary

        assert summary.startswith("INCOMPLETE")
        assert "2 record(s) missing" in summary
        assert "2-3" in summary
        assert "3 present and intact" in summary


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
        records[1]["self_hash"] = compute_action_self_hash(records[1])
        path = _write_jsonl(tmp_path, records)

        result = verify_session_local(str(path))
        assert result.valid is False
        assert result.first_invalid_sequence == 2
        assert "prev_action_hash" in (result.error or "")

    def test_record1_with_null_prev_verifies(self, tmp_path):
        """REGRESSION (audit finding #8): record #1 of any genuine store
        export has `prev_action_hash = NULL`. The pre-fix verifier used
        `compute_payload_hash` which serialized None as JSON `null`,
        while the store-side `compute_action_self_hash` coerces None to
        the empty string `""`. The two hashes differed and `rootsign
        verify --local` returned TAMPERED on real exports at record #1.

        This test pins that record #1 with `prev_action_hash=None` MUST
        verify cleanly — and as a belt-and-braces check, asserts that
        the self_hash actually encodes the None→"" coercion rather than
        JSON null. Memory:
        `feedback_canonical_hash_never_reimplemented`.
        """
        session_id = str(uuid4())
        records = _make_chain(1, session_id)
        assert records[0]["prev_action_hash"] is None, (
            "fixture invariant: record #1 must have prev=None for this regression"
        )
        # The fixture was built with compute_action_self_hash, so verify
        # must agree.
        path = _write_jsonl(tmp_path, records)
        result = verify_session_local(str(path))
        assert result.valid is True
        # Belt-and-braces: directly recompute with the FROZEN canonical
        # function and confirm it matches what verify accepts.
        assert records[0]["self_hash"] == compute_action_self_hash(records[0])

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
