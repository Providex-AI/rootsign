"""On PostgreSQL, a session for an agent the store does not know warns.

Real PostgreSQL + TimescaleDB. `session(agent_id=...)` with an ID that was
never registered: the store refuses SESSION_OPEN (UNKNOWN_AGENT), then every
action (SESSION_NOT_FOUND) and the close. Each refusal used to reach only
DEBUG, so the run looked healthy while nothing was stored.
"""

from __future__ import annotations

import logging
from uuid import uuid4

import pytest
from sqlalchemy import text

import rootsign
from rootsign.sdk.client import LocalIngestClient

pytestmark = pytest.mark.integration


async def test_an_unknown_agent_warns_for_every_record(clean_db, caplog):
    client = LocalIngestClient(db=clean_db)

    with caplog.at_level(logging.WARNING, logger="rootsign"):
        async with rootsign.session(agent_id=uuid4(), client=client) as ctx:

            @rootsign.trace(ingest_client=client, session_context=ctx)
            async def send_invoice(amount: float) -> str:
                return "sent"

            await send_invoice(10.0)

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    refused = [m for m in messages if m.startswith("rootsign: the store refused the ")]
    assert len(refused) == 3, messages
    assert "SESSION_OPEN" in refused[0] and "UNKNOWN_AGENT" in refused[0]
    assert "ACTION_RECORD for tool 'send_invoice'" in refused[1]
    assert "SESSION_NOT_FOUND" in refused[1]
    stored = (
        await clean_db.execute(
            text("SELECT count(*) FROM actions WHERE session_id = :sid"),
            {"sid": str(ctx.session_id)},
        )
    ).scalar_one()
    assert stored == 0
