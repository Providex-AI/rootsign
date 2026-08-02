"""S5-TASK 4 — BufferedIngestClient integration (real LangGraph + DB).

End-to-end proof of the ADR-009 micro-batching contract: run a real
`session()` over a `BufferedIngestClient` with auto-flush disabled
(`flush_interval_seconds=999`), fire five auto-authorized tool calls, and
confirm that after the session closes (a) all five ACTION_RECORDs landed via
the pre-close flush and (b) the hash chain verifies VALID — i.e. buffering
never loses a record and never disturbs chain order.

`seeded_agent` (committed) per Flag 3 — the buffered client's background flush
task lives on the loop.
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
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


class TestBufferedClientIntegration:
    async def test_no_records_lost_after_session_close(self, clean_db, seeded_agent):
        base = LocalIngestClient(db=clean_db)
        async with BufferedIngestClient(base, flush_interval_seconds=999) as buffered:
            async with session(agent_id=seeded_agent.agent_id, client=buffered) as ctx:
                tools = LangGraphTracer.wrap_tools([add], ctx=ctx, client=buffered)
                for i in range(5):
                    await tools[0].ainvoke({"a": i, "b": i})

            # Session closed → pre-close flush drained the buffer.
            await clean_db.commit()
            chain = await action_crud.get_session_chain(clean_db, session_id=ctx.session_id)
            assert len(chain) == 5
            # Chain order preserved and sequence numbers dense 1..5.
            assert [a.sequence_number for a in chain] == [1, 2, 3, 4, 5]

            result = await action_crud.verify_chain(clean_db, session_id=ctx.session_id)
            assert result["valid"] is True
            assert result["record_count"] == 5
