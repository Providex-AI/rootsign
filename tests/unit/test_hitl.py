"""Unit tests for `HiTLCheckpoint` — the async poll loop that waits for
an external decision on a pending Action.

Three behaviors are pinned:

1. **Approved** path returns `HiTLResult(decision=APPROVED, ...)` carrying
   the approval_id and approver_id from the Approval row.

2. **Rejected** path raises `HiTLRejectedError` with the
   `decision_reason` propagated as `.reason`.

3. **Timeout** path raises `HiTLTimeoutError` and (best-effort) writes
   the timeout APPROVAL_RECORD via the session factory.

4. **Race** path: a human's `rootsign approve` commit lands in the same
   window as the timeout fire. The CRUD's terminal-state guard raises
   `ActionAlreadyResolvedError`; the checkpoint catches it, re-polls,
   and returns the human's decision. The human always wins ties (ADR-007).

The mock session factory uses a small in-test context-manager class
rather than `AsyncMock()` directly — `AsyncMock` recursively makes every
child async, which breaks `Result.scalar_one_or_none()` (sync) and the
`session.add()` calls.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from rootsign.errors import (
    ActionAlreadyResolvedError,
    HiTLRejectedError,
    HiTLTimeoutError,
)
from rootsign.sdk.hitl import ApprovalDecision, HiTLCheckpoint, HiTLResult


# ---------------------------------------------------------------------------
# Mock session factory
# ---------------------------------------------------------------------------


class _MockSession:
    """Stand-in for an AsyncSession returned by `session_factory()`.

    Per poll cycle the factory returns one of these. The Result object
    returned by `await session.execute(...)` is a sync MagicMock whose
    `scalar_one_or_none()` yields the configured Approval (or None).
    """

    def __init__(self, *, approval: object | None = None):
        result = MagicMock()
        result.scalar_one_or_none.return_value = approval
        self.execute = AsyncMock(return_value=result)
        self.commit = AsyncMock()
        self.add = MagicMock()
        self.flush = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


def _make_factory(
    *,
    poll_sequence: list[object | None] | None = None,
    constant: object | None = None,
):
    """Build a session_factory callable + a MagicMock spy on its calls.

    If `poll_sequence` is given, sessions return its values one-per-call
    (None thereafter). Otherwise every session returns `constant`.
    """
    if poll_sequence is not None:
        seq = list(poll_sequence)

        def _factory_call():
            value = seq.pop(0) if seq else None
            return _MockSession(approval=value)

    else:

        def _factory_call():
            return _MockSession(approval=constant)

    spy = MagicMock(side_effect=_factory_call)
    return spy


def _mock_approval(
    decision: str, reason: str | None = None, approver_type: str = "human"
) -> MagicMock:
    """Build a stand-in Approval row with just the fields the checkpoint reads."""
    approval = MagicMock()
    approval.decision = decision
    approval.decision_reason = reason
    approval.approver_type = approver_type
    approval.approval_id = uuid4()
    approval.approver_id = "user@test.com"
    return approval


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestApproved:
    async def test_returns_hitlresult_with_approver_metadata(self):
        approved = _mock_approval(decision="approved")
        factory = _make_factory(constant=approved)
        checkpoint = HiTLCheckpoint(
            action_id=uuid4(),
            action_timestamp=datetime.now(timezone.utc),
            session_factory=factory,
            poll_interval_seconds=0.01,
            timeout_seconds=5.0,
        )

        result = await checkpoint.wait_for_approval(context_presented={"tool": "x"})

        assert isinstance(result, HiTLResult)
        assert result.decision is ApprovalDecision.APPROVED
        assert result.approval_id == approved.approval_id
        assert result.approver_id == approved.approver_id


class TestRejected:
    async def test_raises_hitlrejectederror_with_reason(self):
        rejected = _mock_approval(decision="rejected", reason="Looks risky")
        factory = _make_factory(constant=rejected)
        checkpoint = HiTLCheckpoint(
            action_id=uuid4(),
            action_timestamp=datetime.now(timezone.utc),
            session_factory=factory,
            poll_interval_seconds=0.01,
            timeout_seconds=5.0,
        )

        with pytest.raises(HiTLRejectedError, match="Looks risky") as exc_info:
            await checkpoint.wait_for_approval(context_presented={})

        assert exc_info.value.reason == "Looks risky"

    async def test_rejected_without_reason_still_raises(self):
        rejected = _mock_approval(decision="rejected", reason=None)
        factory = _make_factory(constant=rejected)
        checkpoint = HiTLCheckpoint(
            action_id=uuid4(),
            action_timestamp=datetime.now(timezone.utc),
            session_factory=factory,
            poll_interval_seconds=0.01,
            timeout_seconds=5.0,
        )
        with pytest.raises(HiTLRejectedError) as exc_info:
            await checkpoint.wait_for_approval(context_presented={})
        assert exc_info.value.reason is None


class TestTimeout:
    async def test_no_approval_in_window_raises_timeout(self):
        # poll_sequence empty → every poll returns None
        factory = _make_factory(constant=None)
        action_id = uuid4()
        checkpoint = HiTLCheckpoint(
            action_id=action_id,
            action_timestamp=datetime.now(timezone.utc),
            session_factory=factory,
            poll_interval_seconds=0.01,
            timeout_seconds=0.03,  # ~3 polls then timeout
        )
        # Patch the CRUD call so we don't try to insert against a mock
        # without a real Action row — _record_timeout will succeed (None).
        with patch(
            "rootsign.crud.approval.approval.create_with_chain_link",
            new_callable=AsyncMock,
        ) as mocked_crud:
            with pytest.raises(HiTLTimeoutError) as exc_info:
                await checkpoint.wait_for_approval(context_presented={"x": 1})
        assert exc_info.value.action_id == action_id
        # Timeout path wrote the timeout record
        mocked_crud.assert_awaited_once()
        kwargs = mocked_crud.await_args.kwargs
        assert kwargs["approver_id"] == "system:timeout"
        assert kwargs["approver_type"] == "timeout_auto_rejected"
        assert kwargs["decision"] == "rejected"
        assert "No response within" in kwargs["decision_reason"]

    async def test_timeout_opens_fresh_session_per_poll_cycle(self):
        """Flag 4 lock: each poll must open its own session, never reuse
        one across cycles."""
        factory = _make_factory(constant=None)
        checkpoint = HiTLCheckpoint(
            action_id=uuid4(),
            action_timestamp=datetime.now(timezone.utc),
            session_factory=factory,
            poll_interval_seconds=0.01,
            timeout_seconds=0.05,
        )
        with patch(
            "rootsign.crud.approval.approval.create_with_chain_link",
            new_callable=AsyncMock,
        ):
            with pytest.raises(HiTLTimeoutError):
                await checkpoint.wait_for_approval(context_presented={})
        # At least N poll-session opens + 1 for the timeout-write session.
        # We don't pin an exact count because event-loop scheduling jitter
        # makes the poll count non-deterministic; just confirm multiple.
        assert factory.call_count >= 2


class TestHumanWinsRace:
    """The race-tolerance contract from ADR-007: if a human's commit lands
    between the last poll and the timeout-write, the human wins."""

    async def test_record_timeout_catches_action_already_resolved_and_returns_human_approval(
        self,
    ):
        """Simulate: deadline fires, the timeout-INSERT is blocked because
        the action transitioned to 'human_approved' between the last poll
        and the write. The checkpoint re-polls and returns APPROVED.

        Uses timeout_seconds=0 so the wait loop body never executes —
        only the post-deadline race-detection path runs. That way the
        poll_sequence isn't burned on irrelevant in-loop polls and the
        re-poll's session is the FIRST factory call.
        """
        human_approval = _mock_approval(decision="approved")
        # With timeout=0, no in-loop poll happens. _record_timeout opens
        # one session for the INSERT (call #1) and then the re-poll opens
        # another (call #2). Only the re-poll's session reads an Approval,
        # so the sequence here is consumed in INSERT/re-poll order — the
        # INSERT session's scalar_one_or_none() is never invoked.
        polls = [None, human_approval]
        factory = _make_factory(poll_sequence=polls)
        checkpoint = HiTLCheckpoint(
            action_id=uuid4(),
            action_timestamp=datetime.now(timezone.utc),
            session_factory=factory,
            poll_interval_seconds=0.01,
            timeout_seconds=0.0,
        )
        with patch(
            "rootsign.crud.approval.approval.create_with_chain_link",
            new_callable=AsyncMock,
            side_effect=ActionAlreadyResolvedError("already terminal"),
        ):
            result = await checkpoint.wait_for_approval(context_presented={})

        assert isinstance(result, HiTLResult)
        assert result.decision is ApprovalDecision.APPROVED
        assert result.approval_id == human_approval.approval_id

    async def test_race_with_human_rejection_raises_hitlrejected_not_timeout(self):
        """The race-tolerance must apply to rejection too — if a human
        rejected just before the timeout fire, the caller sees
        HiTLRejectedError, not HiTLTimeoutError. The forensic distinction
        matters: it was the human's call, not an abandonment."""
        rejection = _mock_approval(decision="rejected", reason="Nope")
        polls = [None, rejection]
        factory = _make_factory(poll_sequence=polls)
        checkpoint = HiTLCheckpoint(
            action_id=uuid4(),
            action_timestamp=datetime.now(timezone.utc),
            session_factory=factory,
            poll_interval_seconds=0.01,
            timeout_seconds=0.0,
        )
        with patch(
            "rootsign.crud.approval.approval.create_with_chain_link",
            new_callable=AsyncMock,
            side_effect=ActionAlreadyResolvedError("already terminal"),
        ):
            with pytest.raises(HiTLRejectedError, match="Nope"):
                await checkpoint.wait_for_approval(context_presented={})


