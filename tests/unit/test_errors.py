"""Lock the rootsign error hierarchy.

Sprint 4 introduced `RootSignError` as a new top-level base above the
pre-existing `IngestError` tree, plus three HiTL exceptions raised out of
the trace decorator into application code. The contract:

* Every rootsign-raised exception is a `RootSignError` subclass — apps
  that want one catch-all can use it.
* `IngestError` still has the `error_code` / `retryable` class attrs the
  ingest handler depends on — Sprint 4 reparenting must not have broken
  that contract.
* HiTL exceptions expose structured attributes (`.action_id`,
  `.timeout_seconds`, `.reason`) so callers can branch on them without
  string-parsing the message.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

import rootsign
from rootsign.errors import (
    ActionAlreadyResolvedError,
    ActionNotFoundError,
    EscalationDepthExceededError,
    HiTLRejectedError,
    HiTLTimeoutError,
    IngestError,
    RootSignError,
)


class TestHierarchy:
    def test_ingest_errors_are_rootsign_errors(self):
        assert issubclass(IngestError, RootSignError)
        assert issubclass(ActionNotFoundError, RootSignError)
        assert issubclass(ActionAlreadyResolvedError, RootSignError)

    def test_ingest_error_code_contract_intact(self):
        # IngestHandler.handle() depends on these class attrs surviving
        # the Sprint 4 reparenting.
        assert ActionNotFoundError.error_code == "ACTION_NOT_FOUND"
        assert ActionNotFoundError.retryable is False
        assert ActionAlreadyResolvedError.error_code == "ACTION_ALREADY_RESOLVED"

    def test_hitl_errors_are_rootsign_errors_but_not_ingest_errors(self):
        # HiTL errors are SDK-level — they must NOT be caught by the
        # ingest handler's `except IngestError` block, otherwise a HiTL
        # timeout would silently turn into an ingest rejected response.
        for cls in (HiTLTimeoutError, HiTLRejectedError, EscalationDepthExceededError):
            assert issubclass(cls, RootSignError)
            assert not issubclass(cls, IngestError)


class TestHiTLErrorAttributes:
    def test_timeout_carries_action_id_and_seconds(self):
        action_id = uuid4()
        err = HiTLTimeoutError(action_id, 300)
        assert err.action_id == action_id
        assert err.timeout_seconds == 300
        assert "timed_out" in str(err)
        assert str(action_id) in str(err)

    def test_rejected_carries_action_id_and_reason(self):
        action_id = uuid4()
        err = HiTLRejectedError(action_id, reason="Too risky")
        assert err.action_id == action_id
        assert err.reason == "Too risky"
        assert "Too risky" in str(err)

    def test_rejected_without_reason(self):
        action_id = uuid4()
        err = HiTLRejectedError(action_id)
        assert err.reason is None
        assert "rejected by approver" in str(err)

    def test_escalation_depth_carries_action_id(self):
        action_id = uuid4()
        err = EscalationDepthExceededError(action_id)
        assert err.action_id == action_id


class TestPublicReExports:
    """The HiTL errors must be importable from the package root so that
    application code following the README can write:

        from rootsign import HiTLTimeoutError
    """

    def test_root_package_reexports_hitl_errors(self):
        assert rootsign.HiTLTimeoutError is HiTLTimeoutError
        assert rootsign.HiTLRejectedError is HiTLRejectedError
        assert rootsign.RootSignError is RootSignError
        assert rootsign.EscalationDepthExceededError is EscalationDepthExceededError

    def test_hitl_errors_in_all(self):
        # They appear in __all__ so star-imports surface them and tooling
        # (mypy, ruff) doesn't flag them as private re-exports.
        assert "HiTLTimeoutError" in rootsign.__all__
        assert "HiTLRejectedError" in rootsign.__all__
        assert "RootSignError" in rootsign.__all__
        assert "EscalationDepthExceededError" in rootsign.__all__


class TestRaiseAndCatch:
    """Smoke: raising and catching via every level of the hierarchy."""

    def test_catch_hitl_as_rootsign_error(self):
        with pytest.raises(RootSignError):
            raise HiTLTimeoutError(uuid4(), 5)

    def test_catch_ingest_as_rootsign_error(self):
        with pytest.raises(RootSignError):
            raise ActionNotFoundError("msg")

    def test_ingest_handler_still_filters_only_ingest_errors(self):
        # The handler's intended pattern: catch IngestError, let HiTL through.
        # This test fails if Sprint 4 accidentally moved a HiTL error under
        # IngestError.
        try:
            raise HiTLTimeoutError(uuid4(), 5)
        except IngestError:
            pytest.fail("HiTLTimeoutError was caught by IngestError handler")
        except HiTLTimeoutError:
            pass
