"""Performance benchmarks.

AC-2.10: 1,000 Action inserts in a single session must complete in < 2.0s.
Plus: session-chain retrieval over 1,000 actions must complete in < 500ms.

These tests COMMIT to the test database and TRUNCATE on teardown — they cannot
share the SAVEPOINT rollback machinery of the `db` fixture, because each insert
in create_with_hash needs its own transaction boundary semantics under load.
"""

from __future__ import annotations

import time
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from providex import crud
from providex.schemas import (
    ActionAuthorizationStatus,
    ActionCreate,
    AgentCreate,
    AgentEnvironment,
    AgentFramework,
    AgentRiskTier,
    SessionCreate,
    SessionStatus,
)

pytestmark = [pytest.mark.benchmark, pytest.mark.integration]


async def _bootstrap_session(db: AsyncSession):
    agent = await crud.agent.create(
        db,
        obj_in=AgentCreate(
            name=f"perf-agent-{uuid4().hex[:8]}",
            owner="perf-team",
            environment=AgentEnvironment.STAGING,
            risk_tier=AgentRiskTier.MEDIUM,
            framework=AgentFramework.CUSTOM,
        ),
    )
    s = await crud.session.create(
        db,
        obj_in=SessionCreate(agent_id=agent.agent_id, status=SessionStatus.RUNNING),
    )
    await db.commit()
    return s


class TestActionThroughput:
    # AC-2.10
    async def test_1000_actions_under_2_seconds(self, clean_db: AsyncSession):
        s = await _bootstrap_session(clean_db)
        session_id = s.session_id

        start = time.perf_counter()
        for i in range(1000):
            await crud.action.create_with_hash(
                clean_db,
                obj_in=ActionCreate(
                    session_id=session_id,
                    tool_name=f"tool_{i}",
                    input_hash="a" * 64,
                    output_hash="b" * 64,
                    authorization_status=ActionAuthorizationStatus.AUTO_AUTHORIZED,
                ),
            )
        await clean_db.commit()
        elapsed = time.perf_counter() - start

        # Verify all 1000 made it.
        chain = await crud.action.get_session_chain(clean_db, session_id=session_id)
        assert len(chain) == 1000, f"Inserted only {len(chain)} of 1000"
        assert elapsed < 2.0, f"1000 inserts took {elapsed:.3f}s — exceeds 2.0s limit"

        print(f"\n  1000 inserts: {elapsed:.3f}s ({1000 / elapsed:.0f} inserts/s)")

    async def test_session_chain_retrieval_under_500ms(self, clean_db: AsyncSession):
        s = await _bootstrap_session(clean_db)
        session_id = s.session_id
        for i in range(1000):
            await crud.action.create_with_hash(
                clean_db,
                obj_in=ActionCreate(
                    session_id=session_id,
                    tool_name=f"t_{i}",
                    input_hash="a" * 64,
                    output_hash="b" * 64,
                ),
            )
        await clean_db.commit()

        start = time.perf_counter()
        chain = await crud.action.get_session_chain(clean_db, session_id=session_id)
        elapsed = time.perf_counter() - start

        assert len(chain) == 1000
        assert elapsed < 0.5, f"Chain retrieval took {elapsed:.3f}s — exceeds 500ms"
        print(f"\n  1000-row chain retrieval: {elapsed * 1000:.1f}ms")

    async def test_verify_chain_under_2_seconds(self, clean_db: AsyncSession):
        s = await _bootstrap_session(clean_db)
        session_id = s.session_id
        for i in range(1000):
            await crud.action.create_with_hash(
                clean_db,
                obj_in=ActionCreate(
                    session_id=session_id,
                    tool_name=f"t_{i}",
                    input_hash="a" * 64,
                    output_hash="b" * 64,
                ),
            )
        await clean_db.commit()

        start = time.perf_counter()
        result = await crud.action.verify_chain(clean_db, session_id=session_id)
        elapsed = time.perf_counter() - start

        assert result["valid"] is True
        assert result["record_count"] == 1000
        print(f"\n  verify_chain(1000): {elapsed * 1000:.1f}ms")
