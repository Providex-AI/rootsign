"""@rootsign.trace — wraps a tool call and emits an ACTION_RECORD per call.

The decorator routes by callable shape:

  * LangChain `BaseTool` → `LangGraphTracer.wrap_tool` (ADR-004). The wrapped
    tool stays a `BaseTool` so it's a drop-in for `ToolNode([...])`.
  * CrewAI tool (duck-typed `.name: str` + `._run: callable`) →
    `CrewAITracer.wrap_tool` (ADR-005). Checked AFTER LangChain because
    LangChain's `StructuredTool` also satisfies the CrewAI shape.
  * Plain async/sync callable → the framework-agnostic wrapper retained from
    Sprint 1 — same envelope shape, same failure-isolation rule.

Failure isolation (ADR-002):
  The wrapped function's success/failure is the ONLY thing that bubbles up.
  Ingest errors are logged at WARNING and swallowed; the Sprint 3 WAL drain
  turns those warnings into eventual delivery.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from rootsign.ingest.schemas import EventType, IngestResponse
from rootsign.sdk.client import IngestClient
from rootsign.sdk.context import SessionContext
from rootsign.sdk.hashing import compute_payload_hash
from rootsign.sdk.redaction import RedactionConfig

logger = logging.getLogger("rootsign.sdk")

from rootsign._version import SDK_VERSION  # noqa: F401  (re-exported via envelopes)

SCHEMA_VERSION = "1.0"


def _to_json_safe(value: Any) -> Any:
    """Coerce a value into a JSON-serializable shape for storage + hashing.

    Defangs non-JSON returns (LangChain BaseMessage, dataclasses, arbitrary
    objects) before they reach the JSONB column or `compute_payload_hash`.

    Why this exists: LangGraph's ToolNode invokes tools via the LangChain
    ToolCall envelope (`tool.ainvoke({"name", "args", "id", "type"})`).
    BaseTool then wraps the tool's plain-string return into a `ToolMessage`
    before returning. Without this normalization the ToolMessage trips
    JSONB serialization and the ACTION_RECORD never inserts.

    Behavior:
      * `None`, primitives, JSON-clean dicts/lists/tuples → preserved
        (tuples become lists for spec parity).
      * LangChain BaseMessage duck-types (have both `.content` and `.type`)
        → `{"_message_type": <type>, "content": <recursed content>}` so the
        agent's intent is captured without dragging in private fields like
        `tool_call_id`, `additional_kwargs`, or `usage_metadata`.
      * Anything else → `str(value)` as a last resort so the hash never
        explodes.

    Deterministic by construction — calling it twice on equal inputs
    produces equal outputs, so input/output hashes stay stable.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _to_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json_safe(v) for v in value]
    content = getattr(value, "content", None)
    msg_type = getattr(value, "type", None)
    if content is not None and isinstance(msg_type, str):
        return {"_message_type": msg_type, "content": _to_json_safe(content)}
    return str(value)


def _is_langchain_tool(func: Any) -> bool:
    """True if *func* is a LangChain BaseTool instance.

    Returns False (never raises) when langchain_core is not installed, so the
    SDK can be used without any framework extras.
    """
    try:
        from langchain_core.tools import BaseTool
    except ImportError:
        return False
    return isinstance(func, BaseTool)


