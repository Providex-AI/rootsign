from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from rootsign.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Action(Base):
    """Concrete tool call — the hash-chain backbone.

    The `actions` table is converted to a TimescaleDB hypertable in
    `alembic/versions/0001_initial_schema.py`. The primary key includes
    `timestamp` because TimescaleDB requires the partitioning column to
    be part of any unique constraint on the table.
    """

    __tablename__ = "actions"

    action_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False, default=_utcnow
    )
    session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sessions.session_id", ondelete="RESTRICT"),
        nullable=False,
    )
    decision_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    # Forward-looking nullable FK; Policy entity exists in Phase 0 schema for this.
    policy_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("policies.policy_id", ondelete="SET NULL"),
        nullable=True,
    )
    tool_name: Mapped[str] = mapped_column(String(200), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_redacted: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    output_redacted: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    prev_action_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    self_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    authorization_status: Mapped[str] = mapped_column(String(32), nullable=False)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        Index("ix_actions_session_seq", "session_id", "sequence_number"),
        Index("ix_actions_session_timestamp", "session_id", "timestamp"),
        Index("ix_actions_tool_name", "tool_name"),
        Index("ix_actions_authorization_status", "authorization_status"),
        CheckConstraint(
            "authorization_status IN ("
            "'auto_authorized','human_approved','human_rejected',"
            "'pending','bypassed','timed_out')",
            name="ck_actions_authorization_status",
        ),
        CheckConstraint("sequence_number >= 1", name="ck_actions_seq_positive"),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_actions_duration_nonneg",
        ),
        CheckConstraint(
            "length(input_hash) = 64", name="ck_actions_input_hash_length"
        ),
        CheckConstraint(
            "length(self_hash) = 64", name="ck_actions_self_hash_length"
        ),
    )
