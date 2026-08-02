"""Unit tests for BufferedIngestClient (ADR-009) — selective micro-batching.

These exercise the wrapper against a mock inner `IngestClient`, so no DB is
required. Coverage splits into two halves:

  * **Buffering mechanics** — flush triggers (max size, explicit, close),
    retry/backoff, context-manager lifecycle. (The doc's original six tests,
    corrected so buffered envelopes carry `authorization_status`.)
  * **Selective routing (ADR-009 Decision 2)** — only auto-authorized
    ACTION_RECORDs buffer; pending (HiTL) actions, SESSION_*, DECISION and
    APPROVAL envelopes passthrough synchronously, and a passthrough drains
    the buffer first so the hash chain stays ordered.

`asyncio_mode = "auto"` (pyproject) means plain `async def` tests run without
a marker.
"""

from __future__ import annotations

import asyncio

from unittest.mock import AsyncMock, MagicMock

from rootsign.sdk.buffered_client import BufferedIngestClient, _BufferedResponse, _should_buffer


def make_mock_inner() -> AsyncMock:
    inner = AsyncMock()
    inner.handle.return_value = MagicMock(status="accepted")
    return inner


def auto_action(seq: int | None = None) -> dict:
    """An auto-authorized ACTION_RECORD envelope — the only buffered shape."""
    payload: dict = {"authorization_status": "auto_authorized"}
    if seq is not None:
        payload["seq"] = seq
    return {"event_type": "ACTION_RECORD", "payload": payload}


# --------------------------------------------------------------------------
# Selective-routing predicate (ADR-009 Decision 2)
# --------------------------------------------------------------------------


class TestShouldBuffer:
    def test_auto_authorized_action_buffers(self):
        assert _should_buffer(auto_action()) is True

    def test_pending_action_passes_through(self):
        assert (
            _should_buffer(
                {"event_type": "ACTION_RECORD", "payload": {"authorization_status": "pending"}}
            )
            is False
        )

    def test_action_without_status_passes_through(self):
        # Defensive: the decorator always sets authorization_status, but a
        # missing key must never be treated as bufferable.
        assert _should_buffer({"event_type": "ACTION_RECORD"}) is False
        assert _should_buffer({"event_type": "ACTION_RECORD", "payload": {}}) is False

    def test_non_action_events_pass_through(self):
        for event in ("SESSION_OPEN", "SESSION_CLOSE", "DECISION_RECORD", "APPROVAL_RECORD"):
            assert _should_buffer({"event_type": event, "payload": {}}) is False


# --------------------------------------------------------------------------
# Buffering mechanics
# --------------------------------------------------------------------------


