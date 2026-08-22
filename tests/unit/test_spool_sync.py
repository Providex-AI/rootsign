"""The spool, read back and uploaded — `rootsign-admin sync` (ADR-013 D4, T2.5).

Two halves, both driven off a **real spool tree**: a session recorded through
`HttpIngestClient` against a dead endpoint, so the files under test were
written by the code that writes them in production rather than by a fixture
that agrees with the parser by construction.

* `rootsign.sdk.spool` — turning stored records back into envelopes. Records
  are not envelopes (the writer flattens actions, and `IngestEnvelope` forbids
  extras), and the seal has to survive the round trip or the uploaded record is
  a different record.
* `rootsign-admin sync` — the operator command. The property that matters most
  here is the one that has no analogue online: a sync whose endpoint is *still*
  down must not fail over into the spool, because the file it would append to
  is the file it is reading.

The full mock-server matrix (every rejection code, the buffered variant) is
T2.6 in `tests/contract/cloud/`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from typer.testing import CliRunner

from rootsign.cli import app as admin_app
from rootsign.errors import RootSignCloudExtraRequired
from rootsign.ingest.schemas import PAYLOAD_SCHEMAS, ErrorCode, EventType, IngestEnvelope
from rootsign.sdk.cli import app as user_app
from rootsign.sdk.client import HttpIngestClient
from rootsign.sdk.jsonl_client import JsonlIngestClient
from rootsign.sdk.spool import (
    SYNC_BREADCRUMB,
    SpoolFormatError,
    is_spool_path,
    mark_synced,
    read_spool_session,
    spool_files,
)
from tests.conftest import make_envelope

runner = CliRunner()

BASE_URL = "https://ingest.example.test/v1"
API_KEY = "sk-sync-test"


# ---------------------------------------------------------------------------
# A real spool tree
# ---------------------------------------------------------------------------


def _dead_endpoint(request: httpx.Request) -> httpx.Response:
    return httpx.Response(503, text="down")


def _spool_a_session(spool_dir: Path, *, actions: int = 3) -> str:
    """Record a session while the endpoint is down; return its session_id.

    `max_retries=1` keeps the backoff sleeps out of the test — the failover
    path is what is being exercised, not the retry budget (that is
    `test_http_ingest_client.py`).
    """

    async def _run() -> str:
        client = HttpIngestClient(
            BASE_URL,
            API_KEY,
            max_retries=1,
            transport=httpx.MockTransport(_dead_endpoint),
            spool_dir=str(spool_dir),
        )
        agent_id, session_id = uuid4(), uuid4()
        await client.handle(make_envelope("SESSION_OPEN", agent_id, session_id, {"objective": "s"}))
        for i in range(actions):
            await client.handle(
                make_envelope(
                    "ACTION_RECORD",
                    agent_id,
                    session_id,
                    {
                        "tool_name": f"tool_{i}",
                        "input_hash": f"{i:064d}",
                        "output_hash": "b" * 64,
                        "timestamp": "2026-08-21T09:00:00+00:00",
                        "authorization_status": "auto_authorized",
                    },
                )
            )
        await client.handle(
            make_envelope("SESSION_CLOSE", agent_id, session_id, {"status": "completed"})
        )
        await client.close()
        return str(session_id)

    return asyncio.run(_run())


@pytest.fixture
def spool(tmp_path: Path) -> Path:
    return tmp_path / "spool"


@pytest.fixture
def spooled_session(spool: Path) -> tuple[str, Path]:
    session_id = _spool_a_session(spool)
    return session_id, spool / "sessions" / f"{session_id}.jsonl"


class _Endpoint:
    """A mock ingest endpoint that records what it was given.

    Answers index-aligned per spec §7.1, and remembers `event_id`s so a second
    run can answer DUPLICATE_EVENT — which is how server-side idempotency shows
    up to a resumed sync.
    """

    def __init__(self, *, reject_at_sequence: int | None = None) -> None:
        self.batches: list[list[dict[str, Any]]] = []
        self.seen: set[str] = set()
        self._reject_at = reject_at_sequence

    @property
    def envelopes(self) -> list[dict[str, Any]]:
        return [envelope for batch in self.batches for envelope in batch]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        batch = json.loads(request.content)
        self.batches.append(batch)
        return httpx.Response(200, json=[self._answer(envelope) for envelope in batch])

    def _answer(self, envelope: dict[str, Any]) -> dict[str, Any]:
        event_id = envelope["event_id"]
        if event_id in self.seen:
            return {
                "status": "rejected",
                "event_id": event_id,
                "error_code": ErrorCode.DUPLICATE_EVENT.value,
                "error_message": "already ingested",
                "retryable": False,
            }
        sequence = (envelope.get("payload") or {}).get("sequence_number")
        if self._reject_at is not None and sequence == self._reject_at:
            return {
                "status": "rejected",
                "event_id": event_id,
                "error_code": ErrorCode.VALIDATION_ERROR.value,
                "error_message": f"sequence {sequence} refused by the mock",
                "retryable": False,
            }
        self.seen.add(event_id)
        return {
            "status": "accepted",
            "event_id": event_id,
            "entity_id": str(uuid4()),
            "sequence_number": sequence,
            "self_hash": (envelope.get("payload") or {}).get("self_hash"),
        }


@pytest.fixture
def cloud_api_key(monkeypatch) -> None:
    monkeypatch.setenv("ROOTSIGN_API_KEY", API_KEY)


def _bind_transport(monkeypatch, transport: Any) -> None:
    """Make the transport the CLI's own `HttpIngestClient` will use.

    The command imports the class inside the function, so patching the module
    attribute is enough — and it keeps the CLI's own constructor arguments
    (including `enable_spool=False`) under test instead of bypassed.
    """
    real = HttpIngestClient

    def factory(**kwargs: Any) -> HttpIngestClient:
        return real(transport=transport, **kwargs)

    monkeypatch.setattr("rootsign.sdk.client.HttpIngestClient", factory)


# ---------------------------------------------------------------------------
# Reading the spool
# ---------------------------------------------------------------------------


class TestReadSpoolSession:
    def test_stored_records_become_valid_envelopes_again(self, spooled_session):
        """Every reconstructed envelope must pass the wire schema.

        `IngestEnvelope` is `extra="forbid"` and so is `ActionRecordPayload`,
        so "send the line as-is" fails on both counts — the writer flattens
        actions and stamps ids on non-actions. Validating here is the same
        check the server would run, done before the upload rather than after.
        """
        session_id, path = spooled_session
        session = read_spool_session(path)

        assert session.session_id == session_id
        assert len(session.envelopes) == 5  # OPEN + 3 actions + CLOSE
        assert session.action_count == 3
        assert session.sequence_range == (1, 3)

        for envelope in session.envelopes:
            validated = IngestEnvelope.model_validate(envelope)
            PAYLOAD_SCHEMAS[validated.event_type].model_validate(envelope["payload"])

    def test_the_seal_survives_the_round_trip(self, spooled_session):
        """A spooled record must upload as *the same record*.

        Its `self_hash` is the one the chain committed to on disk; re-minting
        identity on the way out would produce a locally consistent chain of
        events that never happened, and the file the auditor holds would no
        longer match the store.
        """
        _, path = spooled_session
        stored = [
            json.loads(line)
            for line in path.read_text().splitlines()
            if json.loads(line).get("event_type") == EventType.ACTION_RECORD.value
        ]
        replayed = [
            e["payload"]
            for e in read_spool_session(path).envelopes
            if e["event_type"] == EventType.ACTION_RECORD.value
        ]

        for record, payload in zip(stored, replayed, strict=True):
            for field in ("action_id", "sequence_number", "prev_action_hash", "self_hash"):
                assert payload[field] == record[field], field

    def test_non_canonical_payload_fields_survive_the_spool(self, spool):
        """`duration_ms` and `policy_id` are excluded from `self_hash` (ADR-001)
        but are part of the wire payload.

        A field the writer drops is a field the store never receives — the
        record that eventually arrives would differ from the one that would
        have arrived online, silently, and only for sessions that happened to
        spool. Nothing in the hash would notice, which is the whole reason to
        assert it here.
        """
        policy_id = str(uuid4())

        async def _run() -> tuple[str, Path]:
            client = HttpIngestClient(
                BASE_URL,
                API_KEY,
                max_retries=1,
                transport=httpx.MockTransport(_dead_endpoint),
                spool_dir=str(spool),
            )
            agent_id, session_id = uuid4(), uuid4()
            await client.handle(
                make_envelope(
                    "ACTION_RECORD",
                    agent_id,
                    session_id,
                    {
                        "tool_name": "charge_card",
                        "input_hash": "a" * 64,
                        "output_hash": "b" * 64,
                        "timestamp": "2026-08-21T09:00:00+00:00",
                        "authorization_status": "auto_authorized",
                        "duration_ms": 1234,
                        "policy_id": policy_id,
                    },
                )
            )
            await client.close()
            return str(session_id), spool / "sessions" / f"{session_id}.jsonl"

        _, path = asyncio.run(_run())
        payload = read_spool_session(path).envelopes[0]["payload"]

        assert payload["duration_ms"] == 1234
        assert payload["policy_id"] == policy_id
        PAYLOAD_SCHEMAS[EventType.ACTION_RECORD].model_validate(payload)

    def test_annotation_lines_are_counted_not_uploaded(self, spooled_session):
        """`RECORD_LOSS` documents records that never reached disk (D4a).

        There is nothing to upload for a record that does not exist, and the
        ingest endpoint has no such event type — but the line must not be
        mistaken for corruption either.
        """
        session_id, path = spooled_session
        JsonlIngestClient(data_dir=path.parent.parent).append_annotation(
            session_id, {"event_type": "RECORD_LOSS", "session_id": session_id, "count": 2}
        )

        session = read_spool_session(path)

        assert session.annotations == 1
        assert len(session.envelopes) == 5
        assert all(e["event_type"] != "RECORD_LOSS" for e in session.envelopes)

    def test_a_repeated_sequence_refuses_to_upload(self, spooled_session):
        """A duplicated action line is a replayed chain, not a resend.

        Uploading it would either fork the chain server-side or be rejected
        halfway, leaving the store's copy as the first place the corruption is
        visible. Refuse the file instead and leave it for a human.
        """
        _, path = spooled_session
        lines = path.read_text().splitlines()
        path.write_text("\n".join([*lines, lines[2]]) + "\n")

        with pytest.raises(SpoolFormatError, match="does not follow"):
            read_spool_session(path)

    def test_a_malformed_line_refuses_to_upload(self, spooled_session):
        _, path = spooled_session
        lines = path.read_text().splitlines()
        lines[1] = "{not json"
        path.write_text("\n".join(lines) + "\n")

        with pytest.raises(SpoolFormatError, match="malformed JSON at line 2"):
            read_spool_session(path)

    def test_a_truncated_final_line_says_so(self, spooled_session):
        """The tell-tale of a live writer or a crash mid-append — worth a
        different sentence than corruption, because the fix is 'wait'."""
        _, path = spooled_session
        path.write_text(path.read_text() + '{"event_type": "ACTION_REC')

        with pytest.raises(SpoolFormatError, match="truncated final line"):
            read_spool_session(path)


class TestSpoolLayout:
    def test_only_unsynced_files_are_listed(self, spool, spooled_session):
        _, path = spooled_session
        assert spool_files(spool) == [path]

        mark_synced(path, spool)

        assert spool_files(spool) == []
        assert (spool / "synced" / path.name).exists()

    def test_marking_synced_never_overwrites_an_earlier_file(self, spool, spooled_session):
        """Evidence is moved, not replaced. A same-named file already in
        `synced/` means the session was synced before (or restored by hand);
        either way the older copy is not ours to delete."""
        _, path = spooled_session
        first = mark_synced(path, spool)
        path.write_text('{"event_type": "SESSION_OPEN"}\n')
        second = mark_synced(path, spool)

        assert first != second
        assert first.exists() and second.exists()

    def test_is_spool_path_does_not_fire_on_an_ordinary_session_file(self, tmp_path, spool):
        """The JSONL backend writes `<data_dir>/sessions/` too. Pointing a
        plain local user at `rootsign-admin sync` would send them after an
        upload that is not pending, to a cloud account they may not have."""
        spooled = spool / "sessions" / "a.jsonl"
        ordinary = tmp_path / "data" / "sessions" / "a.jsonl"
        for target in (spooled, ordinary):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("")

        assert is_spool_path(spooled) is True
        assert is_spool_path(ordinary) is False


# ---------------------------------------------------------------------------
# rootsign-admin sync
# ---------------------------------------------------------------------------


class TestSyncCommand:
    def test_nothing_to_sync_is_not_a_failure(self, spool):
        result = runner.invoke(admin_app, ["sync", "--spool-dir", str(spool)])
        assert result.exit_code == 0, result.output
        assert "Nothing to sync" in result.output

    def test_dry_run_contacts_nothing(self, monkeypatch, spool, spooled_session, cloud_api_key):
        """`--dry-run` must work on a bare install and an unreachable network:
        it is how an operator finds out what is waiting before deciding to
        upload anything."""
        session_id, path = spooled_session

        def explode(**kwargs: Any):
            raise AssertionError("--dry-run constructed a transport")

        monkeypatch.setattr("rootsign.sdk.client.HttpIngestClient", explode)

        result = runner.invoke(admin_app, ["sync", "--dry-run", "--spool-dir", str(spool)])

        assert result.exit_code == 0, result.output
        assert session_id in result.output
        assert "actions 1-3" in result.output
        assert path.exists(), "--dry-run moved a file"

    def test_a_successful_sync_uploads_every_record_and_retires_the_file(
        self, monkeypatch, spool, spooled_session, cloud_api_key
    ):
        session_id, path = spooled_session
        endpoint = _Endpoint()
        _bind_transport(monkeypatch, endpoint.transport())

        result = runner.invoke(admin_app, ["sync", "--spool-dir", str(spool)])

        assert result.exit_code == 0, result.output
        assert "All spooled sessions uploaded." in result.output
        assert [e["event_type"] for e in endpoint.envelopes] == [
            EventType.SESSION_OPEN.value,
            *[EventType.ACTION_RECORD.value] * 3,
            EventType.SESSION_CLOSE.value,
        ]
        assert not path.exists()
        assert (spool / "synced" / path.name).exists()

    def test_a_rejected_record_leaves_the_file_and_names_the_sequence(
        self, monkeypatch, spool, spooled_session, cloud_api_key
    ):
        """The file is the only copy of what the store would not take, so it
        stays. Naming the sequence tells the operator where the next run
        resumes and whether the rejection is theirs to fix."""
        session_id, path = spooled_session
        before = path.read_text()
        _bind_transport(monkeypatch, _Endpoint(reject_at_sequence=2).transport())

        result = runner.invoke(admin_app, ["sync", "--spool-dir", str(spool)])

        assert result.exit_code == 1, result.output
        assert "rejected at sequence 2" in result.output
        assert "VALIDATION_ERROR" in result.output
        assert path.exists() and path.read_text() == before
        assert not (spool / "synced").exists()

    def test_a_resumed_sync_treats_duplicates_as_delivered(
        self, monkeypatch, spool, spooled_session, cloud_api_key
    ):
        """The second run re-sends what the first one landed. Server-side
        idempotency answers DUPLICATE_EVENT, and that has to count as success
        or a partially-uploaded session could never finish."""
        _, path = spooled_session
        endpoint = _Endpoint(reject_at_sequence=2)
        _bind_transport(monkeypatch, endpoint.transport())
        first = runner.invoke(admin_app, ["sync", "--spool-dir", str(spool)])
        assert first.exit_code == 1

        endpoint._reject_at = None  # the operator fixed whatever it was
        second = runner.invoke(admin_app, ["sync", "--spool-dir", str(spool)])

        assert second.exit_code == 0, second.output
        assert "already present" in second.output
        assert not path.exists()
        assert (spool / "synced" / path.name).exists()

    def test_a_still_dead_endpoint_never_spools_into_the_file_it_is_reading(
        self, monkeypatch, spool, spooled_session, cloud_api_key
    ):
        """The failure mode `enable_spool=False` exists to prevent.

        A replay client that failed over would append the records it is
        *reading* back into the same session file — same session id, same
        writer, same path — duplicating sequence numbers and turning a
        recoverable outage into a file that verifies TAMPERED. The spool file
        is already the durable copy; there is nowhere to fall back to.
        """
        _, path = spooled_session
        before = path.read_text()
        # Pin the *configured* spool root to the directory being synced, which
        # is the real-world case (`sync` with no --spool-dir). Without this the
        # failover would land somewhere else and the file comparison below
        # would pass for the wrong reason.
        monkeypatch.setenv("ROOTSIGN_SPOOL_DIR", str(spool))
        _bind_transport(monkeypatch, httpx.MockTransport(_dead_endpoint))

        result = runner.invoke(admin_app, ["sync", "--spool-dir", str(spool)])

        assert result.exit_code == 1, result.output
        assert "did not finish" in result.output
        assert path.read_text() == before, "sync wrote back into the file it was uploading"
        assert len(list((spool / "sessions").iterdir())) == 1
        assert not (spool / "synced").exists()

    def test_a_missing_api_key_is_an_actionable_error(self, monkeypatch, spool, spooled_session):
        monkeypatch.setenv("ROOTSIGN_API_KEY", "")

        result = runner.invoke(admin_app, ["sync", "--spool-dir", str(spool)])

        assert result.exit_code == 1
        assert "ROOTSIGN_API_KEY is not set" in result.output
        assert "--dry-run" in result.output
        assert "Traceback" not in result.output

    def test_without_the_cloud_extra_the_error_names_the_extra(
        self, monkeypatch, spool, spooled_session, cloud_api_key
    ):
        """A bare install must get the install command, not a
        ModuleNotFoundError from inside httpx's import graph (ADR-011's
        packaging discipline, applied to the second extra)."""

        def missing(**kwargs: Any):
            raise RootSignCloudExtraRequired("missing module: httpx")

        monkeypatch.setattr("rootsign.sdk.client.HttpIngestClient", missing)

        result = runner.invoke(admin_app, ["sync", "--spool-dir", str(spool)])

        assert result.exit_code == 1
        assert "rootsign[cloud]" in result.output
        assert "Traceback" not in result.output

    def test_a_corrupt_file_is_skipped_without_stranding_the_others(
        self, monkeypatch, spool, spooled_session, cloud_api_key
    ):
        """One bad file must not hold up the sessions that are intact — the
        operator running this after an outage wants what can be uploaded
        uploaded, and one clear line about what cannot."""
        _, good = spooled_session
        corrupt = spool / "sessions" / "00000000-0000-4000-8000-000000000000.jsonl"
        corrupt.write_text("{not json\n{}\n")
        _bind_transport(monkeypatch, _Endpoint().transport())

        result = runner.invoke(admin_app, ["sync", "--spool-dir", str(spool)])

        assert result.exit_code == 1, result.output
        assert "SKIPPED" in result.output
        assert not good.exists(), "an intact session was stranded by a corrupt neighbor"
        assert corrupt.exists()

    def test_a_file_that_is_not_text_is_skipped_too(
        self, monkeypatch, spool, spooled_session, cloud_api_key
    ):
        """Corruption does not always produce valid UTF-8.

        A half-written or bit-rotted file raises on decode, well before any
        JSON parsing — an exception class the format guard does not cover, and
        an uncaught one would end the run with a traceback instead of a
        SKIPPED line.
        """
        _, good = spooled_session
        binary = spool / "sessions" / "11111111-1111-4111-8111-111111111111.jsonl"
        binary.write_bytes(b"\xff\xfe\x00 not text at all\n")
        _bind_transport(monkeypatch, _Endpoint().transport())

        result = runner.invoke(admin_app, ["sync", "--spool-dir", str(spool)])

        assert result.exit_code == 1, result.output
        assert "SKIPPED" in result.output
        assert "Traceback" not in result.output
        assert not good.exists(), "an intact session was stranded by an unreadable neighbor"


class TestSyncBreadcrumb:
    """Discoverability: `sync` is on the operator CLI, but the person who
    spooled is on the developer one (ADR-013 Decision 4)."""

    def test_verifying_a_spool_file_prints_the_sync_command(self, spooled_session):
        _, path = spooled_session

        result = runner.invoke(user_app, ["verify", "--local", str(path)])

        assert result.exit_code == 0, result.output
        assert "VALID" in result.output
        assert SYNC_BREADCRUMB in result.output

    def test_verifying_an_ordinary_session_file_does_not(self, tmp_path):
        data_dir = tmp_path / "data"

        async def _write() -> str:
            client = JsonlIngestClient(data_dir=data_dir)
            agent_id, session_id = uuid4(), uuid4()
            await client.handle(make_envelope("SESSION_OPEN", agent_id, session_id, {}))
            await client.handle(
                make_envelope(
                    "ACTION_RECORD",
                    agent_id,
                    session_id,
                    {
                        "tool_name": "t",
                        "input_hash": "a" * 64,
                        "output_hash": None,
                        "timestamp": "2026-08-21T09:00:00+00:00",
                        "authorization_status": "auto_authorized",
                    },
                )
            )
            return str(session_id)

        session_id = asyncio.run(_write())
        path = data_dir / "sessions" / f"{session_id}.jsonl"

        result = runner.invoke(user_app, ["verify", "--local", str(path)])

        assert result.exit_code == 0, result.output
        assert SYNC_BREADCRUMB not in result.output
