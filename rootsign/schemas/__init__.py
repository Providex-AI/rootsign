"""Pydantic v2 schemas for RootSign Phase 0 entities.

Per spec, the Pydantic schema for sessions is named `Session` (not `AgentSession`)
— the SQLAlchemy clash only affects ORM models, not schemas.
"""

from rootsign.schemas.action import (
    Action,
    ActionAuthorizationStatus,
    ActionCreate,
    ActionUpdate,
)
from rootsign.schemas.agent import (
    Agent,
    AgentCreate,
    AgentEnvironment,
    AgentFramework,
    AgentRiskTier,
    AgentUpdate,
)
from rootsign.schemas.approval import (
    Approval,
    ApprovalCreate,
    ApprovalDecision,
    ApproverType,
)
from rootsign.schemas.decision import Decision, DecisionCreate, DecisionUpdate
from rootsign.schemas.incident import (
    Incident,
    IncidentCreate,
    IncidentSeverity,
    IncidentStatus,
    IncidentTrigger,
    IncidentUpdate,
)
from rootsign.schemas.policy import (
    Policy,
    PolicyCreate,
    PolicyEnforcement,
    PolicyScope,
    PolicyUpdate,
)
from rootsign.schemas.session import (
    Session,
    SessionCreate,
    SessionStatus,
    SessionUpdate,
)

__all__ = [
    "Action",
    "ActionAuthorizationStatus",
    "ActionCreate",
    "ActionUpdate",
    "Agent",
    "AgentCreate",
    "AgentEnvironment",
    "AgentFramework",
    "AgentRiskTier",
    "AgentUpdate",
    "Approval",
    "ApprovalCreate",
    "ApprovalDecision",
    "ApproverType",
    "Decision",
    "DecisionCreate",
    "DecisionUpdate",
    "Incident",
    "IncidentCreate",
    "IncidentSeverity",
    "IncidentStatus",
    "IncidentTrigger",
    "IncidentUpdate",
    "Policy",
    "PolicyCreate",
    "PolicyEnforcement",
    "PolicyScope",
    "PolicyUpdate",
    "Session",
    "SessionCreate",
    "SessionStatus",
    "SessionUpdate",
]
