"""LangGraph + RootSign invoice agent.

A ReAct loop with three instrumented tools. Every tool call lands on the
RootSign hash chain. Run, then `./verify.sh` to confirm the chain.

Reads OPENAI_API_KEY and DATABASE_URL[_SYNC] from .env. See .env.example for
the expected shape. This example runs against Postgres/TimescaleDB
(`ROOTSIGN_BACKEND=postgres`) because that's the production shape; the same
code runs on the default JSONL backend with no other change — see
`examples/quickstart-jsonl/`.

The RootSign surface here is three calls: `init()`, `session()`, and
`wrap_tools()`. The explicit form — building a `SessionContext` and an ingest
client yourself — is still public and is what tests and multi-agent processes
use; see the comment block at the bottom of this file.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

import rootsign

load_dotenv()

LAST_SESSION_FILE = Path(".last_session")

# One call, once, at startup. No I/O happens here — the agent record is
# get-or-created on the first `rootsign.session()` entry, keyed on
# (name, environment), so re-running this script never re-registers.
rootsign.init(
    agent="langgraph-invoice-agent-example",
    owner="examples",
    environment="production",
    risk_tier="medium",
    framework="langgraph",
)


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


async def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY not set — copy .env.example to .env and fill it in")

    async with rootsign.session(objective="invoice acme and confirm payment") as ctx:
        # No ctx= / client= — the tools resolve the ambient session per call.
        tools = rootsign.wrap_tools([send_invoice, log_payment, notify_customer])

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

        result = await graph.ainvoke({"messages": [HumanMessage(content=user_request)]})

        final = result["messages"][-1].content
        print(f"agent: {final}\n")

    LAST_SESSION_FILE.write_text(str(ctx.session_id))
    print(f"session: {ctx.session_id}")
    print(f"actions emitted: {ctx.current_sequence}")
    print("\nrun ./verify.sh to confirm the chain is intact.")


# --------------------------------------------------------------------------
# Advanced: the explicit API this example used before the facade landed.
# Still public, still tested — use it when one process drives several agents,
# or when you need to own the DB session / transaction yourself.
#
#     from rootsign import LocalIngestClient, register_agent
#     from rootsign.database import AsyncSessionLocal
#
#     agent = await register_agent(name=..., owner=..., environment=...,
#                                  risk_tier=..., framework=...)
#
#     async with AsyncSessionLocal() as db:
#         client = LocalIngestClient(db=db)
#         async with rootsign.session(agent_id=agent.agent_id, client=client) as ctx:
#             tools = rootsign.wrap_tools([...], ctx=ctx, client=client)
#             ...
#         await db.commit()   # the caller owns the commit on this path
# --------------------------------------------------------------------------


if __name__ == "__main__":
    asyncio.run(main())
