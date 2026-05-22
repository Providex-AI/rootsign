"""Unit tests for Pydantic schemas. Map directly to AC-1.1 through AC-1.10.

No database required.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from providex.schemas import (
    Action,
    ActionAuthorizationStatus,
    ActionCreate,
    Agent,
    AgentCreate,
    AgentEnvironment,
    AgentFramework,
    AgentRiskTier,
    ApprovalCreate,
    ApprovalDecision,
    ApproverType,
    Decision,
    DecisionCreate,
    IncidentCreate,
    IncidentSeverity,
    IncidentStatus,
    IncidentTrigger,
    PolicyCreate,
    PolicyEnforcement,
    PolicyScope,
    SessionCreate,
    SessionStatus,
)


def _valid_agent_create_kwargs() -> dict:
    return {
        "name": "test-agent",
        "owner": "platform-team",
        "environment": AgentEnvironment.PRODUCTION,
        "risk_tier": AgentRiskTier.HIGH,
        "framework": AgentFramework.LANGGRAPH,
    }


class TestAgentSchema:
    def test_valid_agent_create(self):
        a = AgentCreate(**_valid_agent_create_kwargs())
        assert a.name == "test-agent"
        assert a.permitted_tools == []  # default empty list
        assert a.regulatory_categories == []

    # AC-1.2
    def test_name_too_short_rejected(self):
        with pytest.raises(ValidationError):
            AgentCreate(**{**_valid_agent_create_kwargs(), "name": "A"})

    def test_name_too_long_rejected(self):
        with pytest.raises(ValidationError):
            AgentCreate(**{**_valid_agent_create_kwargs(), "name": "x" * 201})

    # AC-1.3
    def test_invalid_environment_rejected(self):
        with pytest.raises(ValidationError):
            AgentCreate(**{**_valid_agent_create_kwargs(), "environment": "prod"})

    def test_invalid_risk_tier_rejected(self):
        with pytest.raises(ValidationError):
            AgentCreate(**{**_valid_agent_create_kwargs(), "risk_tier": "very_high"})

    def test_invalid_framework_rejected(self):
        with pytest.raises(ValidationError):
            AgentCreate(**{**_valid_agent_create_kwargs(), "framework": "raindrop"})

    # AC-1.9
    def test_uuid_auto_generated(self):
        a = Agent(**_valid_agent_create_kwargs())
        assert isinstance(a.agent_id, UUID)
        assert a.agent_id.version == 4

    # AC-1.10
    def test_timestamp_defaults_to_utc(self):
        a = Agent(**_valid_agent_create_kwargs())
        assert a.created_at.tzinfo is timezone.utc
        assert a.updated_at.tzinfo is timezone.utc


class TestSessionSchema:
    def _kwargs(self) -> dict:
        return {"agent_id": uuid4(), "status": SessionStatus.RUNNING}

    def test_valid_session_create(self):
        s = SessionCreate(**self._kwargs())
        assert s.status is SessionStatus.RUNNING
        assert s.start_time.tzinfo is timezone.utc

    # AC-1.7
    def test_end_time_before_start_time_rejected(self):
        start = datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)
        with pytest.raises(ValidationError):
            SessionCreate(
                **self._kwargs(),
                start_time=start,
                end_time=start - timedelta(seconds=1),
            )

    def test_end_time_equal_to_start_time_allowed(self):
        start = datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)
        s = SessionCreate(**self._kwargs(), start_time=start, end_time=start)
        assert s.end_time == start

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            SessionCreate(agent_id=uuid4(), status="bogus")


class TestDecisionSchema:
    def _kwargs(self) -> dict:
        return {"session_id": uuid4(), "selected_action": "send_email"}

    def test_valid_decision_create(self):
        d = DecisionCreate(**self._kwargs(), confidence=0.5)
        assert d.confidence == 0.5

    # AC-1.6
    def test_confidence_above_one_rejected(self):
        with pytest.raises(ValidationError):
            DecisionCreate(**self._kwargs(), confidence=1.5)

    def test_confidence_below_zero_rejected(self):
        with pytest.raises(ValidationError):
            DecisionCreate(**self._kwargs(), confidence=-0.01)

    def test_confidence_at_boundary_allowed(self):
        d_lo = DecisionCreate(**self._kwargs(), confidence=0.0)
        d_hi = DecisionCreate(**self._kwargs(), confidence=1.0)
        assert d_lo.confidence == 0.0 and d_hi.confidence == 1.0

    def test_auto_uuid_and_utc_timestamp(self):
        d = Decision(**self._kwargs())
        assert isinstance(d.decision_id, UUID)
        assert d.timestamp.tzinfo is timezone.utc


class TestActionSchema:
    def _kwargs(self) -> dict:
        return {
            "session_id": uuid4(),
            "tool_name": "send_email",
            "input_hash": "a" * 64,
        }

    def test_valid_action_create(self):
        a = ActionCreate(**self._kwargs())
        assert a.authorization_status is ActionAuthorizationStatus.AUTO_AUTHORIZED

    def test_input_hash_must_be_64_hex(self):
        with pytest.raises(ValidationError):
            ActionCreate(**{**self._kwargs(), "input_hash": "abc"})
        with pytest.raises(ValidationError):
            ActionCreate(**{**self._kwargs(), "input_hash": "Z" * 64})  # non-hex

    def test_output_hash_optional_but_validated(self):
        a = ActionCreate(**self._kwargs(), output_hash=None)
        assert a.output_hash is None
        with pytest.raises(ValidationError):
            ActionCreate(**{**self._kwargs(), "output_hash": "nope"})

    def test_invalid_authorization_status_rejected(self):
        with pytest.raises(ValidationError):
            ActionCreate(**{**self._kwargs(), "authorization_status": "vetoed"})

    def test_negative_duration_rejected(self):
        with pytest.raises(ValidationError):
            ActionCreate(**self._kwargs(), duration_ms=-5)

    # AC-1.10
    def test_action_timestamp_defaults_to_utc(self):
        full = Action(
            **self._kwargs(),
            self_hash="b" * 64,
            sequence_number=1,
        )
        assert full.timestamp.tzinfo is timezone.utc


class TestApprovalSchema:
    def _kwargs(self) -> dict:
        return {
            "action_id": uuid4(),
            "session_id": uuid4(),
            "approver_id": "alice@example.com",
            "approver_type": ApproverType.HUMAN,
            "context_presented": {"prompt": "Approve sending email?"},
            "decision": ApprovalDecision.APPROVED,
        }

    def test_valid_approval_create(self):
        a = ApprovalCreate(**self._kwargs())
        assert a.decision is ApprovalDecision.APPROVED

    def test_invalid_decision_rejected(self):
        with pytest.raises(ValidationError):
            ApprovalCreate(**{**self._kwargs(), "decision": "maybe"})

    def test_invalid_approver_type_rejected(self):
        with pytest.raises(ValidationError):
            ApprovalCreate(**{**self._kwargs(), "approver_type": "alien"})

    def test_context_presented_required(self):
        kw = self._kwargs()
        kw.pop("context_presented")
        with pytest.raises(ValidationError):
            ApprovalCreate(**kw)


class TestPolicySchema:
    def _kwargs(self) -> dict:
        return {
            "name": "no-prod-deletes",
            "rule_text": "deny when tool == 'delete'",
            "scope": PolicyScope.GLOBAL,
            "created_by": "ops@example.com",
        }

    def test_valid_policy(self):
        p = PolicyCreate(**self._kwargs())
        assert p.enforcement_mode is PolicyEnforcement.LOG_ONLY
        assert p.version == 1

    def test_invalid_scope_rejected(self):
        with pytest.raises(ValidationError):
            PolicyCreate(**{**self._kwargs(), "scope": "everywhere"})

    def test_version_zero_rejected(self):
        with pytest.raises(ValidationError):
            PolicyCreate(**{**self._kwargs(), "version": 0})


class TestIncidentSchema:
    def _kwargs(self) -> dict:
        return {
            "trigger_type": IncidentTrigger.ANOMALY_DETECTED,
            "severity": IncidentSeverity.HIGH,
            "title": "Unusual tool call rate",
        }

    def test_valid_incident_create(self):
        i = IncidentCreate(**self._kwargs())
        assert i.status is IncidentStatus.OPEN  # default
        assert i.linked_session_ids == []

    def test_invalid_severity_rejected(self):
        with pytest.raises(ValidationError):
            IncidentCreate(**{**self._kwargs(), "severity": "catastrophic"})

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            IncidentCreate(**{**self._kwargs(), "status": "ongoing"})


# AC-1.1 — full surface import
class TestImports:
    def test_all_schemas_importable(self):
        import providex.schemas as s

        for name in [
            "Agent",
            "Session",
            "Decision",
            "Action",
            "Policy",
            "Approval",
            "Incident",
        ]:
            assert hasattr(s, name), f"providex.schemas.{name} missing"

    def test_all_models_importable(self):
        import providex.models as m

        for name in [
            "Agent",
            "ProvidexSession",
            "Action",
            "Decision",
            "Policy",
            "Approval",
            "Incident",
        ]:
            assert hasattr(m, name), f"providex.models.{name} missing"
