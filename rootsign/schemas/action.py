from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class ActionAuthorizationStatus(str, Enum):
    AUTO_AUTHORIZED = "auto_authorized"
    HUMAN_APPROVED = "human_approved"
    HUMAN_REJECTED = "human_rejected"
    PENDING = "pending"
    BYPASSED = "bypassed"


_HEX64 = r"^[0-9a-f]{64}$"


class ActionCreate(BaseModel):
    """Caller-supplied fields. sequence_number, prev_action_hash, and self_hash are
    assigned by CRUDAction.create_with_hash — not by the caller."""

    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    decision_id: UUID | None = None
    policy_id: UUID | None = None
    tool_name: str = Field(..., min_length=1, max_length=200)
    input_hash: str = Field(..., pattern=_HEX64)
    output_hash: str | None = Field(default=None, pattern=_HEX64)
    input_redacted: dict | None = None
    output_redacted: dict | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: int | None = Field(default=None, ge=0)
    authorization_status: ActionAuthorizationStatus = (
        ActionAuthorizationStatus.AUTO_AUTHORIZED
    )


class ActionUpdate(BaseModel):
    """Only mutable fields may be updated after creation. self_hash inputs are immutable."""

    model_config = ConfigDict(extra="forbid")

    output_hash: str | None = Field(default=None, pattern=_HEX64)
    output_redacted: dict | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    authorization_status: ActionAuthorizationStatus | None = None


class Action(ActionCreate):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    action_id: UUID = Field(default_factory=uuid4)
    prev_action_hash: str | None = Field(default=None, pattern=_HEX64)
    self_hash: str = Field(..., pattern=_HEX64)
    sequence_number: int = Field(..., ge=1)
