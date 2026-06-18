"""Coverage for `_to_json_safe` and the LangGraph ToolCall integration path.

Why this exists: before v0.1.2, `_emit_action_record` stored the raw tool
return into `output_payload = {"result": result}` without coercion. When
LangGraph's ToolNode invoked a tool via the LangChain ToolCall envelope,
BaseTool wrapped the return into a `ToolMessage`, which then tripped
JSONB serialization at INSERT time and the ACTION_RECORD never landed.

These tests lock in the fix:
  * `_to_json_safe` handles primitives, dicts, lists, BaseMessage
    duck-types, and arbitrary objects deterministically.
  * Calling `_emit_action_record` with a fake BaseMessage-shaped return
    succeeds (no exception) and the captured payload is JSON-clean.

No real LangChain dependency — tests build a minimal duck-typed shim so
they run with or without the `[langgraph]` extra installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest

from rootsign.sdk.context import SessionContext
from rootsign.sdk.decorator import _emit_action_record, _to_json_safe


class _FakeMessage:
    """Duck-types LangChain BaseMessage: has `.content` and `.type`."""

    def __init__(self, content: str, msg_type: str = "tool") -> None:
        self.content = content
        self.type = msg_type
        self.tool_call_id = "call_should_be_dropped"


@dataclass
class _Arbitrary:
    a: int
    b: str


class TestToJsonSafePrimitives:
    @pytest.mark.parametrize(
        "value",
        [None, "x", 1, 1.5, True, False],
    )
    def test_primitives_pass_through(self, value: Any) -> None:
        assert _to_json_safe(value) == value

    def test_empty_collections(self) -> None:
        assert _to_json_safe({}) == {}
        assert _to_json_safe([]) == []
        assert _to_json_safe(()) == []  # tuple → list parity


class TestToJsonSafeNested:
    def test_nested_dict_recurses(self) -> None:
        out = _to_json_safe({"a": {"b": {"c": "d"}}})
        assert out == {"a": {"b": {"c": "d"}}}

    def test_dict_with_non_string_keys_are_str_coerced(self) -> None:
        out = _to_json_safe({1: "one", 2: "two"})
        assert out == {"1": "one", "2": "two"}

    def test_list_recurses(self) -> None:
        out = _to_json_safe([1, "two", {"three": 3}])
        assert out == [1, "two", {"three": 3}]


class TestToJsonSafeBaseMessage:
    def test_tool_message_yields_type_and_content(self) -> None:
        msg = _FakeMessage(content="hello", msg_type="tool")
        out = _to_json_safe(msg)
        assert out == {"_message_type": "tool", "content": "hello"}

    def test_ai_message_uses_its_type(self) -> None:
        msg = _FakeMessage(content="thinking...", msg_type="ai")
        out = _to_json_safe(msg)
        assert out == {"_message_type": "ai", "content": "thinking..."}

    def test_private_fields_are_dropped(self) -> None:
        msg = _FakeMessage(content="hi", msg_type="tool")
        out = _to_json_safe(msg)
        assert "tool_call_id" not in out

    def test_nested_message_in_dict(self) -> None:
        payload = {"result": _FakeMessage(content="ok", msg_type="tool")}
        out = _to_json_safe(payload)
        assert out == {"result": {"_message_type": "tool", "content": "ok"}}

    def test_recursive_content_normalization(self) -> None:
        inner = _FakeMessage(content="inner", msg_type="ai")
        outer = _FakeMessage(content=inner, msg_type="tool")
        out = _to_json_safe(outer)
        assert out == {
            "_message_type": "tool",
            "content": {"_message_type": "ai", "content": "inner"},
        }


class TestToJsonSafeFallback:
    def test_arbitrary_object_falls_back_to_str(self) -> None:
        obj = _Arbitrary(a=1, b="x")
        out = _to_json_safe(obj)
        assert isinstance(out, str)
        assert "Arbitrary" in out

    def test_object_with_content_but_non_string_type_falls_back(self) -> None:
        class _NotAMessage:
            content = "looks like one"
            type = 42  # not a string — not a BaseMessage

        out = _to_json_safe(_NotAMessage())
        assert isinstance(out, str)


class TestToJsonSafeDeterminism:
    def test_equal_inputs_produce_equal_outputs(self) -> None:
        a = {"args": [{"name": "send_invoice", "args": {"x": 1}}], "kwargs": {}}
        b = {"args": [{"name": "send_invoice", "args": {"x": 1}}], "kwargs": {}}
        assert _to_json_safe(a) == _to_json_safe(b)

    def test_message_outputs_hash_stably(self) -> None:
        m1 = _FakeMessage(content="x", msg_type="tool")
        m2 = _FakeMessage(content="x", msg_type="tool")
        assert _to_json_safe(m1) == _to_json_safe(m2)


class TestEmitActionRecordToolCallPath:
    """End-to-end shape check: the documented LangGraph ToolCall flow now
    flows through without raising, and the captured payload is JSON-clean.

    Doesn't hit the DB — uses a stub IngestClient so we exercise just the
    payload-construction half of `_emit_action_record`.
    """

    async def test_tool_call_envelope_with_tool_message_return(self) -> None:
        captured: dict[str, Any] = {}

        class _StubClient:
            async def handle(self, envelope: Any) -> Any:
                captured["envelope"] = envelope

                class _R:
                    status = "accepted"
                    sequence_number = 1
                    self_hash = "deadbeef"

                return _R()

        async def _tool_impl(input: dict[str, Any]) -> _FakeMessage:
            return _FakeMessage(
                content=f"sent: {input['args']['customer_id']}",
                msg_type="tool",
            )

        tool_call_envelope = {
            "name": "send_invoice",
            "args": {"customer_id": "acme", "amount": 1500.0},
            "id": "call_abc",
            "type": "tool_call",
        }

        ctx = SessionContext(agent_id=uuid4(), session_id=uuid4())
        result = await _emit_action_record(
            func=_tool_impl,
            args=(tool_call_envelope,),
            kwargs={},
            tool_name="send_invoice",
            client=_StubClient(),
            ctx=ctx,
            redaction_config=None,
        )

        assert isinstance(result, _FakeMessage)
        assert captured["envelope"] is not None
        envelope = captured["envelope"]
        assert envelope["event_type"] == "ACTION_RECORD"
        assert envelope["payload"]["input_redacted"] == {
            "args": [
                {
                    "name": "send_invoice",
                    "args": {"customer_id": "acme", "amount": 1500.0},
                    "id": "call_abc",
                    "type": "tool_call",
                }
            ],
            "kwargs": {},
        }
        assert envelope["payload"]["output_redacted"] == {
            "result": {"_message_type": "tool", "content": "sent: acme"}
        }
