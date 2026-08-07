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

import asyncio
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from rootsign.ingest.idempotency import IdempotencyStore
from rootsign.ingest.schemas import IngestResponse

if TYPE_CHECKING:
    # SQLAlchemy is in the optional `postgres` extra (ADR-011). Only used as a
    # type hint here; imported for real inside LocalIngestClient.__init__.
    from sqlalchemy.ext.asyncio import AsyncSession


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
        # Lazy import — IngestHandler pulls the SQLAlchemy/crud stack (postgres
        # extra, ADR-011). LocalIngestClient is only constructed on the postgres
        # path, so the cost lands only when it is actually used.
        from rootsign.ingest.handler import IngestHandler

        self._idempotency = idempotency if idempotency is not None else IdempotencyStore()
        self._handler = IngestHandler(db=db, idempotency=self._idempotency)
        # Serializes handle() calls against the shared AsyncSession.
        #
        # Why this exists: under LangGraph's ToolNode (and create_react_agent)
        # multiple tool calls can interleave on the event loop. Each
        # `await client.handle(envelope)` walks the same AsyncSession through
        # SELECT FOR UPDATE → add → flush. If a second handle() lands during
        # a prior call's flush, SQLAlchemy fires
        # `Session is already flushing` and the SAWarning about
        # `Session.add()` during flush. Postgres row locks serialize the
        # actual writes (chain integrity holds — verify still returns VALID),
        # but the warning is real and points at a hazard that would bite
        # the moment any future code path relied on ORM identity-map
        # consistency mid-flush. See GitHub issue #2.
        #
        # The lock is per-client (not per-process) so multiple LocalIngestClient
        # instances on different AsyncSessions run independently. Within one
        # client, calls serialize — which is exactly the contract a single
        # AsyncSession needs.
        self._handle_lock = asyncio.Lock()

    @property
    def idempotency(self) -> IdempotencyStore:
        """Exposed for tests that want to inspect or reset the cache."""
        return self._idempotency

    async def handle(self, envelope: dict[str, Any]) -> IngestResponse:
        async with self._handle_lock:
            return await self._handler.handle(envelope)


class ManagedLocalIngestClient(IngestClient):
    """Postgres transport that owns its own AsyncSession per record (ADR-012).

    `LocalIngestClient` takes a caller-supplied `AsyncSession` and never
    commits — the application does (`await db.commit()` in the README example).
    The `rootsign.init()` facade has no session to plumb and no natural commit
    point, so it uses this instead: each `handle()` opens a short-lived
    `AsyncSessionLocal`, runs the envelope through a `LocalIngestClient`, and
    commits. Same per-call-session pattern as the HiTL poll loop and the audit
    MCP server.

    Consequences, both deliberate:

    * Every record is durable the moment `handle()` returns, so
      `rootsign approve` and the cross-process HiTL poll loop see pending
      actions mid-run (a single run-long transaction would hide them).
    * One short transaction per record instead of one per run. Chain-link
      reads therefore see committed rows; the lock below keeps concurrent
      tool calls from interleaving their sequence/hash reads.

    The `IdempotencyStore` is owned here (not per session) so DUPLICATE_EVENT
    detection spans the whole run.
    """

    def __init__(
        self,
        session_factory: Any | None = None,
        idempotency: IdempotencyStore | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._idempotency = idempotency if idempotency is not None else IdempotencyStore()
        # Serializes handle() calls so two concurrent tool calls can't
        # interleave their read-chain-tail → insert transactions.
        self._handle_lock = asyncio.Lock()

    @property
    def idempotency(self) -> IdempotencyStore:
        """Exposed for tests that want to inspect or reset the cache."""
        return self._idempotency

    def _factory(self) -> Any:
        if self._session_factory is not None:
            return self._session_factory
        # Lazy — the DB stack is the postgres extra (ADR-011). Translate a
        # missing extra into the actionable error, same as get_ingest_client.
        try:
            from rootsign.database import AsyncSessionLocal
        except ModuleNotFoundError as exc:
            from rootsign.errors import RootSignPostgresExtraRequired

            raise RootSignPostgresExtraRequired(f"missing module: {exc.name}") from exc
        self._session_factory = AsyncSessionLocal
        return self._session_factory

    async def handle(self, envelope: dict[str, Any]) -> IngestResponse:
        factory = self._factory()
        async with self._handle_lock:
            async with factory() as db:
                response = await LocalIngestClient(
                    db=db, idempotency=self._idempotency
                ).handle(envelope)
                await db.commit()
                return response


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
    if s.BACKEND == "jsonl":
        # ADR-011 default: zero-dependency append-only local backend. Needs no db.
        from rootsign.sdk.jsonl_client import JsonlIngestClient

        client: IngestClient = JsonlIngestClient(data_dir=s.DATA_DIR, fsync=s.JSONL_FSYNC)
    elif s.BACKEND == "cloud":
        client = HttpIngestClient(base_url=s.CLOUD_URL, api_key=s.API_KEY)
    elif db is None:
        raise ValueError(
            "The postgres backend requires a db session. Pass db=, set "
            "ROOTSIGN_BACKEND=jsonl (default, no db needed), or "
            "ROOTSIGN_BACKEND=cloud (Phase 2 only)."
        )
    else:
        # The postgres path pulls the DB stack (optional `postgres` extra,
        # ADR-011). Translate a missing-extra ModuleNotFoundError into an
        # actionable error naming the install command.
        try:
            client = LocalIngestClient(db=db)
        except ModuleNotFoundError as exc:
            from rootsign.errors import RootSignPostgresExtraRequired

            if (exc.name or "").split(".")[0] in {
                "sqlalchemy",
                "asyncpg",
                "psycopg2",
                "greenlet",
                "alembic",
            }:
                raise RootSignPostgresExtraRequired(f"missing module: {exc.name}") from exc
            raise

    # ADR-009: wrap in the micro-batching client when ROOTSIGN_BUFFERED is set.
    # Its background flush loop starts lazily on first handle() — the factory
    # is sync and can't await start(). session()'s pre-close flush still
    # guarantees a final drain regardless.
    if s.BUFFERED:
        from rootsign.sdk.buffered_client import BufferedIngestClient

        client = BufferedIngestClient(
            client,
            flush_interval_seconds=s.BUFFER_INTERVAL,
            max_buffer_size=s.BUFFER_MAX_SIZE,
        )
    return client
