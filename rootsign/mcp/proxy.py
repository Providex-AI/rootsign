"""RootSign as an MCP Proxy — protocol-level provenance (ADR-010, Mode A).

A transparent HTTP reverse proxy between an agent's MCP client and the real
MCP server. It intercepts JSON-RPC `tools/call` requests, emits an
ACTION_RECORD through the same SDK machinery the framework tracers use, then
forwards the request upstream and returns the response. The agent changes only
its `MCP_SERVER_URL` — no framework code.

**Lazy imports (ADR-010 Decision 3).** `fastapi` and `httpx` are imported
*inside* the functions that need them, never at module load. `import rootsign`
and even `import rootsign.mcp.proxy` must succeed without `rootsign[mcp]`
installed; the heavy imports fire only when a proxy app is actually built or a
call is intercepted.

Note: this module deliberately does NOT use `from __future__ import
annotations`. FastAPI resolves an endpoint's parameter annotations against the
module globals; the ASGI endpoint below is typed with the locally-imported
`Request` / `JSONResponse`, which only resolve when evaluated eagerly at
def-time. The cross-module SDK types are quoted instead, so they stay strings
and never force an import.

Mirrors LangGraphTracer / CrewAITracer: `_to_json_safe` on arguments,
redaction before hashing (ADR-006), the shared `_emit_action_record` /
`_emit_hitl_action` helpers, and HiTL reuse via `require_approval=True`
(ADR-007).
"""

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rootsign.sdk.client import IngestClient
    from rootsign.sdk.context import SessionContext
    from rootsign.sdk.redaction import RedactionConfig

logger = logging.getLogger("rootsign.mcp.proxy")


class MCPProxyTracer:
    """Protocol-level interceptor for MCP `tools/call` requests.

    Called by the proxy ASGI app for every `tools/call`. Keyword-only
    (Flag 1). Emits an ACTION_RECORD, forwards to upstream, returns the
    upstream JSON-RPC response.
    """

    @staticmethod
    async def intercept_tools_call(
        *,
        request_body: "dict[str, Any]",
        upstream_url: str,
        ctx: "SessionContext | None" = None,
        client: "IngestClient | None" = None,
        redaction_config: "RedactionConfig | None" = None,
        require_approval: bool = False,
        approval_context_builder: "Any | None" = None,
        poll_interval_seconds: float = 2.0,
        timeout_seconds: float = 300.0,
    ) -> "dict[str, Any]":
        """Intercept one `tools/call`, record it, and forward upstream.

        The MCP `params.arguments` dict *is* the input payload — we
        `_to_json_safe` and redact it here (ADR-006 before-hash), then hand it
        to the emit helper via `_input_payload_override` so the stored
        `input_redacted` matches the wire shape rather than a synthetic
        args/kwargs wrapper. `func` forwards to upstream, so its return value
        becomes the recorded output.

        `ctx`/`client` may be omitted when the proxy runs inside an
        `async with rootsign.session(...)` — they resolve from the ambient
        session per ADR-012. Explicit arguments always win.
        """
        # httpx is an optional (mcp-extra) dep — import lazily.
        import httpx

        from rootsign.sdk.decorator import (
            _emit_action_record,
            _emit_hitl_action,
            _to_json_safe,
        )
        from rootsign.sdk.facade import _resolve_ctx_client

        ctx, client = _resolve_ctx_client(ctx, client, surface="MCPProxyTracer")

        params = request_body.get("params", {}) or {}
        tool_name = params.get("name", "unknown_mcp_tool")
        arguments = params.get("arguments", {}) or {}

        # Redaction BEFORE hashing (ADR-006). The proxy pre-builds the redacted
        # input so both emit paths store the faithful MCP arguments shape.
        safe_arguments = _to_json_safe(arguments)
        redacted_input = (
            redaction_config.redact(safe_arguments) if redaction_config else safe_arguments
        )

        async def _forward_to_upstream(*_args: Any, **_kwargs: Any) -> Any:
            async with httpx.AsyncClient() as http:
                response = await http.post(upstream_url, json=request_body)
                response.raise_for_status()
                return response.json()

        if require_approval:
            # HiTL over MCP (ADR-007): pauses before forwarding upstream. The
            # tool (=upstream forward) only runs after approval.
            return await _emit_hitl_action(
                func=_forward_to_upstream,
                args=(),
                kwargs={},
                tool_name=tool_name,
                client=client,
                ctx=ctx,
                redaction_config=redaction_config,
                approval_context_builder=approval_context_builder,
                poll_interval_seconds=poll_interval_seconds,
                timeout_seconds=timeout_seconds,
                _input_payload_override=redacted_input,
            )

        return await _emit_action_record(
            func=_forward_to_upstream,
            args=(),
            kwargs={},
            tool_name=tool_name,
            client=client,
            ctx=ctx,
            redaction_config=redaction_config,
            _input_payload_override=redacted_input,
        )


def create_proxy_app(
    upstream_url: str,
    client: "IngestClient | None" = None,
    ctx: "SessionContext | None" = None,
    redaction_config: "RedactionConfig | None" = None,
    require_approval: bool = False,
) -> Any:
    """Build a FastAPI ASGI app that proxies an MCP server with provenance.

    Usage::

        app = create_proxy_app(
            upstream_url="http://real-mcp-server:8001/mcp",
            client=client,
            ctx=ctx,
        )
        # uvicorn rootsign.mcp.proxy:app --host 0.0.0.0 --port 8000

    `tools/call` requests are intercepted (ACTION_RECORD emitted, forwarded
    upstream). Every other JSON-RPC method (`initialize`, `tools/list`,
    `ping`, …) passes through unchanged.
    """
    # fastapi is an optional (mcp-extra) dep — import lazily so `import
    # rootsign.mcp.proxy` works without rootsign[mcp]. Imported eagerly here
    # (not under __future__ annotations) so FastAPI can resolve the endpoint's
    # Request / JSONResponse annotations below.
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    from rootsign._version import __version__

    app = FastAPI(title="RootSign MCP Proxy", version=__version__)

    @app.post("/")
    @app.post("/mcp")
    async def handle_jsonrpc(request: Request) -> JSONResponse:  # noqa: F811 — two routes
        body = await request.json()
        method = body.get("method", "")

        if method == "tools/call":
            result = await MCPProxyTracer.intercept_tools_call(
                request_body=body,
                upstream_url=upstream_url,
                ctx=ctx,
                client=client,
                redaction_config=redaction_config,
                require_approval=require_approval,
            )
            return JSONResponse(content=result)

        # Non-tool-call methods pass through unchanged.
        import httpx

        async with httpx.AsyncClient() as http:
            response = await http.post(upstream_url, json=body)
            return JSONResponse(content=response.json(), status_code=response.status_code)

    return app
