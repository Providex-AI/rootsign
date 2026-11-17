"""LangGraph + RootSign with no database and no API key.

    pip install 'rootsign[langgraph]'
    python agent.py

A real LangGraph graph: a model node decides which tools to call, LangGraph's
own `ToolNode` runs them, and `tools_condition` routes back to the model until
it stops asking for tools. The only difference from a production agent is the
model node. It is scripted, so the example runs offline and gives the same
result every time. Swap in a real chat model and nothing on the RootSign side
changes; see the README.

The RootSign part is three calls: `init()` once at startup, a `session()`
around the run, and `wrap_tools()` on the tool list you hand to `ToolNode`.
Records land in `~/.rootsign/sessions/<session_id>.jsonl` (ADR-011).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

import rootsign

rootsign.init(agent="langgraph-invoice-agent", risk_tier="high", framework="langgraph")


@tool
async def send_invoice(customer_id: str, amount: float) -> str:
    """Send an invoice to a customer."""
    return f"invoice sent to {customer_id} for ${amount:,.2f}"


@tool
async def log_payment(customer_id: str, amount: float) -> str:
    """Record a payment in the ledger."""
    return f"payment of ${amount:,.2f} recorded for {customer_id}"


def scripted_model(state: MessagesState) -> dict:
    """Stands in for an LLM: ask for both tools, then answer once they ran."""
    if not any(isinstance(m, ToolMessage) for m in state["messages"]):
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "send_invoice",
                            "args": {"customer_id": "acme-corp", "amount": 1500.0},
                            "id": "call_1",
                        },
                        {
                            "name": "log_payment",
                            "args": {"customer_id": "acme-corp", "amount": 1500.0},
                            "id": "call_2",
                        },
                    ],
                )
            ]
        }
    return {"messages": [AIMessage(content="Invoice sent and payment recorded for acme-corp.")]}


def build_graph(tools: list):
    graph = StateGraph(MessagesState)
    graph.add_node("model", scripted_model)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", tools_condition)
    graph.add_edge("tools", "model")
    return graph.compile()


async def main() -> None:
    async with rootsign.session(objective="invoice acme-corp and record payment") as ctx:
        graph = build_graph(rootsign.wrap_tools([send_invoice, log_payment]))
        result = await graph.ainvoke(
            {
                "messages": [
                    HumanMessage(content="Invoice acme-corp for $1,500 and log the payment.")
                ]
            }
        )
        for message in result["messages"]:
            if isinstance(message, ToolMessage):
                print(f"tool:  {message.content}")
        print(f"agent: {result['messages'][-1].content}")

    data_dir = Path(os.environ.get("ROOTSIGN_DATA_DIR", "~/.rootsign")).expanduser()
    target = data_dir / "sessions" / f"{ctx.session_id}.jsonl"
    print(f"\n{ctx.current_sequence} actions recorded → {target}")
    print("\nVerify the chain:")
    print(f"    rootsign verify --local {target}")


if __name__ == "__main__":
    asyncio.run(main())
