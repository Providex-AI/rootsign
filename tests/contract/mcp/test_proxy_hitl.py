"""HiTL-over-MCP contract tests (ADR-010 + ADR-007).

Proves the MCP proxy's `require_approval=True` path: a `tools/call` is gated on
human approval and the upstream MCP server is NOT contacted until the approval
lands. On rejection the upstream is never contacted and the error propagates.

Design note — why this mocks `HiTLCheckpoint.wait_for_approval` rather than
running a real poll loop + `rootsign approve` via `asyncio.to_thread` (as the
sprint DoD phrases it): NO test in this repo drives the real poll loop against
the DB — every HiTL test (`test_trace_hitl.py`, `test_hitl.py`) mocks the
checkpoint or its session factory, a deliberate choice to avoid the issue-#3
cross-loop asyncpg flake. The CLI-approve mechanics (status flip + Approval
row) are already covered end-to-end by `test_approve_cli.py`. The MCP-specific
new behavior is only "route `require_approval` → `_emit_hitl_action` and pause
before forwarding" — which the checkpoint mock pins deterministically. Same
approach as `test_trace_hitl.py`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

pytest.importorskip("httpx", reason="httpx not installed")

from rootsign.errors import HiTLRejectedError  # noqa: E402
from rootsign.ingest.schemas import IngestResponse  # noqa: E402
from rootsign.mcp.proxy import MCPProxyTracer  # noqa: E402
from rootsign.sdk.context import SessionContext  # noqa: E402
from rootsign.sdk.hitl import ApprovalDecision, HiTLResult  # noqa: E402

UPSTREAM = {
    "jsonrpc": "2.0",
    "result": {"content": [{"type": "text", "text": "done"}], "isError": False},
    "id": 1,
}

TOOLS_CALL = {
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": {"name": "wire_funds", "arguments": {"amount": 5000, "to_account": "ACME"}},
    "id": 1,
}


def _accepting_client():
    """Mock IngestClient whose pending-insert returns a real action_id so the
    HiTL wait has something to bind to (mirrors test_trace_hitl.py)."""
    client = MagicMock()
    action_id = uuid4()
    client.handle = AsyncMock(
        return_value=IngestResponse.accepted(
            event_id=uuid4(), entity_id=action_id, sequence_number=1, self_hash="0" * 64
        )
    )
    client._action_id = action_id
    return client


def _mock_upstream():
    """Patch the lazily-imported httpx.AsyncClient; return (patch_cm, post_mock)
    so tests can inspect whether/when the upstream was actually contacted."""
    post = AsyncMock(
        return_value=MagicMock(
            json=lambda: UPSTREAM, raise_for_status=lambda: None, status_code=200
        )
    )
    cm = AsyncMock()
    cm.__aenter__.return_value.post = post
    cm.__aexit__ = AsyncMock(return_value=None)
    return patch("httpx.AsyncClient", return_value=cm), post


class TestHiTLOverMCP:
    async def test_pauses_before_forwarding_then_forwards_on_approval(self):
        client = _accepting_client()
        ctx = SessionContext(agent_id=uuid4())
        upstream_patch, post = _mock_upstream()

        # The wait mock fires BEFORE the upstream forward in _emit_hitl_action,
        # so at wait time the upstream must not have been contacted yet.
        async def wait_side_effect(*_a, **_k):
            assert post.call_count == 0, "upstream forwarded BEFORE approval — gate leaked"
            return HiTLResult(
                decision=ApprovalDecision.APPROVED, approval_id=uuid4(), approver_id="op@test"
            )

        with upstream_patch, patch(
            "rootsign.sdk.hitl.HiTLCheckpoint.wait_for_approval",
            new=AsyncMock(side_effect=wait_side_effect),
        ) as wait:
            result = await MCPProxyTracer.intercept_tools_call(
                request_body=TOOLS_CALL,
                upstream_url="http://mock/mcp",
                ctx=ctx,
                client=client,
                require_approval=True,
                poll_interval_seconds=0.01,
                timeout_seconds=5.0,
            )

        wait.assert_awaited_once()
        # Forwarded exactly once, AFTER approval, and returns the upstream body.
        assert post.call_count == 1
        assert result == UPSTREAM

    async def test_rejection_propagates_and_upstream_never_contacted(self):
        client = _accepting_client()
        ctx = SessionContext(agent_id=uuid4())
        upstream_patch, post = _mock_upstream()

        with upstream_patch, patch(
            "rootsign.sdk.hitl.HiTLCheckpoint.wait_for_approval",
            new=AsyncMock(side_effect=HiTLRejectedError(client._action_id, reason="Too risky")),
        ):
            with pytest.raises(HiTLRejectedError, match="Too risky"):
                await MCPProxyTracer.intercept_tools_call(
                    request_body=TOOLS_CALL,
                    upstream_url="http://mock/mcp",
                    ctx=ctx,
                    client=client,
                    require_approval=True,
                )

        assert post.call_count == 0, "rejected call must never reach upstream"

    async def test_pending_action_recorded_with_faithful_mcp_input_shape(self):
        """The gated ACTION_RECORD is emitted 'pending' with output_hash=None,
        and its input_redacted is the faithful MCP arguments (override path),
        not a synthetic args/kwargs wrapper."""
        client = _accepting_client()
        ctx = SessionContext(agent_id=uuid4())
        upstream_patch, _post = _mock_upstream()

        with upstream_patch, patch(
            "rootsign.sdk.hitl.HiTLCheckpoint.wait_for_approval",
            new=AsyncMock(
                return_value=HiTLResult(
                    decision=ApprovalDecision.APPROVED, approval_id=uuid4(), approver_id="op@test"
                )
            ),
        ):
            await MCPProxyTracer.intercept_tools_call(
                request_body=TOOLS_CALL,
                upstream_url="http://mock/mcp",
                ctx=ctx,
                client=client,
                require_approval=True,
            )

        # The single client.handle call is the pending ACTION_RECORD insert.
        payload = client.handle.call_args[0][0]["payload"]
        assert payload["authorization_status"] == "pending"
        assert payload["output_hash"] is None
        # Faithful shape — the MCP arguments, not {"args": [...], "kwargs": {...}}.
        assert payload["input_redacted"] == {"amount": 5000, "to_account": "ACME"}
