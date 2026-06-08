# ADR-004: LangGraph tool interception via callable wrapping

- **Date**: 2026-05 (Phase 1, Sprint 2)
- **Status**: Accepted
- **Decider**: Founder
- **Related**: ADR-002 (transport-agnostic client), ADR-003 (framework contract tests)

## Context

LangGraph executes tools through two patterns rootsign must intercept:

1. **`ToolNode`** — `langgraph.prebuilt.ToolNode` wraps a list of LangChain
   `BaseTool` callables and is registered as a node in the `StateGraph`. This is
   the common production path.
2. **Direct tool invocation** — a node function calls `some_tool.invoke({...})`
   or `await some_tool.ainvoke({...})` itself.

Interception must work for both patterns without requiring the developer to
modify the graph definition. The hash-chain Action record must be emitted for
every tool call regardless of which path was used.

## Decision

rootsign wraps each `BaseTool` callable by **replacing `.invoke` and
`.ainvoke` in place on the existing tool instance**. The wrapped object is
still a `BaseTool` and is indistinguishable to LangGraph from the original.

Two entry points expose this:

1. `@rootsign.trace(...)` — applied as a decorator at tool definition time.
   When the wrapped object is a `BaseTool`, the decorator delegates to
   `LangGraphTracer.wrap_tool`. When it is a plain async callable, the
   Sprint 1 path is used unchanged.
2. `rootsign.wrap_tools([...], ctx=..., client=...)` — applied to an existing
   list of tools whose definitions the developer does not own.

Both produce identical Action records.

### Critical constraint — preserved metadata

LangChain uses `tool.name`, `tool.description`, and `tool.args_schema` to
generate the function-calling schema sent to the LLM. **These must survive
wrapping unchanged.** Because we mutate `.invoke` / `.ainvoke` on the existing
instance rather than constructing a new one, all metadata is preserved by
construction.

### Double-wrap guard

After wrapping, `tool._rootsign_instrumented = True` is set on the tool.
`wrap_tools()` checks this flag and skips already-wrapped tools so a
`@rootsign.trace`-decorated tool subsequently passed through `wrap_tools()`
is not re-instrumented.

The wrapped tool also carries `tool._rootsign_context` pointing to the
`SessionContext` it was wrapped for.

## Sync-path correctness — running event loop

`BaseTool.invoke` is synchronous. The tracer's `traced_invoke` must execute
an async ingest coroutine before returning. The naive approach
(`asyncio.get_event_loop().run_until_complete(coro)`) **breaks** inside a
process that already has a running loop on the current thread — the most
common case when a LangGraph node calls `tool.invoke(...)` from inside an
async graph.

The tracer therefore detects the caller's loop state:

- **No running loop on the current thread** → execute the ingest via
  `asyncio.run(coro)`. This is the contract-test and benchmark path.
- **Running loop on the current thread** → execute the ingest in a fresh
  thread that owns its own loop (`asyncio.run` inside the worker), then join.
  This preserves correctness without polluting the caller's loop, but
  callers that need to share state bound to the caller's loop (e.g. an
  `AsyncSession`) should prefer the async path `await tool.ainvoke(...)`.

The async path (`traced_ainvoke`) is always preferable when the caller has
the choice; it awaits the ingest on the same loop as the tool execution
and the database session.

## Alternatives rejected

- **Subclassing `ToolNode`** — couples rootsign to LangGraph's class hierarchy
  and breaks on any `ToolNode` API change. Also fails when the developer
  bypasses `ToolNode` and calls tools directly inside a node function.
- **Monkey-patching `ToolNode.__call__`** — invisible to users, violates the
  explicit-instrumentation principle from ADR-003, and fails the contract
  tests that verify wrap-at-definition-time semantics.
- **Building a new wrapper `BaseTool` subclass per call** — would require us
  to re-implement Pydantic schema introspection to keep `args_schema` intact
  across versions. In-place mutation sidesteps that.

## Consequences

- Each wrapped tool carries `_rootsign_instrumented` and `_rootsign_context`
  attributes. These are the only state additions on the tool object.
- The wrapping is shallow — `invoke`/`ainvoke` are replaced in place on the
  existing `BaseTool` instance, not on a copy. This means **the same tool
  object cannot be safely wrapped for two different `SessionContext`s
  simultaneously**; the second wrap will overwrite the first context.
- All ingest failures inside `traced_invoke` / `traced_ainvoke` are swallowed
  and logged at WARNING (ADR-002 failure-isolation rule). The tool's own
  success/failure is the only thing that bubbles to the caller.

## Verification

- Contract tests in `tests/contract/langgraph/test_tool_interception.py`
  exercise the wrap-and-invoke surface against LangGraph 0.1.x and 0.2.x
  with a mock IngestClient.
- Integration test in `tests/integration/test_langgraph_integration.py`
  drives the full path through `LocalIngestClient` against a real DB.
- Performance benchmark in
  `tests/performance/test_langgraph_benchmarks.py` confirms p99 overhead
  is below the 5 ms budget on 1 000 noop tool calls.
