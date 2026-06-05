# ADR-002: IngestClient must be transport-agnostic

- **Date:** May 2026
- **Status:** Accepted
- **Decider:** Founder

## Context

Phase 1 ships the RootSign SDK with **in-process** transport: the `@rootsign.trace` decorator calls `IngestHandler` directly in the same Python process. This is the right default for the open-source SDK — a developer installs `rootsign`, runs `docker-compose up -d db`, and the audit trail just works.

Phase 2 adds **HTTP** transport: the SDK POSTs events to a hosted Providex AI cloud backend, which runs the same `IngestHandler` server-side. We do not want the SDK's `@trace` decorator, redaction layer, session context, or hashing code to know which transport is in play. They are orthogonal concerns.

We also do not want to ship two SDKs, or fork the decorator code per transport. The lesson from earlier audit-logging products is that as soon as transport leaks into the API surface, every framework integration has to be written twice.

## Decision

`IngestClient` is an abstract base class in `rootsign/sdk/client.py` with two concrete implementations:

```python
class IngestClient(ABC):
    @abstractmethod
    async def handle(self, envelope: dict) -> IngestResponse: ...

class LocalIngestClient(IngestClient):
    """Phase 1 — calls IngestHandler in-process."""

class HttpIngestClient(IngestClient):
    """Phase 2 — POSTs to the hosted backend. Stub in Phase 1."""
```

A `get_ingest_client()` factory reads `ROOTSIGN_BACKEND` from the environment and returns the right implementation. `@rootsign.trace` only ever depends on the abstract `IngestClient` interface.

## Consequences

- **Phase 1 ships with `LocalIngestClient` only.** `HttpIngestClient` raises `NotImplementedError("Phase 2")` from `handle()` until the cloud backend is live. This is intentional: the abstract surface is fixed now so Sprint 2 framework integrations cannot accidentally bake in transport assumptions.
- **The wire format is `IngestEnvelope` regardless of transport.** Phase 0 already defines the envelope, idempotency rules, and error codes. Both clients serialize the same dict. HTTP just wraps it in an HTTPS POST with an API key header.
- **The SDK never raises ingest failures into the caller.** Per the Phase 0 SDK failure isolation rule, both `LocalIngestClient` (on DB errors) and `HttpIngestClient` (on network errors) must log at WARNING and fall back to the WAL buffer. The agent's primary tool call must complete or fail on its own terms, not on RootSign's terms.
- **No breaking change when Phase 2 activates cloud mode.** Existing Phase 1 users switch by setting `ROOTSIGN_BACKEND=cloud` plus `ROOTSIGN_API_KEY=…`. No code change in their agent or pipeline.

## Trade-off accepted

Carrying a stub `HttpIngestClient` in the Phase 1 codebase looks like over-engineering until you try to add it later. We've seen what happens when transport is bolted on after the fact: the decorator grows a `transport` kwarg, factories sprout in three places, framework integrations diverge. Locking the abstraction in Phase 1 — even with the cloud half stubbed — prevents that.

## Related

- [ADR-001](ADR-001-hash-canonical-spec.md) — the canonical hash is computed identically on either side of the transport, so chain verification is transport-blind.
- [ADR-003](ADR-003-framework-contract-tests.md) — contract tests run against `LocalIngestClient`; when the cloud client lands, the same contract tests should run against a real cloud staging endpoint with no test rewrites.