def trace(
    *,
    ingest_client: IngestClient,
    session_context: SessionContext,
    tool_name: str | None = None,
    redaction_config: RedactionConfig | None = None,
    require_approval: bool = False,
    approval_context_builder: Callable[[str, dict[str, Any]], dict] | None = None,
    poll_interval_seconds: float = 2.0,
    timeout_seconds: float = 300.0,
) -> Callable[[Any], Any]:
    """Decorator factory. Wraps a callable to emit an ACTION_RECORD per call.

    Args:
        ingest_client: Where to send the envelope. See ADR-002.
        session_context: Carries agent_id, session_id, and the monotonic
            sequence counter. A SESSION_OPEN must already have been sent for
            this session_id (use `rootsign.session(...)` to do this
            automatically).
        tool_name: Logical name for the tool. Defaults to the wrapped
            function's `__name__`. Ignored for the `BaseTool` path since
            LangGraph relies on the original `tool.name`.
        redaction_config: Applied to input args + output before hashing /
            persisting. None ⇒ no redaction.
        require_approval: When True, the tool will not execute until a
            human approves via `rootsign approve <id>` (or the timeout
            fires). The ACTION_RECORD is inserted with
            `authorization_status='pending'` BEFORE the wait; verify_chain
            still sees a complete chain regardless of approval timing
            (ADR-007). Currently supported only for plain async callables —
            the LangGraph/CrewAI paths raise NotImplementedError so users
            get a clear "wrap the underlying tool function" hint rather
            than silently degrading to auto-authorized.
        approval_context_builder: Callable(tool_name, redacted_input) → dict
            building the context the operator sees via
            `rootsign approve --list`. The second arg is the REDACTED
            input payload — never the raw input — because the return
            value is persisted into `approvals.context_presented` and
            must not carry raw PII. Default is
            ``{"tool_name": ..., "input_summary": str(redacted)[:500]}``.
        poll_interval_seconds: HiTLCheckpoint poll cadence. Default 2.0s.
        timeout_seconds: HiTL deadline. After this, the action transitions
            to `timed_out` and `HiTLTimeoutError` is raised. Default 300s.

    Keyword-only signature: matches the Sprint 1 surface so existing call
    sites and tests continue to work unchanged.
    """

    def decorator(func: Any) -> Any:
        if _is_langchain_tool(func):
            if require_approval:
                raise NotImplementedError(
                    "@rootsign.trace(require_approval=True) does not yet wrap "
                    "LangChain BaseTool — wrap the underlying function and "
                    "expose it as a tool from there. Tracking issue: Phase 2."
                )
            # Lazy import — only touched when langchain_core is present.
            from rootsign.sdk.frameworks.langgraph import LangGraphTracer

            return LangGraphTracer.wrap_tool(
                func,
                ctx=session_context,
                client=ingest_client,
                redaction_config=redaction_config,
            )

        # CrewAI tools — duck-typed AFTER the LangChain check because
        # LangChain's StructuredTool also has .name (str) and ._run
        # (callable). See ADR-005.
        from rootsign.sdk.frameworks.crewai import _is_crewai_tool

        if _is_crewai_tool(func):
            if require_approval:
                raise NotImplementedError(
                    "@rootsign.trace(require_approval=True) does not yet wrap "
                    "CrewAI tools — wrap the underlying function and expose "
                    "it as a tool from there. Tracking issue: Phase 2."
                )
            from rootsign.sdk.frameworks.crewai import CrewAITracer

            return CrewAITracer.wrap_tool(
                func,
                ctx=session_context,
                client=ingest_client,
                redaction_config=redaction_config,
            )

        # Plain-callable path (backward compat with Sprint 1 smoke test).
        _tool_name = tool_name or getattr(func, "__name__", "unknown_tool")

        if require_approval:

            @functools.wraps(func)
            async def hitl_wrapper(*args: Any, **kwargs: Any) -> Any:
                return await _emit_hitl_action(
                    func=func,
                    args=args,
                    kwargs=kwargs,
                    tool_name=_tool_name,
                    client=ingest_client,
                    ctx=session_context,
                    redaction_config=redaction_config,
                    approval_context_builder=approval_context_builder,
                    poll_interval_seconds=poll_interval_seconds,
                    timeout_seconds=timeout_seconds,
                )

            return hitl_wrapper

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            return await _emit_action_record(
                func=func,
                args=args,
                kwargs=kwargs,
                tool_name=_tool_name,
                client=ingest_client,
                ctx=session_context,
                redaction_config=redaction_config,
            )

        return wrapper

    return decorator


