"""Dead spool: the bottom rung of the ladder (Sprint B T2.8, ADR-013 D4a).

The wire is down *and* the disk will not take the record. There is nowhere
left to put it, so the design stops being about durability and starts being
about honesty: telemetry drops with accounting and the agent keeps running,
controls fail closed and the gated tool never executes, and the chain keeps
advancing so the loss is provable afterwards rather than silent.

`tests/unit/test_record_loss_ladder.py` proves the ladder against a fake spool
whose `handle()` raises. This file uses a **real read-only directory**, the
real writer, and the real decorator, which is what closes the gap between "the
handler catches OSError" and "the failure a full disk actually produces is the
one it catches" — a `mkdir` on an unwritable parent raises `PermissionError`
from a different line than the `os.open` the fake stands in for.

The last test is the payoff: once writability returns, the session file verifies
**INCOMPLETE** and names the exact sequence range that never made it. Nothing
reconstructs that range from a log — it falls out of the chain, because
`ChainState` kept counting while the disk was dead.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

import rootsign
from rootsign.errors import HiTLPersistenceError, RecordPersistenceError
from rootsign.sdk.chain import verify_session_local
from rootsign.sdk.client import HttpIngestClient
from rootsign.sdk.loss_ledger import LOSS_RECORD_EVENT_TYPE
from rootsign.verdict import Verdict

BASE_URL = "https://ingest.example.test/v1"
API_KEY = "sk-dead-spool"

READ_ONLY = 0o500
WRITABLE = 0o700


def _dead_endpoint(request: httpx.Request) -> httpx.Response:
    return httpx.Response(503, text="down")


@pytest.fixture
def dead_spool(tmp_path: Path) -> Iterator[Path]:
    """A spool root that cannot be written to.

    Skips rather than lying when the process can write anyway — running as
    root (some containers, some CI images) makes the mode bits advisory, and a
    test that silently exercised the happy path would be worse than no test.
    """
    root = tmp_path / "spool"
    root.mkdir()
    os.chmod(root, READ_ONLY)
    try:
        (root / "probe").mkdir()
    except OSError:
        pass  # good: the directory really is read-only
    else:
        os.chmod(root, WRITABLE)
        pytest.skip("filesystem permissions are not enforced for this user (running as root?)")

    yield root

    # Teardown must be able to remove it again.
    os.chmod(root, WRITABLE)


def _offline_client(spool: Path, **kwargs) -> HttpIngestClient:
    """Endpoint down, so every record routes to the (dead) spool."""
    return HttpIngestClient(
        BASE_URL,
        API_KEY,
        max_retries=1,
        transport=httpx.MockTransport(_dead_endpoint),
        spool_dir=str(spool),
        **kwargs,
    )


class TestTelemetryDropsWithAccounting:
    async def test_the_agent_keeps_running_with_nowhere_to_write(self, dead_spool):
        """ADR-002's isolation rule still binds at the bottom rung.

        This is the case where it is most tempting to break: nothing can be
        recorded, so the instinct is to shout. But a logging layer that halts
        the business it observes has inverted its own value — the invoice still
        needs sending.
        """
        client = _offline_client(dead_spool)
        calls: list[int] = []

        async with rootsign.session(agent_id=uuid4(), client=client, objective="dead disk") as ctx:

            @rootsign.trace(ingest_client=client, session_context=ctx)
            async def send_invoice(n: int) -> str:
                calls.append(n)
                return f"sent {n}"

            results = [await send_invoice(i) for i in range(3)]
            session_id = str(ctx.session_id)

        await client.close()

        assert results == ["sent 0", "sent 1", "sent 2"]
        assert calls == [0, 1, 2]
        assert not (dead_spool / "sessions").exists(), "something got written after all"
        assert client.loss_ledger(session_id) is not None

    async def test_one_critical_for_the_outage_and_one_for_the_tally(
        self, dead_spool, caplog: pytest.LogCaptureFixture
    ):
        """One per *event*, never one per lost record.

        Four records are lost and the log gets three lines: the disk is gone,
        the session closed having lost N, and the tally could not be written to
        the file either. Repeating the first per record would bury the two that
        follow it under thousands of identical lines.
        """
        client = _offline_client(dead_spool)

        with caplog.at_level(logging.CRITICAL, logger="rootsign.sdk.client"):
            async with rootsign.session(
                agent_id=uuid4(), client=client, objective="one critical"
            ) as ctx:

                @rootsign.trace(ingest_client=client, session_context=ctx)
                async def send_invoice(n: int) -> str:
                    return f"sent {n}"

                for i in range(4):
                    await send_invoice(i)

                during_session = [r for r in caplog.records if r.levelno == logging.CRITICAL]
                assert len(during_session) == 1, [r.message for r in during_session]
                assert "RECORD LOST" in during_session[0].getMessage()

            await client.close()

        criticals = [r.getMessage() for r in caplog.records if r.levelno == logging.CRITICAL]
        assert len(criticals) == 3, criticals
        # The third only appears when the disk is *still* dead at close: the
        # tally could not be appended to the session file either, so the log is
        # the only place it survives. Worth its own line — it tells the operator
        # the file on disk understates the loss.
        assert "closed with lost records" in criticals[1]
        assert "could not append the loss record" in criticals[2]

    async def test_the_ledger_carries_the_count_the_range_and_the_reason(self, dead_spool):
        """What the operator needs to reason about the hole: how many, where,
        and why. The range comes from the chain, so it survives even though
        nothing about those records reached disk."""
        client = _offline_client(dead_spool)

        async with rootsign.session(agent_id=uuid4(), client=client, objective="ledger") as ctx:

            @rootsign.trace(ingest_client=client, session_context=ctx)
            async def send_invoice(n: int) -> str:
                return f"sent {n}"

            for i in range(3):
                await send_invoice(i)
            session_id = str(ctx.session_id)

        await client.close()
        ledger = client.loss_ledger(session_id)

        # SESSION_OPEN and SESSION_CLOSE are telemetry too, and they are lost
        # as well — but only actions carry a sequence number, so the *range* is
        # the actions' and the *count* is everything.
        assert ledger.count == 5
        assert ledger.first_sequence == 1
        assert ledger.last_sequence == 3
        # One cause, counted — not one string per record.
        assert len(ledger.reasons) == 1
        cause = next(iter(ledger.reasons))
        assert "PermissionError" in cause and "denied" in cause.lower()
        assert ledger.reasons[cause] == 5
        assert ledger.sequence_range == "1-3"


class TestControlsFailClosed:
    async def test_a_gated_tool_never_runs_when_its_control_cannot_be_persisted(self, dead_spool):
        """The asymmetry that makes the ladder defensible.

        An approval is not a record *of* the action — it is the control *on*
        it. If the pending ACTION_RECORD cannot be written, there is nothing
        for a human to approve against, so the only safe answer is to refuse
        to run the tool. Telemetry drops; controls stop the world.
        """
        client = _offline_client(dead_spool)
        executed: list[str] = []

        async with rootsign.session(agent_id=uuid4(), client=client, objective="hitl") as ctx:

            @rootsign.trace(ingest_client=client, session_context=ctx, require_approval=True)
            async def wire_transfer(amount: int) -> str:
                executed.append("ran")
                return f"wired {amount}"

            with pytest.raises(HiTLPersistenceError) as excinfo:
                await wire_transfer(50_000)

        await client.close()

        assert executed == [], "the gated tool ran without a persisted control record"
        assert "wire_transfer" in str(excinfo.value)
        assert "was not executed" in str(excinfo.value)
        # Nothing to account for: the caller is learning the action did not
        # happen, so there is no lost record to tally. The ledger holds only the
        # session envelopes (OPEN, and the CLOSE the raise triggers) — neither
        # of which carries a sequence number.
        ledger = client.loss_ledger(str(ctx.session_id))
        assert ledger.count == 2
        assert ledger.first_sequence is None, "the control record was ledgered as a loss"


class TestOnRecordLossFail:
    async def test_fail_mode_refuses_to_open_a_session_it_cannot_record(
        self, dead_spool, monkeypatch
    ):
        """The opt-in for regulated deployments, at the earliest point it bites.

        `SESSION_OPEN` is telemetry like anything else, so in `fail` mode a
        session whose opening record cannot be persisted never opens — and no
        decorated tool inside it ever runs. That is a stronger guarantee than
        the per-action one below, and it comes free: the ladder does not
        special-case the session envelope.
        """
        monkeypatch.setenv("ROOTSIGN_ON_RECORD_LOSS", "fail")
        client = _offline_client(dead_spool)
        executed: list[int] = []

        with pytest.raises(RecordPersistenceError) as excinfo:
            async with rootsign.session(
                agent_id=uuid4(), client=client, objective="fail mode"
            ) as ctx:

                @rootsign.trace(ingest_client=client, session_context=ctx)
                async def send_invoice(n: int) -> str:
                    executed.append(n)
                    return f"sent {n}"

                await send_invoice(1)

        await client.close()

        assert "ON_RECORD_LOSS=fail" in str(excinfo.value)
        assert executed == [], "the session opened despite having nowhere to record it"

    async def test_fail_mode_on_an_action_raises_after_the_tool_has_run(
        self, dead_spool, monkeypatch
    ):
        """What `fail` mode does *not* buy, stated so nobody mistakes it for a gate.

        The disk dies mid-session, after the session opened cleanly. The tool
        body has already executed by the time its record is written, so the
        raise lands after the side effect — it stops the *next* call and makes
        the loss impossible to ignore, but it does not un-send the invoice.
        Genuine before-the-fact gating is `require_approval=True`.
        """
        monkeypatch.setenv("ROOTSIGN_ON_RECORD_LOSS", "fail")
        os.chmod(dead_spool, WRITABLE)  # the session opens while there is room
        client = _offline_client(dead_spool)
        executed: list[int] = []

        with pytest.raises(RecordPersistenceError):
            async with rootsign.session(
                agent_id=uuid4(), client=client, objective="disk fills mid-session"
            ) as ctx:

                @rootsign.trace(ingest_client=client, session_context=ctx)
                async def send_invoice(n: int) -> str:
                    executed.append(n)
                    return f"sent {n}"

                await send_invoice(1)
                # The session file itself becomes unwritable. Note it has to be
                # the *file*: appending to an existing file needs write
                # permission on the file, not on the directory holding it, so
                # locking the spool root would not have stopped this write.
                os.chmod(dead_spool / "sessions" / f"{ctx.session_id}.jsonl", 0o400)
                await send_invoice(2)

        await client.close()

        assert executed == [1, 2], "the tool did not run — this is not a pre-execution gate"

    async def test_warn_is_the_default(self, dead_spool):
        client = _offline_client(dead_spool)
        assert client.on_record_loss == "warn"


class TestRecovery:
    async def test_when_the_disk_comes_back_the_gap_is_provable(self, dead_spool):
        """The claim the whole ladder exists to make.

        Two records are lost, the disk returns, two more land. Nobody wrote
        down "records 1 and 2 are missing" — the chain did, by continuing to
        count. `verify` reads the file that exists and reports INCOMPLETE with
        the exact range, which is the difference between a log with a hole in
        it and a log that can prove it has one.
        """
        client = _offline_client(dead_spool)
        session_id: str

        async with rootsign.session(agent_id=uuid4(), client=client, objective="recovery") as ctx:

            @rootsign.trace(ingest_client=client, session_context=ctx)
            async def send_invoice(n: int) -> str:
                return f"sent {n}"

            await send_invoice(0)  # sequence 1 — lost
            await send_invoice(1)  # sequence 2 — lost

            os.chmod(dead_spool, WRITABLE)  # the operator frees up the disk

            await send_invoice(2)  # sequence 3 — lands
            await send_invoice(3)  # sequence 4 — lands
            session_id = str(ctx.session_id)

        await client.close()

        path = dead_spool / "sessions" / f"{session_id}.jsonl"
        result = verify_session_local(str(path))

        assert result.verdict is Verdict.INCOMPLETE, result.summary
        assert result.missing_ranges == [(1, 2)]
        assert result.record_count == 2
        assert "1-2" in result.summary

    async def test_the_closing_tally_lands_in_the_file_and_verify_ignores_it(self, dead_spool):
        """The ledger's epitaph is written where the records would have been.

        It is not an ingest record and both verifiers filter to ACTION_RECORD,
        so it documents the loss for a human without perturbing the verdict.
        """
        client = _offline_client(dead_spool)

        async with rootsign.session(agent_id=uuid4(), client=client, objective="tally") as ctx:

            @rootsign.trace(ingest_client=client, session_context=ctx)
            async def send_invoice(n: int) -> str:
                return f"sent {n}"

            await send_invoice(0)
            os.chmod(dead_spool, WRITABLE)
            await send_invoice(1)
            session_id = str(ctx.session_id)

        await client.close()

        import json

        path = dead_spool / "sessions" / f"{session_id}.jsonl"
        records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        tallies = [r for r in records if r.get("event_type") == LOSS_RECORD_EVENT_TYPE]

        assert len(tallies) == 1
        assert tallies[0]["lost_count"] >= 1
        assert tallies[0]["first_sequence"] == 1
        # Inert to the verifier: both verifiers rebuild the chain from
        # ACTION_RECORD lines, so the epitaph documents the loss without
        # perturbing the verdict the chain itself produces.
        assert verify_session_local(str(path)).verdict is Verdict.INCOMPLETE
