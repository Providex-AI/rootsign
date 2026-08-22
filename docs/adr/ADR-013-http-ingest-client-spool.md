# ADR-013: HttpIngestClient — cloud transport, client-side hashing, JSONL offline spool

- **Date**: 2026-08 (Pre-Phase 2 Sprint B — targets v0.3.0)
- **Status**: Accepted (2026-08-21) — implemented in v0.3.0
- **Decider**: Founder
- **Related**: ADR-002 (the stub this replaces; failure-isolation rule),
  ADR-009 (BufferedIngestClient — the batching layer this transport was
  built for), ADR-011 (JsonlIngestClient — becomes the offline spool),
  ADR-001 (canonical hash — now computed client-side in cloud mode)

## Context

`HttpIngestClient` has been a `NotImplementedError` stub since ADR-002
froze the seam. Phase 2's hosted backend needs the client half built,
and building it *before* the server exists is deliberate: the transport,
retry, and offline behavior get hardened against a mock server in CI, so
when the real backend lands, activation is `ROOTSIGN_BACKEND=cloud` plus
an API key — exactly the no-code-change promise ADR-002 made.

Three open questions must be settled now because they shape the server:

1. **Where are hashes computed in cloud mode?** JSONL mode computes
   client-side (ADR-011); Postgres mode computes store-side. Cloud must
   pick one.
2. **What happens when the network is down?** ADR-002's isolation rule
   says ingest failures must never break the agent's tool call.
3. **What is the wire contract?** ADR-009's selective buffering flushes
   *lists* of auto-authorized Actions but needs synchronous single-shot
   responses for HiTL, Decision, and session lifecycle events.

## Decisions

### 1. Client-side hashing is the cloud contract; the server verifies, never trusts

In cloud mode the SDK computes `self_hash` and `prev_action_hash` with
the frozen canonical formula (same code path as `JsonlIngestClient` —
there is still exactly one formula module) and sends them in the
envelope payload. The server **recomputes and verifies** on ingest;
mismatch → reject with `HASH_CHAIN_BROKEN`.

Why client-side:

- **Tamper evidence starts at the source.** The chain is sealed on the
  machine where the action happened, before transit. A compromised or
  buggy pipe cannot silently alter a record without breaking
  verification — which is the product's whole claim.
- **Spool/replay becomes trivial** (Decision 4): spooled records are
  already final, so replay is upload, not recompute.
- **The T2.8 parity test already guards the risk.** Two client-side
  compute sites (jsonl, cloud) share one implementation; the
  byte-identity contract test extends to cover the cloud envelope
  builder.

**What "verifies" covers, and what it must not.** Verification is
recomputation of `self_hash` from the canonical fields as received —
*that record against itself*. It is **not** a check that the record links
to anything the store already holds. A server MUST accept a record whose
`prev_action_hash` names a predecessor it has never seen, and MUST accept
a sequence gap; both are surfaced as `INCOMPLETE` at verification time,
never as a rejection at ingest (spec §8.3, and Decision 4a below).

That distinction is easy to lose, and losing it inverts the design in two
ways. It destroys evidence: when a spool write fails, the chain keeps
advancing precisely so the loss is provable, and the records that follow
legitimately reference an unwritten predecessor — a store that rejected
them would erase the only trace that anything went missing. And it makes
batches fragile: a batch is not a transaction (spec §7.3), so a transiently
rejected element is resent on its own, and the elements behind it must land
on their first attempt rather than being refused for naming a predecessor
that is still in flight. Reject only a *self*-hash that fails recomputation.

Consequence for the Phase 2 server: ingest is *verify-and-append*,
never *compute-and-trust*. Write this into the ingest spec (Decision 5)
so the server team — even if that team is just future-you — cannot
drift.

### 2. Transport: `httpx` behind a `cloud` extra; batch-capable endpoint

- New extra `rootsign[cloud]` containing `httpx>=0.27,<1.0`. Core stays
  four dependencies (verified: pydantic, pydantic-settings, typer,
  python-dotenv). `httpx` is already in the `test` extra for the FastAPI MCP
  tests, so the no-extras job must assert its absence rather than assume it —
  a `dev`/`test` environment will always have it. `ROOTSIGN_BACKEND=cloud` without the extra raises
  `RootSignCloudExtraRequired` — same actionable-error pattern as
  postgres (ADR-011 Decision 7), same no-extras CI gate.
- One endpoint: `POST {ROOTSIGN_CLOUD_URL}/ingest` accepting a JSON
  array of 1..N `IngestEnvelope`s, returning an array of
  `IngestResponse`s in the same order. Single-envelope calls (HiTL,
  DECISION_RECORD, SESSION_OPEN/CLOSE — everything ADR-009 passes
  through) are a 1-element array. `BufferedIngestClient` flushes land as
  N-element arrays. One shape, no special cases.
