from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class IncidentTrigger(str, Enum):
    ANOMALY_DETECTED = "anomaly_detected"
    POLICY_VIOLATION = "policy_violation"
    MANUAL = "manual"
    AUTHORIZATION_BYPASS = "authorization_bypass"


class IncidentSeverity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class IncidentStatus(str, Enum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    RESOLVED = "resolved"
    CLOSED = "closed"
    FALSE_POSITIVE = "false_positive"


class IncidentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trigger_type: IncidentTrigger
    severity: IncidentSeverity
    status: IncidentStatus = IncidentStatus.OPEN
    linked_session_ids: list[UUID] = Field(default_factory=list)
    linked_action_ids: list[UUID] | None = None
    linked_decision_ids: list[UUID] | None = None
    investigator_id: str | None = Field(default=None, max_length=200)
    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10000)
    resolution: str | None = Field(default=None, max_length=5000)


class IncidentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: IncidentStatus | None = None
    severity: IncidentSeverity | None = None
    investigator_id: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=10000)
    resolution: str | None = Field(default=None, max_length=5000)
    resolved_at: datetime | None = None


class Incident(IncidentCreate):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    incident_id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: datetime | None = None
