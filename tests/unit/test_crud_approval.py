"""Unit tests for CRUDApproval.create_with_chain_link.

The hypertable-safe HiTL/CLI entry point. These tests cover the contract
in isolation (mocked AsyncSession) — actual SQL execution is exercised by
the HiTL integration tests in tests/integration/test_hitl_*.py.

Three invariants are pinned here:

1. **Hypertable lookup** — the Action lookup must include `timestamp` in
   the WHERE clause. Sprint 4 Flag 5: single-column FK lookups against the
   actions hypertable either fail or silently return wrong rows.

2. **Terminal-state guard** — all four terminal states ('human_approved',
   'human_rejected', 'timed_out') reject further approvals with
   `ActionAlreadyResolvedError`. The HiTLCheckpoint poll loop relies on
   this to detect the human-vs-timeout race.

3. **Timeout sentinel** — `approver_type='timeout_auto_rejected'` overrides
   the decision-to-status mapping and forces `authorization_status='timed_out'`.
   This is how the poll loop records an abandoned action without changing
   the Approval.decision contract (CHECK constraint still requires
   approved/rejected/escalated).
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from rootsign.crud.approval import CRUDApproval, approval as approval_crud
from rootsign.errors import (
    ActionAlreadyResolvedError,
    ActionNotFoundError,
    IngestValidationError,
)


def _mock_db_with_action(authorization_status: str | None) -> tuple[MagicMock, MagicMock | None]:
    """Build a mock AsyncSession that returns an Action with the given status.

    Pass `authorization_status=None` to simulate "action not found".

    Mock layout:
      * `db.execute` is an AsyncMock — code does `await db.execute(...)`.
      * Its return value (the `Result` object) is a sync MagicMock — code
        does `(...).scalar_one_or_none()` synchronously.
      * `db.flush` is AsyncMock; `db.add` is sync.

    `AsyncMock()` recursively makes every child async, which would turn
    `Result.scalar_one_or_none()` into a coroutine — wrong shape for the
    SQLAlchemy contract. Hence the explicit composition.
    """
    if authorization_status is None:
        action = None
    else:
        action = MagicMock()
        action.authorization_status = authorization_status
        action.session_id = uuid4()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = action
    db = MagicMock()
    db.execute = AsyncMock(return_value=result_mock)
    db.add = MagicMock()
    db.flush = AsyncMock()
    return db, action


class TestSignature:
    """The method must be keyword-only (Sprint 4 Flag 1)."""

    def test_method_is_keyword_only(self):
        sig = inspect.signature(CRUDApproval.create_with_chain_link)
        # `self` is positional-or-keyword; `db` is positional-or-keyword
        # (callers pass it positionally as the first arg after the
        # instance). Everything after the * marker must be keyword-only.
        params = list(sig.parameters.values())
        keyword_only = [
            p for p in params if p.kind == inspect.Parameter.KEYWORD_ONLY
        ]
        names = {p.name for p in keyword_only}
        assert {
            "action_id",
            "action_timestamp",
            "approver_id",
            "approver_type",
            "context_presented",
            "decision",
            "decision_reason",
            "parent_approval_id",
            "response_latency_ms",
        }.issubset(names)


class TestActionLookup:
    async def test_raises_if_action_not_found(self):
        db, _ = _mock_db_with_action(authorization_status=None)
        with pytest.raises(ActionNotFoundError):
            await approval_crud.create_with_chain_link(
                db,
                action_id=uuid4(),
                action_timestamp=datetime.now(timezone.utc),
                approver_id="user@test.com",
                approver_type="human",
                context_presented={},
                decision="approved",
            )

    async def test_lookup_joins_on_action_id_and_timestamp(self):
        """The SELECT must reference both action_id AND timestamp.

        Sprint 4 Flag 5: lookups against the actions hypertable that miss
        the timestamp column either fail at the DB or return non-unique
        rows. Compile the statement and verify both columns are in the
        WHERE clause.
        """
        db, _ = _mock_db_with_action(authorization_status="pending")
        action_id = uuid4()
        action_ts = datetime.now(timezone.utc)
        await approval_crud.create_with_chain_link(
            db,
            action_id=action_id,
            action_timestamp=action_ts,
            approver_id="user@test.com",
            approver_type="human",
            context_presented={},
            decision="approved",
        )
        # First execute call is the SELECT — compile it and look for both
        # column references.
        select_stmt = db.execute.call_args_list[0].args[0]
        compiled = str(select_stmt.compile(compile_kwargs={"literal_binds": False}))
        assert "actions.action_id" in compiled
        assert "actions.timestamp" in compiled


class TestTerminalStateGuard:
    @pytest.mark.parametrize(
        "terminal_status",
        ["human_approved", "human_rejected", "timed_out"],
    )
    async def test_raises_if_action_already_terminal(self, terminal_status: str):
        db, _ = _mock_db_with_action(authorization_status=terminal_status)
        with pytest.raises(ActionAlreadyResolvedError) as exc_info:
            await approval_crud.create_with_chain_link(
                db,
                action_id=uuid4(),
                action_timestamp=datetime.now(timezone.utc),
                approver_id="user@test.com",
                approver_type="human",
                context_presented={},
                decision="approved",
            )
        assert terminal_status in str(exc_info.value)

    async def test_pending_state_is_not_terminal(self):
        db, _ = _mock_db_with_action(authorization_status="pending")
        # Should not raise — pending is exactly the state HiTL is resolving.
        result = await approval_crud.create_with_chain_link(
            db,
            action_id=uuid4(),
            action_timestamp=datetime.now(timezone.utc),
            approver_id="user@test.com",
            approver_type="human",
            context_presented={},
            decision="approved",
        )
        assert result is not None


class TestDecisionToStatusMapping:
    """The approver_type='timeout_auto_rejected' sentinel must override
    the standard decision→status mapping (Sprint 4 spec §2.3)."""

    @staticmethod
    def _captured_update_status(db: AsyncMock) -> str:
        """Extract the `authorization_status` value from the captured
        UPDATE statement (the second `execute` call)."""
        update_stmt = db.execute.call_args_list[1].args[0]
        # SQLAlchemy stores ValuesBase parameters on the statement; pull
        # the authorization_status binding directly.
        params = update_stmt.compile().params
        return params["authorization_status"]

    async def test_approved_maps_to_human_approved(self):
        db, _ = _mock_db_with_action(authorization_status="pending")
        await approval_crud.create_with_chain_link(
            db,
            action_id=uuid4(),
            action_timestamp=datetime.now(timezone.utc),
            approver_id="user@test.com",
            approver_type="human",
            context_presented={},
            decision="approved",
        )
        assert self._captured_update_status(db) == "human_approved"

    async def test_rejected_maps_to_human_rejected(self):
        db, _ = _mock_db_with_action(authorization_status="pending")
        await approval_crud.create_with_chain_link(
            db,
            action_id=uuid4(),
            action_timestamp=datetime.now(timezone.utc),
            approver_id="user@test.com",
            approver_type="human",
            context_presented={},
            decision="rejected",
        )
        assert self._captured_update_status(db) == "human_rejected"

    async def test_timeout_sentinel_forces_timed_out_status(self):
        """The whole point of Sprint 4 Flag 4 — timeout produces a distinct
        terminal state."""
        db, _ = _mock_db_with_action(authorization_status="pending")
        await approval_crud.create_with_chain_link(
            db,
            action_id=uuid4(),
            action_timestamp=datetime.now(timezone.utc),
            approver_id="system:timeout",
            approver_type="timeout_auto_rejected",
            context_presented={},
            decision="rejected",  # decision is 'rejected' to satisfy CHECK constraint
            decision_reason="No response within 300s",
        )
        # But the Action.authorization_status is 'timed_out', not 'human_rejected'.
        assert self._captured_update_status(db) == "timed_out"

    async def test_unknown_decision_raises_validation_error(self):
        db, _ = _mock_db_with_action(authorization_status="pending")
        with pytest.raises(IngestValidationError):
            await approval_crud.create_with_chain_link(
                db,
                action_id=uuid4(),
                action_timestamp=datetime.now(timezone.utc),
                approver_id="user@test.com",
                approver_type="human",
                context_presented={},
                decision="maybe",
            )


class TestApprovalRecordFields:
    async def test_approval_row_inherits_action_session_id(self):
        """Approval.session_id is read from the looked-up Action, not from
        the caller. The HiTL poll loop / CLI don't have the session_id in
        scope — only the action does."""
        db, action = _mock_db_with_action(authorization_status="pending")
        await approval_crud.create_with_chain_link(
            db,
            action_id=uuid4(),
            action_timestamp=datetime.now(timezone.utc),
            approver_id="user@test.com",
            approver_type="human",
            context_presented={},
            decision="approved",
        )
        # The Approval object passed to db.add() must have session_id == action.session_id
        added_obj = db.add.call_args.args[0]
        assert added_obj.session_id == action.session_id

    async def test_decision_reason_persisted(self):
        db, _ = _mock_db_with_action(authorization_status="pending")
        await approval_crud.create_with_chain_link(
            db,
            action_id=uuid4(),
            action_timestamp=datetime.now(timezone.utc),
            approver_id="cli:operator",
            approver_type="human",
            context_presented={},
            decision="rejected",
            decision_reason="Too risky",
        )
        added_obj = db.add.call_args.args[0]
        assert added_obj.decision_reason == "Too risky"
