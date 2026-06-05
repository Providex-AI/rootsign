"""CRUD for Approval — owns the approval insert + atomic Action authorization update.

The interesting method is `create_with_action_status_update`. It guards every
HiTL invariant the spec defines:
  * Action must exist
  * Action must not already be in a terminal authorization state
  * If parent_approval_id is supplied: parent must exist for the SAME action
    and its decision must be 'escalated'
  * 2-level escalation only: a decision='escalated' approval may NOT itself
    have parent_approval_id set (enforced in Python, not SQL — see
    feedback_req03_decisions for why)

When all guards pass, it inserts the Approval and updates
Action.authorization_status in the same flush so the two writes are
transactionally bound.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rootsign.crud.base import CRUDBase
from rootsign.errors import (
    ActionAlreadyResolvedError,
    ActionNotFoundError,
    ApprovalParentNotFoundError,
    IngestValidationError,
)
from rootsign.models.action import Action
from rootsign.models.approval import Approval
from rootsign.schemas.approval import ApprovalCreate, ApprovalDecision

# Mapping from Approval.decision to the resulting Action.authorization_status.
# `escalated` keeps the action in 'pending' until the resolving Approval arrives.
_DECISION_TO_AUTH_STATUS: dict[str, str] = {
    "approved": "human_approved",
    "rejected": "human_rejected",
    "escalated": "pending",
}

# Authorization states that are terminal — no further APPROVAL_RECORD allowed.
_TERMINAL_AUTH_STATES = frozenset({"human_approved", "human_rejected"})


class CRUDApproval(CRUDBase[Approval, ApprovalCreate]):
    async def create_with_action_status_update(
        self, db: AsyncSession, *, obj_in: ApprovalCreate
    ) -> Approval:
        """Insert an Approval and update the target Action atomically.

        Raises:
            ActionNotFoundError: no Action row matches (action_id, session_id).
            ActionAlreadyResolvedError: target Action is already in a terminal
                authorization state.
            ApprovalParentNotFoundError: parent_approval_id supplied but no
                matching parent Approval exists for this action_id.
            IngestValidationError: parent is non-escalated, or this is an
                `escalated` approval with parent_approval_id already set
                (chained escalation — Phase 0 forbids).
        """
        decision = obj_in.decision.value if isinstance(obj_in.decision, ApprovalDecision) else obj_in.decision

        # 1. Look up the target Action by (session_id, action_id). Both fields
        #    are required because actions is a TimescaleDB hypertable — scoping
        #    by session_id keeps the planner inside the relevant chunk via the
        #    ix_actions_session_seq index instead of full-table scanning.
        action = (
            await db.execute(
                select(Action).where(
                    Action.session_id == obj_in.session_id,
                    Action.action_id == obj_in.action_id,
                )
            )
        ).scalar_one_or_none()
        if action is None:
            raise ActionNotFoundError(
                f"action_id={obj_in.action_id} not found in session "
                f"{obj_in.session_id}"
            )

        # 2. Terminal state guard. Any APPROVAL_RECORD (incl. escalated) is
        #    rejected once the Action is fully approved/rejected.
        if action.authorization_status in _TERMINAL_AUTH_STATES:
            raise ActionAlreadyResolvedError(
                f"action_id={obj_in.action_id} is already "
                f"{action.authorization_status}; cannot record further approvals"
            )

        # 3. 2-level escalation rule: an `escalated` approval must NOT itself
        #    have parent_approval_id set. (Soft enforcement — relaxing later is
        #    a code change only.)
        if decision == "escalated" and obj_in.parent_approval_id is not None:
            raise IngestValidationError(
                "Chained escalations are not supported in Phase 0: an "
                "approval with decision='escalated' must have "
                "parent_approval_id=null"
            )

        # 4. parent_approval_id lookup + validation.
        if obj_in.parent_approval_id is not None:
            parent = (
                await db.execute(
                    select(Approval).where(
                        Approval.approval_id == obj_in.parent_approval_id
                    )
                )
            ).scalar_one_or_none()
            if parent is None:
                raise ApprovalParentNotFoundError(
                    f"parent_approval_id={obj_in.parent_approval_id} not found"
                )
            if parent.action_id != obj_in.action_id:
                raise ApprovalParentNotFoundError(
                    f"parent_approval_id={obj_in.parent_approval_id} belongs to "
                    f"action {parent.action_id}, not {obj_in.action_id}"
                )
            if parent.decision != "escalated":
                raise IngestValidationError(
                    f"parent_approval_id={obj_in.parent_approval_id} has "
                    f"decision={parent.decision!r}; only 'escalated' approvals "
                    "can be resolved by a child approval"
                )

        # 5. Insert Approval + flip Action.authorization_status in one flush.
        new_status = _DECISION_TO_AUTH_STATUS[decision]
        db_approval = Approval(
            action_id=obj_in.action_id,
            session_id=obj_in.session_id,
            approver_id=obj_in.approver_id,
            approver_type=obj_in.approver_type.value
            if hasattr(obj_in.approver_type, "value")
            else obj_in.approver_type,
            context_presented=obj_in.context_presented,
            decision=decision,
            decision_reason=obj_in.decision_reason,
            timestamp=obj_in.timestamp,
            response_latency_ms=obj_in.response_latency_ms,
            parent_approval_id=obj_in.parent_approval_id,
        )
        db.add(db_approval)
        action.authorization_status = new_status
        db.add(action)

        await db.flush()
        await db.refresh(db_approval)
        return db_approval


approval = CRUDApproval(Approval, pk_attr="approval_id")
