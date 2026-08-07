"""Relationship + constraint integration tests.

Validates FK behaviour, CASCADE/RESTRICT semantics, and uniqueness constraints
that the schema spec promises.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from rootsign import crud
from rootsign.models.agent import Agent
from rootsign.models.session import AgentSession
from rootsign.schemas import (
    AgentCreate,
    AgentEnvironment,
    AgentFramework,
    AgentRiskTier,
    DecisionCreate,
    SessionCreate,
    SessionStatus,
)

pytestmark = pytest.mark.integration


async def _agent(db: AsyncSession) -> Agent:
    return await crud.agent.create(
        db,
        obj_in=AgentCreate(
            name=f"agent-{uuid4().hex[:8]}",
            owner="platform",
            environment=AgentEnvironment.STAGING,
            risk_tier=AgentRiskTier.MEDIUM,
            framework=AgentFramework.CUSTOM,
        ),
    )


class TestAgentUniqueness:
    """Identity is (name, environment) since ADR-012 / migration 0005."""

    async def test_duplicate_name_and_environment_rejected(self, db: AsyncSession):
        await crud.agent.create(
            db,
            obj_in=AgentCreate(
                name="duplicated-name",
                owner="xx",
                environment=AgentEnvironment.PRODUCTION,
                risk_tier=AgentRiskTier.LOW,
                framework=AgentFramework.LANGGRAPH,
            ),
        )
        with pytest.raises(IntegrityError):
            await crud.agent.create(
                db,
                obj_in=AgentCreate(
                    name="duplicated-name",
                    owner="other",
                    environment=AgentEnvironment.PRODUCTION,
                    risk_tier=AgentRiskTier.HIGH,
                    framework=AgentFramework.CUSTOM,
                ),
            )

    async def test_same_name_in_a_different_environment_allowed(self, db: AsyncSession):
        for env in (AgentEnvironment.DEVELOPMENT, AgentEnvironment.PRODUCTION):
            await crud.agent.create(
                db,
                obj_in=AgentCreate(
                    name="per-environment-name",
                    owner="xx",
                    environment=env,
                    risk_tier=AgentRiskTier.LOW,
                    framework=AgentFramework.LANGGRAPH,
                ),
            )
        await db.flush()  # no IntegrityError


class TestSessionFK:
    async def test_session_requires_valid_agent(self, db: AsyncSession):
        s = AgentSession(
            session_id=uuid4(),
            agent_id=uuid4(),  # no such agent
            status="running",
        )
        db.add(s)
        with pytest.raises(IntegrityError):
            await db.flush()


class TestSessionCheckConstraints:
    async def test_negative_action_count_rejected(self, db: AsyncSession):
        agent = await _agent(db)
        s = await crud.session.create(
            db, obj_in=SessionCreate(agent_id=agent.agent_id, status=SessionStatus.RUNNING)
        )
        s.action_count = -1
        with pytest.raises(IntegrityError):
            await db.flush()


class TestDecisionFK:
    async def test_decision_session_fk_enforced(self, db: AsyncSession):
        with pytest.raises(IntegrityError):
            await crud.decision.create(
                db,
                obj_in=DecisionCreate(
                    session_id=uuid4(),  # no such session
                    selected_action="t1",
                ),
            )
            await db.flush()


class TestIndexesExist:
    async def test_all_required_indexes_present(self, db: AsyncSession):
        # AGENTS.md lists the required indexes — verify each was created.
        result = await db.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname='public' ORDER BY indexname"
            )
        )
        indexes = {row[0] for row in result.all()}
        required = {
            "ix_agents_environment_is_active",
            "ix_agents_risk_tier",
            "ix_sessions_agent_id_status",
            "ix_sessions_start_time_desc",
            "ix_actions_session_seq",
            "ix_actions_session_timestamp",
            "ix_actions_tool_name",
            "ix_actions_authorization_status",
            "ix_approvals_action_id",
            "ix_approvals_decision_timestamp",
            "ix_decisions_session_timestamp",
        }
        missing = required - indexes
        assert not missing, f"Missing required indexes: {missing}"
