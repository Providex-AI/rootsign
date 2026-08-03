# ADR-010: MCP interception strategy — protocol-level provenance

- **Date**: 2026-08 (Pre-Phase 2 sprint — v0.1.5)
- **Status**: Accepted
- **Decider**: Founder
- **Related**: ADR-002 (transport-agnostic client), ADR-004 / ADR-005
  (framework tracer pattern this mirrors), ADR-006 (redaction-before-hash),
  ADR-007 (HiTL — reused over MCP)

## Context

`LangGraphTracer` (ADR-004) and `CrewAITracer` (ADR-005) each require
framework-specific interception code. Every additional framework — Swarm,
AutoGen, LlamaIndex — would need its own adapter. MCP (Model Context
Protocol, JSON-RPC 2.0) is becoming the de-facto layer between agents and
tools. Intercepting at the MCP layer makes *any* MCP-compatible agent
RootSign-compatible with zero framework code — the framework-agnostic moat.

## Decisions

### 1. Two complementary modes

- **Mode A — RootSign as MCP Proxy** (this sprint, Week 2). A transparent
  HTTP reverse proxy between the agent's MCP client and the real MCP server.
  It intercepts `tools/call` requests, emits an ACTION_RECORD, forwards
  upstream, and returns the response. The agent changes only its
  `MCP_SERVER_URL` — no code change.
- **Mode B — RootSign as MCP Server** (Week 3 stretch). Exposes the audit-log
  DB as an MCP server so "Auditor AI agents" can query hash chains in-context.
  Read-only over the existing CRUD layer; no new tables. Deferred; not part of
  this ADR's committed surface.

### 2. HTTP transport, not stdio

stdio only works for a local single-process pairing. A proxy that sits
between an agent container and a tool-server container needs a network
transport. HTTP (FastAPI + httpx) it is. `mcp`, `fastapi`,
`uvicorn[standard]`, and `httpx` ship in an optional `mcp` dependency group.

### 3. Lazy imports — `import rootsign` never requires the mcp extra

`rootsign/mcp/*` must not import `mcp` / `fastapi` / `httpx` at module load.
All such imports live inside functions (or under `TYPE_CHECKING`). `import
rootsign` — and even `import rootsign.mcp.proxy` — must succeed without
`rootsign[mcp]` installed; the imports only fire when a proxy app is actually
built. Tests guard with `pytest.importorskip`.

### 4. MCPProxyTracer mirrors the framework tracers

`MCPProxyTracer.intercept_tools_call(...)` is **keyword-only** (Flag 1) and
reuses the shared SDK machinery:

- `_to_json_safe` on the MCP `arguments` before hashing/persisting.
- Redaction applied **before** hashing (ADR-006 contract).
- The same `_emit_action_record` helper that the framework tracers use — so
  the ACTION_RECORD envelope, the failure-isolation rule (ADR-002), and the
  sequence-counter semantics are identical across every interception surface.
- HiTL is reused: `require_approval=True` pauses the proxy before forwarding
  upstream, gated by the same `HiTLCheckpoint` (ADR-007).

### 5. `_input_payload_override` on `_emit_action_record`

The framework tracers derive the input payload from a Python call's
`(args, kwargs)`. An MCP `tools/call` instead carries a ready-made
`params.arguments` dict, and the proxy has already `_to_json_safe`'d and
redacted it before the emit. So `_emit_action_record` gains a keyword-only
`_input_payload_override: dict | None = None`:

- When provided, it is used **directly** as the redacted input (no second
  `{"args": [...], "kwargs": {...}}` wrapping, no re-redaction) and is the
  value hashed into `input_hash` and stored in `input_redacted`.
- When `None` (every existing caller), behavior is unchanged — input is
  captured from `(args, kwargs)` and redacted as before.

This keeps the stored MCP `input_redacted` shape faithful to the wire
`arguments` (e.g. `{"to": "[REDACTED]", "subject": "Invoice"}`) rather than
burying it under a synthetic args/kwargs envelope. `func` still forwards to
upstream and its return value still drives the output hash.

**Adaptation note.** The sprint doc's illustrative call passes
`require_approval=` and `_input_payload_override=` straight into
`_emit_action_record`. The shipped helper's signature is
`(*, func, args, kwargs, tool_name, client, ctx, redaction_config)` — HiTL is
a separate `_emit_hitl_action` path, not a flag on the auto helper. The proxy
therefore routes to `_emit_hitl_action` when `require_approval=True` and to
`_emit_action_record` (with the new override) otherwise, rather than passing a
non-existent `require_approval` kwarg.

## Alternatives rejected

- **Python decorator on MCP client methods** — couples RootSign to a specific
  MCP SDK version and misses non-Python agents.
- **Monkey-patching httpx** — invisible, and fails the contract tests that
  assert an explicit interception seam.
- **stdio proxy** — breaks in distributed / multi-container deployments.

## Consequences

- Any MCP-compatible agent gets tamper-evident provenance by pointing at the
  proxy URL — no framework adapter.
- The `mcp` extra is optional; the core SDK imports with zero MCP deps.
- A new **required** CI job `framework-contract-mcp` runs the proxy contract
  tests, matching the repo's `checkout@v5` / `setup-python@v6`.
