# RootSign

**Tamper-evident provenance logging for production AI agents.**

[![PyPI](https://img.shields.io/pypi/v/rootsign?cacheSeconds=300)](https://pypi.org/project/rootsign/)
[![Downloads](https://img.shields.io/pypi/dm/rootsign)](https://pypi.org/project/rootsign/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)
[![CI](https://github.com/Providex-AI/rootsign/actions/workflows/ci.yml/badge.svg)](https://github.com/Providex-AI/rootsign/actions/workflows/ci.yml)
[![Stars](https://img.shields.io/github/stars/Providex-AI/rootsign?style=flat&logo=github)](https://github.com/Providex-AI/rootsign/stargazers)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![X](https://img.shields.io/badge/X-@getprovidex-black?logo=x)](https://x.com/getprovidex)
[![LinkedIn](https://img.shields.io/badge/LinkedIn-Providex-0A66C2?logo=linkedin)](https://www.linkedin.com/company/providex)

<p align="center">
  <img src="https://raw.githubusercontent.com/Providex-AI/rootsign/main/docs/demo.gif" alt="RootSign demo — pip install with no database, three instrumented tool calls land on the hash chain, rootsign verify --local confirms VALID" width="780" />
</p>

## What is RootSign?

> *RootSign is a Providex AI product — the agent capture layer of the Providex AI Agent Accountability Platform.*

When AI agents take actions in production — calling tools, hitting APIs, writing to databases — there is no built-in audit trail. If something goes wrong (a wrong refund, a leaked PII record, a malformed deployment), there is no way to prove what the agent did, in what order, on whose authorization, or whether the record has been tampered with after the fact.

RootSign solves this. Each agent action is captured as an `Action` record containing a SHA-256 hash of the previous action — a **cryptographic hash chain** that makes the record tamper-evident. Modify any record after the fact and `rootsign verify` detects it.

Compliance-grade audit trails. Zero changes to your agent code.

## Status

**v0.3.0.** `pip install rootsign` → a verified hash chain in under a minute: no Docker, no database, no plumbing. LangGraph + CrewAI integrations, a framework-agnostic MCP proxy, `rootsign verify` CLI, PII redaction, human-in-the-loop checkpoints, opt-in decision capture (PRD-19 / ADR-008), and opt-in SDK micro-batching are all shipping — plus evidence bundles (`rootsign export`), the cloud transport with its offline spool, and a published wire spec.

| Phase | Scope | Status |
|---|---|---|
| 0 | Data model + storage + ingest handler | ✅ Complete |
| 1 | Python SDK — `@rootsign.trace`, LangGraph + CrewAI + MCP proxy, `rootsign verify` CLI, redaction, HiTL checkpoint, decision capture, micro-batching | ✅ v0.1.5 |
| 1.5 | Zero-dependency onboarding — JSONL default backend, `rootsign.init()` facade | ✅ v0.2.0 |
| 1.6 | Evidence bundles (`rootsign export`), cloud transport + offline spool, published ingest spec | ✅ v0.3.0 |
| 2 | Hosted ingest backend + compliance dashboard | Planned |
| 3 | Policy enforcement + incident workflow | Planned |
| 4 | Cross-platform governance | Planned |

## Quickstart

```bash
pip install rootsign
```

No extras, no database, no Docker. Three RootSign calls around your ordinary agent code:

```python
import asyncio, rootsign

rootsign.init(agent="invoice-agent", risk_tier="high")        # 1. once, at startup

@rootsign.trace()                                            # 2. per tool
async def send_invoice(customer_id: str, amount: float) -> str:
    return "sent"

@rootsign.trace()
async def log_payment(customer_id: str, amount: float) -> str:
    return "logged"

async def main():
    async with rootsign.session(objective="invoice ACME") as ctx:   # 3. per run
        await send_invoice("acme-corp", 1500.00)
        await log_payment("acme-corp", 1500.00)
    print(ctx.session_id)

asyncio.run(main())
```

Then verify the chain — the session lives in `~/.rootsign/sessions/<session_id>.jsonl`:

```bash
$ rootsign verify --local ~/.rootsign/sessions/f758a636-7bcd-4f96-8940-eff7d80e760a.jsonl
VALID ✓  —  2 records, chain intact
  Session:  f758a636-7bcd-4f96-8940-eff7d80e760a
```

Exit code is `0` for VALID, `1` for TAMPERED (a record was altered) and `2` for INCOMPLETE (a record is missing) — so this drops into CI or a cron audit, and a script can tell theft from loss. Change any character in any record and the verifier names the broken link:

```bash
$ rootsign verify --local ~/.rootsign/sessions/f758a636-7bcd-4f96-8940-eff7d80e760a.jsonl
TAMPERED ✗  —  chain broken at record #1
  Detail:   self_hash mismatch
  Session:  f758a636-7bcd-4f96-8940-eff7d80e760a
WARNING: This session log may have been tampered with.
```

When someone outside engineering needs to see it, turn the same session into a self-contained evidence bundle:

```bash
$ rootsign export --local ~/.rootsign/sessions/f758a636-7bcd-4f96-8940-eff7d80e760a.jsonl
VALID — 2 records, chain intact
  Bundle:   ./evidence-f758a636-7bcd-4f96-8940-eff7d80e760a
            redaction.json
            report.html
            report.md
            timeline.json
            verification.json
            manifest.json

  manifest.json SHA-256:  a1d61338d0f793ea03ac472863fadea5660e1e94846913ce7613d8c117d67cc5
  Record that hash outside the bundle — a ticket, an email, a chain-of-custody log.
  It is what proves a bundle you receive later is the one that was generated.
```

Open `report.html` in any browser — [see below](#evidence-bundles).

That's the whole surface: **`init()` → `session()` → `@trace` / `wrap_tools()` → `verify` → `export`.** `init()` is synchronous and does no I/O, so it's safe at module scope and inside a running event loop (notebooks, FastAPI startup); the agent record is get-or-created on the first `session()` entry, keyed on `(name, environment)`. Re-running your script never re-registers.

A runnable version of the above is [`examples/quickstart-jsonl`](examples/quickstart-jsonl/).

### Framework integrations

The three RootSign calls don't change — you only swap in the wrapper for your framework's tool list.

```bash
pip install rootsign[langgraph]     # or [crewai], or [mcp]
```

```python
import rootsign
from langchain_core.tools import tool
from langgraph.prebuilt import ToolNode

@tool
def send_invoice(customer_id: str, amount: float) -> str:
    """Send an invoice to a customer."""
    return "sent"

rootsign.init(agent="invoice-agent", risk_tier="high", framework="langgraph")

async def run_graph():
    async with rootsign.session(objective="invoice ACME") as ctx:
        tool_node = ToolNode(rootsign.wrap_tools([send_invoice]))
        # ...build and run your graph as normal
```

> **Python 3.11 or 3.12 recommended.** RootSign itself supports 3.11+, but the `[crewai]` extra currently lags on 3.13/3.14 wheels. If you hit `No matching distribution found for crewai`, switch to Python 3.12 and reinstall.

See [docs/framework-support.md](docs/framework-support.md) for the version matrix and integration notes. A full runnable LangGraph example (ReAct agent, three instrumented tools, OpenAI-backed) lives in [`examples/langgraph-invoice-agent`](examples/langgraph-invoice-agent/).

## Production backend (PostgreSQL / TimescaleDB)

The default JSONL backend is a single-process, append-only writer — right for local development, evaluation, and single-process jobs. Switch to Postgres when you outgrow it:

| Switch to `postgres` when you need | Why JSONL can't |
|---|---|
| **Multiple writer processes** on one audit trail | No cross-process file locking (ADR-011) — concurrent writers are out of contract |
| **Cross-process human-in-the-loop** — `rootsign approve` from another terminal, or a web UI | Needs a shared store the poll loop can read; JSONL HiTL is an inline TTY prompt only |
| **Queries across sessions** — "every action this agent took last week" | JSONL is one file per session, no index |
| The **Phase 2 hosted dashboard** | Reads from the store, not from laptops |

```bash
pip install 'rootsign[postgres]'
rootsign-admin start-db   # docker run timescale/timescaledb:latest-pg16
rootsign-admin init       # alembic upgrade head
export ROOTSIGN_BACKEND=postgres
```

`start-db` wraps a single `docker run` so you don't need to clone the repo. If you *have* cloned it, `docker-compose up -d db` is the equivalent developer path. Both reuse the same `rootsign-timescaledb` container name and `rootsign_pgdata` volume — pick either, not both.

**Your application code does not change.** The same `init()` / `session()` / `wrap_tools()` above now writes to Postgres, and sessions are verified by id instead of by path:

```bash
$ rootsign verify 660e8400-e29b-41d4-a716-446655440001
VALID ✓  —  3 records, chain intact
  Session:  660e8400-e29b-41d4-a716-446655440001
```

### Advanced: the explicit API

`init()` is a convenience over the real seams, which stay public, tested, and documented. Use them when one process drives several agents, or when you want to own the DB session and its transaction:

```python
import rootsign
from rootsign import LocalIngestClient, register_agent
from rootsign.database import AsyncSessionLocal

agent = await register_agent(
    name="my-invoice-agent", owner="platform-team",
    environment="production", risk_tier="high", framework="langgraph",
)

async with AsyncSessionLocal() as db:
    client = LocalIngestClient(db=db)
    async with rootsign.session(agent_id=agent.agent_id, client=client) as ctx:
        tools = rootsign.wrap_tools([send_invoice], ctx=ctx, client=client)
        # ...run your graph
    await db.commit()          # the caller owns the commit on this path
```

**Explicit arguments always win over the ambient session** — mixing the two is safe, and passing `ctx=`/`client=` never consults the implicit context. See [ADR-012](docs/adr/ADR-012-init-facade-contextvar-session.md).

## Decision capture (opt-in)

Record the *why* before each tool call — foundational for Phase 2 session replay. Off by default; opt in deliberately with `ROOTSIGN_CAPTURE_DECISIONS=true`.

```python
import os
os.environ["ROOTSIGN_CAPTURE_DECISIONS"] = "true"

async with rootsign.session(objective="invoice ACME") as ctx:
    # Record what the agent decided before calling the tool.
    await ctx.record_decision(
        selected_action="send_invoice",
        reasoning_summary="Amount within policy; recipient verified.",
        confidence=0.97,
    )
    tools = rootsign.wrap_tools([send_invoice])
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

rootsign.init(agent="invoice-crew", risk_tier="high", framework="crewai")

async def run_crew():
    async with rootsign.session(objective="send invoices") as ctx:
        agent = Agent(
            role="Invoicing assistant",
            goal="Send invoices",
            tools=rootsign.wrap_crewai_tools([send_invoice]),
        )
        # ...run your crew as normal
```

Tested against CrewAI `0.28`, `0.40`, and `1.x` (see [CI matrix](https://github.com/Providex-AI/rootsign/actions/workflows/ci.yml)).

## Quickstart — MCP proxy (any framework)

Instead of a per-framework adapter, RootSign can intercept at the [Model Context Protocol](https://modelcontextprotocol.io) layer. Point your agent's MCP client at the RootSign proxy and every `tools/call` becomes a tamper-evident `ACTION_RECORD` — any MCP-compatible agent is instrumented with zero framework code.

```bash
pip install rootsign[mcp]
```

```python
import rootsign
import uvicorn
from rootsign.mcp.proxy import create_proxy_app

rootsign.init(agent="mcp-proxied-agent", risk_tier="high")

async def serve_proxy():
    async with rootsign.session(objective="proxy MCP tool calls"):
        app = create_proxy_app(
            upstream_url="http://your-mcp-server:8001/mcp",
            # require_approval=True  # gate every proxied call on human approval
        )
        # A uvicorn-compatible ASGI app. Point the agent's MCP_SERVER_URL
        # here; tools/call is recorded and forwarded, other methods
        # (initialize, tools/list, …) pass through unchanged.
        await uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=8000)).serve()
```

`require_approval=True` gates every proxied tool call on a human decision — the same HiTL flow as `@rootsign.trace`, pausing before the call reaches the upstream server. See [ADR-010](docs/adr/ADR-010-mcp-interception-strategy.md).

RootSign can also run **as an MCP server** — exposing the audit log itself as a read-only data source so an "auditor agent" can list sessions, pull a session's hash chain, verify integrity, and read approval records in-context:

```python
from rootsign.mcp.server import create_server_app

app = create_server_app()   # ASGI app; mounts the MCP server at /mcp
# uvicorn rootsign.mcp.server:app --port 8001
```

Four read-only tools (`list_sessions`, `query_session_chain`, `verify_session_chain`, `get_approval_records`) over the existing store — no new tables.

## Human-in-the-loop checkpoint

High-risk actions can be gated on a human decision. Pass `require_approval=True` to `@rootsign.trace` and the SDK blocks the tool from running until someone approves it via the CLI.

```python
import rootsign

@rootsign.trace(
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

The cross-process flow above needs the Postgres backend — `rootsign approve` runs in a different process than your agent. On the default JSONL backend, `require_approval=True` prompts inline on the terminal instead; a headless run raises `HiTLUnsupportedBackendError` on the tool's first call, before any work happens, naming the fix.

See [ADR-007](docs/adr/ADR-007-hitl-checkpoint-design.md) for the design rationale (poll loop, timeout semantics, race tolerance).

## PII redaction

`RedactionConfig` runs **before** hashing, so stored `input_hash` / `output_hash` values carry no PII signal. Three ready-to-use configs:

```python
from rootsign import StandardPIIConfig, FinancialPIIConfig, HealthcarePIIConfig

# Standard: email, phone, US SSN, credit card, UK NI number
redaction = StandardPIIConfig()

tools = rootsign.wrap_tools([send_invoice], redaction_config=redaction)
```

`FinancialPIIConfig` adds account / routing / IBAN patterns; `HealthcarePIIConfig` adds MRN / NPI / DOB. Each accepts `extra_rules={...}` for domain-specific patterns without subclassing. See [ADR-006](docs/adr/ADR-006-redaction-contract.md).

## Evidence bundles

A verified hash chain answers a developer's question. A compliance officer asks a different one — *what did this agent do, who approved it, and can I trust this file?* — and cannot read JSONL to find out.

`rootsign export` turns one session into a directory that answers it:

```bash
$ rootsign export --local ~/.rootsign/sessions/<session_id>.jsonl --out ./bundles
```

<p align="center">
  <img src="https://raw.githubusercontent.com/Providex-AI/rootsign/main/docs/evidence-report.png" alt="RootSign evidence report — a VALID verdict banner, the session and agent identity block, the per-record chain table with self_hash and prev_action_hash, and the start of the session narrative" width="700" />
</p>

<p align="center"><em><code>report.html</code> — the verdict first, then the chain, then what happened.</em></p>

<p align="center">
  <img src="https://raw.githubusercontent.com/Providex-AI/rootsign/main/docs/evidence-report-approval.png" alt="Further down the same report: the wire_transfer action recorded as human_rejected with no output hash, directly above the approval that rejected it — approver, reason, latency, and the context they were shown" width="700" />
</p>

<p align="center"><em>Further down: a £250,000 transfer the agent proposed, the human who refused it, and the context they were shown.</em></p>

| File | What it holds |
|---|---|
| `manifest.json` | bundle version, generator, agent identity, source backend, a SHA-256 for every other file, and a reserved `compliance` block |
| `verification.json` | the verdict plus a per-record listing — sequence, `action_id`, `self_hash`, `prev_action_hash`, and whether that record was verified |
| `timeline.json` | the session narrative in order: open → decisions → actions → approvals → close, with every field each record carries |
| `redaction.json` | which field paths held the `[REDACTED]` sentinel — the "PII was never stored" evidence |
| `report.md` / `report.html` | human renderings of exactly the above; the verdict is the first thing on the page |

Three properties make the bundle worth handing over:

**It is rendered from the JSON, never assembled twice.** Markdown and HTML are pure functions of the three JSON documents, so a fact that is not in the machine-readable evidence cannot appear in the human-readable report. The HTML has no JavaScript and no external requests — it opens identically from a file share, an email attachment, or an air-gapped review machine.

**It says what it does not know.** A session that stored only hashes says *"payload previews not retained for this session"* rather than showing empty fields. Records after a chain break are marked `unverified`, not `verified` — the verifier stopped there and proved nothing about them. `redaction.json` reports the paths that were redacted and explicitly does not claim which rule fired, because RootSign does not record that.

**It is self-verifying, and honest about the limit.** `manifest.json` carries a SHA-256 for every file, and the manifest's own hash is printed at export. Record that hash somewhere outside the bundle:

```bash
$ rootsign export --check ./bundles/evidence-<session_id>
✓  INTACT — 5 file(s) match manifest.json

  manifest.json SHA-256:  a1d61338d0f793ea03ac472863fadea5660e1e94846913ce7613d8c117d67cc5
  Compare this against the hash recorded when the bundle was exported.
  The file checks above cannot detect an edit that also updated the manifest.
```

Re-hashing files against the manifest proves the bundle is internally consistent — nothing more. Someone who edits a file *and* updates the manifest passes that check trivially. The out-of-band manifest hash is the only anchor they cannot forge, which is why `--check` prints it whether or not anything is wrong.

For bundles leaving the building, `--redact-previews` withholds every field that carries stored content — payload previews, the context a human was shown at a checkpoint, and captured decision summaries — and names what it withheld, so the recipient knows what to ask for rather than guessing whether a field was stripped or never existed. Hashes, identities and timings are unaffected: the chain still verifies.

`--format json|md|html` narrows the renderings; it never drops the machine truth. `rootsign export <session_id>` reads a Postgres-backed session (agent name, owner and risk tier come along, since those live in the registration row). See [ADR-014](docs/adr/ADR-014-export-evidence-bundle.md).

## Offline & sync (cloud mode)

Set `ROOTSIGN_BACKEND=cloud` and records go to the hosted ingest endpoint (`pip install 'rootsign[cloud]'`). The interesting part is what happens when that endpoint is not there.

**Nothing is lost, and the agent never notices.** When retries are exhausted, the SDK fails over to the same append-only writer the local backend uses, rooted at `$ROOTSIGN_DATA_DIR/spool/`. One WARNING is logged — not one per record — and your tool calls keep returning their real results, because an audit layer that halts the business it observes has inverted its own value.

**Spooled records are ordinary session files**, so the evidence is usable before it is uploaded:

```bash
$ rootsign verify --local ~/.rootsign/spool/sessions/<session_id>.jsonl
VALID ✓  —  2 records, chain intact
  Session:  2d333ca5-7b28-4cd3-80b4-8f67350d19ba

This is a spooled session — recorded locally because the cloud endpoint was unreachable.
Upload it (and everything else waiting) with:

    rootsign-admin sync
```

> A file that spooled **mid-session** verifies as `INCOMPLETE`, not `VALID` — the records from before the outage went over the wire and are not in it. That is the honest answer: the verifier is reading one file, and "this file is the whole session" is not a claim anyone can make offline. Sync it and the store's copy verifies `VALID`.

**When connectivity returns, upload:**

```bash
$ rootsign-admin sync
1 spooled session(s), 4 record(s) under ~/.rootsign/spool
Uploading to https://ingest.getprovidex.com/v1/ingest
  synced    2d333ca5-7b28-4cd3-80b4-8f67350d19ba  4 accepted, 0 already present -> synced/
All spooled sessions uploaded.
```

Safe to re-run: idempotency is server-side by `event_id`, so a partially uploaded session resumes and records the store already has count as delivered. Fully-uploaded files move to `spool/synced/`; a partial one stays put and reports the first rejected sequence. `--dry-run` lists what is waiting without contacting anything, and works on a bare install.

**The chain spans the outage.** Records written to the spool continue the sequence the wire path started, because the chain is sealed before the SDK chooses a destination — so after sync the store holds one unbroken session, not two fragments. If the disk *also* fails, telemetry drops with accounting (one CRITICAL, a loss ledger, and a chain gap that makes the loss provable as `INCOMPLETE`) while anything gated by a human approval fails closed and the tool never runs. See [ADR-013](docs/adr/ADR-013-http-ingest-client-spool.md).

## Micro-batching (opt-in)

`BufferedIngestClient` wraps any ingest client and buffers `ACTION_RECORD`s in memory, flushing asynchronously — so a long, tool-heavy pipeline doesn't pay a per-call ingest round-trip. Enable it with `ROOTSIGN_BUFFERED=true` (the factory wraps the transport for you), or wrap explicitly:

```python
from rootsign import BufferedIngestClient, LocalIngestClient

async with BufferedIngestClient(LocalIngestClient(db=db)) as client:
    async with rootsign.session(agent_id=agent_id, client=client) as ctx:
        ...  # session() flushes the buffer before SESSION_CLOSE

# ROOTSIGN_BUFFERED=true also applies to the facade path — `init()` never
# wraps the transport implicitly (ADR-012), so buffering stays a deliberate
# opt-in.
```

Only auto-authorized actions are buffered; HiTL, decision, and session records pass through synchronously, so approvals and hash-chain ordering are never deferred. See [ADR-009](docs/adr/ADR-009-buffered-ingest-client.md).

## Performance

Instrumentation overhead is designed to be negligible. The LangGraph tracer's per-call overhead is benchmarked over 1,000 instrumented tool calls against a mock ingest client — isolating interception cost from the datastore:

| Metric | Per-call overhead |
|---|---|
| p99 | ~0.3 ms |
| mean | ~0.23 ms |
| median | ~0.23 ms |

That is **~15× under the 5 ms p99 budget** enforced by the regression test `test_p99_overhead_under_5ms` (ADR-004). Reproduce it yourself — no database required:

```bash
ROOTSIGN_SKIP_DB_BOOTSTRAP=1 python -m pytest \
    tests/performance/test_langgraph_benchmarks.py -m benchmark -s
```

The `-m benchmark` marker keeps the performance suite opt-in. Run it **without** `--cov`: coverage instrumentation roughly doubles the measured overhead and would not reflect production numbers. Figures above are indicative (dev laptop, Python 3.12); your absolute numbers will vary, but the budget assertion runs in CI-representative conditions.

## Architecture

* **`@rootsign.trace`** wraps a tool callable and emits an `ACTION_RECORD` envelope per call. LangGraph `BaseTool` and CrewAI tools are detected automatically.
* **MCP proxy** — `create_proxy_app` intercepts MCP `tools/call` at the protocol layer, so any MCP-compatible agent is instrumented without a framework adapter (ADR-010).
* **`rootsign.init()`** stores config with no I/O; `rootsign.session()` resolves the backend lazily on first entry and publishes `(ctx, client)` in a `ContextVar`, which is how `wrap_tools` / `@trace` / the MCP proxy find them without arguments (ADR-012).
* **`JsonlIngestClient`** is the default transport: append-only JSONL under `~/.rootsign`, no dependencies (ADR-011). **`LocalIngestClient`** is the Postgres in-process path. **`HttpIngestClient`** is the hosted-backend transport behind the optional `cloud` extra — it seals the chain client-side, owns its retry budget, and fails over to an on-disk spool rather than losing records (ADR-013). **`BufferedIngestClient`** optionally wraps any of them for async micro-batching (ADR-009).
* **Hash chain** is per-session: each `Action` carries `prev_action_hash` so reconstructing the chain detects any after-the-fact modification.
* **The wire format is published**, not internal: [docs/ingest-spec-v1.md](docs/ingest-spec-v1.md) specifies the envelope, the five event types, the full error-code registry, idempotency by `event_id`, batch semantics, and the client-side hashing contract for cloud mode — the server recomputes every hash and never trusts one. It is the contract the Phase 2 backend, the TypeScript SDK, and any community connector implement, and the document a design partner's security reviewer asks for.
* **`HiTLCheckpoint`** is an async poll loop that opens its own DB session per cycle — see ADR-007 for the loop-binding rationale.
* **`rootsign export`** builds the evidence bundle: JSON documents first, Markdown and HTML rendered from those and nothing else, with a SHA-256 per file in `manifest.json` (ADR-014). `rootsign-admin sync` is its counterpart on the operator CLI — batch replay of anything the spool caught while the endpoint was down.
* **Storage** is either the JSONL writer (default, zero-dependency) or PostgreSQL 16 + TimescaleDB 2.14, where the `actions` table is a hypertable and the chain stays intact across chunks. Both use the same frozen canonical hash formula (ADR-001) — `rootsign verify` gives the same verdict either way.

## What's next

* **Phase 2 hosted backend** — the ingest service and compliance dashboard the client already speaks to. `HttpIngestClient` ships now against the published [wire spec](docs/ingest-spec-v1.md), so the SDK half is done and tested against a mock server; cloud-sourced `rootsign export` waits on the server read API.
* **Web UI for HiTL** — approve/reject pending actions from a browser instead of the CLI.
* **AutoGen integration** — same duck-typing shape as CrewAI.

Watch the [GitHub Issues](https://github.com/Providex-AI/rootsign/issues) for the active roadmap.

## Contributing

We welcome contributions. See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, coding standards, and the PR process. By submitting a contribution, you agree to the [CLA](CLA.md).

Have a question, an idea, or feedback from using RootSign? Start a thread in [**GitHub Discussions**](https://github.com/Providex-AI/rootsign/discussions) — that's the best place for design feedback, use-case questions, and feature ideas. For reproducible bugs and concrete feature requests, open a [GitHub Issue](https://github.com/Providex-AI/rootsign/issues).

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

## Security

To report a vulnerability, see [SECURITY.md](SECURITY.md). Do **not** open a public GitHub issue.
