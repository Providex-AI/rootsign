"""Sampling helper for the performance budgets.

Why medians
-----------
Every budget in this directory traces to an acceptance criterion, so the
thresholds are fixed — they are contract, not tuning knobs. What a single
`time.perf_counter()` sample measures, though, is the threshold *plus* whatever
else the machine was doing: a background build, a container checkpoint, a noisy
neighbour on a shared runner, a cold page cache. That variance is what turned
`main` red on 2026-08-18 at 0.203s against a 200ms budget with no code change.

Taking the median of several runs keeps the AC honest while making the
measurement insensitive to one-off stalls: a single slow sample moves the
median very little, whereas a real regression moves every sample and therefore
moves the median too. A mean would let one outlier drag the result; the median
is the point of using it.

These numbers remain hardware- and environment-dependent by nature. A failure
here is a signal to investigate, not proof of a regression — check the machine
is otherwise idle, and compare the reported samples, before concluding anything.
"""

from __future__ import annotations

import statistics
from collections.abc import Awaitable, Callable


async def median_seconds(
    measure: Callable[[], Awaitable[float]],
    *,
    repeats: int = 5,
) -> tuple[float, list[float]]:
    """Run *measure* `repeats` times and return `(median, all_samples)`.

    `measure` is an async callable that performs its own (untimed) setup, times
    only the operation under test, and returns that elapsed span in seconds.
    Keeping the timing inside the callable rather than around it means per-run
    fixture work — fresh sessions, seeding, truncation — never lands in the
    sample.

    `repeats` defaults to 5. Use 3 for a budget whose single run is already
    seconds long; the point is to outvote a stray outlier, and 3 does that.
    """
    if repeats < 1:
        raise ValueError("repeats must be >= 1")
    samples = [await measure() for _ in range(repeats)]
    return statistics.median(samples), samples


def format_samples(samples: list[float], *, unit: str = "ms") -> str:
    """Render samples for the benchmark's stdout line, so a CI failure shows
    the spread rather than just the one number that tripped the assertion."""
    scale = 1000.0 if unit == "ms" else 1.0
    return " ".join(f"{s * scale:.1f}{unit}" for s in samples)
