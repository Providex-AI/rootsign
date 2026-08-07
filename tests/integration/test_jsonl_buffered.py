"""T2.9 — BufferedIngestClient(JsonlIngestClient(...)) composes correctly.

Selective buffering (ADR-009) is backend-blind: auto-authorized ACTION_RECORDs
buffer, session/decision/approval records passthrough. Wrapping the JSONL
backend must lose no records and keep the chain ordered — the buffer drains
FIFO ahead of the SESSION_CLOSE passthrough, so the file's action chain is
dense and verifies VALID. DB-free.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from rootsign.sdk.buffered_client import BufferedIngestClient
from rootsign.sdk.chain import verify_session_local
from rootsign.sdk.hashing import compute_payload_hash
from rootsign.sdk.jsonl_client import JsonlIngestClient


def _env(event_type, session_id, payload=None):
    return {
        "schema_version": "1.0",
        "sdk_version": "0.2.0",
        "event_type": event_type,
        "event_id": str(uuid4()),
        "emitted_at": datetime.now(timezone.utc).isoformat(),
        "agent_id": str(uuid4()),
        "session_id": str(session_id),
        "payload": payload or {},
    }


def _action(session_id, tool):
    ir = {"tool": tool}
    return _env(
        "ACTION_RECORD",
        session_id,
        {
            "tool_name": tool,
            "input_hash": compute_payload_hash(ir),
            "output_hash": "b" * 64,
            "input_redacted": ir,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "authorization_status": "auto_authorized",
        },
    )


async def test_buffered_jsonl_no_records_lost(tmp_path):
    inner = JsonlIngestClient(data_dir=tmp_path)
    sid = uuid4()
    async with BufferedIngestClient(inner, flush_interval_seconds=999) as buffered:
        await buffered.handle(_env("SESSION_OPEN", sid, {"objective": "x"}))
        for tool in ("send_email", "query_db", "write_file", "notify", "log"):
            await buffered.handle(_action(sid, tool))
        # Actions are still buffered — not yet on disk.
        pre = verify_session_local(str(inner._session_path(str(sid))))
        assert pre.record_count == 0
        # SESSION_CLOSE passthrough drains the buffered actions first (FIFO).
        await buffered.handle(_env("SESSION_CLOSE", sid, {"status": "completed"}))

    result = verify_session_local(str(inner._session_path(str(sid))))
    assert result.valid is True
    assert result.record_count == 5
