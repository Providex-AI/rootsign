"""T2.6 — synchronous HiTL on the JSONL backend (ADR-011 §6). DB-free.

Headless (no TTY) + require_approval raises HiTLUnsupportedBackendError before
the tool runs; with a TTY, an inline prompt approves (tool runs) or rejects
(HiTLRejectedError). `input()` is patched (the code prompts via
asyncio.to_thread(input, ...)); `sys.stdin.isatty` is patched for the TTY tests.
"""

from __future__ import annotations

import json
from unittest.mock import patch
from uuid import uuid4

import pytest

from rootsign.errors import HiTLRejectedError, HiTLUnsupportedBackendError
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
