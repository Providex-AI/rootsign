"""CRUD for Decision — opt-in reasoning capture for v0.1.1 (PRD-19, ADR-008).

Decision is NOT in the Action hash chain. `decision_id` is a logical FK on
Action only — see ADR-001 (frozen canonical spec) and ADR-008. This module
deliberately stays thin: inserts go through the standard `CRUDBase.create`
called by the ingest handler; we only add a read helper here for replay
queries.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rootsign.crud.base import CRUDBase
from rootsign.models.decision import Decision
from rootsign.schemas.decision import DecisionCreate


class CRUDDecision(CRUDBase[Decision, DecisionCreate]):
    async def get_by_session(
        self, db: AsyncSession, *, session_id: UUID
    ) -> list[Decision]:
        """Return all Decision rows for a session, oldest first.

        Used by replay queries to walk the Decision narrative for a
        session alongside the Action chain.
        """
        result = await db.execute(
            select(Decision)
            .where(Decision.session_id == session_id)
            .order_by(Decision.timestamp.asc())
        )
        return list(result.scalars().all())


decision = CRUDDecision(Decision, pk_attr="decision_id")
