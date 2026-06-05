# RootSign

**Tamper-evident provenance logging for AI agents.**

[![CI](https://github.com/Providex-AI/rootsign/actions/workflows/ci.yml/badge.svg)](https://github.com/Providex-AI/rootsign/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)
[![PyPI - coming soon](https://img.shields.io/badge/PyPI-coming_soon-lightgrey.svg)](https://pypi.org/project/rootsign/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

> *RootSign is a Providex AI product — the agent capture layer of the Providex AI Agent Accountability Platform.*

## What is RootSign?

When AI agents take actions in production — calling tools, hitting APIs, writing to databases — there is no built-in audit trail. If something goes wrong (a wrong refund, a leaked PII record, a malformed deployment), there is no way to prove what the agent did, in what order, on whose authorization, or whether the record has been tampered with after the fact.

RootSign solves this. Each agent action is captured as an `Action` record containing a SHA-256 hash of the previous action — a **cryptographic hash chain** that makes the record tamper-evident. Modify any record after the fact and `rootsign verify` detects it.

Compliance-grade audit trails. Zero changes to your agent code.

## Status

**Phase 0 — pre-MVP.** Canonical data model, storage layer, and ingest spec are complete. The user-facing `@rootsign.trace` decorator ships in Phase 1 Sprint 2.

| Phase | Scope | Status |
|---|---|---|
| 0 | Data model + storage + ingest handler | ✅ Complete |
| 1 | Python SDK (`@rootsign.trace`, LangGraph integration, CLI) | 🚧 In progress |
| 2 | Hosted ingest backend + compliance dashboard | Planned |
| 3 | Policy enforcement + incident workflow | Planned |
| 4 | Cross-platform governance | Planned |

## Quickstart (Phase 0 — developers only)

```bash
git clone https://github.com/Providex-AI/rootsign
cd rootsign
pip install -e '.[dev]'
docker-compose up -d db           # PostgreSQL 16 + TimescaleDB
rootsign-admin init               # alembic upgrade head
pytest tests/ -v                  # 93 tests in ~2s
```

The user-facing `@rootsign.trace` decorator API will land in Sprint 2. See [AGENTS_Phase1_Sprint01.md](AGENTS_Phase1_Sprint01.md) for the current sprint plan.

## Contributing

We welcome contributions. See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, coding standards, and the PR process. By submitting a contribution, you agree to the [CLA](CLA.md).

Open-source community channels and Discord coming soon.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

## Security

To report a vulnerability, see [SECURITY.md](SECURITY.md). Do **not** open a public GitHub issue.
