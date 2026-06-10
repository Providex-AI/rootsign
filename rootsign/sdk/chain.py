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
    from rootsign.sdk.hashing import compute_payload_hash

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
        # Reconstruct the canonical payload exactly as the DB-side verify
        # uses it. `compute_payload_hash` here stands in for the action
        # self-hash formula — keep this aligned with the CRUD-side
        # `compute_action_self_hash` if its shape ever drifts.
        canonical = {
            "action_id": record.get("action_id"),
            "session_id": record.get("session_id"),
            "tool_name": record.get("tool_name"),
            "input_hash": record.get("input_hash"),
            "output_hash": record.get("output_hash"),
            "prev_action_hash": record.get("prev_action_hash"),
            "timestamp": record.get("timestamp"),
            "sequence_number": record.get("sequence_number"),
        }
        recomputed = compute_payload_hash(canonical)
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
