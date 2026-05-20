from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class AgentEnvironment(str, Enum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class AgentRiskTier(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AgentFramework(str, Enum):
    LANGGRAPH = "langgraph"
    CREWAI = "crewai"
    AUTOGEN = "autogen"
    CUSTOM = "custom"
    UNKNOWN = "unknown"


class AgentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=2, max_length=200)
    owner: str = Field(..., min_length=2, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    environment: AgentEnvironment
    risk_tier: AgentRiskTier
    permitted_tools: list[str] = Field(default_factory=list)
    regulatory_categories: list[str] = Field(default_factory=list)
    framework: AgentFramework
    model_version: str | None = Field(default=None, max_length=100)
    metadata: dict | None = None


class AgentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=2, max_length=200)
    owner: str | None = Field(default=None, min_length=2, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    environment: AgentEnvironment | None = None
    risk_tier: AgentRiskTier | None = None
    permitted_tools: list[str] | None = None
    regulatory_categories: list[str] | None = None
    framework: AgentFramework | None = None
    model_version: str | None = Field(default=None, max_length=100)
    metadata: dict | None = None
    is_active: bool | None = None


class Agent(AgentCreate):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    agent_id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_active: bool = True
