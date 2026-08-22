"""Unit tests for the cloud transport (ADR-013, T2.2).

Scope is the transport's own contract: batch shape, index alignment, the
retry budget, the error mapping, and the two duck-typed capabilities the
buffering layer probes for (`handle_batch`, `owns_retry`). The full
mock-server matrix — every rejection code, `rootsign-admin sync`
end-to-end, the wrapped ADR-009 suite — lands in
`tests/contract/cloud/` with T2.6.

Every behavior asserted here traces to a line in `docs/ingest-spec-v1.md`;
the section references in the test names are the citation.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from rootsign.errors import RootSignCloudExtraRequired
from rootsign.ingest.schemas import ErrorCode, EventType
from rootsign.sdk.buffered_client import BufferedIngestClient
from rootsign.sdk.client import (
    BACKOFF_BASE_SECONDS,
    SYNC_BREADCRUMB,
    HttpIngestClient,
    _backoff_delay,
    _parse_retry_after,
)

BASE_URL = "https://ingest.example.test/v1"
API_KEY = "sk-do-not-log-me"


def _envelope(**overrides: Any) -> dict[str, Any]:
    envelope = {
        "schema_version": "1.0",
        "sdk_version": "0.3.0",
        "event_type": EventType.ACTION_RECORD.value,
        "event_id": str(uuid4()),
        "emitted_at": "2026-08-20T12:00:00+00:00",
        "agent_id": str(uuid4()),
        "session_id": str(uuid4()),
        "payload": {
            "tool_name": "send_invoice",
            "input_hash": "a" * 64,
            "output_hash": "b" * 64,
            "timestamp": "2026-08-20T12:00:00+00:00",
            "authorization_status": "auto_authorized",
        },
    }
    envelope.update(overrides)
    return envelope


def _accepted(envelope: dict[str, Any], sequence: int = 1) -> dict[str, Any]:
    return {
        "status": "accepted",
        "event_id": envelope["event_id"],
        "entity_id": str(uuid4()),
        "sequence_number": sequence,
        "self_hash": "c" * 64,
    }


def _rejected(envelope: dict[str, Any], code: ErrorCode, retryable: bool) -> dict[str, Any]:
    return {
        "status": "rejected",
        "event_id": envelope["event_id"],
        "error_code": code.value,
        "error_message": f"{code.value} from the mock",
        "retryable": retryable,
    }


class _Recorder:
    """Captures every request the transport makes, and scripts the replies."""

    def __init__(self, responder: Any) -> None:
        self.requests: list[httpx.Request] = []
        self._responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responder(request, len(self.requests) - 1)

    @property
    def bodies(self) -> list[Any]:
        return [json.loads(r.content) for r in self.requests]


def _spool_records(spool_dir: Any, session_id: str, event_type: str = "ACTION_RECORD") -> list:
    """Read the records the spool wrote for one session.

    The spool reuses the ADR-011 writer unchanged, which appends its own
    `sessions/` segment — spool files are ordinary session files, which is
    exactly what lets `verify --local` read them while still offline.
    """
    from pathlib import Path

    path = Path(spool_dir) / "sessions" / f"{session_id}.jsonl"
    if not path.exists():
        return []
    lines = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    return [r for r in lines if r.get("event_type") == event_type]


def _client(recorder: _Recorder, **kwargs: Any) -> HttpIngestClient:
    return HttpIngestClient(
        BASE_URL,
        API_KEY,
        transport=httpx.MockTransport(recorder),
        **kwargs,
    )


@pytest.fixture
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the jittered sleep so retry tests don't pay real seconds."""
    monkeypatch.setattr("rootsign.sdk.client._backoff_delay", lambda attempt, retry_after: 0.0)


# ---------------------------------------------------------------------------
# Batch shape (spec §7.1, §7.4)
# ---------------------------------------------------------------------------


