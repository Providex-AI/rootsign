# ADR-015: Sync receipts — durable replay-outcome records

- **Date**: 2026-10 (ships in v0.3.1)
- **Status**: Accepted
- **Decider**: Founder
- **Supersedes**: the separately-drafted "Amendment 1: receipt schema v1.1",
  folded in here. There is one ADR-015, at receipt schema **v1.1**.
- **Related**: ADR-013 (extends Decision 4 — `rootsign-admin sync`; constrained
  by Decision 3's retry doctrine and Decision 4b's verdict vocabulary),
  ADR-014 (receipts join the evidence bundle), issue #37 (the community
  proposal that surfaced the gap)

## Context

v0.3.0's verdict vocabulary distinguishes "records missing" (INCOMPLETE) from
"records altered" (TAMPERED) — but a third state surfaced in community review
has no durable home: **"observed and rejected."** When `rootsign-admin sync`
replays a spool and the server refuses an envelope (`HASH_CHAIN_BROKEN`,
`VALIDATION_ERROR`, ...), the file stays in place and the rejected sequence is
*reported* — to stdout. The rejection evaporates with the terminal scrollback.
A reviewer later examining the spool file cannot tell "never uploaded" from
"uploaded and refused," which are very different facts in an investigation.

Neither existing artifact can hold this record:

- The **session chain** is sealed at the source and closed. Writing post-hoc
  replay outcomes into it would violate the model the whole product rests on.
- The **spool file** is the exhibit under replay. You do not annotate the
  exhibit with the examiner's verdict.

The outcome of handling evidence needs its own record — the chain-of-custody
log, kept beside the evidence, never written on it.

## Decisions

### 1. One receipt file per session, append-only across attempts

`<spool-root>/receipts/<session_id>.sync.json` — created on the first
non-dry-run sync attempt for that session, appended (as a new element in an
`attempts` array) on every subsequent attempt. Receipts follow the spool file
when it moves, to `<spool-root>/synced/receipts/`. `--dry-run` writes nothing:
no server contact, no observation, no receipt.

**Collision rule.** `mark_synced` already suffixes a colliding spool file
(`<stem>.1.jsonl`) rather than overwriting evidence. Receipts inherit the same
rule and the same suffix index, so a twice-synced session keeps each receipt
paired with the spool file it describes. A receipt is never overwritten and
never merged across files. A receipt file that cannot be parsed is moved
aside, not replaced — overwriting an unreadable record of evidence handling is
exactly the move this ADR exists to prevent.

### 2. Fail-fast is drawn at the **batch** boundary, not the response boundary

The ingest endpoint answers a batch with index-aligned responses for the whole
array (spec §7.1). If the server rejects element *k*, the outcomes it returned
for *k+1..n* **in that same response** are genuine observations, and the
receipt records every one of them exactly as returned. What fail-fast governs
is *solicitation*: after a response containing a chain-structural ACTION
rejection, **no further batch is sent** for that session in this attempt.

> Record everything the server actually said; stop asking it about envelopes
> its own chain rules have already doomed.

**A rejection does not cascade under this spec, and the draft rationale for
fail-fast was wrong about that.** The amendment argued that a verify-and-append
server which refused sequence N cannot accept N+1. Spec §7 rule 3 says the
opposite — "a rejection of element *k* MUST NOT abort elements *k+1…N*" — and
§8.3 is emphatic: a server **MUST NOT** reject a record for a
`prev_action_hash` pointing at something it has never seen, because doing so
"would erase the only evidence that anything was lost." Discontinuity is data.

So the correct reason to keep fail-fast is not cascade avoidance. It is
ADR-013 Decision 4's resume contract: a partially-uploaded session stays in
place and resumes on the next run, which is simple to reason about precisely
because delivery is a prefix. Continuing past a refusal would scatter
delivered records around a hole and make "where does the next run start"
a question the spool file can no longer answer by itself.

The batch-boundary rule matters *more* given §7 rule 3, not less: the
elements after a refusal are processed normally, and normally means accepted.
Discarding their responses — the pre-ADR-015 behaviour — filed records as
unattempted while the store already held them.

Issue #37's invariant list ("missing is distinct from rejected") implies the
stronger rule adopted here: **not-attempted must also be distinct from
rejected.**

### 3. Receipt schema v1.1

The file is `{session_id, attempts: [...]}`. `schema_version` is stamped
**per attempt** (`"receipt/1.1"`), so one file may legitimately hold v1.0 and
v1.1 attempts side by side as a long-lived spool is synced across upgrades.
Readers treat a missing `not_attempted` as empty.

Per attempt:

