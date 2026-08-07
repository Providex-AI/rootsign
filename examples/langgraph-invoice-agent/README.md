# LangGraph invoice agent

A minimal but fully runnable LangGraph ReAct agent with three instrumented tools:

- `send_invoice(customer_id, amount)`
- `log_payment(transaction_id, amount)`
- `notify_customer(customer_id, message)`

Every tool call lands on the RootSign hash chain. After the agent finishes, run `rootsign verify <session-id>` and you should see `VALID ✓ — 3 records, chain intact`.

This example runs against **Postgres/TimescaleDB** because that's the production shape — hence the Docker step below. If you just want to see RootSign work, [`../quickstart-jsonl`](../quickstart-jsonl/) needs no database at all. The agent code is identical either way; only `ROOTSIGN_BACKEND` differs.

## Setup

```bash
cd examples/langgraph-invoice-agent
cp .env.example .env             # then edit OPENAI_API_KEY
pip install -e .                  # rootsign + langgraph + langchain-openai
```

Then bring up the shared TimescaleDB and migrate the schema once:

```bash
cd ..
docker-compose up -d db
cd langgraph-invoice-agent
set -a && source .env && set +a       # export DATABASE_URL[_SYNC] for rootsign-admin
rootsign-admin init
```

(`rootsign-admin` reads `DATABASE_URL` / `DATABASE_URL_SYNC` directly — no `ROOTSIGN_` prefix. The `set -a` trick exports every variable in `.env` to the subprocess so the admin CLI sees them.)

## Run it

```bash
python agent.py
```

You'll see the agent loop: model proposes a tool call → RootSign records it → result feeds back → next call. At the end:

```
Session: 7e1c2a3b-...
Actions emitted: 3
Run `rootsign verify 7e1c2a3b-...` to confirm the chain.
```

## Verify the chain

```bash
./verify.sh
```

`verify.sh` reads the last session ID `agent.py` wrote to `.last_session` and runs `rootsign verify` on it. Expected output:

```
VALID ✓  —  3 records, chain intact
  Session:  7e1c2a3b-...
```

## What's happening under the hood

The RootSign surface in `agent.py` is three calls:

```python
rootsign.init(agent="langgraph-invoice-agent-example", ...)   # once, at import

async with rootsign.session(objective="...") as ctx:
    tools = rootsign.wrap_tools([send_invoice, log_payment, notify_customer])
```

`init()` does no I/O — the agent record is get-or-created on the first `session()` entry, keyed on `(name, environment)`, so re-running the script never re-registers (that's why there's no `.agent_id` cache file any more). `wrap_tools()` needs no `ctx=`/`client=`: the session publishes them and each tool call resolves them. The explicit form is still public — see the comment block at the bottom of `agent.py`.

`rootsign.wrap_tools(...)` wraps each `BaseTool` so that every `tool.ainvoke(...)` call:

1. Hashes the input payload
2. Runs the tool
3. Hashes the output
4. Emits an `ACTION_RECORD` envelope with `prev_action_hash` linking it to the previous action in the session

The agent code itself doesn't know any of this is happening — it just calls tools as normal LangGraph tools. The tracer is the only thing between the agent and the wrapped callable.