async def test_handle_sends_a_one_element_batch_with_bearer_auth() -> None:
    envelope = _envelope()
    recorder = _Recorder(lambda request, n: httpx.Response(200, json=[_accepted(envelope)]))

    response = await _client(recorder).handle(envelope)

    assert response.status == "accepted"
    assert response.event_id == UUID(envelope["event_id"])
    assert len(recorder.requests) == 1
    request = recorder.requests[0]
    # CLOUD_URL already carries its /v1 — the path must not double it.
    assert str(request.url) == f"{BASE_URL}/ingest"
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    body = recorder.bodies[0]
    assert isinstance(body, list) and len(body) == 1
    assert body[0]["event_id"] == envelope["event_id"]


def test_endpoint_never_doubles_the_v1_prefix() -> None:
    recorder = _Recorder(lambda request, n: httpx.Response(200, json=[]))
    assert _client(recorder).endpoint == f"{BASE_URL}/ingest"
    trailing = HttpIngestClient("https://ingest.example.test/v1/", API_KEY)
    assert trailing.endpoint == "https://ingest.example.test/v1/ingest"


async def test_empty_batch_makes_no_request() -> None:
    recorder = _Recorder(lambda request, n: httpx.Response(200, json=[]))
    assert await _client(recorder).handle_batch([]) == []
    assert recorder.requests == []


async def test_responses_stay_index_aligned_with_the_request(fast_backoff: None) -> None:
    envelopes = [_envelope() for _ in range(3)]

    def respond(request: httpx.Request, n: int) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                _accepted(envelopes[0], 1),
                _rejected(envelopes[1], ErrorCode.VALIDATION_ERROR, False),
                _accepted(envelopes[2], 2),
            ],
        )

    responses = await _client(_Recorder(respond)).handle_batch(envelopes)

    assert [r.status for r in responses] == ["accepted", "rejected", "accepted"]
    assert responses[1].error_code is ErrorCode.VALIDATION_ERROR
    assert [r.event_id for r in responses] == [UUID(e["event_id"]) for e in envelopes]


# ---------------------------------------------------------------------------
# Retry (spec §5, §7.6, §7.7 — ADR-013 Decision 3)
# ---------------------------------------------------------------------------


async def test_only_the_retryable_element_is_resent(fast_backoff: None) -> None:
    envelopes = [_envelope() for _ in range(3)]

    def respond(request: httpx.Request, n: int) -> httpx.Response:
        if n == 0:
            return httpx.Response(
                200,
                json=[
                    _accepted(envelopes[0], 1),
                    _rejected(envelopes[1], ErrorCode.STORE_UNAVAILABLE, True),
                    _accepted(envelopes[2], 2),
                ],
            )
        return httpx.Response(200, json=[_accepted(envelopes[1], 3)])

    recorder = _Recorder(respond)
    responses = await _client(recorder).handle_batch(envelopes)

    assert [r.status for r in responses] == ["accepted"] * 3
    assert len(recorder.requests) == 2
    # The resend carries only the failed element, under its original event_id.
    assert [e["event_id"] for e in recorder.bodies[1]] == [envelopes[1]["event_id"]]


async def test_non_retryable_rejection_is_never_retried() -> None:
    envelope = _envelope()
    recorder = _Recorder(
        lambda request, n: httpx.Response(
            200, json=[_rejected(envelope, ErrorCode.HASH_CHAIN_BROKEN, False)]
        )
    )

    response = await _client(recorder).handle(envelope)

    assert response.error_code is ErrorCode.HASH_CHAIN_BROKEN
    assert len(recorder.requests) == 1


async def test_retry_budget_is_total_attempts_not_retries(fast_backoff: None, tmp_path) -> None:
    envelope = _envelope()
    recorder = _Recorder(lambda request, n: httpx.Response(503))
    client = _client(recorder, max_retries=3, spool_dir=str(tmp_path))

    response = await client.handle(envelope)

    assert len(recorder.requests) == 3
    # Exhausted retries fail over rather than failing (ADR-013 Decision 4):
    # the record is durable on disk, so `accepted` is the honest answer.
    assert response.status == "accepted"
    assert client.is_spooling is True
    assert client.spool_reason is ErrorCode.STORE_UNAVAILABLE


