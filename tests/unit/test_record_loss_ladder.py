"""The degradation ladder when the spool itself fails (ADR-013 Decision 4a, T2.4a).

Bottom rung: the endpoint is unreachable *and* the local spool cannot be
written. What happens then splits by what the record is —

  * telemetry drops with accounting (one CRITICAL, a loss ledger, and a
    hash-evident gap), because ADR-002 says the agent keeps running;
  * controls fail closed, because an Approval record is the authorization
    itself and a control whose record can be lost is not a control.

`ROOTSIGN_ON_RECORD_LOSS=fail` moves the telemetry path onto the control path's
side of that line. The real read-only-filesystem case is T2.8; here the spool
is a stand-in that raises the same `OSError` family, so the branches can be
driven precisely.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from rootsign.errors import HiTLPersistenceError, RecordPersistenceError
from rootsign.ingest.schemas import EventType
from rootsign.sdk.buffered_client import BufferedIngestClient
from rootsign.sdk.client import HttpIngestClient
from rootsign.sdk.context import SessionContext
from rootsign.sdk.decorator import _try_ingest
from rootsign.sdk.jsonl_client import JsonlIngestClient
from rootsign.sdk.loss_ledger import LOSS_RECORD_EVENT_TYPE, LossLedger

BASE_URL = "https://ingest.example.test/v1"


def _envelope(session_id: str, **overrides: Any) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "schema_version": "1.1",
        "sdk_version": "0.3.0",
        "event_type": EventType.ACTION_RECORD.value,
        "event_id": str(uuid4()),
        "emitted_at": "2026-08-20T12:00:00+00:00",
        "agent_id": str(uuid4()),
        "session_id": session_id,
        "payload": {
            "tool_name": "send_invoice",
            "input_hash": "a" * 64,
            "output_hash": "b" * 64,
            "timestamp": "2026-08-20T12:00:00+00:00",
            "authorization_status": "auto_authorized",
        },
    }
    envelope.update(overrides)
    return envelope


class _DeadSpool:
    """A spool whose every write fails, the way a full disk does."""

    def __init__(self, exc: OSError | None = None) -> None:
        self.exc = exc or OSError(28, "No space left on device")
        self.annotations: list[dict] = []

    async def handle(self, envelope: dict[str, Any]) -> Any:
        raise self.exc

    def append_annotation(self, session_id: str, record: dict[str, Any]) -> None:
        raise self.exc


class _RecoveringSpool(_DeadSpool):
    """Writes fail, but the annotation lands — writability returned at close."""

    def append_annotation(self, session_id: str, record: dict[str, Any]) -> None:
        self.annotations.append(record)


def _offline_client(spool: Any, **kwargs: Any) -> HttpIngestClient:
    """A client whose endpoint is down, so everything routes to `spool`."""
    return HttpIngestClient(
        BASE_URL,
        "sk-loss",
        transport=httpx.MockTransport(lambda request: httpx.Response(503)),
        max_retries=1,
        spool=spool,
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rootsign.sdk.client._backoff_delay", lambda attempt, retry_after: 0.0)


# ---------------------------------------------------------------------------
# Telemetry: drop with accounting
# ---------------------------------------------------------------------------


async def test_a_lost_telemetry_record_never_raises_into_the_caller() -> None:
    """ADR-002 still binds the telemetry path, even at the bottom rung."""
    session_id = str(uuid4())
    client = _offline_client(_DeadSpool())

    response = await client.handle(_envelope(session_id))

    assert response.status == "rejected"
    assert response.retryable is False  # never re-queued: the sink is dead
    assert "record dropped" in (response.error_message or "")


async def test_first_loss_logs_critical_once_and_the_rest_are_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A full disk should leave one alarming line, not one per record."""
    caplog.set_level("DEBUG")
    session_id = str(uuid4())
    client = _offline_client(_DeadSpool())

    for _ in range(6):
        await client.handle(_envelope(session_id))

    criticals = [r for r in caplog.records if r.levelname == "CRITICAL"]
    assert len(criticals) == 1
    assert "RECORD LOST" in criticals[0].getMessage()
    assert client.loss_ledger(session_id).count == 6


