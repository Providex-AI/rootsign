# ADR-007: HiTL checkpoint — poll loop, timeout, sequence policy

- **Date**: 2026-06 (Phase 1, Sprint 4)
- **Status**: Accepted
- **Decider**: Founder
- **Related**: ADR-001 (hash canonical spec), ADR-002 (transport-agnostic
  client), ADR-004 (LangGraph), ADR-005 (CrewAI), ADR-006 (redaction)

## Context

The HiTL (human-in-the-loop) checkpoint pauses agent execution until a
human authorizes or rejects a pending tool call. It is the last core
Phase 1 feature and the gate between auto-authorized agent behavior and
high-stakes actions a compliance officer needs to sign off on (large
transfers, customer-facing notifications, irreversible writes).

Designing the checkpoint surfaces four questions that drive every line of
the implementation. Each was a real fork in the road; resolving them
explicitly here means the next reader doesn't have to re-derive the
trade-offs.

1. Where does the approval wait actually *live*?
2. What happens when nobody responds?
3. How does the hash chain stay intact while the human takes their time?
4. How does the Approval row link to the Action row when the Action row
   is in a TimescaleDB hypertable?

The decisions below are binding across Sprint 4 code and pin the surface
that Phase 2 (cloud backend, multi-tenant) must continue to support.

## Decision

### Wait mechanism: async poll loop inside the SDK

`HiTLCheckpoint` is an asyncio class. `wait_for_approval()` runs a poll
loop that fires every `poll_interval_seconds` (default 2.0) until either
an Approval row appears or `timeout_seconds` elapses (default 300, i.e.
5 minutes). Each poll cycle opens its **own** AsyncSession via the
caller-supplied `session_factory`; sessions are never reused across the
boundary.

Why per-cycle sessions: asyncpg attaches every Future to the event loop
that created it. pytest-asyncio creates a fresh loop per test, and the
`rootsign approve` CLI runs in a different process / asyncio.run scope
entirely. Reusing one session for the whole wait would lock the
checkpoint to whichever loop opened it, breaking both tests and the
external CLI's READ-COMMITTED visibility. Per-cycle sessions cost ~one
asyncpg connect every two seconds — negligible.

The human responds via `rootsign approve <action_id>` on the user CLI
(or `rootsign approve <id> --reject [--reason "..."]`). Naming contract
pinned: `approve` is on the **user** CLI (`rootsign`), not the operator
CLI (`rootsign-admin`). Replay-pending is the operator-CLI name reserved
for Phase 2.

### Timeout: TIMED_OUT terminal state (distinct from REJECTED)

When the deadline elapses with no human response, the SDK writes an
APPROVAL_RECORD with `approver_type='timeout_auto_rejected'` and flips
`Action.authorization_status` to `'timed_out'`. `'timed_out'` is a new
member of the `ck_actions_authorization_status` CHECK constraint added
in Alembic migration `0003_action_timed_out`.

`HiTLTimeoutError` is raised to the caller. The tool **does not** run.

The forensic distinction between `'human_rejected'` and `'timed_out'`
survives in the audit trail forever. A compliance review can ask "show
me every action that timed out last quarter" without conflating "a human
said no" with "nobody responded." That's the whole point of having two
terminal states instead of collapsing both to REJECTED.

### Sequence number reserved at submission

The ACTION_RECORD is inserted with a reserved sequence number AND
`authorization_status='pending'` at the moment the tool call is
intercepted — BEFORE the human is asked. The sequence number does not
change when the Approval arrives.

Consequence: `verify_chain` sees a complete, gap-free chain regardless
of how long the human took. The chain integrity story is exactly the
same as for auto-authorized actions; HiTL adds zero verification
complexity. ACTION_RECORDs with `'pending'` or `'timed_out'` status are
chain-valid by construction.

The Action's `output_hash` and `output_redacted` columns are NULL for
HiTL-gated actions and stay NULL after the human responds. `self_hash`
is computed once at insert time over `(input_hash, output_hash=None,
prev_action_hash)`. Updating `output_hash` post-hoc would invalidate
`self_hash` and break the chain. Output capture for HiTL actions is
deferred to Phase 2 (will require a separate write-back RPC and a
re-hash policy decision).

### Approval→Action linkage: logical only (hypertable constraint)

The `actions` table is a TimescaleDB hypertable with composite primary
key `(action_id, timestamp)` — the partition column must be part of every
unique index. A single-column SQL foreign key from `approvals.action_id`
to `actions.action_id` is therefore impossible at the database layer
(the target is not unique on action_id alone).

Linkage is enforced in Python by `CRUDApproval.create_with_chain_link`,
which requires `action_timestamp` as a kwarg and joins on `(action_id,
action_timestamp)` to locate the Action row. Single-column WHEREs against
the hypertable are forbidden — the planner cannot prune chunks and the
"unique row" assumption goes wrong silently. This is pinned by the
"hypertable lookup" assertion in `tests/unit/test_crud_approval.py`
which compiles the SELECT and confirms both columns appear in the WHERE.

### Race tolerance: the human always wins ties

A human's `rootsign approve` commit can land in the same window as the
poll loop's timeout fires. Two transactions racing on the same Action;
whichever commits last hits the terminal-state guard in
`create_with_chain_link` and raises `ActionAlreadyResolvedError`.

