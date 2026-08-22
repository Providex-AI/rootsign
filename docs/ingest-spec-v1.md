# RootSign Ingest Specification — v1

- **Spec version**: 1 (envelope `schema_version` `1.x`)
- **Status**: Draft — publishes with RootSign v0.3.0
- **Governs**: ADR-013 (`HttpIngestClient` / cloud transport), the Phase 2
  hosted backend, the TypeScript SDK, and any community connector
- **Source of truth**: `rootsign/ingest/schemas.py` (shapes),
  `rootsign/ingest/handler.py` (semantics), `rootsign/hashing.py`
  (canonical hash). Where this document and the code disagree, the code is
  right and this document is a bug.

The key words **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are used in
their RFC 2119 sense. "Store" means whatever accepts envelopes — the
in-process Postgres handler, the JSONL writer, or the hosted backend.
"Client" means whatever emits them — today the Python SDK.

---

## 1. Scope and transport bindings

The ingest protocol is one message shape (`IngestEnvelope`) and one reply
shape (`IngestResponse`), carried over any of three bindings. The bindings
differ only in *how* the envelope travels and *who* computes the hash chain:

| Binding | Client | Hash chain computed by | Batching |
|---|---|---|---|
| In-process, Postgres | `LocalIngestClient` / `ManagedLocalIngestClient` | Store, under `SELECT … FOR UPDATE` on the session row | No |
| In-process, JSONL file | `JsonlIngestClient` | Client, in memory (ADR-011 Decision 3) | No |
| HTTP, hosted backend | `HttpIngestClient` | **Client**, and verified by the server (ADR-013 Decision 1) | Yes — §7 |

Everything in §2–§6 applies to all three bindings. §7 (batch) and §8
(client-side hashing) are normative for the HTTP binding specifically.

The seam is `IngestClient.handle(envelope) -> IngestResponse` (ADR-002).
Its failure-isolation rule binds every binding: **an ingest failure MUST
NOT propagate into the instrumented tool call.** Failures log at WARNING
and the tool returns normally. The single exception is the HiTL control
path — see ADR-013 Decision 4a.

---

## 2. The envelope

`IngestEnvelope` — `rootsign/ingest/schemas.py`. `extra="forbid"`: an
unknown top-level key is a `VALIDATION_ERROR`, not a warning.

| Field | Type | Constraint | Meaning |
|---|---|---|---|
| `schema_version` | string | matches `^\d+\.\d+$` | Wire-format version of this envelope. Emitted as `"1.1"` by the current SDK (`rootsign.ingest.schemas.SCHEMA_VERSION`); `1.0` differs only in lacking the optional fields of §8.2. |
| `sdk_version` | string | 1–64 chars | Emitting SDK's version, from package metadata. Diagnostic only; the store MUST NOT branch on it. |
| `event_type` | enum | one of the five in §3 | Selects the payload schema. |
| `event_id` | UUID | — | Idempotency key. Unique per emitted event; a retry of the *same* event reuses it (§6). |
| `emitted_at` | datetime | ISO 8601, timezone-aware | When the client built the envelope. Client-supplied and therefore untrusted — see §6 on TTL clamping. |
| `agent_id` | UUID | — | Registered agent. Unknown → `UNKNOWN_AGENT`. |
| `session_id` | UUID | — | The session this event belongs to. |
| `payload` | object | per §3 | Event-type-specific body. |

Validation order in the store (`IngestHandler.handle`), which is also the
order a conforming server MUST follow:

1. **Parse the envelope.** Failure → `VALIDATION_ERROR`, non-retryable. The
   response still carries an `event_id` when one could be salvaged from the
   raw body, otherwise the all-zero UUID.
2. **Version check.** MAJOR component only. `SUPPORTED_SCHEMA_MAJOR` is
   `"1"`; anything else → `SCHEMA_VERSION_MISMATCH`, non-retryable. A store
   supporting major `1` MUST accept `1.0`, `1.1`, `1.99` alike — minor
   versions are never rejected on version grounds.
3. **Idempotency check** (§6). A hit short-circuits: no side effects.
4. **Payload validation** against the schema for `event_type` →
   `VALIDATION_ERROR` with field-level detail in `error_message`.
5. **Dispatch** to the per-event handler; application-level failures map to
   the registry codes in §5.
6. **Cache the response** if eligible (§6), then return it.

---

## 3. Event types and payloads

Five event types. `EventType` is a closed enum — a sixth value is a major
version event.

