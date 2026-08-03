"""Self-contained happy-path demo, used to record docs/demo.gif via vhs.

No LLM, no API key — just the SDK mechanics:
  1. register agent (idempotent)
  2. open session
  3. call three @tool-decorated functions through rootsign.wrap_tools
  4. close session
  5. write the session UUID to /tmp/rs_demo_session for the next tape step

The output is intentionally terse and friendly so the GIF reads cleanly.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import UUID

from langchain_core.tools import tool
from sqlalchemy import select

import rootsign
from rootsign import (
    AgentEnvironment,
    AgentFramework,
    AgentRiskTier,
    LocalIngestClient,
    register_agent,
)
from rootsign.database import AsyncSessionLocal
from rootsign.models.agent import Agent

AGENT_NAME = "demo-invoice-agent"
SESSION_FILE = Path("/tmp/rs_demo_session")


@tool
async def send_invoice(customer_id: str, amount: float) -> str:
    """Send an invoice."""
    return f"sent: {customer_id} owes {amount:.2f}"


@tool
async def log_payment(transaction_id: str, amount: float) -> str:
    """Log a payment."""
    return f"logged: tx={transaction_id} amount={amount:.2f}"


@tool
async def notify_customer(customer_id: str, message: str) -> str:
    """Notify a customer."""
    return f"notified: {customer_id}"


async def _resolve_agent_id() -> UUID:
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(select(Agent).where(Agent.name == AGENT_NAME))
        ).scalar_one_or_none()
        if row is not None:
            return row.agent_id
    agent = await register_agent(
        name=AGENT_NAME,
        owner="demo",
        environment=AgentEnvironment.PRODUCTION,
        risk_tier=AgentRiskTier.MEDIUM,
        framework=AgentFramework.LANGGRAPH,
    )
    return agent.agent_id


async def main() -> None:
    agent_id = await _resolve_agent_id()

    async with AsyncSessionLocal() as db:
        client = LocalIngestClient(db=db)

        async with rootsign.session(agent_id=agent_id, client=client) as ctx:
            tools = rootsign.wrap_tools(
                [send_invoice, log_payment, notify_customer],
                ctx=ctx,
                client=client,
            )

            print(f"session opened: {ctx.session_id}")

            await tools[0].ainvoke({"customer_id": "acme", "amount": 1500.00})
            print("  action 1: send_invoice    → recorded")

            await tools[1].ainvoke({"transaction_id": "tx_001", "amount": 1500.00})
            print("  action 2: log_payment     → recorded")

            await tools[2].ainvoke({"customer_id": "acme", "message": "Invoice sent"})
            print("  action 3: notify_customer → recorded")

        await db.commit()

    SESSION_FILE.write_text(str(ctx.session_id))
    print("session closed. 3 actions on the hash chain.")
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