`HiTLCheckpoint._record_timeout` catches that exception, re-polls one
last time, and returns the human's Approval row to `wait_for_approval`.
The decision the caller sees is the human's, not a spurious
`HiTLTimeoutError`. The forensic distinction holds: a forensically
distinct "human said yes at the last second" wins over "the system gave
up." Pinned by `TestHumanWinsRace` in `tests/unit/test_hitl.py`.

### v0.1.0 scope limits

These three limitations are deliberate and live in the code's
docstrings; revisit in Phase 2:

* **Plain async callables only.** `@rootsign.trace(require_approval=True)`
  applied to a LangChain `BaseTool` or a CrewAI tool raises
  `NotImplementedError` with the message "wrap the underlying function."
  Half-implementing the framework paths would silently degrade to
  auto-authorized — an explicit error is safer than a quiet correctness
  hole.
* **Output not chain-captured.** See "Sequence number reserved at
  submission" above. The tool's return value reaches the caller; the
  hash chain just doesn't certify the output for HiTL actions.
* **No envelope re-emit on approval.** When the human approves via the
  CLI, the CLI writes the APPROVAL_RECORD row directly via
  `create_with_chain_link`. The trace decorator's post-approval path
  does NOT additionally call `_emit_approval_record`. Re-emitting would
  hit the same terminal-state guard and produce a logged warning for no
  semantic gain. Phase 2's cloud backend will need the envelope route,
  which is why `_emit_approval_record` exists, is tested, and is wired
  for future use.

## Consequence

* Pending actions survive SDK restarts: they live in the database, not
  in process memory. A long-running approval (3am pager → next-morning
  review) is unaffected by SDK redeploys.
* The poll loop is bounded — no runaway waits, no zombie agents. The
  caller can pass shorter intervals/timeouts for low-latency flows.
* `'timed_out'` is distinguishable from `'human_rejected'` in audit
  trails forever. Compliance reports can show `auto_authorized`,
  `human_approved`, `human_rejected`, `timed_out` counts per agent /
  per tool / per time window.
* The hash chain is unaffected by HiTL timing: same `verify_chain`,
  same VALID/TAMPERED verdict, regardless of whether actions were
  auto-authorized, human-approved, or timed-out.
* HiTL adds ~one asyncpg connect per `poll_interval_seconds` for the
  duration of the wait. With default 2s polling and 5-minute timeout,
  worst-case ~150 connects per pending action — well within asyncpg's
  per-loop budget.

## Alternatives rejected

- **Blocking `input()` in the SDK.** Rejected. Breaks every
  non-interactive deployment (Docker containers, systemd timers,
  Airflow tasks, CI runners). The non-interactive case is the realistic
  production shape; interactive prompts are the development edge case.
- **Webhook callback instead of polling.** Considered. Would shrink
  latency from N×`poll_interval` to ~0, but adds an inbound listener
  surface, a webhook URL, and request signing — out of scope for v0.1.0
  and unnecessary while design partners are exploring HiTL at the
  several-seconds-of-wait scale anyway.
- **Collapsing TIMED_OUT into REJECTED.** Rejected. Loses the
  "nobody-looked-at-it" vs "human-said-no" forensic distinction. A
  single terminal state would make every "why didn't this fire?"
  compliance question harder to answer.
- **Re-issue sequence numbers after approval.** Rejected. Would mean
  the chain has gaps until approval lands, breaking `verify_chain`'s
  contiguity invariant. The current "reserve at submission, fill in
  status later" preserves chain-VALID at every point in time.
- **Adding a SQL foreign key from `approvals.action_id` to
  `actions.action_id`.** Impossible at the DB layer — the target is
  inside a hypertable's composite PK. Application-layer enforcement in
  `create_with_chain_link` is the only path.
- **Operator-CLI-only `approve` command** (i.e. ship `rootsign-admin
  approve` instead). Rejected. Approving a HiTL action is a
  developer/operator workflow, not a privileged schema operation.
  Reserving it for the operator CLI would gate the demo path behind
  privilege the design partner doesn't have.

## Verification

- Unit tests: `tests/unit/test_hitl.py` (HiTLCheckpoint timeout,
  approved, rejected, race tolerance, constructor defaults),
  `tests/unit/test_crud_approval.py` (hypertable-safe lookup, terminal
  set including `'timed_out'`, timeout-sentinel mapping),
  `tests/unit/test_emit_approval_record.py` (envelope shape, no
  sequence number, failure isolation), `tests/unit/test_trace_hitl.py`
  (gate semantics — tool does not run before approval, runs exactly
  once after; framework paths reject `require_approval=True`).
- Integration tests: `tests/integration/test_approve_cli.py` (full
  pending → approve / reject / list flow against the test DB).
- Schema: Alembic migration `0003_action_timed_out` adds `'timed_out'`
  to `ck_actions_authorization_status`; forward-only per project
  policy. `tests/integration/test_crud.py` and `test_relationships.py`
  pass against the widened constraint.
- Show HN reproducibility:
  `tests/integration/test_show_hn_quickstart.py` — three tool calls,
  one verify CLI invocation, VALID ✓ output. Locks the README quickstart
  shape against SDK refactors and is the Sprint 4 DoD §5.4 item 11 gate
  before the Show HN post goes live.