async def _emit_action_record(
    *,
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    tool_name: str,
    client: IngestClient,
    ctx: SessionContext,
    redaction_config: RedactionConfig | None,
) -> Any:
    """Shared emission logic for both plain and LangGraph paths.

    Runs *func* (sync or async), computes input/output hashes around it, then
    fires the ACTION_RECORD envelope. Best-effort ingest — never raises into
    the caller. If *func* raises, the exception is re-raised AFTER the ingest
    attempt so we still record the failed action.
    """
    input_payload: dict[str, Any] = _to_json_safe({"args": list(args), "kwargs": dict(kwargs)})
    redacted_input = redaction_config.redact(input_payload) if redaction_config else input_payload
    input_hash = compute_payload_hash(redacted_input)
    timestamp = datetime.now(timezone.utc)

    # Consume the pending Decision slot BEFORE the tool runs so a failed
    # tool still records the decision_id on its ACTION_RECORD. Single-slot
    # semantics (ADR-008): if record_decision wasn't called or the slot
    # was already consumed by a concurrent action, this returns None and
    # the payload's decision_id will be null.
    decision_id = await ctx._consume_pending_decision_id()

    result: Any = None
    redacted_output: dict[str, Any] | None = None
    output_hash: str | None = None
    error: BaseException | None = None
    try:
        if asyncio.iscoroutinefunction(func):
            result = await func(*args, **kwargs)
        else:
            maybe = func(*args, **kwargs)
            # A sync callable that returns a coroutine — await it. Covers the
            # LangGraphTracer adapter where _call is sync but its body awaits
            # the original ainvoke.
            result = await maybe if asyncio.iscoroutine(maybe) else maybe
        output_payload = _to_json_safe({"result": result})
        redacted_output = (
            redaction_config.redact(output_payload) if redaction_config else output_payload
        )
        output_hash = compute_payload_hash(redacted_output)
    except BaseException as exc:
        error = exc

    await _try_ingest(
        client=client,
        ctx=ctx,
        tool_name=tool_name,
        input_hash=input_hash,
        output_hash=output_hash,
        redacted_input=redacted_input,
        redacted_output=redacted_output,
        timestamp=timestamp,
        decision_id=decision_id,
    )

    if error is not None:
        raise error
    return result


async def _try_ingest(
    *,
    client: IngestClient,
    ctx: SessionContext,
    tool_name: str,
    input_hash: str,
    output_hash: str | None,
    redacted_input: Any,
    redacted_output: Any,
    timestamp: datetime,
    authorization_status: str = "auto_authorized",
    decision_id: UUID | None = None,
) -> IngestResponse | None:
    """Best-effort ingest — never raises (ADR-002 failure isolation rule).

    Returns the `IngestResponse` (which carries `entity_id` = action_id) on
    success, or None when the ingest failed. The original plain-trace path
    discards the return value; the HiTL path uses `entity_id` to bind the
    HiTLCheckpoint to the inserted row.

    `authorization_status` defaults to 'auto_authorized' so existing call
    sites keep their semantics. The HiTL wrapper passes 'pending' to
    publish the ACTION_RECORD before the human-decision wait.

    `decision_id` populates the ACTION_RECORD payload's `decision_id` field
    (already present on ActionRecordPayload as `UUID | None`). Callers fetch
    it from `ctx._consume_pending_decision_id()` so a Decision recorded just
    before this action carries through. None when Decision capture is off
    or the pending slot is already spent. See ADR-008.
    """
    try:
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "sdk_version": SDK_VERSION,
            "event_type": EventType.ACTION_RECORD.value,
            "event_id": str(uuid4()),
            "emitted_at": datetime.now(timezone.utc).isoformat(),
            "agent_id": str(ctx.agent_id),
            "session_id": str(ctx.session_id),
            "payload": {
                "tool_name": tool_name,
                "input_hash": input_hash,
                "output_hash": output_hash,
                "input_redacted": redacted_input if isinstance(redacted_input, dict) else None,
                "output_redacted": redacted_output if isinstance(redacted_output, dict) else None,
                "timestamp": timestamp.isoformat(),
                "authorization_status": authorization_status,
                "decision_id": str(decision_id) if decision_id else None,
            },
        }
        response = await client.handle(envelope)
        # audit #9: advance the SDK counter ONLY when the store accepted the
        # ACTION_RECORD, so SESSION_CLOSE's total_actions (ctx.current_sequence)
        # matches the store's action_count and the AC-3.11 reconciliation
        # warning doesn't fire spuriously on a failed/rejected ingest. The
        # counter is never transmitted — the store assigns the authoritative
        # sequence_number under its session lock and returns it below.
        if response is not None and response.status == "accepted":
            await ctx.next_sequence()
        logger.debug(
            "ACTION_RECORD tool=%s status=%s ingest=%s store_seq=%s",
            tool_name,
            authorization_status,
            response.status if response is not None else "failed",
            response.sequence_number if response is not None else None,
        )
        return response
    except Exception as ingest_err:  # noqa: BLE001 — see failure isolation rule
        logger.warning("rootsign ingest failed for tool %s: %s", tool_name, ingest_err)
        return None


