# ADR-005: CrewAI tool interception via duck-typed `_run` wrapping

- **Date**: 2026-05 (Phase 1, Sprint 3)
- **Status**: Accepted
- **Decider**: Founder
- **Related**: ADR-002 (transport-agnostic client), ADR-003 (framework contract tests), ADR-004 (LangGraph interception)

## Context

CrewAI tools share structural similarity with LangChain `BaseTool` but live
in a separate class hierarchy (`crewai.tools.BaseTool`, `crewai.tools.tool`).
Both pattern types expose `.name`, `.description`, and a synchronous `_run()`
method. CrewAI's `@tool` decorator produces a `StructuredTool`-like object;
the `BaseTool` subclass pattern is a Pydantic v2 model with `args_schema`.

rootsign must intercept CrewAI tool calls without importing from
`langchain_core` — CrewAI projects routinely run without LangChain installed,
and the SDK promises minimal framework coupling (ADR-003).

## Decision

`CrewAITracer` uses **duck typing**, not `isinstance` checks. It wraps any
object with:

- `.name: str` (callable identifier),
- `._run: callable` (the synchronous execution surface CrewAI calls),
- and no existing `_rootsign_instrumented` flag.

The wrap replaces `_run` with a traced version that emits an `ACTION_RECORD`
via the shared `_emit_action_record` helper (the same one used by the
LangGraph path), then delegates to the original `_run`. The wrapped object is
returned in place — `.name`, `.description`, and `.args_schema` survive
unchanged.

### Schema parity with `LangGraphTracer`

The `ACTION_RECORD` payload is constructed by the same `_emit_action_record`
helper, so payload fields (`tool_name`, `input_hash`, `output_hash`,
`input_redacted`, `output_redacted`, `timestamp`, `authorization_status`) are
byte-for-byte identical between LangGraph- and CrewAI-sourced records. The
ingest layer cannot distinguish them — and must not.

### Double-wrap guard

After wrapping, `tool._rootsign_instrumented = True` and
`tool._rootsign_context = ctx` are set. `wrap_tools()` checks the flag and
skips already-wrapped tools, mirroring ADR-004.

### Routing in `@rootsign.trace`

The decorator checks `_is_langchain_tool(func)` **before** `_is_crewai_tool(func)`.
This ordering is load-bearing: LangChain's `StructuredTool` also exposes
`.name: str` and `._run: callable`, so `_is_crewai_tool()` would (correctly,
by its duck-typing contract) return `True` for it. Routing LangChain first
ensures LangChain tools land in `LangGraphTracer`, not `CrewAITracer`.

### Pydantic v2 in-place mutation

CrewAI's `BaseTool` subclass pattern (Pattern B in Sprint Plan §2.1) is a
Pydantic v2 model. Direct attribute assignment (`tool._run = traced_run`) is
blocked by Pydantic's `__setattr__` validation hook. The tracer uses
`object.__setattr__` to bypass validation and mount the traced method, the
same workaround ADR-004 uses for `BaseTool.invoke` / `.ainvoke`.

### Sync-path correctness — shared `_run_sync`

CrewAI's `_run` is synchronous. The tracer's `traced_run` must execute an
async ingest coroutine before returning. The naive `loop.run_until_complete`
pattern raises `RuntimeError: This event loop is already running` whenever
`_run` is called from inside an async context (the typical integration-test
shape: `await client.handle(...)` then `tool._run(...)`).

The tracer therefore uses the same loop-state detector that Sprint 2 added
for LangGraph, now extracted to a shared helper at
`rootsign/sdk/_async_bridge.py`. Both tracers import `_run_sync` from there;
no cross-framework dependency is introduced.

## Alternatives rejected

- **Re-using `LangGraphTracer`** — rejected: it imports from `langchain_core`
  and would force every CrewAI project to install LangChain. Wrong dependency.
- **`isinstance(obj, crewai.tools.BaseTool)`** — rejected: requires a hard
  import of `crewai` at decorator import time. Duck typing keeps the SDK
  installable without the crewai extra.
- **Unified abstract `FrameworkTracer` base class** — considered, but
  premature with only two frameworks. When a third (AutoGen) lands, abstract
  at that point.
- **Constructing a new wrapping `BaseTool` instance** — rejected for the
  same reason as ADR-004: would require re-implementing Pydantic schema
  introspection to keep `args_schema` intact. In-place mutation sidesteps it.

## Consequences

- `rootsign/sdk/frameworks/crewai.py` mirrors `langgraph.py` in shape but
  shares no code with it other than the `_run_sync` helper and
  `_emit_action_record`.
- The `_is_crewai_tool` duck-type check intentionally matches LangChain
  tools as well. The decorator's `_is_langchain_tool`-first ordering is the
  only place this matters; a unit test asserts the routing.
- `tests/contract/crewai/` mirrors `tests/contract/langgraph/` in structure.
  A required CI job `framework-contract-crewai` runs against two CrewAI
  pinned versions (≥0.28,<0.40 and ≥0.40).
- The same tool object cannot be safely wrapped for two different
  `SessionContext`s simultaneously — second wrap overwrites the first
  context. Same limitation as ADR-004.

## Verification

- Unit tests: `tests/unit/test_crewai_tracer.py` — duck-type detection,
  metadata preservation, double-wrap guard, decorator routing including the
  load-bearing LangChain-first assertion.
- Contract tests: `tests/contract/crewai/test_tool_interception.py` — drive
  wrap-and-call against the real `crewai.tools.tool` decorator and assert
  payload-field parity with LangGraph records.
- Integration test: `tests/integration/test_crewai_integration.py` — three
  CrewAI tool calls produce three Action records and `verify_chain` returns
  `valid=True`.
