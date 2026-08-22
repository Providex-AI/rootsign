"""The offline spool, read back — what `rootsign-admin sync` walks (ADR-013 D4).

`HttpIngestClient` fails over to an ordinary `JsonlIngestClient` rooted at
`$ROOTSIGN_DATA_DIR/spool/`, so a spooled session is just a session file:
`rootsign verify --local` works on it while the network is still down. This
module is the other direction — turning those *stored records* back into the
*envelopes* the ingest endpoint accepts, so they can be uploaded when
connectivity returns.

**Records are not envelopes.** The writer flattens an ACTION_RECORD: the eight
canonical fields sit at the top level of the line (that is what lets
`verify_session_local` recompute the hash directly), and envelope metadata
rides alongside them. Replay has to fold that back into
`{...envelope, "payload": {...}}`, and `IngestEnvelope` is `extra="forbid"`, so
"send the line as-is" is not an option — for non-actions either, since the
writer adds `decision_id` / `approval_id` at the top level of those lines.

**The seal survives the round trip.** A spooled action carries `action_id`,
`sequence_number`, `prev_action_hash` and `self_hash` in its payload, and
`HttpIngestClient._seal` adopts an existing seal rather than minting a new one.
That is the whole reason a spooled record can reach the server as *the same
record* it would have been online, rather than a locally-consistent chain of
events that never happened.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rootsign._version import SDK_VERSION
from rootsign.ingest.schemas import (
    SCHEMA_VERSION,
    ActionRecordPayload,
    EventType,
    IngestEnvelope,
)

#: Subdirectories of the spool root. `sessions/` is the JSONL writer's own
#: layout (it always appends that segment); `synced/` is where fully-uploaded
#: files are retired to, so a re-run has nothing left to do.
SESSIONS_SUBDIR = "sessions"
SYNCED_SUBDIR = "synced"
#: Directory name `SDKSettings._derive_spool_dir` appends to `DATA_DIR`. Used
#: only to recognize a spool tree that has been copied somewhere else.
SPOOL_DIRNAME = "spool"

#: The exact command every spool-aware surface prints. One string, because
#: three places tell the user about it — the spool-mode WARNING, `rootsign
#: verify --local` on a spool file, and the docs — and a breadcrumb that has
#: drifted from the real command name is worse than none.
SYNC_BREADCRUMB = "rootsign-admin sync"

_ENVELOPE_FIELDS = tuple(IngestEnvelope.model_fields)
_ACTION_PAYLOAD_FIELDS = tuple(ActionRecordPayload.model_fields)
_EVENT_TYPES = {event.value for event in EventType}


class SpoolFormatError(Exception):
    """A spool file cannot be replayed as written.

    Raised rather than silently repaired: the file is evidence. Skipping the
    bad line, or reordering records into the sequence the chain expects, would
    upload a session that reads as complete while the truth stayed on disk.
    """


@dataclass(frozen=True)
class SpoolSession:
    """One spooled session file, parsed and ready to upload."""

    path: Path
    session_id: str
    envelopes: list[dict[str, Any]]
    #: Non-envelope lines skipped during parse — today only the loss ledger's
    #: `RECORD_LOSS` tally (ADR-013 Decision 4a), which documents records that
    #: never reached disk and so has nothing to upload.
    annotations: int = 0

    @property
    def action_count(self) -> int:
        return sum(1 for e in self.envelopes if e["event_type"] == EventType.ACTION_RECORD.value)

    @property
    def sequence_range(self) -> tuple[int, int] | None:
        sequences = [
            e["payload"]["sequence_number"]
            for e in self.envelopes
            if e["event_type"] == EventType.ACTION_RECORD.value
            and e["payload"].get("sequence_number") is not None
        ]
        return (min(sequences), max(sequences)) if sequences else None


def spool_root(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the spool directory — `--spool-dir`, else `SDKSettings.SPOOL_DIR`.

    Settings are read lazily so importing this module costs nothing and a test
    that monkeypatches `ROOTSIGN_DATA_DIR` is honored (SPOOL_DIR derives from
    DATA_DIR via a model validator, so relocating one moves the other).
    """
    if explicit is not None:
        return Path(explicit).expanduser()

    from rootsign.sdk.config import SDKSettings

    return Path(SDKSettings().SPOOL_DIR).expanduser()


def spool_files(root: str | os.PathLike[str] | None = None) -> list[Path]:
    """Every unsynced session file under the spool root, in a stable order.

    Sorted by name so two runs (and two operators) see the same order. Files
    already moved to `synced/` are not returned — that move is the marker.
    """
    sessions = spool_root(root) / SESSIONS_SUBDIR
    if not sessions.is_dir():
        return []
    return sorted(p for p in sessions.glob("*.jsonl") if p.is_file())


def is_spool_path(path: str | os.PathLike[str], root: str | os.PathLike[str] | None = None) -> bool:
    """True when `path` looks like it lives in a spool tree.

    Two tests, because a spool file is often examined somewhere other than
    where it was written (copied off a laptop, mounted from a container):
    inside the configured spool root, or matching the `spool/sessions/` shape
    wherever it sits. Only used to decide whether to print the sync
    breadcrumb, so a false positive costs one extra hint line.
    """
    candidate = Path(path).expanduser()
    try:
        resolved = candidate.resolve()
    except OSError:  # pragma: no cover - resolve() on a live path rarely fails
        resolved = candidate

    try:
        if resolved.is_relative_to(spool_root(root).resolve()):
            return True
    except (OSError, ValueError):  # unresolvable configured root
        pass

    parts = resolved.parts
    # `<anything>/spool/sessions/<id>.jsonl`. Both segments are required: the
    # ordinary local backend writes to `<data_dir>/sessions/` too, and telling
    # a JSONL user to run `rootsign-admin sync` would send them after an
    # upload that is not pending and a cloud account they may not have.
    return len(parts) >= 3 and parts[-2] == SESSIONS_SUBDIR and parts[-3] == SPOOL_DIRNAME