async def _emit_hitl_action(
    *,
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    tool_name: str,
    client: IngestClient,
    ctx: SessionContext,
    redaction_config: RedactionConfig | None,
    approval_context_builder: Callable[[str, dict[str, Any]], dict] | None,
    poll_interval_seconds: float,
    timeout_seconds: float,
) -> Any:
    """HiTL-gated tool execution. Spec §S4-TASK 6.

    Flow:
      1. Hash + redact the input.
      2. Build the `context_presented` dict the operator will see in
         `rootsign approve --list` — caller-supplied builder or default.
      3. Insert an ACTION_RECORD via the IngestClient with
         `authorization_status='pending'` and `output_hash=None`. The
         IngestResponse carries the assigned `action_id` (entity_id).
      4. Wait on `HiTLCheckpoint` bound to that action_id +
         action_timestamp. Uses `AsyncSessionLocal` as the per-cycle
         session factory so the loop is isolated from the caller's
         session.
      5. On `HiTLRejectedError` / `HiTLTimeoutError`: propagate. The
         tool is NEVER executed in those paths — that's the whole point
         of the gate.
      6. On approval: run the tool synchronously and return its result.

    What this does NOT do (v0.1.0 limitation, documented in ADR-007):
      * The output is returned to the caller but NOT written back into
        the Action row's `output_hash` / `output_redacted` columns —
        those stay NULL for HiTL actions. The hash chain is computed at
        insert time and stays valid; updating output post-hoc would
        invalidate `self_hash`. Output capture for HiTL is a Phase 2 RPC.
      * The wrapper does NOT emit a follow-up APPROVAL_RECORD envelope.
        The CLI's direct DB write (via `create_with_chain_link`) is the
        single source of truth. Re-emitting would hit the
        `ActionAlreadyResolvedError` guard and log a noisy warning for
        no semantic gain.
    """
    # Lazy imports — HiTL is a Sprint 4 surface and pulling these at
    # module load would pessimize cold start for non-HiTL users.
    from rootsign.database import AsyncSessionLocal
    from rootsign.sdk.hitl import HiTLCheckpoint

    # 1. Hash + redact input
    input_payload: dict[str, Any] = {"args": list(args), "kwargs": dict(kwargs)}
    redacted_input = redaction_config.redact(input_payload) if redaction_config else input_payload
    input_hash = compute_payload_hash(redacted_input)
    timestamp = datetime.now(timezone.utc)

    # 2. Build context_presented from the REDACTED input — never the raw
    #    payload. The HiTL CLI / poll loop persists this dict to
    #    `approvals.context_presented`, so raw PII landing here would
    #    survive in the audit table indefinitely. Audit fix.
    if approval_context_builder is not None:
        context_presented = approval_context_builder(tool_name, redacted_input)
    else:
        # Cap the summary at 500 chars so a multi-MB JSON payload doesn't
        # become unreadable in the CLI listing. Operators wanting the
        # full payload can fetch input_redacted directly.
        context_presented = {
            "tool_name": tool_name,
            "input_summary": str(redacted_input)[:500],
        }

    # 3. Submit pending ACTION_RECORD — must succeed; HiTL cannot proceed
    #    without a row to poll on. Unlike the auto_authorized path which
    #    treats ingest as best-effort, here a failed envelope means there
    #    is no DB row to vote on, so the wait would deadlock for
    #    `timeout_seconds`. Raise immediately so the caller can retry or
    #    fall back to auto-authorized.
    #
    # Consume the pending Decision slot before submitting so the pending
    # ACTION_RECORD carries decision_id to its eventual approved/rejected
    # outcome. Single-slot semantics per ADR-008 — same contract as the
    # auto-authorized path.
    decision_id = await ctx._consume_pending_decision_id()
    response = await _try_ingest(
        client=client,
        ctx=ctx,
        tool_name=tool_name,
        input_hash=input_hash,
        output_hash=None,
        redacted_input=redacted_input,
        redacted_output=None,
        timestamp=timestamp,
        authorization_status="pending",
        decision_id=decision_id,
    )
    if response is None or response.entity_id is None:
        raise RuntimeError(
            f"rootsign: failed to submit pending ACTION_RECORD for tool "
            f"{tool_name!r}; HiTL gate cannot proceed without an action_id. "
            "Check IngestClient connectivity and SESSION_OPEN status."
        )
    action_id = response.entity_id

    # 4. Wait for resolution. The checkpoint opens its own session per
    #    poll cycle from AsyncSessionLocal — not the caller's session.
    checkpoint = HiTLCheckpoint(
        action_id=action_id,
        action_timestamp=timestamp,
        session_factory=AsyncSessionLocal,
        poll_interval_seconds=poll_interval_seconds,
        timeout_seconds=timeout_seconds,
    )
    # 5. HiTLRejectedError / HiTLTimeoutError propagate out. The CLI has
    #    already written the corresponding APPROVAL_RECORD (or the
    #    checkpoint's _record_timeout did) so the audit trail is complete.
    await checkpoint.wait_for_approval(context_presented=context_presented)

    # 6. Approved — execute the tool and return the result.
    if asyncio.iscoroutinefunction(func):
        return await func(*args, **kwargs)
    maybe = func(*args, **kwargs)
    return await maybe if asyncio.iscoroutine(maybe) else maybe


