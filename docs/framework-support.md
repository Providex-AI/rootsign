# RootSign — Framework Support

| Framework | Status | Versions tested | Install |
|-----------|--------|-----------------|---------|
| LangGraph | Supported | 0.1.x, 0.2.x (CI matrix) | `pip install rootsign[langgraph]` |
| CrewAI    | Supported | 0.28.x, 0.40.x (CI matrix) | `pip install rootsign[crewai]` |
| AutoGen   | Phase 2 (RootSign v1.0) | — | — |
| Custom    | SDK API available — use `@rootsign.trace` on any async callable | — | `pip install rootsign` |

> **AutoGen + TypeScript SDK** ship together as part of the RootSign **v1.0**
> release in Phase 2 — not Phase 1. Phase 1 (this Sprint series) closes out
> the Python SDK with LangGraph and CrewAI coverage plus the HiTL surface in
> Sprint 4.

## LangGraph integration notes

rootsign wraps tools by replacing `.invoke` / `.ainvoke` in place on the
existing `BaseTool` instance. All LangChain metadata (`.name`,
`.description`, `.args_schema`) is preserved untouched, so the LLM's
function-calling schema is unchanged and the wrapped tool is a drop-in for
`ToolNode([...])`. See [ADR-004](adr/ADR-004-langgraph-interception-strategy.md)
for the full design.

### Two entry points

```python
import rootsign

# 1) Decorator at tool definition time:
@rootsign.trace(ingest_client=client, session_context=ctx)
@tool
def send_invoice(customer_id: str, amount: float) -> str:
    ...

# 2) Wrap an existing list of tools — drop-in for ToolNode([...]):
tools = rootsign.wrap_tools([send_invoice, search_web], ctx=ctx, client=client)
tool_node = ToolNode(tools)
```

Both produce identical `Action` records.

### Sync vs async invocation

Prefer `await tool.ainvoke(...)` whenever the caller is already in an async
context (e.g. inside an async LangGraph node). The sync `tool.invoke(...)`
path still works — the tracer detects a running loop and routes the ingest
to a worker thread — but caller-side state bound to the caller's loop (most
notably a SQLAlchemy `AsyncSession`) is **not** safe to reuse across that
boundary. The integration test suite uses `ainvoke` for exactly this reason.

### Pydantic v2 BaseTool

LangChain 0.2+ moved `BaseTool` onto Pydantic v2, which blocks direct
instance attribute assignment. The tracer uses `object.__setattr__` to mount
the traced `.invoke` / `.ainvoke` and the `_rootsign_instrumented` marker.
This is intentional and covered by the contract tests
(`tests/contract/langgraph/`).

## CrewAI integration notes

rootsign wraps CrewAI tools by replacing `._run` in place on the existing
tool instance. CrewAI metadata (`.name`, `.description`, `.args_schema`)
is preserved untouched. Duck typing is used — `_is_crewai_tool` checks
`.name: str` and `._run: callable` — so `rootsign.sdk.frameworks.crewai`
never imports `crewai` at module load and the SDK installs cleanly
without the `crewai` extra. See [ADR-005](adr/ADR-005-crewai-interception-strategy.md)
for the full design.

### Two entry points

```python
import rootsign

# 1) Decorator at tool definition time:
@rootsign.trace(ingest_client=client, session_context=ctx)
@tool('Send Email')
def send_email(to: str, subject: str, body: str) -> str:
    ...

# 2) Wrap an existing list of tools — drop-in for Agent(tools=[...]):
tools = rootsign.wrap_crewai_tools([send_email, search_web], ctx=ctx, client=client)
agent = Agent(role='...', tools=tools, ...)
```

### Sync `_run` and event-loop affinity

CrewAI's `_run` is synchronous and has no native async surface. When
`_run` is called from inside an already-running event loop, the tracer
uses the same worker-thread bridge LangGraph uses (see ADR-004 §
"Sync-path correctness"). The same caveat applies: a SQLAlchemy
`AsyncSession` bound to the caller's loop is not safe to reuse across
the bridge. Production CrewAI apps that use `LocalIngestClient` should
either run CrewAI from a sync main thread (the typical case) or
configure a per-call session pattern. The integration test suite drives
the same emission logic via `_rootsign_arun` to avoid the bridge — that
attribute is an internal test surface, not a public API.

## Adding a new framework

Open a [framework integration issue](https://github.com/Providex-AI/rootsign/issues/new?template=framework_integration.yml)
describing the framework's tool / agent shape and how interception should
work. The CrewAI integration follows the template established by
`rootsign/sdk/frameworks/langgraph.py`; the AutoGen integration will
follow the same.
