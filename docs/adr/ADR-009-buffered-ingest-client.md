# ADR-009: BufferedIngestClient — SDK micro-batching

- **Date**: 2026-08 (Pre-Phase 2 sprint — v0.1.5)
- **Status**: Accepted
- **Decider**: Founder
- **Related**: ADR-002 (transport-agnostic client — this wraps any
  `IngestClient`), ADR-007 (HiTL checkpoint — the path that forces
  selective buffering), ADR-008 (Decision capture — same constraint)

## Context

In Phase 1 every tool call fires one ingest request synchronously.
`LocalIngestClient` (in-process) is fast, so this is invisible today.
`HttpIngestClient` (Phase 2) will fire one HTTP round-trip per tool call —
a 200-call multi-agent pipeline becomes 200 sequential round-trips of
agent-visible latency. Design partners running complex pipelines will hit
this the moment the hosted backend lands.

The fix is micro-batching: buffer records in memory and flush
asynchronously, so `handle()` returns without waiting on the transport.

The naive design buffers *every* envelope and returns a synthetic
"buffered" response immediately. That does not work against the shipped
SDK, because three call sites depend on a **synchronous, real**
`IngestResponse`:

1. **HiTL** (`_emit_hitl_action`): needs `response.entity_id` — the
   pending `action_id` — to bind the `HiTLCheckpoint` poll loop. It raises
   `RuntimeError` if `entity_id is None`.
2. **Decision capture** (`_emit_decision_record`): needs `response.entity_id`
   to set `SessionContext._pending_decision_id`. A `None` silently drops the
   decision, and the next Action records a null `decision_id`.
3. **Sequence counter** (`_try_ingest`): advances `ctx.next_sequence()` only
   when the store returns `status == "accepted"`, so SESSION_CLOSE's
   `total_actions` stays accurate.

There is also an **ordering constraint**: the Action hash chain links each
row to the previous via `prev_action_hash`, computed at insert order. If an
auto-authorized Action sits in the buffer while a later envelope passes
straight through to the store, the chain is built out of order and
`verify_chain` returns TAMPERED.

## Decisions

### 1. Decorator-pattern wrapper, never a subclass of the inner

`BufferedIngestClient` wraps any `IngestClient` via composition
(`self._inner`). It implements the `IngestClient` ABC so it is a drop-in at
every call site, but it never subclasses `LocalIngestClient` /
`HttpIngestClient`. Construction:

```python
BufferedIngestClient(
    inner,
    *,
    flush_interval_seconds=0.5,
    max_buffer_size=100,
    max_retries=3,
)
```

### 2. Selective buffering — buffer only auto-authorized ACTION_RECORDs

`handle()` inspects the envelope:

- **Buffer** when `event_type == "ACTION_RECORD"` **and**
  `payload["authorization_status"] == "auto_authorized"`. Append to the
  buffer and return a synthetic `_BufferedResponse(status="buffered")`
  immediately. This is the hot path and the entire latency win — an ordinary
  tool call in a long pipeline never blocks on the transport.
- **Flush-then-passthrough** for *everything else*: `SESSION_OPEN`,
  `SESSION_CLOSE`, `DECISION_RECORD`, `APPROVAL_RECORD`, and **pending
  (HiTL) `ACTION_RECORD`s** (`authorization_status != "auto_authorized"`).
  `handle()` first `await self.flush()` to drain any buffered actions —
  preserving chain insertion order — then `await self._inner.handle(envelope)`
  and returns the real `IngestResponse`.

**Rationale.** This makes HiTL and Decision capture correct *by
construction*: their envelopes always reach the store synchronously and get a
real `entity_id` back. No opt-out config guard, no unwrapping of `_inner`
inside the decorator. The chain order holds because any non-buffered envelope
flushes the buffer ahead of itself, and the only buffered records
(auto-authorized actions) are drained FIFO.

**Alternatives rejected.**
- *Buffer everything + HiTL opt-out in the decorator*: pushes
  transport-awareness into `decorator.py` (unwrap `client._inner` for HiTL),
  couples the layers, and still risks buffered actions trailing a pending
  action in the chain.
- *Buffer everything, forbid HiTL/Decision under buffering*: narrows the
  capability and adds a config-time guard for no benefit over selective
  buffering.

### 3. Sequence counter advances on `"buffered"`

`_try_ingest` advances `ctx.next_sequence()` when
`response.status in ("accepted", "buffered")`. A buffered Action has
optimistically "landed" from the SDK's point of view, so SESSION_CLOSE's
`total_actions` stays accurate without waiting for the flush.

**Accepted risk.** If a buffered flush later exhausts its retries and
discards the record, the counter over-counts by that record. This is
tolerated: `total_actions` is a reconciliation hint, and the "never crash
the agent" contract (ADR-002) already permits dropped records under sustained
transport failure. Correctness of the hash chain is unaffected — the store
assigns the authoritative `sequence_number` under its session lock.

### 4. Flush triggers (three, in priority order)

1. **Max buffer size** (default 100) — `handle()` flushes inline once the
   buffer reaches the cap.
2. **Periodic background task** (default 500ms) — `_flush_loop` drains the
   buffer on an interval. Runs in the caller's event loop via
   `asyncio.create_task`; no threads.
3. **Explicit** `flush()` / `close()` / `__aexit__`, and the implicit flush
   that precedes every passthrough envelope (Decision 2).

`asyncio.Lock` guards the buffer `deque`. `start()` launches the background
loop; callers using `async with` get it automatically. Because
`get_ingest_client()` is synchronous and cannot `await start()`, `handle()`
**lazily starts** the loop on first call (it runs inside a live event loop, so
`create_task` is safe). Lazy start is idempotent and is never resurrected
after `close()`. `session()`'s pre-close flush (Decision 5) guarantees a final
drain even for a client whose loop was never started.

### 5. SESSION_CLOSE flush contract

`rootsign.session()`'s `__aexit__` calls `await client.flush()` — guarded by
`hasattr(client, "flush")` duck typing, no ABC change — **before** emitting
`SESSION_CLOSE`. This guarantees every buffered ACTION_RECORD is persisted
before the session record closes. (Belt-and-suspenders with Decision 2, which
already flushes ahead of the SESSION_CLOSE passthrough; the session-level
flush also covers clients that buffer more aggressively in future.)

### 6. Error handling — never crash the agent

A failed batch send re-appends the failed envelopes to the **front** of the
buffer (`extendleft(reversed(failed))`, FIFO preserved) and retries up to
`max_retries` (default 3) with exponential backoff (0.1s, 0.2s, 0.4s). After
the final failure, log at ERROR and discard — the same failure-isolation
contract as `_try_ingest` (ADR-002). Buffering never raises into the
decorated tool.

## Consequences

- **`LocalIngestClient` users**: buffering is optional — in-process calls are
  already fast. `ROOTSIGN_BUFFERED` defaults to `False`.
- **`HttpIngestClient` users (Phase 2)**: buffering is effectively mandatory
  to avoid per-call round-trip latency; `ROOTSIGN_BUFFERED=true` wraps the
  client automatically via `get_ingest_client()`.
- **HiTL / Decision capture**: fully supported under buffering with zero
  changes to their code paths — they passthrough synchronously.
- **Hash chain**: unchanged. Ordering is preserved by flush-before-passthrough
  and FIFO drain; `verify_chain` is unaffected. ADR-001 stays frozen.