def read_spool_session(path: str | os.PathLike[str]) -> SpoolSession:
    """Parse one spool file into uploadable envelopes, in file order.

    File order *is* sequence order — the writer appends under a lock — so this
    does not sort. It verifies instead: a file whose action sequences are not
    strictly ascending is corrupt (a duplicate is a replayed chain), and
    uploading it would either fork the chain server-side or be rejected
    halfway. Better to refuse the file and leave it for a human than to make
    the store's copy the first place the corruption is visible.
    """
    source = Path(path).expanduser()
    lines = source.read_text().splitlines()
    envelopes: list[dict[str, Any]] = []
    annotations = 0
    session_id: str | None = None
    previous_sequence: int | None = None

    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            if number == len(lines):
                raise SpoolFormatError(
                    f"{source}: truncated final line — the writing process is probably still "
                    "running or crashed mid-append; re-run once it has exited"
                ) from exc
            raise SpoolFormatError(f"{source}: malformed JSON at line {number}") from exc

        event_type = record.get("event_type")
        if event_type not in _EVENT_TYPES:
            # RECORD_LOSS and anything else a future annotation adds. Inert to
            # both verifiers, and nothing the ingest endpoint would accept.
            annotations += 1
            continue

        if session_id is None:
            session_id = str(record.get("session_id"))

        if event_type == EventType.ACTION_RECORD.value:
            envelope = _envelope_from_action(record, source, number)
            sequence = envelope["payload"].get("sequence_number")
            if sequence is not None:
                if previous_sequence is not None and sequence <= previous_sequence:
                    raise SpoolFormatError(
                        f"{source}: sequence_number {sequence} at line {number} does not follow "
                        f"{previous_sequence} — the file is out of order or contains a replayed "
                        "chain. Verify it (`rootsign verify --local`) before uploading."
                    )
                previous_sequence = sequence
        else:
            envelope = _envelope_from_record(record, source, number)

        envelopes.append(envelope)

    if session_id is None:
        session_id = source.stem

    return SpoolSession(
        path=source, session_id=session_id, envelopes=envelopes, annotations=annotations
    )


def _envelope_from_action(record: dict[str, Any], source: Path, line: int) -> dict[str, Any]:
    """Re-nest a flattened ACTION_RECORD line into its envelope.

    Payload keys are taken from `ActionRecordPayload`'s own field list, so a
    field added to the wire schema needs no edit here — it is carried the
    moment the writer starts recording it. Keys absent from the line are left
    out rather than sent as null: the payload forbids extras but every field
    here is optional-or-present, and omitting keeps a 1.0-era record 1.0-era.
    """
    payload = {key: record[key] for key in _ACTION_PAYLOAD_FIELDS if key in record}
    if "tool_name" not in payload or "input_hash" not in payload:
        raise SpoolFormatError(
            f"{source}: ACTION_RECORD at line {line} is missing canonical fields "
            "(tool_name/input_hash) — not a record this SDK wrote"
        )
    return _envelope_shell(record, payload)


def _envelope_from_record(record: dict[str, Any], source: Path, line: int) -> dict[str, Any]:
    """Rebuild a non-action envelope, dropping the writer's own additions.

    SESSION_OPEN/CLOSE, DECISION_RECORD and APPROVAL_RECORD are stored as the
    envelope itself — but the writer stamps `decision_id` / `approval_id` at
    the top level of the line so the id it minted survives. `IngestEnvelope`
    forbids extras, so those have to come back off.
    """
    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise SpoolFormatError(
            f"{source}: {record.get('event_type')} at line {line} has no payload object"
        )
    return _envelope_shell(record, dict(payload))


def _envelope_shell(record: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    envelope = {key: record[key] for key in _ENVELOPE_FIELDS if key in record}
    envelope["payload"] = payload
    # Legacy or hand-made lines may predate these. The store checks the MAJOR
    # component only (spec §9), so a conservative default keeps an old file
    # uploadable instead of failing validation on a field nobody chose.
    envelope.setdefault("schema_version", SCHEMA_VERSION)
    envelope.setdefault("sdk_version", SDK_VERSION)
    return envelope


def mark_synced(path: str | os.PathLike[str], root: str | os.PathLike[str] | None = None) -> Path:
    """Retire a fully-uploaded file to `<spool>/synced/`. Returns the new path.

    Moving rather than deleting: the local copy is still a verifiable record of
    what the agent did, and an operator who wants it gone can empty one
    directory. A name collision (same session synced twice, or a file restored
    by hand) gets a numeric suffix rather than overwriting evidence.
    """
    source = Path(path).expanduser()
    destination_dir = spool_root(root) / SYNCED_SUBDIR
    destination_dir.mkdir(parents=True, exist_ok=True)

    destination = destination_dir / source.name
    counter = 1
    while destination.exists():
        destination = destination_dir / f"{source.stem}.{counter}{source.suffix}"
        counter += 1

    source.replace(destination)
    return destination
