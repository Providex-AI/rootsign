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
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID


@dataclass
class VerifyResult:
    """Verification verdict for a single session.

    `valid` is the single source of truth — `record_count` and the failure
    fields are diagnostic detail. `summary` is the one-line string the CLI
    prints; the rest of the dataclass is exposed for tests and machine
    callers.
    """

    valid: bool
    record_count: int
    session_id: UUID | str | None
    first_invalid_sequence: int | None = None
    error: str | None = None

    @property
    def summary(self) -> str:
        if self.valid:
            return f"VALID — {self.record_count} records, chain intact"
        msg = f"TAMPERED — chain broken at record #{self.first_invalid_sequence}"
        if self.error:
            msg += f" ({self.error})"
        return msg


async def verify_session(session_id: UUID, db: Any) -> VerifyResult:
    """Verify the hash chain for a session stored in the database.

    `db` is an `AsyncSession`. Returns a `VerifyResult`. Never raises on
    chain inconsistency — the result's `valid` field is the verdict.
    """
    from rootsign.crud import action as action_crud

    raw = await action_crud.verify_chain(db, session_id=session_id)
    return VerifyResult(
        valid=raw["valid"],
        record_count=raw["record_count"],
        session_id=session_id,
        first_invalid_sequence=raw.get("first_invalid_sequence"),
        error=raw.get("error"),
    )


def verify_session_local(jsonl_path: str) -> VerifyResult:
    """Verify a session stored in a local JSONL file. No DB required.

    Each line of the file is a JSON object representing one Action record
    with at least: `session_id`, `sequence_number`, `self_hash`,
    `prev_action_hash`, plus whatever fields the canonical self-hash
    formula reads. Records are sorted by `sequence_number` before
    verification.

    Raises `FileNotFoundError` if the path does not exist; all chain
    inconsistencies (including an empty file) produce a `VerifyResult`
    with `valid=False`.
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

    path = Path(jsonl_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Session file not found: {path}")

    records: list[dict[str, Any]] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    if not records:
        return VerifyResult(
            valid=False,
            record_count=0,
            session_id=None,
            error="No records found in file",
        )

    records.sort(key=lambda r: r["sequence_number"])
    session_id = records[0].get("session_id")

    expected_prev: str | None = None
    for record in records:
        # `compute_action_self_hash` handles the None→"" coercion and
        # UUID-stringification internally — we just pass the record as-is.
        # Required field guard: any missing canonical field is itself a
        # tamper signal, surface as an explicit error.
        try:
            recomputed = compute_action_self_hash(record)
        except KeyError as missing:
            return VerifyResult(
                valid=False,
                record_count=len(records),
                session_id=session_id,
                first_invalid_sequence=record.get("sequence_number"),
                error=f"missing canonical field {missing!s}",
            )
        if record.get("self_hash") != recomputed:
            return VerifyResult(
                valid=False,
                record_count=len(records),
                session_id=session_id,
                first_invalid_sequence=record["sequence_number"],
                error="self_hash mismatch",
            )
        if (record.get("prev_action_hash") or None) != expected_prev:
            return VerifyResult(
                valid=False,
                record_count=len(records),
                session_id=session_id,
                first_invalid_sequence=record["sequence_number"],
                error="prev_action_hash chain broken",
            )
        expected_prev = record["self_hash"]

    return VerifyResult(
        valid=True,
        record_count=len(records),
        session_id=session_id,
    )
