"""Unit tests for IngestClient ABC and its two implementations.

These do NOT exercise the IngestHandler write path — that's covered by the
Phase 0 integration suite (tests/integration/test_ingest.py). Here we only
verify the SDK-facing surface: ABC contract, stub behaviour, factory wiring.
"""

from __future__ import annotations

import pytest

from rootsign.sdk.buffered_client import BufferedIngestClient
from rootsign.sdk.client import (
    HttpIngestClient,
    IngestClient,
    LocalIngestClient,
    get_ingest_client,
)


class TestIngestClientABC:
    def test_abc_cannot_be_instantiated(self):
        with pytest.raises(TypeError):
            IngestClient()  # type: ignore[abstract]


class TestHttpIngestClientStub:
    async def test_handle_raises_not_implemented(self):
        client = HttpIngestClient(base_url="https://example.com", api_key="sk-test")
        with pytest.raises(NotImplementedError, match="Phase 2"):
            await client.handle({"event_type": "SESSION_OPEN"})

    def test_constructor_accepts_phase2_args(self):
        """Locking the call-site shape now so Sprint 2 doesn't refactor it."""
        client = HttpIngestClient(base_url="https://x", api_key="k")
        assert client._base_url == "https://x"
        assert client._api_key == "k"


class TestLocalIngestClient:
    def test_none_db_rejected(self):
        with pytest.raises(ValueError, match="db session"):
            LocalIngestClient(db=None)  # type: ignore[arg-type]

    def test_owns_idempotency_store_by_default(self, tmp_path):
        """LocalIngestClient constructs its own IdempotencyStore. The
        decorator should never have to plumb one through."""

        class _StubSession:
            pass

        client = LocalIngestClient(db=_StubSession())  # type: ignore[arg-type]
        from rootsign.ingest.idempotency import IdempotencyStore

        assert isinstance(client.idempotency, IdempotencyStore)

    def test_explicit_idempotency_passed_through(self):
        from rootsign.ingest.idempotency import IdempotencyStore

        class _StubSession:
            pass

        store = IdempotencyStore()
        client = LocalIngestClient(db=_StubSession(), idempotency=store)  # type: ignore[arg-type]
        assert client.idempotency is store


class TestLocalIngestClientSerializesHandle:
    """`LocalIngestClient.handle` must serialize concurrent calls (v0.1.3).

    Under LangGraph's ToolNode / create_react_agent, multiple tool calls
    can interleave on the event loop and trigger overlapping
    `await client.handle(envelope)` invocations against the shared
    AsyncSession. Without serialization, SQLAlchemy fires
    `Session is already flushing` mid-flush. See GitHub issue #2.

    These tests use a stub handler that asserts strict non-overlap, so
    they fail loudly if the asyncio.Lock is ever removed or bypassed.
    """

    async def test_concurrent_handle_calls_do_not_overlap(self):
        import asyncio

        class _StubSession:
            pass

        in_flight = 0
        max_concurrent = 0
        completions: list[int] = []

        class _OverlapDetectingHandler:
            async def handle(self_inner, envelope):
                nonlocal in_flight, max_concurrent
                in_flight += 1
                max_concurrent = max(max_concurrent, in_flight)
                # Yield so any concurrent handle() would slip in here if
                # the lock were missing.
                await asyncio.sleep(0.01)
                completions.append(envelope["seq"])
                in_flight -= 1

                class _R:
                    status = "accepted"
                    sequence_number = envelope["seq"]
                    self_hash = "x"

                return _R()

        client = LocalIngestClient(db=_StubSession())  # type: ignore[arg-type]
        client._handler = _OverlapDetectingHandler()

        await asyncio.gather(
            *[client.handle({"seq": i}) for i in range(8)]
        )

        # The lock guarantees serialization: only one handle() in flight
        # at any moment.
        assert max_concurrent == 1, (
            f"handle() calls overlapped (max_concurrent={max_concurrent}) — "
            "asyncio.Lock missing or bypassed"
        )
        # Submission order = completion order under a fair Lock.
        assert completions == list(range(8))

    async def test_handle_lock_attribute_exists(self):
        """Catches an accidental removal of the lock in a refactor."""
        import asyncio

        class _StubSession:
            pass

        client = LocalIngestClient(db=_StubSession())  # type: ignore[arg-type]
        assert isinstance(client._handle_lock, asyncio.Lock)

    async def test_lock_is_per_client_not_global(self):
        """Two clients on independent sessions don't block each other."""
        import asyncio

        class _StubSession:
            pass

        a_in = b_in = False
        a_seen_b = b_seen_a = False

        class _ASeesB:
            async def handle(self_inner, envelope):
                nonlocal a_in, a_seen_b
                a_in = True
                await asyncio.sleep(0.02)
                a_seen_b = b_in
                a_in = False
                return type("R", (), {"status": "accepted", "sequence_number": 1, "self_hash": "x"})()

        class _BSeesA:
            async def handle(self_inner, envelope):
                nonlocal b_in, b_seen_a
                b_in = True
                await asyncio.sleep(0.02)
                b_seen_a = a_in
                b_in = False
                return type("R", (), {"status": "accepted", "sequence_number": 1, "self_hash": "x"})()

        client_a = LocalIngestClient(db=_StubSession())  # type: ignore[arg-type]
        client_b = LocalIngestClient(db=_StubSession())  # type: ignore[arg-type]
        client_a._handler = _ASeesB()
        client_b._handler = _BSeesA()

        await asyncio.gather(client_a.handle({}), client_b.handle({}))

        # If the lock were a class-level (global) Lock, exactly one of
        # these would have seen the other mid-flight. Per-client locks
        # let them overlap.
        assert a_seen_b or b_seen_a, (
            "Two LocalIngestClients on independent sessions blocked each "
            "other — lock must be per-instance, not per-class"
        )


