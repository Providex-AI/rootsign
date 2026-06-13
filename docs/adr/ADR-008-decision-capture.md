# ADR-008: Decision capture — opt-in reasoning records for v0.1.1

- **Date**: 2026-06 (Phase 1, PRD 1.9 patch — v0.1.1)
- **Status**: Accepted
- **Decider**: Founder
- **Related**: ADR-001 (hash canonical spec — unchanged), ADR-002
  (transport-agnostic client), ADR-007 (HiTL checkpoint — same
  out-of-chain pattern)

## Context

PRD requirement 1.9 specifies opt-in capture of LLM reasoning as
`Decision` entity records. Decision capture is foundational for Phase 2
session replay — compliance officers need to read the *why* alongside
the *what* when auditing an agent's behavior. Three design questions
required resolution before v0.1.1 ships:

1. Is `Decision` in the hash chain?
2. How is `decision_id` populated on `Action` records?
3. What is the consent and depth model?

## Decisions

### 1. Decision is NOT in the hash chain

The Action hash chain is computed over Action records only (ADR-001,
frozen). `decision_id` is a logical FK on Action — it is excluded from
the canonical hash spec fields. `verify_chain` is unchanged. Adding or
removing Decision records does not affect chain integrity.

**Rationale.** Mixing optional, opt-in records into a cryptographic
chain would make the chain non-deterministic — a session with capture
disabled would produce a different chain than an identical session with
capture enabled. That breaks the tamper-evidence guarantee. Same pattern
as APPROVAL_RECORD (ADR-007): out-of-chain entity, logical linkage only.

### 2. `decision_id` populated via `SessionContext._pending_decision_id`

`ctx.record_decision()` emits a `DECISION_RECORD` and sets
`_pending_decision_id` on the `SessionContext`. The next
`_emit_action_record` (or `_emit_hitl_action`) call reads and clears
`_pending_decision_id` atomically via `_consume_pending_decision_id()`.
**One Decision links to one Action only** — the pending slot is single,
cleared after first consumption.

**Rationale.** Explicit pairing by the developer avoids ambiguous
auto-linking (which LLM call corresponds to which tool call is not
always deterministic in multi-step agent chains). Developers fanning a
single decision to N tools can call `record_decision` N times. The
single-slot model is intentional — the alternative (queue or per-tool
dispatch) creates more ambiguous failure modes than it solves.

### 3. Consent via `ROOTSIGN_CAPTURE_DECISIONS` env flag

Decision capture is off by default. Opt-in via
`ROOTSIGN_CAPTURE_DECISIONS=true`. Depth controlled by
`ROOTSIGN_REASONING_DEPTH`: `minimal` / `summary` / `full`. Calling
`ctx.record_decision()` when the flag is off is a **silent no-op** — no
exception, no warning. Safe to call in all environments.

**Rationale.** Reasoning summaries tend to be the largest, most
PII-dense data the agent produces. The developer controls what is
persisted. Silent no-op (rather than raise) lets a single codebase ship
to environments with different capture policies without conditionals at
the call site.

## Consequences

- Phase 2 session replay can show Decision → Action narrative for
  sessions where the developer called `ctx.record_decision()`.
- Sessions without Decision records are still replayable — they show
  Action records only (the *what* without the *why*).
- `inputs_summary` capture (full LLM prompt) is deferred to Phase 2
  where a dedicated consent flow can handle it appropriately.
- The actual `ROOTSIGN_REASONING_DEPTH` value at emission time is
  persisted on the Decision row's `reasoning_depth` field so a replay
  consumer can tell why `reasoning_summary` is `None` or truncated.

## What is explicitly NOT in scope for v0.1.1

- LLM reasoning extraction from model completions (Phase 2).
- `inputs_summary` / raw prompt storage (Phase 2 with consent flow).
- Automatic reasoning interception without developer opt-in (Phase 2).
- AutoGen Decision capture (deferred with AutoGen framework support).
