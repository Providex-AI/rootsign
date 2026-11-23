"""A held call on a client bound to the caller's own transaction fails fast.

`LocalIngestClient(db=...)` writes into a transaction the caller commits. A
call held for approval writes its pending record there and waits, but nobody
can approve what is not committed: `rootsign approve --list` showed "No pending
approvals", the hold ended in a misleading ActionNotFoundError rather than a
timeout, and after the caller's commit the action stayed `pending` with no
approval record. Now it raises HiTLUnsupportedBackendError before anything is
written, naming ManagedLocalIngestClient, which commits each record.

Real PostgreSQL + TimescaleDB.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from rootsign.errors import HiTLUnsupportedBackendError
from rootsign.sdk.buffered_client import BufferedIngestClient
from rootsign.sdk.client import LocalIngestClient
from rootsign.sdk.context import SessionContext
from rootsign.sdk.decorator import trace

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("buffered", [False, True])
async def test_a_held_call_in_your_own_transaction_fails_fast(clean_db, seeded_agent, buffered):
    client = LocalIngestClient(db=clean_db)
    if buffered:
        client = BufferedIngestClient(client, flush_interval_seconds=999)
    ctx = SessionContext(agent_id=seeded_agent.agent_id)
    ran = []

    @trace(
        ingest_client=client,
        session_context=ctx,
        require_approval=True,
        timeout_seconds=1,
        poll_interval_seconds=0.2,
    )
    async def wire_transfer(amount: float) -> str:
        ran.append(amount)
        return "sent"

    with pytest.raises(HiTLUnsupportedBackendError) as raised:
        await wire_transfer(amount=10.0)

    assert ran == []
    assert "ManagedLocalIngestClient" in str(raised.value)
    assert "your own transaction" in str(raised.value)
    await clean_db.commit()
    stored = (
        await clean_db.execute(
            text("SELECT count(*) FROM actions WHERE session_id = :s"), {"s": str(ctx.session_id)}
        )
    ).scalar_one()
    assert stored == 0  # nothing was written to be left pending