| Field | Contents |
| --- | --- |
| `schema_version` | `"receipt/1.1"` |
| `attempt_id`, `started_at`, `finished_at` | identity + timing |
| `endpoint`, `sdk_version`, `operator` | where it went, what sent it, OS user who ran it |
| `spool_file_sha256` | hash of the spool file **as replayed** — binds the receipt to the exact bytes observed |
| `record_count` | envelopes in the file at replay time |
| `outcomes` | `accepted`, `duplicate`, `not_attempted` as compressed ranges; `rejected` as a list of objects |
| `halted_at_sequence` | the ACTION sequence whose rejection stopped solicitation; `null` when the attempt ran to completion |
| `disposition` | `fully_accepted` \| `partial` \| `transport_failed` — see 3d |

`DUPLICATE_EVENT` stays "accepted for disposition purposes" (ADR-013) but is
recorded distinctly — an all-duplicate replay is a forensically different
event from a first upload, and the receipt is where that difference survives.

#### 3a. Ranges index **record position**; `halted_at_sequence` names a sequence

Ranges compress over the **1-based position of the envelope in the spool
file**, not over `sequence_number`.

This realizes the amendment's intent — "an ADR-015 receipt must never be
silently incomplete about its own attempt" — more completely than indexing by
sequence would. Only `ACTION_RECORD` envelopes carry a `sequence_number`;
`SESSION_OPEN` and `SESSION_CLOSE` do not (`SpoolSession.sequence_range` skips
them for exactly this reason). A partition over sequences would therefore
leave the session-boundary envelopes in no bucket at all, producing a receipt
that looks like it accounts for the whole file while two records have no
recorded outcome. That is the same ambiguity this ADR exists to close,
reintroduced one level down.

Record position is dense and total, which buys the checkable invariant:

> **The four outcome buckets partition `[1, record_count]` exactly — no
> overlap, no gap.**

A reviewer can verify the receipt accounts for every record by arithmetic,
without trusting the writer. Asserted in the suite, not merely documented.

Sequence numbers are not lost: every rejected entry carries `sequence`
alongside `record` when the envelope has one, and `halted_at_sequence` names
the root-cause sequence as a scalar. Issue #37 explicitly allowed field names
to stay RootSign-native provided the invariants hold; they do, and strictly
more of the file is accounted for.

#### 3b. `not_attempted` is a first-class outcome

Records never presented to the server in this attempt, because solicitation
halted. Not accepted, not duplicate, not rejected — not observed, and the
receipt says so explicitly rather than omitting them. This is the bucket whose
absence was the real defect in the v1.0 draft.

#### 3c. Disposition turns on *why* the attempt stopped

| Value | Meaning | Operator reads it as |
| --- | --- | --- |
| `fully_accepted` | every record taken; spool file and receipt move to `synced/` | nothing to do |
| `partial` | the store **deterministically refused** a record; both files stay | your data has a problem — fix it, then re-run |
| `transport_failed` | the attempt ended because the transport **gave up**; both files stay | try again later |

The split is keyed on the halting response's `retryable` flag, not on whether
an exception escaped. `HttpIngestClient` does not raise when the endpoint
dies — it exhausts its retry budget and returns `STORE_UNAVAILABLE` with
`retryable=True` (ADR-013 Decision 3). Keying off exceptions alone would
label every real outage `partial` and send operators to fix data that was
never the problem. A transport give-up also records the last error, whether it
arrived as an exception or as a retryable rejection.

#### 3d. Rejected entries carry `retryable`

`IngestResponse.retryable` is already on the wire and the transport already
honors it over its own reading of the code. The receipt records it verbatim.
"Retry this later" and "this will never succeed" are different instructions to
an operator, and a receipt that forces them to re-derive which one applies
from an error code is withholding something it was handed. Requested in issue
#37; adopted.

### 4. `PREDECESSOR_REJECTED` was considered and rejected

The amendment proposed a derivative error code so a reviewer could tell "bad
record" from "downstream of a bad record". **It is not adopted, and no code is
added to the registry.**

There is no cascade for it to name. Spec §7 rule 3 forbids a rejection from
aborting later elements of a batch, and §8.3 forbids rejecting a record for a
`prev_action_hash` the store has never seen. A conformant store therefore
never produces the derivative rejections the code was invented to label, and
the mock store's own comment already said so: *"A dangling `prev_action_hash`
is NOT a rejection."*

Adding it would have meant overturning §8.3 — a rule whose stated rationale is
that rejecting those records destroys the evidence that anything was lost. The
spec's registry is closed for v1 for good reasons, and this addition would
have bought nothing in exchange for weakening the strongest evidence-
preservation rule in it.

### 5. Receipts are evidence-grade, by the house rules