### 3.1 `SESSION_OPEN`

Opens a session. Emitted once, on entry to `rootsign.session(...)`.

| Field | Type | Constraint |
|---|---|---|
| `objective` | string \| null | ≤ 2000 chars |
| `user_id` | string \| null | ≤ 200 chars |
| `metadata` | object \| null | — |

`extra="allow"`. Store semantics: the agent MUST exist (`UNKNOWN_AGENT`
otherwise); the session MUST NOT already exist
(`SESSION_ALREADY_EXISTS`). On success the session is created with status
`running` and `entity_id` is the `session_id`.

### 3.2 `ACTION_RECORD`

One instrumented tool call. The only event type in the hash chain.

| Field | Type | Constraint |
|---|---|---|
| `tool_name` | string | 1–200 chars |
| `input_hash` | string | 64 lowercase hex chars |
| `output_hash` | string \| null | 64 lowercase hex chars |
| `input_redacted` | object \| null | post-redaction payload (ADR-006) |
| `output_redacted` | object \| null | post-redaction payload (ADR-006) |
| `timestamp` | datetime | defaults to now(UTC) |
| `duration_ms` | int \| null | ≥ 0 |
| `decision_id` | UUID \| null | links to a preceding `DECISION_RECORD` (ADR-008) |
| `policy_id` | UUID \| null | — |
| `authorization_status` | string | default `"auto_authorized"`; `"pending"` for a HiTL submission |

`extra="forbid"` — this payload feeds the hash chain and MUST be exact.

`input_hash` / `output_hash` are `compute_payload_hash` over the
**redacted** payload (`rootsign/sdk/hashing.py`): `json.dumps(payload,
sort_keys=True, ensure_ascii=True, default=str)`, SHA-256, lowercase hex;
a `null` payload hashes the empty string. When a record carries
`input_redacted` / `output_redacted`, verification re-derives the hash and
a mismatch is tamper (ADR-006 payload↔hash binding). Omitting the redacted
payloads is legal — hash-only sessions verify, they just cannot be read.

Store semantics: the session MUST exist and be `running`
(`SESSION_NOT_FOUND` / `SESSION_CLOSED`). On success the response carries
`entity_id` (the `action_id`), `sequence_number`, and `self_hash`.

For the cloud binding this payload additionally carries the client-computed
chain fields — see §8.

### 3.3 `DECISION_RECORD`

The reasoning step that selected an action (ADR-008). Optional; emitted
only when `ROOTSIGN_CAPTURE_DECISIONS=true`.

| Field | Type | Constraint |
|---|---|---|
| `selected_action` | string | 1–200 chars |
| `inputs_summary` | string \| null | ≤ 5000 chars |
| `reasoning_summary` | string \| null | ≤ 10000 chars |
| `confidence` | float \| null | 0.0 ≤ x ≤ 1.0 |
| `alternatives_considered` | list[string] \| null | — |
| `timestamp` | datetime | defaults to now(UTC) |
| `reasoning_depth` | string | `minimal` \| `summary` \| `full`; default `summary` |
| `reasoning_captured` | bool \| null | derived from `reasoning_summary is not None` when omitted |

`extra="forbid"`. Store semantics: session MUST be `running`; the session's
`decision_count` increments in the same transaction as the insert. The
response MUST return `entity_id` (the `decision_id`) — the SDK stashes it
as the pending decision for the next `ACTION_RECORD`, so this event can
never be batched (ADR-009 Decision 2).

### 3.4 `APPROVAL_RECORD`

A human (or timeout) decision on a HiTL-gated action (ADR-007).
**Not part of the hash chain** — a separate entity queried independently.

| Field | Type | Constraint |
|---|---|---|
| `action_id` | UUID | the gated action |
| `approver_id` | string | 1–200 chars |
| `approver_type` | string | e.g. `human`, `timeout_auto_rejected` |
| `context_presented` | object | what the approver was shown |
| `decision` | string | `approved` \| `rejected` \| `escalated` |
| `decision_reason` | string \| null | ≤ 2000 chars |
| `timestamp` | datetime | defaults to now(UTC) |
| `response_latency_ms` | int \| null | ≥ 0 |
| `parent_approval_id` | UUID \| null | set only when resolving an escalation |

`extra="forbid"`. Store semantics, all of which produce the registry codes
in §5:

- Target action missing → `ACTION_NOT_FOUND`.
- Target action already in a terminal authorization state
  (`human_approved`, `human_rejected`, `timed_out`) →
  `ACTION_ALREADY_RESOLVED`. Terminal states never transition, regardless
  of the incoming `decision`.