class TestTimeoutClassification:
    """audit #10a: a timeout row has decision='rejected' but
    approver_type='timeout_auto_rejected'. It must surface as
    HiTLTimeoutError, never HiTLRejectedError — the timed_out vs
    human_rejected forensic split (ADR-007) depends on it."""

    async def test_timeout_row_raises_timeout_not_rejected(self):
        timeout_row = _mock_approval(
            decision="rejected",
            reason="No response within 300s",
            approver_type="timeout_auto_rejected",
        )
        action_id = uuid4()
        factory = _make_factory(constant=timeout_row)
        checkpoint = HiTLCheckpoint(
            action_id=action_id,
            action_timestamp=datetime.now(timezone.utc),
            session_factory=factory,
            poll_interval_seconds=0.01,
            timeout_seconds=5.0,
        )
        with pytest.raises(HiTLTimeoutError) as exc_info:
            await checkpoint.wait_for_approval(context_presented={})
        assert exc_info.value.action_id == action_id

    async def test_human_rejection_still_raises_rejected(self):
        """Guard against over-correction: an explicit human rejection keeps
        raising HiTLRejectedError."""
        rejection = _mock_approval(decision="rejected", reason="Nope", approver_type="human")
        factory = _make_factory(constant=rejection)
        checkpoint = HiTLCheckpoint(
            action_id=uuid4(),
            action_timestamp=datetime.now(timezone.utc),
            session_factory=factory,
            poll_interval_seconds=0.01,
            timeout_seconds=5.0,
        )
        with pytest.raises(HiTLRejectedError, match="Nope"):
            await checkpoint.wait_for_approval(context_presented={})


