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

## Transitive dependencies

RootSign's framework integrations (`[langgraph]`, `[crewai]`) pull in
larger third-party trees that may carry their own CVEs. Our policy:

1. **CVEs in dependencies whose vulnerable code path is NOT reachable
   from any RootSign code path** are dismissed on the GitHub Dependabot
   board with the reason "Vulnerable code not in execution path" and a
   pointer to this section. Example: the `chromadb` HTTP-server pre-auth
   code injection CVE arrives through `crewai`'s extra, but RootSign
   never imports `chromadb`, never starts its HTTP server, and never
   uses `trust_remote_code=true` — the attack surface lives in the
   user's own infrastructure if they choose to run a ChromaDB server,
   not in RootSign.
2. **CVEs in dependencies that ARE in our execution path** get a pin
   bump in the next patch release. If no upstream fix exists, we pin to
   a non-vulnerable predecessor or remove the dependency.
3. **Test-only dependencies** (anything in the `[test]` / `[dev]` extras,
   not in runtime deps) follow the same rule but with reduced urgency
   because end-users don't install them.

If you believe we've misclassified a transitive CVE (i.e. the
vulnerable code path IS reachable from RootSign), file it via the
private vulnerability reporting flow above. Don't open a public issue.

## Disclosure

Once a fix is released, we will publish a CVE-style advisory describing the issue, affected versions, and the fix. We credit reporters by name (and link, if provided) unless you ask us not to.
