"""Integration tests for `rootsign.session(...)` against the real DB.

Covers the auto-emit lifecycle: SESSION_OPEN on enter, SESSION_CLOSE on exit,
status="completed" on clean exit, status="failed" on exception. The Session
ORM row is read back to verify both envelopes hit the store.
"""

from __future__ import annotations

import pytest

import rootsign
from rootsign.crud import session as session_crud
from rootsign.sdk.client import LocalIngestClient


class TestSessionContextManager:
    async def test_clean_exit_marks_session_completed(self, db, registered_agent):
        client = LocalIngestClient(db=db)
        captured_session_id = None
        async with rootsign.session(
            agent_id=registered_agent.agent_id,
            client=client,
            objective="ctx mgr clean exit",
        ) as ctx:
            captured_session_id = ctx.session_id
            assert ctx.session_open_emitted is True

        row = await session_crud.get(db, id=captured_session_id)
        assert row is not None
        assert row.status == "completed"
        assert row.end_time is not None

    async def test_exception_marks_session_failed_and_reraises(
        self, db, registered_agent
    ):
        client = LocalIngestClient(db=db)
        captured_session_id = None

        class Boom(RuntimeError):
            pass

        with pytest.raises(Boom):
            async with rootsign.session(
                agent_id=registered_agent.agent_id,
                client=client,
                objective="ctx mgr exception path",
            ) as ctx:
                captured_session_id = ctx.session_id
                raise Boom("inside the with block")

        row = await session_crud.get(db, id=captured_session_id)
        assert row is not None
        assert row.status == "failed"

    async def test_session_open_emitted_only_once_via_ctx(self, db, registered_agent):
        """If the body manually calls `mark_session_open` again, the ctx mgr
        has already claimed the flag — body's call must return False."""
        client = LocalIngestClient(db=db)
        async with rootsign.session(
            agent_id=registered_agent.agent_id, client=client
        ) as ctx:
            again = await ctx.mark_session_open()
            assert again is False
