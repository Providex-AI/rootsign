# ADR-001: Hash canonical specification — frozen at Phase 0

- **Date:** May 2026
- **Status:** Accepted
- **Decider:** Founder

## Context

RootSign computes SHA-256 hashes of agent `Action` records to form a tamper-evident chain: each Action embeds the hash of the previous Action in its canonical representation, and re-hashing the entire chain on read is how `verify_chain` detects post-hoc modification.

The hash function must be deterministic and stable across SDK versions, Python runtimes, and operating systems. Any change to the canonical representation — added field, removed field, different serialization order, different string encoding — silently invalidates every audit chain produced before the change. There is no in-place migration; we would have to re-hash with the new spec and store a `hash_version` tag.

This is the highest-stakes single decision in the product. Get it wrong once and every customer's audit history is suspect.

## Decision

The canonical fields and their serialization are frozen as defined in the Phase 0 spec (`rootsign/hashing.py :: compute_action_self_hash`):

**Fields, in serialization order:**

```
action_id          str(UUID)
session_id         str(UUID)
tool_name          str
input_hash         str (64 hex chars)
output_hash        str (64 hex chars, or "" if null)
prev_action_hash   str (64 hex chars, or "" if first action in chain)
timestamp          str (ISO 8601 UTC)
sequence_number    int (1-indexed, monotonically increasing within session)
```

**Serialization:**

```python
serialized = json.dumps(canonical, sort_keys=True, ensure_ascii=True)
return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
```

`sort_keys=True` and `ensure_ascii=True` are both load-bearing — see the test fixture `tests/fixtures/hash_vectors.json` for the golden vectors and `tests/unit/test_hashing.py::test_ensure_ascii_invariant` for the non-ASCII regression test.

## Alternatives considered

1. **Include all Action fields (duration_ms, authorization_status, input_redacted, output_redacted).**
   Rejected. `authorization_status` legitimately changes after the Action is recorded (pending → approved/rejected via Approval). Including it would make every Approval invalidate the chain. `duration_ms` and the redacted payloads are computed by the SDK after the call returns — they are not part of the agent's intent and should not bind the hash chain.

2. **Use BLAKE3 instead of SHA-256.**
   Rejected. BLAKE3 is faster but SHA-256 has substantially wider library support (every compliance framework, every HSM, every language stdlib) and is the default expectation in audit contexts. The marginal speedup is invisible against PostgreSQL write latency.

3. **Use canonical CBOR or Protocol Buffers instead of JSON.**
   Rejected. JSON with `sort_keys=True, ensure_ascii=True` is sufficiently deterministic, easier to hand-verify in incident response, and avoids dragging in another serialization toolchain.

## Consequences

- Any future change to this spec requires: (1) a new ADR, (2) a `hash_version` field increment on the Action schema, (3) a migration plan that re-hashes historical sessions under the new spec OR a dual-verification scheme that accepts both. This is a rare, high-ceremony operation.
- Adding a new field to the `Action` ORM model does *not* break the hash spec, because the canonical hash function reads only the eight fields above. New fields are free; changing the canonical eight is not.
- The golden vectors in `tests/fixtures/hash_vectors.json` are the normative reference. They MUST NOT be edited without a new ADR.

## Compliance note

The combination of "deterministic canonical serialization + SHA-256 + per-record `prev_action_hash` linkage + forward-only migrations" is what lets us claim *cryptographically tamper-evident audit trail* in regulated contexts. ADR-001 is the load-bearing decision under that claim; ADR-002 keeps it intact across transport layers; ADR-003 keeps it intact across framework versions.
