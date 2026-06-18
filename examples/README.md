# RootSign examples

Self-contained, runnable apps demonstrating `@rootsign.trace` end-to-end.

| Example | Framework | What it does |
|---|---|---|
| [`langgraph-invoice-agent`](langgraph-invoice-agent/) | LangGraph + OpenAI | A ReAct agent with three tools (`send_invoice`, `log_payment`, `notify_customer`). Every tool call lands on the hash chain. Verify with `rootsign verify <session-id>`. |

More examples (CrewAI, AutoGen, raw `@rootsign.trace`) coming as the SDK lands those integrations. PRs welcome.

## Prerequisites for any example

- **Docker** — for the shared TimescaleDB container.
- **Python 3.11 or 3.12** — CrewAI lags on 3.13 / 3.14 wheels.
- **An OpenAI API key** — set `OPENAI_API_KEY` in `.env`. Examples that don't need an LLM say so in their own README.

## Shared TimescaleDB

Every example points at the same local TimescaleDB instance, so you can run them back-to-back without juggling containers:

```bash
cd examples
docker-compose up -d db
```

The container name is `rootsign-examples-db`, port `5433` (kept distinct from the main repo's dev DB on 5432 so they don't collide). Stop it with `docker-compose down`.

## Running an example

Each example has its own `README.md` with the exact steps, but the shape is always:

```bash
cd examples/<example-name>
cp .env.example .env             # edit OPENAI_API_KEY
pip install -e .                  # pulls rootsign + framework deps
rootsign-admin init               # migrates the schema once
python agent.py                   # runs the demo workflow
./verify.sh                       # rootsign verify the just-emitted session
```

You should see `VALID ✓  —  N records, chain intact` on the last line.
