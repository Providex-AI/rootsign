"""Sync receipts — the durable record of a replay attempt (ADR-015).

A receipt answers the question neither the chain nor the spool file can:
**what happened when someone tried to upload this evidence.** The chain is
sealed at the source and closed; the spool file is the exhibit under replay.
The outcome of *handling* the exhibit gets its own record, kept beside it.

Layout, under the spool root:

    sessions/<session_id>.jsonl          the exhibit
    receipts/<session_id>.sync.json      this record, appended per attempt
    synced/<session_id>.jsonl            exhibit, once fully accepted
    synced/receipts/<session_id>.sync.json   record, moved with it

## Outcomes are indexed by record position (ADR-015 Decision 3a)

Ranges compress over the 1-based position of an envelope in the spool file,
not over `sequence_number`. Only `ACTION_RECORD` envelopes carry a sequence;
`SESSION_OPEN`/`SESSION_CLOSE` do not, so keying by sequence would silently
drop the session-boundary envelopes and produce a receipt that looks complete
while two records have no outcome at all.

Position is dense and total, which buys the invariant this module guarantees:

    the four buckets partition [1, record_count] exactly — no gap, no overlap

`partition_is_total()` checks it, and the suite asserts it on every receipt it
builds. A reviewer can confirm a receipt accounts for every record by
arithmetic rather than by trusting the writer.

## Fail-fast at the batch boundary (ADR-015 Decision 2)

`replay_envelopes` stops *soliciting* after the first refusal, but records
every response the endpoint already returned for the rest of that batch.
Those are real observations: spec §7 rule 3 forbids a rejection from aborting
later elements, and §8.3 forbids refusing a record for a `prev_action_hash`
the store has not seen, so the elements after a refusal are processed
normally — which usually means **accepted**. Dropping those answers would
file delivered records as unattempted.

Nothing beyond that batch is sent. Not because the store would refuse it, but
because ADR-013 Decision 4's resume contract wants delivery to stay a prefix:
the spool file alone has to answer "where does the next run start".  Records
never sent are `not_attempted` — not accepted, not refused, not observed.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rootsign._version import SDK_VERSION
from rootsign.ingest.schemas import ErrorCode
from rootsign.sdk.spool import SYNCED_SUBDIR, spool_root

#: Stamped per attempt, not per file (ADR-015 Decision 3): a long-lived spool
#: synced across an upgrade legitimately holds v1.0 and v1.1 attempts side by
#: side, and each attempt must say which vocabulary it was written in. Readers
#: treat a missing `not_attempted` as empty.
RECEIPT_SCHEMA_VERSION = "receipt/1.1"

#: Subdirectory holding receipts, under the spool root and under `synced/`.
RECEIPTS_SUBDIR = "receipts"

#: Suffix appended to the session id. `.sync.json` rather than `.json` so a
#: directory listing says what the file is without opening it.
RECEIPT_SUFFIX = ".sync.json"

#: What an attempt did to the spool file.
DISPOSITION_FULLY_ACCEPTED = "fully_accepted"
DISPOSITION_PARTIAL = "partial"
DISPOSITION_TRANSPORT_FAILED = "transport_failed"


def _dumps(document: dict[str, Any]) -> str:
    """Serialize a receipt. Deterministic — the printed hash depends on it.

    Same convention as the bundle documents (`export._dumps`) so a receipt
    hashes identically whether it is read from the spool tree or out of an
    evidence bundle. Duplicated rather than imported: `export` imports this
    module to pull receipts into bundles, and the reverse import would be a
    cycle.
    """
    return json.dumps(document, indent=2, ensure_ascii=False, default=str) + "\n"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str | os.PathLike[str]) -> str:
    """Hash a file's bytes.

    Bytes, not decoded text: `spool_file_sha256` binds the receipt to the
    exact bytes replayed, and a file that is not valid UTF-8 must still get a
    digest rather than raise in the middle of recording an outcome.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def receipts_dir(root: str | os.PathLike[str] | None = None, *, synced: bool = False) -> Path:
    """`<spool>/receipts/`, or `<spool>/synced/receipts/` once retired."""
    base = spool_root(root)
    if synced:
        base = base / SYNCED_SUBDIR
    return base / RECEIPTS_SUBDIR


