"""CI-runnable Show HN reproducibility test (Sprint 4 §S4-TASK 9 / §5.2).

Mirrors both README quickstarts end-to-end.

`TestZeroConfigQuickstart` — the v0.2.0 headline path (ADR-011 + ADR-012).
No Docker, no database, no extras:

    pip install rootsign
    rootsign.init(agent="invoice-agent")
    async with rootsign.session(...): rootsign.wrap_tools([...])
    $ rootsign verify --local ~/.rootsign/sessions/<id>.jsonl
    VALID ✓  —  3 records, chain intact

`TestShowHNQuickstart` — the "Production backend" section, same code against
PostgreSQL/TimescaleDB, verified by session id.

If either fails, the README is not publishable. The Sprint 4 DoD gate
(Section 5.4 item 11) calls it out as the hard gate before the post goes
live.

Sprint 4 flags applied here:
* Flag 2 Rule A: every tool invocation goes through `await tool.ainvoke(...)`.
  Never `.invoke()` or `._run()` from an async test.
* Flag 2 Rule B/C: the CLI verify is dispatched through
  `await asyncio.to_thread(runner.invoke, app, ['verify', ...])` so the
  CLI's internal `asyncio.run(...)` lands in a worker thread with no
  pre-existing loop.
* Flag 3: uses the `seeded_agent` fixture (commits to clean_db) — NOT
  `registered_agent` (SAVEPOINT-rolled-back, invisible cross-session).
"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from typer.testing import CliRunner

import rootsign
from rootsign.config import settings
from rootsign.sdk import facade
from rootsign.crud.action import action as action_crud
from rootsign.sdk.cli import app
from rootsign.sdk.client import LocalIngestClient
from rootsign.sdk.context import SessionContext
from tests.conftest import make_envelope

# seeded_agent fixture is shared from tests/conftest.py (Sprint 4 §S4-TASK 8).

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")
from langchain_core.tools import tool  # noqa: E402

from rootsign.sdk.frameworks.langgraph import LangGraphTracer  # noqa: E402

runner = CliRunner()


def _make_quickstart_tools():
    """Return fresh tools per test.

    `LangGraphTracer.wrap_tool` mutates the BaseTool in place (ADR-004) and
    sets `_rootsign_instrumented=True` as a re-wrap guard. Module-level
    `@tool` definitions would retain that flag across tests, binding their
    underlying ainvoke to whichever test's event loop saw them first —
    later tests then trip asyncpg's "Future attached to a different loop"
    error. Matches the `_make_tools()` factory pattern in
    `test_langgraph_integration.py`.
    """

    @tool
    def send_invoice(customer_id: str, amount: float) -> str:
        """Send an invoice to a customer."""
        return f"sent invoice {amount} to {customer_id}"

    @tool
    def log_payment(transaction_id: str, amount: float) -> str:
        """Log a payment record."""
        return f"logged tx {transaction_id} for {amount}"

    @tool
    def notify_customer(customer_id: str, message: str) -> str:
        """Send a notification to a customer."""
        return f"notified {customer_id}: {message}"

    return [send_invoice, log_payment, notify_customer]


@pytest.fixture
def patched_cli_session(monkeypatch):
    """Bind the CLI's session factory to TEST_DATABASE_URL.

    Without this, `rootsign verify` reads from the production dev DB
    (which doesn't have our test session), so the CLI exits "session not
    found" and the test fails for the wrong reason.
    """
    test_engine_local = create_async_engine(
        settings.TEST_DATABASE_URL, poolclass=NullPool, future=True
    )
    factory = async_sessionmaker(bind=test_engine_local, expire_on_commit=False)
    monkeypatch.setattr("rootsign.sdk.cli.AsyncSessionLocal", factory)
    yield factory


class TestZeroConfigQuickstart:
    """The v0.2.0 README headline: `pip install rootsign` → `VALID ✓`.

    Deliberately DB-free — if this needs a database to pass, the claim on the
    front page of the README is false.
    """

    @pytest.fixture(autouse=True)
    def _jsonl_env(self, monkeypatch, tmp_path):
        # The dev `.env` pins the postgres backend and conftest pins it in the
        # environment for the rest of the suite; env vars beat `.env`.
        monkeypatch.setenv("ROOTSIGN_BACKEND", "jsonl")
        monkeypatch.setenv("ROOTSIGN_DATA_DIR", str(tmp_path))
        facade._reset_init_config()
        yield
        facade._reset_init_config()

    async def test_readme_zero_config_quickstart_end_to_end(self, tmp_path):
        # Step 1-2: the three README calls. No agent registration, no client,
        # no SessionContext, no ctx=/client= on wrap_tools.
        rootsign.init(agent="invoice-agent", risk_tier="high", framework="langgraph")

        async with rootsign.session(objective="Process invoice batch") as ctx:
            tools = rootsign.wrap_tools(_make_quickstart_tools())

            # Step 3: run the tools. `await tool.ainvoke(...)` — Flag 2 Rule A.
            await tools[0].ainvoke({"customer_id": "acme", "amount": 1500.0})
            await tools[1].ainvoke({"transaction_id": "tx_001", "amount": 1500.0})
            await tools[2].ainvoke({"customer_id": "acme", "message": "Invoice sent"})

        session_file = tmp_path / "sessions" / f"{ctx.session_id}.jsonl"
        assert session_file.exists(), "the README's stated file layout changed"

        # Step 4: verify via the CLI, exactly as the README prints it.
        # Flag 2 Rule B: to_thread isolates the CLI's asyncio.run().
        result = await asyncio.to_thread(
            runner.invoke, app, ["verify", "--local", str(session_file)]
        )
        assert result.exit_code == 0, (
            f"verify --local failed (exit={result.exit_code}):\n{result.output}"
        )
        assert "VALID" in result.output
        assert "3" in result.output  # record count
        assert "✓" in result.output

        # Step 5: and mathematically, independent of the CLI's reporting.
        chain_result = rootsign.verify_session_local(str(session_file))
        assert chain_result.valid is True
        assert chain_result.record_count == 3

    async def test_tampering_a_record_flips_the_verdict(self, tmp_path):
        """The other half of the README claim: tampering is *detected*."""
        rootsign.init(agent="tamper-check")

        async with rootsign.session(objective="Process invoice batch") as ctx:
            tools = rootsign.wrap_tools(_make_quickstart_tools())
            await tools[0].ainvoke({"customer_id": "acme", "amount": 1500.0})
            await tools[1].ainvoke({"transaction_id": "tx_001", "amount": 1500.0})

        session_file = tmp_path / "sessions" / f"{ctx.session_id}.jsonl"
        lines = session_file.read_text().splitlines()
        # Mutate the first ACTION_RECORD's output hash — the shape of
        # after-the-fact log editing the hash chain exists to catch.
        for i, line in enumerate(lines):
            record = json.loads(line)
            if record.get("event_type") == "ACTION_RECORD":
                record["output_hash"] = "0" * 64
                lines[i] = json.dumps(record)
                break
        session_file.write_text("\n".join(lines) + "\n")

        result = await asyncio.to_thread(
            runner.invoke, app, ["verify", "--local", str(session_file)]
        )
        assert result.exit_code == 1
        assert "TAMPERED" in result.output


class TestShowHNQuickstart:
    """Reproduces exactly what a developer sees when following the README.

    A passing run means: a fresh `pip install rootsign[langgraph]`, the
    three lines of instrumentation code from the README, and one
    `rootsign verify <session_id>` command produces `VALID ✓  —  3 records`.
    """

    async def test_readme_quickstart_end_to_end(
        self, clean_db, seeded_agent, patched_cli_session
    ):
        # Step 1: open the session (README §3 — implicit via rootsign.session()
        # in the final README copy; here we drive the envelope directly so the
        # test reads against a known fixed shape regardless of context-manager
        # ergonomics changes).
        client = LocalIngestClient(db=clean_db)
        session_id = uuid4()
        ctx = SessionContext(
            agent_id=seeded_agent.agent_id,
            session_id=session_id,
        )
        await client.handle(
            make_envelope(
                "SESSION_OPEN",
                seeded_agent.agent_id,
                session_id,
                {"objective": "Process invoice batch"},
            )
        )

        # Step 2: instrument the tools (README §3 — the one-line wrap).
        # Fresh tool objects per test — see `_make_quickstart_tools` docstring
        # for the loop-binding rationale.
        tools = LangGraphTracer.wrap_tools(
            _make_quickstart_tools(),
            ctx=ctx,
            client=client,
        )

        # Step 3: run the tools (simulating agent execution).
        # `await tool.ainvoke(...)` — Flag 2 Rule A pinned.
        await tools[0].ainvoke(
            {"customer_id": "acme", "amount": 1500.0}
        )
        await tools[1].ainvoke(
            {"transaction_id": "tx_001", "amount": 1500.0}
        )
        await tools[2].ainvoke(
            {"customer_id": "acme", "message": "Invoice sent"}
        )

        # Close the session and commit so the CLI's separate connection
        # can read what we wrote.
        await client.handle(
            make_envelope(
                "SESSION_CLOSE",
                seeded_agent.agent_id,
                session_id,
                {"status": "completed", "metadata": {"total_actions": 3}},
            )
        )
        await clean_db.commit()

        # Step 4: verify via the CLI (README §4). Flag 2 Rule B:
        # asyncio.to_thread isolates the CLI's `asyncio.run` from the
        # test's running loop.
        result = await asyncio.to_thread(
            runner.invoke, app, ["verify", str(session_id)]
        )
        assert result.exit_code == 0, (
            f"verify failed (exit={result.exit_code}):\n{result.output}"
        )
        # The exact terminal output a Show HN reader will see. Locking
        # these three substrings means a README screenshot stays accurate
        # across SDK refactors.
        assert "VALID" in result.output
        assert "3" in result.output  # record count
        assert "✓" in result.output

        # Step 5: belt-and-braces — also verify the chain mathematically
        # via the underlying CRUD. If the CLI lies about VALID for some
        # reason, this catches it.
        chain_result = await action_crud.verify_chain(
            clean_db, session_id=session_id
        )
        assert chain_result["valid"] is True
        assert chain_result["record_count"] == 3