class TestGetIngestClient:
    def test_cloud_backend_returns_http_client_without_db(self, monkeypatch):
        monkeypatch.setenv("ROOTSIGN_BACKEND", "cloud")
        c = get_ingest_client(db=None)
        assert isinstance(c, HttpIngestClient)

    def test_local_backend_requires_db(self, monkeypatch):
        monkeypatch.setenv("ROOTSIGN_BACKEND", "local")
        with pytest.raises(ValueError, match="db"):
            get_ingest_client(db=None)

    def test_local_backend_returns_local_client_with_db(self, monkeypatch):
        monkeypatch.setenv("ROOTSIGN_BACKEND", "local")

        class _StubSession:
            pass

        c = get_ingest_client(db=_StubSession())  # type: ignore[arg-type]
        assert isinstance(c, LocalIngestClient)

    def test_unbuffered_by_default(self, monkeypatch):
        monkeypatch.setenv("ROOTSIGN_BACKEND", "local")

        class _StubSession:
            pass

        c = get_ingest_client(db=_StubSession())  # type: ignore[arg-type]
        assert not isinstance(c, BufferedIngestClient)

    def test_buffered_flag_wraps_local_client(self, monkeypatch):
        # ADR-009: ROOTSIGN_BUFFERED wraps the transport in a
        # BufferedIngestClient without disturbing the inner selection.
        monkeypatch.setenv("ROOTSIGN_BACKEND", "local")
        monkeypatch.setenv("ROOTSIGN_BUFFERED", "true")
        monkeypatch.setenv("ROOTSIGN_BUFFER_INTERVAL", "1.5")
        monkeypatch.setenv("ROOTSIGN_BUFFER_MAX_SIZE", "42")

        class _StubSession:
            pass

        c = get_ingest_client(db=_StubSession())  # type: ignore[arg-type]
        assert isinstance(c, BufferedIngestClient)
        assert isinstance(c._inner, LocalIngestClient)
        # Config values propagate to the wrapper.
        assert c._flush_interval == 1.5
        assert c._max_buffer_size == 42

    def test_buffered_flag_wraps_cloud_client(self, monkeypatch):
        monkeypatch.setenv("ROOTSIGN_BACKEND", "cloud")
        monkeypatch.setenv("ROOTSIGN_BUFFERED", "true")
        c = get_ingest_client(db=None)
        assert isinstance(c, BufferedIngestClient)
        assert isinstance(c._inner, HttpIngestClient)
