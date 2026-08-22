"""A mock ingest backend that implements `docs/ingest-spec-v1.md`.

Shared by the cloud contract suite (T2.6) and the kill-network integration
test (T2.7), because two mock stores would drift and the whole value of this
one is that it is *not* a stub that agrees with whatever it is sent: it
validates with the real schemas, dedupes by `event_id`, and verifies the
client's seal the way ADR-013 Decision 1 says a real backend must.

The severable transport is the point of reuse — an outage is the same event
whether a contract test triggers it in one request or an integration test
triggers it halfway through a live session.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from rootsign.hashing import compute_action_self_hash
from rootsign.ingest.schemas import (
    PAYLOAD_SCHEMAS,
    ErrorCode,
    EventType,
    IngestEnvelope,
)

#: Rejections the store decides deterministically — resending changes nothing,
#: so the client must not retry them and must not spool them.
NON_RETRYABLE = [
    ErrorCode.SCHEMA_VERSION_MISMATCH,
    ErrorCode.UNKNOWN_AGENT,
    ErrorCode.SESSION_NOT_FOUND,
    ErrorCode.SESSION_CLOSED,
    ErrorCode.SESSION_ALREADY_EXISTS,
    ErrorCode.DUPLICATE_EVENT,
    ErrorCode.ACTION_NOT_FOUND,
    ErrorCode.ACTION_ALREADY_RESOLVED,
    ErrorCode.APPROVAL_PARENT_NOT_FOUND,
    ErrorCode.VALIDATION_ERROR,
    ErrorCode.HASH_CHAIN_BROKEN,
]
#: Transient conditions — the same request may well succeed later.
RETRYABLE = [
    ErrorCode.STORE_UNAVAILABLE,
    ErrorCode.WRITE_TIMEOUT,
    ErrorCode.RATE_LIMITED,
    ErrorCode.INTERNAL_ERROR,
]


class MockIngestStore:
    """Minimal ingest backend: validates, dedupes, and *verifies* the seal.

    Deliberately not a fixture-shaped stub. The one behavior that cannot be
    faked usefully is chain verification — if this accepted any `self_hash`,
    the suite would prove only that requests were made.
    """

    def __init__(self) -> None:
        self.requests = 0
        self.batch_sizes: list[int] = []
        self.accepted: list[dict[str, Any]] = []
        self.seen_event_ids: set[str] = set()
        #: Set to an (ErrorCode, retryable) pair to reject everything, or to a
        #: status code to answer at the HTTP level. Cleared to resume service.
        self.reject_all: tuple[ErrorCode, bool] | None = None
        self.http_status: int | None = None
        self.http_headers: dict[str, str] = {}
        #: HTTP status for the *first* request only — the transient-then-fine
        #: shape, without scripting every reply.
        self.first_status: int | None = None
        self.raise_timeout: bool = False

    # -- transport --------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if self.raise_timeout:
            raise httpx.ReadTimeout("too slow", request=request)
        if self.first_status is not None and self.requests == 1:
            return httpx.Response(self.first_status, headers=self.http_headers)
        if self.http_status is not None:
            return httpx.Response(self.http_status, headers=self.http_headers)

        batch = json.loads(request.content)
        self.batch_sizes.append(len(batch))
        return httpx.Response(200, json=[self._ingest(envelope) for envelope in batch])

    # -- store ------------------------------------------------------------

    def _ingest(self, envelope: dict[str, Any]) -> dict[str, Any]:
        event_id = envelope.get("event_id")
        if self.reject_all is not None:
            code, retryable = self.reject_all
            return self._reject(event_id, code, retryable)

        if event_id in self.seen_event_ids:
            # Idempotency by event_id (spec §7.5) — what makes a re-run of
            # `rootsign-admin sync` safe by construction.
            return self._reject(event_id, ErrorCode.DUPLICATE_EVENT, False)

        try:
            validated = IngestEnvelope.model_validate(envelope)
            PAYLOAD_SCHEMAS[validated.event_type].model_validate(envelope["payload"])
        except Exception as exc:  # noqa: BLE001 - any validation failure is one code
            return self._reject(event_id, ErrorCode.VALIDATION_ERROR, False, str(exc))

        if validated.event_type is EventType.ACTION_RECORD:
            broken = self._verify_seal(envelope)
            if broken is not None:
                return self._reject(event_id, ErrorCode.HASH_CHAIN_BROKEN, False, broken)

        self.seen_event_ids.add(event_id)
        self.accepted.append(envelope)
        payload = envelope["payload"]
        return {
            "status": "accepted",
            "event_id": event_id,
            "entity_id": payload.get("action_id") or str(uuid4()),
            "sequence_number": payload.get("sequence_number"),
            "self_hash": payload.get("self_hash"),
        }

    def _verify_seal(self, envelope: dict[str, Any]) -> str | None:
        """The server verifies; it does not compute (ADR-013 Decision 1).

        Recomputing would fork the chain: the client already holds a
        `self_hash` for this action, and a store that assigned its own would
        leave two different records for one event.

        **Scope matters as much as the check.** Verification is this record
        against itself — nothing else. A dangling `prev_action_hash` is NOT a
        rejection (spec §8.3): the records after a failed spool write
        legitimately name a predecessor that was never written, and refusing
        them would erase the only evidence anything was lost. It also makes
        batches fragile — element k+1 names k's hash, so rejecting on linkage
        would turn one transient failure into a permanent refusal of
        everything behind it, since `HASH_CHAIN_BROKEN` is not retryable.
        Gaps surface as INCOMPLETE at verify time, which is where they belong.
        """
        payload = envelope["payload"]
        for field in ("action_id", "sequence_number", "prev_action_hash", "self_hash"):
            if field not in payload:
                return f"unsealed ACTION_RECORD: missing {field}"

        recomputed = compute_action_self_hash({**payload, "session_id": envelope["session_id"]})
        if recomputed != payload["self_hash"]:
            return "self_hash does not match the canonical fields"

        return None

    @staticmethod
    def _reject(
        event_id: Any, code: ErrorCode, retryable: bool, detail: str | None = None
    ) -> dict[str, Any]:
        return {
            "status": "rejected",
            "event_id": event_id,
            "error_code": code.value,
            "error_message": detail or f"{code.value} from the mock store",
            "retryable": retryable,
        }

    # -- assertions helper -------------------------------------------------

    def actions_for(self, session_id: str) -> list[dict[str, Any]]:
        return [
            e["payload"]
            for e in self.accepted
            if e["event_type"] == EventType.ACTION_RECORD.value
            and str(e["session_id"]) == str(session_id)
        ]

    def dump_chain(self, session_id: str, path: Path) -> Path:
        """Write what the store holds as a session file, for `verify_session_local`.

        The verifier is the auditor's tool, not a test helper — running the
        store's own copy through it is the end-to-end claim this suite exists
        to make.
        """
        lines = [
            json.dumps(
                {**payload, "session_id": str(session_id), "event_type": "ACTION_RECORD"},
                default=str,
            )
            for payload in self.actions_for(session_id)
        ]
        path.write_text("\n".join(lines) + "\n")
        return path

    # -- the network switch (T2.7) ----------------------------------------

    def sever(self) -> None:
        """Cut the wire. Every subsequent request times out."""
        self.raise_timeout = True

    def restore(self) -> None:
        """Bring it back. Requests are served normally again."""
        self.raise_timeout = False