async def test_the_ledger_records_count_range_and_reason() -> None:
    session_id = str(uuid4())
    client = _offline_client(_DeadSpool(PermissionError(13, "Read-only file system")))

    for _ in range(3):
        await client.handle(_envelope(session_id))

    ledger = client.loss_ledger(session_id)
    assert ledger.count == 3
    # Sealing happens before the write attempt, so the sequence numbers the
    # lost records consumed are known — that range is the gap to look for.
    assert (ledger.first_sequence, ledger.last_sequence) == (1, 3)
    assert ledger.sequence_range == "1-3"
    assert any("PermissionError" in reason for reason in ledger.reasons)


async def test_ledgers_are_per_session() -> None:
    a, b = str(uuid4()), str(uuid4())
    client = _offline_client(_DeadSpool())

    await client.handle(_envelope(a))
    await client.handle(_envelope(b))
    await client.handle(_envelope(b))

    assert client.loss_ledger(a).count == 1
    assert client.loss_ledger(b).count == 2


async def test_the_chain_advances_past_a_lost_record_so_the_gap_is_provable(
    tmp_path,
) -> None:
    """The load-bearing property: what survives is not the log, it's the chain.

    A record that could not be written still consumed its sequence number and
    still extended the tail, so the next record that *does* land points at a
    predecessor nobody can produce. Logs rotate and the ledger is in memory;
    this discontinuity is in the evidence itself.
    """
    session_id = str(uuid4())
    dead = _DeadSpool()
    client = _offline_client(dead)

    await client.handle(_envelope(session_id))  # sequence 1 — lands nowhere
    await client.handle(_envelope(session_id))  # sequence 2 — also lost

    # Writability returns: swap in a real spool sharing the same chain.
    recovered = JsonlIngestClient(data_dir=tmp_path, chains=client.chains)
    client._spool = recovered
    await client.handle(_envelope(session_id))

    written = [
        json.loads(ln)
        for ln in (tmp_path / "sessions" / f"{session_id}.jsonl").read_text().splitlines()
    ]
    actions = [r for r in written if r["event_type"] == EventType.ACTION_RECORD.value]
    assert len(actions) == 1
    # Not sequence 1: the chain counted what it lost.
    assert actions[0]["sequence_number"] == 3
    # And its predecessor hash names a record that exists nowhere.
    assert actions[0]["prev_action_hash"] is not None
    assert actions[0]["prev_action_hash"] not in {r.get("self_hash") for r in written}


# ---------------------------------------------------------------------------
# Controls: fail closed
# ---------------------------------------------------------------------------


async def test_a_pending_action_record_fails_closed() -> None:
    """The HiTL submission is the thing an approval votes on."""
    session_id = str(uuid4())
    client = _offline_client(_DeadSpool())
    envelope = _envelope(session_id)
    envelope["payload"]["authorization_status"] = "pending"

    with pytest.raises(HiTLPersistenceError) as excinfo:
        await client.handle(envelope)

    assert "send_invoice" in str(excinfo.value)
    assert "was not executed" in str(excinfo.value)
    # Nothing to account for — the caller is learning the action did not happen.
    assert client.loss_ledger(session_id) is None


async def test_an_approval_record_fails_closed() -> None:
    session_id = str(uuid4())
    client = _offline_client(_DeadSpool())
    envelope = _envelope(
        session_id,
        event_type=EventType.APPROVAL_RECORD.value,
        payload={"action_id": str(uuid4()), "decision": "approved", "approver_id": "sile"},
    )

    with pytest.raises(HiTLPersistenceError):
        await client.handle(envelope)


