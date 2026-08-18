"""AC-3.12 — full session round-trip latency benchmark.

Target: SESSION_OPEN + 5 ACTION_RECORDs + SESSION_CLOSE in under 200ms.

Why this lives here and not in tests/integration/
-------------------------------------------------
It used to be `TestAC312_FullSessionRoundTripPerf` in
`tests/integration/test_ingest.py`, which the REQUIRED `Integration tests`
CI job runs by path. That made a wall-clock budget a merge gate: on
2026-08-18 it failed on `main` at 0.203s against the 0.2s limit — a 1.5%
overshoot with no code change, on a commit whose own PR run had passed the
same job twice minutes earlier. Locally the same test runs in 30-40ms, so
the budget is ~5-6x the real cost; the shared runner is simply slow enough
and variable enough to cross the line occasionally.

A timing assertion that goes red on `main` for reasons unrelated to the
diff teaches people to ignore red, which is worse than not measuring at
all. The budget still matters, so the test is preserved verbatim — it is
opt-in via `-m benchmark` rather than deleted or loosened. Loosening the
number was rejected: 200ms traces to an acceptance criterion, and moving
an AC's threshold to suit CI hardware would quietly redefine the AC.

Run it with:  python -m pytest tests/performance/ -m benchmark
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from rootsign import crud
from rootsign.ingest import IdempotencyStore, IngestHandler
from rootsign.schemas import (
    AgentCreate,
    AgentEnvironment,
    AgentFramework,
    AgentRiskTier,
)

from tests.performance._bench import format_samples, median_seconds

pytestmark = [pytest.mark.benchmark, pytest.mark.integration]


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


def _action_payload(*, tool_name: str) -> dict:
    return {
        "tool_name": tool_name,
        "input_hash": "a" * 64,
        "output_hash": "b" * 64,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "authorization_status": "auto_authorized",
    }


@pytest_asyncio.fixture
async def handler(clean_db):
    return IngestHandler(db=clean_db, idempotency=IdempotencyStore())


@pytest_asyncio.fixture
async def bench_agent(clean_db):
    return await crud.agent.create(
        clean_db,
        obj_in=AgentCreate(
            name=f"roundtrip-bench-{uuid4().hex[:8]}",
            owner="test-team",
            environment=AgentEnvironment.PRODUCTION,
            risk_tier=AgentRiskTier.MEDIUM,
            framework=AgentFramework.LANGGRAPH,
        ),
    )


class TestAC312_FullSessionRoundTripPerf:
    async def test_open_5_actions_close_under_200ms(self, handler, bench_agent):
        """Sampled 5x, asserted on the median. A single sample is what turned
        `main` red at 0.203s; the 200ms threshold itself is AC-3.12 and is
        unchanged."""

        async def measure() -> float:
            return await self._one_round_trip(handler, bench_agent)

        median, samples = await median_seconds(measure, repeats=5)
        print(
            f"\n  full round-trip (open + 5 actions + close): "
            f"median {median * 1000:.1f}ms  samples: {format_samples(samples)}"
        )
        assert median < 0.2, (
            f"Full round-trip median {median:.3f}s — exceeds 200ms (AC-3.12). "
            f"Samples: {format_samples(samples)}. Hardware/environment dependent."
        )

    @staticmethod
    async def _one_round_trip(handler, bench_agent) -> float:
        session_id = uuid4()

        start = time.perf_counter()
        r = await handler.handle(
            _envelope(
                event_type="SESSION_OPEN",
                agent_id=bench_agent.agent_id,
                session_id=session_id,
                payload={"objective": "round-trip benchmark"},
            )
        )
        assert r.status == "accepted"
        for i in range(5):
            r = await handler.handle(
                _envelope(
                    event_type="ACTION_RECORD",
                    agent_id=bench_agent.agent_id,
                    session_id=session_id,
                    payload=_action_payload(tool_name=f"t_{i}"),
                )
            )
            assert r.status == "accepted"
        await handler.handle(
            _envelope(
                event_type="SESSION_CLOSE",
                agent_id=bench_agent.agent_id,
                session_id=session_id,
                payload={"status": "completed"},
            )
        )
        return time.perf_counter() - start
