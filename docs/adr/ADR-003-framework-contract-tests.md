# ADR-003: Framework contract tests are mandatory

- **Date:** May 2026
- **Status:** Accepted
- **Decider:** Founder

## Context

RootSign instruments agent frameworks — LangGraph, CrewAI, AutoGen, and (later) LlamaIndex, Pydantic AI, custom loops — by intercepting tool calls and emitting `ACTION_RECORD` envelopes. The interception path is intimate with each framework's internals: the tool registry, the call stack at invocation time, the way structured outputs are serialized.

These frameworks release frequently. They are pre-1.0 and intentionally agile — minor versions add new tool call mechanisms, remove deprecated callback hooks, change how exceptions surface from tools. A silent break in our integration looks like "the audit trail is incomplete for tool calls X and Y" — the failure mode is *missing data*, not a crash, and it can sit in production for weeks before anyone notices.

This is the single highest-risk failure mode in Phase 1. A broken hash chain at least fails loudly; a silently-dropped Action does not.

## Decision

Every framework integration MUST include a contract test suite that:

1. **Runs against a minimum of two framework versions** — the latest stable release and the previous minor. Both are pinned explicitly in the CI matrix (`langgraph~=0.1`, `langgraph~=0.2`, etc.).
2. **Exercises the complete tool call interception path** end to end: decorator → `SessionContext.next_sequence()` → redaction → `compute_payload_hash` → `IngestClient.handle()` → `IngestHandler` → Action row in DB with correct `tool_name`, `input_hash`, `output_hash`, `sequence_number`, and `prev_action_hash`.
3. **Is part of the CI matrix.** Contract test jobs run on every PR. They MUST NOT be marked `optional` or `continue-on-error: true` after the integration ships (Sprint 1 may carry empty stubs with `continue-on-error` until Sprint 2 fills them — that's the only exception).
4. **Fails loudly on framework API drift.** No `try/except` wrappers that swallow `AttributeError` or `TypeError`. If the framework moved a hook, we want CI red, not a partial Action record.

The contract test suite lives in `tests/contract/<framework>/` and is structured so that adding a new framework version means appending one matrix row, not duplicating tests.

## Consequences

- **New framework version releases may break CI.** This is the desired behavior. A broken CI on an upstream framework update is the warning signal *before* a customer's production pipeline starts dropping Action records. The maintainer's SLA is to fix or pin within **48 hours** of CI breaking on a new framework version release.
- **Carrying multiple framework versions in CI costs runtime.** Acceptable. The job matrix is parallelizable and each contract suite is small.
- **Pre-release framework versions are out of scope.** We test against released minor versions (`~=X.Y`), not `main`. Customers running framework prereleases are on their own until that version ships.

## Trade-off accepted

This creates ongoing maintenance burden — every framework's release cadence becomes our release cadence. The alternative is letting framework breaks surface as silently-dropped audit records discovered by customers, which is not acceptable for a product whose entire promise is *tamper-evident audit trails*. We absorb the maintenance cost as a cost of being trustworthy.

## Related

- [ADR-001](ADR-001-hash-canonical-spec.md) — the contract tests must verify that the Action records reaching the DB hash to the expected canonical form; a framework break that mangles `input_hash` is in scope.
- [ADR-002](ADR-002-transport-agnostic-client.md) — contract tests run against `LocalIngestClient` in Phase 1; the same tests should pass against `HttpIngestClient` against a staging endpoint in Phase 2 with no rewrites.
