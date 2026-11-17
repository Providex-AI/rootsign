"""CrewAI + RootSign with no database and no API key.

    pip install 'rootsign[crewai]'
    python crew.py

A real CrewAI crew: an Agent with two tools, a Task, and `Crew.kickoff_async()`
running CrewAI's own agent loop, which is where RootSign's interception
happens. The only difference from a production crew is the LLM. It is a
scripted `BaseLLM` that asks for `send_invoice`, then `log_payment`, then
gives a final answer, so the example runs offline and gives the same result
every time. Swap in a real LLM and nothing on the RootSign side changes; see
the README.

The RootSign part is three calls: `init()` once at startup, a `session()`
around the run, and `wrap_crewai_tools()` on the tool list you give the
Agent. Records land in `~/.rootsign/sessions/<session_id>.jsonl` (ADR-011).

Needs CrewAI 0.120 or later, the first release that exports `BaseLLM`.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from crewai import Agent, BaseLLM, Crew, Task
from crewai.tools import tool

import rootsign

rootsign.init(agent="crewai-invoice-crew", risk_tier="high", framework="crewai")


@tool("send_invoice")
def send_invoice(customer_id: str, amount: float) -> str:
    """Send an invoice to a customer."""
    return f"invoice sent to {customer_id} for ${amount:,.2f}"


@tool("log_payment")
def log_payment(customer_id: str, amount: float) -> str:
    """Record a payment in the ledger."""
    return f"payment of ${amount:,.2f} recorded for {customer_id}"


# One reply per LLM call: a tool, another tool, then the final answer.
SCRIPT = [
    "Thought: I need to send the invoice first.\n"
    "Action: send_invoice\n"
    'Action Input: {"customer_id": "acme-corp", "amount": 1500.0}',
    "Thought: Now I record the payment.\n"
    "Action: log_payment\n"
    'Action Input: {"customer_id": "acme-corp", "amount": 1500.0}',
    "Thought: Both steps are done.\nFinal Answer: Invoice sent and payment recorded for acme-corp.",
]
_replies = iter(SCRIPT)


class ScriptedLLM(BaseLLM):
    """Stands in for a real LLM by replaying SCRIPT, one reply per call."""

    def call(self, messages, *args, **kwargs) -> str:
        return next(_replies, SCRIPT[-1])

    def supports_function_calling(self) -> bool:
        return False

    def supports_stop_words(self) -> bool:
        return False

    def get_context_window_size(self) -> int:
        return 8192


async def main() -> None:
    async with rootsign.session(objective="invoice acme-corp and record payment") as ctx:
        billing = Agent(
            role="Billing clerk",
            goal="Invoice customers and record their payments",
            backstory="You handle accounts receivable.",
            tools=rootsign.wrap_crewai_tools([send_invoice, log_payment]),
            llm=ScriptedLLM(model="scripted"),
        )
        task = Task(
            description="Invoice acme-corp for $1,500, then record the payment.",
            expected_output="A one-line confirmation.",
            agent=billing,
        )
        result = await Crew(agents=[billing], tasks=[task]).kickoff_async()
        print(f"crew: {result}")

    data_dir = Path(os.environ.get("ROOTSIGN_DATA_DIR", "~/.rootsign")).expanduser()
    target = data_dir / "sessions" / f"{ctx.session_id}.jsonl"
    print(f"\n{ctx.current_sequence} actions recorded → {target}")
    print("\nVerify the chain:")
    print(f"    rootsign verify --local {target}")


if __name__ == "__main__":
    asyncio.run(main())
