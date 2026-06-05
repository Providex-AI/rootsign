from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class PolicyScope(str, Enum):
    AGENT = "agent"
    SESSION = "session"
    TOOL = "tool"
    GLOBAL = "global"


class PolicyEnforcement(str, Enum):
    LOG_ONLY = "log_only"
    REQUIRE_APPROVAL = "require_approval"
    BLOCK = "block"


class PolicyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=2, max_length=200)
    rule_text: str = Field(..., min_length=1, max_length=50000)
    scope: PolicyScope
    scope_id: UUID | None = None
    version: int = Field(default=1, ge=1)
    regulatory_refs: list[str] | None = None
    enforcement_mode: PolicyEnforcement = PolicyEnforcement.LOG_ONLY
    created_by: str = Field(..., min_length=1, max_length=200)


class PolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_text: str | None = Field(default=None, min_length=1, max_length=50000)
    enforcement_mode: PolicyEnforcement | None = None
    regulatory_refs: list[str] | None = None
    is_active: bool | None = None


class Policy(PolicyCreate):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    policy_id: UUID = Field(default_factory=uuid4)
    is_active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
