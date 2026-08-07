"""Facade on the Postgres backend — get-or-register + ManagedLocalIngestClient.

ADR-012 W3, T3.3 / T3.3a. Real PostgreSQL + TimescaleDB (no mocks). These tests
commit, so they use the `clean_db` TRUNCATE-on-teardown engine and rebind
`rootsign.database.AsyncSessionLocal` — both `get_or_register_agent` and
`ManagedLocalIngestClient` resolve it lazily, which is what makes the rebind
land (same pattern as `patched_cli_session` in test_approve_cli).
"""

from __future__ import annotations

import logging

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import rootsign
from rootsign.models.agent import Agent
from rootsign.sdk import facade
from rootsign.sdk.client import ManagedLocalIngestClient
from rootsign.sdk.registration import get_or_register_agent

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def pg_facade(monkeypatch, test_engine, clean_db):
    """Point the facade's lazy `AsyncSessionLocal` at the test engine.

    `clean_db` is requested purely for its TRUNCATE-on-teardown — the rows here
    are written through committed sessions of our own, not through it.
    """
    factory = async_sessionmaker(
        bind=test_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    monkeypatch.setattr("rootsign.database.AsyncSessionLocal", factory)
    monkeypatch.setenv("ROOTSIGN_BACKEND", "postgres")
    facade._reset_init_config()
    yield factory
    facade._reset_init_config()


async def _register(**kwargs):
    defaults = {
        "environment": "development",
        "owner": "test-owner",
        "risk_tier": "medium",
        "framework": "langgraph",
    }
    return await get_or_register_agent(**{**defaults, **kwargs})


# --------------------------------------------------------------------------
# T3.3 / T3.3a — get-or-register on (name, environment)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_or_register_is_idempotent(pg_facade):
    first = await _register(name="pg-agent")
    second = await _register(name="pg-agent")
    assert first == second

    async with pg_facade() as db:
        rows = (await db.execute(select(Agent).where(Agent.name == "pg-agent"))).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_same_name_different_environment_are_distinct_agents(pg_facade):
    """The point of the 0005 migration — UNIQUE(name) forbade this."""
    dev = await _register(name="dual-agent", environment="development")
    prod = await _register(name="dual-agent", environment="production")
    assert dev != prod


@pytest.mark.asyncio
async def test_drift_warns_and_keeps_stored_values(pg_facade, caplog):
    agent_id = await _register(name="pg-drift", risk_tier="low")
    with caplog.at_level(logging.WARNING, logger="rootsign.sdk.registration"):
        again = await _register(name="pg-drift", risk_tier="critical")

    assert again == agent_id
    assert "already registered with different attributes" in caplog.text
    async with pg_facade() as db:
        stored = (
            await db.execute(select(Agent).where(Agent.name == "pg-drift"))
        ).scalar_one()
    assert stored.risk_tier == "low"  # mutation is an admin op, not an init side effect


@pytest.mark.asyncio
async def test_migration_replaced_the_name_only_unique_constraint(pg_facade):
    async with pg_facade() as db:
        names = {
            row[0]
            for row in (
                await db.execute(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = 'agents'::regclass AND contype = 'u'"
                    )
                )
            ).all()
        }
    assert "uq_agents_name_environment" in names
    assert "uq_agents_name" not in names


# --------------------------------------------------------------------------
# ManagedLocalIngestClient — commits per record
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_managed_client_commits_each_record(pg_facade):
    """Durability per handle() is what makes cross-process HiTL work."""
    agent_id = await _register(name="managed-agent")
    client = ManagedLocalIngestClient(session_factory=pg_facade)

    async with rootsign.session(agent_id=agent_id, client=client) as ctx:
        # Mid-session, from a *different* connection: SESSION_OPEN is committed.
        async with pg_facade() as observer:
            rows = (
                await observer.execute(
                    text("SELECT status FROM sessions WHERE session_id = :sid"),
                    {"sid": str(ctx.session_id)},
                )
            ).all()
            assert len(rows) == 1

    async with pg_facade() as observer:
        status = (
            await observer.execute(
                text("SELECT status FROM sessions WHERE session_id = :sid"),
                {"sid": str(ctx.session_id)},
            )
        ).scalar_one()
    assert status == "completed"


# --------------------------------------------------------------------------
# End-to-end: init() → session() → trace() → verify, all on Postgres
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_facade_end_to_end_on_postgres(pg_facade):
    rootsign.init(agent="pg-quickstart", risk_tier="high")

    @rootsign.trace()
    async def add(a: int, b: int) -> int:
        return a + b

    async with rootsign.session(objective="add on postgres") as ctx:
        assert await add(2, 3) == 5
        assert await add(4, 5) == 9

    async with pg_facade() as db:
        result = await rootsign.verify_session(ctx.session_id, db)
    assert result.valid is True
    assert result.record_count == 2
