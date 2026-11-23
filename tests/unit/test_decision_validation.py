"""A decision is checked the same way on every backend before it is stored.

PostgreSQL validates a DECISION_RECORD with `DecisionRecordPayload`; the JSONL
backend stored whatever it was given. `record_decision(confidence=1.5)` wrote
a decision on JSONL and returned its id, while on PostgreSQL the store refused
it, a WARNING carried a pydantic dump, and the action was left unlinked. Now
the SDK validates first, on every backend: an invalid decision is not stored,
`record_decision()` returns None with one WARNING naming the field, and the
agent keeps running (ADR-002).

Each case runs as its own Python process on the JSONL backend.
"""

from __future__ import annotations

import textwrap

import pytest

from tests.support.docs_pages import run_script

AGENT = textwrap.dedent(
    """
    import asyncio
    import logging
    import os
    import sys

    import rootsign

    logging.basicConfig(stream=sys.stdout, format="%(levelname)s %(message)s")
    os.environ["ROOTSIGN_CAPTURE_DECISIONS"] = "true"
    rootsign.init(agent="decision-check")


    @rootsign.trace()
    async def refund(order_id: str) -> str:
        return "refunded"


    async def main() -> None:
        async with rootsign.session(objective="validate decisions") as ctx:
            decision_id = await ctx.record_decision({kwargs})
            await refund("ord-1")
        print("returned", decision_id)


    asyncio.run(main())
    """
)


def _run(tmp_path, kwargs: str):
    run = run_script(AGENT.format(kwargs=kwargs), tmp_path, name="agent.py")
    records = [r for r in _records(run)]
    decisions = [r for r in records if r["event_type"] == "DECISION_RECORD"]
    (action,) = [r for r in records if r["event_type"] == "ACTION_RECORD"]
    return run.stdout, decisions, action


def _records(run):
    import json

    lines = run.only_session().read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ('selected_action="refund", confidence=1.5', "confidence"),
        ('selected_action="refund", confidence=-0.1', "confidence"),
        ('selected_action=""', "selected_action"),
        ('selected_action="r" * 201', "selected_action"),
    ],
)
def test_an_invalid_decision_is_not_stored(tmp_path, kwargs, field):
    printed, decisions, action = _run(tmp_path, kwargs)

    assert "returned None" in printed
    assert decisions == []
    assert action["decision_id"] is None
    (warning,) = [line for line in printed.splitlines() if line.startswith("WARNING")]
    assert "decision not recorded" in warning
    assert field in warning


def test_a_valid_decision_is_still_stored(tmp_path):
    printed, decisions, action = _run(
        tmp_path, 'selected_action="refund", confidence=1.0, reasoning_summary="in policy"'
    )

    assert "returned None" not in printed
    assert "WARNING" not in printed
    (decision,) = decisions
    assert action["decision_id"] == decision["decision_id"]
