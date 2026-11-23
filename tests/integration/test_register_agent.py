"""Integration tests for `rootsign.register_agent(...)`.

Production usage opens its own AsyncSession internally, but the test passes
`db=` the per-test fixture so registration runs inside the SAVEPOINT and is
rolled back at teardown — no cross-loop asyncpg state to worry about.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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


class TestRegisteringTwice:
    """`register_agent()` is a one-time setup step. A second call for the same
    (name, environment) used to fail with a raw sqlalchemy IntegrityError from
    the unique constraint, which named neither the agent nor the fix, and left
    a caller's own transaction unusable."""

    @staticmethod
    async def _register(name: str, **kwargs):
        return await rootsign.register_agent(
            name=name,
            owner="twice-team",
            environment=rootsign.AgentEnvironment.PRODUCTION,
            risk_tier=rootsign.AgentRiskTier.MEDIUM,
            framework=rootsign.AgentFramework.CUSTOM,
            **kwargs,
        )

    async def test_a_second_registration_names_the_existing_agent(self, db):
        name = f"register-twice-{uuid4().hex[:8]}"
        first = await self._register(name, db=db)

        with pytest.raises(ValueError) as raised:
            await self._register(name, db=db)

        assert str(raised.value) == (
            f"register_agent(): an agent named '{name}' is already registered in "
            f"'production' (agent_id={first.agent_id}). Registration is a one-time setup "
            "step: pass that ID to rootsign.session(agent_id=...), or let "
            f"rootsign.init(agent='{name}', environment='production') reuse it."
        )
        # The caller's transaction is still usable.
        still = (
            await db.execute(text("SELECT count(*) FROM agents WHERE name = :n"), {"n": name})
        ).scalar_one()
        assert still == 1

    async def test_the_same_name_in_another_environment_is_a_new_agent(self, db):
        name = f"register-envs-{uuid4().hex[:8]}"
        await self._register(name, db=db)
        other = await rootsign.register_agent(
            name=name,
            owner="twice-team",
            environment=rootsign.AgentEnvironment.STAGING,
            risk_tier=rootsign.AgentRiskTier.MEDIUM,
            framework=rootsign.AgentFramework.CUSTOM,
            db=db,
        )
        assert other.environment == "staging"

    async def test_without_db_the_second_call_raises_the_same(
        self, monkeypatch, test_engine, clean_db
    ):
        factory = async_sessionmaker(bind=test_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("rootsign.database.AsyncSessionLocal", factory)
        name = f"register-own-{uuid4().hex[:8]}"
        first = await self._register(name)

        with pytest.raises(ValueError, match=rf"agent_id={first.agent_id}\)"):
            await self._register(name)
