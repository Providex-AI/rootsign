"""In-memory 24-hour TTL idempotency store.

Used by IngestHandler to dedupe events within a 24h window keyed by event_id.
Phase 0 only — Phase 2 upgrades to Redis/PostgreSQL-backed for multi-process
safety (see MEMORY.md decision 2026-05-01).

Caching policy (founder decision, see feedback_req03_decisions):
  * Cache `accepted` responses → idempotent successful writes
  * Cache non-retryable `rejected` responses → deterministic failures dedup too
  * Do NOT cache retryable failures (STORE_UNAVAILABLE, WRITE_TIMEOUT, etc.) —
    the SDK retry must actually re-hit the store

`_evict_expired` runs O(n) on every get/set, which is fine for Phase 0
volumes. Phase 2 will use a backing store with native expiry.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID

from rootsign.ingest.schemas import IngestResponse

TTL = timedelta(hours=24)


class IdempotencyStore:
    """Async-safe (single-event-loop) TTL store. NOT thread-safe."""

    def __init__(self) -> None:
        # value tuple: (response, expires_at)
        self._entries: dict[str, tuple[IngestResponse, datetime]] = {}
        self._lock = asyncio.Lock()

    async def get(self, event_id: UUID) -> IngestResponse | None:
        async with self._lock:
            self._evict_expired_locked()
            entry = self._entries.get(str(event_id))
            return entry[0] if entry is not None else None

    async def set(
        self,
        event_id: UUID,
        response: IngestResponse,
        emitted_at: datetime,
    ) -> None:
        """Store a response. Caller is responsible for deciding whether to
        store (see policy in module docstring) — this method does not filter."""
        async with self._lock:
            self._evict_expired_locked()
            expires_at = emitted_at + TTL
            self._entries[str(event_id)] = (response, expires_at)

    def _evict_expired_locked(self) -> None:
        """Eviction must run with the lock already held."""
        now = datetime.now(timezone.utc)
        expired = [k for k, (_, exp) in self._entries.items() if exp <= now]
        for k in expired:
            del self._entries[k]

    async def size(self) -> int:
        """For testing/observability — not part of the public protocol."""
        async with self._lock:
            self._evict_expired_locked()
            return len(self._entries)
