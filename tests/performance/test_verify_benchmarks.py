"""`crud.action.verify_chain` performance benchmark.

Sprint 3 target: a 10 000-record session must verify in under 5 seconds.
That's the worst case for a single Show-HN-quality demo — anything slower
makes the CLI feel broken even though the chain is intact.

The session is seeded via the IngestHandler directly (no tracer) — the
benchmark is about verify_chain hash-recomputation cost, not interception.
A real DB is required; the `clean_db` fixture commits and TRUNCATEs.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from rootsign import crud
from rootsign.crud import action as action_crud
from rootsign.ingest import IdempotencyStore, IngestHandler
from rootsign.schemas import (
    AgentCreate,
    AgentEnvironment,
    AgentFramework,
    AgentRiskTier,
)

from tests.performance._bench import format_samples, median_seconds

pytestmark = pytest.mark.benchmark


def _envelope(*, event_type: str, agent_id: UUID, session_id: UUID, payload: dict) -> dict:
    return {
        "schema_version": "1.0",
        "sdk_version": "0.1.0",
        "event_type": event_type,
        "event_id": str(uuid4()),
        "emitted_at": datetime.now(timezone.utc).isoformat(),
        "agent_id": str(agent_id),
        "session_id": str(session_id),
        "payload": payload,
    }


def _action_payload(i: int) -> dict:
    return {
        "tool_name": f"bench_tool_{i % 16}",
        "input_hash": "a" * 64,
        "output_hash": "b" * 64,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "authorization_status": "auto_authorized",
    }


@pytest_asyncio.fixture
async def bench_agent(clean_db):
    return await crud.agent.create(
        clean_db,
        obj_in=AgentCreate(
            name=f"verify-bench-{uuid4().hex[:8]}",
            owner="test-team",
            environment=AgentEnvironment.PRODUCTION,
            risk_tier=AgentRiskTier.MEDIUM,
            framework=AgentFramework.LANGGRAPH,
        ),
    )


class TestVerifyBenchmark:
    @pytest.mark.benchmark
    async def test_10000_record_verify_under_5_seconds(self, clean_db, bench_agent):
        handler = IngestHandler(db=clean_db, idempotency=IdempotencyStore())
        session_id = uuid4()
        await handler.handle(
            _envelope(
                event_type="SESSION_OPEN",
                agent_id=bench_agent.agent_id,
                session_id=session_id,
                payload={"objective": "verify benchmark"},
            )
        )

        seed_start = time.perf_counter()
        for i in range(10_000):
            await handler.handle(
                _envelope(
                    event_type="ACTION_RECORD",
                    agent_id=bench_agent.agent_id,
                    session_id=session_id,
                    payload=_action_payload(i),
                )
            )
        await clean_db.commit()
        seed_elapsed = time.perf_counter() - seed_start
        print(f"\nseeded 10,000 records in {seed_elapsed:.2f}s")

        # The 41s seed above runs once and is not part of the budget; only
        # verify_chain is sampled, so repeating it is nearly free.
        async def measure() -> float:
            verify_start = time.perf_counter()
            result = await action_crud.verify_chain(clean_db, session_id=session_id)
            verify_elapsed = time.perf_counter() - verify_start
            assert result["valid"] is True
            assert result["record_count"] == 10_000
            return verify_elapsed

        median, samples = await median_seconds(measure, repeats=5)
        print(
            f"10,000-record verify: median {median:.3f}s  samples: {format_samples(samples)}"
        )
        assert median < 5.0, (
            f"verify_chain median {median:.2f}s — exceeds 5s budget. "
            f"Samples: {format_samples(samples)}. Hardware/environment dependent."
        )