class TestPollBeforeSleep:
    """audit #10a: the loop must poll before sleeping so an already-resolved
    action returns immediately instead of eating a full poll_interval."""

    async def test_already_resolved_returns_without_sleeping(self):
        approved = _mock_approval(decision="approved")
        factory = _make_factory(constant=approved)
        checkpoint = HiTLCheckpoint(
            action_id=uuid4(),
            action_timestamp=datetime.now(timezone.utc),
            session_factory=factory,
            poll_interval_seconds=999.0,  # would hang if we slept first
            timeout_seconds=5.0,
        )
        with patch("rootsign.sdk.hitl.asyncio.sleep", new_callable=AsyncMock) as mocked_sleep:
            result = await checkpoint.wait_for_approval(context_presented={})
        assert result.decision is ApprovalDecision.APPROVED
        mocked_sleep.assert_not_awaited()


class TestConstructor:
    def test_defaults_match_spec(self):
        """Spec §2.4: poll_interval=2.0s, timeout=300s."""
        checkpoint = HiTLCheckpoint(
            action_id=uuid4(),
            action_timestamp=datetime.now(timezone.utc),
            session_factory=_make_factory(),
        )
        assert checkpoint.poll_interval == 2.0
        assert checkpoint.timeout == 300.0

    def test_all_args_keyword_only(self):
        """Mirrors Flag 1 for the constructor — caller can't accidentally
        swap action_id and action_timestamp positionally."""
        with pytest.raises(TypeError):
            HiTLCheckpoint(  # type: ignore[misc]
                uuid4(), datetime.now(timezone.utc), _make_factory()
            )
