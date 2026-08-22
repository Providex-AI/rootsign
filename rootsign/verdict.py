"""The verification vocabulary, shared by every verifier (ADR-013 Decision 4b).

`verify` used to answer a boolean, which stopped being enough the moment
records could be *lost* as well as *altered* (ADR-013 Decision 4a). Three
verdicts now:

* **VALID** — dense sequences, every `self_hash` recomputes, every link matches.
* **TAMPERED** — a record was altered.
* **INCOMPLETE** — a record is missing.

**Why the precedence rule is the load-bearing part.** A gap *causes* a hash
discontinuity: record N+1's `prev_action_hash` points at a record that was
never written, which looks exactly like tampering to a naive check — and in
fact reports as TAMPERED today. The two conditions therefore have to be
disambiguated deliberately, not detected independently:

- a break at the far side of a sequence gap is **explained** by the gap, so it
  reads INCOMPLETE, not TAMPERED;
- a break between *contiguous* sequences has no such explanation, so it is
  TAMPERED;
- when a session has both, **TAMPERED wins** and the missing ranges are still
  reported. Worst-verdict-wins is the only safe default: a tampered session
  that also has gaps must never be downgraded to "just incomplete".

Which is why a verifier must not stop at the first gap-explained break — it
re-anchors and keeps looking, or a single dropped record would mask every
alteration after it.

This module lives at the package root, next to `hashing` (the frozen formula)
and `chain_state` (construction), because both verifiers depend on it and
neither may depend on the other: the local one is DB-free core, the Postgres
one lives behind the optional extra.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum


class Verdict(str, Enum):
    """A session's verification outcome. `str` so it serializes as itself."""

    VALID = "VALID"
    TAMPERED = "TAMPERED"
    INCOMPLETE = "INCOMPLETE"


class FailureKind(str, Enum):
    """Why a chain failed, for the cases that are *not* a plain gap."""

    SELF_HASH_MISMATCH = "self_hash_mismatch"
    PREV_HASH_MISMATCH = "prev_hash_mismatch"
    DUPLICATE_SEQUENCE = "duplicate_sequence"
    PAYLOAD_BINDING = "payload_binding"
    MISSING_FIELD = "missing_field"
    MALFORMED = "malformed"


#: Process exit codes, one per verdict. Additive by design: consumers testing
#: `!= 0` or `== 1` keep working, and CI can now tell "records missing" from
#: "records altered" without parsing stdout.
EXIT_CODES: dict[Verdict, int] = {
    Verdict.VALID: 0,
    Verdict.TAMPERED: 1,
    Verdict.INCOMPLETE: 2,
}


def exit_code(verdict: Verdict) -> int:
    return EXIT_CODES[verdict]


def missing_ranges(sequences: Iterable[int]) -> list[tuple[int, int]]:
    """Contiguous runs of sequence numbers absent from a dense `1..max` chain.

    A chain is 1-based and dense by construction, so anything not present
    between 1 and the highest sequence observed is missing. Returned as
    inclusive `(start, end)` pairs — `[(3, 5)]` reads "records 3 through 5 are
    not here", which is what an auditor needs to ask the operator about.

    Duplicates are ignored: they are a separate (and worse) failure, handled
    by the caller as `DUPLICATE_SEQUENCE`.
    """
    present = {s for s in sequences if isinstance(s, int)}
    if not present:
        return []
    ranges: list[tuple[int, int]] = []
    start: int | None = None
    for candidate in range(1, max(present) + 1):
        if candidate in present:
            if start is not None:
                ranges.append((start, candidate - 1))
                start = None
        elif start is None:
            start = candidate
    # No trailing run is possible: the loop ends on `max(present)`, which is
    # present by definition. A record missing from the *end* of a chain is
    # undetectable from the records alone — the chain cannot know how long it
    # was meant to be. (SESSION_CLOSE's `total_actions` is the cross-check for
    # that case, which is why the store logs a warning when it disagrees.)
    return ranges


def explains_break(sequence_number: int, ranges: list[tuple[int, int]]) -> bool:
    """True when the record at `sequence_number` sits immediately after a gap.

    That is the one situation where a `prev_action_hash` mismatch is expected
    rather than suspicious: the predecessor this record names was never
    written, so nothing in the file can match it.
    """
    return any(end == sequence_number - 1 for _, end in ranges)


def decide(
    *,
    missing: list[tuple[int, int]],
    failure_kind: FailureKind | None,
) -> Verdict:
    """Combine what a verifier found into one verdict. Worst wins.

    `failure_kind` must already exclude gap-explained breaks — use
    `explains_break` while walking the chain. Passing one in anyway is not
    wrong, merely pessimistic: the result is TAMPERED, which is the safe
    direction to be wrong in.
    """
    if failure_kind is not None:
        return Verdict.TAMPERED
    if missing:
        return Verdict.INCOMPLETE
    return Verdict.VALID


def describe_missing(ranges: list[tuple[int, int]]) -> str:
    """Human-readable summary of the gaps, e.g. `3` or `3-5, 9`."""
    return ", ".join(str(start) if start == end else f"{start}-{end}" for start, end in ranges)


def missing_count(ranges: list[tuple[int, int]]) -> int:
    return sum(end - start + 1 for start, end in ranges)
