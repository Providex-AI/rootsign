"""IngestClient — transport-agnostic surface the decorator depends on.

ADR-002 makes this the single seam between the SDK and its transport. Two
implementations live here:

  - LocalIngestClient — Phase 1 default. Wraps a Phase 0 IngestHandler that
    runs in-process against the local DB. Constructs its own IdempotencyStore
    so the decorator never has to know about it.

  - HttpIngestClient — Phase 2 transport. Stubbed in Phase 1: .handle()
    raises NotImplementedError. The class exists so the factory + ABC are
    fully wired now and Sprint 2 doesn't have to refactor callers.

A get_ingest_client() factory selects the right implementation based on
ROOTSIGN_BACKEND (read via SDKSettings).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from rootsign.ingest.handler import IngestHandler
from rootsign.ingest.idempotency import IdempotencyStore
from rootsign.ingest.schemas import IngestResponse


class IngestClient(ABC):
    """Abstract transport for ingest envelopes. See ADR-002."""

    @abstractmethod
    async def handle(self, envelope: dict[str, Any]) -> IngestResponse:
        """Send an envelope and return the store's response."""
        raise NotImplementedError


class LocalIngestClient(IngestClient):
    """In-process transport — calls IngestHandler directly. Phase 1 default.

    Phase 0's IngestHandler takes a (db, idempotency) pair. We own an
    IdempotencyStore here so callers (the decorator, tests) don't have to
    plumb one through. Pass `idempotency=` explicitly when a test needs to
    seed or inspect the cache directly.
    """

    def __init__(
        self,
        db: AsyncSession,
        idempotency: IdempotencyStore | None = None,
    ) -> None:
        if db is None:
            raise ValueError(
                "LocalIngestClient requires a db session. Pass an AsyncSession "
                "or switch to HttpIngestClient via ROOTSIGN_BACKEND=cloud "
                "(Phase 2 only)."
            )
        self._idempotency = idempotency if idempotency is not None else IdempotencyStore()
        self._handler = IngestHandler(db=db, idempotency=self._idempotency)

    @property
    def idempotency(self) -> IdempotencyStore:
        """Exposed for tests that want to inspect or reset the cache."""
        return self._idempotency

    async def handle(self, envelope: dict[str, Any]) -> IngestResponse:
        return await self._handler.handle(envelope)


class HttpIngestClient(IngestClient):
    """HTTP transport — Phase 2 stub. See ADR-002.

    The constructor accepts what Phase 2 will need (base_url + api_key) so
    we lock the call site shape today, but .handle() refuses to operate.
    """

    def __init__(self, base_url: str, api_key: str) -> None:
        self._base_url = base_url
        self._api_key = api_key

    async def handle(self, envelope: dict[str, Any]) -> IngestResponse:
        raise NotImplementedError(
            "HttpIngestClient is not available until Phase 2. "
            "Set ROOTSIGN_BACKEND=local (default) to use LocalIngestClient."
        )


def get_ingest_client(db: AsyncSession | None = None) -> IngestClient:
    """Factory — picks an implementation based on ROOTSIGN_BACKEND.

    `db` is required for the local backend (it's how IngestHandler reaches
    the storage layer). It is ignored for the cloud backend.
    """
    # Read settings fresh so monkeypatched env vars in tests take effect
    # without needing to reload the module.
    from rootsign.sdk.config import SDKSettings

    s = SDKSettings()
    if s.BACKEND == "cloud":
        return HttpIngestClient(base_url=s.CLOUD_URL, api_key=s.API_KEY)
    if db is None:
        raise ValueError(
            "LocalIngestClient requires a db session. Pass db= or set "
            "ROOTSIGN_BACKEND=cloud (Phase 2 only)."
        )
    return LocalIngestClient(db=db)
