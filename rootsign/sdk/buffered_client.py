"""BufferedIngestClient — SDK micro-batching wrapper (ADR-009).

Wraps any `IngestClient` via composition and buffers records so `handle()`
returns without waiting on the transport. This matters for Phase 2's
`HttpIngestClient`, where a 200-call pipeline would otherwise pay 200
sequential round-trips of agent-visible latency.

**Selective buffering (ADR-009 Decision 2).** Buffering is NOT applied to
every envelope — three SDK paths depend on a synchronous, real
`IngestResponse`:

  * HiTL (`_emit_hitl_action`) needs the pending `action_id` (entity_id) to
    bind its poll loop.
  * Decision capture (`_emit_decision_record`) needs entity_id to set the
    pending-decision slot.
  * The Action hash chain links rows in insertion order, so a passthrough
    envelope must never overtake a buffered Action.

So only **auto-authorized `ACTION_RECORD`s** are buffered. Every other
envelope — `SESSION_OPEN`/`SESSION_CLOSE`, `DECISION_RECORD`,
`APPROVAL_RECORD`, and pending (HiTL) `ACTION_RECORD`s — first flushes the
buffer (preserving chain order) then passes straight through to the inner
client, returning the store's real response.

**Batch flush (ADR-013).** A flush forwards the whole batch in one call when
the inner client exposes `handle_batch(list) -> list` — which the cloud
transport does, turning an N-record flush into one HTTP round-trip. The probe
is duck-typed on purpose: `handle_batch` is deliberately NOT on the
`IngestClient` ABC, so third-party transports (and the jsonl / postgres inners,
which have no batch API) keep routing through sequential `handle()` untouched.

**Exactly one layer retries.** A transport that owns its own retry loop sets
`owns_retry = True`; this client then makes a single attempt per flush instead
of stacking `max_retries` on top of the transport's, which would multiply into
3 x 3 requests per flush (ADR-013 Decision 3).

Use as an async context manager to guarantee a final flush on exit.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any

from rootsign.ingest.schemas import EventType
from rootsign.sdk.client import IngestClient, _log_safe

logger = logging.getLogger("rootsign.sdk.buffered_client")


class _BufferedResponse:
    """Synthetic response returned immediately for a buffered envelope.

    Attribute-compatible with the subset of `IngestResponse` that
    `_try_ingest` reads (`status`, `sequence_number`), plus `entity_id` /
    `self_hash` for defensive parity. `status == "buffered"` signals the
    decorator to advance the sequence counter optimistically (ADR-009
    Decision 3) without treating the record as store-confirmed. entity_id
    is deliberately None — callers that need a real action_id (HiTL,
    Decision capture) never reach this path; their envelopes passthrough.
    """

    status: str = "buffered"
    entity_id = None
    sequence_number = None
    self_hash = None


def _should_buffer(envelope: dict[str, Any]) -> bool:
    """True only for auto-authorized ACTION_RECORDs (ADR-009 Decision 2).

    Everything else must passthrough synchronously so HiTL/Decision get a
    real entity_id and the hash chain stays ordered. A missing
    `authorization_status` is treated as non-bufferable — the decorator's
    auto path sets it explicitly to `"auto_authorized"`.
    """
    if envelope.get("event_type") != EventType.ACTION_RECORD.value:
        return False
    payload = envelope.get("payload") or {}
    return payload.get("authorization_status") == "auto_authorized"


def _needs_retry(response: Any) -> bool:
    """True for a rejection the server marked retryable.

    Honors the wire `retryable` flag rather than the code (ingest-spec-v1
    §9.3), so an `error_code` this SDK predates is still classified the way
    the server intended. Anything that isn't a rejection — including the
    synthetic `_BufferedResponse` — is left alone.
    """
    return getattr(response, "status", None) == "rejected" and bool(
        getattr(response, "retryable", False)
    )


def _log_refusal(envelope: dict[str, Any], response: Any) -> None:
    """WARN for a record the store refused for good.

    It is not re-sent (the refusal is deterministic), so this line is the only
    trace it leaves: an ingest failure is never swallowed silently.
    """
    if getattr(response, "status", None) != "rejected":
        return
    code = getattr(response, "error_code", None)
    logger.warning(
        "rootsign: the store refused a buffered action record for session %s "
        "(%s: %s); it was not recorded",
        _log_safe(envelope.get("session_id")),
        _log_safe(getattr(code, "value", code)),
        _log_safe(getattr(response, "error_message", "")),
    )


class BufferedIngestClient(IngestClient):
    """Micro-batching wrapper around any `IngestClient`. See ADR-009."""

    def __init__(
        self,
        inner: IngestClient,
        *,
        flush_interval_seconds: float = 0.5,
        max_buffer_size: int = 100,
        max_retries: int = 3,
    ) -> None:
        self._inner = inner
        self._flush_interval = flush_interval_seconds
        self._max_buffer_size = max_buffer_size
        self._max_retries = max_retries
        self._buffer: deque[dict[str, Any]] = deque()
        self._lock = asyncio.Lock()
        # Held for a whole drain-and-send, not just the drain. Without it a
        # flush that found the buffer empty returned while another flush was
        # still sending the batch it had taken, so SESSION_CLOSE overtook that
        # batch and the store refused the rest of it.
        self._flush_lock = asyncio.Lock()
        self._flush_task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        """Launch the background flush loop. Idempotent; call before use.

        Callers using `async with` get this automatically. Factory-created
        clients (`get_ingest_client()` under ROOTSIGN_BUFFERED) never enter a
        context manager, so `handle()` also starts the loop lazily on first
        use — see there.
        """
        if self._flush_task is None:
            self._flush_task = asyncio.create_task(self._flush_loop())

    async def handle(self, envelope: dict[str, Any]) -> Any:
        """Buffer auto-authorized ACTION_RECORDs; passthrough everything else.

        Buffered path returns immediately with a `_BufferedResponse`. The
        passthrough path flushes the buffer first (chain order) then awaits
        the inner client, returning its real `IngestResponse`.
        """
        # Lazy start (ADR-009 Decision 4): the factory can't `await start()`
        # from sync context, so the periodic flush loop is launched here on
        # first handle() — we're inside a running loop, so create_task is
        # safe. Idempotent, and never resurrected after close().
        if self._flush_task is None and not self._closed:
            await self.start()

        if _should_buffer(envelope):
            async with self._lock:
                self._buffer.append(envelope)
                should_flush = len(self._buffer) >= self._max_buffer_size
            if should_flush:
                await self.flush()
            return _BufferedResponse()

        # Passthrough: drain buffered actions ahead of this envelope so the
        # store inserts them in order, then forward synchronously. The flush
        # lock stays held for the forward too, so no background flush can
        # slip a later batch in between.
        async with self._flush_lock:
            await self._drain_and_send()
            return await self._inner.handle(envelope)

    async def flush(self) -> None:
        """Forward every buffered record to the inner client.

        Returns only once every record buffered before the call has been
        sent, including a batch another flush (the background loop) was
        already sending. `session()` relies on this to store every action
        before SESSION_CLOSE.
        """
        async with self._flush_lock:
            await self._drain_and_send()

    async def _drain_and_send(self) -> None:
        """Take the buffer and send it. The caller holds `_flush_lock`."""
        async with self._lock:
            if not self._buffer:
                return
            batch = list(self._buffer)
            self._buffer.clear()
        failed = await self._send(batch)
        if failed:
            # Re-queue at the FRONT so FIFO order is preserved on next flush.
            async with self._lock:
                self._buffer.extendleft(reversed(failed))

    def _attempts(self) -> int:
        """How many times this client may send before giving up.

        One, when the inner transport runs its own retry loop — stacking two
        bounded retries multiplies them (ADR-013 Decision 3). The probe is
        duck-typed: an inner without the flag keeps the historical behavior.
        """
        return 1 if getattr(self._inner, "owns_retry", False) else self._max_retries

    async def _send(self, batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Forward a drained batch; return the envelopes that must be re-queued."""
        handle_batch = getattr(self._inner, "handle_batch", None)
        if callable(handle_batch):
            return await self._send_batch_with_retry(handle_batch, batch)
        failed: list[dict[str, Any]] = []
        for envelope in batch:
            if not await self._send_with_retry(envelope):
                failed.append(envelope)
        return failed

    async def _send_batch_with_retry(
        self, handle_batch: Any, batch: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Forward the whole batch in one call; retry only the failed elements.

        Returns the envelopes still unsent after the last attempt. Never
        raises (ADR-002). A non-retryable rejection is NOT re-queued — it is
        deterministic, so replaying it only re-walks the same failure.
        """
        pending = list(batch)
        attempts = self._attempts()
        delay = 0.1
        for attempt in range(attempts):
            try:
                responses = list(await handle_batch(pending))
            except Exception as e:  # noqa: BLE001 — see failure-isolation rule
                logger.warning("rootsign: buffered batch flush raised: %s", e)
                responses = []
            failed = []
            for env, resp in zip(pending, responses):
                if _needs_retry(resp):
                    failed.append(env)
                else:
                    _log_refusal(env, resp)
            # A short response array is a protocol violation (spec §7.1); treat
            # the unanswered tail as unsent rather than silently dropping it.
            failed.extend(pending[len(responses) :])
            if not failed:
                return []
            pending = failed
            if attempt < attempts - 1:
                await asyncio.sleep(delay)
                delay *= 2
        logger.error(
            "rootsign: buffered batch flush left %d record(s) unsent after %d attempt(s)",
            len(pending),
            attempts,
        )
        return pending

    async def _send_with_retry(self, envelope: dict[str, Any]) -> bool:
        """Forward one envelope with bounded exponential backoff.

        Returns True when the record needs no resend: it was stored, or the
        store refused it for good (logged). False after the attempt budget is
        spent on exceptions or retryable refusals. Never raises — the
        never-crash-the-agent contract (ADR-002).
        """
        attempts = self._attempts()
        delay = 0.1
        for attempt in range(attempts):
            try:
                response = await self._inner.handle(envelope)
            except Exception as e:  # noqa: BLE001 — see failure-isolation rule
                failure: Any = e
            else:
                if not _needs_retry(response):
                    _log_refusal(envelope, response)
                    return True
                # The store did not take it and said to try again: a retryable
                # refusal counted as delivered was a lost record.
                failure = getattr(response, "error_code", "retryable rejection")
            if attempt < attempts - 1:
                await asyncio.sleep(delay)
                delay *= 2
            else:
                logger.error(
                    "rootsign: buffered flush failed after %d attempt(s): %s",
                    attempts,
                    _log_safe(getattr(failure, "value", failure)),
                )
        return False

    async def _flush_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(self._flush_interval)
            try:
                await self.flush()
            except Exception as e:  # noqa: BLE001 — never let the loop die
                logger.warning("rootsign: background flush error: %s", e)

    async def close(self) -> None:
        """Stop the background task, flush, then close the inner client.

        The inner close is duck-typed and best-effort. Only the cloud
        transport currently owns closable resources (an `httpx.AsyncClient`
        connection pool) — the jsonl and postgres inners have no `close`, so
        this is a no-op for them.
        """
        self._closed = True
        if self._flush_task is not None:
            # Cancel only between sends. Cancelling mid-send dropped the
            # unsent rest of that batch, which lived only inside the flush.
            async with self._flush_lock:
                self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
        await self.flush()
        inner_close = getattr(self._inner, "close", None)
        if callable(inner_close):
            try:
                result = inner_close()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:  # noqa: BLE001 — close must never raise
                logger.warning("rootsign: inner client close failed: %s", e)

    async def __aenter__(self) -> BufferedIngestClient:
        await self.start()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()
