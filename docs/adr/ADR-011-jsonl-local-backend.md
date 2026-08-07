# ADR-011: JsonlIngestClient — zero-dependency local backend as the default

- **Date**: 2026-08 (Pre-Phase 2 Sprint A — targets v0.2.0)
- **Status**: Accepted
- **Decider**: Founder
- **Related**: ADR-002 (transport-agnostic client — this is a third
  `IngestClient` implementation), ADR-001 (canonical hash — computed
  identically regardless of backend), ADR-009 (BufferedIngestClient —
  wraps this client unchanged), ADR-006 (redaction — runs before this
  client ever sees a payload), ADR-007 (HiTL — constrained in JSONL
  mode, see Decision 6)

## Context

PRD requirement 1.5 (P0) reads: *"Default append-only JSONL file store.
No external dependency for self-hosted mode."* What shipped in v0.1.x is
the inverse: the only working write path is `LocalIngestClient` →
`IngestHandler` → PostgreSQL + TimescaleDB. A first-time evaluator must
run Docker, start a database container, and apply alembic migrations
before the first Action record exists. JSONL exists only on the *read*
side (`rootsign verify --local`).

This has three costs:

1. **TTFR.** The "< 10 minutes to first record" success criterion is
   unreachable for anyone without Docker running. Competing onboarding
   (Langfuse, Braintrust) is `pip install` + env var.
2. **Package weight.** `sqlalchemy`, `alembic`, `asyncpg`,
   `psycopg2-binary`, and `greenlet` are hard dependencies of the SDK.
   That violates the "< 2MB, zero transitive dependency conflicts" NFR
   and drags a database driver stack into every consumer's resolver —
   including agent pipelines that will never touch our Postgres.
3. **A broken promise in ADR-002.** The failure-isolation rule says
   ingest errors "fall back to the WAL buffer." No WAL buffer was ever
   built. A durable local file writer is that artifact.

The Timescale-first choice was correct for Phase 0 (query APIs, chain
sequencing via `SELECT FOR UPDATE`, Phase 2 dashboard reads). The mistake
was making it the *only* mode and the *default* mode. This ADR fixes the
default without discarding the Postgres path.

## Decisions

### 1. Third `IngestClient` implementation, selected by `ROOTSIGN_BACKEND=jsonl`

`JsonlIngestClient` lives in `rootsign/sdk/jsonl_client.py` and
implements the `IngestClient` ABC (ADR-002). It is a drop-in at every
existing call site, including as the `inner` of `BufferedIngestClient`.
`get_ingest_client()` gains the new value and **`jsonl` becomes the
default**:

```
ROOTSIGN_BACKEND = jsonl      # new default — zero external dependencies
ROOTSIGN_BACKEND = postgres   # previous behavior ("local" accepted as a
                              # deprecated alias through v0.x)
ROOTSIGN_BACKEND = cloud      # Phase 2 HttpIngestClient
```

Flipping the default is a behavior change → this ships as **v0.2.0**,
with a MIGRATION note in the release doc: existing design partners set
`ROOTSIGN_BACKEND=postgres` (one env var) and nothing else changes.

### 2. File layout — one JSONL file per session, plus an agent registry

```
$ROOTSIGN_DATA_DIR/                 # default: ~/.rootsign
├── agents.jsonl                    # append-only agent registry
└── sessions/
    └── <session_id>.jsonl          # every envelope for the session
```

One file per session matches what `rootsign verify --local <path>`
already consumes. **All five event types** are appended as lines
(`SESSION_OPEN`, `DECISION_RECORD`, `ACTION_RECORD`, `APPROVAL_RECORD`,
`SESSION_CLOSE`) so a session file is a complete, self-contained
replay artifact — not just the Action chain. `verify_session_local`
is updated to filter `event_type == "ACTION_RECORD"` before rebuilding
the chain; files produced by older tooling (Actions only, no
event_type field) still verify, preserving backward compatibility.

`agents.jsonl` gives `register_agent` a get-or-create path with no
database: one JSON object per agent keyed on `(name, environment)`.

Agent identity is `(name, environment)` across both backends (see ADR-012
Decision 2). The Postgres schema currently enforces `UNIQUE(name)`
(`uq_agents_name`); aligning it requires the sprint's **one** migration —
drop `uq_agents_name`, add `uq_agents_name_environment`. Because `name` is
globally unique today, every existing row already satisfies the composite
key, so the migration applies with no backfill and no conflict.

### 3. Hashing moves client-side — same canonical formula, same verifier

In Postgres mode the store computes `self_hash` / `prev_action_hash` at
insert time. In JSONL mode there is no store-side compute, so
`JsonlIngestClient` computes both using the **existing canonical
formula** (ADR-001, `rootsign/hashing.py`) before appending. The
written record carries every field `verify_session_local` requires:
`session_id`, `sequence_number`, `self_hash`, `prev_action_hash`, plus
the canonical-hash input fields.

