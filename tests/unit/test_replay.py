"""The shared batch-replay core (`rootsign.replay`, Sprint B T2.5).

`rootsign-admin sync` uses this to upload a spooled session; `replay-pending`
will use it to re-drive records the store never resolved. What both need from
it is narrow and unforgiving:

* order is preserved, because these envelopes are a hash chain;
* it **stops** at the first hard failure, so the far side of a rejection is
  never uploaded referencing a parent the store refused;
* `DUPLICATE_EVENT` is success, or the second run of a resumed sync could
  never finish.

The client here is a stub rather than `HttpIngestClient`: this file is about
the walk, not the transport. The transport's own contract lives in
`test_http_ingest_client.py`, and the two meet in `test_spool_sync.py`.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from rootsign.ingest.schemas import ErrorCode, IngestResponse
from rootsign.replay import DEFAULT_BATCH_SIZE, replay_envelopes


def _envelope() -> dict[str, Any]:
    return {"event_id": str(uuid4()), "event_type": "ACTION_RECORD", "payload": {}}


def _accepted(envelope: dict[str, Any]) -> IngestResponse:
    return IngestResponse.accepted(event_id=envelope["event_id"], entity_id=uuid4())


def _rejected(envelope: dict[str, Any], code: ErrorCode, retryable: bool = False) -> IngestResponse:
    return IngestResponse.rejected(
        event_id=envelope["event_id"],
        error_code=code,
        error_message=f"{code.value} from the stub",
        retryable=retryable,
    )


class _Store:
    """Records what it was asked to take, and answers by a scripted rule."""

    def __init__(self, rule=None, *, batches: bool = True) -> None:
        self.seen: list[dict[str, Any]] = []
        self.requests = 0
        self._rule = rule or (lambda index, envelope: _accepted(envelope))
        self._batches = batches

    async def handle_batch(self, envelopes: list[dict[str, Any]]) -> list[IngestResponse]:
        if not self._batches:
            raise AssertionError("handle_batch called on a store that does not batch")
        self.requests += 1
        responses = []
        for envelope in envelopes:
            responses.append(self._rule(len(self.seen), envelope))
            self.seen.append(envelope)
        return responses

    async def handle(self, envelope: dict[str, Any]) -> IngestResponse:
        self.requests += 1
        response = self._rule(len(self.seen), envelope)
        self.seen.append(envelope)
        return response


class _SingleShotStore(_Store):
    """A client with no batch endpoint — the shape `handle_batch` must not assume."""

    def __init__(self, rule=None) -> None:
        super().__init__(rule, batches=False)

    handle_batch = None  # type: ignore[assignment]


class TestReplayHappyPath:
    async def test_nothing_to_replay_is_complete(self):
        report = await replay_envelopes(_Store(), [])
        assert report.complete
        assert report.total == report.delivered == 0

    async def test_every_envelope_delivered_in_order(self):
        store = _Store()
        envelopes = [_envelope() for _ in range(5)]

        report = await replay_envelopes(store, envelopes)

        assert report.complete
        assert report.accepted == 5
        assert report.duplicates == 0
        assert [e["event_id"] for e in store.seen] == [e["event_id"] for e in envelopes]

    async def test_duplicates_count_as_delivered(self):
        """Re-running a partially-uploaded sync is the normal case, not an error.

        Idempotency is server-side by `event_id` (spec §7.5), so the records
        the first run landed come back DUPLICATE_EVENT on the second. Counting
        that as failure would make a resumed sync permanently unfinishable.
        """
        envelopes = [_envelope() for _ in range(4)]
        already_there = {0, 1}
        store = _Store(
            lambda index, envelope: (
                _rejected(envelope, ErrorCode.DUPLICATE_EVENT)
                if index in already_there
                else _accepted(envelope)
            )
        )

        report = await replay_envelopes(store, envelopes)

        assert report.complete
        assert report.duplicates == 2
        assert report.accepted == 2
        assert report.delivered == 4

    async def test_a_client_without_handle_batch_still_replays(self):
        """`handle_batch` is duck-typed, never on the `IngestClient` ABC (ADR-013).

        A client that only implements `handle` must still be replayable —
        otherwise the core is a cloud-only helper wearing a general name, and
        `replay-pending` (which drives the local store) could not share it.
        """
        store = _SingleShotStore()
        envelopes = [_envelope() for _ in range(3)]

        report = await replay_envelopes(store, envelopes)

        assert report.complete
        assert store.requests == 3
        assert [e["event_id"] for e in store.seen] == [e["event_id"] for e in envelopes]

    @pytest.mark.parametrize("batch_size", [1, 2, 3, 10])
    async def test_batching_changes_request_count_not_outcome(self, batch_size: int):
        store = _Store()
        envelopes = [_envelope() for _ in range(6)]

        report = await replay_envelopes(store, envelopes, batch_size=batch_size)

        assert report.complete
        assert report.accepted == 6
        assert store.requests == -(-6 // batch_size)  # ceil
        assert [e["event_id"] for e in store.seen] == [e["event_id"] for e in envelopes]


class TestReplayStopsAtTheFirstFailure:
    async def test_later_envelopes_are_never_sent(self):
        """The chain is why. Record N+1 names record N's `self_hash` as parent;
        uploading it after the store refused N would leave the server holding a
        chain that references a record it does not have."""
        envelopes = [_envelope() for _ in range(6)]
        store = _Store(
            lambda index, envelope: (
                _rejected(envelope, ErrorCode.HASH_CHAIN_BROKEN)
                if index == 2
                else _accepted(envelope)
            )
        )

        report = await replay_envelopes(store, envelopes, batch_size=1)

        assert not report.complete
        assert report.failed_index == 2
        assert report.error_code is ErrorCode.HASH_CHAIN_BROKEN
        assert report.accepted == 2
        assert len(store.seen) == 3, "replay kept going past a rejection"

    async def test_a_failure_mid_batch_reports_the_absolute_index(self):
        """The caller thinks in records, not in transport chunking."""
        envelopes = [_envelope() for _ in range(10)]
        store = _Store(
            lambda index, envelope: (
                _rejected(envelope, ErrorCode.VALIDATION_ERROR)
                if index == 7
                else _accepted(envelope)
            )
        )

        report = await replay_envelopes(store, envelopes, batch_size=4)

        assert report.failed_index == 7
        assert report.error_message is not None
        assert "VALIDATION_ERROR" in report.error_message

    async def test_a_retryable_rejection_also_stops_the_walk(self):
        """The transport already exhausted its own retries by the time a
        response reaches here (`owns_retry`), so a still-retryable rejection
        means the endpoint is down. Continuing would just fail 500 more times
        and leave a half-uploaded chain."""
        envelopes = [_envelope() for _ in range(3)]
        store = _Store(
            lambda index, envelope: _rejected(envelope, ErrorCode.STORE_UNAVAILABLE, retryable=True)
        )

        report = await replay_envelopes(store, envelopes)

        assert not report.complete
        assert report.failed_index == 0
        assert report.delivered == 0

    async def test_a_short_response_array_is_a_failure_not_silent_success(self):
        """Index alignment is the store's obligation (spec §7.1). An answer for
        3 of 5 envelopes says nothing about the other 2 — treating silence as
        acceptance would retire a spool file holding records nobody took."""

        class _ShortStore(_Store):
            async def handle_batch(self, envelopes):
                self.requests += 1
                self.seen.extend(envelopes)
                return [_accepted(e) for e in envelopes[:3]]

        store = _ShortStore()
        report = await replay_envelopes(store, [_envelope() for _ in range(5)])

        assert not report.complete
        assert report.failed_index == 3
        assert report.error_code is ErrorCode.INTERNAL_ERROR


class TestReplayReporting:
    async def test_progress_callback_sees_every_response_in_order(self):
        seen: list[int] = []
        envelopes = [_envelope() for _ in range(4)]

        await replay_envelopes(
            _Store(), envelopes, batch_size=2, on_progress=lambda i, r: seen.append(i)
        )

        assert seen == [0, 1, 2, 3]

    async def test_default_batch_size_is_a_single_request_for_a_small_session(self):
        store = _Store()
        await replay_envelopes(store, [_envelope() for _ in range(DEFAULT_BATCH_SIZE)])
        assert store.requests == 1
