"""ROOTSIGN_BUFFERED=true on PostgreSQL keeps every action of a session.

The facade path a user gets from `rootsign.init()` with buffering on: the
background flush runs every `ROOTSIGN_BUFFER_INTERVAL`, and the agent does
other work between tool calls, so a flush is usually mid-batch when the
session ends. The flush at session close used to return as soon as it found
the buffer empty, SESSION_CLOSE then reached the store first, and the store
refused the rest of the batch: sessions closed `completed`, verified VALID,
and were missing their last actions, with nothing logged.

The older buffered integration tests set `flush_interval_seconds=999`, which
switches the background flush off and with it the race.

Real PostgreSQL + TimescaleDB; `pg_facade` commits through its own sessions.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import rootsign
from rootsign.sdk import facade

pytestmark = pytest.mark.integration

CALLS = 60


@pytest_asyncio.fixture
async def pg_facade(monkeypatch, test_engine, clean_db):
    factory = async_sessionmaker(
        bind=test_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    monkeypatch.setattr("rootsign.database.AsyncSessionLocal", factory)
    monkeypatch.setenv("ROOTSIGN_BACKEND", "postgres")
    monkeypatch.setenv("ROOTSIGN_BUFFERED", "true")
    monkeypatch.setenv("ROOTSIGN_BUFFER_INTERVAL", "0.01")
    facade._reset_init_config()
    yield factory
    facade._reset_init_config()


@rootsign.trace()
async def step(n: int) -> int:
    return n


@pytest.mark.asyncio
async def test_buffered_session_on_postgres_keeps_every_action(pg_facade):
    rootsign.init(agent="buffered-agent", owner="test-owner")

    async with rootsign.session(objective="buffered run") as ctx:
        for i in range(CALLS):
            await asyncio.sleep(0.002)  # the model thinking between tool calls
            await step(i)

    async with pg_facade() as db:
        stored = (
            await db.execute(
                text("SELECT count(*) FROM actions WHERE session_id = :sid"),
                {"sid": str(ctx.session_id)},
            )
        ).scalar_one()
        action_count = (
            await db.execute(
                text("SELECT action_count FROM sessions WHERE session_id = :sid"),
                {"sid": str(ctx.session_id)},
            )
        ).scalar_one()

    assert stored == CALLS
    assert action_count == CALLS