The receipt file's SHA-256 is printed at write time (same out-of-band noting
pattern as bundle manifests, ADR-014 Decision 2), and `rootsign export` of a
spooled or synced session **includes the receipt** in the bundle, hashed in
`manifest.json` like every other file. `verification.json` gains a `replay`
block summarizing the latest attempt.

**The `replay` block is omitted entirely when no receipt exists — not present
and null.** A session that was never synced must produce byte-identical output
to what it produced before this ADR, which is what keeps the addition additive
under bundle v1.0 (confirmed against `tests/unit/test_export_golden.py`: the
pinned fixture is a local JSONL session with no receipt, and `manifest.files`
is built from whatever documents exist). "Optional" is ambiguous between
absent and null; this ADR means absent. The receipt rides in the bundle as a
file governed by its own `schema_version`, so evidence bundles inherit the
vocabulary with no bundle-version break.

### 6. Sync does not pre-verify the chain before uploading

Issue #37's first fixture expects no receipt when the source chain fails local
verification. `sync` is deliberately kept as pure transport: if a chain is
broken, the server's rejection **is** the observation worth recording, and a
receipt saying `HASH_CHAIN_BROKEN` is more useful than no receipt at all.
Pre-verifying would substitute our prediction for the store's answer and
suppress the very record this ADR adds.

Local verification stays available and separate: `rootsign verify --local` on
the spool file answers the chain-integrity question, and the bundle's
`verification.json` carries that verdict next to the `replay` block. Two
independent verdicts, which is the separation issue #37's thread converged on.

### 7. `sync` stops calling a rejection INCOMPLETE

`_sync_one` printed `INCOMPLETE` when the store *rejected* a record. That is
the verdict word for "a record is missing" (ADR-013 Decision 4b) — the exact
conflation this ADR exists to remove, on the very command that now writes the
receipt. The operator-facing label becomes `REJECTED`, and the line names the
receipt path. stdout wording only: exit codes and the verdict enum are
unchanged.

### 8. The boundary principle, stated once

Recorded as doctrine so future features inherit it without re-litigation:
**the chain is sealed at the source; the exhibit is never annotated; every act
of evidence handling gets its own record.** A rejected replay is also a natural
Incident trigger candidate — this receipt is the Phase 3 Incident entity's
ancestor, and its schema should be read with that lineage in mind (which is
why `error_code` uses the registry verbatim).

### 9. Scoped out, recorded for later

- **Non-ACTION envelopes** (`DECISION_RECORD`, `APPROVAL_RECORD`) are not links
  in the action chain, so their rejections are independent and a future
  refinement may record-and-continue past them while still halting on ACTION
  rejections. Deferred: one halt rule is easier to verify and explain, and the
  append-only `attempts` history already accumulates the diagnostic picture
  across re-runs.
- No change to retry behavior, spool format, or disposition values.
- **Cross-implementation fixture vectors** (issue #37's actual ask) are not in
  this ADR. The schema frozen here is the prerequisite; publishing vectors
  independent implementations can run is tracked separately so v0.3.1 stays
  small.

## Consequences

- "Observed and rejected" becomes durable, exportable, and hash-bound —
  closing the last unrecorded state in the v0.3.0 evidence model.
- Issue #37's fixture 3 lands cleanly: duplicates recorded, the hard failure
  recorded with code and retryability, per-sequence outcomes preserved for
  everything observed, `not_attempted` covering the rest.
- Re-running `sync` after a partial failure leaves a legible history
  (attempt 1: partial, halted at 45; attempt 2: fully_accepted) instead of
  overwriting the story.
- A reviewer can confirm a receipt accounts for every record in the file it
  describes, by arithmetic, without trusting the writer.
- The first community design question gets answered with shipped code, in
  RootSign's own vocabulary.

## Trade-offs accepted

- **Receipts are content-hashed, not hash-chained.** A receipt attests to an
  attempt; it is not part of the session chain and must not pretend to be. If
  partners later need receipt-tampering resistance beyond the bundle manifest,
  that is the signing ADR (ADR-014 Decision 5's deferral), not a chain
  extension.
- **`operator` is the OS user, not an authenticated identity.** Honest
  labeling: it records who ran the command on that machine. Real operator
  identity arrives with SSO in Phase 3.
- **A single attempt yields at most one root-cause rejection** for
  chain-linked records, plus whatever the same batch already reported.
  Deliberate: the cascade's later rejections carry no independent information,
  and the append-only attempts history is where the full story accumulates
  across fix-and-re-run cycles.
- **A reviewer cannot tell, from the receipt alone, which rejections share a
  root cause.** That was `PREDECESSOR_REJECTED`'s job, and it is withdrawn
  (Decision 4). In practice the question does not arise: a conformant store
  refuses records on their own merits, so each rejection in a receipt is
  independent. `halted_at_sequence` names the one that stopped solicitation.
