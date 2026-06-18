# LangGraph invoice agent

A minimal but fully runnable LangGraph ReAct agent with three instrumented tools:

- `send_invoice(customer_id, amount)`
- `log_payment(transaction_id, amount)`
- `notify_customer(customer_id, message)`

Every tool call lands on the RootSign hash chain. After the agent finishes, run `rootsign verify <session-id>` and you should see `VALID ✓ — 3 records, chain intact`.

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
DATABASE_URL=$ROOTSIGN_DATABASE_URL rootsign-admin init
```

(`ROOTSIGN_DATABASE_URL` is read from `.env`; the wrapper above just hands it to `rootsign-admin` for the migration step.)

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

`rootsign.wrap_tools(...)` wraps each `BaseTool` so that every `tool.ainvoke(...)` call:

1. Hashes the input payload
2. Runs the tool
3. Hashes the output
4. Emits an `ACTION_RECORD` envelope with `prev_action_hash` linking it to the previous action in the session

The agent code itself doesn't know any of this is happening — it just calls tools as normal LangGraph tools. The tracer is the only thing between the agent and the wrapped callable.
