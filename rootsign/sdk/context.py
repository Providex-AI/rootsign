"""SessionContext — per-session sequence counter for ACTION_RECORDs.

`sequence_number` is part of the canonical hash spec (ADR-001) and must be
strictly monotonic within a session. The decorator does not trust timestamps
for ordering (clock skew, simultaneous tool calls). This context owns the
counter and serialises increments through an asyncio.Lock so concurrent
@trace-decorated coroutines never collide on the next value.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from rootsign.sdk.client import IngestClient


@dataclass
class SessionContext:
    agent_id: UUID
    session_id: UUID = field(default_factory=uuid4)
    _sequence: int = field(default=0, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _session_open_emitted: bool = field(default=False, init=False, repr=False)
    # Single-slot pending Decision id (ADR-008). Set by record_decision();
    # consumed and cleared atomically by _emit_action_record /
    # _emit_hitl_action via _consume_pending_decision_id(). One Decision
    # links to one Action — second record_decision without an intervening
    # tool call overwrites the slot.
    _pending_decision_id: UUID | None = field(default=None, init=False, repr=False)

    async def next_sequence(self) -> int:
        """Atomically increment and return the next sequence number.

        The lock guards against two concurrent tool calls (e.g. parallel
        LangGraph branches) racing on the same counter. Sprint 2 may add
        a contextvars-based session lookup; the lock stays.
        """
        async with self._lock:
            self._sequence += 1
            return self._sequence

    async def mark_session_open(self) -> bool:
        """Atomically claim the right to emit SESSION_OPEN.

        Returns True the first time it's called for this context (the caller
        should emit SESSION_OPEN), False on every subsequent call. Used by
        `rootsign.session(...)` to be safely re-entrant if a caller decides
        to also open the session manually.
        """
        async with self._lock:
            if self._session_open_emitted:
                return False
            self._session_open_emitted = True
            return True

    async def record_decision(
        self,
        *,
        selected_action: str,
        reasoning_summary: str | None = None,
        confidence: float | None = None,
        alternatives_considered: list[str] | None = None,
        ingest_client: IngestClient | None = None,
    ) -> UUID | None:
        """Emit a DECISION_RECORD and stash decision_id as the pending slot.

        The next ACTION_RECORD emitted from this context — via either the
        auto-authorized `_emit_action_record` path or the HiTL
        `_emit_hitl_action` path — consumes the slot and includes the
        decision_id in its payload. Silent no-op when CAPTURE_DECISIONS is
        False so the same codebase can ship to capture-on and capture-off
        environments without call-site conditionals. See ADR-008.

        `ingest_client` may be omitted inside an `async with
        rootsign.session(...)` — it then resolves from the ambient session
        (ADR-012). Explicit wins. Resolution happens only on the capture-on
        path, so the no-op path never raises.

        Keyword-only signature (Sprint 4 Flag 1).
        """
        # Lazy import — Decision capture is opt-in and pulling settings at
        # module load would pessimize cold start. `from ... import` re-reads
        # module attributes each call, so importlib.reload of the config
        # module in tests is honored.
        from rootsign.sdk.config import sdk_settings

        if not sdk_settings.CAPTURE_DECISIONS:
            return None

        from rootsign.sdk.facade import _resolve_ctx_client

        # `self` is already the context; only the client needs resolving.
        _, ingest_client = _resolve_ctx_client(
            self, ingest_client, surface="record_decision"
        )

        # _emit_decision_record is only imported on the capture-on path so
        # the no-op path stays free of dependency on the helper.
        from rootsign.sdk.decorator import _emit_decision_record

        decision_id = await _emit_decision_record(
            client=ingest_client,
            ctx=self,
            selected_action=selected_action,
            reasoning_summary=reasoning_summary,
            confidence=confidence,
            alternatives_considered=alternatives_considered or [],
        )
        async with self._lock:
            self._pending_decision_id = decision_id
        return decision_id

    async def _consume_pending_decision_id(self) -> UUID | None:
        """Read and atomically clear the pending decision_id.

        Returns the pending value on first call after `record_decision()`,
        then `None` until the next `record_decision()` call. The clear is
        unconditional — partial consumes (e.g. error path mid-action) still
        clear the slot, matching the one-Decision-one-Action contract from
        ADR-008.
        """
        async with self._lock:
            decision_id = self._pending_decision_id
            self._pending_decision_id = None
            return decision_id

    @property
    def current_sequence(self) -> int:
        """Last issued sequence number. 0 means no Actions emitted yet."""
        return self._sequence

    @property
    def session_open_emitted(self) -> bool:
        """True once SESSION_OPEN has been claimed via `mark_session_open()`."""
        return self._session_open_emitted
