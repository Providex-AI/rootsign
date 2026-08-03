"""S5-TASK 7 — `_input_payload_override` on the emit helpers (ADR-010).

The MCP proxy pre-builds a redacted `arguments` dict and hands it to the emit
helpers via this kwarg, instead of the usual `(args, kwargs)` capture. These
tests pin the contract: the kwarg is keyword-only on BOTH helpers, and when
provided it becomes the stored `input_redacted` verbatim and drives
`input_hash` (ADR-006 redact-before-hash).
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

from rootsign.sdk.context import SessionContext
from rootsign.sdk.decorator import _emit_action_record, _emit_hitl_action
from rootsign.sdk.hashing import compute_payload_hash

AGENT_ID = UUID("550e8400-e29b-41d4-a716-446655440000")


class TestOverrideSignature:
    def test_kwarg_is_keyword_only_on_both_helpers(self):
        for fn in (_emit_action_record, _emit_hitl_action):
            params = inspect.signature(fn).parameters
            assert "_input_payload_override" in params, fn.__name__
            p = params["_input_payload_override"]
            assert p.kind == inspect.Parameter.KEYWORD_ONLY, fn.__name__
            assert p.default is None, fn.__name__


class TestOverrideBehavior:
    async def test_override_is_stored_and_hashed_verbatim(self):
        client = AsyncMock()
        client.handle.return_value = MagicMock(
            status="accepted", entity_id=None, sequence_number=1
        )
        ctx = SessionContext(agent_id=AGENT_ID)
        override = {"to": "[REDACTED]", "subject": "Invoice"}

        async def forward(*_a, **_k):
            return {"ok": True}

        await _emit_action_record(
            func=forward,
            args=(),
            kwargs={},
            tool_name="send_email",
            client=client,
            ctx=ctx,
            redaction_config=None,
            _input_payload_override=override,
        )

        payload = client.handle.call_args[0][0]["payload"]
        # Stored verbatim — no {"args", "kwargs"} wrapper.
        assert payload["input_redacted"] == override
        # Hash computed over the override itself.
        assert payload["input_hash"] == compute_payload_hash(override)

    async def test_none_override_falls_back_to_args_kwargs_capture(self):
        client = AsyncMock()
        client.handle.return_value = MagicMock(
            status="accepted", entity_id=None, sequence_number=1
        )
        ctx = SessionContext(agent_id=AGENT_ID)

        async def forward(*_a, **_k):
            return {"ok": True}

        await _emit_action_record(
            func=forward,
            args=(1,),
            kwargs={"b": 2},
            tool_name="add",
            client=client,
            ctx=ctx,
            redaction_config=None,
        )

        payload = client.handle.call_args[0][0]["payload"]
        # Default path keeps the args/kwargs envelope shape.
        assert payload["input_redacted"] == {"args": [1], "kwargs": {"b": 2}}
