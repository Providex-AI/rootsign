from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from providex.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProvidexSession(Base):
    """One logical workflow run linking user, agent, and objective.

    Named ProvidexSession (not Session) to avoid collision with SQLAlchemy's Session.
    """

    __tablename__ = "sessions"

    session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    agent_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agents.agent_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    objective: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    end_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    action_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    decision_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chain_head_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    chain_tail_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    extra_metadata: Mapped[dict | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )

    __table_args__ = (
        Index("ix_sessions_agent_id_status", "agent_id", "status"),
        Index("ix_sessions_start_time_desc", "start_time"),
        CheckConstraint(
            "status IN ('running','completed','failed','abandoned')",
            name="ck_sessions_status",
        ),
        CheckConstraint("action_count >= 0", name="ck_sessions_action_count_nonneg"),
        CheckConstraint(
            "decision_count >= 0", name="ck_sessions_decision_count_nonneg"
        ),
        CheckConstraint(
            "end_time IS NULL OR end_time >= start_time",
            name="ck_sessions_end_after_start",
        ),
    )
