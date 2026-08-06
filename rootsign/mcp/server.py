"""RootSign as an MCP Server — the audit log as a queryable data source (ADR-010, Mode B).

Exposes the tamper-evident audit log over MCP so an "auditor agent" can pull
session hash chains, verify integrity, and read approval records directly into
its context window — the same data `rootsign verify` and the CLI surface, but
reachable by any MCP client.

Four **read-only** tools over the existing CRUD / models — no new tables or
migrations:

  * ``list_sessions(agent_id?, limit=20)``      — recent sessions
  * ``query_session_chain(session_id)``         — ordered Action chain
  * ``verify_session_chain(session_id)``        — integrity check (same as `rootsign verify`)
  * ``get_approval_records(action_id)``         — HiTL approval rows for an action

**Lazy imports (ADR-010 Decision 3).** `mcp` (FastMCP) is imported *inside*
`create_server` / `create_server_app`, never at module load — so `import
rootsign` and `import rootsign.mcp.server` work without `rootsign[mcp]`. The
query helpers below use only core deps (sqlalchemy + rootsign.crud), so they
stay importable and unit-testable without the extra.

The tool bodies are thin wrappers around the standalone ``_…`` query
functions, which take a ``session_factory`` and open their own ``AsyncSession``
per call (never a caller's session) — mirroring the HiTL poll loop's isolation.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable
from uuid import UUID

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from sqlalchemy.ext.asyncio import AsyncSession

    from mcp.server.fastmcp import FastMCP

    SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

logger = logging.getLogger("rootsign.mcp.server")


def _default_session_factory() -> Any:
    """Lazily resolve the app's AsyncSessionLocal so importing this module
    never constructs a DB engine. Only touched when a tool actually runs."""
    from rootsign.database import AsyncSessionLocal

    return AsyncSessionLocal()


# --------------------------------------------------------------------------
# Standalone audit queries — read-only, session-factory injected, directly
# testable against a DB without going through FastMCP.
# --------------------------------------------------------------------------


async def _list_sessions(
    session_factory: SessionFactory,
    *,
    agent_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Most-recent sessions, optionally filtered by agent, newest first."""
    from sqlalchemy import select

    from rootsign.models.session import AgentSession

    async with session_factory() as db:
        stmt = select(AgentSession).order_by(AgentSession.start_time.desc()).limit(limit)
        if agent_id is not None:
            stmt = stmt.where(AgentSession.agent_id == UUID(agent_id))
        rows = (await db.execute(stmt)).scalars().all()
        return [
            {
                "session_id": str(s.session_id),
                "agent_id": str(s.agent_id),
                "status": s.status,
                "objective": s.objective,
                "start_time": s.start_time.isoformat(),
                "end_time": s.end_time.isoformat() if s.end_time else None,
                "action_count": s.action_count,
            }
            for s in rows
        ]


async def _query_session_chain(
    session_factory: SessionFactory,
    *,
    session_id: str,
) -> list[dict[str, Any]]:
    """The ordered Action chain for a session (metadata + hashes, no payloads)."""
    from rootsign.crud import action as action_crud

    async with session_factory() as db:
        actions = await action_crud.get_session_chain(db, session_id=UUID(session_id))
        return [
            {
                "sequence_number": a.sequence_number,
                "action_id": str(a.action_id),
                "tool_name": a.tool_name,
                "timestamp": a.timestamp.isoformat(),
                "authorization_status": a.authorization_status,
                "input_hash": a.input_hash,
                "output_hash": a.output_hash,
                "self_hash": a.self_hash,
                "prev_action_hash": a.prev_action_hash,
            }
            for a in actions
        ]


async def _verify_session_chain(
    session_factory: SessionFactory,
    *,
    session_id: str,
) -> dict[str, Any]:
    """Recompute + verify the chain — identical result to `rootsign verify`."""
    from rootsign.crud import action as action_crud

    async with session_factory() as db:
        return await action_crud.verify_chain(db, session_id=UUID(session_id))


async def _get_approval_records(
    session_factory: SessionFactory,
    *,
    action_id: str,
) -> list[dict[str, Any]]:
    """All approval rows for an action (a list — escalation can yield several)."""
    from sqlalchemy import select

    from rootsign.models.approval import Approval

    async with session_factory() as db:
        stmt = (
            select(Approval)
            .where(Approval.action_id == UUID(action_id))
            .order_by(Approval.timestamp)
        )
        rows = (await db.execute(stmt)).scalars().all()
        return [
            {
                "approval_id": str(r.approval_id),
                "action_id": str(r.action_id),
                "session_id": str(r.session_id),
                "decision": r.decision,
                "approver_id": r.approver_id,
                "approver_type": r.approver_type,
                "decision_reason": r.decision_reason,
                "timestamp": r.timestamp.isoformat(),
                "response_latency_ms": r.response_latency_ms,
            }
            for r in rows
        ]


# --------------------------------------------------------------------------
# FastMCP wiring — lazy `mcp` import.
# --------------------------------------------------------------------------


def create_server(session_factory: SessionFactory | None = None) -> FastMCP:
    """Build a FastMCP server exposing the four read-only audit tools.

    ``session_factory`` defaults to the app's ``AsyncSessionLocal`` (each tool
    opens its own session). Pass a test-bound factory in tests.
    """
    from mcp.server.fastmcp import FastMCP

    sf: SessionFactory = session_factory or _default_session_factory
    mcp = FastMCP(
        "RootSign Audit Log",
        instructions=(
            "Read-only access to the RootSign tamper-evident agent audit log. "
            "Use verify_session_chain to confirm a session's hash chain is intact."
        ),
    )

    @mcp.tool()
    async def list_sessions(agent_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """List recent agent sessions (newest first), optionally filtered by agent_id."""
        return await _list_sessions(sf, agent_id=agent_id, limit=limit)

    @mcp.tool()
    async def query_session_chain(session_id: str) -> list[dict[str, Any]]:
        """Return the ordered Action hash chain for a session."""
        return await _query_session_chain(sf, session_id=session_id)

    @mcp.tool()
    async def verify_session_chain(session_id: str) -> dict[str, Any]:
        """Verify a session's hash chain. Returns {valid, record_count, first_invalid_sequence, error}."""
        return await _verify_session_chain(sf, session_id=session_id)

    @mcp.tool()
    async def get_approval_records(action_id: str) -> list[dict[str, Any]]:
        """Return all human-in-the-loop approval records for an action."""
        return await _get_approval_records(sf, action_id=action_id)

    return mcp


def create_server_app(session_factory: SessionFactory | None = None) -> Any:
    """A uvicorn-compatible ASGI app serving the audit MCP server over HTTP.

    Usage::

        from rootsign.mcp.server import create_server_app
        app = create_server_app()
        # uvicorn rootsign.mcp.server:app --port 8001   (mounts MCP at /mcp)
    """
    return create_server(session_factory).streamable_http_app()
