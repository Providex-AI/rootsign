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

import warnings
from enum import Enum
from typing import Literal

from pathlib import Path

from pydantic import field_validator, model_validator
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

    # Transport selector (ADR-011). Default is now 'jsonl' — the zero-dependency
    # append-only local backend (no Docker, no Postgres). 'postgres' uses
    # LocalIngestClient against PostgreSQL/TimescaleDB (requires the `postgres`
    # extra). 'cloud' routes through HttpIngestClient (ADR-013, requires the
    # `cloud` extra; the hosted backend itself is Phase 2). 'local' is
    # the pre-0.2.0 alias for 'postgres', accepted with a DeprecationWarning.
    BACKEND: Literal["jsonl", "postgres", "cloud", "local"] = "jsonl"

    # JSONL backend (ADR-011). Root directory for session files
    # ($DATA_DIR/sessions/<session_id>.jsonl and $DATA_DIR/agents.jsonl).
    DATA_DIR: str = "~/.rootsign"
    # fsync policy: 'chain' (after ACTION/APPROVAL records — the default),
    # 'always' (every record), 'never' (rely on the page cache).
    JSONL_FSYNC: Literal["chain", "always", "never"] = "chain"

    # Hosted ingest endpoint (Phase 2). Default points at the Providex AI
    # cloud — RootSign products are hosted under getprovidex.com. NOTE the
    # default already ends in `/v1`, so the request path is
    # `{CLOUD_URL}/ingest`; `{CLOUD_URL}/v1/ingest` doubles the prefix
    # (ADR-013 Decision 2, ingest-spec-v1 §7).
    CLOUD_URL: str = "https://ingest.getprovidex.com/v1"
    API_KEY: str = ""

    # Offline spool root (ADR-013 Decision 4). Empty means "derive from
    # DATA_DIR" — see the validator below. Spool files are ordinary session
    # files, which is what lets `rootsign verify --local` and
    # `rootsign export --local` read them while the network is still down.
    SPOOL_DIR: str = ""

    # What to do when a record cannot be persisted at all — the endpoint is
    # unreachable AND the spool write fails (ADR-013 Decision 4a).
    #   'warn' (default): telemetry records drop with accounting — one CRITICAL,
    #           an in-memory loss ledger, and a hash-evident gap in the chain.
    #           Honors ADR-002: the agent keeps running.
    #   'fail':  telemetry raises `RecordPersistenceError` into the caller, for
    #           deployments that prefer a halted agent to an incomplete record.
    # HiTL/approval records fail closed either way — the setting only moves the
    # telemetry path. Note this is best-effort under ROOTSIGN_BUFFERED=true:
    # buffered records are flushed after the tool has already returned, so
    # there is no caller left to raise into.
    ON_RECORD_LOSS: Literal["warn", "fail"] = "warn"

    # HttpIngestClient transport knobs (ADR-013 Decisions 2-3). HTTP_MAX_RETRIES
    # is the cap on **total attempts** per request, not retries after the first
    # — 3 means one send plus at most two retries, matching
    # BufferedIngestClient.max_retries. The transport is the only layer that
    # retries when it is the inner client; see BufferedIngestClient.
    HTTP_TIMEOUT_SECONDS: float = 10.0
    HTTP_MAX_RETRIES: int = 3

    @model_validator(mode="after")
    def _derive_spool_dir(self) -> "SDKSettings":
        """Default the spool root to `$DATA_DIR/spool`.

        Derived rather than defaulted to a literal so that setting
        `ROOTSIGN_DATA_DIR` alone moves *both* the session files and the spool
        — a user who redirects their data directory and finds spooled records
        still landing in `~/.rootsign` has lost evidence they thought they had
        relocated. `ROOTSIGN_SPOOL_DIR` still overrides for the split case.

        The path stays un-expanded (`~/...`); `JsonlIngestClient` expands it,
        and it appends its own `sessions/` segment underneath.
        """
        if not self.SPOOL_DIR:
            self.SPOOL_DIR = str(Path(self.DATA_DIR) / "spool")
        return self

    @field_validator("BACKEND")
    @classmethod
    def _normalize_backend(cls, v: str) -> str:
        # 'local' → 'postgres' (deprecated alias, ADR-011 Decision 1).
        if v == "local":
            warnings.warn(
                "ROOTSIGN_BACKEND=local is deprecated; use 'postgres'. "
                "'local' will be removed in a future release.",
                DeprecationWarning,
                stacklevel=2,
            )
            return "postgres"
        return v

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

    # LEGACY, read by no code path. Superseded by HTTP_TIMEOUT_SECONDS /
    # HTTP_MAX_RETRIES above (ADR-013), which are what the cloud transport
    # actually honors. Kept so an existing .env that sets them still loads;
    # setting them changes nothing. Removal is a (minor) breaking change and
    # wants a founder call.
    MAX_RETRIES: int = 3
    RETRY_BASE_DELAY: float = 0.1
    RETRY_MAX_DELAY: float = 5.0


sdk_settings = SDKSettings()
