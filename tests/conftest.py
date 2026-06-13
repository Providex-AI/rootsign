"""Pytest config — function-scoped test engine with NullPool, per-test rollback.

The test database is initialized via `alembic upgrade head -x db=test` (NOT
Base.metadata.create_all) so the `actions` table becomes a real TimescaleDB
hypertable. Each test runs inside a SAVEPOINT that is rolled back on teardown,
leaving the schema intact for the next test.

NullPool + a function-scoped engine avoids the classic asyncpg "Future attached
to a different loop" error caused by SQLAlchemy reusing pooled connections
across pytest-asyncio's per-test event loops.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from rootsign.config import settings

REPO_ROOT = Path(__file__).resolve().parent.parent


def _strip_driver(sqla_url: str) -> str:
    """Strip the `+driver` portion to get a plain libpq DSN for psycopg2.connect."""
    return re.sub(r"^postgresql\+[^:]+://", "postgresql://", sqla_url)


def _ensure_test_database() -> None:
    """Create the test database from the maintenance DB if it doesn't exist."""
    import psycopg2
    from psycopg2 import sql

    maint_dsn = _strip_driver(settings.DATABASE_URL_SYNC)
    conn = psycopg2.connect(maint_dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname='rootsign_test'")
            if cur.fetchone() is None:
                cur.execute(
                    sql.SQL("CREATE DATABASE {} OWNER rootsign").format(
                        sql.Identifier("rootsign_test")
                    )
                )
    finally:
        conn.close()

    test_dsn = _strip_driver(settings.TEST_DATABASE_URL_SYNC)
    conn = psycopg2.connect(test_dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    finally:
        conn.close()


def _run_alembic_on_test_db() -> None:
    env = os.environ.copy()
    subprocess.run(
        [sys.executable, "-m", "alembic", "-x", "db=test", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        check=True,
    )


@pytest.fixture(scope="session", autouse=True)
def _bootstrap_test_db():
    """Bootstrap the test DB once per session.

    Set `ROOTSIGN_SKIP_DB_BOOTSTRAP=1` to skip this entirely — used by the
    `framework-contract-langgraph` CI job, where contract tests are
    deliberately DB-free (mock IngestClient) and the runner has no Postgres
    service attached. Skipping here avoids a connection-refused error at
    session start that would otherwise abort the whole collection.
    """
    if os.environ.get("ROOTSIGN_SKIP_DB_BOOTSTRAP") == "1":
        return
    try:
        _ensure_test_database()
        _run_alembic_on_test_db()
    except Exception as e:  # noqa: BLE001
        print(f"[conftest] Test DB bootstrap failed: {e}", file=sys.stderr)
        raise


@pytest_asyncio.fixture
async def test_engine():
    """Function-scoped async engine using NullPool.

    NullPool is necessary because pytest-asyncio creates a new event loop per
    test by default, and asyncpg Future objects are tied to the loop that
    created them. NullPool ensures we open a fresh connection per test.
    """
    engine = create_async_engine(
        settings.TEST_DATABASE_URL, poolclass=NullPool, future=True
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db(test_engine) -> AsyncIterator[AsyncSession]:
    """Per-test AsyncSession with SAVEPOINT-based rollback.

    Pattern: open a transaction on a connection, attach a SAVEPOINT, and
    reopen one each time the session commits. The outer rollback at teardown
    discards everything the test wrote.
    """
    connection = await test_engine.connect()
    transaction = await connection.begin()
    async_session_factory = async_sessionmaker(
        bind=connection,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    session = async_session_factory()
    await connection.begin_nested()

    @event.listens_for(session.sync_session, "after_transaction_end")
    def _restart_savepoint(sess, trans):  # noqa: ANN001
        if trans.nested and not trans._parent.nested:
            connection.sync_connection.begin_nested()

    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()


@pytest_asyncio.fixture
async def clean_db(test_engine) -> AsyncIterator[AsyncSession]:
    """Like `db`, but TRUNCATEs every table on teardown.

    Use this for tests that need to commit (e.g. concurrent-write tests where
    SAVEPOINTs interfere) or that span multiple sessions.
    """
    async_session_factory = async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    session = async_session_factory()
    try:
        yield session
    finally:
        await session.close()
        async with test_engine.begin() as conn:
            await conn.execute(
                text(
                    "TRUNCATE TABLE approvals, actions, decisions, sessions, "
                    "incidents, policies, agents RESTART IDENTITY CASCADE"
                )
            )


# ---------------------------------------------------------------------------
# SDK-layer fixtures (Sprint 1+)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def ingest_client(db):
    """LocalIngestClient bound to the per-test `db` session.

    The client owns its own IdempotencyStore — see the Phase 0 IngestHandler
    signature resolution in feedback_phase1_sprint01_decisions memory.
    """
    from rootsign.sdk.client import LocalIngestClient

    return LocalIngestClient(db=db)


@pytest_asyncio.fixture
async def registered_agent(db):
    """Pre-registered Agent on the SAVEPOINT-rollback `db` session.

    Use this for tests that stay entirely within one async DB session — the
    agent is invisible to any process / thread / session that doesn't share
    this connection (everything is wrapped in a SAVEPOINT and rolled back
    on teardown). For tests that cross process/session/thread boundaries
    (CLI invocations, asyncio.to_thread, HiTL poll loops, subprocesses)
    use `seeded_agent` instead.
    """
    from uuid import uuid4

    from rootsign.crud import agent as agent_crud
    from rootsign.schemas import (
        AgentCreate,
        AgentEnvironment,
        AgentFramework,
        AgentRiskTier,
    )

    return await agent_crud.create(
        db,
        obj_in=AgentCreate(
            name=f"sdk-agent-{uuid4().hex[:8]}",
            owner="test-team",
            environment=AgentEnvironment.PRODUCTION,
            risk_tier=AgentRiskTier.MEDIUM,
            framework=AgentFramework.LANGGRAPH,
        ),
    )


@pytest_asyncio.fixture
async def seeded_agent(clean_db):
    """Committed Agent on the `clean_db` session — visible across connections.

    Sprint 4 §S4-TASK 8. Counterpart to `registered_agent` for tests that
    exit the test loop's connection: CLI invocations (`asyncio.to_thread(
    runner.invoke, ...)`), the HiTL poll loop (opens its own session per
    cycle from `AsyncSessionLocal`), `test_show_hn_quickstart`. All of
    those would see ZERO rows if the agent were inside a SAVEPOINT.

    The `clean_db` fixture's TRUNCATE-on-teardown reaps every row this
    fixture commits — including downstream Sessions, Actions, and
    Approvals from the test body — so isolation is preserved between
    tests without a per-row cleanup ritual.

    HIGH risk tier + PRODUCTION environment to match what a realistic
    HiTL-gated agent would look like. Framework is LANGGRAPH for parity
    with the Show HN quickstart story.
    """
    from uuid import uuid4

    from rootsign.crud import agent as agent_crud
    from rootsign.schemas import (
        AgentCreate,
        AgentEnvironment,
        AgentFramework,
        AgentRiskTier,
    )

    agent = await agent_crud.create(
        clean_db,
        obj_in=AgentCreate(
            name=f"seeded-test-agent-{uuid4().hex[:8]}",
            owner="test-team",
            environment=AgentEnvironment.PRODUCTION,
            risk_tier=AgentRiskTier.HIGH,
            framework=AgentFramework.LANGGRAPH,
        ),
    )
    await clean_db.commit()
    return agent


def _make_envelope(event_type, agent_id, session_id, payload):
    """Build an IngestEnvelope dict for SDK and integration tests.

    Mirrors the helper in tests/integration/test_ingest.py — kept here so
    SDK-layer tests don't have to import from the Phase 0 suite.
    """
    from datetime import datetime, timezone
    from uuid import uuid4

    from rootsign._version import SDK_VERSION

    return {
        "schema_version": "1.0",
        "sdk_version": SDK_VERSION,
        "event_type": event_type,
        "event_id": str(uuid4()),
        "emitted_at": datetime.now(timezone.utc).isoformat(),
        "agent_id": str(agent_id),
        "session_id": str(session_id),
        "payload": payload,
    }


@pytest.fixture
def make_envelope_fixture():
    """Inject the envelope helper as a fixture for SDK tests that prefer it
    over importing the underscore-private function."""
    return _make_envelope


# Public alias — Sprint 4 §S4-TASK 9 (Show HN quickstart test) does
#     from tests.conftest import make_envelope
# rather than fixture injection so the test body reads like README code.
make_envelope = _make_envelope