- One endpoint URL setting, and it already exists: `SDKSettings.CLOUD_URL`
  (`ROOTSIGN_CLOUD_URL`, default `https://ingest.getprovidex.com/v1`). Do
  **not** introduce `ENDPOINT` — `CLOUD_URL` and `API_KEY` have shipped since
  v0.1.x and ADR-002 already promises `ROOTSIGN_BACKEND=cloud` +
  `ROOTSIGN_API_KEY` as the entire activation story. Note the default already
  ends in `/v1`, so the request path is `{CLOUD_URL}/ingest`, not
  `{CLOUD_URL}/v1/ingest` — the doubled prefix is an easy and silent mistake.
- Auth: `Authorization: Bearer $ROOTSIGN_API_KEY`. Genuinely new settings:
  `HTTP_TIMEOUT_SECONDS` (default 10), `HTTP_MAX_RETRIES` (default 3).
- **The API key is a secret and must never reach a log record or an exception
  message.** Redact the `Authorization` header from any diagnostic output, and
  never interpolate the key into an error. Route any interpolated server-
  supplied string through `_log_safe` (`rootsign/sdk/decorator.py`) — a remote
  endpoint is exactly the untrusted source that guard exists for.
- Server→client error mapping is the existing `ErrorCode` registry —
  HTTP 429 → `RATE_LIMITED`, 5xx → `STORE_UNAVAILABLE` /
  `INTERNAL_ERROR`, 4xx carries the body's error_code through
  unchanged. No new error vocabulary.

### 3. Retry: bounded exponential backoff with jitter, retryable codes only

Retry only on the registry's retryable class (`STORE_UNAVAILABLE`,
`WRITE_TIMEOUT`, `RATE_LIMITED`, `INTERNAL_ERROR`) and transport-level
failures (connect/read timeouts). Backoff `0.5s · 2^n` with full
jitter, capped at `HTTP_MAX_RETRIES`. Non-retryable rejections
(`VALIDATION_ERROR`, `DUPLICATE_EVENT`, `HASH_CHAIN_BROKEN`, ...)
return immediately — retrying a rejection is how you build a DDoS
against your own backend.

**Exactly one layer retries.** `BufferedIngestClient` already has its own
retry loop (`_send_with_retry`, `max_retries` with doubling delay,
`rootsign/sdk/buffered_client.py`). Wrapping it around a transport that also
retries multiplies attempts and latency — 3 x 3 = 9 requests and tens of
seconds for one flush, while the buffer's flush interval keeps firing. The
transport owns retry; when `HttpIngestClient` is the inner client, the
buffer's own retry must not stack on top of it. Whichever way this is
implemented, the mock-server suite must assert the **total** request count for
a flush that fails and recovers, so the nesting can never regress silently.

### 4. Offline spool = the ADR-011 JSONL writer; replay via `rootsign-admin sync`

When retries are exhausted (or the endpoint is unreachable at session
open), `HttpIngestClient` **fails over to an internal
`JsonlIngestClient` rooted at `$ROOTSIGN_DATA_DIR/spool/`**, logs one
WARNING (not one per record — a flag flips the session into spool mode), and
the agent continues untouched.

Concretely, reusing the ADR-011 writer unchanged means files land at
`$ROOTSIGN_DATA_DIR/spool/sessions/<session_id>.jsonl` — the writer always
appends its own `sessions/` segment (`JsonlIngestClient._session_path`). Do
not "fix" this by special-casing the path: an ordinary layout is what lets
`rootsign verify --local` and `rootsign export --local` treat a spool file as
just another session file, which is the entire point of reusing the writer.
`$ROOTSIGN_DATA_DIR` is `SDKSettings.DATA_DIR`, already wired into the jsonl
backend at `rootsign/sdk/client.py`. This cashes in ADR-002's WAL-buffer promise
with zero new formats: spool files are ordinary session files, so
`rootsign verify --local` works on them *while offline*.

**Replay lives on the operator CLI**: `rootsign-admin sync [--dry-run]`
reads the spool directory, uploads each session's envelopes in
sequence order through the same batch endpoint, and moves
fully-accepted files to `spool/synced/`. Batch replay of pending
records is operational by nature — it is the transport-level analogue
of `rootsign-admin replay-pending`, shares its batch-replay core, and
keeping it there keeps the `cloud`-extra error surface off the
developer CLI. Because the person whose laptop spooled is usually a
developer, discoverability is preserved by breadcrumb: the spool-mode
WARNING and `rootsign verify --local` on a spool-directory path both
print the exact `rootsign-admin sync` command. Idempotency is
server-side by `event_id` — re-running `sync` after a partial failure
is safe by construction; `DUPLICATE_EVENT` responses count as success
for the mover.

Mid-session recovery (endpoint comes back while spooling): out of scope
for v0.3.0. A session that entered spool mode stays spooled to its end,
then `sync` uploads it. Automatic mid-flight failback is a Phase 2
refinement; the simple rule is explainable and testable.

### 4a. When the spool itself fails: fail-open for telemetry, fail-closed for controls

Disk full, read-only filesystem, or permission failure on the spool
write is the last rung, and ADR-002's isolation rule still binds the
telemetry path — but not the control path:

