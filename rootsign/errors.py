"""RootSign error hierarchy.

Two layers live here:

* Ingest-side errors (`IngestError` and subclasses) are raised by the CRUD
  layer when an ingest-side invariant fails, then caught by
  `IngestHandler.handle()` which maps them to an `error_code` in the
  `IngestResponse`. Adding a new subclass means adding a new code to the
  registry in the Ingestion Spec, Section 5.3.

* HiTL / SDK errors (`HiTLTimeoutError`, `HiTLRejectedError`,
  `EscalationDepthExceededError`) propagate out of `@rootsign.trace(
  require_approval=True)` to the application code. They are NOT ingest
  rejections — they're synchronous failures of the human-in-the-loop
  contract that the caller is expected to handle.

Both layers share the `RootSignError` root so `except RootSignError` is the
single catch-all when something rootsign-shaped goes wrong.

Living in rootsign/ root (rather than rootsign/ingest/ or rootsign/sdk/)
keeps the dependency direction clean — crud, sdk, and ingest can all
import from here without back-edges.
"""

from __future__ import annotations

from uuid import UUID


class RootSignError(Exception):
    """Top of the rootsign error hierarchy.

    Every exception rootsign raises out of public surfaces (CRUD, SDK
    decorators, CLIs) is a subclass of this. Application code that wants
    the broadest possible catch-all can use:

        try:
            await tool.ainvoke(...)
        except RootSignError:
            ...
    """


class IngestError(RootSignError):
    """Base class for all ingest-side, application-level rejections.

    Subclasses set `error_code` and `retryable` as class attributes so the
    handler can translate uniformly:

        except IngestError as e:
            return rejected(error_code=e.error_code,
                            error_message=str(e),
                            retryable=e.retryable)
    """

    error_code: str = "INTERNAL_ERROR"
    retryable: bool = True


class UnknownAgentError(IngestError):
    error_code = "UNKNOWN_AGENT"
    retryable = False


class SessionNotFoundError(IngestError):
    error_code = "SESSION_NOT_FOUND"
    retryable = False


class SessionClosedError(IngestError):
    error_code = "SESSION_CLOSED"
    retryable = False


class SessionAlreadyExistsError(IngestError):
    error_code = "SESSION_ALREADY_EXISTS"
    retryable = False


class ActionNotFoundError(IngestError):
    error_code = "ACTION_NOT_FOUND"
    retryable = False


class ActionAlreadyResolvedError(IngestError):
    """The target Action is in a terminal authorization_status — no further
    APPROVAL_RECORDs (regardless of decision) may modify it."""

    error_code = "ACTION_ALREADY_RESOLVED"
    retryable = False


class ApprovalParentNotFoundError(IngestError):
    """parent_approval_id supplied but no matching Approval row exists for the
    same action_id."""

    error_code = "APPROVAL_PARENT_NOT_FOUND"
    retryable = False


class IngestValidationError(IngestError):
    """Payload validation failure that isn't covered by a more specific code
    (e.g. an `escalated` decision arriving with parent_approval_id set —
    Phase 0 enforces 2-level escalation only)."""

    error_code = "VALIDATION_ERROR"
    retryable = False


class HashChainBrokenError(IngestError):
    error_code = "HASH_CHAIN_BROKEN"
    retryable = False


# ---------------------------------------------------------------------------
# HiTL checkpoint errors (Sprint 4)
#
# These are raised out of @rootsign.trace(require_approval=True) into the
# caller's frame. They are not IngestError subclasses because they do not
# represent ingest rejections — the ACTION_RECORD / APPROVAL_RECORD writes
# succeed; what failed is the human-in-the-loop contract.
# ---------------------------------------------------------------------------


class HiTLTimeoutError(RootSignError):
    """The HiTL poll loop timed out before a human responded.

    By the time this is raised, the SDK has already written:
      * an APPROVAL_RECORD with approver_type='timeout_auto_rejected'
      * Action.authorization_status='timed_out' (terminal)

    The forensic distinction between "a human said no" (human_rejected) and
    "nobody responded" (timed_out) is preserved by these two records.
    """

    def __init__(self, action_id: UUID, timeout_seconds: int | float):
        super().__init__(
            f"HiTL approval timed out after {int(timeout_seconds)}s for "
            f"action {action_id}. Status set to timed_out."
        )
        self.action_id = action_id
        self.timeout_seconds = timeout_seconds


class HiTLRejectedError(RootSignError):
    """A human explicitly rejected the pending action via `rootsign approve --reject`.

    By the time this is raised, the SDK has already written:
      * an APPROVAL_RECORD with decision='rejected'
      * Action.authorization_status='human_rejected' (terminal)
    """

    def __init__(self, action_id: UUID, reason: str | None = None):
        msg = f"Action {action_id} rejected by approver."
        if reason:
            msg += f" Reason: {reason}"
        super().__init__(msg)
        self.action_id = action_id
        self.reason = reason


class EscalationDepthExceededError(RootSignError):
    """A chained escalation arrived where Phase 0 / Phase 1 only allows
    2-level escalation. Reserved for use by the trace decorator's escalation
    handling — CRUDApproval still raises IngestValidationError for the
    ingest-path equivalent because the Ingestion Spec already pins that
    error_code."""

    def __init__(self, action_id: UUID):
        super().__init__(
            f"Escalation depth limit (1) exceeded for action {action_id}"
        )
        self.action_id = action_id


class RootSignPostgresExtraRequired(RootSignError):
    """The Postgres backend was requested but the DB stack isn't installed.

    Since ADR-011, the SQLAlchemy / asyncpg / alembic stack lives in the
    optional ``postgres`` extra so ``pip install rootsign`` stays dependency-
    light and DB-free (the default backend is JSONL). Selecting
    ``ROOTSIGN_BACKEND=postgres`` (or the deprecated ``local``) without that
    extra raises this — with the exact install command — instead of a bare
    ``ModuleNotFoundError`` deep in the import graph.
    """

    INSTALL_HINT = (
        "ROOTSIGN_BACKEND=postgres requires the database extra. Install with:  "
        "pip install 'rootsign[postgres]'"
    )

    def __init__(self, detail: str | None = None):
        message = self.INSTALL_HINT if detail is None else f"{self.INSTALL_HINT}  ({detail})"
        super().__init__(message)


class HiTLUnsupportedBackendError(RootSignError):
    """`require_approval=True` used with a backend that can't support it here.

    The JSONL backend (ADR-011) offers *synchronous* HiTL only — an inline TTY
    prompt (PRD 1.4). It has no shared store for the async poll loop / the
    cross-process `rootsign approve` CLI. So a headless run (no TTY) with
    `require_approval=True` on the JSONL backend raises this on the tool's first
    invocation, before any work runs — with the fix in the message.
    """

    FIX_HINT = (
        "Human-in-the-loop with require_approval=True needs either an interactive "
        "terminal (JSONL backend prompts inline) or the shared store: set "
        "ROOTSIGN_BACKEND=postgres for the async poll-loop / `rootsign approve` flow."
    )

    def __init__(self, tool_name: str | None = None):
        prefix = f"HiTL not available for tool {tool_name!r} on this backend. " if tool_name else ""
        super().__init__(prefix + self.FIX_HINT)
        self.tool_name = tool_name
