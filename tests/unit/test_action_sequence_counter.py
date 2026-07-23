"""audit #9: the SDK sequence counter must advance only on ACCEPTED ingests.

`total_actions` in SESSION_CLOSE is `ctx.current_sequence`. If the counter
advanced on failed/rejected ingests too, it would diverge from the store's
action_count and trip the AC-3.11 reconciliation warning spuriously. The
counter is never transmitted — the store assigns its own sequence_number —
so its only job is to tally accepted actions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from rootsign.ingest.schemas import ErrorCode, IngestResponse
from rootsign.sdk.context import SessionContext
from rootsign.sdk.decorator import _try_ingest


def _client_returning(response) -> MagicMock:
    client = MagicMock()
    client.handle = AsyncMock(return_value=response)
    return client


def _client_raising(exc: Exception) -> MagicMock:
    client = MagicMock()
    client.handle = AsyncMock(side_effect=exc)
    return client


async def _emit(client, ctx) -> object:
    return await _try_ingest(
        client=client,
        ctx=ctx,
        tool_name="do_thing",
        input_hash="a" * 64,
        output_hash=None,
        redacted_input={"x": 1},
        redacted_output=None,
        timestamp=datetime.now(timezone.utc),
    )


class TestSequenceCounterOnlyCountsAccepted:
    async def test_accepted_advances_counter(self):
        ctx = SessionContext(agent_id=uuid4())
        accepted = IngestResponse.accepted(event_id=uuid4(), entity_id=uuid4(), sequence_number=1)
        await _emit(_client_returning(accepted), ctx)
        assert ctx.current_sequence == 1
        await _emit(_client_returning(accepted), ctx)
        assert ctx.current_sequence == 2

    async def test_rejected_does_not_advance_counter(self):
        ctx = SessionContext(agent_id=uuid4())
        rejected = IngestResponse.rejected(
            event_id=uuid4(),
            error_code=ErrorCode.SESSION_CLOSED,
            error_message="session already closed",
            retryable=False,
        )
        await _emit(_client_returning(rejected), ctx)
        assert ctx.current_sequence == 0

    async def test_failed_ingest_does_not_advance_counter(self):
        """ADR-002: ingest failures are swallowed (return None). They must
        not advance the counter either."""
        ctx = SessionContext(agent_id=uuid4())
        result = await _emit(_client_raising(RuntimeError("boom")), ctx)
        assert result is None
        assert ctx.current_sequence == 0

    async def test_mixed_stream_counts_only_accepted(self):
        ctx = SessionContext(agent_id=uuid4())
        accepted = IngestResponse.accepted(event_id=uuid4(), entity_id=uuid4())
        rejected = IngestResponse.rejected(
            event_id=uuid4(),
            error_code=ErrorCode.VALIDATION_ERROR,
            error_message="bad",
            retryable=False,
        )
        await _emit(_client_returning(accepted), ctx)
        await _emit(_client_raising(RuntimeError("boom")), ctx)
        await _emit(_client_returning(rejected), ctx)
        await _emit(_client_returning(accepted), ctx)
        assert ctx.current_sequence == 2  # only the two accepted count