- `decision="escalated"` **with** `parent_approval_id` set → `VALIDATION_ERROR`.
  Escalation is two-level only in v1; chained escalation is rejected.
- `parent_approval_id` set but no matching approval for the same action →
  `APPROVAL_PARENT_NOT_FOUND`; likewise when the parent exists but its
  `decision` is not `escalated`.
- Otherwise the action's `authorization_status` transitions:
  `approved → human_approved`, `rejected → human_rejected`,
  `escalated → pending` (stays open for the resolving approval).

Note the deliberate asymmetry in how the two write paths locate the action
(a TimescaleDB hypertable consequence, not a protocol one): the ingest path
keys on `(session_id, action_id)` because this payload carries no
`action_timestamp`. Adding one would be a schema change and is not in v1.

### 3.5 `SESSION_CLOSE`

Terminal record for a session.

| Field | Type | Constraint |
|---|---|---|
| `status` | string | **MUST** be `completed`, `failed`, or `abandoned` |
| `error_message` | string \| null | ≤ 2000 chars |
| `metadata` | object \| null | the SDK sends `{"total_actions": <n>}` |

`extra="allow"`. Store semantics: session MUST be `running`; status and
`end_time` are set. If `metadata.total_actions` is present and disagrees
with the store's own `action_count`, the store MUST log a warning and
still accept — the count is a reconciliation signal, not an assertion.

### 3.6 Why two payloads allow extras and three forbid them

`SESSION_OPEN` / `SESSION_CLOSE` use `extra="allow"` on purpose: they are
the forward-compatibility seam. An SDK ahead of its store may attach new
session-level context, and a rejected `SESSION_OPEN` would break the entire
session. The three per-event payloads use `extra="forbid"` because they
feed the chain and must be exact.

A consequence worth stating plainly, because it bites at exactly one
moment: **adding a field to an `extra="forbid"` payload is additive for new
stores but rejected by old ones.** The MAJOR-only version check (§2 step 2)
lets a `1.1` envelope past the version gate and into a payload validator
that has never heard of the new field, producing `VALIDATION_ERROR`. Any
minor-version payload addition therefore requires either a store upgraded
first, or a client that omits the field when talking to an older store.
The versioning policy that governs this lives in §9.

---

## 4. Responses

`IngestResponse` — one shape for accept and reject, `extra="forbid"`.

| Field | Type | Present when |
|---|---|---|
| `status` | `"accepted"` \| `"rejected"` | always |
| `event_id` | UUID | always — echoes the request |
| `entity_id` | UUID \| null | accepted; the created row's id (session / action / decision / approval) |
| `sequence_number` | int \| null | accepted `ACTION_RECORD` |
| `self_hash` | string \| null | accepted `ACTION_RECORD` |
| `error_code` | `ErrorCode` \| null | rejected |
| `error_message` | string \| null | rejected; human-readable detail |
| `retryable` | bool \| null | rejected; §5 |

Rules:

- A rejection MUST carry all three of `error_code`, `error_message`,
  `retryable`.
- `error_message` is diagnostic text, not a machine surface. Clients MUST
  branch on `error_code` only, and MUST treat `error_message` as untrusted
  remote input when logging it (route it through the SDK's log-safety
  guard — a remote endpoint is exactly the injection source that exists
  for).
- An accepted `ACTION_RECORD` MUST return `sequence_number` and
  `self_hash`. In the cloud binding these echo the values the client sent
  (§8), which is how a client detects that it and the server disagree.

---

## 5. Error-code registry

The complete `ErrorCode` enum. The split is not advisory: it is the
retry rule. Clients MUST retry the retryable class and MUST NOT retry the
non-retryable class — retrying a deterministic rejection is how a client
builds a denial-of-service attack against its own backend.

### Non-retryable

