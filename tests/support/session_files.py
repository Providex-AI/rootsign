"""Session-file fixtures: sessions written by the real store, then damaged.

Shared by the export suites (T3.1/T3.2/T3.6). Records come from
`JsonlIngestClient` rather than hand-built dicts on purpose — the bundle's job
is to report what a store actually wrote, and a fixture that invented the
records would let the two drift while every test stayed green.

`damage_action` / `drop_action` are the two ways a chain goes wrong, so a test
can ask for a TAMPERED or INCOMPLETE session without knowing how either verdict
is produced.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from rootsign.sdk.hashing import compute_payload_hash
from rootsign.sdk.jsonl_client import JsonlIngestClient
from tests.conftest import make_envelope

TOOLS = ("send_email", "query_db", "charge_card", "write_file")


def write_session_file(
    data_dir: Path,
    *,
    actions: int = 3,
    previews: bool = True,
    approval: bool = False,
    decision: bool = False,
) -> Path:
    """Drive a real session through the JSONL writer; return its file.

    `previews=True` stores redacted payloads AND binds their hashes — an
    unbound preview makes the session verify TAMPERED on ADR-006's payload
    binding, which would silently turn every caller's test into a test about a
    broken session.
    """

    async def _run() -> Path:
        client = JsonlIngestClient(data_dir=data_dir)
        agent_id, session_id = uuid4(), uuid4()
        await client.handle(
            make_envelope("SESSION_OPEN", agent_id, session_id, {"objective": "quarterly run"})
        )
        if decision:
            await client.handle(
                make_envelope(
                    "DECISION_RECORD",
                    agent_id,
                    session_id,
                    {
                        "selected_action": "escalate_to_human",
                        "confidence": 0.42,
                        "alternatives_considered": ["auto_approve", "reject"],
                        "reasoning_summary": "amount over threshold",
                        "timestamp": "2026-08-21T09:59:00+00:00",
                    },
                )
            )
        first_action_id: str | None = None
        for i in range(actions):
            payload: dict[str, Any] = {
                "tool_name": TOOLS[i % len(TOOLS)],
                "input_hash": f"{i:064d}",
                "output_hash": "b" * 64,
                "timestamp": f"2026-08-21T10:0{i}:00+00:00",
                "authorization_status": "auto_authorized",
            }
            if previews:
                input_redacted = {
                    "to": "[REDACTED]",
                    "cc": ["ops@example.com", "[REDACTED]"],
                    "meta": {"account": "[REDACTED]", "region": "eu-west-1"},
                }
                output_redacted = {"status": "ok"}
                # The hashes must match the previews or `verify` reports
                # TAMPERED on the payload binding (ADR-006) — which would make
                # every assertion in this file about a broken session.
                payload["input_redacted"] = input_redacted
                payload["output_redacted"] = output_redacted
                payload["input_hash"] = compute_payload_hash(input_redacted)
                payload["output_hash"] = compute_payload_hash(output_redacted)
            response = await client.handle(
                make_envelope("ACTION_RECORD", agent_id, session_id, payload)
            )
            first_action_id = first_action_id or str(response.entity_id)
        if approval:
            await client.handle(
                make_envelope(
                    "APPROVAL_RECORD",
                    agent_id,
                    session_id,
                    {
                        "action_id": first_action_id,
                        "approver_id": "sile",
                        "approver_type": "human",
                        "context_presented": {"tool_name": TOOLS[0], "input_summary": "..."},
                        "decision": "rejected",
                        "decision_reason": "amount exceeds mandate",
                        "timestamp": "2026-08-21T10:30:00+00:00",
                    },
                )
            )
        await client.handle(
            make_envelope("SESSION_CLOSE", agent_id, session_id, {"status": "completed"})
        )
        return client._session_path(str(session_id))

    return asyncio.run(_run())


def damage_action(path: Path, sequence: int, field: str, value: Any) -> None:
    """Rewrite one canonical field — the session now verifies TAMPERED."""
    lines = path.read_text().splitlines()
    for i, line in enumerate(lines):
        record = json.loads(line)
        if (
            record.get("event_type") == "ACTION_RECORD"
            and record.get("sequence_number") == sequence
        ):
            record[field] = value
            lines[i] = json.dumps(record)
            break
    path.write_text("\n".join(lines) + "\n")


def drop_action(path: Path, sequence: int) -> None:
    """Delete one record — the session now verifies INCOMPLETE."""
    kept = [
        line
        for line in path.read_text().splitlines()
        if not (
            json.loads(line).get("event_type") == "ACTION_RECORD"
            and json.loads(line).get("sequence_number") == sequence
        )
    ]
    path.write_text("\n".join(kept) + "\n")
