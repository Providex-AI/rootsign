"""MCP audit-log server integration (ADR-010, Mode B) — real DB.

Seeds a session with a chain of actions (+ one approval) via the ingest path,
then exercises the four audit query helpers through a session factory bound to
the test DB — proving they return the real, committed audit data and that
verify_session_chain agrees with `action_crud.verify_chain`.

Drives the standalone `_…` query functions directly (each opens its own
session from the factory), not the FastMCP HTTP transport: the transport/tool
wiring is covered by tests/contract/mcp/test_server.py, and a full MCP client
over an anyio portal loop would cross-loop the asyncpg factory. `seeded_agent`
(committed) per Flag 3 — the factory reads on its own connection.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

pytest.importorskip("mcp", reason="rootsign[mcp] not installed")

from rootsign.config import settings  # noqa: E402
from rootsign.crud import action as action_crud  # noqa: E402
from rootsign.ingest import IdempotencyStore, IngestHandler  # noqa: E402
from rootsign.mcp.server import (  # noqa: E402
    _get_approval_records,
    _list_sessions,
    _query_session_chain,
    _verify_session_chain,
)
from rootsign.models.approval import Approval  # noqa: E402
from tests.conftest import make_envelope  # noqa: E402


@pytest.fixture
def audit_session_factory():
    """A session factory bound to the test DB (NullPool → fresh conn per call),
    mirroring the isolation of the server's default AsyncSessionLocal."""
    engine = create_async_engine(settings.TEST_DATABASE_URL, poolclass=NullPool, future=True)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield factory


def _action_payload(tool_name: str) -> dict:
    return {
        "tool_name": tool_name,
        "input_hash": "a" * 64,
        "output_hash": "b" * 64,
        "authorization_status": "auto_authorized",
    }


async def _seed_session(clean_db, agent_id: UUID) -> tuple[UUID, list[UUID]]:
    """Open a session + insert 3 actions. Returns (session_id, [action_id...])."""
    handler = IngestHandler(db=clean_db, idempotency=IdempotencyStore())
    session_id = uuid4()
    await handler.handle(
        make_envelope("SESSION_OPEN", agent_id, session_id, {"objective": "mcp-server test"})
    )
    action_ids: list[UUID] = []
    for tool in ("send_email", "query_db", "write_file"):
        resp = await handler.handle(
            make_envelope("ACTION_RECORD", agent_id, session_id, _action_payload(tool))
        )
        assert resp.entity_id is not None
        action_ids.append(resp.entity_id)
    await clean_db.commit()
    return session_id, action_ids


class TestMCPServerIntegration:
    async def test_list_sessions_returns_seeded_session(
        self, clean_db, seeded_agent, audit_session_factory
    ):
        session_id, _ = await _seed_session(clean_db, seeded_agent.agent_id)

        rows = await _list_sessions(audit_session_factory, agent_id=str(seeded_agent.agent_id))
        ids = {r["session_id"] for r in rows}
        assert str(session_id) in ids
        row = next(r for r in rows if r["session_id"] == str(session_id))
        assert row["agent_id"] == str(seeded_agent.agent_id)
        assert row["action_count"] == 3

    async def test_query_session_chain_is_ordered(
        self, clean_db, seeded_agent, audit_session_factory
    ):
        session_id, _ = await _seed_session(clean_db, seeded_agent.agent_id)

        chain = await _query_session_chain(audit_session_factory, session_id=str(session_id))
        assert [a["sequence_number"] for a in chain] == [1, 2, 3]
        assert chain[0]["tool_name"] == "send_email"
        # Chain metadata is present; hashes are exposed for the auditor.
        assert all(a["self_hash"] for a in chain)

    async def test_verify_session_chain_matches_crud(
        self, clean_db, seeded_agent, audit_session_factory
    ):
        session_id, _ = await _seed_session(clean_db, seeded_agent.agent_id)

        result = await _verify_session_chain(audit_session_factory, session_id=str(session_id))
        assert result["valid"] is True
        assert result["record_count"] == 3
        # Identical to the CLI/CRUD verify path.
        crud_result = await action_crud.verify_chain(clean_db, session_id=session_id)
        assert result == crud_result

    async def test_get_approval_records_returns_seeded_approval(
        self, clean_db, seeded_agent, audit_session_factory
    ):
        session_id, action_ids = await _seed_session(clean_db, seeded_agent.agent_id)
        target = action_ids[0]
        clean_db.add(
            Approval(
                approval_id=uuid4(),
                action_id=target,
                session_id=session_id,
                approver_id="cli:tester",
                approver_type="human",
                context_presented={"tool_name": "send_email"},
                decision="approved",
                decision_reason="verified",
                timestamp=datetime.now(timezone.utc),
            )
        )
        await clean_db.commit()

        records = await _get_approval_records(audit_session_factory, action_id=str(target))
        assert len(records) == 1
        assert records[0]["decision"] == "approved"
        assert records[0]["approver_id"] == "cli:tester"
        assert records[0]["action_id"] == str(target)

    async def test_get_approval_records_empty_for_unapproved_action(
        self, clean_db, seeded_agent, audit_session_factory
    ):
        _, action_ids = await _seed_session(clean_db, seeded_agent.agent_id)
        records = await _get_approval_records(audit_session_factory, action_id=str(action_ids[1]))
        assert records == []
