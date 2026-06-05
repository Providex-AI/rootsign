# Contributing to RootSign

## Welcome

RootSign is the open-source agent capture layer of the Providex AI Agent Accountability Platform. We welcome contributions of all kinds: bug fixes, documentation improvements, new framework integrations, and test coverage.

## Before you contribute

- **Code of Conduct.** RootSign adopts the [Contributor Covenant v2.1](https://www.contributor-covenant.org/version/2/1/code_of_conduct/) as its code of conduct. By participating in this project (issues, PRs, discussions, Discord), you agree to uphold its standards. Report unacceptable behavior to `info@getprovidex.com`.
- Sign the [CLA](CLA.md) (automated — you'll be prompted on your first PR)
- Check the issue tracker for existing discussion before opening a new issue

## Development setup

```bash
git clone https://github.com/Providex-AI/rootsign
cd rootsign
pip install -e '.[dev]'          # installs dev + test deps
docker-compose up -d db          # PostgreSQL + TimescaleDB
rootsign-admin init              # alembic upgrade head
pytest tests/unit/ -v            # unit tests (no DB needed at runtime — see note)
pytest tests/integration/ -v     # integration tests (needs DB)
```

> **Python:** RootSign requires Python 3.11 or 3.12. We do not support 3.10 or below.
>
> **Note on unit tests:** The session-scoped `_bootstrap_test_db` fixture in `tests/conftest.py` runs alembic against the test DB before any test (including unit tests). Bring `docker-compose up -d db` up first.

## Branch naming

```
feat/[short-description]         # new feature
fix/[short-description]          # bug fix
test/[short-description]         # test additions
docs/[short-description]         # documentation only
```

## Commit message format

```
type(scope): short description
```

Examples:
```
feat(sdk): add CrewAI tool wrapper
fix(hashing): correct canonical field order
test(crud): add verify_chain corruption test
docs(adr): add ADR-004 for retry strategy
```

## Pull request requirements

- All existing tests must pass: `pytest tests/ -v`
- New code must have test coverage >= 90%
- Overall coverage must not drop below 85% (enforced by `fail_under = 85` in `pyproject.toml`)
- No breaking changes to the canonical hash spec without a new ADR (see [ADR-001](docs/adr/ADR-001-hash-canonical-spec.md))
- Framework integrations must pass contract tests on all supported framework versions (see CI matrix)
- Run `ruff check .` and `ruff format .` before pushing

## What we will NOT merge

- Changes to `compute_action_self_hash` canonical spec (see [ADR-001](docs/adr/ADR-001-hash-canonical-spec.md)) without a new ADR approved by the maintainer
- Synchronous SQLAlchemy in any non-Alembic code
- Mock-based integration tests (real PostgreSQL + TimescaleDB only — see [the data model rationale](AGENTS.md))
- Any PR that reduces test coverage below 85% overall
- Code that swallows ingest failures silently (RootSign's promise is that ingest never raises into the agent, but failures *must* be logged at WARNING level — see [ADR-002](docs/adr/ADR-002-transport-agnostic-client.md))

## Adding a new framework integration

1. Open a `framework_integration` issue first so we can discuss API surface and version targets
2. Copy the LangGraph integration as a reference (lands in Sprint 2)
3. Implement contract tests against a minimum of two framework versions (latest stable + previous minor) — see [ADR-003](docs/adr/ADR-003-framework-contract-tests.md)
4. Update `docs/framework-support.md` (created in Sprint 2)

## Questions?

Open a [GitHub Discussion](https://github.com/Providex-AI/rootsign/discussions). A `#rootsign` channel on the LangChain Discord is coming alongside Sprint 2.
