"""SDK-user-facing settings for the RootSign Python SDK.

These are the env vars an application developer sets when wiring @rootsign.trace
into their agent. They are distinct from the operator/infra settings in
`rootsign/config.py` (DATABASE_URL et al.), which describe the storage backend
the local-transport path connects to.

Env var prefix: ROOTSIGN_  (e.g. ROOTSIGN_BACKEND=cloud).

All fields have safe defaults; an unconfigured installation runs against the
bundled docker-compose db using the local transport.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class ReasoningDepth(str, Enum):
    """Controls how much of a Decision's reasoning is persisted.

    MINIMAL: only selected_action + confidence (reasoning_summary dropped).
    SUMMARY: + reasoning_summary truncated to 500 chars (default).
    FULL:    + reasoning_summary truncated to 10,000 chars + alternatives.

    The persisted depth is recorded on the Decision row's
    `reasoning_depth` field so a replay consumer can tell why
    `reasoning_summary` is None or truncated. See ADR-008.
    """

    MINIMAL = "minimal"
    SUMMARY = "summary"
    FULL = "full"


class SDKSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ROOTSIGN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Transport selector. 'local' uses LocalIngestClient (Phase 1 default);
    # 'cloud' would route through HttpIngestClient, which is stubbed until
    # Phase 2 (calling .handle() raises NotImplementedError). See ADR-002.
    BACKEND: Literal["local", "cloud"] = "local"

    # Hosted ingest endpoint (Phase 2). Default points at the Providex AI
    # cloud — RootSign products are hosted under getprovidex.com.
    CLOUD_URL: str = "https://ingest.getprovidex.com/v1"
    API_KEY: str = ""

    # When True, the decorator also emits DECISION_RECORD envelopes before
    # each ACTION_RECORD. Off by default because Decision payloads tend to
    # contain the largest, most PII-dense data the agent produces.
    CAPTURE_DECISIONS: bool = False

    # How much reasoning to persist when CAPTURE_DECISIONS is True. Ignored
    # when capture is off. See `ReasoningDepth` above and ADR-008.
    REASONING_DEPTH: ReasoningDepth = ReasoningDepth.SUMMARY

    # Micro-batching (ADR-009). When True, get_ingest_client() wraps the
    # transport in a BufferedIngestClient so tool calls don't block on the
    # ingest round-trip. Off by default — LocalIngestClient is already fast;
    # this earns its keep against the Phase 2 HttpIngestClient. Field names
    # are unprefixed; env vars carry the ROOTSIGN_ prefix (ROOTSIGN_BUFFERED,
    # ROOTSIGN_BUFFER_INTERVAL, ROOTSIGN_BUFFER_MAX_SIZE).
    BUFFERED: bool = False
    BUFFER_INTERVAL: float = 0.5  # background flush cadence, seconds
    BUFFER_MAX_SIZE: int = 100  # records buffered before a forced flush

    # WAL buffer for events that fail ingest. Drained on the next successful
    # handle(). Phase 1 ships manual replay; an auto-replay loop lands in
    # Sprint 3 alongside the `rootsign verify` CLI.
    WAL_PATH: str = "~/.rootsign/wal"

    # Retry policy for HttpIngestClient (Phase 2 — unused while the cloud
    # transport is stubbed). Exponential backoff with cap.
    MAX_RETRIES: int = 3
    RETRY_BASE_DELAY: float = 0.1
    RETRY_MAX_DELAY: float = 5.0


sdk_settings = SDKSettings()
