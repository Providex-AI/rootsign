from datetime import datetime, timezone
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class DecisionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    policy_id: UUID | None = None
    inputs_summary: str | None = Field(default=None, max_length=5000)
    reasoning_summary: str | None = Field(default=None, max_length=10000)
    selected_action: str = Field(..., min_length=1, max_length=200)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    alternatives_considered: list[str] | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    reasoning_captured: bool = False


class DecisionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inputs_summary: str | None = Field(default=None, max_length=5000)
    reasoning_summary: str | None = Field(default=None, max_length=10000)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    alternatives_considered: list[str] | None = None
    reasoning_captured: bool | None = None


class Decision(DecisionCreate):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    decision_id: UUID = Field(default_factory=uuid4)
