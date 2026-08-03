"""MCP proxy contract tests (ADR-010) — the interception seam.

Drive JSON-RPC requests through a real FastAPI proxy app (via `TestClient`)
with a mocked upstream MCP server and a mock `IngestClient` (no DB). Verifies:

  * `tools/call` emits an ACTION_RECORD and forwards the upstream response;
  * every other method (`tools/list`, …) passes through with no emit;
  * redaction runs before hashing (ADR-006);
  * `intercept_tools_call` is keyword-only (Flag 1).

Mocking notes for this repo:
  * No `pytest-mock` — we use `unittest.mock` directly.
  * `httpx` is imported *lazily inside* proxy functions, so `httpx.AsyncClient`
    is patched on the global module (patching `rootsign.mcp.proxy.httpx` would
    fail — there's no module-level attribute, by design). The FastAPI
    `TestClient` uses the sync `httpx.Client`, so patching the async class
    doesn't disturb it.
  * The doc's redaction assertion targeted key `to`; `StandardPIIConfig`
    actually redacts the leaf key `email` (leaf-key matching), so this test
    uses `email`.
"""

from __future__ import annotations

import inspect
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

pytest.importorskip("fastapi", reason="rootsign[mcp] not installed")
pytest.importorskip("httpx", reason="httpx not installed")

from fastapi.testclient import TestClient  # noqa: E402

from rootsign.ingest.schemas import EventType  # noqa: E402
from rootsign.mcp.proxy import MCPProxyTracer, create_proxy_app  # noqa: E402
from rootsign.sdk.context import SessionContext  # noqa: E402
from rootsign.sdk.redaction import StandardPIIConfig  # noqa: E402

MOCK_UPSTREAM_RESPONSE = {
    "jsonrpc": "2.0",
    "result": {"content": [{"type": "text", "text": "Email sent."}], "isError": False},
    "id": 1,
}

TOOLS_CALL_REQUEST = {
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": {"name": "send_email", "arguments": {"to": "alice@example.com", "subject": "Invoice"}},
    "id": 1,
}


@contextmanager
def mock_upstream(json_body, status_code=200):
    """Patch the lazily-imported httpx.AsyncClient to a mock MCP upstream."""
    resp = MagicMock(
        json=lambda: json_body,
        raise_for_status=lambda: None,
        status_code=status_code,
    )
    cm = AsyncMock()
    cm.__aenter__.return_value.post = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    with patch("httpx.AsyncClient", return_value=cm):
        yield


def _mock_ingest_client():
    client = AsyncMock()
    client.handle.return_value = MagicMock(status="accepted", entity_id=None, sequence_number=1)
    return client


class TestMCPProxyInterception:
    def test_intercept_tools_call_is_keyword_only(self):
        # Flag 1 — no positional args on the interception helper.
        sig = inspect.signature(MCPProxyTracer.intercept_tools_call)
        for name in ("request_body", "upstream_url", "ctx", "client"):
            assert sig.parameters[name].kind == inspect.Parameter.KEYWORD_ONLY, name

    def test_tools_call_emits_action_record(self):
        client = _mock_ingest_client()
        ctx = SessionContext(agent_id=uuid4())
        app = create_proxy_app(upstream_url="http://mock/mcp", client=client, ctx=ctx)

        with mock_upstream(MOCK_UPSTREAM_RESPONSE):
            with TestClient(app) as tc:
                response = tc.post("/", json=TOOLS_CALL_REQUEST)

        assert response.status_code == 200
        # Upstream response forwarded verbatim.
        assert response.json() == MOCK_UPSTREAM_RESPONSE
        # ACTION_RECORD emitted with the MCP tool name.
        assert client.handle.called
        envelope = client.handle.call_args[0][0]
        assert envelope["event_type"] == EventType.ACTION_RECORD.value
        assert envelope["payload"]["tool_name"] == "send_email"

    def test_non_tools_call_passes_through(self):
        client = _mock_ingest_client()
        ctx = SessionContext(agent_id=uuid4())
        app = create_proxy_app(upstream_url="http://mock/mcp", client=client, ctx=ctx)

        list_request = {"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 2}
        with mock_upstream({"jsonrpc": "2.0", "result": {"tools": []}, "id": 2}):
            with TestClient(app) as tc:
                response = tc.post("/", json=list_request)

        assert response.status_code == 200
        # No ACTION_RECORD for a non-tool-call method.
        assert not client.handle.called

    def test_redaction_applied_before_hashing(self):
        """PII in MCP arguments is redacted before it reaches input_redacted."""
        client = _mock_ingest_client()
        ctx = SessionContext(agent_id=uuid4())
        app = create_proxy_app(
            upstream_url="http://mock/mcp",
            client=client,
            ctx=ctx,
            redaction_config=StandardPIIConfig(),
        )
        pii_request = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            # `email` is a StandardPIIConfig leaf key; `subject` is not.
            "params": {"name": "send_email", "arguments": {"email": "user@example.com", "subject": "Invoice"}},
            "id": 1,
        }
        with mock_upstream(MOCK_UPSTREAM_RESPONSE):
            with TestClient(app) as tc:
                tc.post("/", json=pii_request)

        payload = client.handle.call_args[0][0]["payload"]
        assert payload["input_redacted"]["email"] == "[REDACTED]"
        assert payload["input_redacted"]["subject"] == "Invoice"


def test_module_imports_without_touching_httpx_at_load():
    """Sanity: proxy module carries no module-level httpx/fastapi attribute."""
    import rootsign.mcp.proxy as proxy_mod

    assert not hasattr(proxy_mod, "httpx")
    assert not hasattr(proxy_mod, "fastapi")
