# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## NEVER commit or push project/sprint artifacts

Sprint plans, agent guides, internal audits, memory snapshots, and similar working documents **must stay local**. They are gitignored as a backstop, but the rule is the rule regardless of whether `.gitignore` catches a given filename:

- `AGENTS*.md`, `AGENTS_Phase*.md`, `RootSign_Phase*.{md,docx}`, `ProvidexAI_Phase*.{md,docx}`
- `MEMORY.md` and anything under a `memory/` directory
- `rootsign_review.md` and other founder-run audit artifacts

If a new artifact like this gets created during a session, treat it as local-only by default. Do not `git add`, do not stage, do not include in commits. If a `git add -A` / `git add .` would sweep one in, switch to per-file staging. If a similar file pattern appears that isn't already gitignored, add the pattern to `.gitignore` in the same change.

**Why:** these documents contain unreleased product strategy, audit findings, and pre-decision context. A Sprint 4 history-rewrite was needed once to scrub an accidentally-tracked sprint plan — the gitignore patterns added in commit 128cec5 exist as a mechanical backstop. The rule is to never need that backstop.

## Where the code actually lives

The Python project is `phase0/` (this directory). The parent `turbo/` is just a workspace wrapper. All paths below are relative to `phase0/`.

## Dev environment

CONTRIBUTING.md has the full setup. The non-obvious bits:

- **Always run `python -m pytest`, never bare `pytest`.** A brew-installed `pytest` shadows the venv binary and silently runs under the system interpreter — produces `ModuleNotFoundError: sqlalchemy` and similar.
- **Even unit tests need a running database.** `tests/conftest.py` has a session-scoped `_bootstrap_test_db` fixture that runs alembic before any test collection. Start the DB first: `rootsign-admin start-db` (or `docker-compose up -d db` in-repo).
- **Schema bootstrap:** `rootsign-admin init` applies migrations. Idempotent. `--reset` drops and recreates the public schema (no confirmation prompt — be careful).
- **Single test:** `python -m pytest tests/integration/test_show_hn_quickstart.py::TestShowHNQuickstart::test_readme_quickstart_end_to_end -v`
- **Test markers:** `integration` and `benchmark` are declared in `pyproject.toml`. Performance suite at `tests/performance/` is opt-in via `-m benchmark`.
- **Coverage gate:** `fail_under = 85` in `pyproject.toml`. CI enforces it. New code expected at ≥90% per CONTRIBUTING.
- **Lint:** `ruff check .` and `ruff format .` before pushing.

## Two CLIs — do not flip them

Naming is locked (sprint plan Flag, do not re-derive):

| Surface | Entry point | Commands |
|---|---|---|
| **User CLI** `rootsign` | `rootsign.sdk.cli:app` | `verify`, `approve`, `approve --list` |
| **Operator CLI** `rootsign-admin` | `rootsign.cli:app` | `start-db`, `stop-db`, `init`, `status` |

`rootsign approve <id>` is a developer workflow on the user CLI. `rootsign-admin replay-pending` (when it lands) is a privileged schema-level operation on the operator CLI. Decorator is `@rootsign.trace`, never `@providex.trace`. Package is `rootsign`, never `providex`.

## Big-picture architecture

**Hash chain (per-session, tamper-evident).** Each `Action` row carries `prev_action_hash` linking to the previous action in its session. `compute_action_self_hash` in `rootsign/hashing.py` is the **frozen canonical spec** — ADR-001 governs it, and CONTRIBUTING refuses to merge changes without a new ADR. Never re-implement it (the local-verify path got this wrong once and silently broke; see commit 128cec5). `self_hash` deliberately excludes `input_redacted`/`output_redacted` — the chain proves hash integrity, not payload-to-hash binding (an open pre-Phase-2 audit item).

**Ingest path.** SDK constructs an envelope → `IngestClient.handle(envelope)` → `IngestHandler` validates and routes by `event_type` → CRUD writes. `LocalIngestClient` is the in-process v0.1.0 path. `HttpIngestClient` is the Phase 2 hosted-backend path. Failure isolation rule (ADR-002): ingest **never raises into the decorated tool**; failures log at WARNING and the tool returns normally.

**HiTL checkpoint (ADR-007 — locked design).** `require_approval=True` on `@rootsign.trace` inserts an `ACTION_RECORD` with `authorization_status='pending'` and a reserved sequence number at submission time, then waits on `HiTLCheckpoint` (async poll loop). On approval: tool runs. Timeout: status becomes `'timed_out'` (terminal, distinct from `'human_rejected'`) and an Approval row is written with `approver_type='timeout_auto_rejected'`. **APPROVAL_RECORD rows are NOT in the hash chain** — they're a separate entity queried independently. The poll loop opens its own `AsyncSession` per cycle (`session_factory=AsyncSessionLocal`), never the caller's.

