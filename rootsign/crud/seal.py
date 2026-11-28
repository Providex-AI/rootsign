"""A stored session's seal facts (ADR-016), read the one way both sides read them.

The ingest handler seals a session from these facts when it closes, and
`verify_session` checks the seal against them. Reading them through one
function is what makes the two agree: a field the writer projected one way
and the verifier another would turn an honest session into a TAMPERED one.

Queries filter by `session_id`, never by `action_id` alone, so the actions
hypertable is scanned by its session index, as `verify_chain` does.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rootsign.models.action import Action
from rootsign.models.approval import Approval
from rootsign.models.decision import Decision
from rootsign.models.session import AgentSession
from rootsign.seal import ACTION_SEAL_FIELDS, APPROVAL_FIELDS, DECISION_FIELDS, SessionFacts


def _row(obj: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    return {name: getattr(obj, name) for name in fields}


async def session_facts(
    db: AsyncSession, session: AgentSession, *, status: str | None
) -> SessionFacts:
    """What `session` holds, as the seal covers it. `status` is the close status."""
    session_id: UUID = session.session_id
    actions = (
        await db.execute(
            select(Action).where(Action.session_id == session_id).order_by(Action.sequence_number)
        )
    ).scalars()
    decisions = (
        await db.execute(select(Decision).where(Decision.session_id == session_id))
    ).scalars()
    approvals = (
        await db.execute(select(Approval).where(Approval.session_id == session_id))
    ).scalars()
    return SessionFacts(
        session_id=session_id,
        agent_id=session.agent_id,
        objective=session.objective,
        user_id=session.user_id,
        status=status,
        decisions=tuple(_row(d, DECISION_FIELDS) for d in decisions),
        approvals=tuple(_row(a, APPROVAL_FIELDS) for a in approvals),
        actions=tuple(_row(a, ACTION_SEAL_FIELDS) for a in actions),
    )
