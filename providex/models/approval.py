from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from providex.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Approval(Base):
    """Human authorization record attached to an Action."""

    __tablename__ = "approvals"

    approval_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    # Note: no FK on action_id because Actions live in a TimescaleDB hypertable
    # whose composite PK (action_id, timestamp) makes single-column FK references
    # impractical. We enforce referential integrity at the application layer
    # (CRUDApproval) and through indexes for lookup performance.
    action_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    session_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    approver_id: Mapped[str] = mapped_column(String(200), nullable=False)
    approver_type: Mapped[str] = mapped_column(String(32), nullable=False)
    context_presented: Mapped[dict] = mapped_column(JSONB, nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    decision_reason: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    response_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Self-FK forming the escalation chain of custody. NULL for standalone
    # approvals; for an Approval that resolves an escalation, points at the
    # earlier escalated Approval. The 2-level rule (no escalating an
    # escalation) is enforced in CRUDApproval, NOT via a SQL CHECK — see
    # feedback_req03_decisions in agent memory for the rationale.
    parent_approval_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("approvals.approval_id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        Index("ix_approvals_action_id", "action_id"),
        Index("ix_approvals_decision_timestamp", "decision", "timestamp"),
        Index("ix_approvals_parent_approval_id", "parent_approval_id"),
        CheckConstraint(
            "approver_type IN ("
            "'human','automated_policy','timeout_auto_approved','timeout_auto_rejected')",
            name="ck_approvals_approver_type",
        ),
        CheckConstraint(
            "decision IN ('approved','rejected','escalated')",
            name="ck_approvals_decision",
        ),
        CheckConstraint(
            "response_latency_ms IS NULL OR response_latency_ms >= 0",
            name="ck_approvals_latency_nonneg",
        ),
    )
