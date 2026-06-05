"""CRUD for Action — owns the hash-chain write path and verification logic.

`create_with_hash` is the most critical write path in the platform. It must
execute steps 1-5 of the AGENTS.md spec atomically (within one transaction)
so concurrent writers never produce duplicate sequence_numbers or fork the chain.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rootsign.crud.base import CRUDBase
from rootsign.hashing import compute_action_self_hash
from rootsign.models.action import Action
from rootsign.models.session import AgentSession
from rootsign.schemas.action import ActionCreate


class CRUDAction(CRUDBase[Action, ActionCreate]):
    async def create_with_hash(
        self,
        db: AsyncSession,
        *,
        obj_in: ActionCreate,
        session_obj: AgentSession | None = None,
    ) -> Action:
        """Insert an Action while extending the session hash chain.

        Steps (all in one transaction, holding a row lock on the session):
          1. Lock the session row via SELECT FOR UPDATE
          2. Read current chain_tail_hash as prev_action_hash
          3. Assign sequence_number = action_count + 1
          4. Compute self_hash via the canonical spec
          5. Insert Action
          6. Update session.chain_tail_hash (and chain_head_hash if first)
          7. Increment session.action_count

        Returns the inserted Action with self_hash populated.
        """
        session_id = obj_in.session_id

        # 1. Row-lock the session — serializes concurrent writers.
        locked_session = await db.execute(
            select(AgentSession)
            .where(AgentSession.session_id == session_id)
            .with_for_update()
        )
        session_row = locked_session.scalar_one()

        # 2-3. Read prev tail; assign next sequence_number.
        prev_hash = session_row.chain_tail_hash
        seq = session_row.action_count + 1

        # 4. Compute self_hash from the canonical fields. action_id is generated here
        #    so it can be part of the canonical representation.
        action_id = uuid4()
        canonical_input: dict[str, Any] = {
            "action_id": action_id,
            "session_id": session_id,
            "tool_name": obj_in.tool_name,
            "input_hash": obj_in.input_hash,
            "output_hash": obj_in.output_hash,
            "prev_action_hash": prev_hash,
            "timestamp": obj_in.timestamp,
            "sequence_number": seq,
        }
        self_hash = compute_action_self_hash(canonical_input)

        # 5. Insert the Action row.
        db_action = Action(
            action_id=action_id,
            session_id=session_id,
            decision_id=obj_in.decision_id,
            policy_id=obj_in.policy_id,
            tool_name=obj_in.tool_name,
            input_hash=obj_in.input_hash,
            output_hash=obj_in.output_hash,
            input_redacted=obj_in.input_redacted,
            output_redacted=obj_in.output_redacted,
            prev_action_hash=prev_hash,
            self_hash=self_hash,
            timestamp=obj_in.timestamp,
            duration_ms=obj_in.duration_ms,
            authorization_status=obj_in.authorization_status.value
            if hasattr(obj_in.authorization_status, "value")
            else obj_in.authorization_status,
            sequence_number=seq,
        )
        db.add(db_action)

        # 6-7. Update session denormalized fields.
        if session_row.chain_head_hash is None:
            session_row.chain_head_hash = self_hash
        session_row.chain_tail_hash = self_hash
        session_row.action_count = seq
        db.add(session_row)

        await db.flush()
        await db.refresh(db_action)

        # Keep the in-memory session_obj passed by the caller in sync.
        if session_obj is not None and session_obj is not session_row:
            session_obj.chain_head_hash = session_row.chain_head_hash
            session_obj.chain_tail_hash = session_row.chain_tail_hash
            session_obj.action_count = session_row.action_count

        return db_action

    async def get_session_chain(
        self, db: AsyncSession, *, session_id: UUID
    ) -> list[Action]:
        """Return all actions for a session ordered by sequence_number ASC."""
        stmt = (
            select(Action)
            .where(Action.session_id == session_id)
            .order_by(Action.sequence_number.asc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def verify_chain(
        self, db: AsyncSession, *, session_id: UUID
    ) -> dict[str, Any]:
        """Reconstruct the chain and verify each link.

        Returns:
            {
              "valid": bool,
              "record_count": int,
              "first_invalid_sequence": int | None,
              "error": str | None,
            }
        """
        actions = await self.get_session_chain(db, session_id=session_id)
        if not actions:
            return {
                "valid": True,
                "record_count": 0,
                "first_invalid_sequence": None,
                "error": None,
            }

        expected_prev: str | None = None
        for idx, action in enumerate(actions, start=1):
            # Recompute the canonical self_hash from the stored canonical fields.
            recomputed = compute_action_self_hash(
                {
                    "action_id": action.action_id,
                    "session_id": action.session_id,
                    "tool_name": action.tool_name,
                    "input_hash": action.input_hash,
                    "output_hash": action.output_hash,
                    "prev_action_hash": action.prev_action_hash,
                    "timestamp": action.timestamp,
                    "sequence_number": action.sequence_number,
                }
            )
            if recomputed != action.self_hash:
                return {
                    "valid": False,
                    "record_count": len(actions),
                    "first_invalid_sequence": action.sequence_number,
                    "error": (
                        f"self_hash mismatch at sequence_number={action.sequence_number} "
                        f"(stored={action.self_hash}, recomputed={recomputed})"
                    ),
                }
            if action.sequence_number != idx:
                return {
                    "valid": False,
                    "record_count": len(actions),
                    "first_invalid_sequence": action.sequence_number,
                    "error": (
                        f"sequence gap or duplicate: expected {idx}, "
                        f"got {action.sequence_number}"
                    ),
                }
            if (action.prev_action_hash or None) != expected_prev:
                return {
                    "valid": False,
                    "record_count": len(actions),
                    "first_invalid_sequence": action.sequence_number,
                    "error": (
                        f"prev_action_hash mismatch at sequence_number={action.sequence_number}"
                    ),
                }
            expected_prev = action.self_hash

        return {
            "valid": True,
            "record_count": len(actions),
            "first_invalid_sequence": None,
            "error": None,
        }


action = CRUDAction(Action, pk_attr="action_id")