| Code | Meaning | Typical cause |
|---|---|---|
| `SCHEMA_VERSION_MISMATCH` | Envelope MAJOR version unsupported | SDK newer than store, or vice versa |
| `UNKNOWN_AGENT` | `agent_id` not registered | agent never registered, or wrong tenant/key |
| `SESSION_NOT_FOUND` | `session_id` unknown to the store | `SESSION_OPEN` lost or never sent |
| `SESSION_CLOSED` | Session exists but is not `running` | event after `SESSION_CLOSE` |
| `SESSION_ALREADY_EXISTS` | `SESSION_OPEN` for a live session id | duplicate open with a fresh `event_id` |
| `DUPLICATE_EVENT` | `event_id` already handled | replay / re-sync (§6) |
| `ACTION_NOT_FOUND` | Approval targets an unknown action | approval raced ahead of its action |
| `ACTION_ALREADY_RESOLVED` | Target action is in a terminal state | second approval, or approval after timeout |
| `APPROVAL_PARENT_NOT_FOUND` | `parent_approval_id` has no matching approval | bad escalation link, or parent not `escalated` |
| `VALIDATION_ERROR` | Envelope or payload failed validation | missing/extra/malformed field; chained escalation |
| `HASH_CHAIN_BROKEN` | Chain invariant violated | client-computed `self_hash` failed server recomputation (§8) |

### Retryable

| Code | Meaning | Client action |
|---|---|---|
| `STORE_UNAVAILABLE` | Backing store unreachable | retry with backoff |
| `WRITE_TIMEOUT` | Store accepted the request but did not complete the write in time | retry with backoff; the same `event_id` makes it safe |
| `RATE_LIMITED` | Quota / throughput limit | retry with backoff, honoring `Retry-After` when present |
| `INTERNAL_ERROR` | Unclassified server-side failure | retry with backoff |

This registry is closed for v1. A new failure mode maps onto an existing
code or waits for the next version — "no new error vocabulary" is a
standing constraint on the Phase 2 server (ADR-013 Decision 2). §9.3 gives
the rule for growing it if that ever changes, and the client-side rule
(honor the wire `retryable` flag over an unrecognized code) that makes
growth possible without a major bump.

### HTTP status mapping (cloud binding)

| HTTP | Meaning | Maps to |
|---|---|---|
| `200` | Batch processed; per-element outcomes in the body | per-element `error_code` |
| `400` | Request body was not a valid batch (not a JSON array, empty, unparseable) | `VALIDATION_ERROR`, non-retryable |
| `401` / `403` | Missing, malformed, or rejected API key | `VALIDATION_ERROR`, non-retryable — the v1 registry has no auth-specific code and adding one is a version event (§9.3). The client MUST NOT retry and MUST NOT log the key |
| `413` | Batch exceeds the server's element or byte limit | `VALIDATION_ERROR`, non-retryable; client splits and resends |
| `429` | Throttled | `RATE_LIMITED`, retryable |
| `5xx` | Server fault | `STORE_UNAVAILABLE` (502/503/504) or `INTERNAL_ERROR` (500), retryable |

A `200` is about the *batch*, never about its elements: a batch in which
every element was rejected is still `200`. Clients MUST read per-element
`status`, and MUST NOT infer success from the HTTP status alone.

---

## 6. Idempotency

**The idempotency key is `event_id`, and nothing else.** It is minted once
per logical event and reused across every retry of that event — that is
what makes retry, reconnect, and spool replay safe by construction.

Rules:

1. A store MUST deduplicate by `event_id` within a window of at least **24
   hours** from receipt.
2. A hit MUST NOT produce a second side effect. No row, no line, no
   counter increment.
3. A store MUST NOT cache retryable failures. `STORE_UNAVAILABLE` and
   friends must actually re-hit the store on retry, or a transient blip
   becomes permanent.
4. A store SHOULD cache non-retryable rejections. They are deterministic —
   re-running them wastes work and, for a spool replay, re-walks a failure
   the operator already saw.
5. `emitted_at` is client-supplied. A store using it to compute expiry MUST
   clamp to `now + TTL`, or a far-future timestamp pins an entry forever.
6. A store MUST bound its dedupe set (the in-process store caps at 100 000
   entries, evicting oldest-first) so a flood of unique `event_id`s cannot
   grow it without limit.

### What a duplicate returns

The two in-repo stores answer differently, and both are conforming:

- The Postgres handler **replays the cached response verbatim** — a
  duplicate of an accepted event returns `accepted` again, with the
  original `entity_id`, `sequence_number`, and `self_hash`.
- The JSONL client **rejects with `DUPLICATE_EVENT`** (non-retryable, no
  line written).

So the normative client rule is: **a client MUST treat both a replayed
`accepted` and a `DUPLICATE_EVENT` rejection as success.** `rootsign-admin
sync` depends on exactly this — it is what makes re-running a partially
completed sync a no-op rather than a duplication. A server SHOULD replay
the original response when it still holds it, and fall back to
`DUPLICATE_EVENT` when the response body is no longer retained.

