"""Hash-chain verification engine for the SDK — used by `rootsign verify`.

Two surfaces:

* `verify_session(session_id, db)` — async, talks to Postgres via the
  existing `crud.action.verify_chain`. Used by the CLI's default path and
  any in-process tooling that already owns an `AsyncSession`.
* `verify_session_local(jsonl_path)` — sync, reads a JSONL file and rebuilds
  the chain offline. No DB required; for air-gapped verification.

Both return a `VerifyResult` dataclass with a human-readable `summary`
property the CLI renders.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from rootsign.errors import postgres_extra_required
from rootsign.verdict import (
    FailureKind,
    Verdict,
    decide,
    describe_missing,
    explains_break,
    missing_count,
    missing_ranges,
)


@dataclass
class VerifyResult:
    """Verification verdict for a single session.

    `verdict` is the single source of truth (ADR-013 Decision 4b): VALID,
    TAMPERED, or INCOMPLETE. `record_count`, `missing_ranges` and the failure
    fields are diagnostic detail. `summary` is the one-line string the CLI
    prints; the rest is exposed for tests and machine callers.

    `valid` survives as a **property**, false for both failure verdicts, so
    every existing consumer keeps working. It is derived rather than stored on
    purpose: two fields that must agree are two fields that will eventually
    disagree, and this one decides whether an auditor trusts a log.
    """

    verdict: Verdict
    record_count: int
    session_id: UUID | str | None
    first_invalid_sequence: int | None = None
    error: str | None = None
    #: Inclusive `(start, end)` sequence ranges that are absent. Populated for
    #: INCOMPLETE — and also for TAMPERED, since a session can have both and
    #: the gaps are still worth reporting (worst verdict wins, detail does not).
    missing_ranges: list[tuple[int, int]] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return self.verdict is Verdict.VALID

    @property
    def summary(self) -> str:
        if self.verdict is Verdict.VALID:
            return f"VALID — {self.record_count} records, chain intact"
        if self.verdict is Verdict.INCOMPLETE:
            msg = (
                f"INCOMPLETE — {missing_count(self.missing_ranges)} record(s) missing "
                f"(sequence {describe_missing(self.missing_ranges)}); "
                f"{self.record_count} present and intact"
            )
            if self.error:
                msg += f" ({self.error})"
            return msg
        msg = f"TAMPERED — chain broken at record #{self.first_invalid_sequence}"
        if self.error:
            msg += f" ({self.error})"
        if self.missing_ranges:
            msg += f"; also missing sequence {describe_missing(self.missing_ranges)}"
        return msg


async def verify_session(session_id: UUID, db: Any) -> VerifyResult:
    """Verify the hash chain for a session stored in the database.

    `db` is an `AsyncSession`. Returns a `VerifyResult`. Never raises on
    chain inconsistency — the result's `valid` field is the verdict.
    """
    with postgres_extra_required():
        from rootsign.crud import action as action_crud

    raw = await action_crud.verify_chain(db, session_id=session_id)
    # `verdict` is additive on the CRUD dict (ADR-013 Decision 4b); fall back to
    # the boolean for any caller still returning the pre-0.3.0 shape.
    verdict = raw.get("verdict")
    if verdict is None:
        verdict = Verdict.VALID if raw["valid"] else Verdict.TAMPERED
    return VerifyResult(
        verdict=Verdict(verdict),
        record_count=raw["record_count"],
        session_id=session_id,
        first_invalid_sequence=raw.get("first_invalid_sequence"),
        error=raw.get("error"),
        missing_ranges=[tuple(r) for r in raw.get("missing_ranges", [])],
    )


def verify_session_local(jsonl_path: str) -> VerifyResult:
    """Verify a session stored in a local JSONL file. No DB required.

    Each line of the file is a JSON object representing one Action record
    with at least: `session_id`, `sequence_number`, `self_hash`,
    `prev_action_hash`, plus whatever fields the canonical self-hash
    formula reads. Records are sorted by `sequence_number` before
    verification.

    Raises `FileNotFoundError` if the path does not exist; every chain
    inconsistency (including an empty file) produces a `VerifyResult` — with
    `verdict` TAMPERED for an alteration and INCOMPLETE for a missing record,
    disambiguated by the shared precedence rule in `rootsign.verdict`.
    """
    # CRITICAL: use the FROZEN canonical hash from `rootsign.hashing`, NOT
    # `rootsign.sdk.hashing.compute_payload_hash`. The two diverge on
    # None-handling (canonical coerces `None`→`""` for `output_hash` and
    # `prev_action_hash`; payload-hash serializes them as JSON `null`).
    # Record #1 of any real store-exported chain has `prev_action_hash =
    # NULL`, so re-implementing the canonical formula inline reliably
    # produces TAMPERED on genuine exports. Audit fix.
    # Memory: feedback_canonical_hash_never_reimplemented.
    from rootsign.hashing import compute_action_self_hash

    # audit #4: re-bind the redacted payloads to the input_hash/output_hash
    # the chain protects. `compute_action_self_hash` excludes the redacted
    # payloads, so a tampered export could rewrite the human-readable
    # evidence while the self_hash chain still verifies. Only enforced when a
    # record actually carries the redacted field.
    from rootsign.sdk.hashing import compute_payload_hash

    path = Path(jsonl_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Session file not found: {path}")

    raw_lines = [ln.strip() for ln in path.read_text().splitlines() if ln.strip()]
    if not raw_lines:
        return VerifyResult(
            verdict=Verdict.TAMPERED,
            record_count=0,
            session_id=None,
            error="No records found in file",
        )

    # Parse each line. A malformed JSON line is corruption; a malformed *final*
    # line is the tell-tale of a crash mid-write (partial append) — report it
    # distinctly (ADR-011 Decision 4) rather than raising.
    all_records: list[dict[str, Any]] = []
    for idx, ln in enumerate(raw_lines):
        try:
            all_records.append(json.loads(ln))
        except json.JSONDecodeError:
            is_final = idx == len(raw_lines) - 1
            return VerifyResult(
                verdict=Verdict.TAMPERED,
                record_count=len(all_records),
                session_id=all_records[0].get("session_id") if all_records else None,
                error=(
                    "truncated final line — incomplete write (crash mid-append?)"
                    if is_final
                    else f"malformed JSON at line {idx + 1}"
                ),
            )

    session_id = all_records[0].get("session_id")

    # ADR-011 Decision 2 / T2.7: a session file now holds all five event types.
    # Rebuild the chain from ACTION_RECORD lines only. Legacy store-exports carry
    # no `event_type` field and are all Actions — a missing `event_type` is
    # therefore treated as an Action (backward compatible).
    records = [r for r in all_records if r.get("event_type", "ACTION_RECORD") == "ACTION_RECORD"]

    if not records:
        # A session with no actions (only SESSION_OPEN/CLOSE, decisions,
        # approvals) is a valid, empty chain — same verdict as the DB path.
        return VerifyResult(verdict=Verdict.VALID, record_count=0, session_id=session_id)

    records.sort(key=lambda r: r["sequence_number"])

    # Gaps are computed BEFORE the walk, because their presence changes how a
    # `prev_action_hash` mismatch must be read: at the far side of a gap the
    # mismatch is expected, not suspicious (ADR-013 Decision 4b).
    gaps = missing_ranges([r["sequence_number"] for r in records])

    def _tampered(kind: FailureKind, sequence: int | None, error: str) -> VerifyResult:
        # Worst verdict wins: gaps are still reported, but an alteration is
        # never downgraded to "just incomplete".
        return VerifyResult(
            verdict=decide(missing=gaps, failure_kind=kind),
            record_count=len(records),
            session_id=session_id,
            first_invalid_sequence=sequence,
            error=error,
            missing_ranges=gaps,
        )

    # Duplicate sequence_number ⇒ TAMPERED (Decision 5): a re-appended chain
    # (e.g. a restart re-using the same session file) collides on the sequence
    # counter. Distinct error string so the cause is diagnosable.
    for a, b in zip(records, records[1:]):
        if a["sequence_number"] == b["sequence_number"]:
            return _tampered(
                FailureKind.DUPLICATE_SEQUENCE,
                b["sequence_number"],
                f"duplicate sequence_number {b['sequence_number']} (chain replay/corruption)",
            )

    expected_prev: str | None = None
    for record in records:
        sequence = record.get("sequence_number")
        # `compute_action_self_hash` handles the None→"" coercion and
        # UUID-stringification internally — we just pass the record as-is.
        # Required field guard: any missing canonical field is itself a
        # tamper signal, surface as an explicit error.
        try:
            recomputed = compute_action_self_hash(record)
        except KeyError as missing:
            return _tampered(
                FailureKind.MISSING_FIELD, sequence, f"missing canonical field {missing!s}"
            )
        if record.get("self_hash") != recomputed:
            return _tampered(FailureKind.SELF_HASH_MISMATCH, sequence, "self_hash mismatch")
        if (record.get("prev_action_hash") or None) != expected_prev:
            if not explains_break(sequence, gaps):
                return _tampered(
                    FailureKind.PREV_HASH_MISMATCH, sequence, "prev_action_hash chain broken"
                )
            # A gap explains this break: the predecessor named here was never
            # written. Re-anchor and keep walking — stopping now would let a
            # single dropped record mask every alteration after it, which is
            # exactly the downgrade the precedence rule exists to prevent.
        input_redacted = record.get("input_redacted")
        if input_redacted is not None and compute_payload_hash(input_redacted) != record.get(
            "input_hash"
        ):
            return _tampered(
                FailureKind.PAYLOAD_BINDING,
                sequence,
                "payload_hash mismatch: input_redacted does not match input_hash",
            )
        output_redacted = record.get("output_redacted")
        if output_redacted is not None and compute_payload_hash(output_redacted) != record.get(
            "output_hash"
        ):
            return _tampered(
                FailureKind.PAYLOAD_BINDING,
                sequence,
                "payload_hash mismatch: output_redacted does not match output_hash",
            )
        expected_prev = record["self_hash"]

    return VerifyResult(
        verdict=decide(missing=gaps, failure_kind=None),
        record_count=len(records),
        session_id=session_id,
        first_invalid_sequence=gaps[0][0] if gaps else None,
        error=(
            f"{missing_count(gaps)} record(s) missing at sequence {describe_missing(gaps)}"
            if gaps
            else None
        ),
        missing_ranges=gaps,
    )