async def test_controls_fail_closed_even_under_the_default_warn_setting() -> None:
    """ON_RECORD_LOSS moves the telemetry line only — never the control line."""
    session_id = str(uuid4())
    client = _offline_client(_DeadSpool(), on_record_loss="warn")
    envelope = _envelope(session_id)
    envelope["payload"]["authorization_status"] = "pending"

    with pytest.raises(HiTLPersistenceError):
        await client.handle(envelope)


async def test_a_control_failure_survives_the_buffering_wrapper() -> None:
    """Control records passthrough (ADR-009 Decision 2), so the raise reaches out."""
    session_id = str(uuid4())
    client = _offline_client(_DeadSpool())
    buffered = BufferedIngestClient(client, max_buffer_size=1000)
    envelope = _envelope(session_id)
    envelope["payload"]["authorization_status"] = "pending"

    with pytest.raises(HiTLPersistenceError):
        await buffered.handle(envelope)

    buffered._closed = True


async def test_the_decorator_does_not_swallow_a_persistence_failure() -> None:
    """`_try_ingest` swallows everything else; this is the documented exception.

    Without the exemption the HiTL gate would log a warning and keep going —
    exactly the ungoverned execution the fail-closed rule exists to prevent.
    """
    session_id = str(uuid4())
    client = _offline_client(_DeadSpool())
    ctx = SessionContext(agent_id=uuid4())
    ctx.session_id = session_id  # type: ignore[misc]

    with pytest.raises(HiTLPersistenceError):
        await _try_ingest(
            client=client,
            ctx=ctx,
            tool_name="wire_transfer",
            input_hash="a" * 64,
            output_hash=None,
            redacted_input={"amount": 1},
            redacted_output=None,
            timestamp=datetime.now(timezone.utc),
            authorization_status="pending",
        )


# ---------------------------------------------------------------------------
# ON_RECORD_LOSS=fail
# ---------------------------------------------------------------------------


async def test_fail_mode_raises_on_telemetry_too() -> None:
    session_id = str(uuid4())
    client = _offline_client(_DeadSpool(), on_record_loss="fail")

    with pytest.raises(RecordPersistenceError) as excinfo:
        await client.handle(_envelope(session_id))

    assert "ON_RECORD_LOSS=fail" in str(excinfo.value)
    # Accounting happens first: the raise does not cost the operator the tally.
    assert client.loss_ledger(session_id).count == 1


async def test_fail_mode_is_read_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROOTSIGN_ON_RECORD_LOSS", "fail")
    client = _offline_client(_DeadSpool())

    with pytest.raises(RecordPersistenceError):
        await client.handle(_envelope(str(uuid4())))


async def test_warn_is_the_default() -> None:
    client = _offline_client(_DeadSpool())
    assert client.on_record_loss == "warn"


# ---------------------------------------------------------------------------
# Session close: the tally lands next to the run
# ---------------------------------------------------------------------------


