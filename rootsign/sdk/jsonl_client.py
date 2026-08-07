"""JsonlIngestClient — append-only JSONL local backend (ADR-011).

The zero-dependency default: `pip install rootsign` writes a tamper-evident
hash chain to `$ROOTSIGN_DATA_DIR/sessions/<session_id>.jsonl` with no Docker,
no Postgres, no migrations. Implements the `IngestClient` ABC (ADR-002), so it
is a drop-in at every call site — including as the inner of
`BufferedIngestClient` (ADR-009).

**Client-side chain compute (ADR-011 Decision 3).** In Postgres mode the store
assigns `sequence_number` / `prev_action_hash` and computes `self_hash` under a
row lock. There is no store here, so this client replicates that logic in
memory per session — `prev = chain tail`, `seq = count + 1`, `action_id =
uuid4()` — and computes `self_hash` with the **same frozen canonical formula**
(`rootsign.hashing.compute_action_self_hash`, ADR-001). No second hash formula
is written; the cross-backend contract test pins byte-identical results.

**Record shape.** ACTION_RECORD lines are written *flat* — the eight canonical
fields at the top level — so `verify_session_local` can recompute the hash
directly (that is also the legacy store-export shape). All five event types are
appended, so a session file is a complete replay artifact; `verify` filters to
ACTION_RECORD lines when rebuilding the chain.

**Durability (Decision 4).** `O_APPEND`, one JSON object per line, `fsync` per
`ROOTSIGN_JSONL_FSYNC` (`chain` = after ACTION/APPROVAL records, the default;
`always`; `never`). Single-writer-per-session-file is the documented contract —
an in-process `asyncio.Lock` serializes sequence assignment and file writes;
there is no cross-process file locking (that's the graduation to `postgres`).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import json

from rootsign.hashing import compute_action_self_hash
from rootsign.ingest.idempotency import IdempotencyStore
from rootsign.ingest.schemas import ErrorCode, EventType, IngestResponse
from rootsign.sdk.client import IngestClient

logger = logging.getLogger("rootsign.sdk.jsonl_client")

DEFAULT_DATA_DIR = "~/.rootsign"
_FSYNC_MODES = ("chain", "always", "never")
# Event types whose durability we guarantee under the default `chain` policy —
# they carry the tamper-evidence (the chain and its approvals).
_CHAIN_CRITICAL = {EventType.ACTION_RECORD.value, EventType.APPROVAL_RECORD.value}


class _ChainState:
    """Per-session in-memory tail of the hash chain."""

    __slots__ = ("tail", "count")

    def __init__(self) -> None:
        self.tail: str | None = None
        self.count: int = 0


class JsonlIngestClient(IngestClient):
    """Append-only JSONL local backend. See ADR-011."""

    def __init__(
        self,
        *,
        data_dir: str | os.PathLike[str] | None = None,
        fsync: str = "chain",
        idempotency: IdempotencyStore | None = None,
    ) -> None:
        if fsync not in _FSYNC_MODES:
            raise ValueError(f"ROOTSIGN_JSONL_FSYNC must be one of {_FSYNC_MODES}, got {fsync!r}")
        self._data_dir = Path(data_dir if data_dir is not None else DEFAULT_DATA_DIR).expanduser()
        self._fsync = fsync
        self._idempotency = idempotency if idempotency is not None else IdempotencyStore()
        self._chains: dict[str, _ChainState] = {}
        # Serializes sequence assignment + file writes (mirrors
        # LocalIngestClient._handle_lock). Single writer per session file.
        self._lock = asyncio.Lock()

    @property
    def idempotency(self) -> IdempotencyStore:
        """Exposed for tests that want to inspect or seed the cache."""
        return self._idempotency

    def _session_path(self, session_id: str) -> Path:
        return self._data_dir / "sessions" / f"{session_id}.jsonl"

    async def handle(self, envelope: dict[str, Any]) -> IngestResponse:
        event_id = UUID(str(envelope["event_id"]))
        event_type = envelope["event_type"]
        session_id = str(envelope["session_id"])
        emitted_at = _parse_dt(envelope.get("emitted_at"))

        # Idempotency (Decision 5 / T2.3): a duplicate event_id within the
        # process is dropped — no line written — with a DUPLICATE_EVENT reject.
        if await self._idempotency.get(event_id) is not None:
            return IngestResponse.rejected(
                event_id=event_id,
                error_code=ErrorCode.DUPLICATE_EVENT,
                error_message=f"event_id {event_id} already handled (jsonl backend, in-process)",
                retryable=False,
            )

        async with self._lock:
            if event_type == EventType.ACTION_RECORD.value:
                response, record = self._build_action(envelope, event_id, session_id)
            else:
                response, record = self._build_non_action(envelope, event_id, event_type, session_id)
            self._append_line(session_id, record, do_fsync=self._should_fsync(event_type))

        await self._idempotency.set(event_id, response, emitted_at)
        return response

    def _build_action(
        self, envelope: dict[str, Any], event_id: UUID, session_id: str
    ) -> tuple[IngestResponse, dict[str, Any]]:
        payload = envelope.get("payload") or {}
        state = self._chains.setdefault(session_id, _ChainState())
        action_id = uuid4()
        seq = state.count + 1
        prev = state.tail
        # Same 8 canonical fields the store feeds compute_action_self_hash.
        self_hash = compute_action_self_hash(
            {
                "action_id": action_id,
                "session_id": session_id,
                "tool_name": payload["tool_name"],
                "input_hash": payload["input_hash"],
                "output_hash": payload.get("output_hash"),
                "prev_action_hash": prev,
                "timestamp": payload["timestamp"],
                "sequence_number": seq,
            }
        )
        state.tail = self_hash
        state.count = seq

        # Flat record — verify_session_local reads the canonical fields at the
        # top level. Envelope metadata + redacted payloads ride along so the
        # line is a complete, bindable replay artifact (ADR-006 payload↔hash).
        record = {
            "schema_version": envelope.get("schema_version"),
            "sdk_version": envelope.get("sdk_version"),
            "event_type": EventType.ACTION_RECORD.value,
            "event_id": str(event_id),
            "emitted_at": envelope.get("emitted_at"),
            "agent_id": envelope.get("agent_id"),
            "session_id": session_id,
            "action_id": str(action_id),
            "tool_name": payload["tool_name"],
            "input_hash": payload["input_hash"],
            "output_hash": payload.get("output_hash"),
            "prev_action_hash": prev,
            "timestamp": payload["timestamp"],
            "sequence_number": seq,
            "self_hash": self_hash,
            "input_redacted": payload.get("input_redacted"),
            "output_redacted": payload.get("output_redacted"),
            "authorization_status": payload.get("authorization_status", "auto_authorized"),
            "decision_id": payload.get("decision_id"),
        }
        response = IngestResponse.accepted(
            event_id=event_id,
            entity_id=action_id,
            sequence_number=seq,
            self_hash=self_hash,
        )
        return response, record

    def _build_non_action(
        self, envelope: dict[str, Any], event_id: UUID, event_type: str, session_id: str
    ) -> tuple[IngestResponse, dict[str, Any]]:
        # SESSION_OPEN/CLOSE, DECISION_RECORD, APPROVAL_RECORD: append the
        # envelope as-is (nested payload). verify filters these out. DECISION
        # needs a real entity_id back (the decorator stashes it as the pending
        # decision_id), so we mint one and persist it on the written line.
        record = dict(envelope)
        entity_id: UUID | None = None
        if event_type == EventType.DECISION_RECORD.value:
            entity_id = uuid4()
            record = {**record, "decision_id": str(entity_id)}
        elif event_type == EventType.APPROVAL_RECORD.value:
            entity_id = uuid4()
            record = {**record, "approval_id": str(entity_id)}
        response = IngestResponse.accepted(event_id=event_id, entity_id=entity_id)
        return response, record

    def _should_fsync(self, event_type: str) -> bool:
        if self._fsync == "always":
            return True
        if self._fsync == "never":
            return False
        return event_type in _CHAIN_CRITICAL  # "chain"

    def _append_line(self, session_id: str, record: dict[str, Any], *, do_fsync: bool) -> None:
        path = self._session_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
        # O_APPEND makes each write atomic w.r.t. other appends; fsync per policy
        # so a crash never loses a chain-critical link to the page cache.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
            if do_fsync:
                os.fsync(fd)
        finally:
            os.close(fd)


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now(timezone.utc)
