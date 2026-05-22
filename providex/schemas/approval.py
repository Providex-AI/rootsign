from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class ApproverType(str, Enum):
    HUMAN = "human"
    AUTOMATED_POLICY = "automated_policy"
    TIMEOUT_AUTO_APPROVED = "timeout_auto_approved"
    TIMEOUT_AUTO_REJECTED = "timeout_auto_rejected"


class ApprovalDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    ESCALATED = "escalated"


class ApprovalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: UUID
    session_id: UUID
    approver_id: str = Field(..., min_length=1, max_length=200)
    approver_type: ApproverType
    context_presented: dict
    decision: ApprovalDecision
    decision_reason: str | None = Field(default=None, max_length=2000)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    response_latency_ms: int | None = Field(default=None, ge=0)
    # NULL for standalone approvals. When the approval resolves a prior
    # escalation, points at that escalation's approval_id. Depth validation
    # (only 2 levels in Phase 0) lives in CRUDApproval, not here.
    parent_approval_id: UUID | None = None


class Approval(ApprovalCreate):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    approval_id: UUID = Field(default_factory=uuid4)
