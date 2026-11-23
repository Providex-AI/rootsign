"""On PostgreSQL, a session for an agent the store does not know does not open.

Real PostgreSQL + TimescaleDB. `session(agent_id=...)` with an ID that was
never registered: the store refuses SESSION_OPEN (UNKNOWN_AGENT). The session
used to run anyway, and the store then refused every action
(SESSION_NOT_FOUND) and the close, so the run looked healthy while nothing was
stored. Now `session()` raises RecordPersistenceError and the body does not run.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

import rootsign
from rootsign.errors import RecordPersistenceError
from rootsign.sdk.client import LocalIngestClient
from tests.support.docs_pages import output_after

pytestmark = pytest.mark.integration


async def test_an_unknown_agent_fails_the_session_closed(clean_db):
    client = LocalIngestClient(db=clean_db)
    agent_id = uuid4()
    ran = []

    with pytest.raises(RecordPersistenceError) as raised:
        async with rootsign.session(agent_id=agent_id, client=client):
            ran.append("body")

    assert ran == []
    message = str(raised.value)
    assert message.startswith(
        "session could not be opened (the store refused SESSION_OPEN: UNKNOWN_AGENT:"
    )
    assert str(agent_id) in message
    assert message.endswith("Nothing was recorded, and the session body did not run.")
    sessions = (await clean_db.execute(text("SELECT count(*) FROM sessions"))).scalar_one()
    assert sessions == 0
    (shown,) = output_after("reference/errors.mdx", "For an unknown agent ID:")
    assert shown.replace("<agent_id>", str(agent_id)) == message
