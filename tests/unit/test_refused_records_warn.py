"""A record the store refuses is logged at WARNING, on every backend.

An exception from the client was logged at WARNING, but a refusal (an
IngestResponse with status "rejected") reached only DEBUG. An agent whose
records were all refused, for example by a cloud URL with nothing behind it or
an agent ID the store does not know, ran normally with nothing recorded and
nothing said. Each refused record now leaves one WARNING naming the record,
the error code and the store's message.
"""

from __future__ import annotations

import logging
from uuid import uuid4

import rootsign
from rootsign.ingest.schemas import ErrorCode, IngestResponse
from rootsign.sdk.client import IngestClient


class _RefusesEverything(IngestClient):
    async def handle(self, envelope):
        return IngestResponse.rejected(
            event_id=uuid4(),
            error_code=ErrorCode.UNKNOWN_AGENT,
            error_message="agent_id not registered",
            retryable=False,
        )


async def test_every_refused_record_is_a_warning(caplog):
    client = _RefusesEverything()
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
    assert len(warnings) == 3, warnings
    opened, action, closed = warnings
    assert opened.startswith(
        f"rootsign: the store refused the SESSION_OPEN for session {ctx.session_id}"
    )
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
