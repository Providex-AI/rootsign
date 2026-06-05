# Security Policy

## Supported versions

| Version | Supported |
| ------- | --------- |
| 0.1.x   | Yes       |

## Reporting a vulnerability

**Do NOT open a public GitHub issue for security vulnerabilities.**

Use GitHub's private vulnerability reporting:

  https://github.com/Providex-AI/rootsign/security/advisories/new

Or email: **info@getprovidex.com**

You will receive acknowledgement within 48 hours. We target a patch release within 14 days for critical issues.

## Scope

**In scope:** vulnerabilities in the RootSign SDK, IngestHandler, or CLI that could allow:

- Tampering with audit records without breaking the hash chain
- Bypassing hash chain verification (`verify_chain`)
- Leaking redacted PII through the SDK
- Forging Approval / Decision records or escalation chains
- Unauthorized writes to the canonical data model

**Out of scope:**

- Issues in third-party dependencies — report upstream first; if RootSign needs to pin or work around, open a security advisory here as a tracking issue once the upstream report exists
- Theoretical attacks with no practical exploit path
- Denial-of-service against single-tenant local deployments (the Phase 1 storage layer assumes a trusted operator)
- Social engineering of maintainers

## Disclosure

Once a fix is released, we will publish a CVE-style advisory describing the issue, affected versions, and the fix. We credit reporters by name (and link, if provided) unless you ask us not to.