def receipt_path(
    session_id: str,
    root: str | os.PathLike[str] | None = None,
    *,
    synced: bool = False,
    suffix_index: int = 0,
) -> Path:
    """Where this session's receipt lives.

    `suffix_index` mirrors `spool.mark_synced`'s collision handling: the same
    session synced twice leaves `<id>.jsonl` and `<id>.1.jsonl` in `synced/`,
    and their receipts take the matching `<id>.sync.json` / `<id>.1.sync.json`
    so each record stays paired with the file it describes (ADR-015 D1).
    """
    stem = session_id if suffix_index == 0 else f"{session_id}.{suffix_index}"
    return receipts_dir(root, synced=synced) / f"{stem}{RECEIPT_SUFFIX}"


def find_receipt(session_id: str, root: str | os.PathLike[str] | None = None) -> Path | None:
    """The receipt for a session, wherever it ended up. `None` if never synced.

    Checks the live spool first, then `synced/` — a session that completed has
    its receipt in the latter, and that is the common case at export time.
    """
    for synced in (False, True):
        candidate = receipt_path(session_id, root, synced=synced)
        if candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Outcome ranges
# ---------------------------------------------------------------------------


def compress_ranges(positions: Iterable[int]) -> list[list[int]]:
    """Collapse sorted positions into inclusive `[start, end]` pairs.

    `[1,2,3,7,8]` -> `[[1,3],[7,8]]`. Lists rather than tuples because this
    goes straight to JSON, where the distinction does not survive anyway —
    emitting lists keeps a round-tripped receipt equal to the one written.
    """
    ordered = sorted(set(positions))
    if not ordered:
        return []
    ranges: list[list[int]] = []
    start = previous = ordered[0]
    for position in ordered[1:]:
        if position == previous + 1:
            previous = position
            continue
        ranges.append([start, previous])
        start = previous = position
    ranges.append([start, previous])
    return ranges


def expand_ranges(ranges: Iterable[Sequence[int]]) -> set[int]:
    """Inverse of `compress_ranges`, for verifying a receipt partitions."""
    out: set[int] = set()
    for pair in ranges:
        start, end = int(pair[0]), int(pair[1])
        out.update(range(start, end + 1))
    return out


def partition_is_total(attempt: dict[str, Any]) -> bool:
    """True when the attempt's buckets cover `[1, record_count]` exactly once.

    The property that lets a reviewer trust a receipt accounts for the whole
    file without trusting whoever wrote it (ADR-015 Decision 3a).
    """
    outcomes = attempt.get("outcomes") or {}
    covered: list[int] = []
    for key in ("accepted", "duplicate", "not_attempted"):
        covered.extend(expand_ranges(outcomes.get(key) or []))
    covered.extend(int(entry["record"]) for entry in (outcomes.get("rejected") or []))

    total = int(attempt.get("record_count") or 0)
    # A list, not a set, so a record appearing in two buckets fails here
    # rather than being silently de-duplicated into looking correct.
    return sorted(covered) == list(range(1, total + 1))


# ---------------------------------------------------------------------------
# Building an attempt
# ---------------------------------------------------------------------------


def _sequence_of(envelope: dict[str, Any]) -> int | None:
    payload = envelope.get("payload") or {}
    value = payload.get("sequence_number")
    return int(value) if isinstance(value, int) else None


