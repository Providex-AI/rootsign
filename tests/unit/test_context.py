"""Unit tests for SessionContext."""

from __future__ import annotations

import asyncio
from uuid import UUID

from rootsign.sdk.context import SessionContext

AGENT_ID = UUID("550e8400-e29b-41d4-a716-446655440000")


class TestSessionContextCreation:
    def test_session_id_auto_generated(self):
        ctx = SessionContext(agent_id=AGENT_ID)
        assert isinstance(ctx.session_id, UUID)
        assert ctx.session_id.version == 4

    def test_explicit_session_id(self):
        sid = UUID("660e8400-e29b-41d4-a716-446655440001")
        ctx = SessionContext(agent_id=AGENT_ID, session_id=sid)
        assert ctx.session_id == sid

    def test_current_sequence_starts_at_zero(self):
        ctx = SessionContext(agent_id=AGENT_ID)
        assert ctx.current_sequence == 0


class TestSequenceIncrement:
    async def test_sequence_monotonic(self):
        ctx = SessionContext(agent_id=AGENT_ID)
        assert await ctx.next_sequence() == 1
        assert await ctx.next_sequence() == 2
        assert await ctx.next_sequence() == 3
        assert ctx.current_sequence == 3

    async def test_concurrent_increments_unique(self):
        """Fire 50 concurrent next_sequence() calls; the result set must
        be exactly {1..50} with no duplicates and no gaps."""
        ctx = SessionContext(agent_id=AGENT_ID)
        results = await asyncio.gather(*[ctx.next_sequence() for _ in range(50)])
        assert sorted(results) == list(range(1, 51))
        assert ctx.current_sequence == 50

    async def test_two_contexts_have_independent_counters(self):
        a = SessionContext(agent_id=AGENT_ID)
        b = SessionContext(agent_id=AGENT_ID)
        assert await a.next_sequence() == 1
        assert await a.next_sequence() == 2
        assert await b.next_sequence() == 1


class TestMarkSessionOpen:
    async def test_first_call_returns_true(self):
        ctx = SessionContext(agent_id=AGENT_ID)
        assert ctx.session_open_emitted is False
        assert await ctx.mark_session_open() is True
        assert ctx.session_open_emitted is True

    async def test_subsequent_calls_return_false(self):
        ctx = SessionContext(agent_id=AGENT_ID)
        await ctx.mark_session_open()
        assert await ctx.mark_session_open() is False
        assert await ctx.mark_session_open() is False

    async def test_concurrent_calls_only_one_true(self):
        """Fire many `mark_session_open` calls concurrently — exactly one wins."""
        ctx = SessionContext(agent_id=AGENT_ID)
        results = await asyncio.gather(*[ctx.mark_session_open() for _ in range(20)])
        assert sum(1 for r in results if r is True) == 1
        assert ctx.session_open_emitted is True
