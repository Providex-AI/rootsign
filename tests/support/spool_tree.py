"""A real spool tree, plus the mock endpoint that replays it.

Shared by `test_spool_sync.py` (ADR-013 D4) and `test_sync_receipts.py`
(ADR-015). Both need a spool written by the code that writes spools in
production rather than by a fixture that agrees with the parser by
construction, so the builder lives here instead of being copied.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from rootsign.ingest.schemas import ErrorCode
from rootsign.sdk.client import HttpIngestClient
from tests.conftest import make_envelope

BASE_URL = "https://ingest.example.test/v1"
API_KEY = "sk-sync-test"


def dead_endpoint(request: httpx.Request) -> httpx.Response:
    return httpx.Response(503, text="down")


def spool_a_session(spool_dir: Path, *, actions: int = 3) -> str:
    """Record a session while the endpoint is down; return its session_id.

    `max_retries=1` keeps the backoff sleeps out of the test — the failover
    path is what is being exercised, not the retry budget (that is
    `test_http_ingest_client.py`).
    """

    async def _run() -> str:
        client = HttpIngestClient(
            BASE_URL,
            API_KEY,
            max_retries=1,
            transport=httpx.MockTransport(dead_endpoint),
            spool_dir=str(spool_dir),
        )
        agent_id, session_id = uuid4(), uuid4()
        await client.handle(make_envelope("SESSION_OPEN", agent_id, session_id, {"objective": "s"}))
        for i in range(actions):
            await client.handle(
                make_envelope(
                    "ACTION_RECORD",
                    agent_id,
                    session_id,
                    {
                        "tool_name": f"tool_{i}",
                        "input_hash": f"{i:064d}",
                        "output_hash": "b" * 64,
                        "timestamp": "2026-08-21T09:00:00+00:00",
                        "authorization_status": "auto_authorized",
                    },
                )
            )
        await client.handle(
            make_envelope("SESSION_CLOSE", agent_id, session_id, {"status": "completed"})
        )
        await client.close()
        return str(session_id)

    return asyncio.run(_run())


class Endpoint:
    """A mock ingest endpoint that records what it was given.

    Answers index-aligned per spec §7.1, and remembers `event_id`s so a second
    run can answer DUPLICATE_EVENT — which is how server-side idempotency shows
    up to a resumed sync.

    Rejection does NOT cascade, because the spec forbids it: §7 rule 3 says a
    rejection of element *k* must not abort *k+1..N*, and §8.3 says a server
    MUST NOT reject a record for a `prev_action_hash` pointing at something it
    has never seen ("discontinuity is data"). So the elements after a refusal
    are answered normally — and normally means accepted. That is precisely why
    a receipt has to record the whole batch's answers rather than stopping at
    the first refusal.
    """

    def __init__(
        self,
        *,
        reject_at_sequence: int | None = None,
    ) -> None:
        self.batches: list[list[dict[str, Any]]] = []
        self.seen: set[str] = set()
        self._reject_at = reject_at_sequence

    @property
    def envelopes(self) -> list[dict[str, Any]]:
        return [envelope for batch in self.batches for envelope in batch]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        batch = json.loads(request.content)
        self.batches.append(batch)
        # Every element answered, index-aligned, regardless of an earlier
        # refusal — spec §7 rule 3, no cross-element atomicity.
        return httpx.Response(200, json=[self._answer(envelope) for envelope in batch])

    def _answer(self, envelope: dict[str, Any]) -> dict[str, Any]:
        event_id = envelope["event_id"]
        if event_id in self.seen:
            return {
                "status": "rejected",
                "event_id": event_id,
                "error_code": ErrorCode.DUPLICATE_EVENT.value,
                "error_message": "already ingested",
                "retryable": False,
            }
        sequence = (envelope.get("payload") or {}).get("sequence_number")
        if self._reject_at is not None and sequence == self._reject_at:
            return {
                "status": "rejected",
                "event_id": event_id,
                "error_code": ErrorCode.VALIDATION_ERROR.value,
                "error_message": f"sequence {sequence} refused by the mock",
                "retryable": False,
            }
        self.seen.add(event_id)
        return {
            "status": "accepted",
            "event_id": event_id,
            "entity_id": str(uuid4()),
            "sequence_number": sequence,
            "self_hash": (envelope.get("payload") or {}).get("self_hash"),
        }


def bind_transport(monkeypatch: Any, transport: Any) -> None:
    """Make the transport the CLI's own `HttpIngestClient` will use.

    The command imports the class inside the function, so patching the module
    attribute is enough — and it keeps the CLI's own constructor arguments
    (including `enable_spool=False`) under test instead of bypassed.
    """
    real = HttpIngestClient

    def factory(**kwargs: Any) -> HttpIngestClient:
        return real(transport=transport, **kwargs)

    monkeypatch.setattr("rootsign.sdk.client.HttpIngestClient", factory)
