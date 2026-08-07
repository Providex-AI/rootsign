# ADR-012: `rootsign.init()` facade + contextvar session resolution

- **Date**: 2026-08 (Pre-Phase 2 Sprint A — targets v0.2.0)
- **Status**: Accepted
- **Decider**: Founder
- **Related**: ADR-011 (JSONL default backend — init's zero-config path
  assumes it), ADR-002 (transport-agnostic client — init only ever
  holds an `IngestClient`), ADR-004/005/010 (framework tracers — all
  three gain implicit-context resolution)

## Context

The v0.1.x quickstart requires a first-time user to touch five
infrastructure concepts before their first record:
`AsyncSessionLocal`, `LocalIngestClient`, `asyncio.run(register_agent(...))`,
an explicit `SessionContext`, and `ctx=`/`client=` plumbed into
`wrap_tools`. Every one of those is the *right* seam internally
(ADR-002 exists precisely so they stay separable) — but none of them
belongs in the first thing an evaluator types. The competitive
onboarding bar is a one-line decorator plus an env var.

There is also a Phase 2 forcing function: the TypeScript SDK ships in
Phase 2 and will copy whatever public surface exists at that moment.
If the facade is not designed now, the plumbing-heavy API becomes the
cross-language contract by default.

## Decisions

### 1. Three-call public surface; the explicit API remains underneath

```python
import rootsign

rootsign.init(agent="invoice-agent", risk_tier="high")   # once, at startup

async with rootsign.session(objective="process invoice batch"):
    tools = rootsign.wrap_tools([send_invoice, search_web])   # no ctx, no client
    # ... run the graph
```

`init()` → `session()` → `wrap_tools()` / `@rootsign.trace` is the
entire documented quickstart. The explicit forms
(`wrap_tools(tools, ctx=..., client=...)`, hand-built `SessionContext`)
remain public, tested, and documented under "Advanced" — power users,
tests, and multi-agent processes need them. **Explicit arguments always
win over implicit resolution.** No existing call site breaks.

### 2. `init()` is synchronous and lazy — registration happens at first session open

`init()` must be callable from module scope in a plain script *and*
inside an already-running event loop (notebooks, FastAPI startup).
`asyncio.run()` inside `init()` would crash the latter. So `init()`
does no I/O: it validates arguments, resolves `SDKSettings`, and stores
an `_InitConfig` singleton. The agent **get-or-create** executes lazily
inside the first `rootsign.session()` entry — which is already async.

Get-or-create is keyed on `(name, environment)` — the same name may exist
independently per environment (e.g. `invoice-agent` in `development` and in
`production`):
- JSONL backend (ADR-011): lookup/append in `agents.jsonl`.
- Postgres backend: `INSERT ... ON CONFLICT (name, environment) DO NOTHING`
  then `SELECT` get-or-create in `rootsign/sdk/registration.py` (new
  `get_or_register_agent`). **This requires the sprint's one migration:**
  the shipped schema enforces `UNIQUE(name)` (`uq_agents_name`), which both
  forbids per-environment duplicates and gives `ON CONFLICT` the wrong
  target. The migration drops `uq_agents_name` and adds
  `uq_agents_name_environment`. It is safe with no backfill — `name` is
  globally unique today, so every existing row already satisfies the
  composite key (see ADR-011).
- Re-running a script never re-registers; changing `risk_tier` etc. for
  an existing `(name, environment)` logs a WARNING and keeps the stored
  values (mutation is an admin operation, not a side effect of init).

`rootsign.init()` with **zero arguments** also works: agent name
defaults to the entrypoint script name, environment to `development`,
risk tier to `medium`, backend to `jsonl`. Zero-config must reach a
verified chain.

### 3. Ambient session context via `contextvars.ContextVar`

A module-level
`_current_session: ContextVar[tuple[SessionContext, IngestClient] | None]`
is set by `rootsign.session()` on entry and reset on exit.
`ContextVar` (not a global, not thread-local) is the only primitive
that propagates correctly across `asyncio` task boundaries — parallel
LangGraph branches spawned inside one session inherit the right
context, while two concurrent sessions in one process (multi-agent
tests) stay isolated.

Resolution order everywhere a tracer needs context
(`wrap_tools`, `@trace`, `MCPProxyTracer`, decision capture):

1. explicit `ctx=` / `client=` kwargs
2. the ContextVar
3. → `RootSignNotInitializedError` naming the two-line fix

The existing `SessionContext` asyncio.Lock semantics (sequence
monotonicity, ADR-001) are untouched — this ADR changes how the context
is *found*, never how it behaves. The Sprint 2 note in
`context.py` ("Sprint 2 may add a contextvars-based session lookup; the
lock stays") is hereby cashed in.

### 4. The facade is the cross-SDK contract

`init / session / wrap_tools / trace / verify` is the surface the
Phase 2 TypeScript SDK mirrors (`rootsign.init({...})`,
`rootsign.session(async () => {...})` via `AsyncLocalStorage` — the
Node equivalent of `ContextVar`). Anything not in the facade is
Python-internal and may diverge per language. Record this in
`docs/framework-support.md` so contributors know which surface is
frozen.

### 5. What init() deliberately does not do

- No network calls, no DB connections, no file writes (lazy — Decision 2).
- No implicit `BufferedIngestClient` wrapping in v0.2.0. Buffering
  changes response semantics (ADR-009); it stays opt-in
  (`rootsign.init(..., buffered=True)` is reserved for a follow-up once
  the HTTP transport exists to justify it).
- No global mutable session. Only `init` config is a singleton; session
  state lives exclusively in the ContextVar.

## Consequences

- Quickstart drops from ~25 lines and five concepts to 6 lines and one
  concept. Combined with ADR-011, `pip install rootsign` → verified
  chain with no Docker and no plumbing.
- All three tracers (LangGraph, CrewAI, MCP proxy) share one resolution
  helper — the MCP proxy's keyword-only ctx/client params gain the same
  implicit fallback for free.
- The demo GIF, README, and site code sample all shrink to the facade;
  the "Manual" tab on the site becomes the Advanced section.
- TypeScript SDK scope in Phase 2 is now enumerable: five calls, one
  wire format (ingest-spec v1), one hash formula.

## Trade-offs accepted

- **Implicit context is action at a distance.** Mitigated by rule 1
  (explicit always wins), a loud `RootSignNotInitializedError`, and
  keeping the explicit API first-class in tests.
- **Lazy registration means a bad agent config surfaces at first
  session, not at init.** Acceptable: `init()` still validates
  everything validatable without I/O, so only backend-reachability
  errors move later — and they move to an async context where they can
  be raised cleanly.
