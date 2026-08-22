"""`rootsign export <session_id>` against a real database (Sprint B T3.5).

The file-backed half of the command is covered in `tests/unit/test_export_cli.py`;
this is the path that needs Postgres — and the one where the CLI's session
factory, the `postgres` extra, and the hypertable-backed chain all have to line
up behind a single command.

Typer's runner calls `asyncio.run` internally, so every invocation goes through
`asyncio.to_thread` (CLAUDE.md test invariant 2) and the CLI's factory is bound
to the test database via the same `AsyncSessionLocal` seam `verify` uses.
`seeded_agent` (committed) because the CLI reads through its own engine.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from typer.testing import CliRunner

from rootsign.config import settings
from rootsign.sdk.cli import app
from rootsign.sdk.client import LocalIngestClient
from rootsign.sdk.export import MANIFEST_FILE
from rootsign.sdk.hashing import compute_payload_hash
from tests.conftest import make_envelope

runner = CliRunner()


@pytest.fixture
def patched_cli_session(monkeypatch):
    """Bind the CLI's session factory to TEST_DATABASE_URL."""
    engine = create_async_engine(settings.TEST_DATABASE_URL, poolclass=NullPool, future=True)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr("rootsign.sdk.cli.AsyncSessionLocal", factory)
    yield factory


@pytest.fixture
async def stored_session(clean_db, seeded_agent):
    """A committed session with a decision, three actions and an approval."""
    session_id = uuid4()
    client = LocalIngestClient(db=clean_db)
    await client.handle(
        make_envelope(
            "SESSION_OPEN", seeded_agent.agent_id, session_id, {"objective": "export via cli"}
        )
    )
    await client.handle(
        make_envelope(
            "DECISION_RECORD",
            seeded_agent.agent_id,
            session_id,
            {
                "selected_action": "proceed",
                "confidence": 0.77,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
    )
    first_action_id = None
    for tool in ("send_email", "query_db", "charge_card"):
        redacted = {"to": "[REDACTED]", "tool": tool}
        response = await client.handle(
            make_envelope(
                "ACTION_RECORD",
                seeded_agent.agent_id,
                session_id,
                {
                    "tool_name": tool,
                    "input_hash": compute_payload_hash(redacted),
                    "output_hash": "b" * 64,
                    "input_redacted": redacted,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "authorization_status": "auto_authorized",
                },
            )
        )
        first_action_id = first_action_id or response.entity_id
    await client.handle(
        make_envelope(
            "APPROVAL_RECORD",
            seeded_agent.agent_id,
            session_id,
            {
                "action_id": str(first_action_id),
                "approver_id": "sile",
                "approver_type": "human",
                "context_presented": {"tool_name": "send_email"},
                "decision": "approved",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
    )
    await client.handle(
        make_envelope("SESSION_CLOSE", seeded_agent.agent_id, session_id, {"status": "completed"})
    )
    await clean_db.commit()
    return session_id


def _bundle_dir(out: Path) -> Path:
    return next(out.glob("evidence-*"))


class TestExportFromPostgres:
    async def test_a_stored_session_exports_with_its_registered_agent(
        self, stored_session, tmp_path, patched_cli_session, seeded_agent
    ):
        """What the database source adds over a file: who the agent was.

        An auditor needs owner and risk tier to know whose process this was,
        and that lives in the registration row — nowhere in a session file.
        """
        result = await asyncio.to_thread(
            runner.invoke, app, ["export", str(stored_session), "--out", str(tmp_path)]
        )

        assert result.exit_code == 0, result.output
        assert result.output.splitlines()[0].startswith("VALID")

        manifest = json.loads((_bundle_dir(tmp_path) / MANIFEST_FILE).read_text())
        assert manifest["source"] == {"backend": "postgres", "location": "database"}
        assert manifest["agent"]["name"] == seeded_agent.name
        assert manifest["agent"]["owner"] == seeded_agent.owner
        assert manifest["verdict"] == "VALID"

    async def test_the_narrative_survives_the_round_trip(
        self, stored_session, tmp_path, patched_cli_session
    ):
        result = await asyncio.to_thread(
            runner.invoke, app, ["export", str(stored_session), "--out", str(tmp_path)]
        )
        assert result.exit_code == 0, result.output

        timeline = json.loads((_bundle_dir(tmp_path) / "timeline.json").read_text())

        assert [e["type"] for e in timeline["events"]] == [
            "SESSION_OPEN",
            "DECISION",
            "ACTION",
            "ACTION",
            "ACTION",
            "APPROVAL",
            "SESSION_CLOSE",
        ]
        assert timeline["session"]["objective"] == "export via cli"

    async def test_the_bundle_it_wrote_passes_its_own_check(
        self, stored_session, tmp_path, patched_cli_session
    ):
        await asyncio.to_thread(
            runner.invoke, app, ["export", str(stored_session), "--out", str(tmp_path)]
        )

        result = await asyncio.to_thread(
            runner.invoke, app, ["export", "--check", str(_bundle_dir(tmp_path))]
        )

        assert result.exit_code == 0, result.output
        assert "INTACT" in result.output

    async def test_an_unknown_session_is_one_clean_line(self, tmp_path, patched_cli_session):
        """Better than an empty bundle: evidence for a session that does not
        exist would read as a session in which nothing happened."""
        result = await asyncio.to_thread(
            runner.invoke, app, ["export", str(uuid4()), "--out", str(tmp_path)]
        )

        assert result.exit_code == 1
        assert "not found" in result.output
        assert "Traceback" not in result.output
        assert list(tmp_path.glob("evidence-*")) == []
