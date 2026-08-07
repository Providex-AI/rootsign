"""Ingest layer — SDK ↔ store wire protocol (Phase 0 Requirement 0.3)."""

from typing import TYPE_CHECKING

from rootsign.ingest.idempotency import IdempotencyStore
from rootsign.ingest.schemas import (
    ErrorCode,
    EventType,
    IngestEnvelope,
    IngestResponse,
)

if TYPE_CHECKING:
    from rootsign.ingest.handler import IngestHandler

__all__ = [
    "ErrorCode",
    "EventType",
    "IdempotencyStore",
    "IngestEnvelope",
    "IngestHandler",
    "IngestResponse",
]


def __getattr__(name: str) -> object:
    """Lazily expose `IngestHandler` (PEP 562).

    `IngestHandler` pulls the SQLAlchemy / crud / models stack, which lives in
    the optional `postgres` extra (ADR-011 packaging split). Importing it
    eagerly here would taint every `from rootsign.ingest.schemas import ...`
    across the SDK — so `from rootsign.ingest import IngestHandler` resolves it
    on first access instead, keeping the DB-free import path clean.
    """
    if name == "IngestHandler":
        from rootsign.ingest.handler import IngestHandler

        return IngestHandler
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
