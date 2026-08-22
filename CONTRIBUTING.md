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
python -m pytest tests/unit/ -v          # unit tests (no DB needed at runtime — see note)
python -m pytest tests/integration/ -v   # integration tests (needs DB)
python -m pytest -m benchmark -v         # performance budgets (opt-in — see note)
```

> **Python:** RootSign requires Python 3.11 or 3.12. We do not support 3.10 or below.
>
> The package's `requires-python` is `>=3.11,<3.15`, but the `[crewai]` extra currently lacks wheels for 3.13/3.14 so installs of `'.[crewai]'` on those versions fail with `No matching distribution found`. Bump the recommendation only after upstream ships matching wheels.
>
> **Always invoke pytest as `python -m pytest`.** If you `brew install`-ed pytest, the system binary will resolve ahead of the venv's pytest on PATH and run under the system Python — which does not see your venv's site-packages and will fail with confusing `ModuleNotFoundError` (typically on `sqlalchemy` first). `python -m pytest` always uses the venv's interpreter.
>
> **Note on unit tests:** The session-scoped `_bootstrap_test_db` fixture in `tests/conftest.py` runs alembic against the test DB before any test (including unit tests). Bring `docker-compose up -d db` up first.
>
> **Performance benchmarks are opt-in.** Everything under `tests/performance/` is marked `benchmark` and is **skipped** by a default `python -m pytest` run; pass `-m benchmark` to execute it. These tests assert wall-clock budgets, so they are the most hardware-sensitive part of the suite — `test_1000_actions_under_2_seconds` has only ~1.36x headroom on a developer laptop. They are deliberately kept out of every CI gating job: a timing budget that reddens `main` for reasons unrelated to the diff teaches people to ignore red. Run them locally (or on purpose in CI) when touching the ingest, hashing, or verify paths.
>
> **The thresholds are ACs and do not move.** To keep them honest without making them flaky, each budget is sampled several times and asserted on the **median** (`tests/performance/_bench.py`). A single stalled run barely shifts a median, while a real regression shifts every sample — so the AC stays exactly as specified and the measurement stops reacting to background load, cold caches, or a noisy neighbour. Failures print every sample, not just the median, so you can see the spread. `test_p99_overhead_under_5ms` is the exception: it already asserts on the p99 of 1,000 calls, so it is a distribution by construction.
>
> These numbers are still **hardware- and environment-dependent** — they measure your disk, your Postgres tuning, and whatever else the machine is doing. Treat a failure as a prompt to investigate on an idle machine, not as proof of a regression. For scale: a trial run of `test_1000_actions_under_2_seconds` produced samples of 1.6s / 2.4s / 1.4s on an idle laptop; the 2.4s sample alone exceeds the 2.0s budget, which is exactly why the median exists.

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

### Golden fixtures

Three fixtures in `tests/fixtures/` freeze contracts that other people's code
depends on. A failure against one is a decision, not a bug:

| Fixture | Freezes | If it fails |
| --- | --- | --- |
| `hash_vectors.json` | the canonical Action hash (ADR-001) | stop — a new ADR is required, see below |
| `redaction_vectors.json` | the PII rule-set mappings (ADR-006) | confirm the mapping change is intended |
| `evidence_bundle_v1.json` | the `rootsign export` bundle schema (ADR-014) | see below |

The bundle schema is read by Phase 2 tooling and by anything a partner builds
on an exported bundle. **Additive** changes (a new optional field, content in
the reserved `compliance` block) are fine — regenerate and commit the diff:

```bash
ROOTSIGN_UPDATE_GOLDEN=1 python -m pytest tests/unit/test_export_golden.py
```

Renaming or removing a field is a **bundle version bump**: change
`EVIDENCE_BUNDLE_VERSION` in `rootsign/sdk/export.py` in the same PR, and say
why in the description. Never regenerate a fixture to make a red test green
without first deciding which of the two you are doing.

## What we will NOT merge

- Changes to `compute_action_self_hash` canonical spec (see [ADR-001](docs/adr/ADR-001-hash-canonical-spec.md)) without a new ADR approved by the maintainer
- Synchronous SQLAlchemy in any non-Alembic code
- Mock-based integration tests (real PostgreSQL + TimescaleDB only — see [the data model rationale](AGENTS.md))
- Any PR that reduces test coverage below 85% overall
- A regenerated `evidence_bundle_v1.json` that renames or removes a field without a matching `EVIDENCE_BUNDLE_VERSION` bump (see [ADR-014](docs/adr/ADR-014-export-evidence-bundle.md))
- Code that swallows ingest failures silently (RootSign's promise is that ingest never raises into the agent, but failures *must* be logged at WARNING level — see [ADR-002](docs/adr/ADR-002-transport-agnostic-client.md))

## Adding a new framework integration

1. Open a `framework_integration` issue first so we can discuss API surface and version targets
2. Copy the LangGraph integration as a reference (lands in Sprint 2)
3. Implement contract tests against a minimum of two framework versions (latest stable + previous minor) — see [ADR-003](docs/adr/ADR-003-framework-contract-tests.md)
4. Update `docs/framework-support.md` (created in Sprint 2)

## Questions?

Open a [GitHub Discussion](https://github.com/Providex-AI/rootsign/discussions). A `#rootsign` channel on the LangChain Discord is coming alongside Sprint 2.
