"""Unit tests for `_emit_approval_record` (Sprint 4 §S4-TASK 4).

Pin three contracts:

1. **Keyword-only signature** (Flag 1) — every parameter is keyword-only
   so positional misalignment can't silently send the wrong field. The
   `test_emit_action_record_is_keyword_only` parallel for ACTION_RECORD
   already exists in the broader Sprint 3 test surface.

2. **Envelope shape** — the helper must emit an APPROVAL_RECORD envelope
   with the expected payload fields. Ingest schema validation will fail
   if any required field is missing, so we assert on what gets passed to
   `client.handle()`.

3. **Failure isolation** (ADR-002) — an ingest exception must NOT bubble
   into the caller. HiTL/CLI call sites rely on the DB write
   (`create_with_chain_link`) for correctness; this envelope is best-effort
   for the cloud backend's eventual-consistency view.
"""

from __future__ import annotations

import inspect
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from rootsign.sdk.context import SessionContext
from rootsign.sdk.decorator import _emit_approval_record


def _make_ctx() -> SessionContext:
    return SessionContext(agent_id=uuid4(), session_id=uuid4())


class TestSignature:
    def test_every_parameter_is_keyword_only(self):
        """Flag 1: positional args forbidden so the wrong UUID can't land
        in the wrong slot."""
        sig = inspect.signature(_emit_approval_record)
        non_keyword = [
            name
            for name, p in sig.parameters.items()
            if p.kind != inspect.Parameter.KEYWORD_ONLY
        ]
        assert non_keyword == [], (
            f"Parameters {non_keyword} must be keyword-only"
        )

    def test_rejects_positional_call(self):
        """Smoke: calling positionally must raise TypeError."""
        client = MagicMock()
        client.handle = AsyncMock()
        ctx = _make_ctx()
        with pytest.raises(TypeError):
            # ruff: noqa - intentional positional misuse
            _emit_approval_record(
                client, ctx, uuid4(), datetime.now(timezone.utc),  # type: ignore[misc]
                "u", "human", {}, "approved",
            )


class TestEnvelopeShape:
    async def test_emits_approval_record_event_type(self):
        client = MagicMock()
        client.handle = AsyncMock()
        ctx = _make_ctx()
        await _emit_approval_record(
            client=client,
            ctx=ctx,
            action_id=uuid4(),
            action_timestamp=datetime.now(timezone.utc),
            approver_id="user@test.com",
            approver_type="human",
            context_presented={"tool": "send_invoice"},
            decision="approved",
        )
        client.handle.assert_awaited_once()
        envelope = client.handle.await_args.args[0]
        assert envelope["event_type"] == "APPROVAL_RECORD"

    async def test_payload_includes_all_required_fields(self):
        client = MagicMock()
        client.handle = AsyncMock()
        ctx = _make_ctx()
        action_id = uuid4()
        await _emit_approval_record(
            client=client,
            ctx=ctx,
            action_id=action_id,
            action_timestamp=datetime.now(timezone.utc),
            approver_id="cli:operator",
            approver_type="human",
            context_presented={"tool_name": "send_invoice", "amount": 1500},
            decision="rejected",
            decision_reason="Looks risky",
            response_latency_ms=4200,
        )
        envelope = client.handle.await_args.args[0]
        payload = envelope["payload"]
        # Every ApprovalRecordPayload-required field is present.
        assert payload["action_id"] == str(action_id)
        assert payload["approver_id"] == "cli:operator"
        assert payload["approver_type"] == "human"
        assert payload["context_presented"] == {
            "tool_name": "send_invoice",
            "amount": 1500,
        }
        assert payload["decision"] == "rejected"
        assert payload["decision_reason"] == "Looks risky"
        assert payload["response_latency_ms"] == 4200
        assert payload["parent_approval_id"] is None
        assert "timestamp" in payload

    async def test_envelope_carries_agent_and_session_at_top_level(self):
        """ApprovalRecordPayload has no session_id field — it's in the
        envelope wrapper, where every event_type expects it."""
        client = MagicMock()
        client.handle = AsyncMock()
        ctx = _make_ctx()
        await _emit_approval_record(
            client=client,
            ctx=ctx,
            action_id=uuid4(),
            action_timestamp=datetime.now(timezone.utc),
            approver_id="user@test.com",
            approver_type="human",
            context_presented={},
            decision="approved",
        )
        envelope = client.handle.await_args.args[0]
        assert envelope["agent_id"] == str(ctx.agent_id)
        assert envelope["session_id"] == str(ctx.session_id)

    async def test_no_sequence_number_in_payload(self):
        """Flag 4: APPROVAL_RECORD is NOT in the Action chain — must not
        carry a sequence_number that would imply chain participation."""
        client = MagicMock()
        client.handle = AsyncMock()
        ctx = _make_ctx()
        await _emit_approval_record(
            client=client,
            ctx=ctx,
            action_id=uuid4(),
            action_timestamp=datetime.now(timezone.utc),
            approver_id="user@test.com",
            approver_type="human",
            context_presented={},
            decision="approved",
        )
        envelope = client.handle.await_args.args[0]
        assert "sequence_number" not in envelope["payload"]
        assert "sequence_number" not in envelope


class TestFailureIsolation:
    async def test_ingest_exception_does_not_propagate(self, caplog):
        """ADR-002: a downstream ingest failure must not raise out of this
        helper. HiTL correctness comes from the DB write
        (`create_with_chain_link`), not the envelope round-trip."""
        client = MagicMock()
        client.handle = AsyncMock(side_effect=RuntimeError("backend exploded"))
        ctx = _make_ctx()
        with caplog.at_level(logging.WARNING, logger="rootsign.sdk"):
            # Must not raise — and must return None.
            result = await _emit_approval_record(
                client=client,
                ctx=ctx,
                action_id=uuid4(),
                action_timestamp=datetime.now(timezone.utc),
                approver_id="user@test.com",
                approver_type="human",
                context_presented={},
                decision="approved",
            )
        assert result is None
        # A WARNING was logged so an operator can spot the divergence.
        assert any(
            "rootsign: _emit_approval_record failed" in rec.message
            for rec in caplog.records
        )

    async def test_timeout_envelope_carries_timeout_sentinel(self):
        """The poll loop calls this with approver_type='timeout_auto_rejected'.
        Smoke-test that path so a future refactor can't accidentally strip
        the value."""
        client = MagicMock()
        client.handle = AsyncMock()
        ctx = _make_ctx()
        await _emit_approval_record(
            client=client,
            ctx=ctx,
            action_id=uuid4(),
            action_timestamp=datetime.now(timezone.utc),
            approver_id="system:timeout",
            approver_type="timeout_auto_rejected",
            context_presented={"reason": "no human in 300s"},
            decision="rejected",
            decision_reason="No response within 300s",
        )
        payload = client.handle.await_args.args[0]["payload"]
        assert payload["approver_type"] == "timeout_auto_rejected"
        assert payload["decision"] == "rejected"
