"""T2.6 — synchronous HiTL on the JSONL backend (ADR-011 §6). DB-free.

Headless (no TTY) + require_approval raises HiTLUnsupportedBackendError before
the tool runs; with a TTY, an inline prompt approves (tool runs) or rejects
(HiTLRejectedError). `input()` is patched (the code prompts via
asyncio.to_thread(input, ...)); `sys.stdin.isatty` is patched for the TTY tests.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch
from uuid import uuid4

import pytest

from rootsign.errors import (
    HiTLPersistenceError,
    HiTLRejectedError,
    HiTLUnsupportedBackendError,
    RootSignError,
)
from rootsign.sdk.context import SessionContext
from rootsign.sdk.decorator import trace
from rootsign.sdk.jsonl_client import JsonlIngestClient


def _client(tmp_path):
    return JsonlIngestClient(data_dir=tmp_path)


def _ctx():
    return SessionContext(agent_id=uuid4(), session_id=uuid4())


def _lines(client, sid):
    return [json.loads(ln) for ln in client._session_path(str(sid)).read_text().splitlines()]


async def test_headless_raises_before_tool_runs(tmp_path):
    client = _client(tmp_path)
    ctx = _ctx()
    ran = []

    @trace(ingest_client=client, session_context=ctx, require_approval=True)
    async def tool(x: int) -> int:
        ran.append(x)
        return x * 2

    # In the test process stdin is not a TTY → fail-fast.
    with patch("sys.stdin.isatty", return_value=False):
        with pytest.raises(HiTLUnsupportedBackendError, match="ROOTSIGN_BACKEND=postgres"):
            await tool(21)
    assert ran == [], "tool must not run when HiTL is unsupported"


async def test_tty_approve_runs_tool_and_writes_approval(tmp_path):
    client = _client(tmp_path)
    ctx = _ctx()
    ran = []

    @trace(ingest_client=client, session_context=ctx, require_approval=True)
    async def tool(x: int) -> int:
        ran.append(x)
        return x * 2

    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", return_value="y verified"),
    ):
        result = await tool(21)

    assert result == 42
    assert ran == [21]
    lines = _lines(client, ctx.session_id)
    pending = [r for r in lines if r.get("event_type") == "ACTION_RECORD"]
    approvals = [r for r in lines if r.get("event_type") == "APPROVAL_RECORD"]
    assert pending and pending[0]["authorization_status"] == "pending"
    assert approvals and approvals[0]["payload"]["decision"] == "approved"
    assert approvals[0]["payload"]["decision_reason"] == "verified"


async def test_tty_reject_raises_and_skips_tool(tmp_path):
    client = _client(tmp_path)
    ctx = _ctx()
    ran = []

    @trace(ingest_client=client, session_context=ctx, require_approval=True)
    async def tool(x: int) -> int:
        ran.append(x)
        return x

    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", return_value="n too risky"),
    ):
        with pytest.raises(HiTLRejectedError):
            await tool(21)

    assert ran == []
    approvals = [
        r for r in _lines(client, ctx.session_id) if r.get("event_type") == "APPROVAL_RECORD"
    ]
    assert approvals and approvals[0]["payload"]["decision"] == "rejected"


@pytest.mark.parametrize(
    ("answer", "approved", "note"),
    [
        ("y", True, None),
        ("yes", True, None),
        ("Yes", True, None),
        ("yes, verified with the supplier", True, "verified with the supplier"),
        ("y verified", True, "verified"),
        ("approve", True, None),
        ("n", False, None),
        ("no", False, None),
        ("no too risky", False, "too risky"),
        ("", False, None),
        ("maybe later", False, "later"),
    ],
)
def test_the_answer_is_split_by_word_not_by_letter(answer, approved, note):
    """Typing "yes" used to approve with the note "es" (and "no" reject with "o")."""
    from rootsign.sdk.decorator import _parse_approval_answer

    assert _parse_approval_answer(answer) == (approved, note)


async def test_answering_yes_records_no_stray_note(tmp_path):
    client = _client(tmp_path)
    ctx = _ctx()

    @trace(ingest_client=client, session_context=ctx, require_approval=True)
    async def tool(x: int) -> int:
        return x

    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", return_value="yes"),
    ):
        await tool(1)

    [approval] = [
        r for r in _lines(client, ctx.session_id) if r.get("event_type") == "APPROVAL_RECORD"
    ]
    assert approval["payload"]["decision"] == "approved"
    assert approval["payload"]["decision_reason"] is None


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root writes to read-only files, so the disk failure cannot be simulated",
)
class TestApprovalRecordsFailClosed:
    """An approval record is the authorization control, not telemetry: if it
    cannot be stored, the held tool must not run (the rule HiTLPersistenceError
    states, and the cloud transport already followed).

    On the JSONL backend the approval went through the best-effort emit path,
    which logged a WARNING and let the approved tool run with no approval
    record. A pending record that could not be written raised a bare
    RuntimeError, which `except RootSignError` does not catch.

    The disk failure here is real: the session file is made read-only.
    """

    @staticmethod
    def _tool(client, ctx, ran):
        @trace(ingest_client=client, session_context=ctx, require_approval=True)
        async def tool(x: int) -> int:
            ran.append(x)
            return x * 2

        return tool

    @staticmethod
    def _answer_then_lose_the_disk(client, ctx, answer: str):
        def _input(prompt: str = "") -> str:
            # The pending record is on disk; the file becomes unwritable as
            # the person answers.
            client._session_path(str(ctx.session_id)).chmod(0o400)
            return answer

        return _input

    @pytest.mark.parametrize("answer", ["y", "n"])
    async def test_a_lost_approval_record_stops_the_tool(self, tmp_path, answer):
        client, ctx, ran = _client(tmp_path), _ctx(), []
        tool = self._tool(client, ctx, ran)

        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", self._answer_then_lose_the_disk(client, ctx, answer)),
        ):
            with pytest.raises(HiTLPersistenceError, match="The gated action was not executed"):
                await tool(21)

        assert ran == [], "the tool ran without a stored approval"
        lines = _lines(client, ctx.session_id)
        assert [r["event_type"] for r in lines] == ["ACTION_RECORD"]

    async def test_a_lost_pending_record_raises_a_rootsign_error(self, tmp_path):
        client, ctx, ran = _client(tmp_path), _ctx(), []
        path = client._session_path(str(ctx.session_id))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o400)
        tool = self._tool(client, ctx, ran)

        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input") as asked:
            with pytest.raises(HiTLPersistenceError) as raised:
                await tool(21)

        assert isinstance(raised.value, RootSignError)
        assert raised.value.tool_name == "tool"
        asked.assert_not_called()
        assert ran == []
