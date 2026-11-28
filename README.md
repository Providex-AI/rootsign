# RootSign

**Tamper-evident provenance logging for production AI agents.**

[![PyPI](https://img.shields.io/pypi/v/rootsign?cacheSeconds=300)](https://pypi.org/project/rootsign/)
[![Downloads](https://img.shields.io/pypi/dm/rootsign)](https://pypi.org/project/rootsign/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)
[![CI](https://github.com/Providex-AI/rootsign/actions/workflows/ci.yml/badge.svg)](https://github.com/Providex-AI/rootsign/actions/workflows/ci.yml)
[![Stars](https://img.shields.io/github/stars/Providex-AI/rootsign?style=flat&logo=github)](https://github.com/Providex-AI/rootsign/stargazers)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Docs](https://img.shields.io/badge/docs-docs.getprovidex.com-blue)](https://docs.getprovidex.com)
[![X](https://img.shields.io/badge/X-@getprovidex-black?logo=x)](https://x.com/getprovidex)
[![LinkedIn](https://img.shields.io/badge/LinkedIn-Providex-0A66C2?logo=linkedin)](https://www.linkedin.com/company/providex)

<p align="center">
  <img src="https://raw.githubusercontent.com/Providex-AI/rootsign/main/docs/demo.gif" alt="RootSign demo: pip install rootsign, two traced tool calls, rootsign verify --local reports VALID, then TAMPERED for an edited record and INCOMPLETE for a deleted one" width="780" />
</p>

RootSign is an open-source Python SDK that records each tool call your AI agent makes as a hash-chained record. When something goes wrong, such as a wrong refund or a leaked record, you can show what the agent did, in what order and on whose approval, and `rootsign verify` tells you whether anyone changed the record afterwards.

Each record carries the SHA-256 hash of the one before it, so editing a hashed field breaks the chain at that record. You add three calls around your existing code; your agent logic does not change.

> RootSign is a Providex AI product: the agent capture layer of the Providex AI Agent Accountability Platform.

**Documentation: [docs.getprovidex.com](https://docs.getprovidex.com)**

## Quickstart

A verified hash chain in under a minute, with no database, no Docker and no API key. You need Python 3.11 or 3.12.

```bash
pip install rootsign
```

Save this as `agent.py`. The three numbered lines are the only RootSign code:

```python
import asyncio, rootsign

rootsign.init(agent="invoice-agent", risk_tier="high")        # 1. once, at startup

@rootsign.trace()                                            # 2. per tool
async def send_invoice(customer_id: str, amount: float) -> str:
    return "sent"

@rootsign.trace()
async def log_payment(customer_id: str, amount: float) -> str:
    return "logged"

async def main():
    async with rootsign.session(objective="invoice ACME") as ctx:   # 3. per run
        await send_invoice("acme-corp", 1500.00)
        await log_payment("acme-corp", 1500.00)
    print(ctx.session_id)

asyncio.run(main())
```

Run it. It prints the session ID; use yours in place of the one below:

```bash
python agent.py
rootsign verify --local ~/.rootsign/sessions/88f15492-3be7-4f7e-af16-3f247b1134a9.jsonl
```

```text
VALID ✓  —  2 records, chain intact
  Session:  88f15492-3be7-4f7e-af16-3f247b1134a9
  Seal:     sealed at record 2
```

Edit a record and `verify` reports `TAMPERED` with exit code `1`; delete one from the middle and it reports `INCOMPLETE` with exit code `2`. The [quickstart](https://docs.getprovidex.com/quickstart) walks through both and turns the session into an [evidence bundle](https://docs.getprovidex.com/guides/evidence-bundles) for a reviewer. [How it works](https://docs.getprovidex.com/how-it-works#what-verify-does-not-check) lists what `verify` cannot detect.

## Documentation

| Section | Pages |
| --- | --- |
| Get started | [Introduction](https://docs.getprovidex.com) · [Quickstart](https://docs.getprovidex.com/quickstart) · [How it works](https://docs.getprovidex.com/how-it-works) |
| Integrations | [LangGraph and LangChain](https://docs.getprovidex.com/integrations/langgraph) · [CrewAI](https://docs.getprovidex.com/integrations/crewai) · [MCP](https://docs.getprovidex.com/integrations/mcp) · [Any Python function](https://docs.getprovidex.com/integrations/python) |
| Guides | [Verify a session](https://docs.getprovidex.com/guides/verify) · [Export an evidence bundle](https://docs.getprovidex.com/guides/evidence-bundles) · [Require human approval](https://docs.getprovidex.com/guides/human-approval) · [Redact PII](https://docs.getprovidex.com/guides/redaction) · [Capture decisions](https://docs.getprovidex.com/guides/decision-capture) · [Run on PostgreSQL](https://docs.getprovidex.com/guides/postgres) · [Without init()](https://docs.getprovidex.com/guides/without-init) · [Batch writes](https://docs.getprovidex.com/guides/batching) · [Cloud and sync (preview)](https://docs.getprovidex.com/guides/cloud-and-sync) |
| Reference | [Python API](https://docs.getprovidex.com/reference/python-api) · [Configuration](https://docs.getprovidex.com/reference/configuration) · [Errors](https://docs.getprovidex.com/reference/errors) · [`rootsign` CLI](https://docs.getprovidex.com/reference/cli-rootsign) · [`rootsign-admin` CLI](https://docs.getprovidex.com/reference/cli-rootsign-admin) · [Evidence bundle format](https://docs.getprovidex.com/reference/evidence-bundle) · [Ingest spec v1](https://docs.getprovidex.com/reference/ingest-spec-v1) |
| Project | [Framework support](https://docs.getprovidex.com/resources/framework-support) · [Changelog](https://docs.getprovidex.com/resources/changelog) · [Roadmap](https://docs.getprovidex.com/resources/roadmap) · [Design decisions](https://docs.getprovidex.com/resources/design-decisions) · [Security and contributing](https://docs.getprovidex.com/resources/security) |

Runnable apps for each integration are in [`examples/`](https://github.com/Providex-AI/rootsign/tree/main/examples).

## Status

RootSign is at **v0.4.0** and classified as **alpha**. The five calls `init`, `session`, `wrap_tools`, `trace` and `verify` are the stable public surface. Cloud mode is a preview: the SDK's HTTP transport and offline spool ship today, but the hosted ingest service is not live yet. See the [roadmap](https://docs.getprovidex.com/resources/roadmap) for what comes next and the [changelog](https://docs.getprovidex.com/resources/changelog) for what changed.

## Contributing

We welcome contributions. See [CONTRIBUTING.md](https://github.com/Providex-AI/rootsign/blob/main/CONTRIBUTING.md) for development setup, coding standards and the PR process. By submitting a contribution, you agree to the [CLA](https://github.com/Providex-AI/rootsign/blob/main/CLA.md).

Questions, ideas and feedback from using RootSign go in [GitHub Discussions](https://github.com/Providex-AI/rootsign/discussions). For reproducible bugs and concrete feature requests, open a [GitHub issue](https://github.com/Providex-AI/rootsign/issues).

## License

Apache License 2.0. See [LICENSE](https://github.com/Providex-AI/rootsign/blob/main/LICENSE) and [NOTICE](https://github.com/Providex-AI/rootsign/blob/main/NOTICE).

## Security

To report a vulnerability, see [SECURITY.md](https://github.com/Providex-AI/rootsign/blob/main/SECURITY.md). Do **not** open a public GitHub issue.