- **Auto-authorized records (telemetry): drop with accounting, never
  raise.** First loss logs one CRITICAL (not one per record) and opens
  an in-memory **loss ledger** (count, sequence range, reason) that is
  re-logged at session close and appended to the session file if
  writability returns. Crucially, `ChainState` keeps advancing in
  memory, so any record persisted after recovery references an
  unwritten predecessor's hash — **the gap is cryptographically
  evident at verify time** (missing sequences + hash discontinuity ⇒
  INCOMPLETE, never VALID). RootSign never pretends completeness; the
  chain testifies to its own losses. Silent-drop-without-evidence is
  the one failure mode this product must not have, and the hash chain
  structurally rules it out.
- **HiTL-gated records (controls): fail closed.** An Approval record
  is not observability — it *is* the authorization control. If it
  cannot be durably persisted (live transport down *and* spool
  unwritable), the gated action must not proceed:
  `HiTLPersistenceError` raises into the caller, exactly as a
  rejection would. A control whose record can be lost is not a
  control.
- **`ROOTSIGN_ON_RECORD_LOSS = warn | fail`** (default `warn`) lets
  regulated deployments opt the *telemetry* path into fail-closed too
  — some environments legitimately prefer a halted agent to an
  incomplete record. The default honors ADR-002; the setting honors
  the customer whose auditor disagrees.

### 4b. The verdict contract: three values, one precedence rule, both backends

Decision 4a makes gaps a first-class outcome, so `verify` needs a vocabulary
wider than a boolean.

**Shape.** `VerifyResult` gains `verdict: "VALID" | "TAMPERED" | "INCOMPLETE"`
and *keeps* `valid: bool`, false for both failure verdicts. Nothing consuming
the current shape breaks; new consumers read `verdict`.

**Precedence — this is the load-bearing part.** A gap *causes* a hash
discontinuity, so today a missing record already surfaces as TAMPERED: record
N+1's `prev_action_hash` points at a record that was never written. The two
conditions must therefore be disambiguated deliberately, not detected
independently:

- sequence gap at the point of discontinuity → **INCOMPLETE**
- hash mismatch across *contiguous* sequences → **TAMPERED**
- both present in one session → **TAMPERED wins** (worst verdict), with the
  incomplete ranges still listed in the result detail

Worst-verdict-wins is the only safe default: a tampered session that also has
gaps must never be downgraded to "just incomplete". Note this is new logic —
the local verifier currently checks duplicate sequence numbers and
`prev_action_hash` continuity, but never asserts the sequence set is dense.

**Exit codes.** `0` VALID, `1` TAMPERED, `2` INCOMPLETE. Additive: existing
consumers testing `!= 0` or `== 1` are unaffected, and CI can distinguish
"records missing" from "records altered" without parsing stdout.

**Both backends, or the parity claim is void.** `crud.action.verify_chain`
returns its own `{valid: ...}` dict, so today the two verifiers could disagree
about the same session — disqualifying for an evidence product. Gaps are just
as real in Postgres (a partially-uploaded sync, a deleted row — and a deleted
row is arguably the more audit-relevant INCOMPLETE). One shared verdict enum
and one shared precedence function; both verifiers return it; `rootsign verify
<session_id>` maps to the same three exit codes. Add `verdict` to the Postgres
dict additively — `rootsign/mcp/server.py` surfaces that dict as an MCP tool
result, so changing its shape would break a published surface.

A verdict-parity test drives the same gap-bearing and tamper-bearing fixtures
through both paths and asserts identical verdicts — the T2.8 byte-identity
principle, applied one level up at the conclusion rather than the input.

### 5. The wire format is published as `docs/ingest-spec-v1.md`

The envelope, the five event types, the error-code registry, the
idempotency rules, the batch semantics, and the client-side-hashing
contract move out of the internal docx and into a versioned markdown
spec in-repo. `schema_version` semantics: additive fields = minor bump,
breaking = major + `SCHEMA_VERSION_MISMATCH` from the server. This doc
is the contract for the Phase 2 server, the TypeScript SDK, and any
community connector — and it is the artifact a design partner's
security reviewer asks for.

## Consequences

- Phase 2 backend development starts with its client already tested:
  CI drives `HttpIngestClient` (direct and wrapped in
  `BufferedIngestClient`) against a mock server covering accept,
  reject-per-code, 429/5xx retry, timeout, and mid-batch partial
  failure.
- The demo story gains its best beat: kill the network mid-run, records
  spool, `verify --local` the spool file, restore the network,
  `rootsign-admin sync`, chain VALID in the cloud. Tamper-evidence *through*
  an outage.
- JSONL evaluators inherit a paved upgrade path: session files and
  spool files are the same format the cloud importer will accept.

## Trade-offs accepted

- **Client-side hashing exposes the canonical formula as a public,
  frozen contract.** It already was (ADR-001, hash vectors); this makes
  it load-bearing. Any future formula change is a major
  `schema_version` event — accepted, that discipline is the product.
- **No mid-session failback** means a long-running session that loses
  the network for one minute spools for hours. Acceptable for v0.3.0;
  `sync` closes the gap and the rule is simple enough to document in
  one sentence.
- **A mock server is not the real server.** The contract tests define
  the server's behavior rather than discover it — which is the point,
  but the first integration against the real Phase 2 backend must
  re-run this suite against staging (ADR-003 already promises exactly
  this).
