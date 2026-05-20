from datetime import datetime, timezone
from enum import Enum
from typing import Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SessionStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: UUID
    user_id: str | None = Field(default=None, max_length=200)
    objective: str | None = Field(default=None, max_length=2000)
    status: SessionStatus = SessionStatus.RUNNING
    start_time: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    end_time: datetime | None = None
    metadata: dict | None = None

    @model_validator(mode="after")
    def _check_end_after_start(self) -> Self:
        if self.end_time is not None and self.end_time < self.start_time:
            raise ValueError("end_time must be >= start_time")
        return self


class SessionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: SessionStatus | None = None
    end_time: datetime | None = None
    objective: str | None = Field(default=None, max_length=2000)
    metadata: dict | None = None


class Session(SessionCreate):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    session_id: UUID = Field(default_factory=uuid4)
    action_count: int = Field(default=0, ge=0)
    decision_count: int = Field(default=0, ge=0)
    chain_head_hash: str | None = Field(default=None, min_length=64, max_length=64)
    chain_tail_hash: str | None = Field(default=None, min_length=64, max_length=64)
