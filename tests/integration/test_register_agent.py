"""Integration tests for `rootsign.register_agent(...)`.

Production usage opens its own AsyncSession internally, but the test passes
`db=` the per-test fixture so registration runs inside the SAVEPOINT and is
rolled back at teardown — no cross-loop asyncpg state to worry about.
"""

from __future__ import annotations

from uuid import uuid4

import rootsign


class TestRegisterAgent:
    async def test_creates_agent_record(self, db):
        name = f"register-test-{uuid4().hex[:8]}"
        agent = await rootsign.register_agent(
            name=name,
            owner="reg-test-team",
            environment=rootsign.AgentEnvironment.PRODUCTION,
            risk_tier=rootsign.AgentRiskTier.MEDIUM,
            framework=rootsign.AgentFramework.LANGGRAPH,
            db=db,
        )

        assert agent.agent_id is not None
        assert agent.name == name
        assert agent.owner == "reg-test-team"
        assert agent.framework == rootsign.AgentFramework.LANGGRAPH.value
        assert agent.is_active is True

    async def test_optional_fields_default_to_empty(self, db):
        name = f"register-defaults-{uuid4().hex[:8]}"
        agent = await rootsign.register_agent(
            name=name,
            owner="defaults-team",
            environment=rootsign.AgentEnvironment.STAGING,
            risk_tier=rootsign.AgentRiskTier.LOW,
            framework=rootsign.AgentFramework.CUSTOM,
            db=db,
        )

        assert agent.permitted_tools == []
        assert agent.regulatory_categories == []
        assert agent.description is None

    async def test_explicit_optional_fields_persisted(self, db):
        name = f"register-explicit-{uuid4().hex[:8]}"
        agent = await rootsign.register_agent(
            name=name,
            owner="explicit-team",
            environment=rootsign.AgentEnvironment.PRODUCTION,
            risk_tier=rootsign.AgentRiskTier.HIGH,
            framework=rootsign.AgentFramework.LANGGRAPH,
            description="High-risk invoice agent",
            model_version="gpt-4o-2026-04",
            permitted_tools=["send_invoice", "lookup_customer"],
            regulatory_categories=["SOC2", "GDPR"],
            db=db,
        )

        assert agent.description == "High-risk invoice agent"
        assert agent.model_version == "gpt-4o-2026-04"
        assert agent.permitted_tools == ["send_invoice", "lookup_customer"]
        assert agent.regulatory_categories == ["SOC2", "GDPR"]
