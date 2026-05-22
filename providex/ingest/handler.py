"""IngestHandler — receives IngestEnvelopes and routes to CRUD operations.

handle() executes these steps in order, per AGENTS.md Task 12:
    1. Parse envelope → VALIDATION_ERROR on failure
    2. Version check → SCHEMA_VERSION_MISMATCH on MAJOR mismatch
    3. Idempotency check → return cached response on duplicate event_id
    4. Route by event_type to private handler
    5. Store result in idempotency store (only if accepted OR non-retryable)
    6. Return response

Errors from the CRUD layer surface as IngestError subclasses; handle()
catches them and translates to IngestResponse.rejected.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from providex import crud
from providex.errors import (
    IngestError,
    SessionAlreadyExistsError,
    SessionClosedError,
    SessionNotFoundError,
    UnknownAgentError,
)
from providex.ingest.idempotency import IdempotencyStore
from providex.ingest.schemas import (
    PAYLOAD_SCHEMAS,
    ActionRecordPayload,
    ApprovalRecordPayload,
    DecisionRecordPayload,
    ErrorCode,
    EventType,
    IngestEnvelope,
    IngestResponse,
    SessionClosePayload,
    SessionOpenPayload,
)
from providex.models.agent import Agent
from providex.models.session import ProvidexSession
from providex.schemas.action import ActionCreate
from providex.schemas.approval import ApprovalCreate
from providex.schemas.decision import DecisionCreate
from providex.schemas.session import SessionCreate, SessionStatus

logger = logging.getLogger("providex.ingest")

SUPPORTED_SCHEMA_MAJOR = "1"


class IngestHandler:
    def __init__(self, db: AsyncSession, idempotency: IdempotencyStore) -> None:
        self.db = db
        self.idempotency = idempotency

    # -----------------------------------------------------------------------
    # Public entrypoint
    # -----------------------------------------------------------------------

    async def handle(self, envelope: dict[str, Any]) -> IngestResponse:
        # Step 1: envelope parse
        try:
            env = IngestEnvelope.model_validate(envelope)
        except ValidationError as e:
            # We don't have a reliable event_id yet — fall back to a synthetic
            # zero UUID for telemetry. The SDK is expected to log this on its
            # side; the response is still well-formed.
            event_id = self._safe_event_id(envelope)
            return IngestResponse.rejected(
                event_id=event_id,
                error_code=ErrorCode.VALIDATION_ERROR,
                error_message=f"envelope validation failed: {e.errors()}",
                retryable=False,
            )

        # Step 2: version check (MAJOR only)
        major = env.schema_version.split(".", 1)[0]
        if major != SUPPORTED_SCHEMA_MAJOR:
            response = IngestResponse.rejected(
                event_id=env.event_id,
                error_code=ErrorCode.SCHEMA_VERSION_MISMATCH,
                error_message=(
                    f"store supports schema_version MAJOR={SUPPORTED_SCHEMA_MAJOR}, "
                    f"received {env.schema_version}"
                ),
                retryable=False,
            )
            await self._maybe_cache(env.event_id, response, env.emitted_at)
            return response

        # Step 3: idempotency
        cached = await self.idempotency.get(env.event_id)
        if cached is not None:
            logger.debug(
                "idempotency hit",
                extra={"event_id": str(env.event_id), "event_type": env.event_type},
            )
            return cached

        # Step 4: route + execute
        try:
            response = await self._dispatch(env)
        except IngestError as e:
            response = IngestResponse.rejected(
                event_id=env.event_id,
                error_code=ErrorCode(e.error_code),
                error_message=str(e),
                retryable=e.retryable,
            )

        # Step 5: cache (if eligible)
        await self._maybe_cache(env.event_id, response, env.emitted_at)
        return response

    # -----------------------------------------------------------------------
    # Dispatcher
    # -----------------------------------------------------------------------

    async def _dispatch(self, env: IngestEnvelope) -> IngestResponse:
        payload_schema = PAYLOAD_SCHEMAS[env.event_type]
        try:
            payload = payload_schema.model_validate(env.payload)
        except ValidationError as e:
            return IngestResponse.rejected(
                event_id=env.event_id,
                error_code=ErrorCode.VALIDATION_ERROR,
                error_message=f"payload validation failed: {e.errors()}",
                retryable=False,
            )

        if env.event_type is EventType.SESSION_OPEN:
            return await self._handle_session_open(env, payload)
        if env.event_type is EventType.ACTION_RECORD:
            return await self._handle_action_record(env, payload)
        if env.event_type is EventType.DECISION_RECORD:
            return await self._handle_decision_record(env, payload)
        if env.event_type is EventType.APPROVAL_RECORD:
            return await self._handle_approval_record(env, payload)
        if env.event_type is EventType.SESSION_CLOSE:
            return await self._handle_session_close(env, payload)
        # Unreachable — EventType is closed.
        raise IngestError(f"unhandled event_type: {env.event_type}")

    # -----------------------------------------------------------------------
    # Per-event handlers
    # -----------------------------------------------------------------------

    async def _handle_session_open(
        self, env: IngestEnvelope, payload: SessionOpenPayload
    ) -> IngestResponse:
        # Agent must exist
        agent = await self.db.get(Agent, env.agent_id)
        if agent is None:
            raise UnknownAgentError(f"agent_id={env.agent_id} not registered")

        # Session must NOT already exist (idempotency above handles the
        # duplicate event_id case)
        existing = await self.db.get(ProvidexSession, env.session_id)
        if existing is not None:
            raise SessionAlreadyExistsError(
                f"session_id={env.session_id} already exists"
            )

        session_obj = await crud.session.create(
            self.db,
            obj_in=SessionCreate(
                agent_id=env.agent_id,
                status=SessionStatus.RUNNING,
                user_id=payload.user_id,
                objective=payload.objective,
                metadata=payload.metadata,
            ),
            session_id=env.session_id,
        )
        return IngestResponse.accepted(
            event_id=env.event_id, entity_id=session_obj.session_id
        )

    async def _handle_action_record(
        self, env: IngestEnvelope, payload: ActionRecordPayload
    ) -> IngestResponse:
        session = await self._require_running_session(env.session_id)

        action = await crud.action.create_with_hash(
            self.db,
            obj_in=ActionCreate(
                session_id=env.session_id,
                decision_id=payload.decision_id,
                policy_id=payload.policy_id,
                tool_name=payload.tool_name,
                input_hash=payload.input_hash,
                output_hash=payload.output_hash,
                input_redacted=payload.input_redacted,
                output_redacted=payload.output_redacted,
                timestamp=payload.timestamp,
                duration_ms=payload.duration_ms,
                authorization_status=payload.authorization_status,
            ),
            session_obj=session,
        )
        return IngestResponse.accepted(
            event_id=env.event_id,
            entity_id=action.action_id,
            sequence_number=action.sequence_number,
            self_hash=action.self_hash,
        )

    async def _handle_decision_record(
        self, env: IngestEnvelope, payload: DecisionRecordPayload
    ) -> IngestResponse:
        session = await self._require_running_session(env.session_id)

        reasoning_captured = (
            payload.reasoning_captured
            if payload.reasoning_captured is not None
            else payload.reasoning_summary is not None
        )

        decision = await crud.decision.create(
            self.db,
            obj_in=DecisionCreate(
                session_id=env.session_id,
                selected_action=payload.selected_action,
                inputs_summary=payload.inputs_summary,
                reasoning_summary=payload.reasoning_summary,
                confidence=payload.confidence,
                alternatives_considered=payload.alternatives_considered,
                timestamp=payload.timestamp,
                reasoning_captured=reasoning_captured,
            ),
        )
        # AC-3.6: increment session.decision_count atomically with the insert.
        # We're inside the same AsyncSession so it's already in the same
        # transaction.
        session.decision_count = (session.decision_count or 0) + 1
        self.db.add(session)
        await self.db.flush()

        return IngestResponse.accepted(
            event_id=env.event_id, entity_id=decision.decision_id
        )

    async def _handle_approval_record(
        self, env: IngestEnvelope, payload: ApprovalRecordPayload
    ) -> IngestResponse:
        await self._require_running_session(env.session_id)

        approval = await crud.approval.create_with_action_status_update(
            self.db,
            obj_in=ApprovalCreate(
                action_id=payload.action_id,
                session_id=env.session_id,
                approver_id=payload.approver_id,
                approver_type=payload.approver_type,
                context_presented=payload.context_presented,
                decision=payload.decision,
                decision_reason=payload.decision_reason,
                timestamp=payload.timestamp,
                response_latency_ms=payload.response_latency_ms,
                parent_approval_id=payload.parent_approval_id,
            ),
        )
        return IngestResponse.accepted(
            event_id=env.event_id, entity_id=approval.approval_id
        )

    async def _handle_session_close(
        self, env: IngestEnvelope, payload: SessionClosePayload
    ) -> IngestResponse:
        session = await self._require_running_session(env.session_id)

        # Reconcile action_count against payload metadata if provided (AC-3.11).
        if payload.metadata is not None:
            reported_total = payload.metadata.get("total_actions")
            if (
                isinstance(reported_total, int)
                and reported_total != session.action_count
            ):
                logger.warning(
                    "session_close action_count mismatch",
                    extra={
                        "session_id": str(env.session_id),
                        "stored_action_count": session.action_count,
                        "reported_total_actions": reported_total,
                    },
                )

        from datetime import datetime, timezone

        session.status = payload.status
        session.end_time = datetime.now(timezone.utc)
        self.db.add(session)
        await self.db.flush()

        return IngestResponse.accepted(
            event_id=env.event_id, entity_id=session.session_id
        )

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    async def _require_running_session(self, session_id: UUID) -> ProvidexSession:
        session = await self.db.get(ProvidexSession, session_id)
        if session is None:
            raise SessionNotFoundError(f"session_id={session_id} not found")
        if session.status != "running":
            raise SessionClosedError(
                f"session_id={session_id} is {session.status!r}, not running"
            )
        return session

    async def _maybe_cache(
        self,
        event_id: UUID,
        response: IngestResponse,
        emitted_at: Any,
    ) -> None:
        """Cache accepted + non-retryable rejected; skip retryable failures.

        See feedback_req03_decisions in memory for the policy rationale.
        """
        if response.status == "accepted":
            await self.idempotency.set(event_id, response, emitted_at)
            return
        if response.retryable is False:
            await self.idempotency.set(event_id, response, emitted_at)

    @staticmethod
    def _safe_event_id(envelope: dict[str, Any]) -> UUID:
        """Extract a usable event_id when envelope validation fails.

        Used only in the IngestResponse for telemetry — callers can still
        correlate the rejection with their request. Falls back to all-zero
        UUID if the envelope didn't supply one.
        """
        raw = envelope.get("event_id") if isinstance(envelope, dict) else None
        if isinstance(raw, str):
            try:
                return UUID(raw)
            except (ValueError, TypeError):
                pass
        if isinstance(raw, UUID):
            return raw
        return UUID(int=0)
