"""Unit tests for IngestClient ABC and its two implementations.

These do NOT exercise the IngestHandler write path — that's covered by the
Phase 0 integration suite (tests/integration/test_ingest.py). Here we only
verify the SDK-facing surface: ABC contract, stub behaviour, factory wiring.
"""

from __future__ import annotations

import pytest

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
