from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from providex.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Policy(Base):
    """Declarative governance rule. Schema defined in Phase 0; populated from Phase 3."""

    __tablename__ = "policies"

    policy_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    rule_text: Mapped[str] = mapped_column(String(50000), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    regulatory_refs: Mapped[list[str] | None] = mapped_column(
        ARRAY(String), nullable=True
    )
    enforcement_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="log_only"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)

    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_policies_name_version"),
        CheckConstraint(
            "scope IN ('agent','session','tool','global')", name="ck_policies_scope"
        ),
        CheckConstraint(
            "enforcement_mode IN ('log_only','require_approval','block')",
            name="ck_policies_enforcement_mode",
        ),
        CheckConstraint("version >= 1", name="ck_policies_version_positive"),
    )
