from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class ActionAuthorizationStatus(str, Enum):
    AUTO_AUTHORIZED = "auto_authorized"
    HUMAN_APPROVED = "human_approved"
    HUMAN_REJECTED = "human_rejected"
    PENDING = "pending"
    BYPASSED = "bypassed"


_HEX64 = r"^[0-9a-f]{64}$"


class ActionCreate(BaseModel):
    """Caller-supplied fields. sequence_number, prev_action_hash, and self_hash are
    assigned by CRUDAction.create_with_hash — not by the caller."""

    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    decision_id: UUID | None = None
    policy_id: UUID | None = None
    tool_name: str = Field(..., min_length=1, max_length=200)
    input_hash: str = Field(..., pattern=_HEX64)
    output_hash: str | None = Field(default=None, pattern=_HEX64)
    input_redacted: dict | None = None
    output_redacted: dict | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: int | None = Field(default=None, ge=0)
    authorization_status: ActionAuthorizationStatus = ActionAuthorizationStatus.AUTO_AUTHORIZED


class ActionUpdate(BaseModel):
    """Partial update of an Action.

    WARNING — `output_hash` IS a canonical hash input. The previous wording
    here ("self_hash inputs are immutable") was factually wrong: the canonical
    spec in `rootsign.hashing.compute_action_self_hash` covers `action_id`,
    `session_id`, `tool_name`, `input_hash`, **`output_hash`**,
    `prev_action_hash`, `timestamp` and `sequence_number`. Persisting a changed
    `output_hash` without recomputing `self_hash` — and every downstream
    `prev_action_hash` in the session — makes the chain fail verification with
    a `self_hash mismatch`, which reads as tampering.

    `output_redacted`, `duration_ms` and `authorization_status` are genuinely
    outside the canonical input and are safe to update in place. That
    asymmetry is the whole reason this docstring is explicit.

    This model currently has **no call sites** in `rootsign/`. v0.1.0+ writes
    each Action exactly once, complete, via `CRUDAction.create_with_hash` —
    the decorator does not run the tool until it has both hashes, so there is
    no fill-in-the-output-later path to serve. It is kept as the shape a future
    update path would take; wire it up only alongside a self_hash recompute.
    ADR-001 governs the hash spec and is frozen.
    """

    model_config = ConfigDict(extra="forbid")

    output_hash: str | None = Field(default=None, pattern=_HEX64)
    output_redacted: dict | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    authorization_status: ActionAuthorizationStatus | None = None


class Action(ActionCreate):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    action_id: UUID = Field(default_factory=uuid4)
    prev_action_hash: str | None = Field(default=None, pattern=_HEX64)
    self_hash: str = Field(..., pattern=_HEX64)
    sequence_number: int = Field(..., ge=1)