def build_attempt(
    *,
    envelopes: Sequence[dict[str, Any]],
    report: Any,
    endpoint: str,
    spool_file_sha256: str,
    started_at: datetime,
    finished_at: datetime | None = None,
    attempt_id: str | None = None,
    operator: str | None = None,
    transport_error: str | None = None,
) -> dict[str, Any]:
    """Turn a `ReplayReport` into one receipt attempt.

    `transport_error` set means the attempt never completed an observation —
    retries were exhausted mid-flight — so the disposition is
    `transport_failed` and nothing is claimed about individual records beyond
    what the report managed to collect before the exception.
    """
    total = len(envelopes)
    responses = list(getattr(report, "responses", []) or [])
    failed_index = getattr(report, "failed_index", None)

    accepted: list[int] = []
    duplicate: list[int] = []
    rejected: list[dict[str, Any]] = []

    for index, response in enumerate(responses):
        position = index + 1
        code = getattr(response, "error_code", None)
        if getattr(response, "status", None) == "accepted":
            accepted.append(position)
        elif code is ErrorCode.DUPLICATE_EVENT:
            duplicate.append(position)
        else:
            entry: dict[str, Any] = {
                "record": position,
                "error_code": code.value if isinstance(code, ErrorCode) else code,
                "retryable": getattr(response, "retryable", None),
                "error_message": getattr(response, "error_message", None),
            }
            sequence = _sequence_of(envelopes[index]) if index < total else None
            if sequence is not None:
                entry["sequence"] = sequence
            rejected.append(entry)

    observed = len(responses)
    not_attempted = list(range(observed + 1, total + 1))

    # Disposition turns on WHY the attempt stopped, because the two answers
    # are different instructions to an operator: `partial` means the store
    # deterministically refused a record and someone has to fix it;
    # `transport_failed` means the network gave up and the same bytes will
    # very likely go through on the next run.
    #
    # The retryable check is what makes this work against the real client.
    # `HttpIngestClient` does not raise when the endpoint dies — it exhausts
    # its budget and returns STORE_UNAVAILABLE with `retryable=True` (ADR-013
    # Decision 3). Keying only off an escaped exception would label every real
    # outage `partial` and tell the operator to go fix data that is fine.
    halting = responses[failed_index] if failed_index is not None else None
    gave_up = transport_error is not None or bool(getattr(halting, "retryable", False))

    if gave_up:
        disposition = DISPOSITION_TRANSPORT_FAILED
    elif failed_index is None and not rejected:
        disposition = DISPOSITION_FULLY_ACCEPTED
    else:
        disposition = DISPOSITION_PARTIAL

    halted_at_sequence = None
    if failed_index is not None and failed_index < total:
        halted_at_sequence = _sequence_of(envelopes[failed_index])

    attempt: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "attempt_id": attempt_id or str(uuid.uuid4()),
        "started_at": _iso(started_at),
        "finished_at": _iso(finished_at or datetime.now(timezone.utc)),
        "endpoint": endpoint,
        "sdk_version": SDK_VERSION,
        "operator": operator if operator is not None else _operator(),
        "spool_file_sha256": spool_file_sha256,
        "record_count": total,
        "outcomes": {
            "accepted": compress_ranges(accepted),
            "duplicate": compress_ranges(duplicate),
            "rejected": rejected,
            "not_attempted": compress_ranges(not_attempted),
        },
        "halted_at_sequence": halted_at_sequence,
        "disposition": disposition,
    }
    if transport_error is not None:
        attempt["transport_error"] = transport_error
    elif gave_up and halting is not None:
        code = getattr(halting, "error_code", None)
        attempt["transport_error"] = (
            f"{code.value if isinstance(code, ErrorCode) else code}: "
            f"{getattr(halting, 'error_message', None)}"
        )
    return attempt


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _operator() -> str:
    """The OS user who ran the command.

    Not an authenticated identity — ADR-015 labels this honestly. `getpass`
    consults the environment before the password database, and both can fail
    in a container with no passwd entry, so this never raises.
    """
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 — identity is a nice-to-have, never fatal
        return "unknown"


# ---------------------------------------------------------------------------
# Reading and writing
# ---------------------------------------------------------------------------


