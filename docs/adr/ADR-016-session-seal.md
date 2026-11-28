# ADR-016: Session seal — making a session's length and its bounds verifiable

- **Date**: 2026-11 (accepted 2026-11-27; implementation follows v0.4.0)
- **Status**: Accepted. Not yet implemented: the seal ships in a release after
  0.4.0.
- **Decider**: Founder
- **Related**: ADR-001 (canonical action hash, unchanged by this ADR),
  ADR-008 (decision capture), ADR-007 (HiTL approvals), ADR-013 (client-side
  chain sealing on the cloud transport), ADR-014 (evidence bundles), ingest
  spec v1 §3.5, §8 and §9

## Context

The per-session hash chain (ADR-001) proves that every action record it holds
is the one that was written, in that order. It cannot prove what it does not
hold. The docs list four gaps under "What verify does not check":

1. **Records deleted from the end.** A chain cannot tell how long it was meant
   to be. Removing the last tool calls leaves a shorter chain that verifies
   `VALID`. `SESSION_CLOSE` carries `metadata.total_actions`, but it is not
   hashed and `verify` does not compare against it.
2. **Session events.** `SESSION_OPEN` (including `objective` and `user_id`)
   and `SESSION_CLOSE` (including `status`) are outside the chain.
3. **Fields that change after the call.** `authorization_status` is excluded
   from the action hash because it legitimately changes when a human decides.
4. **Side records.** Decision records, approval records and an action's
   `decision_id` are outside the chain.

Gap 1 matters most: tail truncation is the cheapest edit an insider can make,
and today it is the one edit `verify` reports as `VALID`. The rest let a
session's framing (its objective, how it ended, who approved what) be edited
without a verdict changing.

Two facts constrain the design:

- **The action hash is frozen.** Folding session events into the action chain
  (for example, making `SESSION_OPEN` link 0) changes ADR-001's formula, every
  stored chain, the cross-backend vectors and the TypeScript SDK's contract.
  This ADR must leave `compute_action_self_hash` exactly as it is.
- **The chain is built where records are written, not in the SDK.**
  `SessionContext` tracks the sequence number but not the tail hash. The JSONL
  writer, the PostgreSQL ingest handler (`crud/action.py`) and, on the cloud
  transport, `HttpIngestClient` (ADR-013) each compute `prev_action_hash` and
  `self_hash`. Whatever seals a session must run in those same three places.

## Decisions

### 1. A seal on `SESSION_CLOSE`, computed by one function

When a session closes, the component that built its chain computes a **seal**
and writes it on the `SESSION_CLOSE` record:

```json
"seal": {
  "version": 1,
  "final_sequence": 7,
  "tail_hash": "<self_hash of the action at final_sequence, or null>",
  "open_hash": "<sha256 of the canonical SESSION_OPEN fields>",
  "side_digest": "<sha256 over the side records; see Decision 2>",
  "seal_hash": "<sha256 of the canonical seal fields + close status>"
}
```

- `final_sequence` is the **SDK's** count (`total_actions`, from
  `SessionContext`), not the store's. It is the independent statement of how
  long the chain should be; the store does not get to decide it.
- `tail_hash` is the store's own chain tail at close.
- `open_hash` covers `session_id`, `agent_id`, `objective`, `user_id` and the
  open time.
- `seal_hash` covers `version`, `session_id`, `final_sequence`, `tail_hash`,
  `open_hash`, `side_digest`, `status` and `error_message`, canonicalised the
  way ADR-001 canonicalises an action (sorted keys, compact separators,
  UTF-8).

The formula lives in one module, `rootsign/seal.py`, under the same rule as
ADR-001: every writer and every verifier imports it, none re-implements it,
and it ships with golden vectors (`tests/fixtures/session-seal/v1/`) that a
second implementation must reproduce.

### 2. What the seal covers beyond the chain: bounds and side records

- **Option A, bounds only.** `side_digest` is omitted. The seal covers the
  session's length, its tail, its open fields and its close status. Closes
  gaps 1 and 2.
- **Option B, bounds and side records.** `side_digest` is a SHA-256 over the
  session's decision records, approval records and each action's final
  `authorization_status` and `decision_id`, in record order. Closes gaps 1 to
  4 for everything written **before** the close. The writer has all of these
  at close time: the JSONL file holds them, the PostgreSQL handler can read
  them in the same transaction, and the cloud client saw what it sent.

**Decided: B.** Gaps 3 and 4 are exactly what a reviewer of an approval looks
at; sealing the bounds but not the approvals would leave the most
consequential record in a session the easiest to rewrite.

### 3. What `verify` reports

| Situation | Verdict | Exit |
| --- | --- | --- |
| Seal present, recomputes, chain ends at `final_sequence` with `tail_hash` | `VALID` (sealed) | 0 |
| Seal present, chain shorter than `final_sequence` | `INCOMPLETE`: "N records missing at the end" | 2 |
| Seal present, `seal_hash`, `open_hash`, `tail_hash` or `side_digest` does not recompute | `TAMPERED`: names the part that failed | 1 |
| No `SESSION_CLOSE`, or a close without a seal (0.4.0 and earlier, a crash, a session still running) | the chain's verdict as today, marked **unsealed** with a note that a cut tail cannot be detected | as today |
| Seal with an unknown `version` | as unsealed, with a note naming the version | as today |
| Any unsealed session, with `--require-seal` | `INCOMPLETE`: "not sealed: its end cannot be verified" | 2 |

