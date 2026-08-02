"""S5-TASK 3 — session() flushes a buffered client before SESSION_CLOSE.

Verifies the ADR-009 Decision 5 contract end-to-end against a real DB: while
a `session()` is open over a `BufferedIngestClient` (auto-flush disabled), an
auto-authorized ACTION_RECORD stays buffered and absent from the Action
chain; once the session exits, `__aexit__`'s pre-close flush drains it so the
record lands before SESSION_CLOSE.

DB-backed (real PG + Timescale). Uses `seeded_agent` (committed) not
`registered_agent`: the BufferedIngestClient's background flush task exists on
the loop, so we stay on the cross-boundary-safe fixture (Flag 3).
"""

from __future__ import annotations

import pytest

from rootsign.crud import action as action_crud
from rootsign.sdk.buffered_client import BufferedIngestClient
from rootsign.sdk.client import LocalIngestClient
from rootsign.sdk.frameworks.langgraph import LangGraphTracer
from rootsign.sdk.session import session

pytest.importorskip("langchain_core")
from langchain_core.tools import tool  # noqa: E402


@tool
def noop(x: int) -> int:
    """No-op passthrough tool."""
    return x


async def test_session_flushes_buffered_client_before_close(clean_db, seeded_agent):
    base_client = LocalIngestClient(db=clean_db)
    # flush_interval_seconds=999 → the background loop never ticks during the
    # test, so the ONLY thing that can drain the buffer is session()'s
    # pre-close flush. That isolates the behavior under test.
    async with BufferedIngestClient(base_client, flush_interval_seconds=999) as buffered:
        async with session(agent_id=seeded_agent.agent_id, client=buffered) as ctx:
            tools = LangGraphTracer.wrap_tools([noop], ctx=ctx, client=buffered)
            await tools[0].ainvoke({"x": 1})

            # The action is buffered, not yet in the DB.
            chain_before = await action_crud.get_session_chain(
                clean_db, session_id=ctx.session_id
            )
            assert len(chain_before) == 0

        # session().__aexit__ ran: flush() fired before SESSION_CLOSE.
        await clean_db.commit()
        chain_after = await action_crud.get_session_chain(clean_db, session_id=ctx.session_id)
        assert len(chain_after) == 1