**Storage.** PostgreSQL 16 + TimescaleDB 2.x. The `actions` table is a **hypertable partitioned by `timestamp`**, which has hard consequences for any code joining against it (see below).

**Migrations.** Live in `rootsign/_migrations/` so they ship inside the wheel. `rootsign-admin init` resolves the directory via `importlib.resources.files("rootsign") / "_migrations"` and builds `alembic.config.Config` programmatically — no `alembic.ini` lookup, no cwd assumption. Developers running `alembic` directly from repo root work via the root-level `alembic.ini` whose `script_location` points at the same directory.

## Test invariants (binding rules)

These were locked in the Sprint 3/4 plans because each one traces to a specific incident. Audit any new SDK or test code against them.

1. **`_emit_action_record` and `_emit_approval_record` are keyword-only.** No positional args.
2. **AsyncSession loop binding:**
   - Tool calls in async tests use `await tool.ainvoke({...})` — never `tool._run(...)` or sync `tool.invoke(...)`.
   - CLI tests in async use `await asyncio.to_thread(runner.invoke, app, [...])` — never bare `runner.invoke(...)`. Typer's runner does `asyncio.run()` internally and crashes inside an already-running loop.
3. **`seeded_agent` (commits) for HiTL / CLI / `asyncio.to_thread` tests.** `registered_agent` uses SAVEPOINT rollback — its data is invisible to the poll loop's per-cycle session, the `to_thread` CLI runner, and the timeout recorder.
4. **HiTL design is locked (ADR-007).** Don't re-derive it in code.
5. **Hypertable-safe Action lookups.** Single-column `action_id` lookups are forbidden. Two valid two-column forms:
   - `(action_id, action_timestamp)` — canonical chain-link form (`CRUDApproval.create_with_chain_link`, HiTL CLI / poll-loop paths). Prunes chunks on the partition column.
   - `(session_id, action_id)` — `CRUDApproval.create_with_action_status_update` (ingest path). `ApprovalRecordPayload` doesn't carry `action_timestamp`; switching would require an envelope-schema bump.
6. **`tests/integration/test_show_hn_quickstart.py` is the launch gate.** It mirrors the README's LangGraph quickstart end-to-end (register → instrument → verify) and must stay green.

## What's deliberately orphaned

- **`_emit_approval_record`** in `rootsign/sdk/decorator.py` has zero call sites in `rootsign/`. It's a Phase 2 hook for when `HttpIngestClient` lands — the v0.1.0 HiTL CLI writes APPROVAL_RECORD rows directly via `CRUDApproval.create_with_chain_link` (single source of truth). Re-emitting via the IngestClient path would hit the terminal-state guard and warn for no semantic gain. The marker comment above the def points at this; don't delete the helper or its tests.

## Authoritative context

When changing anything in the SDK, HiTL, or hashing paths:

- `docs/adr/ADR-001` — canonical hash spec (frozen)
- `docs/adr/ADR-002` — ingest failure isolation
- `docs/adr/ADR-006` — redaction contract
- `docs/adr/ADR-007` — HiTL checkpoint design
- `AGENTS_Phase1_Sprint4.md` — sprint plan with naming contract, six binding flags, ADR-007 summary, the launch review checklist. **Gitignored**, so absent from fresh clones; it's a working doc for whoever's driving the sprint.
- `rootsign_review.md` — pre-launch audit findings. Also gitignored. High-severity items were fixed in commit 128cec5; the remainder is the Pre-Phase-2 backlog (concurrency on approvals, payload→hash binding at verify, HiTL classification edge cases, CLI polish).

## Conventions

- **Commits:** `type(scope): short description` (see CONTRIBUTING). Recent history matches: `fix(packaging):`, `fix(security):`, `feat(sdk):`, `docs:`. Body explains *why*, references file:line, and includes a `Co-Authored-By` trailer when relevant.
- **Branches:** `feat/`, `fix/`, `test/`, `docs/` prefixes.
- **Hard "no":** mock-based integration tests (real PG+TimescaleDB only), sync SQLAlchemy outside Alembic, changing `compute_action_self_hash` without a new ADR, silently swallowing ingest failures (must log at WARNING), committing/pushing project or sprint artifacts (see top section).
