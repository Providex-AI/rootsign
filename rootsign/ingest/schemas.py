"""Pydantic schemas for the SDK ↔ store wire format.

Top-level types:
  * `IngestEnvelope` — the outer envelope every event arrives in
  * `IngestResponse` — the unified accept/reject reply
  * `EventType` — enum of the 5 supported event types
  * `ErrorCode` — full registry from Spec Section 5.3 (plus the two Req 0.3
    additions: APPROVAL_PARENT_NOT_FOUND, ACTION_ALREADY_RESOLVED)

Per-event-type payload schemas live here too. The handler validates the
envelope first, then re-validates `envelope.payload` against the appropriate
PayloadSchema for the event_type. This keeps a clean error_code surface:
envelope problems → VALIDATION_ERROR, payload problems → VALIDATION_ERROR
with field-level detail in error_message.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class EventType(str, Enum):
    SESSION_OPEN = "SESSION_OPEN"
    ACTION_RECORD = "ACTION_RECORD"
    DECISION_RECORD = "DECISION_RECORD"
    APPROVAL_RECORD = "APPROVAL_RECORD"
    SESSION_CLOSE = "SESSION_CLOSE"


class ErrorCode(str, Enum):
    # Non-retryable client errors
    SCHEMA_VERSION_MISMATCH = "SCHEMA_VERSION_MISMATCH"
    UNKNOWN_AGENT = "UNKNOWN_AGENT"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    SESSION_CLOSED = "SESSION_CLOSED"
    SESSION_ALREADY_EXISTS = "SESSION_ALREADY_EXISTS"
    DUPLICATE_EVENT = "DUPLICATE_EVENT"
    ACTION_NOT_FOUND = "ACTION_NOT_FOUND"
    ACTION_ALREADY_RESOLVED = "ACTION_ALREADY_RESOLVED"  # Req 0.3 addition
    APPROVAL_PARENT_NOT_FOUND = "APPROVAL_PARENT_NOT_FOUND"  # Req 0.3 addition
    VALIDATION_ERROR = "VALIDATION_ERROR"
    HASH_CHAIN_BROKEN = "HASH_CHAIN_BROKEN"
    # Retryable transient errors
    STORE_UNAVAILABLE = "STORE_UNAVAILABLE"
    WRITE_TIMEOUT = "WRITE_TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# Envelope + response
# ---------------------------------------------------------------------------


class IngestEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(..., pattern=r"^\d+\.\d+$")
    sdk_version: str = Field(..., min_length=1, max_length=64)
    event_type: EventType
    event_id: UUID
    emitted_at: datetime
    agent_id: UUID
    session_id: UUID
    payload: dict[str, Any]


class IngestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str  # "accepted" | "rejected"
    event_id: UUID
    entity_id: UUID | None = None
    sequence_number: int | None = None
    self_hash: str | None = None
    error_code: ErrorCode | None = None
    error_message: str | None = None
    retryable: bool | None = None

    @classmethod
    def accepted(
        cls,
        *,
        event_id: UUID,
        entity_id: UUID | None = None,
        sequence_number: int | None = None,
        self_hash: str | None = None,
    ) -> Self:
        return cls(
            status="accepted",
            event_id=event_id,
            entity_id=entity_id,
            sequence_number=sequence_number,
            self_hash=self_hash,
        )

    @classmethod
    def rejected(
        cls,
        *,
        event_id: UUID,
        error_code: ErrorCode,
        error_message: str,
        retryable: bool,
    ) -> Self:
        return cls(
            status="rejected",
            event_id=event_id,
            error_code=error_code,
            error_message=error_message,
            retryable=retryable,
        )


# ---------------------------------------------------------------------------
# Per-event-type payload schemas
# ---------------------------------------------------------------------------


# NB: SessionOpenPayload / SessionClosePayload deliberately use extra="allow"
# while every other payload forbids extras. Session envelopes are the
# forward-compatibility seam — SDK versions ahead of the store may attach new
# session-level fields (extra tracing context, framework hints) that older
# stores should tolerate and pass through rather than reject, since a rejected
# SESSION_OPEN/CLOSE would break the whole session. Per-event payloads
# (ACTION/DECISION/APPROVAL) stay extra="forbid" because they feed the hash
# chain and must be exact. (audit #12a — documenting the intentional asymmetry.)
class SessionOpenPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    objective: str | None = Field(default=None, max_length=2000)
    user_id: str | None = Field(default=None, max_length=200)
    metadata: dict | None = None


class ActionRecordPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(..., min_length=1, max_length=200)
    input_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    output_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    input_redacted: dict | None = None
    output_redacted: dict | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: int | None = Field(default=None, ge=0)
    decision_id: UUID | None = None
    policy_id: UUID | None = None
    authorization_status: str = "auto_authorized"


class DecisionRecordPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_action: str = Field(..., min_length=1, max_length=200)
    inputs_summary: str | None = Field(default=None, max_length=5000)
    reasoning_summary: str | None = Field(default=None, max_length=10000)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    alternatives_considered: list[str] | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    reasoning_depth: str = "summary"
    reasoning_captured: bool | None = None


class ApprovalRecordPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: UUID
    approver_id: str = Field(..., min_length=1, max_length=200)
    approver_type: str
    context_presented: dict
    decision: str
    decision_reason: str | None = Field(default=None, max_length=2000)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    response_latency_ms: int | None = Field(default=None, ge=0)
    parent_approval_id: UUID | None = None


class SessionClosePayload(BaseModel):
    # extra="allow" for the same forward-compat reason as SessionOpenPayload.
    model_config = ConfigDict(extra="allow")

    status: str
    error_message: str | None = Field(default=None, max_length=2000)
    metadata: dict | None = None

    @model_validator(mode="after")
    def _validate_status(self) -> Self:
        if self.status not in {"completed", "failed", "abandoned"}:
            raise ValueError(
                f"status must be one of completed/failed/abandoned, got {self.status!r}"
            )
        return self


PAYLOAD_SCHEMAS: dict[EventType, type[BaseModel]] = {
    EventType.SESSION_OPEN: SessionOpenPayload,
    EventType.ACTION_RECORD: ActionRecordPayload,
    EventType.DECISION_RECORD: DecisionRecordPayload,
    EventType.APPROVAL_RECORD: ApprovalRecordPayload,
    EventType.SESSION_CLOSE: SessionClosePayload,
}