Idempotency is scoped per store. The in-process stores dedupe within one
process; the hosted backend MUST dedupe across processes and clients, since
that is the only layer at which a re-run of `sync` from a different machine
can be recognized.

---

## 7. Batch semantics (cloud binding)

One endpoint carries every event type:

```
POST {ROOTSIGN_CLOUD_URL}/ingest
Authorization: Bearer {ROOTSIGN_API_KEY}
Content-Type: application/json

[ {envelope}, {envelope}, ... ]      # 1..N, N ≥ 1
```

`ROOTSIGN_CLOUD_URL` defaults to `https://ingest.getprovidex.com/v1` —
**the default already ends in `/v1`**, so the path is `{CLOUD_URL}/ingest`.
`{CLOUD_URL}/v1/ingest` doubles the prefix, and does so silently.

The response is a JSON array of `IngestResponse` objects:

```
200 OK
[ {response}, {response}, ... ]
```

Normative rules:

1. **Ordered in, ordered out.** The response array MUST have exactly the
   same length as the request array, and element *k* of the response MUST
   correspond to element *k* of the request. Index alignment is the only
   correlation mechanism the client relies on; `event_id` echo is a
   cross-check, not the primary key.
2. **In-order processing.** The server MUST process elements in array
   order. Chain records are order-dependent, and a client that batches a
   session's actions expects them appended in the order it sent them.
3. **No cross-element atomicity.** A batch is not a transaction. A
   rejection of element *k* MUST NOT abort elements *k+1…N*, and MUST NOT
   roll back elements *1…k-1*. Each element succeeds or fails on its own.
   For chain records this rule and §7.2 only coexist because of §8.3:
   element *k+1* names *k*'s `self_hash` as its parent, so a server that
   rejected it for pointing at a record it does not (yet) have would abort
   the rest of the batch by the back door — and a transient failure on one
   element would permanently refuse every action behind it, since
   `HASH_CHAIN_BROKEN` is not retryable. **A dangling `prev_action_hash` is
   never an ingest rejection** (§8.3). The retry of element *k* closes the
   link; until it does, the discontinuity is data, not an error.
4. **Single events are 1-element batches.** There is no separate
   single-envelope endpoint. Everything ADR-009 passes through
   synchronously — `SESSION_OPEN`/`SESSION_CLOSE`, `DECISION_RECORD`,
   `APPROVAL_RECORD`, and HiTL (`pending`) `ACTION_RECORD`s — is a batch of
   one. One shape, no special cases.
