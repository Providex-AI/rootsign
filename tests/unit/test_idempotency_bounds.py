"""audit #7b: the IdempotencyStore must stay bounded — expiry clamped to the
TTL window (a far-future client emitted_at can't pin an entry forever) and a
hard size cap that FIFO-evicts the oldest entries.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from rootsign.ingest.idempotency import TTL, IdempotencyStore
from rootsign.ingest.schemas import IngestResponse


def _resp() -> IngestResponse:
    return IngestResponse.accepted(event_id=uuid4())


class TestExpiryClamp:
    async def test_far_future_emitted_at_clamped_to_ttl_window(self):
        store = IdempotencyStore()
        event_id = uuid4()
        far_future = datetime.now(timezone.utc) + timedelta(days=3650)
        await store.set(event_id, _resp(), far_future)

        # Retrievable now (not treated as already expired)...
        assert await store.get(event_id) is not None
        # ...but its expiry is clamped to ~now + TTL, not now + 3650 days.
        _, expires_at = store._entries[str(event_id)]
        assert expires_at <= datetime.now(timezone.utc) + TTL + timedelta(seconds=5)


class TestSizeCap:
    async def test_evicts_oldest_when_over_cap(self, monkeypatch):
        monkeypatch.setattr("rootsign.ingest.idempotency.MAX_ENTRIES", 3)
        store = IdempotencyStore()
        now = datetime.now(timezone.utc)
        ids = [uuid4() for _ in range(5)]
        for event_id in ids:
            await store.set(event_id, _resp(), now)

        assert await store.size() == 3
        # The two oldest-inserted were evicted; the three newest survive.
        assert await store.get(ids[0]) is None
        assert await store.get(ids[1]) is None
        assert await store.get(ids[2]) is not None
        assert await store.get(ids[4]) is not None

    async def test_refresh_moves_key_to_newest(self, monkeypatch):
        monkeypatch.setattr("rootsign.ingest.idempotency.MAX_ENTRIES", 3)
        store = IdempotencyStore()
        now = datetime.now(timezone.utc)
        ids = [uuid4() for _ in range(3)]
        for event_id in ids:
            await store.set(event_id, _resp(), now)
        # Re-set the oldest → it becomes newest, so inserting one more evicts
        # ids[1] (now the oldest), not ids[0].
        await store.set(ids[0], _resp(), now)
        await store.set(uuid4(), _resp(), now)

        assert await store.get(ids[0]) is not None
        assert await store.get(ids[1]) is None
