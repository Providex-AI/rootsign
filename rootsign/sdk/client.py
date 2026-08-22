"""IngestClient — transport-agnostic surface the decorator depends on.

ADR-002 makes this the single seam between the SDK and its transport. Two
implementations live here:

  - LocalIngestClient — Phase 1 default. Wraps a Phase 0 IngestHandler that
    runs in-process against the local DB. Constructs its own IdempotencyStore
    so the decorator never has to know about it.

  - HttpIngestClient — the hosted-backend transport (ADR-013). Batches
    envelopes onto `POST {CLOUD_URL}/ingest` per docs/ingest-spec-v1.md,
    owns its own retry, and keeps `httpx` behind the optional `cloud` extra
    via lazy imports so a bare install still imports this module.

A get_ingest_client() factory selects the right implementation based on
ROOTSIGN_BACKEND (read via SDKSettings).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import random
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import ValidationError

from rootsign._version import SDK_VERSION
from rootsign.chain_state import ChainRegistry, is_sealed
from rootsign.ingest.idempotency import IdempotencyStore
from rootsign.ingest.schemas import ErrorCode, EventType, IngestResponse
from rootsign.sdk.loss_ledger import LossLedger
from rootsign.sdk.spool import SYNC_BREADCRUMB as _SYNC_BREADCRUMB

logger = logging.getLogger("rootsign.sdk.client")

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
        from rootsign.errors import postgres_extra_required

        with postgres_extra_required():
            from rootsign.database import AsyncSessionLocal
        self._session_factory = AsyncSessionLocal
        return self._session_factory

    async def handle(self, envelope: dict[str, Any]) -> IngestResponse:
        factory = self._factory()
        async with self._handle_lock:
            async with factory() as db:
                response = await LocalIngestClient(db=db, idempotency=self._idempotency).handle(
                    envelope
                )
                await db.commit()
                return response


# ---------------------------------------------------------------------------
# Cloud transport (ADR-013) — wire contract in docs/ingest-spec-v1.md
# ---------------------------------------------------------------------------

#: Exponential backoff base: attempt *n* waits a jittered `0.5 * 2**n` seconds.
BACKOFF_BASE_SECONDS = 0.5
#: Ceiling on any single sleep, including a server-supplied `Retry-After`.
BACKOFF_MAX_SECONDS = 60.0
DEFAULT_TIMEOUT_SECONDS = 10.0
#: Printed with the spool-mode WARNING so the person whose laptop just went
#: offline learns the replay command at the moment it becomes relevant
#: (ADR-013 Decision 4 — `sync` lives on the operator CLI, so it needs a
#: breadcrumb from the developer-facing path). Defined in `rootsign.sdk.spool`
#: and re-exported here: the replay surface owns the command name, and the two
#: printers must never disagree about it.
SYNC_BREADCRUMB = _SYNC_BREADCRUMB
#: Total attempts per request (one send plus at most two retries), matching
#: `BufferedIngestClient.max_retries` semantics.
DEFAULT_MAX_RETRIES = 3


#: Filled into every slot of a batch result before the first attempt. The
#: first pass always overwrites every slot, so it is never returned — it
#: exists so the result list is typed and index-aligned from the start.
_PLACEHOLDER = IngestResponse.rejected(
    event_id=UUID(int=0),
    error_code=ErrorCode.INTERNAL_ERROR,
    error_message="no response recorded",
    retryable=True,
)


def _import_httpx() -> Any:
    """Import httpx behind the actionable missing-extra error (ADR-013 D2)."""
    from rootsign.errors import cloud_extra_required

    with cloud_extra_required():
        import httpx

    return httpx


def _log_safe(value: Any) -> str:
    """Defang a server-supplied string before it reaches a log record.

    Imported lazily: `rootsign.sdk.decorator` imports *this* module, so a
    module-level import here would close the cycle.
    """
    from rootsign.sdk.decorator import _log_safe as sanitize

    return sanitize(value)


def _event_id_of(envelope: dict[str, Any]) -> UUID:
    """Best-effort `event_id` for a synthesized rejection (spec §4)."""
    raw = envelope.get("event_id")
    if isinstance(raw, UUID):
        return raw
    try:
        return UUID(str(raw))
    except (ValueError, TypeError, AttributeError):
        return UUID(int=0)


def _is_control_record(envelope: dict[str, Any]) -> bool:
    """True for records that *are* the control, not observability of it.

    An APPROVAL_RECORD is the authorization decision itself, and a `pending`
    ACTION_RECORD is the submission an approval will be voted on — losing
    either means an action ran ungoverned. Everything else (auto-authorized
    actions, decisions, session lifecycle) is telemetry.
    """
    event_type = envelope.get("event_type")
    if event_type == EventType.APPROVAL_RECORD.value:
        return True
    if event_type != EventType.ACTION_RECORD.value:
        return False
    payload = envelope.get("payload") or {}
    return payload.get("authorization_status") == "pending"


def _needs_retry(response: IngestResponse) -> bool:
    """Honor the wire `retryable` flag, not our own reading of the code.

    Spec §9.3: an `error_code` this client does not recognize must still be
    classified by the flag the server sent, which is what lets the registry
    grow on a minor version bump without stranding fielded clients.
    """
    return response.status == "rejected" and bool(response.retryable)


def _backoff_delay(attempt: int, retry_after: float | None) -> float:
    """Full-jittered exponential backoff, floored by `Retry-After` (ADR-013 D3)."""
    ceiling = min(BACKOFF_BASE_SECONDS * (2**attempt), BACKOFF_MAX_SECONDS)
    delay = random.uniform(0.0, ceiling)
    if retry_after is not None:
        delay = max(delay, min(retry_after, BACKOFF_MAX_SECONDS))
    return delay


def _parse_retry_after(http_response: Any) -> float | None:
    """Seconds form of `Retry-After` only; the HTTP-date form is ignored."""
    raw = http_response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


class HttpIngestClient(IngestClient):
    """HTTP transport for the hosted backend (ADR-013).

    The wire contract is `docs/ingest-spec-v1.md`. One endpoint takes a JSON
    array of 1..N envelopes and returns an index-aligned array of responses,
    so `handle()` is a 1-element batch and `handle_batch()` is what
    `BufferedIngestClient` flushes into. Same shape either way, no special
    case for the single-event path.

    Three properties are load-bearing:

    * **The extra stays optional.** `httpx` lives in `rootsign[cloud]`. This
      class is importable without it — every real import is deferred into a
      method behind `cloud_extra_required()`, so a bare install can still
      import `rootsign.sdk.client` and get an actionable error only when it
      actually selects the cloud backend (ADR-011's packaging discipline,
      applied to the second extra).
    * **This layer owns retry** — `owns_retry = True`, read duck-typed by
      `BufferedIngestClient`. Stacking the buffer's retry loop on top of this
      one turns a 3-attempt flush into 9 requests and tens of seconds while
      the flush interval keeps firing (ADR-013 Decision 3).
    * **The API key never leaves this object.** It goes into one header, and
      is never logged nor interpolated into an error message. Strings that
      came back from the server go through `_log_safe` first — a remote
      endpoint is exactly the untrusted source that guard exists for.

    Failure semantics (ADR-002): neither method raises on a transport
    failure. Exhausted retries come back as *rejected* responses carrying
    `WRITE_TIMEOUT` / `STORE_UNAVAILABLE` with `retryable=True`, which is the
    signal T2.4's spool failover flips on. Until the spool lands, an
    exhausted send is a lost record — which is precisely why the spool
    exists.

    **Sealing (ADR-013 Decision 1).** In cloud mode the chain is computed
    here, client-side, and the server *verifies* rather than computes: every
    ACTION_RECORD envelope gets `action_id`, `sequence_number`,
    `prev_action_hash`, and `self_hash` attached before it leaves the process
    (spec §8.2). The sealer is the shared `ChainRegistry` — the same code the
    JSONL backend runs, not a second implementation — and the seal is applied
    **in place** so a re-send after a retryable rejection carries the original
    seal rather than burning a second sequence number. The registry is exposed
    so the offline spool (T2.4) can share one chain across both destinations.
    """

    #: Duck-typed capability flag — see `BufferedIngestClient._attempts`.
    owns_retry = True

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        transport: Any | None = None,
        chains: ChainRegistry | None = None,
        spool_dir: str | None = None,
        spool: Any | None = None,
        on_record_loss: str | None = None,
        enable_spool: bool = True,
    ) -> None:
        # Probe rather than import: constructing on a bare install must fail
        # with the install command, not a ModuleNotFoundError from httpx's
        # import graph — and probing keeps the import itself lazy. `find_spec`
        # itself can raise rather than return None (an import hook ahead of it
        # on `sys.meta_path` may object), so absence is caught both ways.
        try:
            installed = importlib.util.find_spec("httpx") is not None
        except (ImportError, ValueError):
            installed = False
        if not installed:
            from rootsign.errors import RootSignCloudExtraRequired

            raise RootSignCloudExtraRequired("missing module: httpx")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = float(timeout_seconds)
        self._max_retries = max(1, int(max_retries))
        # `httpx.MockTransport` in the contract suite (T2.6); None in production.
        self._transport = transport
        self._http: Any | None = None
        self._chains = chains if chains is not None else ChainRegistry()
        self._spool_dir = spool_dir
        # Built lazily on first failover — a healthy session never touches the
        # filesystem, and a bare `HttpIngestClient()` costs nothing to make.
        # Supplying `spool=` pre-seeds the writer (tests, and T2.5's replay
        # tooling) without itself declaring an outage: failover is still what
        # switches the destination.
        self._spool = spool
        # `rootsign-admin sync` sets this False (T2.5). A replay client that
        # failed over would append the records it is *reading* back into the
        # very file it is uploading — same session_id, so the writer appends to
        # the same path — duplicating sequence numbers and turning a recoverable
        # outage into a file that verifies TAMPERED. A replay has somewhere
        # durable to fall back to already: the spool file it started from.
        self._spool_enabled = bool(enable_spool)
        self._spooling = False
        self._spool_reason: ErrorCode | None = None
        self._on_record_loss = on_record_loss
        self._ledgers: dict[str, LossLedger] = {}

    @property
    def chains(self) -> ChainRegistry:
        """The chain state this client seals with.

        Exposed so the spool (T2.4) can be constructed against the *same*
        registry: a record must keep one identity whether it reaches durable
        storage over the wire or through the spool file.
        """
        return self._chains

    @property
    def is_spooling(self) -> bool:
        """True once this client has failed over to the offline spool."""
        return self._spooling

    @property
    def spool_reason(self) -> ErrorCode | None:
        """The registry code that triggered failover, or None if still online.

        Kept because the returned response no longer carries it: once a record
        is spooled the caller sees an `accepted`, which is the truth (it is
        durable) but hides *why* it went to disk.
        """
        return self._spool_reason

    @property
    def spool_dir(self) -> str:
        """Root of the spool tree. Files land under `<spool_dir>/sessions/`."""
        if self._spool_dir is None:
            from rootsign.sdk.config import SDKSettings

            self._spool_dir = SDKSettings().SPOOL_DIR
        return self._spool_dir

    @property
    def on_record_loss(self) -> str:
        """`warn` (drop telemetry with accounting) or `fail` (raise)."""
        if self._on_record_loss is None:
            from rootsign.sdk.config import SDKSettings

            self._on_record_loss = SDKSettings().ON_RECORD_LOSS
        return self._on_record_loss

    def loss_ledger(self, session_id: str) -> LossLedger | None:
        """What this client lost for a session, if anything."""
        return self._ledgers.get(str(session_id))

    @property
    def endpoint(self) -> str:
        """`{CLOUD_URL}/ingest`.

        `CLOUD_URL` already carries its `/v1` (default
        `https://ingest.getprovidex.com/v1`), so appending `/v1/ingest` here
        would silently double the prefix — ADR-013 Decision 2.
        """
        return f"{self._base_url}/ingest"

    def _ensure_client(self) -> Any:
        if self._http is None:
            httpx = _import_httpx()
            kwargs: dict[str, Any] = {
                "timeout": httpx.Timeout(self._timeout),
                "headers": {
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": f"rootsign-python/{SDK_VERSION}",
                },
            }
            if self._transport is not None:
                kwargs["transport"] = self._transport
            self._http = httpx.AsyncClient(**kwargs)
        return self._http

    async def handle(self, envelope: dict[str, Any]) -> IngestResponse:
        """Send one envelope as a 1-element batch (spec §7.4)."""
        responses = await self.handle_batch([envelope])
        return responses[0]

    async def handle_batch(self, envelopes: list[dict[str, Any]]) -> list[IngestResponse]:
        """Send 1..N envelopes as one batch; return one response per envelope.

        The returned list is index-aligned with `envelopes` (spec §7.1) even
        when the transport failed outright — every element gets a response,
        synthesized if the server never produced one.

        Retry is per element, not per batch (spec §7.7): a batch that comes
        back part accepted, part retryably-rejected resends only the
        rejected elements, under their original `event_id`s. Server-side
        idempotency makes an over-resend harmless.
        """
        if not envelopes:
            return []

        self._seal(envelopes)
        if self._spooling:
            # One-way: this client already gave up on the network (ADR-013
            # Decision 4). Sealing still happened above, so the spooled record
            # is the same record the server would have received.
            return await self._write_to_spool(envelopes)

        results: list[IngestResponse] = [_PLACEHOLDER] * len(envelopes)
        pending = list(range(len(envelopes)))
        for attempt in range(self._max_retries):
            responses, retry_after = await self._post_once([envelopes[i] for i in pending])
            for slot, response in zip(pending, responses):
                results[slot] = response
            pending = [slot for slot, response in zip(pending, responses) if _needs_retry(response)]
            if not pending or attempt == self._max_retries - 1:
                # Giving up is not logged here: the failover below is the event
                # worth one line, and logging both makes one outage look like two.
                break
            await asyncio.sleep(_backoff_delay(attempt, retry_after))

        # Anything still retryable is a record the network would not take.
        # Non-retryable rejections are NOT spooled: the server made a
        # deterministic decision about that record, and spooling it would
        # replay the same rejection later while implying the record is safe.
        unsent = [i for i, response in enumerate(results) if _needs_retry(response)]
        if unsent and self._spool_enabled:
            self._enter_spool_mode(results[unsent[0]], record_count=len(unsent))
            spooled = await self._write_to_spool([envelopes[i] for i in unsent])
            for slot, response in zip(unsent, spooled):
                results[slot] = response
        return results

    def _enter_spool_mode(self, cause: IngestResponse, *, record_count: int) -> None:
        """Flip to spool mode and say so exactly once.

        One WARNING per client, not one per record: a long offline stretch
        should leave a legible line in the log, not a wall of them. The line
        names the replay command because `sync` lives on the operator CLI and
        the person reading this log is usually on the developer one.
        """
        if self._spooling:
            return
        self._spooling = True
        self._spool_reason = cause.error_code
        logger.warning(
            "rootsign: cloud ingest unreachable after %d attempt(s) (%s) — %d record(s) and "
            "everything after them are spooling to %s instead of %s. They stay tamper-evident "
            "and verify offline (`rootsign verify --local`); upload them when connectivity "
            "returns with:  %s",
            self._max_retries,
            _log_safe(cause.error_code),
            record_count,
            self.spool_dir,
            self.endpoint,
            SYNC_BREADCRUMB,
        )

    def _ensure_spool(self) -> Any:
        if self._spool is None:
            # Lazy import: the spool is the ADR-011 writer, and importing it
            # eagerly would pull the jsonl backend into every cloud install.
            from rootsign.sdk.jsonl_client import JsonlIngestClient

            # The same ChainRegistry the wire path seals with, so a record
            # spooled after a failure links onto the ones already uploaded
            # rather than starting a second chain (T2.3 adoption).
            self._spool = JsonlIngestClient(data_dir=self.spool_dir, chains=self._chains)
        return self._spool

    async def _write_to_spool(self, envelopes: list[dict[str, Any]]) -> list[IngestResponse]:
        """Persist envelopes locally. Responses are index-aligned as always."""
        spool = self._ensure_spool()
        responses: list[IngestResponse] = []
        for envelope in envelopes:
            responses.append(await self._spool_one(spool, envelope))
            if envelope.get("event_type") == EventType.SESSION_CLOSE.value:
                self._flush_ledger(spool, str(envelope.get("session_id")))
        return responses

    async def _spool_one(self, spool: Any, envelope: dict[str, Any]) -> IngestResponse:
        try:
            return await spool.handle(envelope)
        except OSError as exc:
            # Disk full, read-only mount, permissions — the bottom rung
            # (ADR-013 Decision 4a). Note this catches only *write* failures;
            # a bug in the writer would surface as some other exception and
            # should not be quietly accounted as a record loss.
            return self._on_spool_write_failure(envelope, exc)

    def _on_spool_write_failure(self, envelope: dict[str, Any], exc: OSError) -> IngestResponse:
        session_id = str(envelope.get("session_id"))
        reason = f"{type(exc).__name__}: {_log_safe(exc)}"

        if _is_control_record(envelope):
            # Fail closed, unconditionally — no ledger entry, because nothing
            # was lost: the caller is about to learn the action did not happen.
            from rootsign.errors import HiTLPersistenceError

            payload = envelope.get("payload") or {}
            raise HiTLPersistenceError(
                f"spool write to {self.spool_dir} failed ({reason})",
                tool_name=payload.get("tool_name"),
            ) from exc

        ledger = self._ledgers.setdefault(session_id, LossLedger(session_id=session_id))
        payload = envelope.get("payload") or {}
        sequence_number = payload.get("sequence_number")
        if ledger.record(sequence_number=sequence_number, reason=reason):
            logger.critical(
                "rootsign: RECORD LOST — cloud ingest is unreachable and the spool at %s cannot "
                "be written (%s). Session %s will verify as INCOMPLETE: the hash chain advanced "
                "past the missing record, so the gap is provable rather than silent. Further "
                "losses in this session are counted, not logged individually.",
                self.spool_dir,
                reason,
                _log_safe(session_id),
            )

        if self.on_record_loss == "fail":
            from rootsign.errors import RecordPersistenceError

            raise RecordPersistenceError(
                f"record for session {session_id} could not be persisted ({reason}) and "
                "ROOTSIGN_ON_RECORD_LOSS=fail"
            ) from exc

        # Deliberately non-retryable: the wire is already given up on and the
        # local sink is dead, so re-sending only re-walks the same failure —
        # and a retryable rejection would make BufferedIngestClient re-queue
        # the record forever while the disk stays full. The wire `retryable`
        # flag is authoritative (spec §9.3), which is what lets this differ
        # from INTERNAL_ERROR's registry class.
        return IngestResponse.rejected(
            event_id=_event_id_of(envelope),
            error_code=ErrorCode.INTERNAL_ERROR,
            error_message=f"record dropped: spool write failed ({reason})",
            retryable=False,
        )

    def _flush_ledger(self, spool: Any, session_id: str) -> None:
        """Re-log the session's losses and try to leave them in the file.

        Runs at SESSION_CLOSE so the tally lands next to the run it describes.
        The append is best-effort by nature: if the disk is still full it fails
        exactly like the records did, and the chain gap remains the evidence
        that survives.
        """
        ledger = self._ledgers.get(session_id)
        if ledger is None or ledger.is_empty:
            return
        logger.critical(
            "rootsign: session %s closed with lost records — %s. The chain gap is permanent; "
            "verify will report INCOMPLETE for this session.",
            _log_safe(session_id),
            ledger.summary(),
        )
        try:
            spool.append_annotation(session_id, ledger.as_record())
        except OSError as exc:
            logger.critical(
                "rootsign: could not append the loss record for session %s either (%s). "
                "The tally survives only in this log; the chain gap survives in the data.",
                _log_safe(session_id),
                _log_safe(exc),
            )

    def _seal(self, envelopes: list[dict[str, Any]]) -> None:
        """Attach the client-computed chain fields to every unsealed action.

        Mutates the payloads in place, which is the point: the buffer and the
        retry loop both hold these dicts, so a re-sent envelope must carry the
        seal it was first given. Re-sealing would mint a second `action_id` and
        consume a second sequence number for one action — a fork the server
        would (correctly) reject as HASH_CHAIN_BROKEN, or worse, accept as two
        records for one event.

        Non-actions are untouched: only ACTION_RECORD is in the chain.
        """
        for envelope in envelopes:
            if envelope.get("event_type") != EventType.ACTION_RECORD.value:
                continue
            payload = envelope.get("payload")
            if not isinstance(payload, dict) or is_sealed(payload):
                continue
            sealed = self._chains.seal(envelope.get("session_id"), payload)
            payload.update(sealed.as_payload_fields())

    async def _post_once(
        self, batch: list[dict[str, Any]]
    ) -> tuple[list[IngestResponse], float | None]:
        """One POST. Never raises; transport failures become rejections."""
        httpx = _import_httpx()
        client = self._ensure_client()
        # Serialize ourselves with `default=str` so a caller that left a UUID
        # or datetime in the envelope gets a request rather than a TypeError
        # thrown into the instrumented tool call (ADR-002).
        body = json.dumps(batch, default=str)
        try:
            http_response = await client.post(self.endpoint, content=body)
        except httpx.TimeoutException as exc:
            return (
                self._reject_batch(
                    batch,
                    ErrorCode.WRITE_TIMEOUT,
                    f"request timed out after {self._timeout}s ({type(exc).__name__})",
                    True,
                ),
                None,
            )
        except httpx.HTTPError as exc:
            return (
                self._reject_batch(
                    batch,
                    ErrorCode.STORE_UNAVAILABLE,
                    f"transport failure: {_log_safe(exc)}",
                    True,
                ),
                None,
            )
        return self._interpret(batch, http_response)

    def _interpret(
        self, batch: list[dict[str, Any]], http_response: Any
    ) -> tuple[list[IngestResponse], float | None]:
        """Map an HTTP response onto per-element `IngestResponse`s (spec §5)."""
        retry_after = _parse_retry_after(http_response)
        # A 4xx may still carry a well-formed body — the server's own
        # error_code passes through unchanged when it does (ADR-013 D2).
        parsed = self._parse_body(batch, http_response)
        if parsed is not None:
            return parsed, retry_after

        status = http_response.status_code
        if status == 200:
            code, retryable, message = (
                ErrorCode.INTERNAL_ERROR,
                True,
                f"malformed or misaligned batch response (expected {len(batch)} elements)",
            )
        elif status in (401, 403):
            # No auth-specific code exists in the v1 registry and adding one
            # is a version event (spec §9.3), so this lands in the generic
            # non-retryable bucket. The key itself is never echoed.
            code, retryable, message = (
                ErrorCode.VALIDATION_ERROR,
                False,
                f"authentication rejected (HTTP {status}) — check ROOTSIGN_API_KEY",
            )
        elif status == 429:
            code, retryable, message = (ErrorCode.RATE_LIMITED, True, "rate limited (HTTP 429)")
        elif status == 500:
            code, retryable, message = (ErrorCode.INTERNAL_ERROR, True, "server error (HTTP 500)")
        elif status > 500:
            code, retryable, message = (
                ErrorCode.STORE_UNAVAILABLE,
                True,
                f"server unavailable (HTTP {status})",
            )
        else:
            code, retryable, message = (
                ErrorCode.VALIDATION_ERROR,
                False,
                f"request rejected (HTTP {status})",
            )
        return self._reject_batch(batch, code, message, retryable), retry_after

    @staticmethod
    def _parse_body(batch: list[dict[str, Any]], http_response: Any) -> list[IngestResponse] | None:
        """Parse an index-aligned response array, or None if it isn't one."""
        try:
            payload = http_response.json()
        except ValueError:
            return None
        if not isinstance(payload, list) or len(payload) != len(batch):
            return None
        try:
            return [IngestResponse.model_validate(item) for item in payload]
        except ValidationError:
            return None

    @staticmethod
    def _reject_batch(
        batch: list[dict[str, Any]],
        error_code: ErrorCode,
        error_message: str,
        retryable: bool,
    ) -> list[IngestResponse]:
        return [
            IngestResponse.rejected(
                event_id=_event_id_of(envelope),
                error_code=error_code,
                error_message=error_message,
                retryable=retryable,
            )
            for envelope in batch
        ]

    async def close(self) -> None:
        """Release the underlying connection pool. Duck-typed by the facade."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None


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
        # Raises RootSignCloudExtraRequired when `httpx` is absent — the
        # actionable-error twin of the postgres branch below (ADR-013 D2).
        client = HttpIngestClient(
            base_url=s.CLOUD_URL,
            api_key=s.API_KEY,
            timeout_seconds=s.HTTP_TIMEOUT_SECONDS,
            max_retries=s.HTTP_MAX_RETRIES,
            spool_dir=s.SPOOL_DIR,
            on_record_loss=s.ON_RECORD_LOSS,
        )
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