5. **Batch size.** A server MUST accept batches of at least 500 elements
   (the SDK's default forced-flush threshold is 100). Over its limit, the
   server returns `413` and the client MUST split rather than retry
   unchanged.
6. **Exactly one layer retries.** The transport owns retry: bounded
   exponential backoff `0.5s · 2^n` with full jitter, capped at
   `ROOTSIGN_HTTP_MAX_RETRIES` — which counts **total attempts**, not retries
   after the first, so the default of 3 means one send plus at most two
   retries. Retry applies to the retryable class of §5 plus transport-level
   connect/read timeouts. A server-supplied `Retry-After` (seconds form)
   raises the floor of the next delay. A buffering layer wrapped
   around a retrying transport MUST NOT stack its own retry on top —
   3 × 3 attempts per flush is a self-inflicted outage.
7. **Partial-failure retry is per element.** When a batch comes back with a
   mix of accepted and retryably-rejected elements, the client resends only
   the retryable ones, with their original `event_id`s. §6 makes any
   over-resend harmless.
8. **The API key never appears in output.** The `Authorization` header MUST
   be redacted from any diagnostic, log record, or exception message.

---

## 8. Client-side hashing (cloud binding)

**In cloud mode the client seals the chain and the server verifies it. The
server MUST NOT compute the chain on the client's behalf, and MUST NOT
trust the values it receives without recomputation.**

This is the product's central claim made structural: the record is sealed
on the machine where the action happened, before it crosses a network, so
no intermediary — including RootSign's own backend — can alter a record
without verification failing.

### 8.1 The canonical hash

Frozen by ADR-001 and implemented once, in
`rootsign.hashing.compute_action_self_hash`. Every implementation — Python,
TypeScript, server-side verifier — MUST reproduce it byte for byte and MUST
NOT re-derive it from this prose:

```
canonical = {
  "action_id":        str(action_id),
  "session_id":       str(session_id),
  "tool_name":        tool_name,
  "input_hash":       input_hash,
  "output_hash":      output_hash or "",
  "prev_action_hash": prev_action_hash or "",
  "timestamp":        timestamp.isoformat(),
  "sequence_number":  sequence_number,      # int, 1-based
}
self_hash = sha256(json.dumps(canonical, sort_keys=True,
                              ensure_ascii=True).encode("utf-8")).hexdigest()
```

Details that are part of the contract, not incidental:

- `output_hash` and `prev_action_hash` are coerced `None → ""`. The first
  record of every chain has `prev_action_hash = ""`. A verifier that
  serializes them as JSON `null` reports genuine chains as tampered.
- `sequence_number` is 1-based and dense within a session.
- `timestamp` is the action's own timestamp (payload `timestamp`), not
  `emitted_at`.
- Serialization is Python `json.dumps` defaults apart from the two flags
  shown: `", "` / `": "` separators, keys sorted, non-ASCII escaped.
- Fields deliberately **excluded** from the hash: `duration_ms`,
  `input_redacted`, `output_redacted`, `authorization_status`. The chain
  proves record integrity; the payload↔hash binding of §3.2 is what binds
  the readable evidence to it.

### 8.2 What the client sends

For the cloud binding, the `ACTION_RECORD` payload of §3.2 additionally
carries the four chain fields the client computed:

| Field | Type | Constraint |
|---|---|---|
| `action_id` | UUID | client-minted |
| `sequence_number` | int | ≥ 1, 1-based, dense per session |
| `prev_action_hash` | string \| null | previous record's `self_hash`; `null` for sequence 1 |
| `self_hash` | string | 64 lowercase hex chars |

These fields are **optional** in the schema, which is what makes their
addition an additive, minor-version change (§9) — and one that lands under
the `extra="forbid"` caveat of §3.6, so a store predating them rejects a
cloud envelope with `VALIDATION_ERROR` rather than ignoring the extras.

The other two bindings handle a sealed record differently, and both
behaviors are deliberate:

- **Postgres rejects it** with `VALIDATION_ERROR`. That store assigns chain
  identity itself, under a row lock, so it cannot honor a seal — and
  accepting one while quietly recomputing would fork the chain, leaving the
  client holding a `self_hash` the store never stored. A loud rejection is
  the only honest answer.
- **The JSONL writer adopts it** verbatim. That is how a spooled record
  reaches a session file under the identity it was sealed with (§7 and
  ADR-013 Decision 4). Re-minting would produce a locally consistent chain
  that never happened, and the record the client believes it sent would
  exist nowhere.

The rule underneath both: a record is sealed exactly once, at the point the
action happened.

### 8.3 What the server does

On each `ACTION_RECORD` the server:

1. Recomputes `self_hash` from the canonical fields as received.
2. Rejects with `HASH_CHAIN_BROKEN` (non-retryable) on mismatch.
3. Stores the record with the client's values — it never substitutes its
   own `action_id`, `sequence_number`, or hashes.
4. Echoes `sequence_number` and `self_hash` in the accepted response, so a
   client can detect divergence.

**A server MUST NOT reject a record for a sequence gap or for a
`prev_action_hash` that points at a record it has never seen.** This is the
interaction between Decision 1 and Decision 4a of ADR-013, and getting it
backwards destroys evidence: when a client's spool write fails, the chain
state keeps advancing so the loss is cryptographically evident. The records
that follow *legitimately* reference an unwritten predecessor. Their job is
to make the gap provable at verification time — a session with a gap
verifies as `INCOMPLETE`, never as `VALID` and never as absent. A server
that rejected them would erase the only evidence that anything was lost.
Discontinuity is data. Only a *self*-hash that fails recomputation is a
rejection.

### 8.4 Verification vocabulary

Verification of a stored chain yields one of three verdicts, under one
precedence rule shared by every verifier (the three-value vocabulary ships
in v0.3.0; `VerifyResult` keeps its `valid: bool`, false for both failure
verdicts):

| Verdict | Condition | CLI exit |
|---|---|---|
| `VALID` | dense sequences, every `self_hash` recomputes, every link matches | `0` |
| `TAMPERED` | hash mismatch across contiguous sequences | `1` |
| `INCOMPLETE` | sequence gap at the point of discontinuity | `2` |

Both conditions in one session → **`TAMPERED` wins** (worst verdict), with
the incomplete ranges still reported in the detail. A tampered session that
also has gaps must never be downgraded to "just incomplete".

---

## 9. Schema versioning

`schema_version` is `MAJOR.MINOR` and describes the **wire format**, not the
SDK, not the store, and not this document's revision. Spec v1 governs every
`1.x` envelope. `sdk_version` travels alongside it for diagnostics and MUST
NOT be used for compatibility branching.

### 9.1 The policy

- **Additive change → MINOR bump.** A store supporting MAJOR `1` MUST accept
  `1.0`, `1.1`, `1.99` alike. Only the MAJOR component is checked (§2 step 2).
- **Breaking change → MAJOR bump**, and a store that does not support the
  incoming MAJOR MUST reject with `SCHEMA_VERSION_MISMATCH`
  (non-retryable), naming the MAJOR it does support in `error_message`.
  Clients MUST NOT retry that rejection — no amount of backoff turns a
  version mismatch into an accept — and SHOULD surface it as an upgrade
  instruction rather than an ingest failure.

| Additive — MINOR | Breaking — MAJOR |
|---|---|
| New **optional** payload field with a default | New **required** field anywhere |
| New optional envelope field | Removing or renaming any field |
| Widening a constraint (raising a `max_length`, relaxing a pattern) | Tightening a constraint, or changing a field's type |
| New session-level key on the `extra="allow"` payloads (§3.6) | New `EventType` value — the enum is closed |
| New `ErrorCode`, under the rule in §9.3 | New value in a closed value set (`SESSION_CLOSE.status`, `decision`) |
| New optional field in `IngestResponse` | Changing batch index-alignment, ordering, or atomicity (§7) |
| | Changing the idempotency key away from `event_id` (§6) |
| | **Any change to the canonical hash formula** (§9.4) |

Anything not clearly on the left is on the right. The asymmetry is
deliberate: a wrong MINOR bump silently breaks a fielded client, while a
conservative MAJOR bump costs one coordinated release.

### 9.2 Upgrade ordering: stores first

The MAJOR-only check means a `1.1` envelope reaches a `1.0`-era payload
validator, and three of the five payloads are `extra="forbid"` (§3.6). So a
MINOR addition to `ACTION_RECORD`, `DECISION_RECORD`, or `APPROVAL_RECORD`
is forward-compatible **only if the store is upgraded first**; against an
older store it produces `VALIDATION_ERROR`, not tolerant pass-through.

Rules that follow:

1. A MINOR field addition to a forbidding payload MUST be deployed
   store-first. For the hosted backend that ordering is RootSign's to
   guarantee — the server ships the field before any SDK release emits it.
2. A client MUST NOT depend on a store ignoring fields it does not know.
3. Session-level forward compatibility goes through `SESSION_OPEN` /
   `SESSION_CLOSE`, which allow extras precisely so that a rejected session
   envelope — which would break an entire run — is not the cost of an SDK
   running ahead of its store.

### 9.3 Growing the error registry

The §5 registry is **closed for v1 by policy**: a new failure mode maps onto
an existing code or waits. The mechanism to add one without a MAJOR bump
nevertheless exists, and it exists because of one client rule:

> `retryable` on the wire is authoritative. A client encountering an
> `error_code` it does not recognize MUST honor the response's `retryable`
> flag rather than guessing from the code, and MUST NOT crash on the unknown
> value.

Given that, adding a code is a MINOR change plus a spec revision. Removing
one, or reclassifying an existing code between the retryable and
non-retryable classes, is MAJOR — clients have retry behavior compiled
around the current split.

### 9.4 The canonical hash formula is frozen under this same policy

`compute_action_self_hash` (§8.1, ADR-001) is frozen. **Changing it is a
MAJOR `schema_version` event** and additionally requires, per ADR-001 and
`rootsign/hashing.py`:

1. a new hash algorithm version identifier,
2. a migration plan for existing records, and
3. founder sign-off.

CONTRIBUTING refuses a change to that module without a new ADR. This is not
ceremony. Since ADR-013 the formula is computed **client-side** for the cloud
binding, which promotes it from an implementation detail to a published wire
contract with three independent implementations (Python SDK, server verifier,
TypeScript SDK) that must agree byte for byte. And because records are
verified against the formula that sealed them, a formula change does not
migrate — both formulas must coexist, selected by the record's
`schema_version` MAJOR, for as long as any record hashed under the old one
must remain verifiable. Which is forever, for an evidence product.

The cost of a formula change is therefore permanent, and pricing it as a
MAJOR version event is the mechanism that keeps it deliberate.

### 9.5 Versioning and the offline spool

Spooled records were sealed under the `schema_version` in force when they
were written, possibly months before they are replayed.

- `rootsign-admin sync` MUST replay envelopes byte-for-byte as spooled. It
  MUST NOT rewrite `schema_version` (or any other field) to match the
  server's current version — the envelope records what the client actually
  emitted, and rewriting it misrepresents provenance for a record whose
  entire purpose is provenance.
- A store SHOULD therefore continue accepting the previous MAJOR for as long
  as clients may plausibly hold spooled records under it, and MUST announce
  a cutoff before enforcing one.
- If a store does drop a MAJOR, the spooled files remain **locally**
  verifiable forever — they are ordinary session files, and
  `rootsign verify --local` needs no server. Evidence is never stranded by a
  version policy; only its upload is.

---

## 10. Server conformance checklist

A hosted backend conforms to ingest spec v1 when:

- [ ] It accepts `POST {base}/ingest` with a JSON array of 1..N envelopes and
      bearer auth, returning an index-aligned array of the same length (§7.1).
- [ ] It processes elements in order, without cross-element atomicity (§7.2–7.3).
- [ ] It validates envelope → version → idempotency → payload → dispatch, in
      that order, with the codes of §5 (§2).
- [ ] It rejects unknown top-level envelope keys and unknown keys in the three
      `extra="forbid"` payloads (§2, §3.6).
- [ ] It accepts every minor version within major `1`, and rejects other
      majors with `SCHEMA_VERSION_MISMATCH` naming the major it supports
      (§2 step 2, §9.1).
- [ ] It accepts replayed spool envelopes at the `schema_version` they were
      sealed under, without requiring the current one (§9.5).
- [ ] It deduplicates by `event_id` for ≥ 24h, never double-writes, never
      caches retryable failures, and answers duplicates in a way the client
      can read as success (§6).
- [ ] It emits only codes from the §5 registry, with `retryable` set to the
      registry's classification.
- [ ] It recomputes `self_hash` on every `ACTION_RECORD` and rejects
      mismatches with `HASH_CHAIN_BROKEN` (§8.3).
- [ ] It stores client-supplied `action_id` / `sequence_number` /
      `prev_action_hash` / `self_hash` unmodified (§8.3).
- [ ] It accepts records with sequence gaps and dangling `prev_action_hash`,
      surfacing them as `INCOMPLETE` at verification rather than rejecting
      them at ingest (§8.3–8.4).
- [ ] It returns `429` with `RATE_LIMITED` and `5xx` with
      `STORE_UNAVAILABLE`/`INTERNAL_ERROR` rather than inventing codes (§5).

---

## Appendix A — decisions this spec makes that the code does not yet pin

The mock server in `tests/contract/cloud/` is a *rendering* of this
document, never its source. Where the current code did not already answer a
question the HTTP binding raises, the answer was decided here first. Those
answers, listed for review:

1. **Duplicate-event reply** (§6): client MUST accept either a replayed
   `accepted` or a `DUPLICATE_EVENT` rejection as success. Forced by the two
   in-repo stores already disagreeing.
2. **HTTP status mapping** (§5): `400` malformed batch, `401`/`403` auth,
   `413` oversize, `429` → `RATE_LIMITED`, `5xx` → `STORE_UNAVAILABLE` /
   `INTERNAL_ERROR`; `200` always means "batch processed", never
   "all accepted".
3. **Minimum batch size of 500 elements** (§7.5), against the SDK's default
   flush threshold of 100.
4. **No cross-element atomicity** (§7.3) — stated explicitly because the
   opposite (transactional batch) is the more common default elsewhere.
5. **Servers must not reject gaps or dangling `prev_action_hash`** (§8.3).
   The consequence of ADR-013 Decision 4a that is easiest to implement
   backwards, and the most destructive to get wrong.
6. **Cloud-mode chain fields are optional additive payload fields** (§8.2),
   not a new event type or a separate header — with the `extra="forbid"`
   forward-compatibility caveat stated in §3.6.
7. **Store-first upgrade ordering** for any minor addition to a forbidding
   payload (§9.2). Follows from §3.6, but it is a release-process
   commitment, not just a fact about the validators.
8. **An unrecognized `error_code` is handled by the wire `retryable` flag**
   (§9.3), which is what lets the registry grow on a minor bump.
9. **`sync` never rewrites `schema_version`**, and a store SHOULD keep
   accepting the previous major while clients may still hold spool under it
   (§9.5). No cutoff duration is fixed here — only the requirement to
   announce one before enforcing it.