def load_receipt(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Parse a receipt file. Raises `ValueError` if it is not one."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: not valid JSON ({exc})") from exc
    if not isinstance(document, dict) or "attempts" not in document:
        raise ValueError(f"{path}: not a sync receipt (no `attempts`)")
    return document


def append_attempt(
    session_id: str,
    attempt: dict[str, Any],
    root: str | os.PathLike[str] | None = None,
) -> tuple[Path, str]:
    """Create or extend this session's receipt. Returns `(path, sha256)`.

    Append-only across attempts: re-running `sync` after fixing a rejection
    adds attempt 2 rather than overwriting attempt 1, so the history of how
    the evidence was handled survives (ADR-015 Decision 1).

    A receipt that cannot be parsed is left untouched and the new attempt goes
    to a sibling `*.corrupt-<n>.sync.json`. Overwriting an unreadable record of
    evidence handling to make room for a readable one is precisely the move
    this module exists to prevent.
    """
    path = receipt_path(session_id, root)
    path.parent.mkdir(parents=True, exist_ok=True)

    document: dict[str, Any] | None = None
    if path.is_file():
        try:
            document = load_receipt(path)
        except (ValueError, OSError):
            path = _corrupt_sibling(path)
            document = None

    if document is None:
        document = {"session_id": session_id, "attempts": []}

    document["attempts"].append(attempt)
    text = _dumps(document)
    _write_atomic(path, text)
    return path, sha256_text(text)


def _corrupt_sibling(path: Path) -> Path:
    counter = 1
    while True:
        candidate = path.with_name(f"{path.name}.corrupt-{counter}")
        if not candidate.exists():
            return candidate
        counter += 1


def _write_atomic(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, then rename.

    A receipt half-written by an interrupted sync is worse than no receipt:
    it would read as a complete record of an incomplete truth.
    """
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def mark_receipt_synced(
    session_id: str,
    root: str | os.PathLike[str] | None = None,
    *,
    suffix_index: int = 0,
) -> Path | None:
    """Move a receipt into `synced/receipts/`, following its spool file.

    Returns the new path, or `None` when there was no receipt to move (a
    session that completed on an attempt which wrote none — only `--dry-run`,
    which never gets here). Never overwrites: a collision takes the next
    suffix, matching `spool.mark_synced`.
    """
    source = receipt_path(session_id, root)
    if not source.is_file():
        return None

    destination = receipt_path(session_id, root, synced=True, suffix_index=suffix_index)
    destination.parent.mkdir(parents=True, exist_ok=True)
    counter = max(suffix_index, 1)
    while destination.exists():
        destination = receipt_path(session_id, root, synced=True, suffix_index=counter)
        counter += 1

    source.replace(destination)
    return destination


# ---------------------------------------------------------------------------
# Bundle integration (ADR-015 Decision 3)
# ---------------------------------------------------------------------------


def replay_summary(document: dict[str, Any]) -> dict[str, Any]:
    """The `verification.json` `replay` block — latest attempt, summarized.

    Only the latest attempt is summarized; the full history stays in the
    receipt file the bundle carries. A reviewer reading `verification.json`
    wants "what is the current state of this evidence", and gets pointed at
    the receipt for how it got there.
    """
    attempts = document.get("attempts") or []
    latest = attempts[-1] if attempts else {}
    outcomes = latest.get("outcomes") or {}
    return {
        "attempts": len(attempts),
        "latest_attempt_id": latest.get("attempt_id"),
        "latest_disposition": latest.get("disposition"),
        "finished_at": latest.get("finished_at"),
        "endpoint": latest.get("endpoint"),
        "spool_file_sha256": latest.get("spool_file_sha256"),
        "record_count": latest.get("record_count"),
        "halted_at_sequence": latest.get("halted_at_sequence"),
        "observed_and_rejected": outcomes.get("rejected") or [],
        "not_attempted": outcomes.get("not_attempted") or [],
    }
