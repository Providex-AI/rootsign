"""`rootsign.register_agent(...)` — one-shot Agent registration helper.

A thin async wrapper around `crud.agent.create` that opens its own
`AsyncSession` so the user doesn't have to plumb one through in their app
startup code. Returns the persisted ORM `Agent` so the caller can grab
`.agent_id` for `rootsign.session(agent_id=..., client=...)`.

Phase 1 is single-tenant local mode (per spec) — Phase 2 will move
registration to a hosted control plane.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from rootsign.crud import agent as agent_crud
from rootsign.database import AsyncSessionLocal
from rootsign.models.agent import Agent
from rootsign.schemas.agent import (
    AgentCreate,
    AgentEnvironment,
    AgentFramework,
    AgentRiskTier,
)


async def register_agent(
    *,
    name: str,
    owner: str,
    environment: AgentEnvironment,
    risk_tier: AgentRiskTier,
    framework: AgentFramework,
    description: str | None = None,
    model_version: str | None = None,
    permitted_tools: list[str] | None = None,
    regulatory_categories: list[str] | None = None,
    db: AsyncSession | None = None,
) -> Agent:
    """Register an agent in the local store and return its ORM record.

    Production callers omit `db` — registration opens its own short-lived
    session against `AsyncSessionLocal`, commits, and returns. Callers that
    want to participate in an existing transaction (or that need explicit
    loop control, e.g. inside a pytest fixture with a function-scoped
    engine) pass `db=` an `AsyncSession`; the caller then owns flushing
    and committing.

    The same name twice will raise via the `uq_agents_name` unique
    constraint — registration is a one-shot setup step.
    """
    obj_in = AgentCreate(
        name=name,
        owner=owner,
        environment=environment,
        risk_tier=risk_tier,
        framework=framework,
        description=description,
        model_version=model_version,
        permitted_tools=permitted_tools or [],
        regulatory_categories=regulatory_categories or [],
    )

    if db is not None:
        agent = await agent_crud.create(db, obj_in=obj_in)
        await db.flush()
        return agent

    async with AsyncSessionLocal() as session:
        agent = await agent_crud.create(session, obj_in=obj_in)
        await session.commit()
        await session.refresh(agent)
        return agent
