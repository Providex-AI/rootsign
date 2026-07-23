"""CRUD for Approval — owns the approval insert + atomic Action authorization update.

Two entry points live on `CRUDApproval`:

* `create_with_action_status_update` (Phase 0) — driven by the ingest handler.
  Takes a validated `ApprovalCreate` payload, enforces the full escalation
  contract (parent_approval_id lookup, 2-level rule, etc.). Scopes the
  Action lookup by (session_id, action_id) which is sufficient for the
  ingest path because the envelope always carries the session.

* `create_with_chain_link` (Sprint 4) — driven by the HiTL poll loop and
  the `rootsign approve` CLI. Takes raw kwargs (no schema). Uses the
  hypertable-safe lookup key (action_id, action_timestamp) because those
  callers don't have an envelope to hand and the actions table's composite
  PK requires the partition column on every unique lookup. Recognises the
  'timeout_auto_rejected' approver_type and maps it to the new
  authorization_status='timed_out' terminal state.

Both methods share the "terminal states cannot transition" invariant.
The widened terminal set (Sprint 4 adds 'timed_out') lives at module scope
so a future ingest-path timeout would automatically pick it up.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select, update
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
# Sprint 4 adds 'timed_out' (poll-loop expiry recorded by HiTLCheckpoint).
_TERMINAL_AUTH_STATES = frozenset({"human_approved", "human_rejected", "timed_out"})

# Sentinel approver_type for poll-loop timeouts. When the HiTL checkpoint
# fails to receive a human decision within timeout_seconds, it records an
# Approval with this approver_type and the resulting Action status is
# 'timed_out' regardless of the (rejected) decision field.
_TIMEOUT_APPROVER_TYPE = "timeout_auto_rejected"


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
        decision = (
            obj_in.decision.value
            if isinstance(obj_in.decision, ApprovalDecision)
            else obj_in.decision
        )

        # 1. Look up the target Action by (session_id, action_id). Both
        #    columns are required because `actions` is a TimescaleDB hypertable
        #    and a single-column action_id lookup is not unique under the
        #    composite PK. This diverges from Sprint 4 Flag 5's canonical
        #    (action_id, action_timestamp) form deliberately — the ingest-path
        #    ApprovalRecordPayload does not carry the action's original
        #    timestamp, only its own. Switching would require an envelope
        #    schema bump for no semantic gain since action_id is a UUID and
        #    becomes unique once scoped to a session. The lookup is served by
        #    `ix_actions_session_seq (session_id, sequence_number)`; chunks are
        #    still scanned (TimescaleDB prunes on the partition column
        #    `timestamp`, not session_id), but sessions are short-lived so
        #    only 1–2 chunks are touched in practice. `create_with_chain_link`
        #    below uses the canonical (action_id, action_timestamp) form
        #    because the HiTL / CLI callers have the timestamp on hand.
        #    `with_for_update()` row-locks the Action so two concurrent
        #    resolvers (e.g. ingest + a stray CLI/timeout writer) serialise
        #    here instead of both passing the terminal guard below and
        #    racing to insert (audit #5). Matches the session-row lock in
        #    `CRUDAction.create_with_hash`.
        action = (
            await db.execute(
                select(Action)
                .where(
                    Action.session_id == obj_in.session_id,
                    Action.action_id == obj_in.action_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if action is None:
            raise ActionNotFoundError(
                f"action_id={obj_in.action_id} not found in session {obj_in.session_id}"
            )

        # 2. Terminal state guard. Any APPROVAL_RECORD (incl. escalated) is
        #    rejected once the Action is fully approved/rejected. Safe under
        #    concurrency because the FOR UPDATE above means the loser of a
        #    race re-reads the winner's committed authorization_status.
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
                    select(Approval).where(Approval.approval_id == obj_in.parent_approval_id)
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

    async def create_with_chain_link(
        self,
        db: AsyncSession,
        *,
        action_id: UUID,
        action_timestamp: datetime,
        approver_id: str,
        approver_type: str,
        context_presented: dict,
        decision: str,
        decision_reason: str | None = None,
        parent_approval_id: UUID | None = None,
        response_latency_ms: int | None = None,
    ) -> Approval:
        """Insert an APPROVAL_RECORD bound to a hypertable Action row.

        Sprint 4 entry point used by `HiTLCheckpoint._record_timeout` and the
        `rootsign approve` CLI. Differs from `create_with_action_status_update`
        in three ways:

        1. **Lookup key** — joins on `(action_id, action_timestamp)` rather
           than `(session_id, action_id)`. The actions table is a TimescaleDB
           hypertable whose composite primary key is `(action_id, timestamp)`,
           so any single-row lookup must include the partition column.
           HiTL/CLI callers reserve `action_timestamp` at submission time
           and pass it through here.

        2. **Raw kwargs** — no `ApprovalCreate` schema. The HiTL path
           doesn't have an envelope to validate against, and the CLI takes
           values straight from the operator.

        3. **Timeout mapping** — when `approver_type == 'timeout_auto_rejected'`
           the resulting `Action.authorization_status` is `'timed_out'`
           (a Sprint 4 terminal state), regardless of the `decision` value.
           This is how the poll loop records an abandoned action.

        Raises:
            ActionNotFoundError: no Action row matches (action_id,
                action_timestamp). Either the action was never submitted or
                the caller passed the wrong timestamp.
            ActionAlreadyResolvedError: target Action is already in a
                terminal authorization state ('human_approved',
                'human_rejected', or 'timed_out'). The HiTL poll loop
                catches this when a human decision lands during the same
                window as the timeout fires — see HiTLCheckpoint.
        """
        # 1. Hypertable-safe lookup. Both columns required for the actions
        #    composite PK. Without action_timestamp the planner cannot prune
        #    chunks and the row would not be unique. `with_for_update()`
        #    row-locks the Action so the `rootsign approve` CLI and the poll
        #    loop's timeout writer serialise on it — the ADR-007
        #    "human wins ties" invariant depends on this guard serialising,
        #    which a plain SELECT under READ COMMITTED does not (audit #5).
        stmt = (
            select(Action)
            .where(
                Action.action_id == action_id,
                Action.timestamp == action_timestamp,
            )
            .with_for_update()
        )
        action = (await db.execute(stmt)).scalar_one_or_none()
        if action is None:
            raise ActionNotFoundError(
                f"action_id={action_id} at timestamp={action_timestamp.isoformat()} "
                f"not found in hypertable"
            )

        # 2. Terminal-state guard. The race against a human approval lives
        #    here: if `rootsign approve <id>` committed milliseconds before
        #    the poll loop's timeout fires, the Action is already terminal
        #    and HiTLCheckpoint will catch this error and return the human's
        #    decision instead of raising HiTLTimeoutError.
        if action.authorization_status in _TERMINAL_AUTH_STATES:
            raise ActionAlreadyResolvedError(
                f"action_id={action_id} is already "
                f"{action.authorization_status}; cannot record further approvals"
            )

        # 3. Compute the new authorization_status. The timeout sentinel
        #    overrides whatever decision value the caller passed — the poll
        #    loop currently passes decision='rejected' alongside
        #    approver_type='timeout_auto_rejected' so the Approval row
        #    satisfies ck_approvals_decision while the Action row reads
        #    'timed_out' for forensic clarity.
        if approver_type == _TIMEOUT_APPROVER_TYPE:
            new_status = "timed_out"
        else:
            try:
                new_status = _DECISION_TO_AUTH_STATUS[decision]
            except KeyError as exc:
                raise IngestValidationError(
                    f"Unknown decision={decision!r}; expected one of "
                    f"{sorted(_DECISION_TO_AUTH_STATUS)}"
                ) from exc

        # 4. Insert Approval + update Action atomically. The UPDATE goes
        #    through `update(Action).where(...)` rather than mutating the
        #    ORM object because TimescaleDB's hypertable layer rewrites
        #    these into chunk-targeted statements; raw SQL avoids the
        #    occasional dialect quirk when SQLAlchemy autoflush emits the
        #    update without the timestamp column.
        approval_row = Approval(
            approval_id=uuid4(),
            action_id=action_id,
            session_id=action.session_id,
            approver_id=approver_id,
            approver_type=approver_type,
            context_presented=context_presented,
            decision=decision,
            decision_reason=decision_reason,
            parent_approval_id=parent_approval_id,
            response_latency_ms=response_latency_ms,
            timestamp=datetime.now(timezone.utc),
        )
        db.add(approval_row)
        await db.execute(
            update(Action)
            .where(
                Action.action_id == action_id,
                Action.timestamp == action_timestamp,
            )
            .values(authorization_status=new_status)
        )
        await db.flush()
        return approval_row


approval = CRUDApproval(Approval, pk_attr="approval_id")
