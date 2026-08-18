"""LangGraph interception overhead benchmark.

Sprint 2 target: p99 < 5 ms on 1 000 instrumented `noop_tool.invoke()` calls
with a mock IngestClient. The mock is used deliberately so the benchmark
isolates rootsign overhead and is not bottlenecked by DB latency — DB
performance is covered by the Phase 0 hash-chain benchmarks.

If this ever goes red, profile in this order:
  1. `json.dumps` in `compute_payload_hash` — a large output payload here is
     the most common culprit.
  2. The `_run_sync` thread fallback — when called inside a running loop,
     each call spins a thread. The contract / benchmark sync test is the
     "no running loop" branch, but profile to confirm.
  3. Redaction deep-copy — only relevant if the test runs with rules set.
"""

from __future__ import annotations

import statistics
import time
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

pytest.importorskip("langgraph", reason="LangGraph not installed")
pytest.importorskip("langchain_core", reason="langchain-core not installed")

from langchain_core.tools import tool  # noqa: E402

from rootsign.sdk.context import SessionContext  # noqa: E402
from rootsign.sdk.frameworks.langgraph import LangGraphTracer  # noqa: E402


@tool
def noop_tool(x: int) -> int:
    """No-op tool for benchmarking — just echoes its input."""
    return x


class TestLangGraphOverhead:
    @pytest.mark.benchmark
    def test_p99_overhead_under_5ms(self):
        """Left on a single pass deliberately — unlike the other budgets here,
        this one is already a distribution: it takes 1,000 samples and asserts
        on the p99, so a stray stall is absorbed by construction. It also has
        ~15x headroom (p99 ~0.33ms against 5ms), and re-running 1,000 calls
        several times would cost far more than it buys. Still hardware- and
        environment-dependent: treat a failure as a prompt to investigate on an
        idle machine, not as proof of a regression.
        """
        mock_client = AsyncMock()
        mock_client.handle.return_value = MagicMock(
            status="accepted",
            entity_id=uuid4(),
            sequence_number=1,
            self_hash="a" * 64,
        )
        ctx = SessionContext(agent_id=uuid4())
        wrapped = LangGraphTracer.wrap_tool(noop_tool, ctx=ctx, client=mock_client)

        # Warm-up — first invoke amortises import / one-time setup costs that
        # shouldn't show up in the p99.
        wrapped.invoke({"x": 0})

        n = 1000
        times: list[float] = []
        for i in range(n):
            t0 = time.perf_counter()
            wrapped.invoke({"x": i})
            times.append((time.perf_counter() - t0) * 1000.0)  # ms

        p99 = statistics.quantiles(times, n=100)[98]
        mean = statistics.mean(times)
        median = statistics.median(times)
        print(f"\np99={p99:.2f}ms  mean={mean:.2f}ms  median={median:.2f}ms")

        assert p99 < 5.0, (
            f"p99 latency {p99:.2f}ms exceeds 5ms budget. Profile per the "
            "module docstring before relaxing the threshold."
        )