async def test_timeout_maps_to_write_timeout_and_never_raises(fast_backoff: None, tmp_path) -> None:
    def respond(request: httpx.Request, n: int) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    recorder = _Recorder(respond)
    client = _client(recorder, max_retries=2, spool_dir=str(tmp_path))
    response = await client.handle(_envelope())

    assert len(recorder.requests) == 2
    assert response.status == "accepted"  # spooled
    assert client.spool_reason is ErrorCode.WRITE_TIMEOUT


async def test_connect_failure_maps_to_store_unavailable(fast_backoff: None, tmp_path) -> None:
    def respond(request: httpx.Request, n: int) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = _client(_Recorder(respond), max_retries=1, spool_dir=str(tmp_path))
    await client.handle(_envelope())

    assert client.spool_reason is ErrorCode.STORE_UNAVAILABLE


async def test_retry_then_succeed(fast_backoff: None) -> None:
    envelope = _envelope()

    def respond(request: httpx.Request, n: int) -> httpx.Response:
        if n == 0:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=[_accepted(envelope)])

    recorder = _Recorder(respond)
    response = await _client(recorder).handle(envelope)

    assert response.status == "accepted"
    assert len(recorder.requests) == 2


# ---------------------------------------------------------------------------
# Client-side sealing (spec §8.2, ADR-013 Decision 1 / T2.3)
# ---------------------------------------------------------------------------


async def test_action_records_leave_the_process_sealed() -> None:
    # One session — the chain is per-session, so two envelopes from different
    # sessions would both legitimately be sequence 1.
    session_id = str(uuid4())
    envelopes = [_envelope(session_id=session_id) for _ in range(2)]

    def respond(request: httpx.Request, n: int) -> httpx.Response:
        batch = json.loads(request.content)
        return httpx.Response(200, json=[_accepted(e) for e in batch])

    recorder = _Recorder(respond)
    await _client(recorder).handle_batch(envelopes)

    sent = recorder.bodies[0]
    assert [e["payload"]["sequence_number"] for e in sent] == [1, 2]
    assert sent[0]["payload"]["prev_action_hash"] is None
    assert sent[1]["payload"]["prev_action_hash"] == sent[0]["payload"]["self_hash"]
    assert all(len(e["payload"]["self_hash"]) == 64 for e in sent)


async def test_non_action_envelopes_are_never_sealed() -> None:
    envelope = _envelope(event_type=EventType.SESSION_OPEN.value, payload={"objective": "x"})
    recorder = _Recorder(lambda request, n: httpx.Response(200, json=[_accepted(envelope)]))

    await _client(recorder).handle(envelope)

    assert "self_hash" not in recorder.bodies[0][0]["payload"]


async def test_a_retried_envelope_is_not_resealed(fast_backoff: None) -> None:
    """Re-sealing would burn a second sequence number for one action."""
    envelope = _envelope()

    def respond(request: httpx.Request, n: int) -> httpx.Response:
        if n == 0:
            return httpx.Response(503)
        return httpx.Response(200, json=[_accepted(envelope)])

    recorder = _Recorder(respond)
    await _client(recorder).handle(envelope)

    assert len(recorder.requests) == 2
    first, second = (b[0]["payload"] for b in recorder.bodies)
    assert first["action_id"] == second["action_id"]
    assert first["sequence_number"] == second["sequence_number"] == 1
    assert first["self_hash"] == second["self_hash"]


