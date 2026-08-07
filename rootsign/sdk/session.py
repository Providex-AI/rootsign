"""`rootsign.session(...)` — async context manager for a tamper-evident session.

Auto-emits a SESSION_OPEN envelope on enter and a SESSION_CLOSE envelope on
exit (status=`completed` on clean exit, `failed` when the body raised). The
underlying SessionContext is yielded to the caller so it can be passed into
`wrap_tools(..., ctx=ctx, client=client)` or `@rootsign.trace(...)`.

Re-entrancy: `SessionContext.mark_session_open()` is the atomic flag — if a
caller manually emits SESSION_OPEN before entering the context manager, this
will skip the duplicate emit. SESSION_CLOSE always fires on exit.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from rootsign.ingest.schemas import EventType
from rootsign.sdk.client import IngestClient
from rootsign.sdk.context import SessionContext

logger = logging.getLogger("rootsign.sdk.session")

from rootsign._version import SDK_VERSION  # noqa: F401  (re-exported via envelopes)

SCHEMA_VERSION = "1.0"


def _envelope_base(
    *,
    agent_id: UUID,
    session_id: UUID,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "sdk_version": SDK_VERSION,
        "agent_id": str(agent_id),
        "session_id": str(session_id),
    }


@contextlib.asynccontextmanager
async def session(
    *,
    agent_id: UUID | None = None,
    client: IngestClient | None = None,
    objective: str | None = None,
    user_id: str | None = None,
) -> AsyncIterator[SessionContext]:
    """Open a rootsign session that auto-bounds SESSION_OPEN / SESSION_CLOSE.

    Facade form (ADR-012) — `rootsign.init()` supplies the agent and backend::

        rootsign.init(agent="invoice-agent")

        async with rootsign.session(objective="process batch"):
            tools = rootsign.wrap_tools([my_tool])   # implicit ctx + client

    Explicit form — unchanged, and still what tests and multi-agent processes
    should use::

        async with rootsign.session(agent_id=..., client=client) as ctx:
            tools = rootsign.wrap_tools([my_tool], ctx=ctx, client=client)

    Args:
        agent_id: The registered agent's UUID. Use `rootsign.register_agent`
            once to create it. Omit it to have the agent get-or-created lazily
            from `rootsign.init()`'s config on entry (ADR-012 Decision 2 — this
            is where init()'s deferred I/O happens).
        client: An IngestClient (typically a LocalIngestClient bound to your DB
            session). The SESSION_OPEN/CLOSE envelopes flow through this. Omit
            it to have the backend's client built from `init()`'s config; a
            client this function builds itself is also closed on exit, while a
            caller-supplied one is left alone.
        objective: Free-text description of what the agent is being asked to
            do. Persisted on the Session record.
        user_id: Logical end-user identifier, if your application has one.

    While the body runs, `(ctx, client)` is published to a ContextVar so
    `wrap_tools` / `@trace` / the MCP proxy resolve it implicitly. The
    ContextVar is always reset on exit. Concurrent sessions in one process stay
    isolated — each task sees only its own.

    The yielded SessionContext exposes `.session_id`, `.agent_id`, and the
    monotonic sequence counter.

    Raises:
        RootSignNotInitializedError: neither `rootsign.init()` was called nor
            both `agent_id=` and `client=` were passed.
    """
    from rootsign.sdk import facade

    owns_client = False
    if agent_id is None or client is None:
        config = facade.get_init_config()
        if config is None:
            from rootsign.errors import RootSignNotInitializedError

            raise RootSignNotInitializedError("rootsign.session()")
        if agent_id is None:
            agent_id = await facade._resolve_agent_id(config)
        if client is None:
            client = facade._build_client(config)
            owns_client = True

    ctx = SessionContext(agent_id=agent_id)
    base = _envelope_base(agent_id=agent_id, session_id=ctx.session_id)

    should_open = await ctx.mark_session_open()
    if should_open:
        await client.handle(
            {
                **base,
                "event_type": EventType.SESSION_OPEN.value,
                "event_id": str(uuid4()),
                "emitted_at": datetime.now(timezone.utc).isoformat(),
                "payload": {
                    "objective": objective,
                    "user_id": user_id,
                },
            }
        )

    close_status = "completed"
    # Publish (ctx, client) for implicit resolution by the tracers (ADR-012
    # Decision 3). Set AFTER SESSION_OPEN so nothing can emit an ACTION_RECORD
    # into a session that hasn't been opened yet. Always reset in the finally —
    # a leaked token shows up as cross-test pollution.
    ambient_token = facade._set_current_session(ctx, client)
    try:
        yield ctx
    except BaseException:
        close_status = "failed"
        raise
    finally:
        facade._reset_current_session(ambient_token)
        # Flush any buffered ACTION_RECORDs BEFORE emitting SESSION_CLOSE so
        # every action is persisted ahead of the session's terminal record
        # (ADR-009 Decision 5). Duck-typed — works with any client exposing
        # flush(), no IngestClient ABC change. Belt-and-suspenders with the
        # BufferedIngestClient passthrough path, which already flushes ahead
        # of a SESSION_CLOSE envelope; this also covers future clients that
        # buffer terminal records or change that ordering. Best-effort:
        # a flush failure is logged, never raised, and never blocks the
        # SESSION_CLOSE emit (ADR-002 failure isolation).
        flush = getattr(client, "flush", None)
        if callable(flush):
            try:
                await flush()
            except Exception as flush_err:  # noqa: BLE001
                logger.warning(
                    "rootsign pre-close flush failed for session %s: %s",
                    ctx.session_id,
                    flush_err,
                )
        # Best-effort SESSION_CLOSE — log but never swallow the original
        # exception from inside the `with` body.
        try:
            await client.handle(
                {
                    **base,
                    "event_type": EventType.SESSION_CLOSE.value,
                    "event_id": str(uuid4()),
                    "emitted_at": datetime.now(timezone.utc).isoformat(),
                    "payload": {
                        "status": close_status,
                        "metadata": {"total_actions": ctx.current_sequence},
                    },
                }
            )
        except Exception as close_err:  # noqa: BLE001
            logger.warning(
                "rootsign SESSION_CLOSE emit failed for session %s: %s",
                ctx.session_id,
                close_err,
            )
        # Only a client this function built is ours to close — a caller-supplied
        # one outlives the session by contract.
        if owns_client:
            await facade._maybe_close(client)
