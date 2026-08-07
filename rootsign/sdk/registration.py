"""`rootsign.register_agent(...)` — one-shot Agent registration helper.

A thin async wrapper around `crud.agent.create` that opens its own
`AsyncSession` so the user doesn't have to plumb one through in their app
startup code. Returns the persisted ORM `Agent` so the caller can grab
`.agent_id` for `rootsign.session(agent_id=..., client=...)`.

Phase 1 is single-tenant local mode (per spec) — Phase 2 will move
registration to a hosted control plane.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from rootsign.schemas.agent import (
    AgentCreate,
    AgentEnvironment,
    AgentFramework,
    AgentRiskTier,
)

if TYPE_CHECKING:
    # DB stack lives in the optional `postgres` extra (ADR-011); these are only
    # type hints here. The real imports happen inside register_agent's body.
    from sqlalchemy.ext.asyncio import AsyncSession

    from rootsign.models.agent import Agent
    from rootsign.sdk.config import SDKSettings

logger = logging.getLogger("rootsign.sdk.registration")


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

    The same `(name, environment)` twice will raise via the
    `uq_agents_name_environment` unique constraint — registration is a one-shot
    setup step. Use `get_or_register_agent` for the idempotent variant the
    `rootsign.init()` facade needs.
    """
    # Lazy imports — the DB stack is the postgres extra (ADR-011). Registration
    # only runs on the postgres path today; the jsonl backend gets its own
    # get-or-create in Sprint A Workstream 2.
    from rootsign.crud import agent as agent_crud
    from rootsign.database import AsyncSessionLocal

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


# Attributes compared for drift when an agent already exists. `name` and
# `environment` are the identity key, so they're excluded by construction.
_DRIFT_FIELDS = (
    "owner",
    "risk_tier",
    "framework",
    "description",
    "model_version",
    "permitted_tools",
    "regulatory_categories",
)


def _warn_on_drift(
    *, name: str, environment: str, stored: dict[str, Any], requested: dict[str, Any]
) -> None:
    """Log a WARNING per changed attribute; the stored values always win.

    Mutating a registered agent is an admin operation, not a side effect of
    `init()` (ADR-012 Decision 2).
    """
    drifted = []
    for field in _DRIFT_FIELDS:
        if field not in requested or requested[field] is None:
            continue
        want = requested[field]
        have = stored.get(field)
        if isinstance(want, list) and not want:
            # An unset list arg is indistinguishable from an intentional empty
            # one; don't cry drift over a default.
            continue
        if have != want:
            drifted.append(f"{field}: stored={have!r} requested={want!r}")
    if drifted:
        logger.warning(
            "rootsign: agent (%s, %s) already registered with different attributes; "
            "keeping the stored values. Change them with an admin operation, not "
            "init(). Drift: %s",
            name,
            environment,
            "; ".join(drifted),
        )


async def get_or_register_agent(
    *,
    name: str,
    environment: str,
    owner: str,
    risk_tier: str,
    framework: str,
    description: str | None = None,
    model_version: str | None = None,
    permitted_tools: list[str] | None = None,
    regulatory_categories: list[str] | None = None,
    settings: SDKSettings | None = None,
) -> UUID:
    """Idempotent get-or-create keyed on `(name, environment)`. Returns agent_id.

    Backend-dispatched (ADR-012 Decision 2):

    * `jsonl` — lookup/append in `$ROOTSIGN_DATA_DIR/agents.jsonl`.
    * `postgres` — `INSERT ... ON CONFLICT (name, environment) DO NOTHING`
      followed by a `SELECT`, which is why the sprint's one migration replaces
      `uq_agents_name` with `uq_agents_name_environment`.

    Re-running a script never re-registers. Attribute drift against an existing
    `(name, environment)` logs a WARNING and keeps the stored values.
    """
    if settings is None:
        from rootsign.sdk.config import SDKSettings as _SDKSettings

        settings = _SDKSettings()

    requested: dict[str, Any] = {
        "owner": owner,
        "risk_tier": risk_tier,
        "framework": framework,
        "description": description,
        "model_version": model_version,
        "permitted_tools": permitted_tools or [],
        "regulatory_categories": regulatory_categories or [],
    }

    if settings.BACKEND == "jsonl":
        from rootsign.sdk import jsonl_registry

        existing = jsonl_registry.find_agent(
            settings.DATA_DIR, name=name, environment=environment
        )
        if existing is not None:
            _warn_on_drift(
                name=name, environment=environment, stored=existing, requested=requested
            )
            return UUID(str(existing["agent_id"]))
        created = jsonl_registry.get_or_create_agent(
            settings.DATA_DIR, name=name, environment=environment, **requested
        )
        return UUID(str(created["agent_id"]))

    if settings.BACKEND != "postgres":
        raise ValueError(
            f"rootsign.init() does not support ROOTSIGN_BACKEND={settings.BACKEND!r}. "
            "Use 'jsonl' (default) or 'postgres'."
        )

    # Postgres path — validate through the same schema `register_agent` uses so
    # a bad value fails identically on both paths.
    obj_in = AgentCreate(
        name=name,
        owner=owner,
        environment=AgentEnvironment(environment),
        risk_tier=AgentRiskTier(risk_tier),
        framework=AgentFramework(framework),
        description=description,
        model_version=model_version,
        permitted_tools=requested["permitted_tools"],
        regulatory_categories=requested["regulatory_categories"],
    )

    # Lazy imports — the DB stack is the postgres extra (ADR-011).
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from rootsign.database import AsyncSessionLocal
    from rootsign.models.agent import Agent

    values = obj_in.model_dump(mode="json")
    # The schema field is `metadata`; the ORM attribute is `extra_metadata`
    # (`Base.metadata` is SQLAlchemy's own). Same rename CRUDBase does.
    values["extra_metadata"] = values.pop("metadata", None)

    async with AsyncSessionLocal() as db:
        # ON CONFLICT DO NOTHING then SELECT: two concurrent first-runs both
        # end up with the same row instead of one hitting a unique violation.
        await db.execute(
            pg_insert(Agent)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_agents_name_environment")
        )
        await db.commit()
        result = await db.execute(
            select(Agent).where(Agent.name == name, Agent.environment == environment)
        )
        agent = result.scalar_one()
        stored = {
            "owner": agent.owner,
            "risk_tier": agent.risk_tier,
            "framework": agent.framework,
            "description": agent.description,
            "model_version": agent.model_version,
            "permitted_tools": list(agent.permitted_tools or []),
            "regulatory_categories": list(agent.regulatory_categories or []),
        }
        _warn_on_drift(
            name=name, environment=environment, stored=stored, requested=requested
        )
        return agent.agent_id
