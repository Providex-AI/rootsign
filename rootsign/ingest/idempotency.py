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

Two bounds keep a long-lived process from growing without limit (audit #7b):
  * Expiry is clamped to `now + TTL`. `emitted_at` is client-supplied, so a
    far-future timestamp would otherwise pin an entry forever.
  * A hard `MAX_ENTRIES` cap evicts the oldest-inserted entries (FIFO) once
    exceeded, so a flood of unique event_ids within the window can't grow
    the dict unbounded between expiries.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID

from rootsign.ingest.schemas import IngestResponse

TTL = timedelta(hours=24)

# Hard ceiling on cached entries. ~100k events × a small response is a few tens
# of MB — generous for Phase 0 single-process volumes while still bounded.
MAX_ENTRIES = 100_000


class IdempotencyStore:
    """Async-safe (single-event-loop) TTL store. NOT thread-safe."""

    def __init__(self) -> None:
        # value tuple: (response, expires_at). Insertion-ordered (dict) so the
        # FIFO eviction below drops the oldest entries first.
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
            # Clamp so a far-future client `emitted_at` can't pin an entry
            # past the TTL window (audit #7b).
            now = datetime.now(timezone.utc)
            expires_at = min(emitted_at + TTL, now + TTL)
            key = str(event_id)
            # Re-insert at the end so a refreshed key counts as most-recent
            # for FIFO eviction.
            self._entries.pop(key, None)
            self._entries[key] = (response, expires_at)
            self._enforce_size_cap_locked()

    def _enforce_size_cap_locked(self) -> None:
        """Drop oldest-inserted entries until under MAX_ENTRIES. Lock held."""
        overflow = len(self._entries) - MAX_ENTRIES
        if overflow <= 0:
            return
        for key in list(self._entries)[:overflow]:
            del self._entries[key]

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
