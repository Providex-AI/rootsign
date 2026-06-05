"""@rootsign.trace — wraps a tool call and emits an ACTION_RECORD per call.

Sprint 1 ships the framework-agnostic skeleton: any async callable can be
decorated, the wrapper computes input/output hashes, builds an envelope, and
sends it via an IngestClient. LangGraph-specific tool interception (and the
CrewAI/AutoGen siblings) land in Sprint 2 — they reuse this same envelope
shape and never re-implement the hashing/redaction logic.

Failure isolation rule (Phase 0 spec + ADR-002):
  The wrapped function's success/failure is the ONLY thing that bubbles up
  to the caller. Ingest errors are logged at WARNING and swallowed; the WAL
  drain in Sprint 3 turns those warnings into eventual delivery.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
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


def trace(
    *,
    ingest_client: IngestClient,
    session_context: SessionContext,
    tool_name: str | None = None,
    redaction_config: RedactionConfig | None = None,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Decorator factory. Wraps an async callable to emit an ACTION_RECORD.

    Args:
        ingest_client: Where to send the envelope. Either a LocalIngestClient
            bound to a DB session or (Phase 2) an HttpIngestClient. The wrapper
            never inspects the concrete type — see ADR-002.
        session_context: Carries the agent_id, session_id, and monotonic
            sequence counter. A SESSION_OPEN envelope must already have been
            sent for this session_id before any decorated calls.
        tool_name: Logical name for the tool. Defaults to the wrapped
            function's __name__. Used as Action.tool_name in the store.
        redaction_config: Applied to both the input args dict and the output
            result before hashing/persisting. None ⇒ no redaction.

    The decorator only accepts async functions. Sync wrappers will land
    alongside the LangGraph integration in Sprint 2.
    """

    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        _tool_name = tool_name or func.__name__

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            # 1. Snapshot input BEFORE invoking the tool so we capture what
            #    the agent intended, not what mutated during execution.
            input_payload: dict[str, Any] = {
                "args": list(args),
                "kwargs": dict(kwargs),
            }
            redacted_input = (
                redaction_config.redact(input_payload) if redaction_config else input_payload
            )
            input_hash = compute_payload_hash(redacted_input)

            timestamp = datetime.now(timezone.utc)
            sequence_number = await session_context.next_sequence()

            # 2. Run the wrapped function. The result (or exception) is the
            #    caller's reality — we never swallow it.
            result: Any = None
            redacted_output: dict[str, Any] | None = None
            output_hash: str | None = None
            error: BaseException | None = None
            try:
                result = await func(*args, **kwargs)
                output_payload = {"result": result}
                redacted_output = (
                    redaction_config.redact(output_payload)
                    if redaction_config
                    else output_payload
                )
                output_hash = compute_payload_hash(redacted_output)
            except BaseException as exc:
                error = exc

            # 3. Best-effort ingest. Any failure here MUST NOT surface to
            #    the caller — see ADR-002 / Phase 0 SDK failure isolation
            #    rule. Logged at WARNING; Sprint 3 will replay via WAL.
            try:
                envelope = {
                    "schema_version": SCHEMA_VERSION,
                    "sdk_version": SDK_VERSION,
                    "event_type": EventType.ACTION_RECORD.value,
                    "event_id": str(uuid4()),
                    "emitted_at": datetime.now(timezone.utc).isoformat(),
                    "agent_id": str(session_context.agent_id),
                    "session_id": str(session_context.session_id),
                    "payload": {
                        "tool_name": _tool_name,
                        "input_hash": input_hash,
                        "output_hash": output_hash,
                        "input_redacted": redacted_input
                        if isinstance(redacted_input, dict)
                        else None,
                        "output_redacted": redacted_output
                        if isinstance(redacted_output, dict)
                        else None,
                        "timestamp": timestamp.isoformat(),
                        "authorization_status": "auto_authorized",
                    },
                }
                await ingest_client.handle(envelope)
            except Exception as ingest_err:  # noqa: BLE001 — see failure isolation rule
                logger.warning(
                    "rootsign ingest failed for tool %s (seq=%d): %s",
                    _tool_name,
                    sequence_number,
                    ingest_err,
                )

            # 4. Re-raise the wrapped function's exception, if any. Done
            #    AFTER the ingest attempt so we record the failed action.
            if error is not None:
                raise error
            return result

        return wrapper

    return decorator
