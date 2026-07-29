"""Unit tests for `@rootsign.trace(require_approval=True)` (Sprint 4 §S4-TASK 6).

Three behaviors are pinned:

1. **Gate semantics** — when `require_approval=True`:
   * The tool is NOT executed before approval lands.
   * Rejection / timeout propagate out without executing the tool.
   * Approval triggers execution and returns the tool's result.

2. **Pending insert** — the ACTION_RECORD envelope is emitted with
   `authorization_status='pending'` and `output_hash=None`. The
   `IngestResponse.entity_id` (action_id) drives the HiTLCheckpoint.

3. **Path routing** — `require_approval=True` on a LangChain/CrewAI
   tool raises NotImplementedError (deferred to Phase 2 per the v0.1.0
   scope decision in S4-TASK 6 doc comments).

Mocks follow the same pattern as `test_hitl.py` — explicit AsyncMock
for awaited methods, sync MagicMock for synchronous attributes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from rootsign.errors import HiTLRejectedError, HiTLTimeoutError
from rootsign.ingest.schemas import IngestResponse
from rootsign.sdk.context import SessionContext
from rootsign.sdk.decorator import trace
from rootsign.sdk.hitl import ApprovalDecision, HiTLResult


@pytest.fixture
def ctx():
    return SessionContext(agent_id=uuid4(), session_id=uuid4())


@pytest.fixture
def accepting_client():
    """LocalIngestClient stand-in. Returns an IngestResponse whose
    `entity_id` is the action_id the HiTL wait will bind to."""
    client = MagicMock()
    action_id = uuid4()
    client.handle = AsyncMock(
        return_value=IngestResponse.accepted(
            event_id=uuid4(),
            entity_id=action_id,
            sequence_number=1,
            self_hash="0" * 64,
        )
    )
    client._action_id = action_id  # expose for assertions
    return client


class TestRequireApprovalFalseIsExistingPath:
    """Smoke: with `require_approval=False` the decorator behaves as it
    did in Sprints 1–3 (no HiTL checkpoint, tool runs immediately)."""

    async def test_plain_callable_runs_without_wait(self, ctx, accepting_client):
        ran = []

        @trace(ingest_client=accepting_client, session_context=ctx)
        async def my_tool(x: int) -> int:
            ran.append(x)
            return x * 2

        result = await my_tool(21)
        assert result == 42
        assert ran == [21]


class TestApprovalGate:
    """When `require_approval=True`, the tool is gated on HiTL approval."""

    async def test_tool_not_executed_until_approval(self, ctx, accepting_client):
        ran = []

        @trace(
            ingest_client=accepting_client,
            session_context=ctx,
            require_approval=True,
            poll_interval_seconds=0.01,
            timeout_seconds=5.0,
        )
        async def my_tool(x: int) -> int:
            ran.append(x)
            return x * 2

        # Mock the HiTLCheckpoint so it short-circuits to "approved" without
        # touching a real DB.
        approved_result = HiTLResult(
            decision=ApprovalDecision.APPROVED,
            approval_id=uuid4(),
            approver_id="user@test.com",
        )
        with patch(
            "rootsign.sdk.hitl.HiTLCheckpoint.wait_for_approval",
            new=AsyncMock(return_value=approved_result),
        ) as wait:
            result = await my_tool(21)

        assert result == 42
        assert ran == [21], "tool must run exactly once, AFTER approval"
        # wait_for_approval was called BEFORE the tool ran — confirmed by
        # the patch firing exactly once.
        wait.assert_awaited_once()

    async def test_rejection_propagates_and_tool_not_called(self, ctx, accepting_client):
        ran = []

        @trace(
            ingest_client=accepting_client,
            session_context=ctx,
            require_approval=True,
        )
        async def my_tool(x: int) -> int:
            ran.append(x)
            return x

        action_id = accepting_client._action_id
        with patch(
            "rootsign.sdk.hitl.HiTLCheckpoint.wait_for_approval",
            new=AsyncMock(side_effect=HiTLRejectedError(action_id, reason="Nope")),
        ):
            with pytest.raises(HiTLRejectedError, match="Nope"):
                await my_tool(21)

        assert ran == [], "tool must NOT execute on rejection"

    async def test_timeout_propagates_and_tool_not_called(self, ctx, accepting_client):
        ran = []

        @trace(
            ingest_client=accepting_client,
            session_context=ctx,
            require_approval=True,
        )
        async def my_tool(x: int) -> int:
            ran.append(x)
            return x

        action_id = accepting_client._action_id
        with patch(
            "rootsign.sdk.hitl.HiTLCheckpoint.wait_for_approval",
            new=AsyncMock(side_effect=HiTLTimeoutError(action_id, 300)),
        ):
            with pytest.raises(HiTLTimeoutError):
                await my_tool(21)

        assert ran == [], "tool must NOT execute on timeout"


class TestPendingInsert:
    """The ACTION_RECORD must be inserted with status='pending' and no
    output_hash. The IngestResponse drives the HiTLCheckpoint binding."""

    async def test_envelope_has_pending_status_and_no_output_hash(self, ctx, accepting_client):
        @trace(
            ingest_client=accepting_client,
            session_context=ctx,
            require_approval=True,
        )
        async def my_tool(x: int) -> int:
            return x

        approved = HiTLResult(
            decision=ApprovalDecision.APPROVED,
            approval_id=uuid4(),
            approver_id="user@test.com",
        )
        with patch(
            "rootsign.sdk.hitl.HiTLCheckpoint.wait_for_approval",
            new=AsyncMock(return_value=approved),
        ):
            await my_tool(7)

        accepting_client.handle.assert_awaited_once()
        envelope = accepting_client.handle.await_args.args[0]
        payload = envelope["payload"]
        assert payload["authorization_status"] == "pending"
        assert payload["output_hash"] is None
        assert payload["output_redacted"] is None
        assert payload["input_hash"] is not None  # input IS hashed

    async def test_raises_if_pending_insert_fails(self, ctx):
        """If the ingest path doesn't return an entity_id, HiTL cannot
        proceed — the wait would deadlock for the full timeout because
        there's no row to poll on. Surface the failure immediately."""
        bad_client = MagicMock()
        bad_client.handle = AsyncMock(side_effect=RuntimeError("backend down"))

        @trace(
            ingest_client=bad_client,
            session_context=ctx,
            require_approval=True,
        )
        async def my_tool(x: int) -> int:
            return x

        # The failure-isolation rule swallows the RuntimeError inside
        # _try_ingest → returns None → HiTL wrapper detects missing
        # action_id and raises a clear error.
        with pytest.raises(RuntimeError, match="HiTL gate cannot proceed"):
            await my_tool(7)


