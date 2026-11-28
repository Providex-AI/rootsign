"""Hash-chain verification engine for the SDK — used by `rootsign verify`.

Two surfaces:

* `verify_session(session_id, db)` — async, talks to Postgres via the
  existing `crud.action.verify_chain`. Used by the CLI's default path and
  any in-process tooling that already owns an `AsyncSession`.
* `verify_session_local(jsonl_path)` — sync, reads a JSONL file and rebuilds
  the chain offline. No DB required; for air-gapped verification.

Both return a `VerifyResult` dataclass with a human-readable `summary`
property the CLI renders.

**The session seal (ADR-016).** After the chain is walked, both check the
seal the store wrote at close, through `rootsign.seal`, and fold it into the
verdict: records cut from the end read INCOMPLETE, an edited open, close,
decision or approval reads TAMPERED. A session without a seal keeps the
chain's verdict with an "unsealed" note, unless the caller passes
`require_seal=True`, which reports it INCOMPLETE: without the seal, deleting
the close record with the tail would otherwise read as a clean audit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from rootsign.errors import postgres_extra_required
from rootsign.seal import (
    INCOMPLETE_OUTCOMES,
    TAMPERED_OUTCOMES,
    UNSEALED_OUTCOMES,
    SealCheck,
    SealOutcome,
    check_client_seal,
    check_seal,
    jsonl_session_facts,
)
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
    #: What checking the session seal found (ADR-016). None when the seal was
    #: not consulted: the chain itself was already broken or had gaps.
    seal: SealCheck | None = None
    #: Whether the session carries a seal at all, checked or not.
    seal_present: bool = False

    @property
    def valid(self) -> bool:
        return self.verdict is Verdict.VALID

    @property
    def sealed(self) -> bool:
        return self.seal is not None and self.seal.sealed

    @property
    def seal_failed(self) -> bool:
        """The chain held, and the verdict comes from the seal."""
        return self.seal is not None and self.seal.outcome in TAMPERED_OUTCOMES

    @property
    def summary(self) -> str:
        if self.verdict is Verdict.VALID:
            return f"VALID — {self.record_count} records, chain intact"
        if (
            self.verdict is Verdict.INCOMPLETE
            and self.seal is not None
            and (self.seal.outcome in UNSEALED_OUTCOMES | {SealOutcome.NOT_CHECKABLE})
        ):
            return f"INCOMPLETE — {self.error}; {self.record_count} present and intact"
        if self.verdict is Verdict.TAMPERED and self.seal_failed:
            return f"TAMPERED — the session's seal does not match its records ({self.error})"
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


async def verify_session(session_id: UUID, db: Any, *, require_seal: bool = False) -> VerifyResult:
    """Verify the hash chain and the seal for a session stored in the database.

    `db` is an `AsyncSession`. Returns a `VerifyResult`. Never raises on
    chain inconsistency — the result's `valid` field is the verdict.
    `require_seal=True` reports a session with no seal as INCOMPLETE.
    """
    with postgres_extra_required():
        from rootsign.crud import action as action_crud
        from rootsign.crud.seal import session_facts
        from rootsign.models.session import AgentSession

    raw = await action_crud.verify_chain(db, session_id=session_id)
    # `verdict` is additive on the CRUD dict (ADR-013 Decision 4b); fall back to
    # the boolean for any caller still returning the pre-0.3.0 shape.
    verdict = raw.get("verdict")
    if verdict is None:
        verdict = Verdict.VALID if raw["valid"] else Verdict.TAMPERED
    result = VerifyResult(
        verdict=Verdict(verdict),
        record_count=raw["record_count"],
        session_id=session_id,
        first_invalid_sequence=raw.get("first_invalid_sequence"),
        error=raw.get("error"),
        missing_ranges=[tuple(r) for r in raw.get("missing_ranges", [])],
    )
    session = await db.get(AgentSession, session_id)
    if session is None:
        return result
    stored = dict(session.seal or {})
    client = stored.pop("client", None)
    seal = stored or None
    if result.verdict is not Verdict.VALID or seal is None:
        return _with_seal(result, None, seal, require_seal=require_seal)
    facts = await session_facts(db, session, status=session.status)
    check = check_seal(seal, facts)
    if check.sealed:
        check = check_client_seal(client, facts) or check
    return _with_seal(result, check, seal, require_seal=require_seal)


def _with_seal(
    result: VerifyResult,
    check: SealCheck | None,
    seal: dict[str, Any] | None,
    *,
    require_seal: bool,
) -> VerifyResult:
    """Fold a seal check into the chain's verdict (ADR-016 Decision 3).

    The chain's TAMPERED stands as is. When the chain has gaps, the seal cannot
    be checked against records that are not there, but its count still says
    whether more are missing at the end. When the chain is intact, the seal
    decides; with no seal, `require_seal` decides.
    """
    result.seal_present = bool(seal)
    if result.verdict is Verdict.TAMPERED:
        return result
    if result.verdict is Verdict.INCOMPLETE:
        final = (seal or {}).get("final_sequence")
        highest = max((end for _, end in result.missing_ranges), default=0)
        highest = max(highest, result.record_count + missing_count(result.missing_ranges))
        if isinstance(final, int) and final > highest:
            result.missing_ranges = [*result.missing_ranges, (highest + 1, final)]
            result.error = (
                f"{missing_count(result.missing_ranges)} record(s) missing at sequence "
                f"{describe_missing(result.missing_ranges)}"
            )
        return result
    if check is None:
        check = SealCheck(SealOutcome.UNSEALED, "not sealed: its end cannot be verified")
    result.seal = check
    if check.outcome in UNSEALED_OUTCOMES:
        if require_seal:
            result.verdict = Verdict.INCOMPLETE
            result.error = check.detail
        return result
    if check.outcome in INCOMPLETE_OUTCOMES:
        result.verdict = Verdict.INCOMPLETE
        result.error = check.detail
        if check.missing is not None:
            result.missing_ranges = [check.missing]
            result.first_invalid_sequence = check.missing[0]
        return result
    if check.outcome in TAMPERED_OUTCOMES:
        result.verdict = Verdict.TAMPERED
        result.error = check.detail
        result.first_invalid_sequence = None
    return result


def verify_session_local(jsonl_path: str, *, require_seal: bool = False) -> VerifyResult:
    """Verify the chain and the seal of a session in a local JSONL file. No DB required.

    `require_seal=True` reports a session with no seal as INCOMPLETE.
    """
    result, records = _verify_chain_local(jsonl_path)
    if records is None:
        return result
    check, seal = _local_seal(records)
    return _with_seal(result, check, seal, require_seal=require_seal)


def _local_seal(records: list[dict[str, Any]]) -> tuple[SealCheck | None, dict[str, Any] | None]:
    """The seal on a session file's close line, checked against the file."""
    closes = [r for r in records if r.get("event_type") == "SESSION_CLOSE"]
    if not closes:
        return None, None
    if len(closes) > 1:
        return SealCheck(
            SealOutcome.SEAL_EDITED, "the file has more than one session close record"
        ), None
    close = closes[0]
    payload = close.get("payload") or {}
    seal = payload.get("seal")
    if seal and not any(r.get("event_type") == "SESSION_OPEN" for r in records):
        return SealCheck(
            SealOutcome.NOT_CHECKABLE,
            "the session open record is not in this file, so the seal cannot be checked",
        ), seal
    facts = jsonl_session_facts(
        [r for r in records if r is not close], status=payload.get("status")
    )
    return check_seal(seal, facts), seal