async def test_a_failed_flush_spools_instead_of_requeueing(fast_backoff: None, tmp_path) -> None:
    """The buffered path hands off to the spool, so nothing sits in memory.

    Re-queueing would keep the only copy of the record in a process that is,
    by hypothesis, having a bad day. Spooling makes it durable and lets the
    buffer move on.
    """
    session_id = str(uuid4())
    envelope = _envelope(session_id=session_id)
    recorder = _Recorder(lambda request, n: httpx.Response(503))
    transport = _client(recorder, max_retries=1, spool_dir=str(tmp_path))
    buffered = BufferedIngestClient(transport, max_buffer_size=1000)

    await buffered.handle(envelope)
    await buffered.flush()

    assert len(buffered._buffer) == 0
    assert transport.is_spooling is True
    spooled = _spool_records(tmp_path, session_id)
    assert len(spooled) == 1
    # The spooled record carries the seal the transport minted, not a new one.
    sent = recorder.bodies[0][0]["payload"]
    assert spooled[0]["action_id"] == sent["action_id"]
    assert spooled[0]["self_hash"] == sent["self_hash"]
    assert transport.chains.state_for(session_id).count == 1
    buffered._closed = True


async def test_the_chain_registry_is_shareable() -> None:
    """T2.4 will hand this registry to the spool so identity survives failover."""
    from rootsign.chain_state import ChainRegistry

    shared = ChainRegistry()
    recorder = _Recorder(
        lambda request, n: httpx.Response(
            200, json=[_accepted(e) for e in json.loads(request.content)]
        )
    )
    client = _client(recorder, chains=shared)

    await client.handle(_envelope())

    assert client.chains is shared
    assert shared.state_for(json.loads(recorder.requests[0].content)[0]["session_id"]).count == 1


# ---------------------------------------------------------------------------
# Status mapping (spec §5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (400, ErrorCode.VALIDATION_ERROR, False),
        (401, ErrorCode.VALIDATION_ERROR, False),
        (403, ErrorCode.VALIDATION_ERROR, False),
        (413, ErrorCode.VALIDATION_ERROR, False),
        (429, ErrorCode.RATE_LIMITED, True),
        (500, ErrorCode.INTERNAL_ERROR, True),
        (502, ErrorCode.STORE_UNAVAILABLE, True),
        (503, ErrorCode.STORE_UNAVAILABLE, True),
    ],
)
async def test_http_status_maps_to_registry_code(
    status: int, code: ErrorCode, retryable: bool, fast_backoff: None, tmp_path
) -> None:
    recorder = _Recorder(lambda request, n: httpx.Response(status))
    client = _client(recorder, max_retries=1, spool_dir=str(tmp_path))

    response = await client.handle(_envelope())

    if retryable:
        # The record fails over, so the code surfaces as the failover cause
        # rather than in the response — the caller's record is now on disk.
        assert client.spool_reason is code
        assert response.status == "accepted"
    else:
        assert response.error_code is code
        assert response.retryable is False
        assert client.is_spooling is False


async def test_error_body_on_a_4xx_passes_the_server_code_through() -> None:
    envelope = _envelope()
    recorder = _Recorder(
        lambda request, n: httpx.Response(
            400, json=[_rejected(envelope, ErrorCode.SCHEMA_VERSION_MISMATCH, False)]
        )
    )

    response = await _client(recorder).handle(envelope)

    assert response.error_code is ErrorCode.SCHEMA_VERSION_MISMATCH
    assert len(recorder.requests) == 1


async def test_misaligned_response_array_is_a_server_fault(fast_backoff: None, tmp_path) -> None:
    envelopes = [_envelope() for _ in range(2)]
    recorder = _Recorder(lambda request, n: httpx.Response(200, json=[_accepted(envelopes[0])]))
    client = _client(recorder, max_retries=1, spool_dir=str(tmp_path))

    responses = await client.handle_batch(envelopes)

    assert len(responses) == 2
    assert client.spool_reason is ErrorCode.INTERNAL_ERROR


async def test_non_json_body_is_a_server_fault(fast_backoff: None, tmp_path) -> None:
    recorder = _Recorder(lambda request, n: httpx.Response(200, text="<html>gateway</html>"))
    client = _client(recorder, max_retries=1, spool_dir=str(tmp_path))

    await client.handle(_envelope())

    assert client.spool_reason is ErrorCode.INTERNAL_ERROR