# Phase 2 hook — not invoked in v0.1.0; the v0.1.0 HiTL CLI writes
# APPROVAL_RECORD rows directly via CRUDApproval.create_with_chain_link.
# See _emit_hitl_action docstring (above) for the rationale. Helper kept +
# unit-tested now so Phase 2's HttpIngestClient can wire it in trivially.
async def _emit_approval_record(
    *,
    client: IngestClient,
    ctx: SessionContext,
    action_id: UUID,
    action_timestamp: datetime,  # noqa: ARG001 — see comment below
    approver_id: str,
    approver_type: str,
    context_presented: dict,
    decision: str,
    decision_reason: str | None = None,
    parent_approval_id: UUID | None = None,
    response_latency_ms: int | None = None,
) -> None:
    """Emit an APPROVAL_RECORD via the IngestClient.

    Sibling of `_emit_action_record`. Two intentional differences:

    * **No sequence number.** APPROVAL_RECORD is not part of the hash
      chain (Sprint 4 Flag 4). The Action chain stays gap-free regardless
      of whether/how long the human took, so verify_chain still returns
      VALID on sessions that contain pending or timed-out actions.

    * **`action_timestamp` is accepted but not serialized.** Callers (the
      `rootsign approve` CLI and `HiTLCheckpoint`) carry it forward for
      hypertable-safe DB lookups via `CRUDApproval.create_with_chain_link`.
      Including it on this helper's signature keeps both code paths
      (envelope emit and direct CRUD insert) symmetrical so we can later
      add it to `ApprovalRecordPayload` without changing call sites.

    Failure-isolation rule (ADR-002): an ingest failure here is logged at
    WARNING and swallowed. The HiTL contract is enforced by the DB write
    in `CRUDApproval.create_with_chain_link`; the envelope is for the
    cloud backend's eventual-consistency view and must never block the
    decorated tool from returning. Keyword-only signature throughout
    (Sprint 4 Flag 1).
    """
    try:
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "sdk_version": SDK_VERSION,
            "event_type": EventType.APPROVAL_RECORD.value,
            "event_id": str(uuid4()),
            "emitted_at": datetime.now(timezone.utc).isoformat(),
            "agent_id": str(ctx.agent_id),
            "session_id": str(ctx.session_id),
            "payload": {
                "action_id": str(action_id),
                "approver_id": approver_id,
                "approver_type": approver_type,
                "context_presented": context_presented,
                "decision": decision,
                "decision_reason": decision_reason,
                "parent_approval_id": (str(parent_approval_id) if parent_approval_id else None),
                "response_latency_ms": response_latency_ms,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }
        await client.handle(envelope)
        logger.debug(
            "APPROVAL_RECORD emitted action_id=%s decision=%s approver_type=%s",
            action_id,
            decision,
            approver_type,
        )
    except Exception as ingest_err:  # noqa: BLE001 — see failure isolation rule
        logger.warning(
            "rootsign: _emit_approval_record failed for action_id=%s: %s",
            action_id,
            ingest_err,
        )


