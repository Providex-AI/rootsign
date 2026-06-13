"""Unit tests for PRD-19 T6 — decision_id plumbing through _emit_action_record.

The HiTL path's wiring is exercised by the integration suite (T7) because
testing _emit_hitl_action in isolation requires mocking the poll loop +
HiTLCheckpoint — overkill for this scope. The unit test here covers the
auto-authorized path: consume happens, decision_id ends up in the payload,
slot is cleared.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

from rootsign.sdk.context import SessionContext
from rootsign.sdk.decorator import _emit_action_record


AGENT_ID = UUID("550e8400-e29b-41d4-a716-446655440000")


def _mock_client():
    client = AsyncMock()
    client.handle.return_value = MagicMock(entity_id=uuid4(), status="accepted")
    return client


class TestDecisionIdPlumbing:
    async def test_action_payload_includes_pending_decision_id(self):
        ctx = SessionContext(agent_id=AGENT_ID)
        pending = uuid4()
        ctx._pending_decision_id = pending
        client = _mock_client()

        async def noop(*a, **kw):
            return "ok"

        await _emit_action_record(
            func=noop,
            args=(),
            kwargs={},
            tool_name="test_tool",
            client=client,
            ctx=ctx,
            redaction_config=None,
        )

        envelope = client.handle.call_args[0][0]
        assert envelope["payload"]["decision_id"] == str(pending)

    async def test_pending_slot_cleared_after_action(self):
        ctx = SessionContext(agent_id=AGENT_ID)
        ctx._pending_decision_id = uuid4()
        client = _mock_client()

        async def noop(*a, **kw):
            return "ok"

        await _emit_action_record(
            func=noop,
            args=(),
            kwargs={},
            tool_name="test_tool",
            client=client,
            ctx=ctx,
            redaction_config=None,
        )

        assert ctx._pending_decision_id is None

    async def test_no_pending_decision_id_means_null_in_payload(self):
        ctx = SessionContext(agent_id=AGENT_ID)
        assert ctx._pending_decision_id is None
        client = _mock_client()

        async def noop(*a, **kw):
            return "ok"

        await _emit_action_record(
            func=noop,
            args=(),
            kwargs={},
            tool_name="test_tool",
            client=client,
            ctx=ctx,
            redaction_config=None,
        )

        envelope = client.handle.call_args[0][0]
        assert envelope["payload"]["decision_id"] is None

    async def test_consume_happens_even_when_tool_raises(self):
        """The decision slot is spent the moment the action attempts to run."""
        ctx = SessionContext(agent_id=AGENT_ID)
        ctx._pending_decision_id = uuid4()
        client = _mock_client()

        async def boom(*a, **kw):
            raise RuntimeError("tool failed")

        try:
            await _emit_action_record(
                func=boom,
                args=(),
                kwargs={},
                tool_name="test_tool",
                client=client,
                ctx=ctx,
                redaction_config=None,
            )
        except RuntimeError:
            pass

        # Slot is cleared regardless of tool outcome
        assert ctx._pending_decision_id is None
        # The (failed) ACTION_RECORD still carries decision_id
        envelope = client.handle.call_args[0][0]
        assert envelope["payload"]["decision_id"] is not None