class TestBufferedIngestClientUnit:
    async def test_buffered_response_shape(self):
        resp = _BufferedResponse()
        assert resp.status == "buffered"
        assert resp.entity_id is None
        assert resp.sequence_number is None

    async def test_handle_returns_immediately(self):
        inner = make_mock_inner()
        client = BufferedIngestClient(inner, flush_interval_seconds=999)
        await client.start()
        response = await client.handle(auto_action())
        assert response.status == "buffered"
        # Inner must NOT have been called yet.
        assert inner.handle.call_count == 0
        await client.close()

    async def test_max_buffer_size_triggers_flush(self):
        inner = make_mock_inner()
        client = BufferedIngestClient(inner, max_buffer_size=3, flush_interval_seconds=999)
        await client.start()
        for i in range(3):
            await client.handle(auto_action(i))
        # Third record trips the size cap → inline flush.
        await asyncio.sleep(0.05)
        assert inner.handle.call_count == 3
        await client.close()

    async def test_explicit_flush_drains_buffer(self):
        inner = make_mock_inner()
        client = BufferedIngestClient(inner, flush_interval_seconds=999)
        await client.start()
        await client.handle(auto_action())
        await client.handle(auto_action())
        assert inner.handle.call_count == 0  # not flushed yet
        await client.flush()
        assert inner.handle.call_count == 2
        await client.close()

    async def test_close_performs_final_flush(self):
        inner = make_mock_inner()
        client = BufferedIngestClient(inner, flush_interval_seconds=999)
        await client.start()
        await client.handle(auto_action())
        await client.close()
        assert inner.handle.call_count == 1

    async def test_retry_on_inner_failure(self):
        inner = AsyncMock()
        # Fail twice, succeed on the third attempt.
        inner.handle.side_effect = [
            Exception("fail"),
            Exception("fail"),
            MagicMock(status="accepted"),
        ]
        client = BufferedIngestClient(inner, max_retries=3, flush_interval_seconds=999)
        await client.start()
        await client.handle(auto_action())
        await client.flush()
        assert inner.handle.call_count == 3
        await client.close()

    async def test_failed_send_requeued_to_front(self):
        # After max_retries are exhausted the record is re-queued (never
        # silently lost mid-session) — it rides the next flush.
        inner = AsyncMock()
        inner.handle.side_effect = RuntimeError("boom")  # always fails
        client = BufferedIngestClient(inner, max_retries=1, flush_interval_seconds=999)
        await client.handle(auto_action())  # lazily starts the background loop
        await client.flush()
        assert inner.handle.call_count == 1  # one attempt (max_retries=1)
        assert len(client._buffer) == 1  # re-queued, not dropped
        await client.close()  # cancel the lazily-started loop; buffer re-drains

    async def test_context_manager_lifecycle(self):
        inner = make_mock_inner()
        async with BufferedIngestClient(inner, flush_interval_seconds=999) as client:
            await client.handle(auto_action())
        # __aexit__ → close() → final flush.
        assert inner.handle.call_count == 1

    async def test_periodic_background_flush(self):
        inner = make_mock_inner()
        async with BufferedIngestClient(inner, flush_interval_seconds=0.05) as client:
            await client.handle(auto_action())
            await asyncio.sleep(0.15)  # let the background loop tick
            assert inner.handle.call_count == 1

    async def test_start_is_idempotent(self):
        inner = make_mock_inner()
        client = BufferedIngestClient(inner, flush_interval_seconds=999)
        await client.start()
        task = client._flush_task
        await client.start()
        assert client._flush_task is task  # no second loop spawned
        await client.close()

    async def test_handle_lazily_starts_background_loop(self):
        # Factory-created clients never enter `async with`; the loop must
        # start on first handle() (ADR-009 Decision 4).
        inner = make_mock_inner()
        client = BufferedIngestClient(inner, flush_interval_seconds=999)
        assert client._flush_task is None  # not started yet
        await client.handle(auto_action())
        assert client._flush_task is not None  # lazily launched
        await client.close()

    async def test_no_resurrection_after_close(self):
        # A handle() after close() must not restart the loop.
        inner = make_mock_inner()
        client = BufferedIngestClient(inner, flush_interval_seconds=999)
        await client.start()
        await client.close()
        assert client._flush_task is None
        await client.handle(auto_action())
        assert client._flush_task is None  # stayed closed


# --------------------------------------------------------------------------
# Selective routing — passthrough behavior (ADR-009 Decision 2)
# --------------------------------------------------------------------------


class TestSelectivePassthrough:
    async def test_pending_action_passes_through_synchronously(self):
        inner = make_mock_inner()
        real = MagicMock(status="accepted", entity_id="action-123")
        inner.handle.return_value = real
        client = BufferedIngestClient(inner, flush_interval_seconds=999)
        await client.start()
        pending = {"event_type": "ACTION_RECORD", "payload": {"authorization_status": "pending"}}
        response = await client.handle(pending)
        # HiTL needs the real response (entity_id), not a buffered stub.
        assert response is real
        assert inner.handle.call_count == 1
        await client.close()

    async def test_session_and_decision_events_pass_through(self):
        inner = make_mock_inner()
        client = BufferedIngestClient(inner, flush_interval_seconds=999)
        await client.start()
        for event in ("SESSION_OPEN", "DECISION_RECORD", "APPROVAL_RECORD", "SESSION_CLOSE"):
            await client.handle({"event_type": event, "payload": {}})
        assert inner.handle.call_count == 4
        await client.close()

    async def test_passthrough_flushes_buffer_first(self):
        # Ordering guarantee: a passthrough envelope must never overtake
        # buffered auto-authorized actions in the hash chain. The buffer is
        # drained (FIFO) ahead of the passthrough call.
        inner = make_mock_inner()
        client = BufferedIngestClient(inner, flush_interval_seconds=999)
        await client.start()
        await client.handle(auto_action(1))
        await client.handle(auto_action(2))
        assert inner.handle.call_count == 0  # both buffered

        session_close = {"event_type": "SESSION_CLOSE", "payload": {}}
        await client.handle(session_close)

        # Three inner calls, in order: auto(1), auto(2), then SESSION_CLOSE.
        assert inner.handle.call_count == 3
        sent = [call.args[0] for call in inner.handle.call_args_list]
        assert sent[0]["payload"]["seq"] == 1
        assert sent[1]["payload"]["seq"] == 2
        assert sent[2]["event_type"] == "SESSION_CLOSE"
        await client.close()
