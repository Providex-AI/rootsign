"""`rootsign verify` checks the session seal (ADR-016 Decision 3).

Each test writes a real session through the JSONL writer, edits the file the
way an insider could, and runs the CLI and `verify_session_local`. The output
is the one the founder approved: a `Seal:` line under VALID, a TAMPERED or
INCOMPLETE headline of its own when the seal decides, and `--require-seal`
turning an unsealed session into INCOMPLETE.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from rootsign.sdk.chain import verify_session_local
from rootsign.sdk.cli import app
from rootsign.sdk.export import export_local
from rootsign.sdk.jsonl_client import JsonlIngestClient
from rootsign.sdk.report import attach_reports
from rootsign.seal import SealOutcome
from rootsign.verdict import Verdict
from tests.conftest import make_envelope

runner = CliRunner()


def _write_session(tmp_path: Path, *, actions: int = 2, seal: bool = True) -> Path:
    agent_id, session_id = uuid4(), uuid4()
    client = JsonlIngestClient(data_dir=tmp_path / "data", seal_sessions=seal)

    async def _run():
        await client.handle(
            make_envelope("SESSION_OPEN", agent_id, session_id, {"objective": "pay supplier"})
        )
        held = await client.handle(
            make_envelope(
                "ACTION_RECORD",
                agent_id,
                session_id,
                {
                    "tool_name": "wire_transfer",
                    "input_hash": "a" * 64,
                    "output_hash": None,
                    "timestamp": "2026-11-27T10:00:00+00:00",
                    "authorization_status": "pending",
                },
            )
        )
        await client.handle(
            make_envelope(
                "APPROVAL_RECORD",
                agent_id,
                session_id,
                {
                    "action_id": str(held.entity_id),
                    "approver_id": "tty:operator",
                    "approver_type": "human",
                    "context_presented": {"tool_name": "wire_transfer"},
                    "decision": "approved",
                    "decision_reason": "verified",
                },
            )
        )
        for i in range(1, actions):
            await client.handle(
                make_envelope(
                    "ACTION_RECORD",
                    agent_id,
                    session_id,
                    {
                        "tool_name": "log_payment",
                        "input_hash": f"{i:064d}",
                        "output_hash": "b" * 64,
                        "timestamp": f"2026-11-27T10:0{i}:00+00:00",
                    },
                )
            )
        await client.handle(
            make_envelope(
                "SESSION_CLOSE",
                agent_id,
                session_id,
                {"status": "completed", "metadata": {"total_actions": actions}},
            )
        )

    asyncio.run(_run())
    return client._session_path(str(session_id))


def _edit(path: Path, change) -> Path:
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    records = change(records)
    edited = path.with_name("edited.jsonl")
    edited.write_text("".join(json.dumps(r) + "\n" for r in records))
    return edited


def _verify(path: Path, *flags: str):
    return runner.invoke(app, ["verify", "--local", str(path), *flags])


def _without_last_action(records):
    last = max(r["sequence_number"] for r in records if r.get("event_type") == "ACTION_RECORD")
    return [r for r in records if r.get("sequence_number") != last]


class TestTheCli:
    def test_a_sealed_session_says_so(self, tmp_path):
        result = _verify(_write_session(tmp_path))
        assert result.exit_code == 0
        assert "VALID ✓  —  2 records, chain intact" in result.output
        assert "  Seal:     sealed at record 2" in result.output

    def test_records_cut_from_the_end_are_incomplete(self, tmp_path):
        result = _verify(_edit(_write_session(tmp_path), _without_last_action))
        assert result.exit_code == 2
        assert (
            "INCOMPLETE ⚠  —  1 records present and intact, but the chain is missing records"
            in (result.output)
        )
        assert (
            "  Detail:   1 record(s) missing at the end (sealed at 2, 1 present)" in result.output
        )
        assert "the session's seal proves records are missing from the end" in result.output

    @pytest.mark.parametrize(
        "change, detail",
        [
            (
                lambda rs: [
                    {**r, "payload": {**r["payload"], "objective": "pay someone else"}}
                    if r.get("event_type") == "SESSION_OPEN"
                    else r
                    for r in rs
                ],
                "the session open record was edited",
            ),
            (
                lambda rs: [
                    {**r, "payload": {**r["payload"], "decision": "rejected"}}
                    if r.get("event_type") == "APPROVAL_RECORD"
                    else r
                    for r in rs
                ],
                "a decision, an approval, or an action's authorization_status",
            ),
            (
                lambda rs: [
                    {**r, "payload": {**r["payload"], "status": "failed"}}
                    if r.get("event_type") == "SESSION_CLOSE"
                    else r
                    for r in rs
                ],
                "the seal or the close status was edited",
            ),
            (lambda rs: [*rs, rs[-1]], "more than one session close record"),
        ],
        ids=["objective", "approval", "close-status", "second-close"],
    )
    def test_an_edit_the_seal_covers_is_tampered(self, tmp_path, change, detail):
        result = _verify(_edit(_write_session(tmp_path), change))
        assert result.exit_code == 1
        assert "TAMPERED ✗  —  the session's seal does not match its records" in result.output
        assert detail in result.output

    def test_an_unsealed_session_keeps_its_verdict_with_a_note(self, tmp_path):
        result = _verify(_write_session(tmp_path, seal=False))
        assert result.exit_code == 0
        assert "  Seal:     none (records cut from the end would not show;" in result.output
        assert "--require-seal reports this as INCOMPLETE)" in result.output

    def test_require_seal_makes_it_incomplete(self, tmp_path):
        result = _verify(_write_session(tmp_path, seal=False), "--require-seal")
        assert result.exit_code == 2
        assert "but the session is not sealed" in result.output
        assert "  Detail:   not sealed: its end cannot be verified" in result.output

    def test_require_seal_is_quiet_about_a_sealed_session(self, tmp_path):
        result = _verify(_write_session(tmp_path), "--require-seal")
        assert result.exit_code == 0 and "sealed at record 2" in result.output

    def test_deleting_the_close_with_the_tail_is_caught_only_by_require_seal(self, tmp_path):
        """Why the flag exists: without the close there is no seal to contradict."""
        edited = _edit(
            _write_session(tmp_path),
            lambda rs: [r for r in _without_last_action(rs) if r["event_type"] != "SESSION_CLOSE"],
        )
        assert _verify(edited).exit_code == 0
        assert _verify(edited, "--require-seal").exit_code == 2


class TestTheLibrary:
    def test_a_broken_chain_is_not_downgraded_by_the_seal(self, tmp_path):
        def edit_first_action(rs):
            for r in rs:
                if r.get("sequence_number") == 1:
                    r["tool_name"] = "wire_transfer_to_me"
            return rs

        result = verify_session_local(str(_edit(_write_session(tmp_path), edit_first_action)))
        assert result.verdict is Verdict.TAMPERED
        assert result.seal is None and result.seal_present

    def test_a_gap_and_a_cut_tail_are_both_reported(self, tmp_path):
        def drop_first_and_last(rs):
            return [r for r in _without_last_action(rs) if r.get("sequence_number") != 1]

        path = _edit(_write_session(tmp_path, actions=4), drop_first_and_last)
        result = verify_session_local(str(path))
        assert result.verdict is Verdict.INCOMPLETE
        assert result.missing_ranges == [(1, 1), (4, 4)]

    def test_a_file_without_its_open_record_cannot_be_checked(self, tmp_path):
        """A spool file that starts mid-session: missing, not altered."""
        path = _edit(
            _write_session(tmp_path),
            lambda rs: [r for r in rs if r["event_type"] != "SESSION_OPEN"],
        )
        result = verify_session_local(str(path))
        assert result.verdict is Verdict.INCOMPLETE
        assert result.seal.outcome is SealOutcome.NOT_CHECKABLE

    def test_an_unknown_seal_version_is_treated_as_unsealed(self, tmp_path):
        def bump(rs):
            for r in rs:
                if r["event_type"] == "SESSION_CLOSE":
                    r["payload"]["seal"]["version"] = 2
            return rs

        path = _edit(_write_session(tmp_path), bump)
        assert verify_session_local(str(path)).verdict is Verdict.VALID
        strict = verify_session_local(str(path), require_seal=True)
        assert strict.verdict is Verdict.INCOMPLETE
        assert "seal version 2" in strict.error


class TestTheBundle:
    def test_a_sealed_session_carries_its_seal(self, tmp_path):
        bundle = attach_reports(export_local(_write_session(tmp_path)))
        assert bundle.verification["seal"] == {"outcome": "sealed", "detail": "sealed at record 2"}
        for name in ("report.md", "report.html"):
            assert (
                "Seal: sealed at record 2, so the session ends where its seal says."
                in (bundle.files()[name])
            )

    def test_an_unsealed_session_exports_as_it_did_before(self, tmp_path):
        """The key is absent, not null, so a pre-seal bundle's shape is unchanged."""
        bundle = attach_reports(export_local(_write_session(tmp_path, seal=False)))
        assert "seal" not in bundle.verification
        assert "Seal: none, so records cut from the end" in bundle.files()["report.md"]

    def test_a_cut_tail_reads_incomplete_in_the_bundle(self, tmp_path):
        bundle = export_local(_edit(_write_session(tmp_path), _without_last_action))
        assert bundle.verification["verdict"] == "INCOMPLETE"
        assert bundle.verification["seal"]["outcome"] == "missing_at_end"
