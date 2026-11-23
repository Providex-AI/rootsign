"""A record the store refuses is reported, on every backend.

An exception from the client was logged at WARNING, but a refusal (an
IngestResponse with status "rejected") reached only DEBUG. An agent whose
records were all refused, for example by a cloud URL with nothing behind it or
an agent ID the store does not know, ran normally with nothing recorded and
nothing said. Each refused record now leaves one WARNING naming the record,
the error code and the store's message.

A refused SESSION_OPEN is different: it is a session that never opened, so
`session()` fails closed with RecordPersistenceError, as it does when the open
cannot be written at all (#83), instead of running a body whose every record
the store would refuse too.
"""

from __future__ import annotations

import logging
from uuid import uuid4

import pytest

import rootsign
from rootsign.errors import RecordPersistenceError
from rootsign.ingest.schemas import ErrorCode, IngestResponse
from rootsign.sdk.client import IngestClient


class _Refuses(IngestClient):
    """Refuses the given event types and accepts the rest."""

    def __init__(self, *refused: str) -> None:
        self.refused = set(refused)

    async def handle(self, envelope):
        if envelope["event_type"] in self.refused:
            return IngestResponse.rejected(
                event_id=uuid4(),
                error_code=ErrorCode.UNKNOWN_AGENT,
                error_message="agent_id not registered",
                retryable=False,
            )
        return IngestResponse.accepted(event_id=uuid4(), entity_id=uuid4())


async def test_a_refused_session_open_fails_closed():
    client = _Refuses("SESSION_OPEN", "ACTION_RECORD", "SESSION_CLOSE")
    ran = []

    with pytest.raises(RecordPersistenceError) as raised:
        async with rootsign.session(agent_id=uuid4(), client=client):
            ran.append("body")

    assert ran == []
    assert str(raised.value) == (
        "session could not be opened (the store refused SESSION_OPEN: UNKNOWN_AGENT: "
        "agent_id not registered). Nothing was recorded, and the session body did not run."
    )


async def test_an_open_that_was_not_delivered_lets_the_agent_run(caplog):
    """The cloud transport reports a record it could neither send nor spool with
    a transient code. That is ROOTSIGN_ON_RECORD_LOSS's case, not a refusal:
    under the default, warn, the agent keeps running."""

    class _LosesOpen(IngestClient):
        async def handle(self, envelope):
            if envelope["event_type"] == "SESSION_OPEN":
                return IngestResponse.rejected(
                    event_id=uuid4(),
                    error_code=ErrorCode.INTERNAL_ERROR,
                    error_message="record dropped: spool write failed (PermissionError)",
                    retryable=False,
                )
            return IngestResponse.accepted(event_id=uuid4(), entity_id=uuid4())

    ran = []
    with caplog.at_level(logging.WARNING, logger="rootsign"):
        async with rootsign.session(agent_id=uuid4(), client=_LosesOpen()):
            ran.append("body")

    assert ran == ["body"]
    (warning,) = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warning.startswith("rootsign: the store refused the SESSION_OPEN for session ")


async def test_every_other_refused_record_is_a_warning(caplog):
    client = _Refuses("ACTION_RECORD", "SESSION_CLOSE")
    ran = []

    with caplog.at_level(logging.WARNING, logger="rootsign"):
        async with rootsign.session(agent_id=uuid4(), client=client) as ctx:

            @rootsign.trace(ingest_client=client, session_context=ctx)
            async def send_invoice(amount: float) -> str:
                ran.append(amount)
                return "sent"

            assert await send_invoice(10.0) == "sent"  # the agent keeps running

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert ran == [10.0]
    assert len(warnings) == 2, warnings
    action, closed = warnings
    assert action.startswith(
        "rootsign: the store refused the ACTION_RECORD for tool 'send_invoice'"
    )
    assert closed.startswith(
        f"rootsign: the store refused the SESSION_CLOSE for session {ctx.session_id}"
    )
    for message in warnings:
        assert "(UNKNOWN_AGENT: agent_id not registered); it was not recorded" in message


async def test_an_accepted_record_logs_nothing(caplog, tmp_path):
    client = rootsign.JsonlIngestClient(data_dir=tmp_path)

    with caplog.at_level(logging.WARNING, logger="rootsign"):
        async with rootsign.session(agent_id=uuid4(), client=client) as ctx:

            @rootsign.trace(ingest_client=client, session_context=ctx)
            async def send_invoice(amount: float) -> str:
                return "sent"

            await send_invoice(10.0)

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
