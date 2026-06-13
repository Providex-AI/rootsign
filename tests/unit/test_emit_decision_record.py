"""Unit tests for `_emit_decision_record` (PRD-19 T5)."""

from __future__ import annotations

import importlib
import inspect
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from rootsign.sdk import config as cfg
from rootsign.sdk.context import SessionContext
from rootsign.sdk.decorator import _emit_decision_record


AGENT_ID = UUID("550e8400-e29b-41d4-a716-446655440000")


@pytest.fixture
def capture_on(monkeypatch):
    monkeypatch.setenv("ROOTSIGN_CAPTURE_DECISIONS", "true")
    monkeypatch.setenv("ROOTSIGN_REASONING_DEPTH", "summary")
    importlib.reload(cfg)
    yield
    monkeypatch.delenv("ROOTSIGN_CAPTURE_DECISIONS", raising=False)
    monkeypatch.delenv("ROOTSIGN_REASONING_DEPTH", raising=False)
    importlib.reload(cfg)


def _mock_client_returning(entity_id):
    client = AsyncMock()
    client.handle.return_value = MagicMock(entity_id=entity_id, status="accepted")
    return client


class TestSignature:
    def test_emit_decision_record_is_keyword_only(self):
        """Sprint 4 Flag 1."""
        sig = inspect.signature(_emit_decision_record)
        for name, param in sig.parameters.items():
            assert param.kind == inspect.Parameter.KEYWORD_ONLY, (
                f"Parameter {name} must be keyword-only"
            )


class TestEnvelopeShape:
    async def test_envelope_omits_decision_id_field(self, capture_on):
        """DecisionRecordPayload is extra='forbid'; decision_id is handler-assigned."""
        ctx = SessionContext(agent_id=AGENT_ID)
        client = _mock_client_returning(uuid4())

        await _emit_decision_record(
            client=client,
            ctx=ctx,
            selected_action="test_tool",
        )
        envelope = client.handle.call_args[0][0]
        assert "decision_id" not in envelope["payload"]
        assert envelope["event_type"] == "DECISION_RECORD"
        assert envelope["payload"]["selected_action"] == "test_tool"

    async def test_returns_entity_id_from_response(self, capture_on):
        """Caller stashes the returned id as _pending_decision_id."""
        ctx = SessionContext(agent_id=AGENT_ID)
        expected = uuid4()
        client = _mock_client_returning(expected)

        result = await _emit_decision_record(
            client=client,
            ctx=ctx,
            selected_action="test_tool",
        )
        assert result == expected


class TestReasoningDepth:
    async def test_minimal_drops_reasoning_summary(self, monkeypatch):
        monkeypatch.setenv("ROOTSIGN_CAPTURE_DECISIONS", "true")
        monkeypatch.setenv("ROOTSIGN_REASONING_DEPTH", "minimal")
        importlib.reload(cfg)

        ctx = SessionContext(agent_id=AGENT_ID)
        client = _mock_client_returning(uuid4())
        await _emit_decision_record(
            client=client,
            ctx=ctx,
            selected_action="test_tool",
            reasoning_summary="This should be dropped at minimal depth.",
            alternatives_considered=["a", "b"],
        )

        payload = client.handle.call_args[0][0]["payload"]
        assert payload["reasoning_summary"] is None
        assert payload["alternatives_considered"] == []
        assert payload["reasoning_depth"] == "minimal"
        assert payload["reasoning_captured"] is False

        monkeypatch.delenv("ROOTSIGN_REASONING_DEPTH", raising=False)
        importlib.reload(cfg)

    async def test_summary_truncates_at_500_drops_alternatives(self, capture_on):
        ctx = SessionContext(agent_id=AGENT_ID)
        client = _mock_client_returning(uuid4())
        long_reasoning = "x" * 1000

        await _emit_decision_record(
            client=client,
            ctx=ctx,
            selected_action="test_tool",
            reasoning_summary=long_reasoning,
            alternatives_considered=["a", "b", "c"],
        )

        payload = client.handle.call_args[0][0]["payload"]
        assert payload["reasoning_summary"] == "x" * 500
        assert payload["alternatives_considered"] == []
        assert payload["reasoning_depth"] == "summary"
        assert payload["reasoning_captured"] is True

    async def test_full_keeps_10k_and_alternatives(self, monkeypatch):
        monkeypatch.setenv("ROOTSIGN_CAPTURE_DECISIONS", "true")
        monkeypatch.setenv("ROOTSIGN_REASONING_DEPTH", "full")
        importlib.reload(cfg)

        ctx = SessionContext(agent_id=AGENT_ID)
        client = _mock_client_returning(uuid4())
        long_reasoning = "y" * 15000

        await _emit_decision_record(
            client=client,
            ctx=ctx,
            selected_action="test_tool",
            reasoning_summary=long_reasoning,
            alternatives_considered=["a", "b", "c"],
        )

        payload = client.handle.call_args[0][0]["payload"]
        assert payload["reasoning_summary"] == "y" * 10_000
        assert payload["alternatives_considered"] == ["a", "b", "c"]
        assert payload["reasoning_depth"] == "full"

        monkeypatch.delenv("ROOTSIGN_REASONING_DEPTH", raising=False)
        importlib.reload(cfg)


class TestFailureIsolation:
    async def test_ingest_failure_returns_none_does_not_raise(self, capture_on, caplog):
        ctx = SessionContext(agent_id=AGENT_ID)
        client = AsyncMock()
        client.handle.side_effect = RuntimeError("transport down")

        result = await _emit_decision_record(
            client=client,
            ctx=ctx,
            selected_action="test_tool",
        )

        assert result is None
        assert any(
            "_emit_decision_record failed" in rec.message
            for rec in caplog.records
        )
