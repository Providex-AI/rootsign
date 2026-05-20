from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from providex.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Agent(Base):
    """Registered AI system with identity, owner, and risk tier."""

    __tablename__ = "agents"

    agent_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    owner: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    risk_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    permitted_tools: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list
    )
    regulatory_categories: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list
    )
    framework: Mapped[str] = mapped_column(String(32), nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # `metadata` is reserved on SQLAlchemy Base — keep the SQL column name `metadata`
    # but expose it on the ORM attribute as `extra_metadata`.
    extra_metadata: Mapped[dict | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )

    __table_args__ = (
        UniqueConstraint("name", name="uq_agents_name"),
        Index("ix_agents_environment_is_active", "environment", "is_active"),
        Index("ix_agents_risk_tier", "risk_tier"),
        Index("ix_agents_is_active", "is_active"),
        CheckConstraint(
            "environment IN ('development','staging','production')",
            name="ck_agents_environment",
        ),
        CheckConstraint(
            "risk_tier IN ('low','medium','high','critical')",
            name="ck_agents_risk_tier",
        ),
        CheckConstraint(
            "framework IN ('langgraph','crewai','autogen','custom','unknown')",
            name="ck_agents_framework",
        ),
    )