Exit codes keep their meaning, and by default nothing that verifies today
changes verdict.

**`--require-seal` is what makes the seal enforceable.** The seal is written on
the `SESSION_CLOSE` record, so someone who deletes the last actions *and* the
close record leaves a session that looks like one that crashed before closing:
unsealed, and by default `VALID` with a note. A verifier that cannot insist on
a seal lets anyone defeat the seal by removing it. `rootsign verify
--require-seal`, and `verify_session(..., require_seal=True)` /
`verify_session_local(..., require_seal=True)`, report every unsealed session
as `INCOMPLETE`. It is the setting for CI, cron audits and anyone verifying
sessions recorded by a sealing SDK. It is opt-in so that sessions recorded by
0.4.0 and earlier, and sessions that genuinely crashed, keep verifying as
they do today.

### 4. Wire format: an additive field (spec §3.5, MINOR)

`SESSION_CLOSE` is `extra="allow"` (§3.6), so `seal` is a new session-level
key: additive under §9.1, schema `1.1`. An older store accepts and ignores it.
A store that supports seals **recomputes** it from what it holds and never
trusts the client's (§8.3).

When the recomputed seal and the client's disagree, the store **accepts the
close, stores its own seal, and keeps the client's claim beside it**
(`seal.client`). It does not reject: a rejected close would leave the session
`running` forever and the spool holding a close the store will never take.
Keeping both follows ADR-015's rule that a discontinuity is data. `verify`
then reports the disagreement: `INCOMPLETE` when the client counted more
actions than the store holds (records lost on the way), `TAMPERED` for any
other difference. On the JSONL and PostgreSQL backends the writer computes the
only seal, so this applies to the cloud transport, the hosted backend and the
mock ingest store.

### 5. Storage

- **JSONL**: the seal is part of the `SESSION_CLOSE` line.
- **PostgreSQL**: a nullable `seal` JSONB column on `sessions` (**migration
  0007**; the release notes say "run `rootsign-admin init` after upgrading").
  Not the free-form `metadata` column, so the seal is a first-class field the
  verifier and the export read directly, not a convention inside JSON anything
  can write to.
- **Spool**: a spooled close carries its seal like any other record, so a
  spooled session verifies sealed offline and syncs as is.

### 6. Evidence bundles

`verification.json` gains a `seal` block (`present`, `valid`,
`final_sequence`, `detail`), additive under bundle format 1.0; the report adds
a "Sealed" line beside the verdict.

### 7. What the seal does not do

- **It is an unkeyed hash, like the chain.** Someone with write access who
  recomputes the chain *and* the seal produces a session that verifies. The
  defence against that is the same as today: an anchor kept outside the
  store, such as the `manifest.json` hash `rootsign export` prints. A keyed
  or signed seal (`version: 2`) is the natural next step once key management
  exists (roadmap: KMS, Phase 3+), and the `version` field leaves room for it.
- **It cannot seal a session that never closes.** A crash before
  `SESSION_CLOSE` leaves an unsealed session, reported as such.
- **It covers what existed at close.** An approval or decision written after
  `SESSION_CLOSE` (none are today) would fall outside it.

## Alternatives considered

- **Put session events in the action chain.** Changes ADR-001's formula and
  every stored chain; rejected (see Context).
- **Verify against `total_actions` as it stands.** It is unhashed, so editing
  it is as easy as deleting the records; it would turn a silent truncation
  into a silent truncation plus an edited count.
- **A keyed MAC or signature now.** Needs key distribution and rotation the
  project does not have yet; deferred to `version: 2`.
- **An external transparency log.** Strongest, and far beyond the current
  roadmap.

## Decisions taken at review (2026-11-27)

The draft left four questions open. The founder decided:

- **Q1. Scope:** Option B, bounds and side records (Decision 2).
- **Q2. Unsealed sessions:** keep their verdict with an "unsealed" note by
  default, plus `--require-seal` / `require_seal=True`, which reports them as
  `INCOMPLETE` (Decision 3). The draft had the note only; review found that
  deleting the close record would then defeat the seal.
- **Q3. Store-side mismatch:** accept the close, store the store's seal and
  keep the client's beside it; `verify` reports the disagreement (Decision 4).
  The draft had "keep the store's seal and warn", which would have lost the
  client's claim.
- **Q4. PostgreSQL storage:** a `seal` column, migration 0007 (Decision 5).

## Consequences

- `rootsign verify` can report a cut tail, an edited open or close, and an
  edited approval, decision or `authorization_status`. With `--require-seal`
  it also refuses to call an unsealed session complete.
- The "What verify does not check" list shrinks to the unkeyed-hash limit and,
  without `--require-seal`, sessions that never closed.
- One new module, one migration (0007), one additive spec field, one additive
  bundle block and one new verify option. `compute_action_self_hash` and every
  existing chain are untouched.

## Implementation plan (the sprint after v0.4.0)

1. `rootsign/seal.py` with golden vectors and the never-re-implement rule.
2. Writers: the JSONL writer, the PostgreSQL handler (with migration 0007) and
   `HttpIngestClient` compute and write the seal; the mock ingest store
   recomputes it and keeps a differing client seal beside its own.
3. Verifiers: `verify` (file and database) with `--require-seal`,
   `verify_session` / `verify_session_local` with `require_seal`, the
   evidence bundle and the report.
4. Spec §3.5, §8 and §10, the docs pages (How it works, Verify, bundle
   format, Errors, CLI reference), the changelog, and the conformance
   checklist.
