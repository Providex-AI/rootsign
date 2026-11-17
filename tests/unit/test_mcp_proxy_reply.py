"""`_jsonrpc_reply`: finding the tools/call response in a JSON or SSE body.

A streamable-HTTP MCP server may answer a POST with `application/json` or with
a `text/event-stream`. The proxy records the JSON-RPC response either way, so
these cases pin the SSE parsing it relies on. No MCP extra needed.
"""

from __future__ import annotations

import json

import pytest

from rootsign.mcp.proxy import _jsonrpc_reply

REPLY = {"jsonrpc": "2.0", "id": 7, "result": {"content": [{"type": "text", "text": "sent"}]}}


class _Response:
    def __init__(self, content_type: str, text: str) -> None:
        self.headers = {"content-type": content_type}
        self.text = text

    def json(self):
        return json.loads(self.text)


def _sse(*messages: dict, newline: str = "\n") -> str:
    events = [f"event: message{newline}data: {json.dumps(m)}{newline}" for m in messages]
    return newline.join(events) + newline


def test_a_json_body_is_returned_as_is():
    assert _jsonrpc_reply(_Response("application/json", json.dumps(REPLY)), 7) == REPLY


def test_an_sse_body_yields_the_reply():
    response = _Response("text/event-stream", _sse(REPLY))
    assert _jsonrpc_reply(response, 7) == REPLY


def test_notifications_ahead_of_the_reply_are_skipped():
    progress = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}}
    other = {"jsonrpc": "2.0", "id": 8, "result": {}}
    response = _Response("text/event-stream; charset=utf-8", _sse(progress, other, REPLY))
    assert _jsonrpc_reply(response, 7) == REPLY


def test_crlf_line_endings_are_accepted():
    response = _Response("text/event-stream", _sse(REPLY, newline="\r\n"))
    assert _jsonrpc_reply(response, 7) == REPLY


def test_an_error_reply_counts_as_the_reply():
    error = {"jsonrpc": "2.0", "id": 7, "error": {"code": -32602, "message": "bad args"}}
    assert _jsonrpc_reply(_Response("text/event-stream", _sse(error)), 7) == error


def test_a_stream_without_the_reply_raises():
    with pytest.raises(ValueError, match="no response to request id 7"):
        _jsonrpc_reply(_Response("text/event-stream", _sse({"jsonrpc": "2.0", "id": 8})), 7)
