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

SCHEMA_VERSION = "1.0"
SDK_VERSION = "0.1.0.dev0"


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
    agent_id: UUID,
    client: IngestClient,
    objective: str | None = None,
    user_id: str | None = None,
) -> AsyncIterator[SessionContext]:
    """Open a rootsign session that auto-bounds SESSION_OPEN / SESSION_CLOSE.

    Usage::

        async with rootsign.session(agent_id=..., client=client) as ctx:
            tools = rootsign.wrap_tools([my_tool], ctx=ctx, client=client)
            # run your graph

    Args:
        agent_id: The registered agent's UUID. Use `rootsign.register_agent`
            once to create it.
        client: An IngestClient (typically a LocalIngestClient bound to your
            DB session). The SESSION_OPEN/CLOSE envelopes flow through this.
        objective: Free-text description of what the agent is being asked to
            do. Persisted on the Session record.
        user_id: Logical end-user identifier, if your application has one.

    The yielded SessionContext exposes `.session_id`, `.agent_id`, and the
    monotonic sequence counter.
    """
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
    try:
        yield ctx
    except BaseException:
        close_status = "failed"
        raise
    finally:
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
