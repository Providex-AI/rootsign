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

from rootsign import crud
from rootsign.schemas import (
    ActionAuthorizationStatus,
    ActionCreate,
    AgentCreate,
    AgentEnvironment,
    AgentFramework,
    AgentRiskTier,
    SessionCreate,
    SessionStatus,
)

from tests.performance._bench import format_samples, median_seconds

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
        """Tightest budget in the repo — ~1.47s measured against a 2.0s limit,
        roughly 1.36x headroom, and it measures raw insert throughput, which is
        disk- and Postgres-tuning dependent. Sampled 3x and asserted on the
        median so one stalled run can't fail the build; the 2.0s threshold is
        AC-2.10 and stays exactly as specified.

        Sampled 5x despite each run costing ~1.5s. A trial run produced
        samples 1.6s / 2.4s / 1.4s — the 2.4s sample alone exceeds the budget
        and would have failed the build under the old single-sample scheme.
        With the least headroom of any budget here, this one earns the extra
        samples: 3 tolerates one outlier, 5 tolerates two.
        """

        async def measure() -> float:
            # Fresh session per sample — untimed, and it keeps each run
            # inserting into an empty chain rather than an ever-growing one.
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

            # Verify all 1000 made it (untimed).
            chain = await crud.action.get_session_chain(clean_db, session_id=session_id)
            assert len(chain) == 1000, f"Inserted only {len(chain)} of 1000"
            return elapsed

        median, samples = await median_seconds(measure, repeats=5)
        print(
            f"\n  1000 inserts: median {median:.3f}s ({1000 / median:.0f} inserts/s)"
            f"  samples: {format_samples(samples, unit='s')}"
        )
        assert median < 2.0, (
            f"1000 inserts median {median:.3f}s — exceeds 2.0s limit (AC-2.10). "
            f"Samples: {format_samples(samples, unit='s')}. "
            "Budgets here are hardware/environment dependent — confirm the machine "
            "is idle before treating this as a regression."
        )

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

        # Seeded once above; only the read is sampled.
        async def measure() -> float:
            start = time.perf_counter()
            chain = await crud.action.get_session_chain(clean_db, session_id=session_id)
            elapsed = time.perf_counter() - start
            assert len(chain) == 1000
            return elapsed

        median, samples = await median_seconds(measure, repeats=5)
        print(
            f"\n  1000-row chain retrieval: median {median * 1000:.1f}ms"
            f"  samples: {format_samples(samples)}"
        )
        assert median < 0.5, (
            f"Chain retrieval median {median:.3f}s — exceeds 500ms. "
            f"Samples: {format_samples(samples)}. Hardware/environment dependent."
        )

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

        # Seeded once above; only verify_chain is sampled.
        async def measure() -> float:
            start = time.perf_counter()
            result = await crud.action.verify_chain(clean_db, session_id=session_id)
            elapsed = time.perf_counter() - start
            assert result["valid"] is True
            assert result["record_count"] == 1000
            return elapsed

        median, samples = await median_seconds(measure, repeats=5)
        print(
            f"\n  verify_chain(1000): median {median * 1000:.1f}ms"
            f"  samples: {format_samples(samples)}"
        )
        # The 2s budget the test name promises was never actually asserted —
        # elapsed was measured, printed, and dropped. Enforced now. There is
        # ~42x headroom (median ~47ms), so this is closing a gap rather than
        # tightening anything.
        assert median < 2.0, (
            f"verify_chain(1000) median {median:.3f}s — exceeds 2.0s limit. "
            f"Samples: {format_samples(samples)}. Hardware/environment dependent."
        )
