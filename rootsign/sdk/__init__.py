"""RootSign SDK — user-facing API surface (Phase 1, Sprint 1).

Public re-exports:
    trace               — @rootsign.trace decorator (Sprint 2 adds LangGraph)
    SessionContext      — agent_id + session_id + monotonic sequence counter
    RedactionConfig     — per-field regex redaction for tool payloads
    IngestClient        — transport-agnostic ABC (ADR-002)
    LocalIngestClient   — in-process transport (Phase 1 default)
    HttpIngestClient    — cloud transport: batched HTTP ingest (ADR-013)
    get_ingest_client   — env-var-driven factory (reads ROOTSIGN_BACKEND)
    SDKSettings         — pydantic-settings model, env_prefix=ROOTSIGN_
    sdk_settings        — SDKSettings() singleton
    compute_payload_hash — SDK-side input/output digest
"""

from rootsign.sdk.client import (
    HttpIngestClient,
    IngestClient,
    LocalIngestClient,
    get_ingest_client,
)
from rootsign.sdk.config import SDKSettings, sdk_settings
from rootsign.sdk.context import SessionContext
from rootsign.sdk.decorator import trace
from rootsign.sdk.hashing import compute_payload_hash
from rootsign.sdk.redaction import REDACTED_PLACEHOLDER, RedactionConfig

__all__ = [
    "HttpIngestClient",
    "IngestClient",
    "LocalIngestClient",
    "REDACTED_PLACEHOLDER",
    "RedactionConfig",
    "SDKSettings",
    "SessionContext",
    "compute_payload_hash",
    "get_ingest_client",
    "sdk_settings",
    "trace",
]
