"""MCP audit-log server contract tests (ADR-010, Mode B) — DB-less.

Verifies the server's wiring without a database: import isolation, that
`create_server` registers exactly the four read-only audit tools with the
expected input schemas, and that `create_server_app` yields an ASGI app.
The tools' DB behavior is covered by tests/integration/test_mcp_server.py.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("mcp", reason="rootsign[mcp] not installed")

from rootsign.mcp.server import create_server, create_server_app  # noqa: E402

EXPECTED_TOOLS = {
    "list_sessions",
    "query_session_chain",
    "verify_session_chain",
    "get_approval_records",
}


def _dummy_factory():
    # Never invoked in these DB-less tests — tool bodies aren't called here.
    raise AssertionError("session factory should not be used in contract tests")


def test_module_imports_without_touching_mcp_at_load():
    import rootsign.mcp.server as server_mod

    # FastMCP must be imported lazily inside create_server, not at module load.
    assert not hasattr(server_mod, "FastMCP")


def test_registers_exactly_the_four_audit_tools():
    server = create_server(session_factory=_dummy_factory)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == EXPECTED_TOOLS


def test_tool_input_schemas():
    server = create_server(session_factory=_dummy_factory)
    tools = {t.name: t for t in asyncio.run(server.list_tools())}

    # list_sessions: both params optional (have defaults).
    assert tools["list_sessions"].inputSchema.get("required", []) == []
    # session-scoped tools require their id.
    assert tools["query_session_chain"].inputSchema["required"] == ["session_id"]
    assert tools["verify_session_chain"].inputSchema["required"] == ["session_id"]
    assert tools["get_approval_records"].inputSchema["required"] == ["action_id"]


def test_create_server_app_is_asgi():
    app = create_server_app(session_factory=_dummy_factory)
    # Starlette ASGI app — callable, and exposes routes.
    assert callable(app)
    assert hasattr(app, "routes")