def _verify_chain_local(jsonl_path: str) -> tuple[VerifyResult, list[dict[str, Any]] | None]:
    """Walk the chain of a session file. Records come back when the file parsed.

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
        ), None

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
            ), None

    session_id = all_records[0].get("session_id")

    # ADR-011 Decision 2 / T2.7: a session file now holds all five event types.
    # Rebuild the chain from ACTION_RECORD lines only. Legacy store-exports carry
    # no `event_type` field and are all Actions — a missing `event_type` is
    # therefore treated as an Action (backward compatible).
    records = [r for r in all_records if r.get("event_type", "ACTION_RECORD") == "ACTION_RECORD"]

    if not records:
        # A session with no actions (only SESSION_OPEN/CLOSE, decisions,
        # approvals) is a valid, empty chain — same verdict as the DB path.
        return VerifyResult(
            verdict=Verdict.VALID, record_count=0, session_id=session_id
        ), all_records

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

    def _tampered_with_records(
        kind: FailureKind, sequence: int | None, error: str
    ) -> tuple[VerifyResult, list[dict[str, Any]]]:
        return _tampered(kind, sequence, error), all_records

    # Duplicate sequence_number ⇒ TAMPERED (Decision 5): a re-appended chain
    # (e.g. a restart re-using the same session file) collides on the sequence
    # counter. Distinct error string so the cause is diagnosable.
    for a, b in zip(records, records[1:]):
        if a["sequence_number"] == b["sequence_number"]:
            return _tampered_with_records(
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
            return _tampered_with_records(
                FailureKind.MISSING_FIELD, sequence, f"missing canonical field {missing!s}"
            )
        if record.get("self_hash") != recomputed:
            return _tampered_with_records(
                FailureKind.SELF_HASH_MISMATCH, sequence, "self_hash mismatch"
            )
        if (record.get("prev_action_hash") or None) != expected_prev:
            if not explains_break(sequence, gaps):
                return _tampered_with_records(
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
            return _tampered_with_records(
                FailureKind.PAYLOAD_BINDING,
                sequence,
                "payload_hash mismatch: input_redacted does not match input_hash",
            )
        output_redacted = record.get("output_redacted")
        if output_redacted is not None and compute_payload_hash(output_redacted) != record.get(
            "output_hash"
        ):
            return _tampered_with_records(
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
    ), all_records
