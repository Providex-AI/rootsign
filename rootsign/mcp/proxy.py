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

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rootsign.sdk.client import IngestClient
    from rootsign.sdk.context import SessionContext
    from rootsign.sdk.redaction import RedactionConfig

logger = logging.getLogger("rootsign.mcp.proxy")

# Headers that describe one HTTP hop rather than the MCP exchange, so they are
# never relayed. Everything else (Accept, Mcp-Session-Id, MCP-Protocol-Version,
# Authorization, Last-Event-ID, ...) passes through in both directions: a
# streamable-HTTP MCP server answers 406 without the client's Accept header,
# and a stateful one rejects requests that lose the session id. Accept-Encoding
# and Content-Encoding are dropped so bodies cross the proxy uncompressed and
# their length is recomputed on the way out.
_HOP_BY_HOP = frozenset(
    {
        "accept-encoding",
        "connection",
        "content-encoding",
        "content-length",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# What the streamable-HTTP transport requires a client to accept. Used when a
# caller of `intercept_tools_call` passes no headers of its own.
_MCP_ACCEPT = "application/json, text/event-stream"


def _relay_headers(headers: Any) -> dict[str, str]:
    """The end-to-end headers from `headers` (any mapping), hop-by-hop ones removed."""
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


def _upstream_client() -> Any:
    """The httpx client every upstream request goes through.

    * No read deadline: a tool may legitimately run for minutes, and httpx's
      5-second default would cut it off. Connecting still fails fast.
    * Redirects are followed here, never relayed. A server mounted at `/mcp/`
      answers `/mcp` with a 307 whose Location is the upstream's own address;
      relaying it would let the client follow it straight past the proxy, and
      every call after that would go unrecorded.
    """
    import httpx

    return httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None), follow_redirects=True)


def _jsonrpc_reply(response: Any, request_id: Any) -> "dict[str, Any]":
    """The JSON-RPC response to `request_id`, from a JSON or an SSE body.

    A streamable-HTTP server may answer a POST with `application/json` or with
    a `text/event-stream` whose events are JSON-RPC messages. In the SSE case
    the reply is the message carrying our id and a `result` or `error`; any
    notifications sent ahead of it are not part of the recorded output.
    """
    content_type = response.headers.get("content-type", "")
    if not content_type.startswith("text/event-stream"):
        return response.json()

    for event in response.text.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(
            line[5:].removeprefix(" ") for line in event.split("\n") if line.startswith("data:")
        )
        if not data:
            continue
        message = json.loads(data)
        if (
            isinstance(message, dict)
            and message.get("id") == request_id
            and ("result" in message or "error" in message)
        ):
            return message
    raise ValueError(f"upstream SSE stream carried no response to request id {request_id!r}")


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
        headers: "dict[str, str] | None" = None,
    ) -> "dict[str, Any]":
        """Intercept one `tools/call`, record it, and forward upstream.

        `headers` are the client's end-to-end request headers, relayed to the
        upstream as-is so its session id, protocol version and auth survive the
        hop. Without them the upstream still gets the `Accept` value the
        streamable-HTTP transport requires. The upstream may reply with JSON or
        SSE; either way the JSON-RPC response is returned, and recorded, as a
        dict. A non-2xx upstream status raises `httpx.HTTPStatusError` after the
        call is recorded with no output.

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

        forward_headers = httpx.Headers(_relay_headers(headers or {}))
        forward_headers.setdefault("accept", _MCP_ACCEPT)

        async def _forward_to_upstream(*_args: Any, **_kwargs: Any) -> Any:
            async with _upstream_client() as http:
                response = await http.post(upstream_url, json=request_body, headers=forward_headers)
                response.raise_for_status()
                return _jsonrpc_reply(response, request_body.get("id"))

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
    upstream) and answered with the upstream's JSON-RPC response as
    `application/json`, which the streamable-HTTP transport allows for any
    POST. Everything else passes through unchanged, in both directions: other
    POSTs (`initialize`, `tools/list`, notifications and their empty `202`
    replies), the `GET` stream for server-initiated messages, and the `DELETE`
    that ends a session. Status codes, bodies and end-to-end headers such as
    `Mcp-Session-Id` are relayed as the upstream sent them.
    """
    # fastapi is an optional (mcp-extra) dep — import lazily so `import
    # rootsign.mcp.proxy` works without rootsign[mcp]. Imported eagerly here
    # (not under __future__ annotations) so FastAPI can resolve the endpoint's
    # Request / Response annotations below.
    import httpx
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, Response, StreamingResponse

    from rootsign._version import __version__

    app = FastAPI(title="RootSign MCP Proxy", version=__version__)

    def _relayed(upstream: Any) -> Response:
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=_relay_headers(upstream.headers),
        )

    @app.post("/")
    @app.post("/mcp")
    async def handle_jsonrpc(request: Request) -> Response:  # noqa: F811 — two routes
        raw = await request.body()
        headers = _relay_headers(request.headers)
        try:
            body = json.loads(raw)
        except ValueError:
            body = None

        if isinstance(body, dict) and body.get("method") == "tools/call":
            try:
                result = await MCPProxyTracer.intercept_tools_call(
                    request_body=body,
                    upstream_url=upstream_url,
                    ctx=ctx,
                    client=client,
                    redaction_config=redaction_config,
                    require_approval=require_approval,
                    headers=headers,
                )
            except httpx.HTTPStatusError as exc:
                # Already recorded with no output; give the client the
                # upstream's own error (an expired session, a bad request).
                return _relayed(exc.response)
            return JSONResponse(content=result)

        # Everything else is relayed byte for byte, including bodies that are
        # not JSON-RPC requests (batches, notifications).
        async with _upstream_client() as http:
            return _relayed(await http.post(upstream_url, content=raw, headers=headers))

    @app.get("/")
    @app.get("/mcp")
    async def handle_stream(request: Request) -> Response:  # noqa: F811 — two routes
        # The long-lived SSE stream a client may open for server-initiated
        # messages. Streamed, not buffered, and closed when either side ends.
        http = _upstream_client()
        upstream = await http.send(
            http.build_request("GET", upstream_url, headers=_relay_headers(request.headers)),
            stream=True,
        )

        async def relay() -> Any:
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
                await http.aclose()

        return StreamingResponse(
            relay(), status_code=upstream.status_code, headers=_relay_headers(upstream.headers)
        )

    @app.delete("/")
    @app.delete("/mcp")
    async def handle_delete(request: Request) -> Response:  # noqa: F811 — two routes
        async with _upstream_client() as http:
            return _relayed(
                await http.delete(upstream_url, headers=_relay_headers(request.headers))
            )

    return app
