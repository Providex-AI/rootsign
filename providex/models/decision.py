from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from providex.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Decision(Base):
    """The 'why' — agent reasoning captured for a specific tool selection."""

    __tablename__ = "decisions"

    decision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sessions.session_id", ondelete="CASCADE"),
        nullable=False,
    )
    policy_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("policies.policy_id", ondelete="SET NULL"),
        nullable=True,
    )
    inputs_summary: Mapped[str | None] = mapped_column(String(5000), nullable=True)
    reasoning_summary: Mapped[str | None] = mapped_column(String(10000), nullable=True)
    selected_action: Mapped[str] = mapped_column(String(200), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    alternatives_considered: Mapped[list[str] | None] = mapped_column(
        ARRAY(String), nullable=True
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    reasoning_captured: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    __table_args__ = (
        Index("ix_decisions_session_timestamp", "session_id", "timestamp"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)",
            name="ck_decisions_confidence_range",
        ),
    )