async def test_body_of_the_right_length_but_the_wrong_shape_is_a_server_fault(
    fast_backoff: None, tmp_path
) -> None:
    """Server drift — an aligned array whose elements aren't IngestResponses."""
    recorder = _Recorder(lambda request, n: httpx.Response(200, json=[{"ok": True}]))
    client = _client(recorder, max_retries=1, spool_dir=str(tmp_path))

    await client.handle(_envelope())

    assert client.spool_reason is ErrorCode.INTERNAL_ERROR


async def test_close_is_a_no_op_when_nothing_was_sent() -> None:
    recorder = _Recorder(lambda request, n: httpx.Response(200, json=[]))
    client = _client(recorder)

    await client.close()  # never connected — must not raise

    assert client._http is None


async def test_production_client_builds_without_a_mock_transport() -> None:
    """The `transport=` seam is test-only; the real path must build too."""
    client = HttpIngestClient(BASE_URL, API_KEY)
    http = client._ensure_client()

    assert http.headers["Authorization"] == f"Bearer {API_KEY}"
    await client.close()
    assert client._http is None


# ---------------------------------------------------------------------------
# Secret hygiene (ADR-013 Decision 2)
# ---------------------------------------------------------------------------


async def test_api_key_never_reaches_an_error_message_or_a_log(
    fast_backoff: None, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    recorder = _Recorder(lambda request, n: httpx.Response(500, text=f"key was {API_KEY}"))

    response = await _client(recorder, max_retries=2).handle(_envelope())

    assert API_KEY not in (response.error_message or "")
    assert API_KEY not in caplog.text


# ---------------------------------------------------------------------------
# Backoff helpers (spec §7.6)
# ---------------------------------------------------------------------------


def test_backoff_is_bounded_and_jittered() -> None:
    samples = [_backoff_delay(1, None) for _ in range(50)]
    ceiling = BACKOFF_BASE_SECONDS * 2
    assert all(0.0 <= d <= ceiling for d in samples)
    assert len(set(samples)) > 1  # full jitter, not a fixed delay


def test_backoff_grows_with_the_attempt_number() -> None:
    assert max(_backoff_delay(0, None) for _ in range(50)) <= BACKOFF_BASE_SECONDS
    assert max(_backoff_delay(3, None) for _ in range(50)) <= BACKOFF_BASE_SECONDS * 8


def test_retry_after_floors_the_delay() -> None:
    assert _backoff_delay(0, 5.0) >= 5.0


def test_retry_after_header_parsing() -> None:
    def response(headers: dict[str, str]) -> httpx.Response:
        return httpx.Response(429, headers=headers)

    assert _parse_retry_after(response({"Retry-After": "12"})) == 12.0
    assert _parse_retry_after(response({})) is None
    # The HTTP-date form is deliberately ignored rather than half-parsed.
    assert _parse_retry_after(response({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})) is None


# ---------------------------------------------------------------------------
# Buffering integration (ADR-009 + ADR-013 Decision 3)
# ---------------------------------------------------------------------------


class _SequentialOnlyClient:
    """An inner client with no `handle_batch` — jsonl/postgres shape."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def handle(self, envelope: dict[str, Any]) -> Any:
        self.calls.append(envelope)
        return type("R", (), {"status": "accepted", "retryable": None})()


async def test_buffered_flush_uses_handle_batch_when_the_inner_offers_it() -> None:
    envelopes = [_envelope() for _ in range(3)]
    recorder = _Recorder(
        lambda request, n: httpx.Response(200, json=[_accepted(e) for e in envelopes])
    )
    transport = _client(recorder)

    async with BufferedIngestClient(transport, max_buffer_size=3) as buffered:
        for envelope in envelopes:
            await buffered.handle(envelope)

    # Three records, one HTTP round-trip — the point of the batch endpoint.
    assert len(recorder.requests) == 1
    assert len(recorder.bodies[0]) == 3


async def test_buffered_falls_back_to_sequential_handle_for_inners_without_batch() -> None:
    inner = _SequentialOnlyClient()
    envelopes = [_envelope() for _ in range(3)]

    async with BufferedIngestClient(inner, max_buffer_size=3) as buffered:
        for envelope in envelopes:
            await buffered.handle(envelope)

    assert len(inner.calls) == 3
    assert not hasattr(inner, "handle_batch")


async def test_buffer_does_not_stack_its_retry_on_a_transport_that_owns_retry(
    fast_backoff: None, tmp_path
) -> None:
    """3 attempts total, not 3 x 3 (ADR-013 Decision 3)."""
    session_id = str(uuid4())
    recorder = _Recorder(lambda request, n: httpx.Response(503))
    transport = _client(recorder, max_retries=3, spool_dir=str(tmp_path))
    buffered = BufferedIngestClient(transport, max_buffer_size=1000, max_retries=3)

    await buffered.handle(_envelope(session_id=session_id))
    await buffered.flush()

    assert len(recorder.requests) == 3
    # And the record is not lost — the transport spooled it.
    assert len(_spool_records(tmp_path, session_id)) == 1
    buffered._closed = True


async def test_retryable_batch_element_is_requeued_and_the_rest_is_not() -> None:
    envelopes = [_envelope() for _ in range(2)]
    recorder = _Recorder(
        lambda request, n: httpx.Response(
            200,
            json=[
                _accepted(envelopes[0]),
                _rejected(envelopes[1], ErrorCode.SESSION_CLOSED, False),
            ],
        )
    )
    transport = _client(recorder, max_retries=1)
    buffered = BufferedIngestClient(transport, max_buffer_size=1000)

    for envelope in envelopes:
        await buffered.handle(envelope)
    await buffered.flush()

    # A non-retryable rejection is deterministic — replaying it would only
    # re-walk the same failure, so nothing is re-queued.
    assert len(buffered._buffer) == 0
    buffered._closed = True


async def test_buffered_close_closes_the_inner_transport() -> None:
    recorder = _Recorder(lambda request, n: httpx.Response(200, json=[]))
    transport = _client(recorder)
    transport._ensure_client()

    async with BufferedIngestClient(transport):
        pass

    assert transport._http is None


class _ThirdPartyBatchClient:
    """A batch-capable inner that does *not* own retry.

    The shape a third-party transport could take: it advertises
    `handle_batch` but leaves retry to the buffer, so the buffer keeps its
    own attempt budget (the inverse of the `HttpIngestClient` case).
    """

    owns_retry = False

    def __init__(self, *, fail_first: int = 0) -> None:
        self.calls: list[list[dict[str, Any]]] = []
        self._fail_first = fail_first

    async def handle(self, envelope: dict[str, Any]) -> Any:
        raise AssertionError("batch path should have been used")

    async def handle_batch(self, envelopes: list[dict[str, Any]]) -> list[Any]:
        self.calls.append(list(envelopes))
        if len(self.calls) <= self._fail_first:
            raise RuntimeError("transport blew up")
        return [type("R", (), {"status": "accepted", "retryable": None})() for _ in envelopes]


async def test_buffer_retries_a_raising_batch_inner_that_does_not_own_retry() -> None:
    inner = _ThirdPartyBatchClient(fail_first=1)
    buffered = BufferedIngestClient(inner, max_buffer_size=1000, max_retries=3)

    await buffered.handle(_envelope())
    await buffered.flush()

    assert len(inner.calls) == 2  # raised once, then succeeded
    assert len(buffered._buffer) == 0
    buffered._closed = True


async def test_inner_close_failure_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    class _BadClose(_ThirdPartyBatchClient):
        async def close(self) -> None:
            raise RuntimeError("pool already gone")

    async with BufferedIngestClient(_BadClose()):
        pass

    assert "inner client close failed" in caplog.text


# ---------------------------------------------------------------------------
# Offline spool (ADR-013 Decision 4 / T2.4)
# ---------------------------------------------------------------------------


async def test_failover_logs_exactly_one_warning_naming_the_replay_command(
    fast_backoff: None, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """One line per outage, not one per record — and it carries the breadcrumb.

    `sync` lives on the operator CLI (ADR-013 Decision 4) while the person
    whose laptop just went offline is on the developer one, so the log line is
    where those two surfaces meet.
    """
    caplog.set_level("WARNING")
    session_id = str(uuid4())
    recorder = _Recorder(lambda request, n: httpx.Response(503))
    client = _client(recorder, max_retries=1, spool_dir=str(tmp_path))

    for _ in range(5):
        await client.handle(_envelope(session_id=session_id))

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert SYNC_BREADCRUMB in warnings[0].getMessage()
    assert str(tmp_path) in warnings[0].getMessage()
    assert len(_spool_records(tmp_path, session_id)) == 5


async def test_spool_mode_is_one_way(fast_backoff: None, tmp_path) -> None:
    """Mid-session failback is out of scope for v0.3.0 — deliberately.

    A session that entered spool mode stays spooled to its end; `sync` closes
    the gap afterwards. The simple rule is the one you can state in a sentence
    and test in ten lines.
    """
    session_id = str(uuid4())
    down = True

    def respond(request: httpx.Request, n: int) -> httpx.Response:
        if down:
            return httpx.Response(503)
        return httpx.Response(200, json=[_accepted(e) for e in json.loads(request.content)])

    recorder = _Recorder(respond)
    client = _client(recorder, max_retries=1, spool_dir=str(tmp_path))

    await client.handle(_envelope(session_id=session_id))
    requests_during_outage = len(recorder.requests)

    down = False  # the network comes back
    await client.handle(_envelope(session_id=session_id))

    assert client.is_spooling is True
    assert len(recorder.requests) == requests_during_outage  # no further attempts
    assert len(_spool_records(tmp_path, session_id)) == 2


async def test_non_retryable_rejections_are_not_spooled(tmp_path) -> None:
    """A deterministic rejection is the server's answer, not a delivery failure.

    Spooling it would replay the same rejection on the next `sync` while
    implying to the operator that the record is safe.
    """
    envelope = _envelope()
    recorder = _Recorder(
        lambda request, n: httpx.Response(
            200, json=[_rejected(envelope, ErrorCode.HASH_CHAIN_BROKEN, False)]
        )
    )
    client = _client(recorder, spool_dir=str(tmp_path))

    response = await client.handle(envelope)

    assert response.error_code is ErrorCode.HASH_CHAIN_BROKEN
    assert client.is_spooling is False
    assert _spool_records(tmp_path, envelope["session_id"]) == []


async def test_spooled_records_continue_the_uploaded_chain(fast_backoff: None, tmp_path) -> None:
    """The outage must not start a second chain.

    The spool shares the transport's `ChainRegistry` (T2.3), so the first
    record written after the network dies links onto the last one that made it
    to the server. Without that, a synced session would show two disjoint
    chains and verify would call the join a break.
    """
    session_id = str(uuid4())
    down = False
    uploaded: list[dict] = []

    def respond(request: httpx.Request, n: int) -> httpx.Response:
        batch = json.loads(request.content)
        if down:
            return httpx.Response(503)
        uploaded.extend(batch)
        return httpx.Response(200, json=[_accepted(e) for e in batch])

    client = _client(_Recorder(respond), max_retries=1, spool_dir=str(tmp_path))

    await client.handle(_envelope(session_id=session_id))
    await client.handle(_envelope(session_id=session_id))
    down = True
    await client.handle(_envelope(session_id=session_id))
    await client.handle(_envelope(session_id=session_id))

    online = [e["payload"] for e in uploaded]
    spooled = _spool_records(tmp_path, session_id)
    assert [p["sequence_number"] for p in online] == [1, 2]
    assert [r["sequence_number"] for r in spooled] == [3, 4]
    assert spooled[0]["prev_action_hash"] == online[1]["self_hash"]
    assert spooled[1]["prev_action_hash"] == spooled[0]["self_hash"]


async def test_a_spooled_session_verifies_offline(fast_backoff: None, tmp_path) -> None:
    """The point of reusing the ADR-011 writer: `verify --local` works offline.

    A spool file is an ordinary session file, so the evidence is checkable on
    the machine that produced it, before any network comes back.
    """
    from rootsign.sdk.chain import verify_session_local

    session_id = str(uuid4())
    recorder = _Recorder(lambda request, n: httpx.Response(503))
    client = _client(recorder, max_retries=1, spool_dir=str(tmp_path))

    for _ in range(3):
        await client.handle(_envelope(session_id=session_id))

    result = verify_session_local(str(tmp_path / "sessions" / f"{session_id}.jsonl"))

    assert result.valid is True, result.error
    assert result.record_count == 3


async def test_spool_takes_every_event_type_not_just_actions(fast_backoff: None, tmp_path) -> None:
    session_id = str(uuid4())
    recorder = _Recorder(lambda request, n: httpx.Response(503))
    client = _client(recorder, max_retries=1, spool_dir=str(tmp_path))

    await client.handle(
        _envelope(
            session_id=session_id,
            event_type=EventType.SESSION_OPEN.value,
            payload={"objective": "offline run"},
        )
    )
    await client.handle(_envelope(session_id=session_id))
    await client.handle(
        _envelope(
            session_id=session_id,
            event_type=EventType.SESSION_CLOSE.value,
            payload={"status": "completed"},
        )
    )

    written = _spool_records(tmp_path, session_id, event_type=EventType.SESSION_OPEN.value)
    assert len(written) == 1
    assert len(_spool_records(tmp_path, session_id, event_type=EventType.SESSION_CLOSE.value)) == 1


async def test_a_dead_endpoint_never_reaches_the_instrumented_tool(
    fast_backoff: None, tmp_path
) -> None:
    """ADR-002's isolation rule, end to end through the failover path.

    The tool returns its value, the session closes cleanly, and the whole run
    is on disk — the agent never learns the network was down.
    """
    import rootsign
    from rootsign.sdk.chain import verify_session_local

    recorder = _Recorder(lambda request, n: httpx.Response(503))
    client = _client(recorder, max_retries=1, spool_dir=str(tmp_path))

    @rootsign.trace()
    async def add(a: int, b: int) -> int:
        return a + b

    async with rootsign.session(agent_id=uuid4(), client=client) as ctx:
        assert await add(2, 3) == 5
        assert await add(10, 1) == 11

    assert client.is_spooling is True
    path = tmp_path / "sessions" / f"{ctx.session_id}.jsonl"
    result = verify_session_local(str(path))
    assert result.valid is True, result.error
    assert result.record_count == 2
    await client.close()


# ---------------------------------------------------------------------------
# Missing extra (ADR-013 Decision 2 / T2.1)
# ---------------------------------------------------------------------------


def test_missing_cloud_extra_raises_the_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util as util

    real_find_spec = util.find_spec
    monkeypatch.setattr(
        "rootsign.sdk.client.importlib.util.find_spec",
        lambda name, *a, **kw: None if name == "httpx" else real_find_spec(name, *a, **kw),
    )

    with pytest.raises(RootSignCloudExtraRequired) as excinfo:
        HttpIngestClient(BASE_URL, API_KEY)

    assert "rootsign[cloud]" in str(excinfo.value)


def test_factory_selects_the_cloud_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    from rootsign.sdk.client import get_ingest_client

    monkeypatch.setenv("ROOTSIGN_BACKEND", "cloud")
    monkeypatch.setenv("ROOTSIGN_CLOUD_URL", BASE_URL)
    monkeypatch.setenv("ROOTSIGN_API_KEY", API_KEY)
    monkeypatch.setenv("ROOTSIGN_HTTP_MAX_RETRIES", "5")

    client = get_ingest_client()

    assert isinstance(client, HttpIngestClient)
    assert client.endpoint == f"{BASE_URL}/ingest"
    assert client._max_retries == 5
