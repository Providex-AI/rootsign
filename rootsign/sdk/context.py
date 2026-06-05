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
from uuid import UUID, uuid4


@dataclass
class SessionContext:
    agent_id: UUID
    session_id: UUID = field(default_factory=uuid4)
    _sequence: int = field(default=0, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def next_sequence(self) -> int:
        """Atomically increment and return the next sequence number.

        The lock guards against two concurrent tool calls (e.g. parallel
        LangGraph branches) racing on the same counter. Sprint 2 may add
        a contextvars-based session lookup; the lock stays.
        """
        async with self._lock:
            self._sequence += 1
            return self._sequence

    @property
    def current_sequence(self) -> int:
        """Last issued sequence number. 0 means no Actions emitted yet."""
        return self._sequence