async def _emit_decision_record(
    *,
    client: IngestClient,
    ctx: SessionContext,
    selected_action: str,
    reasoning_summary: str | None = None,
    confidence: float | None = None,
    alternatives_considered: list[str] | None = None,
) -> UUID | None:
    """Emit a DECISION_RECORD envelope and return the handler-assigned id.

    Called by `SessionContext.record_decision` after the CAPTURE_DECISIONS
    gate. Honors `ROOTSIGN_REASONING_DEPTH`:

    * MINIMAL — `reasoning_summary` dropped; `alternatives_considered` dropped.
    * SUMMARY — `reasoning_summary` truncated to 500 chars; alternatives dropped.
    * FULL    — `reasoning_summary` truncated to 10,000 chars; alternatives kept.

    The actual depth used is recorded on the payload's `reasoning_depth`
    field so a replay consumer can tell whether `reasoning_summary` is
    `None` because the developer didn't supply one or because MINIMAL
    dropped it (ADR-008).

    Wire-format detail: the envelope does NOT include `decision_id`.
    `DecisionRecordPayload` is `extra='forbid'` and the handler assigns
    the id itself, returning it in `IngestResponse.entity_id`. This helper
    reads it back and returns it so the caller can stash it in
    `_pending_decision_id`.

    Failure isolation (ADR-002): any ingest failure is logged at WARNING
    and the helper returns `None` instead of raising. The caller treats
    `None` as "Decision was not captured" — the auto-authorized path
    continues and the next Action gets a null `decision_id`.

    Keyword-only signature (Sprint 4 Flag 1).
    """
    # Lazy import — capture is opt-in; avoid module-load cost on the
    # default no-capture path. `from ... import` re-reads on each call so
    # importlib.reload in tests is honored.
    from rootsign.sdk.config import ReasoningDepth, sdk_settings

    depth = sdk_settings.REASONING_DEPTH
    if depth == ReasoningDepth.MINIMAL:
        summary: str | None = None
        alts: list[str] = []
    else:
        max_chars = 500 if depth == ReasoningDepth.SUMMARY else 10_000
        summary = reasoning_summary[:max_chars] if reasoning_summary else None
        alts = list(alternatives_considered or []) if depth == ReasoningDepth.FULL else []

    try:
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "sdk_version": SDK_VERSION,
            "event_type": EventType.DECISION_RECORD.value,
            "event_id": str(uuid4()),
            "emitted_at": datetime.now(timezone.utc).isoformat(),
            "agent_id": str(ctx.agent_id),
            "session_id": str(ctx.session_id),
            "payload": {
                "selected_action": selected_action,
                "reasoning_summary": summary,
                "confidence": confidence,
                "alternatives_considered": alts,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "reasoning_depth": depth.value,
                "reasoning_captured": summary is not None,
            },
        }
        response = await client.handle(envelope)
        if response is None or response.entity_id is None:
            logger.warning(
                "rootsign: _emit_decision_record got no entity_id "
                "(response=%s); pending slot will not be set",
                response,
            )
            return None
        logger.debug(
            "DECISION_RECORD emitted decision_id=%s selected_action=%s depth=%s",
            response.entity_id,
            selected_action,
            depth.value,
        )
        return response.entity_id
    except Exception as ingest_err:  # noqa: BLE001 — see failure isolation rule
        logger.warning(
            "rootsign: _emit_decision_record failed for selected_action=%s: %s",
            selected_action,
            ingest_err,
        )
        return None
