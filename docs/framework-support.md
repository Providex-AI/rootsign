# RootSign — Framework Support

| Framework | Status | Versions tested | Install |
|-----------|--------|-----------------|---------|
| LangGraph | Supported | 0.1.x, 0.2.x (CI matrix) | `pip install rootsign[langgraph]` |
| CrewAI    | Coming soon (Sprint 3) | — | — |
| AutoGen   | Coming soon (Sprint 3) | — | — |
| Custom    | SDK API available — use `@rootsign.trace` on any async callable | — | `pip install rootsign` |

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

## Adding a new framework

Open a [framework integration issue](https://github.com/Providex-AI/rootsign/issues/new?template=framework_integration.yml)
describing the framework's tool / agent shape and how interception should
work. The CrewAI and AutoGen integrations in Sprint 3 will follow the same
template established by `rootsign/sdk/frameworks/langgraph.py`.
