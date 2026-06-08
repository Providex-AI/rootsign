# RootSign

**Tamper-evident provenance logging for AI agents.**

[![CI](https://github.com/Providex-AI/rootsign/actions/workflows/ci.yml/badge.svg)](https://github.com/Providex-AI/rootsign/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)
[![PyPI - coming soon](https://img.shields.io/badge/PyPI-coming_soon-lightgrey.svg)](https://pypi.org/project/rootsign/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

> *RootSign is a Providex AI product — the agent capture layer of the Providex AI Agent Accountability Platform.*

## What is RootSign?

When AI agents take actions in production — calling tools, hitting APIs, writing to databases — there is no built-in audit trail. If something goes wrong (a wrong refund, a leaked PII record, a malformed deployment), there is no way to prove what the agent did, in what order, on whose authorization, or whether the record has been tampered with after the fact.

RootSign solves this. Each agent action is captured as an `Action` record containing a SHA-256 hash of the previous action — a **cryptographic hash chain** that makes the record tamper-evident. Modify any record after the fact and `rootsign verify` detects it.

Compliance-grade audit trails. Zero changes to your agent code.

## Status

**Phase 0 — pre-MVP.** Canonical data model, storage layer, and ingest spec are complete. The user-facing `@rootsign.trace` decorator ships in Phase 1 Sprint 2.

| Phase | Scope | Status |
|---|---|---|
| 0 | Data model + storage + ingest handler | ✅ Complete |
| 1 | Python SDK (`@rootsign.trace`, LangGraph integration, CLI) | 🚧 Sprint 2 (LangGraph) complete; `rootsign verify` CLI in Sprint 3 |
| 2 | Hosted ingest backend + compliance dashboard | Planned |
| 3 | Policy enforcement + incident workflow | Planned |
| 4 | Cross-platform governance | Planned |

## Quickstart — LangGraph

### 1. Install

```bash
pip install rootsign[langgraph]
```

Start PostgreSQL + TimescaleDB locally and apply the schema:

```bash
docker-compose up -d db
rootsign-admin init       # alembic upgrade head
```

### 2. Register your agent (one-time setup)

```python
import asyncio
from rootsign import register_agent, AgentEnvironment, AgentRiskTier, AgentFramework

agent = asyncio.run(register_agent(
    name="my-invoice-agent",
    owner="platform-team",
    environment=AgentEnvironment.PRODUCTION,
    risk_tier=AgentRiskTier.HIGH,
    framework=AgentFramework.LANGGRAPH,
))
print(agent.agent_id)
```

### 3. Instrument your tools

```python
import rootsign
from rootsign import LocalIngestClient
from rootsign.database import AsyncSessionLocal
from langchain_core.tools import tool
from langgraph.prebuilt import ToolNode

# Your existing tools — no changes needed.
@tool
def send_invoice(customer_id: str, amount: float) -> str:
    """Send an invoice to a customer."""
    return "sent"

async def run_graph(agent_id):
    async with AsyncSessionLocal() as db:
        client = LocalIngestClient(db=db)
        async with rootsign.session(agent_id=agent_id, client=client) as ctx:
            tools = rootsign.wrap_tools([send_invoice], ctx=ctx, client=client)
            tool_node = ToolNode(tools)
            # ... build and run your graph as normal
        await db.commit()
```

That's it. Every tool call now produces a tamper-evident `Action` record on the hash chain.

### 4. Verify the chain

```python
from rootsign.crud import action as action_crud
result = await action_crud.verify_chain(db, session_id=session_id)
assert result["valid"] is True
```

> **Coming soon (Sprint 3):** a `rootsign verify <session_id>` CLI that prints the same result with a single shell command.

See [docs/framework-support.md](docs/framework-support.md) for the LangGraph version matrix and integration notes.

## Contributing

We welcome contributions. See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, coding standards, and the PR process. By submitting a contribution, you agree to the [CLA](CLA.md).

Open-source community channels and Discord coming soon.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

## Security

To report a vulnerability, see [SECURITY.md](SECURITY.md). Do **not** open a public GitHub issue.
