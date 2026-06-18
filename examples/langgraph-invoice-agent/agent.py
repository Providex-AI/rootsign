"""LangGraph + RootSign invoice agent.

A ReAct loop with three instrumented tools. Every tool call lands on the
RootSign hash chain. Run, then `./verify.sh` to confirm the chain.

Reads OPENAI_API_KEY and ROOTSIGN_DATABASE_URL[_SYNC] from .env. See
.env.example for the expected shape.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import UUID

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
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

load_dotenv()

AGENT_NAME = "langgraph-invoice-agent-example"
AGENT_ID_CACHE = Path(".agent_id")
LAST_SESSION_FILE = Path(".last_session")


@tool
async def send_invoice(customer_id: str, amount: float) -> str:
    """Send an invoice to a customer. Returns a confirmation string."""
    return f"invoice sent: {customer_id} owes {amount:.2f}"


@tool
async def log_payment(transaction_id: str, amount: float) -> str:
    """Log a payment to the accounting ledger. Returns the ledger entry id."""
    return f"payment logged: tx={transaction_id} amount={amount:.2f}"


@tool
async def notify_customer(customer_id: str, message: str) -> str:
    """Send a notification message to a customer."""
    return f"notified {customer_id}: {message!r}"


async def _ensure_agent() -> UUID:
    """Get an agent_id for this example, idempotently.

    Order of resolution:
      1. The cached `.agent_id` file (fastest path on re-runs).
      2. A row in the DB with `name == AGENT_NAME` (handles the case where
         `.agent_id` was deleted but the row from a prior run is still
         around — `agents.name` has a UNIQUE constraint, so a blind
         re-register would crash).
      3. Otherwise register fresh.
    """
    if AGENT_ID_CACHE.exists():
        return UUID(AGENT_ID_CACHE.read_text().strip())

    async with AsyncSessionLocal() as db:
        existing = (
            await db.execute(select(Agent).where(Agent.name == AGENT_NAME))
        ).scalar_one_or_none()
        if existing is not None:
            AGENT_ID_CACHE.write_text(str(existing.agent_id))
            return existing.agent_id

    agent = await register_agent(
        name=AGENT_NAME,
        owner="examples",
        environment=AgentEnvironment.PRODUCTION,
        risk_tier=AgentRiskTier.MEDIUM,
        framework=AgentFramework.LANGGRAPH,
    )
    AGENT_ID_CACHE.write_text(str(agent.agent_id))
    print(f"registered agent: {agent.agent_id}")
    return agent.agent_id


async def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY not set — copy .env.example to .env and fill it in")

    agent_id = await _ensure_agent()

    async with AsyncSessionLocal() as db:
        client = LocalIngestClient(db=db)

        async with rootsign.session(agent_id=agent_id, client=client) as ctx:
            tools = rootsign.wrap_tools(
                [send_invoice, log_payment, notify_customer],
                ctx=ctx,
                client=client,
            )

            graph = create_react_agent(
                ChatOpenAI(model="gpt-4o-mini", temperature=0),
                tools=tools,
            )

            user_request = (
                "Customer acme owes 1500.00. Send the invoice, log payment "
                "tx_001 for 1500.00 once it lands, then notify acme that the "
                "invoice was sent. Use the three tools in that order. Don't "
                "ask follow-up questions."
            )

            print(f"\n--- agent run, session {ctx.session_id} ---\n")
            print(f"user: {user_request}\n")

            result = await graph.ainvoke(
                {"messages": [HumanMessage(content=user_request)]}
            )

            final = result["messages"][-1].content
            print(f"agent: {final}\n")

        await db.commit()

    LAST_SESSION_FILE.write_text(str(ctx.session_id))
    print(f"session: {ctx.session_id}")
    print(f"actions emitted: {ctx.current_sequence}")
    print("\nrun ./verify.sh to confirm the chain is intact.")


if __name__ == "__main__":
    asyncio.run(main())
