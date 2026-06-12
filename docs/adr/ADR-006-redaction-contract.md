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

### Matching semantics — leaf-key default with path-key opt-in

**Update (Sprint 4 audit fix).** The original ADR-006 implementation
used path-key matching only: rule `"email"` matched only the top-level
field `email`. That made the pre-built `StandardPIIConfig` effectively
non-functional on the decorator's actual envelope shape
`{"args": [...], "kwargs": {...}}` — a kwarg `email` has path
`kwargs.email`, which a rule keyed `email` did not match.

Current semantics:

* **Bare rule keys** (no dot) → **leaf-key match.** Rule `"email"` fires
  on any field named `email` regardless of nesting depth. Pre-built
  configs (`StandardPIIConfig`, `FinancialPIIConfig`,
  `HealthcarePIIConfig`) now redact PII anywhere in the tree, including
  the decorator's `kwargs.*` envelope.

* **Dotted rule keys** → **exact-path match.** Rule `"user.email"`
  still fires only at the exact path `user.email`. Backward-compatible
  with custom configs from earlier sprints.

* **`match_mode="path"` constructor argument** → opt-in strict mode.
  Every rule key is treated as a full path, even bare names. Useful
  when you need to distinguish `audit.email` from `user.email` and
  don't want a bare `email` rule to redact both.

* **Precedence.** When both a path-keyed rule and a leaf-keyed rule
  could match the same field, the path-keyed rule wins. Explicit beats
  inferred. This lets users tighten a broad leaf rule with a narrow
  path-specific override.

### Nested field support

`RedactionConfig` walks dicts and lists recursively. List items keep
the parent's path for rule lookup so `{"emails": ["a@b", "c@d"]}`
redacts both list items.

### Depth limit — fails closed (Sprint 4 audit fix)

Depth is capped at `MAX_REDACTION_DEPTH = 5` to bound recursion. Past
the limit the redactor returns `REDACTED_PLACEHOLDER`, **not** the raw
subtree. Returning the raw value would invert the privacy default —
deeply nested or adversarial payloads must fail closed.

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
- **Path-key matching only** (the pre-audit default). Rejected — pre-built
  configs designed around path keys don't survive contact with real
  envelope shapes. Leaf-key matching is the semantic users want ("redact
  PII fields by their name, wherever they are"); path-key is the
  advanced-user opt-in. Memory: `feedback_audit_redaction_leaf_keys`.
- **Depth-limit fail-open** (the pre-audit default). Rejected — a privacy
  control must err toward MORE redaction when uncertain, not less. Memory:
  `feedback_privacy_fails_closed`.

## Verification

- Unit tests: `tests/unit/test_redaction_pii.py` — instantiation,
  per-pattern coverage, nested + list traversal, depth limit, and the
  golden-vector contract.
- Golden vectors: `tests/fixtures/redaction_vectors.json` — pinned
  `(config, input) → expected` mappings. Editing this file is a contract
  change and demands an ADR amendment.
- Sprint 1 baseline: `tests/unit/test_redaction.py` continues to pass
  (no regression on dot-path and edge-case semantics).