Contract test: the **same session driven through both backends produces
byte-identical canonical hash inputs**, and `rootsign verify` (DB) and
`rootsign verify --local` (file) agree on VALID/TAMPERED for the shared
hash vectors in `scripts/generate_hash_vectors.py`.

### 4. Durability and concurrency — single writer, append-only, fsync on chain-critical records

- File opened with `O_APPEND`; one JSON object per line; partial final
  lines (crash mid-write) are detected and reported by `verify`.
- `fsync` after every `ACTION_RECORD` and `APPROVAL_RECORD` by default
  (`ROOTSIGN_JSONL_FSYNC=chain|always|never`). The chain is the product;
  we do not lose links to the page cache.
- **Single-writer-per-session-file is the documented contract.** The
  in-memory `SessionContext` lock already serializes sequence numbers
  within a process; we do not add cross-process file locking. Teams that
  need multi-process writers, concurrent query, or the Phase 2 dashboard
  graduate to `postgres`. This boundary is the explicit answer to "when
  do I stop using JSONL?" — write it in the README graduation table.

### 5. Idempotency — in-memory per client

The existing `IdempotencyStore` (in-memory) is sufficient: JSONL mode is
single-process by contract (Decision 4). Duplicate `event_id`s within a
process are dropped exactly as in Postgres mode; cross-restart
duplicates are tolerated and flagged by `verify` (duplicate
`sequence_number` ⇒ TAMPERED verdict with a distinct error string).

### 6. HiTL in JSONL mode — synchronous prompt only

The v0.1.x HiTL poll loop (ADR-007) requires a database: the checkpoint
polls the `approvals` table while `rootsign approve` commits from
another process. Without a shared store there is nothing to poll.

JSONL mode therefore supports **synchronous HiTL only** — the original
PRD 1.4 "pause via CLI prompt" design: when `require_approval=True` and
a TTY is attached, the decorator prompts inline
(`approve / reject / note`, via `asyncio.to_thread(input, …)` so the event
loop is never blocked), writes the `APPROVAL_RECORD` line, and continues.
Headless (no TTY) + `require_approval=True` + JSONL backend raises
`HiTLUnsupportedBackendError` **on the first invocation, before the wrapped
tool body runs** — not at decoration/wrap time, because the facade
(ADR-012) resolves the backend/client lazily and it is not known when the
decorator is applied. The error message names the fix
(`ROOTSIGN_BACKEND=postgres`). This is still fail-fast: no tool work
happens before the raise. The async webhook variant and cross-process
`rootsign approve` remain postgres/cloud features.

### 7. Packaging split — DB stack becomes `rootsign[postgres]`

Core install (`pip install rootsign`) depends only on: `pydantic`,
`pydantic-settings`, `typer`, `python-dotenv`. Moved to the new
`postgres` extra: `sqlalchemy`, `alembic`, `asyncpg`,
`psycopg2-binary`, `greenlet`.

This requires import-time discipline: `rootsign.database`,
`rootsign.models.*`, and `rootsign.crud.*` import SQLAlchemy at module
load, and `rootsign.sdk.hitl` / `rootsign.sdk.chain` import crud. The
precedent is ADR-010 Decision 3 (the MCP module's lazy imports): DB-touching
imports move inside the functions that need them, and a missing extra
produces one actionable error:

```
RootSignPostgresExtraRequired: ROOTSIGN_BACKEND=postgres requires the
database extra. Install with:  pip install 'rootsign[postgres]'
```

Guard-rail contract test (mirrors `tests/contract/mcp`): a tox/uv env
with **no extras** must pass `import rootsign`, run the JSONL
quickstart end-to-end, and `rootsign verify --local` the result.

## Consequences

- The quickstart drops from "install Docker, start Timescale, run
  migrations" to `pip install rootsign` + 6 lines of Python. TTFR for
  an evaluator is minutes, on any machine, offline.
- ADR-002's WAL-buffer promise becomes real: Phase 2's
  `HttpIngestClient` can spool to the same JSONL writer when the
  network is down and replay on reconnect — one durable format for
  offline mode, crash recovery, and cloud import.
- The Phase 2 hosted backend gains a free import path: "upload your
  `~/.rootsign/sessions/*.jsonl`" is the demo-to-paid bridge.
- `BufferedIngestClient(JsonlIngestClient(...))` works with zero
  changes — selective buffering logic (ADR-009) is backend-blind.
- Postgres mode is unchanged for design partners; one env var opts back
  in.

## Trade-offs accepted

- **Two hash-compute sites** (client-side for JSONL, store-side for
  Postgres) is real duplication risk. Mitigated by the shared canonical
  formula module and the cross-backend hash-vector contract test — if
  the formulas ever drift, CI fails before a release does.
- **No cross-process HiTL in the default backend.** Acceptable: the
  evaluation persona (JSONL) and the production-approval persona
  (postgres/cloud) are different users at different stages.
- **JSONL is not queryable.** Correct — querying is the Phase 2
  dashboard's job, and the graduation table says so out loud.
