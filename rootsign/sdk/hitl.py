"""HiTL (Human-in-the-Loop) checkpoint — async poll loop for `@rootsign.trace(require_approval=True)`.

When a tool is decorated with `require_approval=True`, the trace decorator
inserts the ACTION_RECORD with `authorization_status='pending'`, instantiates
this checkpoint, and blocks on `wait_for_approval(...)` until a human (or
the timeout) resolves it via `rootsign approve <id>` (user CLI).

Design — Sprint 4 ADR-007:

* **Async poll loop.** Each cycle opens its OWN AsyncSession via
  `session_factory()`. Reusing a session across awaits would re-bind the
  asyncpg Future to whichever loop is current — fine in production, broken
  in pytest-asyncio (which creates a fresh loop per test). Per-cycle
  sessions also let the CLI's commit on a separate connection become
  immediately visible (READ COMMITTED — the default isolation).

* **Sequence number reserved at submission.** The ACTION_RECORD was
  inserted with a reserved seq # BEFORE this checkpoint started polling.
  APPROVAL_RECORD is a separate entity with its own table — it does NOT
  appear in the Action hash chain. `verify_chain` sees a complete chain
  with no gaps regardless of how long the human took.

* **Timeout produces TIMED_OUT (terminal).** When the deadline elapses
  with no human response, `_record_timeout` writes an APPROVAL_RECORD
  with `approver_type='timeout_auto_rejected'` and sets the Action's
  `authorization_status='timed_out'`. The forensic distinction from
  `'human_rejected'` survives in the audit trail forever.

* **Race tolerance.** A human's `rootsign approve` commit can land in the
  same window as the poll loop's timeout fires. The DB write order
  guarantees one wins — whichever transaction commits second hits the
  terminal-state guard in `CRUDApproval.create_with_chain_link` and
  raises `ActionAlreadyResolvedError`. The poll loop catches that error,
  re-reads the Approval row, and returns the human's decision. **The
  human always wins ties.**
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Callable
from uuid import UUID

from sqlalchemy import select

from rootsign.crud.approval import approval as approval_crud
from rootsign.errors import (
    ActionAlreadyResolvedError,
    HiTLRejectedError,
    HiTLTimeoutError,
)
from rootsign.models.approval import Approval

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("rootsign.sdk.hitl")


class ApprovalDecision(str, Enum):
    """The three terminal states a HiTL checkpoint can resolve into.

    Maps onto Action.authorization_status as follows:
        APPROVED   → human_approved
        REJECTED   → human_rejected  (and raises HiTLRejectedError)
        TIMED_OUT  → timed_out       (and raises HiTLTimeoutError)
    """

    APPROVED = "approved"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"


@dataclass
class HiTLResult:
    """Successful HiTL resolution — returned only on APPROVED.

    For REJECTED / TIMED_OUT the checkpoint raises rather than returning,
    so callers don't have to branch on the decision field. This struct
    exists so the trace decorator can include the approver_id in its
    follow-up APPROVAL_RECORD envelope.
    """

    decision: ApprovalDecision
    approval_id: UUID | None = None
    approver_id: str | None = None


# Type alias: callable returning an AsyncSession async context manager.
# In production this is `rootsign.database.AsyncSessionLocal`; in tests
# it's a factory that yields a mock session.
SessionFactory = Callable[[], "AsyncSession"]


class HiTLCheckpoint:
    """Async poll loop that waits for an external decision on a pending Action.

    Construct one per Action awaiting approval; call `wait_for_approval()`
    exactly once. Not reusable — the deadline math and timeout-write are
    one-shot.
    """

    def __init__(
        self,
        *,
        action_id: UUID,
        action_timestamp: datetime,
        session_factory: SessionFactory,
        poll_interval_seconds: float = 2.0,
        timeout_seconds: float = 300.0,
    ):
        self.action_id = action_id
        self.action_timestamp = action_timestamp
        self.session_factory = session_factory
        self.poll_interval = poll_interval_seconds
        self.timeout = timeout_seconds

    async def wait_for_approval(self, *, context_presented: dict) -> HiTLResult:
        """Block until the Action resolves, timeout fires, or rejection lands.

        Args:
            context_presented: The dict the operator will see via
                `rootsign approve --list`. Persisted on the timeout-path
                APPROVAL_RECORD so the audit trail records what the human
                was offered (even if they never saw it).

        Returns:
            HiTLResult with decision=APPROVED on human approval.

        Raises:
            HiTLRejectedError: a human explicitly rejected via
                `rootsign approve --reject`.
            HiTLTimeoutError: the deadline elapsed with no resolution.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        logger.info(
            "HiTL waiting for approval action_id=%s timeout=%ss",
            self.action_id,
            self.timeout,
        )

        while loop.time() < deadline:
            await asyncio.sleep(self.poll_interval)
            approval = await self._poll_once()
            if approval is not None and approval.decision in ("approved", "rejected"):
                return self._raise_or_return(approval)

        # Deadline elapsed. Try to write the timeout record — if a human
        # raced us and committed first, _record_timeout will re-poll and
        # surface their decision instead.
        racing = await self._record_timeout(context_presented=context_presented)
        if racing is not None:
            logger.info(
                "HiTL timeout raced by human action_id=%s decision=%s",
                self.action_id,
                racing.decision,
            )
            return self._raise_or_return(racing)

        raise HiTLTimeoutError(self.action_id, self.timeout)

    async def _poll_once(self) -> Approval | None:
        """Fetch the most-recent Approval row for this Action, if any.

        Fresh session per call so the asyncpg connection stays bound to
        the current loop and the CLI's external commit is immediately
        visible (READ COMMITTED).
        """
        async with self.session_factory() as poll_db:
            result = await poll_db.execute(
                select(Approval)
                .where(Approval.action_id == self.action_id)
                .order_by(Approval.timestamp.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def _record_timeout(
        self, *, context_presented: dict
    ) -> Approval | None:
        """Write the timeout APPROVAL_RECORD. Return the racing human's row
        on conflict, None on a clean timeout.

        The terminal-state guard in `CRUDApproval.create_with_chain_link`
        is the contention point: whichever transaction commits first wins.
        If a human's approve transaction committed in the deadline-fire
        window, our INSERT will lose and the CRUD raises
        ActionAlreadyResolvedError; we re-poll and return their row so
        `wait_for_approval` surfaces the human decision rather than a
        spurious timeout.
        """
        try:
            async with self.session_factory() as db:
                await approval_crud.create_with_chain_link(
                    db,
                    action_id=self.action_id,
                    action_timestamp=self.action_timestamp,
                    approver_id="system:timeout",
                    approver_type="timeout_auto_rejected",
                    context_presented=context_presented,
                    decision="rejected",
                    decision_reason=f"No response within {int(self.timeout)}s",
                )
                await db.commit()
            return None
        except ActionAlreadyResolvedError:
            # Race lost — a human approve/reject committed in the gap
            # between our last poll and the timeout fire. Re-poll one
            # last time so the caller gets the actual decision.
            return await self._poll_once()

    def _raise_or_return(self, approval: Approval) -> HiTLResult:
        """Convert an Approval row into the appropriate caller-visible outcome.

        Rejection is a raise, not a return, so trace-decorator call sites
        don't have to inspect a decision field — `except HiTLRejectedError`
        is enough.
        """
        if approval.decision == "rejected":
            reason = getattr(approval, "decision_reason", None)
            raise HiTLRejectedError(self.action_id, reason=reason)
        return HiTLResult(
            decision=ApprovalDecision.APPROVED,
            approval_id=approval.approval_id,
            approver_id=approval.approver_id,
        )
