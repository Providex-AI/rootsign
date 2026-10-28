"""Builds the cross-implementation sync-receipt vectors (issue #47).

Every byte of a vector comes out of production code: the spool file is written
by `HttpIngestClient` failing over while its endpoint is down, the receipt by
the real `rootsign-admin sync` command, and the evidence bundle by the real
`export` path. The only things this module decides are the *inputs* (which
envelopes, which store behaviour) and the values that would otherwise be
random or describe the machine that ran it (ids, clocks, the OS user, the
absolute spool path). A vector built by hand would be a vector that agrees
with the verifier by construction — the failure mode
`feedback-canonical-hash-never-reimplemented` exists to prevent.

Regenerate the committed set with:

    ROOTSIGN_UPDATE_VECTORS=1 python -m pytest tests/unit/test_sync_receipt_vectors.py

and review the diff. A changed receipt is a schema decision, not a refresh.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import itertools
import json
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from typer.testing import CliRunner

from rootsign.cli import app as admin_app
from rootsign.sdk.client import HttpIngestClient
from tests.support.mock_ingest_store import MockIngestStore
from tests.support.spool_tree import API_KEY, BASE_URL, Endpoint, bind_transport, dead_endpoint

VECTORS_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "sync-receipt" / "v1.1"

#: Pinned instants. Spooled 09:00, synced 10:00 / 11:00 — far enough apart that
#: nobody mistakes an attempt timestamp for a record timestamp.
RECORDED_AT = "2026-10-21T09:00:00+00:00"
ATTEMPT_TIMES = (
    datetime(2026, 10, 21, 10, 0, 0, tzinfo=timezone.utc),
    datetime(2026, 10, 21, 11, 0, 0, tzinfo=timezone.utc),
)
EXPORTED_AT = "2026-10-21T12:00:00+00:00"
OPERATOR = "vector-generator"

#: Receipt fields that describe the run, not the evidence. Pinned when a
#: vector is written; ignored when the suite re-derives a receipt to compare.
PROVENANCE_FIELDS = ("attempt_id", "started_at", "finished_at", "sdk_version", "operator")


def _uuid(lane: int, kind: int, n: int) -> str:
    """Readable, valid v4 UUIDs: lane, then what the id names, then a counter."""
    return f"{lane:08x}-{kind:04x}-4000-8000-{n:012x}"


AGENT, SESSION, EVENT, RECORD, ATTEMPT = 0xA, 0x5, 0xE, 0xA1, 0xA7


@dataclass(frozen=True)
class Lane:
    """One vector: what was recorded, what happened to it, how it was synced."""

    number: int
    slug: str
    title: str
    actions: int
    #: Mutates the spool file after recording, before any sync. Receives the
    #: path; must leave the file a well-formed spool file.
    damage: Callable[[Path], None] | None = None
    #: Builds the store the sync talks to. Called once per lane, so the same
    #: store answers every attempt (it remembers `event_id`s across them).
    store: Callable[[], Any] = MockIngestStore
    attempts: int = 1
    batch_size: int | None = None
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def directory(self) -> Path:
        return VECTORS_ROOT / self.slug

    @property
    def session_id(self) -> str:
        return _uuid(self.number, SESSION, 1)


# ---------------------------------------------------------------------------
# Damage — the two ways a chain goes wrong between recording and sync
# ---------------------------------------------------------------------------


def _edit_action_line(path: Path, sequence: int, edit: Callable[[str], str | None]) -> None:
    """Rewrite (or delete, when `edit` returns None) one ACTION line, in place.

    Text-level rather than parse-and-redump, so every other byte of the file
    is exactly what the writer produced — the vector shows an edit to a
    recorded file, not a re-serialization of it.
    """
    out: list[str] = []
    hit = False
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        record = json.loads(line)
        if (
            record.get("event_type") == "ACTION_RECORD"
            and record.get("sequence_number") == sequence
        ):
            hit = True
            replaced = edit(line)
            if replaced is not None:
                out.append(replaced)
            continue
        out.append(line)
    assert hit, f"no ACTION_RECORD with sequence {sequence} in {path}"
    path.write_text("".join(out), encoding="utf-8")


def tamper_tool_name(sequence: int, new_name: str) -> Callable[[Path], None]:
    def _damage(path: Path) -> None:
        def _edit(line: str) -> str:
            record = json.loads(line)
            # The writer emits compact separators; match its bytes exactly.
            old = f'"tool_name":{json.dumps(record["tool_name"])}'
            assert line.count(old) == 1, line
            return line.replace(old, f'"tool_name":{json.dumps(new_name)}')

        _edit_action_line(path, sequence, _edit)

    return _damage


def drop_sequence(sequence: int) -> Callable[[Path], None]:
    def _damage(path: Path) -> None:
        _edit_action_line(path, sequence, lambda _line: None)

    return _damage


LANES = (
    Lane(
        number=1,
        slug="v1-contiguous-tamper",
        title="Contiguous tamper: chain verdict TAMPERED, receipt records the store's HASH_CHAIN_BROKEN",
        actions=2,
        damage=tamper_tool_name(2, "wire_transfer"),
        notes={
            "recorded": "SESSION_OPEN, two ACTION_RECORDs (sequences 1-2), SESSION_CLOSE",
            "damage": "tool_name of sequence 2 edited in the spool file after recording, before sync",
            "store": "verifies each ACTION_RECORD's self_hash against its own canonical fields",
            "sync": "one attempt, batch size 100",
        },
    ),
    Lane(
        number=2,
        slug="v2-sequence-gap",
        title="Sequence gap: chain verdict INCOMPLETE, receipt shows no rejection for the missing record",
        actions=3,
        damage=drop_sequence(2),
        notes={
            "recorded": "SESSION_OPEN, three ACTION_RECORDs (sequences 1-3), SESSION_CLOSE",
            "damage": "the line for sequence 2 deleted from the spool file before sync",
            "store": (
                "verifies each ACTION_RECORD's self_hash against its own canonical fields; "
                "a dangling prev_action_hash is not a rejection (ingest spec section 8.3)"
            ),
            "sync": "one attempt, batch size 100",
        },
    ),
    Lane(
        number=3,
        slug="v3-duplicates-and-hard-failure",
        title="Replayed twice with one non-retryable refusal: all four outcome buckets observed",
        actions=4,
        store=functools.partial(Endpoint, reject_at_sequence=2),
        attempts=2,
        batch_size=2,
        notes={
            "recorded": "SESSION_OPEN, four ACTION_RECORDs (sequences 1-4), SESSION_CLOSE",
            "damage": "none — the chain is intact",
            "store": (
                "refuses sequence 2 with VALIDATION_ERROR (retryable: false) on every attempt; "
                "answers DUPLICATE_EVENT for any event_id it has already accepted"
            ),
            "sync": "two attempts against the same store, batch size 2",
        },
    ),
)


# ---------------------------------------------------------------------------
# Building one lane
# ---------------------------------------------------------------------------


def _envelope(lane: Lane, index: int, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.1",
        "sdk_version": "0.3.1",
        "event_type": event_type,
        "event_id": _uuid(lane.number, EVENT, index + 1),
        "emitted_at": RECORDED_AT,
        "agent_id": _uuid(lane.number, AGENT, 1),
        "session_id": lane.session_id,
        "payload": payload,
    }


def _session_envelopes(lane: Lane) -> list[dict[str, Any]]:
    envelopes = [_envelope(lane, 0, "SESSION_OPEN", {"objective": lane.slug})]
    for i in range(lane.actions):
        envelopes.append(
            _envelope(
                lane,
                len(envelopes),
                "ACTION_RECORD",
                {
                    "tool_name": ("send_email", "query_db", "charge_card", "write_file")[i],
                    "input_hash": hashlib.sha256(f"input {i + 1}".encode()).hexdigest(),
                    "output_hash": hashlib.sha256(f"output {i + 1}".encode()).hexdigest(),
                    "timestamp": RECORDED_AT,
                    "authorization_status": "auto_authorized",
                },
            )
        )
    envelopes.append(_envelope(lane, len(envelopes), "SESSION_CLOSE", {"status": "completed"}))
    return envelopes


def _spool(lane: Lane, spool_dir: Path) -> Path:
    """Record the session while the endpoint is down — the production spool path."""

    async def _run() -> None:
        client = HttpIngestClient(
            BASE_URL,
            API_KEY,
            max_retries=1,
            transport=httpx.MockTransport(dead_endpoint),
            spool_dir=str(spool_dir),
        )
        for envelope in _session_envelopes(lane):
            await client.handle(envelope)
        await client.close()

    asyncio.run(_run())
    return spool_dir / "sessions" / f"{lane.session_id}.jsonl"


@contextmanager
def _pinned(lane: Lane, mp: pytest.MonkeyPatch, *, pin_receipts: bool = True) -> Iterator[None]:
    """Pin every id and clock a lane's output would otherwise vary on."""
    import rootsign.replay as replay_module
    import rootsign.sdk.receipt as receipt_module

    records = itertools.count(1)
    mp.setattr(
        "rootsign.chain_state.uuid4", lambda: UUID(_uuid(lane.number, RECORD, next(records)))
    )

    if pin_receipts:
        real_build = receipt_module.build_attempt
        attempts = itertools.count(0)

        def _build(**kwargs: Any) -> dict[str, Any]:
            n = next(attempts)
            kwargs.update(
                attempt_id=_uuid(lane.number, ATTEMPT, n + 1),
                operator=OPERATOR,
                started_at=ATTEMPT_TIMES[n],
                finished_at=ATTEMPT_TIMES[n].replace(second=1),
            )
            return real_build(**kwargs)

        mp.setattr("rootsign.sdk.receipt.build_attempt", _build)

    if lane.batch_size is not None:
        mp.setattr(
            "rootsign.replay.replay_envelopes",
            functools.partial(replay_module.replay_envelopes, batch_size=lane.batch_size),
        )
    mp.setenv("ROOTSIGN_API_KEY", API_KEY)
    mp.setenv("ROOTSIGN_CLOUD_URL", BASE_URL)
    yield


