# ADR-006: Redaction contract — what is guaranteed before hashing

- **Date**: 2026-05 (Phase 1, Sprint 3)
- **Status**: Accepted
- **Decider**: Founder
- **Related**: ADR-001 (hash canonical spec), ADR-004 (LangGraph), ADR-005 (CrewAI)

## Context

rootsign hashes tool inputs and outputs before storing them. If PII is not
redacted before hashing, the hash is a fingerprint of PII —
re-identification is possible by hashing known PII values and comparing.
This is a GDPR Article 4(5) concern: a hash of `alice@acme.com` is still
personal data when the attacker has the candidate value space.

The Sprint 1 `RedactionConfig` was a working scaffold. Sprint 3 hardens
it and binds the redaction-before-hashing pipeline into a load-bearing
contract: any code path that stores or transmits the hash MUST have
applied redaction first, and the test suite enforces this with golden
vectors.

## Decision

Redaction runs **before** hashing. The pipeline is, strictly:

```
raw_payload → redact() → redacted_payload → compute_payload_hash()
```

The hash is always computed on the **redacted** payload, never the raw
payload. Stored `input_hash` and `output_hash` values therefore carry no
PII signal.

This is enforced in two places:

1. **Implementation** — `_emit_action_record` in
   `rootsign/sdk/decorator.py` applies `redaction_config.redact(...)` and
   feeds the *result* to `compute_payload_hash(...)`. There is no
   intermediate "hash the raw input then redact" path.
2. **Test surface** — `tests/fixtures/redaction_vectors.json` pins
   `(config, input) → expected_redacted_output` for each documented PII
   pattern, and `tests/unit/test_redaction_pii.py` asserts that two
   distinct PII inputs collide on hash after redaction (proving the hash
   does not encode the PII), while *raw* inputs do not collide (proving
   the test isn't trivially true).

### Default behaviour (no `RedactionConfig` supplied)

No redaction. Raw payload is hashed. The customer is responsible.
`StandardPIIConfig()` is a one-line convenience for the common case.

### Nested field support

`RedactionConfig` walks dicts recursively with dot-path keys:
`{"user.email": ".*@.*"}` redacts `payload["user"]["email"]`. Lists are
walked item-by-item, items keep the parent's path for rule lookup. Depth
is capped at `MAX_REDACTION_DEPTH = 5` to bound recursion on adversarial
payloads — anything deeper is returned unchanged. Five levels covers every
realistic tool payload (LangChain / CrewAI tools rarely nest beyond 2–3).

### Standard PII configs

* `StandardPIIConfig` — email, phone, US SSN, credit card, UK NI number.
* `FinancialPIIConfig` — Standard + account number, routing number, IBAN.
* `HealthcarePIIConfig` — Standard + MRN, NPI, date of birth.

Each accepts `extra_rules={}` so design partners can extend without
subclassing. The patterns are pragmatic — they aim to catch the common
shape and accept some false positives over false negatives. Tighten per
design-partner feedback as real PII patterns surface.

### Type safety

The redactor silently skips non-string values when a rule's path matches
but the value isn't a string. Applying a regex to a number would throw;
silently passing through is the safer default for a tool-input payload
that may include numeric IDs and string fields under the same path.

## Consequence

Two users with the same email who send the same tool input produce the
same `input_hash` **only if** neither has additional PII in other fields.
That is acceptable — the hash is not a unique identifier of the person,
only of the (redacted) input structure.

The customer carries no PII liability for stored `input_hash` /
`output_hash` values when they use `StandardPIIConfig` (or a stricter
config) and their PII matches the configured patterns. PII that escapes
the regexes — typos, unusual formats, custom identifiers — still reaches
the hash. The customer is responsible for extending the config to cover
their domain.

## Alternatives rejected

- **Hash raw, redact for storage only.** Rejected — the hash is what
  travels in WAL, audit logs, and Phase 2 cloud telemetry. Hashing raw
  leaves PII fingerprints in every downstream system.
- **Field allow-list (only hash named fields).** Considered but premature.
  Customers building tools want their fields hashed by default; a
  deny-list of PII patterns matches the way they think about the
  problem.
- **Unbounded recursion.** Rejected. Five-level depth limit catches
  realistic payloads and refuses pathological inputs without changing the
  contract surface.

## Verification

- Unit tests: `tests/unit/test_redaction_pii.py` — instantiation,
  per-pattern coverage, nested + list traversal, depth limit, and the
  golden-vector contract.
- Golden vectors: `tests/fixtures/redaction_vectors.json` — pinned
  `(config, input) → expected` mappings. Editing this file is a contract
  change and demands an ADR amendment.
- Sprint 1 baseline: `tests/unit/test_redaction.py` continues to pass
  (no regression on dot-path and edge-case semantics).
