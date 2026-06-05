"""Ingest-layer error hierarchy.

These exceptions are raised by the CRUD layer when an ingest-side invariant
fails, and caught by IngestHandler.handle() which maps them to the
corresponding error_code in IngestResponse. Living in rootsign/ root rather
than rootsign/ingest/ keeps the dependency direction clean (crud doesn't
import from ingest).

Add a new subclass here when adding a new error code to the registry in
the Ingestion Spec, Section 5.3.
"""

from __future__ import annotations


class IngestError(Exception):
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
