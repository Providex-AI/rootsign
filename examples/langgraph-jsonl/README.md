# LangGraph, no database, no API key

A LangGraph agent whose tool calls land on the RootSign hash chain, with
nothing to set up. Records go to a local JSONL file and `rootsign verify
--local` checks them.

```bash
pip install 'rootsign[langgraph]'
python agent.py
```

```text
tool:  invoice sent to acme-corp for $1,500.00
tool:  payment of $1,500.00 recorded for acme-corp
agent: Invoice sent and payment recorded for acme-corp.

2 actions recorded → ~/.rootsign/sessions/<session_id>.jsonl

Verify the chain:
    rootsign verify --local ~/.rootsign/sessions/<session_id>.jsonl
```

Run the command it prints:

```text
VALID ✓  —  2 records, chain intact
```

## What is real and what is scripted

The graph is a normal LangGraph graph: a `model` node, LangGraph's own
`ToolNode`, and `tools_condition` routing between them. `ToolNode` is where
RootSign's interception happens, so this exercises the same path as a
production agent.

The `model` node is scripted. It asks for `send_invoice` and `log_payment`,
then answers once both have run. That keeps the example offline and
deterministic. To use a real model, replace `scripted_model` with a node that
calls a chat model bound to the same tools. The RootSign lines (`init()`,
`session()`, `wrap_tools()`) do not change. For an example with a real OpenAI
model on PostgreSQL, see [`langgraph-invoice-agent`](../langgraph-invoice-agent/).

## Tested versions

CI runs this example against every LangGraph line in the contract matrix:
0.2.x, 0.6.x and 1.x (`tests/contract/langgraph/test_example_jsonl.py`).
