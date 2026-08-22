"""CRUD for Action — owns the hash-chain write path and verification logic.

`create_with_hash` is the most critical write path in the platform. It must
execute steps 1-5 of the AGENTS.md spec atomically (within one transaction)
so concurrent writers never produce duplicate sequence_numbers or fork the chain.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rootsign.chain_state import new_record_id
from rootsign.verdict import (
    FailureKind,
    Verdict,
    decide,
    describe_missing,
    explains_break,
    missing_count,
    missing_ranges,
)
from rootsign.crud.base import CRUDBase
from rootsign.hashing import compute_action_self_hash
from rootsign.models.action import Action
from rootsign.models.session import AgentSession
from rootsign.schemas.action import ActionCreate

# `compute_payload_hash` lives in the SDK layer but is a pure leaf utility
# (stdlib-only, no rootsign imports), so importing it here introduces no
# cycle. It re-derives the input/output payload fingerprint that
# `compute_action_self_hash` binds into the chain via input_hash/output_hash.
from rootsign.sdk.hashing import compute_payload_hash


def _payload_binding_error(
    *,
    input_redacted: Any,
    input_hash: str,
    output_redacted: Any,
    output_hash: str | None,
    sequence_number: int,
) -> str | None:
    """Return an error string if a stored redacted payload no longer hashes
    to the input_hash/output_hash the chain protects, else None.

    audit #4: `self_hash` deliberately excludes `input_redacted`/
    `output_redacted` (ADR-001), so the chain proves the *hashes* are intact
    but not that the human-readable evidence still matches them. DB write
    access could rewrite a redacted payload without tripping TAMPERED. This
    re-binds them. Only checked when a redacted payload is actually present
    (void tools and non-dict payloads store NULL and are skipped).
    """
    if input_redacted is not None and compute_payload_hash(input_redacted) != input_hash:
        return (
            f"payload_hash mismatch at sequence_number={sequence_number}: "
            f"input_redacted does not match input_hash"
        )
    if output_redacted is not None and compute_payload_hash(output_redacted) != output_hash:
        return (
            f"payload_hash mismatch at sequence_number={sequence_number}: "
            f"output_redacted does not match output_hash"
        )
    return None


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
            select(AgentSession).where(AgentSession.session_id == session_id).with_for_update()
        )
        session_row = locked_session.scalar_one()

        # 2-3. Read prev tail; assign next sequence_number.
        prev_hash = session_row.chain_tail_hash
        seq = session_row.action_count + 1

        # 4. Compute self_hash from the canonical fields. The id is minted through
        #    the shared minting point (T2.3) so identity has exactly one source
        #    across all three backends — `rootsign.chain_state.new_record_id`.
        #    It must exist before hashing: action_id is inside the canonical input.
        action_id = new_record_id()
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

    async def get_session_chain(self, db: AsyncSession, *, session_id: UUID) -> list[Action]:
        """Return all actions for a session ordered by sequence_number ASC."""
        stmt = (
            select(Action)
            .where(Action.session_id == session_id)
            .order_by(Action.sequence_number.asc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def verify_chain(self, db: AsyncSession, *, session_id: UUID) -> dict[str, Any]:
        """Reconstruct the chain and verify each link.

        Returns:
            {
              "valid": bool,                      # False for both failure verdicts
              "verdict": "VALID"|"TAMPERED"|"INCOMPLETE",
              "record_count": int,
              "first_invalid_sequence": int | None,
              "missing_ranges": list[tuple[int, int]],
              "error": str | None,
            }

        `verdict` and `missing_ranges` were added in v0.3.0 (ADR-013 Decision
        4b) **additively**: `rootsign/mcp/server.py` publishes this dict as an
        MCP tool result, so removing or renaming a key breaks a published
        surface. `valid` therefore stays, meaning exactly "verdict is VALID".

        Gaps are as real here as they are offline — a partially uploaded sync,
        or a deleted row, which is arguably the more audit-relevant INCOMPLETE.
        The precedence rule is shared with the local verifier
        (`rootsign.verdict`) so the two can never disagree about the same
        session.
        """
        actions = await self.get_session_chain(db, session_id=session_id)
        if not actions:
            return {
                "valid": True,
                "verdict": Verdict.VALID.value,
                "record_count": 0,
                "first_invalid_sequence": None,
                "missing_ranges": [],
                "error": None,
            }

        gaps = missing_ranges([a.sequence_number for a in actions])

        def _result(
            kind: FailureKind | None, sequence: int | None = None, error: str | None = None
        ) -> dict[str, Any]:
            verdict = decide(missing=gaps, failure_kind=kind)
            if kind is None and gaps:
                sequence = gaps[0][0]
                error = (
                    f"{missing_count(gaps)} record(s) missing at sequence {describe_missing(gaps)}"
                )
            return {
                "valid": verdict is Verdict.VALID,
                "verdict": verdict.value,
                "record_count": len(actions),
                "first_invalid_sequence": sequence,
                "missing_ranges": gaps,
                "error": error,
            }

        seen: set[int] = set()
        expected_prev: str | None = None
        for action in actions:
            if action.sequence_number in seen:
                return _result(
                    FailureKind.DUPLICATE_SEQUENCE,
                    action.sequence_number,
                    f"duplicate sequence_number {action.sequence_number} (chain replay/corruption)",
                )
            seen.add(action.sequence_number)

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
                return _result(
                    FailureKind.SELF_HASH_MISMATCH,
                    action.sequence_number,
                    f"self_hash mismatch at sequence_number={action.sequence_number} "
                    f"(stored={action.self_hash}, recomputed={recomputed})",
                )
            if (action.prev_action_hash or None) != expected_prev:
                if not explains_break(action.sequence_number, gaps):
                    return _result(
                        FailureKind.PREV_HASH_MISMATCH,
                        action.sequence_number,
                        f"prev_action_hash mismatch at sequence_number={action.sequence_number}",
                    )
                # Explained by the gap immediately before this row: the
                # predecessor it names is not in the table. Re-anchor and keep
                # checking, so a deleted row cannot hide a rewritten one.
            binding_error = _payload_binding_error(
                input_redacted=action.input_redacted,
                input_hash=action.input_hash,
                output_redacted=action.output_redacted,
                output_hash=action.output_hash,
                sequence_number=action.sequence_number,
            )
            if binding_error is not None:
                return _result(FailureKind.PAYLOAD_BINDING, action.sequence_number, binding_error)
            expected_prev = action.self_hash

        return _result(None)


action = CRUDAction(Action, pk_attr="action_id")