async def test_session_close_relogs_the_tally(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    session_id = str(uuid4())
    # Writability returns in time for the annotation, so the only CRITICALs are
    # the first loss and the closing tally (a still-dead disk adds a third —
    # see `test_a_still_dead_disk_at_close_is_logged_not_raised`).
    client = _offline_client(_RecoveringSpool())

    for _ in range(2):
        await client.handle(_envelope(session_id))
    await client.handle(
        _envelope(
            session_id,
            event_type=EventType.SESSION_CLOSE.value,
            payload={"status": "completed"},
        )
    )

    criticals = [r.getMessage() for r in caplog.records if r.levelname == "CRITICAL"]
    # One at the first loss, one at close with the tally.
    assert len(criticals) == 2
    assert "3 record(s) lost" in criticals[1]  # the SESSION_CLOSE write was lost too
    assert "INCOMPLETE" in criticals[1]


async def test_the_tally_is_appended_to_the_file_when_writability_returns() -> None:
    session_id = str(uuid4())
    spool = _RecoveringSpool()
    client = _offline_client(spool)

    await client.handle(_envelope(session_id))
    await client.handle(
        _envelope(
            session_id,
            event_type=EventType.SESSION_CLOSE.value,
            payload={"status": "completed"},
        )
    )

    assert len(spool.annotations) == 1
    record = spool.annotations[0]
    assert record["event_type"] == LOSS_RECORD_EVENT_TYPE
    assert record["lost_count"] == 2
    assert record["first_sequence"] == 1


async def test_a_still_dead_disk_at_close_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    session_id = str(uuid4())
    client = _offline_client(_DeadSpool())

    await client.handle(_envelope(session_id))
    # Must not raise — the close path is telemetry like any other.
    await client.handle(
        _envelope(
            session_id,
            event_type=EventType.SESSION_CLOSE.value,
            payload={"status": "completed"},
        )
    )

    assert any("could not append the loss record" in r.getMessage() for r in caplog.records)


async def test_a_clean_session_writes_no_tally(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    session_id = str(uuid4())
    spool = _RecoveringSpool()
    spool.handle = _accept  # type: ignore[assignment]
    client = _offline_client(spool)

    await client.handle(_envelope(session_id))
    await client.handle(
        _envelope(
            session_id,
            event_type=EventType.SESSION_CLOSE.value,
            payload={"status": "completed"},
        )
    )

    assert spool.annotations == []
    assert [r for r in caplog.records if r.levelname == "CRITICAL"] == []


async def _accept(envelope: dict[str, Any]) -> Any:
    from rootsign.ingest.schemas import IngestResponse

    return IngestResponse.accepted(event_id=uuid4(), entity_id=uuid4())


# ---------------------------------------------------------------------------
# The annotation is inert to verification
# ---------------------------------------------------------------------------


def test_a_loss_annotation_does_not_disturb_verify(tmp_path) -> None:
    """`RECORD_LOSS` is a file annotation, not an event — verify filters it out.

    It documents the gap for a human; the gap itself is what the verifier
    reports. If the annotation could change a verdict, an attacker could
    change verdicts by writing one.
    """
    from rootsign.sdk.chain import verify_session_local

    session_id = str(uuid4())
    ledger = LossLedger(session_id=session_id)
    ledger.record(sequence_number=4, reason="OSError: No space left on device")

    async def _write() -> None:
        spool = JsonlIngestClient(data_dir=tmp_path)
        for _ in range(3):
            await spool.handle(_envelope(session_id))
        spool.append_annotation(session_id, ledger.as_record())

    import asyncio

    asyncio.run(_write())

    path = Path(tmp_path) / "sessions" / f"{session_id}.jsonl"
    assert LOSS_RECORD_EVENT_TYPE in path.read_text()
    result = verify_session_local(str(path))
    assert result.valid is True, result.error
    assert result.record_count == 3


# ---------------------------------------------------------------------------
# Ledger unit behavior
# ---------------------------------------------------------------------------


def test_ledger_summary_reads_as_one_actionable_line() -> None:
    ledger = LossLedger(session_id="s")
    ledger.record(sequence_number=2, reason="OSError: disk full")
    ledger.record(sequence_number=5, reason="OSError: disk full")

    summary = ledger.summary()

    assert "2 record(s) lost" in summary
    assert "sequence 2-5" in summary
    assert "OSError: disk full (x2)" in summary


def test_ledger_handles_records_without_a_sequence() -> None:
    """Session and decision records carry no sequence number."""
    ledger = LossLedger(session_id="s")
    assert ledger.record(sequence_number=None, reason="OSError: disk full") is True
    assert ledger.is_empty is False
    assert ledger.sequence_range == "n/a"
    assert ledger.as_record()["first_sequence"] is None


def test_ledger_timestamps_bracket_the_losses() -> None:
    before = datetime.now(timezone.utc)
    ledger = LossLedger(session_id="s")
    ledger.record(sequence_number=1, reason="x")
    ledger.record(sequence_number=2, reason="x")

    assert ledger.first_at is not None and ledger.last_at is not None
    assert before <= ledger.first_at <= ledger.last_at
