<p align="center">
  <img src="https://raw.githubusercontent.com/Providex-AI/rootsign/main/docs/rootsign-logo.png" alt="RootSign" width="220" />
</p>

# RootSign

**Tamper-evident provenance logging for AI agents.**

[![CI](https://github.com/Providex-AI/rootsign/actions/workflows/ci.yml/badge.svg)](https://github.com/Providex-AI/rootsign/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)
[![PyPI](https://img.shields.io/pypi/v/rootsign.svg)](https://pypi.org/project/rootsign/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![X](https://img.shields.io/badge/X-@getprovidex-black?logo=x)](https://x.com/getprovidex)
[![LinkedIn](https://img.shields.io/badge/LinkedIn-Providex-0A66C2?logo=linkedin)](https://www.linkedin.com/company/providex)

> *RootSign is a Providex AI product — the agent capture layer of the Providex AI Agent Accountability Platform.*

## What is RootSign?

When AI agents take actions in production — calling tools, hitting APIs, writing to databases — there is no built-in audit trail. If something goes wrong (a wrong refund, a leaked PII record, a malformed deployment), there is no way to prove what the agent did, in what order, on whose authorization, or whether the record has been tampered with after the fact.

RootSign solves this. Each agent action is captured as an `Action` record containing a SHA-256 hash of the previous action — a **cryptographic hash chain** that makes the record tamper-evident. Modify any record after the fact and `rootsign verify` detects it.

Compliance-grade audit trails. Zero changes to your agent code.

## Status

**Phase 1 MVP — v0.1.1.** LangGraph + CrewAI integrations, `rootsign verify` CLI, PII redaction, human-in-the-loop checkpoints, and opt-in decision capture (PRD-19 / ADR-008) are all shipping.

| Phase | Scope | Status |
|---|---|---|
| 0 | Data model + storage + ingest handler | ✅ Complete |
| 1 | Python SDK — `@rootsign.trace`, LangGraph + CrewAI, `rootsign verify` CLI, redaction, HiTL checkpoint, opt-in decision capture | ✅ v0.1.1 |
| 2 | Hosted ingest backend + compliance dashboard | Planned |
| 3 | Policy enforcement + incident workflow | Planned |
| 4 | Cross-platform governance | Planned |

## Quickstart — LangGraph

### 1. Install

> **Python 3.11 or 3.12 recommended.** RootSign itself supports 3.11+, but the `[crewai]` extra currently lags on 3.13/3.14 wheels. If you hit `No matching distribution found for crewai`, switch to Python 3.12 and reinstall.

```bash
pip install rootsign[langgraph]
```

Start PostgreSQL + TimescaleDB locally and apply the schema:

```bash
rootsign-admin start-db   # docker run timescale/timescaledb:latest-pg16
rootsign-admin init       # alembic upgrade head
```

`start-db` wraps a single `docker run` so you don't need to clone the repo. If you *have* cloned it, `docker-compose up -d db` is the equivalent developer path. Both reuse the same `rootsign-timescaledb` container name and `rootsign_pgdata` volume — pick either, not both.

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
            # ...build and run your graph as normal
        await db.commit()
```

Every tool call now produces a tamper-evident `Action` record on the hash chain.

### 4. Verify the chain

```bash
$ rootsign verify 660e8400-e29b-41d4-a716-446655440001
VALID ✓  —  3 records, chain intact
  Session:  660e8400-e29b-41d4-a716-446655440001
```

Exit code is `0` for VALID, `1` for TAMPERED. Use `--local <path.jsonl>` for offline JSONL session files (no DB required).

If a record was modified, the verifier names the broken link:

```bash
$ rootsign verify 660e8400-e29b-41d4-a716-446655440001
TAMPERED ✗  —  chain broken at record #2
  Detail:   self_hash mismatch on action <action_id>
  Session:  660e8400-e29b-41d4-a716-446655440001
WARNING: This session log may have been tampered with.
```

See [docs/framework-support.md](docs/framework-support.md) for the version matrix and integration notes.

## Decision capture (opt-in)

Record the *why* before each tool call — foundational for Phase 2 session replay. Off by default; opt in deliberately with `ROOTSIGN_CAPTURE_DECISIONS=true`.

```python
import os
os.environ["ROOTSIGN_CAPTURE_DECISIONS"] = "true"

async with rootsign.session(agent_id=agent.agent_id, client=client) as ctx:
    # Record what the agent decided before calling the tool.
    await ctx.record_decision(
        selected_action="send_invoice",
        reasoning_summary="Amount within policy; recipient verified.",
        confidence=0.97,
        ingest_client=client,
    )
    tools = rootsign.wrap_tools([send_invoice], ctx=ctx, client=client)
    await tools[0].ainvoke({"customer_id": "acme", "amount": 1500.0})
    # The Action record now carries decision_id linking it to the reasoning above.
```

Depth controls how much reasoning is persisted, via `ROOTSIGN_REASONING_DEPTH`:

| Value | What's stored |
|---|---|
| `minimal` | `selected_action` + `confidence` only |
| `summary` (default) | + `reasoning_summary` truncated to 500 chars |
| `full` | + `reasoning_summary` truncated to 10,000 chars + `alternatives_considered` |

Calling `ctx.record_decision()` when the flag is off is a silent no-op — safe to ship in capture-on and capture-off environments without conditionals at the call site. One `Decision` links to one `Action`; the pending slot is single and cleared after the next tool call consumes it. Decisions are **not** in the hash chain (ADR-008) — `verify_chain` is unchanged.

## Quickstart — CrewAI

CrewAI integration is the same shape — wrap the tool list at construction time.

```bash
pip install rootsign[crewai]
```

```python
import rootsign
from crewai import Agent
from crewai.tools import tool

@tool("send_invoice")
def send_invoice(customer_id: str, amount: float) -> str:
    """Send an invoice to a customer."""
    return "sent"

async def run_crew(agent_id):
    async with AsyncSessionLocal() as db:
        client = LocalIngestClient(db=db)
        async with rootsign.session(agent_id=agent_id, client=client) as ctx:
            wrapped = rootsign.wrap_crewai_tools(
                [send_invoice], ctx=ctx, client=client
            )
            agent = Agent(
                role="Invoicing assistant",
                goal="Send invoices",
                tools=wrapped,
            )
            # ...run your crew as normal
        await db.commit()
```

Tested against CrewAI `0.28`, `0.40`, and `1.x` (see [CI matrix](https://github.com/Providex-AI/rootsign/actions/workflows/ci.yml)).

## Human-in-the-loop checkpoint

High-risk actions can be gated on a human decision. Pass `require_approval=True` to `@rootsign.trace` and the SDK blocks the tool from running until someone approves it via the CLI.

```python
import rootsign

@rootsign.trace(
    ingest_client=client,
    session_context=ctx,
    require_approval=True,
    timeout_seconds=300,   # 5 minutes
)
async def wire_transfer(account: str, amount: float) -> str:
    # This runs ONLY after a human approves.
    return execute_transfer(account, amount)
```

When `wire_transfer(...)` is called, the SDK inserts an `ACTION_RECORD` with `authorization_status='pending'` and waits. An operator approves (or rejects) from another terminal:

```bash
$ rootsign approve --list
Pending approvals (1):
  <action-id>  wire_transfer  session=<session-id>  submitted=<timestamp>

$ rootsign approve <action-id> --reason "Verified with customer"
✓  Action <action-id> approved.
```

The decorated function returns normally. Rejection (`--reject`) raises `HiTLRejectedError`; a 5-minute timeout raises `HiTLTimeoutError` and the action's authorization status becomes `'timed_out'` (a terminal forensic state distinct from `'human_rejected'`).

See [ADR-007](docs/adr/ADR-007-hitl-checkpoint-design.md) for the design rationale (poll loop, timeout semantics, race tolerance).

## PII redaction

`RedactionConfig` runs **before** hashing, so stored `input_hash` / `output_hash` values carry no PII signal. Three ready-to-use configs:

```python
from rootsign import StandardPIIConfig, FinancialPIIConfig, HealthcarePIIConfig

# Standard: email, phone, US SSN, credit card, UK NI number
redaction = StandardPIIConfig()

tools = rootsign.wrap_tools(
    [send_invoice], ctx=ctx, client=client,
    redaction_config=redaction,
)
```

`FinancialPIIConfig` adds account / routing / IBAN patterns; `HealthcarePIIConfig` adds MRN / NPI / DOB. Each accepts `extra_rules={...}` for domain-specific patterns without subclassing. See [ADR-006](docs/adr/ADR-006-redaction-contract.md).

## Architecture

* **`@rootsign.trace`** wraps a tool callable and emits an `ACTION_RECORD` envelope per call. LangGraph `BaseTool` and CrewAI tools are detected automatically.
* **`LocalIngestClient`** is the in-process ingest path for v0.1.x. A `HttpIngestClient` for the hosted backend lands in Phase 2.
* **Hash chain** is per-session: each `Action` carries `prev_action_hash` so reconstructing the chain detects any after-the-fact modification.
* **`HiTLCheckpoint`** is an async poll loop that opens its own DB session per cycle — see ADR-007 for the loop-binding rationale.
* **Storage** is PostgreSQL 16 + TimescaleDB 2.14. The `actions` table is a hypertable; the chain stays intact across chunks.

## What's next

* **Phase 2 cloud backend** — `HttpIngestClient` + hosted compliance dashboard. Drop-in replacement for `LocalIngestClient` once available.
* **Web UI for HiTL** — approve/reject pending actions from a browser instead of the CLI.
* **AutoGen integration** — same duck-typing shape as CrewAI.

Watch the [GitHub Issues](https://github.com/Providex-AI/rootsign/issues) for the active roadmap.

## Contributing

We welcome contributions. See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, coding standards, and the PR process. By submitting a contribution, you agree to the [CLA](CLA.md).

Open-source community channels and Discord coming soon — for now, GitHub Issues is the canonical place to file bugs and propose features.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

## Security

To report a vulnerability, see [SECURITY.md](SECURITY.md). Do **not** open a public GitHub issue.
