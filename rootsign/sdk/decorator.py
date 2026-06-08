"""@rootsign.trace — wraps a tool call and emits an ACTION_RECORD per call.

Sprint 2 lands the full implementation. The decorator routes by callable
shape:

  * LangChain `BaseTool` → `LangGraphTracer.wrap_tool` (ADR-004). The wrapped
    tool stays a `BaseTool` so it's a drop-in for `ToolNode([...])`.
  * Plain async/sync callable → the framework-agnostic wrapper retained from
    Sprint 1 — same envelope shape, same failure-isolation rule.

Failure isolation (ADR-002):
  The wrapped function's success/failure is the ONLY thing that bubbles up.
  Ingest errors are logged at WARNING and swallowed; the Sprint 3 WAL drain
  turns those warnings into eventual delivery.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from rootsign.ingest.schemas import EventType
from rootsign.sdk.client import IngestClient
from rootsign.sdk.context import SessionContext
from rootsign.sdk.hashing import compute_payload_hash
from rootsign.sdk.redaction import RedactionConfig

logger = logging.getLogger("rootsign.sdk")

SCHEMA_VERSION = "1.0"
SDK_VERSION = "0.1.0.dev0"


def _is_langchain_tool(func: Any) -> bool:
    """True if *func* is a LangChain BaseTool instance.

    Returns False (never raises) when langchain_core is not installed, so the
    SDK can be used without any framework extras.
    """
    try:
        from langchain_core.tools import BaseTool
    except ImportError:
        return False
    return isinstance(func, BaseTool)


def trace(
    *,
    ingest_client: IngestClient,
    session_context: SessionContext,
    tool_name: str | None = None,
    redaction_config: RedactionConfig | None = None,
) -> Callable[[Any], Any]:
    """Decorator factory. Wraps a callable to emit an ACTION_RECORD per call.

    Args:
        ingest_client: Where to send the envelope. See ADR-002.
        session_context: Carries agent_id, session_id, and the monotonic
            sequence counter. A SESSION_OPEN must already have been sent for
            this session_id (use `rootsign.session(...)` to do this
            automatically).
        tool_name: Logical name for the tool. Defaults to the wrapped
            function's `__name__`. Ignored for the `BaseTool` path since
            LangGraph relies on the original `tool.name`.
        redaction_config: Applied to input args + output before hashing /
            persisting. None ⇒ no redaction.

    Keyword-only signature: matches the Sprint 1 surface so existing call
    sites and tests continue to work unchanged.
    """

    def decorator(func: Any) -> Any:
        if _is_langchain_tool(func):
            # Lazy import — only touched when langchain_core is present.
            from rootsign.sdk.frameworks.langgraph import LangGraphTracer

            return LangGraphTracer.wrap_tool(
                func,
                ctx=session_context,
                client=ingest_client,
                redaction_config=redaction_config,
            )

        # Plain-callable path (backward compat with Sprint 1 smoke test).
        _tool_name = tool_name or getattr(func, "__name__", "unknown_tool")

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            return await _emit_action_record(
                func=func,
                args=args,
                kwargs=kwargs,
                tool_name=_tool_name,
                client=ingest_client,
                ctx=session_context,
                redaction_config=redaction_config,
            )

        return wrapper

    return decorator


async def _emit_action_record(
    *,
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    tool_name: str,
    client: IngestClient,
    ctx: SessionContext,
    redaction_config: RedactionConfig | None,
) -> Any:
    """Shared emission logic for both plain and LangGraph paths.

    Runs *func* (sync or async), computes input/output hashes around it, then
    fires the ACTION_RECORD envelope. Best-effort ingest — never raises into
    the caller. If *func* raises, the exception is re-raised AFTER the ingest
    attempt so we still record the failed action.
    """
    input_payload: dict[str, Any] = {
        "args": list(args),
        "kwargs": dict(kwargs),
    }
    redacted_input = (
        redaction_config.redact(input_payload) if redaction_config else input_payload
    )
    input_hash = compute_payload_hash(redacted_input)
    timestamp = datetime.now(timezone.utc)

    result: Any = None
    redacted_output: dict[str, Any] | None = None
    output_hash: str | None = None
    error: BaseException | None = None
    try:
        if asyncio.iscoroutinefunction(func):
            result = await func(*args, **kwargs)
        else:
            maybe = func(*args, **kwargs)
            # A sync callable that returns a coroutine — await it. Covers the
            # LangGraphTracer adapter where _call is sync but its body awaits
            # the original ainvoke.
            result = await maybe if asyncio.iscoroutine(maybe) else maybe
        output_payload = {"result": result}
        redacted_output = (
            redaction_config.redact(output_payload) if redaction_config else output_payload
        )
        output_hash = compute_payload_hash(redacted_output)
    except BaseException as exc:
        error = exc

    await _try_ingest(
        client=client,
        ctx=ctx,
        tool_name=tool_name,
        input_hash=input_hash,
        output_hash=output_hash,
        redacted_input=redacted_input,
        redacted_output=redacted_output,
        timestamp=timestamp,
    )

    if error is not None:
        raise error
    return result


async def _try_ingest(
    *,
    client: IngestClient,
    ctx: SessionContext,
    tool_name: str,
    input_hash: str,
    output_hash: str | None,
    redacted_input: Any,
    redacted_output: Any,
    timestamp: datetime,
) -> None:
    """Best-effort ingest — never raises (ADR-002 failure isolation rule)."""
    try:
        sequence_number = await ctx.next_sequence()
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "sdk_version": SDK_VERSION,
            "event_type": EventType.ACTION_RECORD.value,
            "event_id": str(uuid4()),
            "emitted_at": datetime.now(timezone.utc).isoformat(),
            "agent_id": str(ctx.agent_id),
            "session_id": str(ctx.session_id),
            "payload": {
                "tool_name": tool_name,
                "input_hash": input_hash,
                "output_hash": output_hash,
                "input_redacted": redacted_input if isinstance(redacted_input, dict) else None,
                "output_redacted": redacted_output
                if isinstance(redacted_output, dict)
                else None,
                "timestamp": timestamp.isoformat(),
                "authorization_status": "auto_authorized",
            },
        }
        await client.handle(envelope)
        logger.debug("ACTION_RECORD emitted tool=%s seq=%d", tool_name, sequence_number)
    except Exception as ingest_err:  # noqa: BLE001 — see failure isolation rule
        logger.warning("rootsign ingest failed for tool %s: %s", tool_name, ingest_err)