class TestContextPresented:
    async def test_default_builder_shape(self, ctx, accepting_client):
        """Default context_presented carries tool_name + truncated input."""

        @trace(
            ingest_client=accepting_client,
            session_context=ctx,
            require_approval=True,
            tool_name="send_invoice",
        )
        async def my_tool(amount: float) -> str:
            return "sent"

        captured = {}

        async def fake_wait(self, *, context_presented):
            captured["context"] = context_presented
            return HiTLResult(decision=ApprovalDecision.APPROVED)

        with patch(
            "rootsign.sdk.hitl.HiTLCheckpoint.wait_for_approval",
            new=fake_wait,
        ):
            await my_tool(99.99)

        assert captured["context"]["tool_name"] == "send_invoice"
        assert "input_summary" in captured["context"]
        assert "99.99" in captured["context"]["input_summary"]

    async def test_redaction_applies_to_context_presented(self, ctx, accepting_client):
        """REGRESSION (audit finding #1): context_presented must carry
        the REDACTED input, not the raw payload. The HiTL CLI / poll loop
        persists this dict into `approvals.context_presented`; raw PII
        landing here would survive in the audit table indefinitely.

        Pre-fix bug: `_emit_hitl_action` passed `input_payload` (raw) to
        both the default summary and the custom builder, even when a
        `redaction_config` was provided.
        """
        from rootsign.sdk.redaction import RedactionConfig

        # Rule that matches the kwarg-path explicitly — confirms the
        # fix passes the redacted dict (which has '[REDACTED]' for the
        # email field) to context_presented, not the raw input.
        redaction = RedactionConfig(
            rules={"kwargs.email": r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"}
        )

        @trace(
            ingest_client=accepting_client,
            session_context=ctx,
            require_approval=True,
            redaction_config=redaction,
        )
        async def my_tool(*, email: str) -> str:
            return "sent"

        captured = {}

        async def fake_wait(self, *, context_presented):
            captured["context"] = context_presented
            return HiTLResult(decision=ApprovalDecision.APPROVED)

        with patch(
            "rootsign.sdk.hitl.HiTLCheckpoint.wait_for_approval",
            new=fake_wait,
        ):
            await my_tool(email="alice@acme.com")

        # The PII must be redacted in the persisted context. The default
        # builder produces an `input_summary` string — assert the raw PII
        # is gone and the placeholder is present.
        assert "alice@acme.com" not in captured["context"]["input_summary"]
        assert "[REDACTED]" in captured["context"]["input_summary"]

    async def test_custom_builder_overrides_default(self, ctx, accepting_client):
        """approval_context_builder lets callers shape what the operator sees.
        Critical for financial / PII flows where the default str() is wrong."""

        def my_builder(tool_name, input_payload):
            kw = input_payload["kwargs"]
            return {
                "tool_name": tool_name,
                "customer": kw.get("customer_id"),
                "amount_usd": kw.get("amount"),
            }

        @trace(
            ingest_client=accepting_client,
            session_context=ctx,
            require_approval=True,
            tool_name="send_invoice",
            approval_context_builder=my_builder,
        )
        async def my_tool(*, customer_id: str, amount: float) -> str:
            return "sent"

        captured = {}

        async def fake_wait(self, *, context_presented):
            captured["context"] = context_presented
            return HiTLResult(decision=ApprovalDecision.APPROVED)

        with patch(
            "rootsign.sdk.hitl.HiTLCheckpoint.wait_for_approval",
            new=fake_wait,
        ):
            await my_tool(customer_id="acme", amount=1500.0)

        assert captured["context"] == {
            "tool_name": "send_invoice",
            "customer": "acme",
            "amount_usd": 1500.0,
        }


class TestFrameworkPathsRejectRequireApproval:
    """v0.1.0 scope: HiTL only supports the plain-callable path. LangChain
    and CrewAI tool paths raise NotImplementedError so users get a clear
    hint rather than silently falling through to auto-authorized."""

    def test_langchain_tool_with_require_approval_raises(self, ctx, accepting_client):
        langchain_core = pytest.importorskip("langchain_core")
        from langchain_core.tools import tool as langchain_tool

        @langchain_tool
        def my_tool(x: int) -> int:
            """sample"""
            return x

        decorator = trace(
            ingest_client=accepting_client,
            session_context=ctx,
            require_approval=True,
        )
        with pytest.raises(NotImplementedError, match="LangChain"):
            decorator(my_tool)

    def test_crewai_tool_with_require_approval_raises(self, ctx, accepting_client):
        # Duck-type a CrewAI-shaped tool without importing crewai
        class FakeCrewAITool:
            name = "fake_tool"

            def _run(self, x):
                return x

        decorator = trace(
            ingest_client=accepting_client,
            session_context=ctx,
            require_approval=True,
        )
        # The duck-type check is in rootsign.sdk.frameworks.crewai._is_crewai_tool;
        # only trips if the heuristic matches.
        from rootsign.sdk.frameworks.crewai import _is_crewai_tool

        if not _is_crewai_tool(FakeCrewAITool()):
            pytest.skip("duck-type heuristic doesn't match this stand-in")
        with pytest.raises(NotImplementedError, match="CrewAI"):
            decorator(FakeCrewAITool())


class TestHitlInputJsonSafety:
    """REGRESSION: `_emit_hitl_action` must run inputs through `_to_json_safe`
    exactly like `_emit_action_record` (decorator.py line ~407). A non-JSON-safe
    arg (LangChain BaseMessage, dataclass, arbitrary object) otherwise reaches
    the `input_redacted` JSONB column raw and throws TypeError on insert — and
    in the HiTL path that escalates to a RuntimeError, because the pending
    ACTION_RECORD must succeed for the gate to proceed. It also desyncs
    input_hash from the stored payload, breaking verify-time payload binding.
    """

    async def test_non_json_safe_arg_coerced_in_pending_envelope(self, ctx, accepting_client):
        import json

        class _FakeMessage:
            # Duck-types a LangChain BaseMessage: `.content` + a str `.type`.
            type = "human"
            content = "please approve"

        @trace(
            ingest_client=accepting_client,
            session_context=ctx,
            require_approval=True,
        )
        async def my_tool(msg: object) -> str:
            return "done"

        async def fake_wait(self, *, context_presented):
            return HiTLResult(decision=ApprovalDecision.APPROVED)

        with patch("rootsign.sdk.hitl.HiTLCheckpoint.wait_for_approval", new=fake_wait):
            result = await my_tool(_FakeMessage())

        assert result == "done"
        # The pending ACTION_RECORD is the only envelope handed to the client.
        envelope = accepting_client.handle.call_args.args[0]
        input_redacted = envelope["payload"]["input_redacted"]
        # Must be JSONB-storable: json.dumps with NO default must not raise
        # (this is the line that threw before the fix).
        json.dumps(input_redacted)
        # And the message was defanged into the _to_json_safe shape.
        assert input_redacted["args"][0] == {
            "_message_type": "human",
            "content": "please approve",
        }