def sync_attempts(lane: Lane, spool_dir: Path, mp: pytest.MonkeyPatch) -> list[int]:
    """Run `rootsign-admin sync` `lane.attempts` times against one store."""
    store = lane.store()
    bind_transport(mp, store.transport())
    runner = CliRunner()
    codes = []
    for _ in range(lane.attempts):
        result = runner.invoke(admin_app, ["sync", "--spool-dir", str(spool_dir)])
        if result.exception and not isinstance(result.exception, SystemExit):
            raise result.exception
        codes.append(result.exit_code)
    return codes


def located(spool_dir: Path, session_id: str) -> tuple[Path, Path]:
    """Where sync left the spool file and its receipt (live or retired)."""
    for spool_file, receipt in (
        (
            spool_dir / "sessions" / f"{session_id}.jsonl",
            spool_dir / "receipts" / f"{session_id}.sync.json",
        ),
        (
            spool_dir / "synced" / f"{session_id}.jsonl",
            spool_dir / "synced" / "receipts" / f"{session_id}.sync.json",
        ),
    ):
        if spool_file.is_file():
            return spool_file, receipt
    raise FileNotFoundError(f"spool file for {session_id} not found under {spool_dir}")


def build_lane(lane: Lane, work: Path) -> Path:
    """Produce a lane's complete vector directory under `work`. Returns it."""
    from rootsign.sdk.chain import verify_session_local
    from rootsign.sdk.export import export_local, write_bundle

    out = work / lane.slug
    spool_dir = out / "spool"
    with pytest.MonkeyPatch.context() as mp, _pinned(lane, mp):
        spool_file = _spool(lane, spool_dir)
        if lane.damage is not None:
            lane.damage(spool_file)
        exit_codes = sync_attempts(lane, spool_dir, mp)

        spool_file, receipt_file = located(spool_dir, lane.session_id)
        verdict = verify_session_local(str(spool_file))

        bundle = export_local(spool_file)
        bundle.generated_at = EXPORTED_AT
        # The absolute path of the generating machine is not evidence, and
        # would make every regeneration a diff.
        bundle.source_location = spool_file.relative_to(out).as_posix()
        bundle_dir = write_bundle(bundle, out / "bundle")

    expected = {
        "vector": lane.slug,
        "title": lane.title,
        "receipt_schema_version": "receipt/1.1",
        "scenario": lane.notes,
        "files": {
            "spool_file": spool_file.relative_to(out).as_posix(),
            "receipt": receipt_file.relative_to(out).as_posix(),
            "bundle": bundle_dir.relative_to(out).as_posix(),
        },
        "chain": {
            "verdict": verdict.verdict.value,
            "first_invalid_sequence": verdict.first_invalid_sequence,
            "missing_ranges": [list(r) for r in verdict.missing_ranges],
        },
        "sync_exit_codes": exit_codes,
        "attempts": [
            _expected_attempt(attempt)
            for attempt in json.loads(receipt_file.read_text(encoding="utf-8"))["attempts"]
        ],
    }
    (out / "expected.json").write_text(
        json.dumps(expected, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return out


def _expected_attempt(attempt: dict[str, Any]) -> dict[str, Any]:
    """What a conforming implementation must reproduce — the non-provenance part."""
    keep = {key: value for key, value in attempt.items() if key not in PROVENANCE_FIELDS}
    keep.pop("endpoint", None)
    for entry in keep["outcomes"]["rejected"]:
        # Free text. The code and retryability are the contract; the prose is
        # whatever the store chose to say.
        entry.pop("error_message", None)
    keep.pop("transport_error", None)
    return keep


def build_all(destination: Path) -> list[Path]:
    """Build every lane into `destination`, replacing what was there."""
    built = []
    for lane in LANES:
        target = destination / lane.slug
        if target.exists():
            shutil.rmtree(target)
        built.append(build_lane(lane, destination))
    return built


def sums_text(root: Path) -> str:
    """`SHA256SUMS` for the set: every file under `root` but itself.

    `sha256sum -c` format, sorted, POSIX paths — so a mirror can be checked
    with coreutils alone, before anyone runs any code from it.
    """
    lines = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root)
        if relative.name == "SHA256SUMS" or "__pycache__" in relative.parts:
            continue
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative.as_posix()}")
    return "\n".join(lines) + "\n"
