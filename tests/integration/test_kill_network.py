"""Kill-network: the offline story end to end (Sprint B T2.7, ADR-013 D4).

Every other cloud test drives envelopes. This one drives the **SDK surface a
user actually writes** — `rootsign.session(...)` and `@rootsign.trace` — and
severs the network in the middle of a live session, which is the only way to
test the claim the sprint is selling:

1. the agent never notices (ADR-002: ingest failures do not reach the tool);
2. records recorded during the outage are still tamper-evident on disk, and
   `rootsign verify --local` says so *while still offline*;
3. `rootsign-admin sync` uploads them when the network returns;
4. and the store then holds **one chain across the outage** — the records that
   went over the wire before it and the ones replayed days later verify as a
   single unbroken session.

(4) is the load-bearing one, and it is why this file asserts against a store
that verifies seals rather than a stub. Turning off seal adoption
(`ChainState.seal`) fails five of these tests: the spooled half would start a
second chain from sequence 1, and the store's copy would verify TAMPERED
instead of VALID.

One expectation from the sprint plan turned out to be wrong, and the test that
covers it says why: a *mid-session* spool file verifies INCOMPLETE, not VALID,
because the records from before the outage are not in it.

No database: cloud transport plus the JSONL spool, so nothing here needs
Postgres even though it lives with the integration suite.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

import rootsign
from rootsign.cli import app as admin_app
from rootsign.ingest.schemas import EventType
from rootsign.sdk.chain import verify_session_local
from rootsign.sdk.cli import app as user_app
from rootsign.sdk.client import HttpIngestClient
from rootsign.sdk.spool import SYNC_BREADCRUMB
from rootsign.verdict import Verdict
from tests.support.mock_ingest_store import MockIngestStore

runner = CliRunner()

BASE_URL = "https://ingest.example.test/v1"
API_KEY = "sk-kill-network"

ONLINE_CALLS = 3
OFFLINE_CALLS = 3


@pytest.fixture
def store() -> MockIngestStore:
    return MockIngestStore()


@pytest.fixture
def spool(tmp_path: Path) -> Path:
    return tmp_path / "spool"


async def _run_session_with_an_outage(store: MockIngestStore, spool: Path) -> tuple[str, list[str]]:
    """Run one session, severing the network partway. Returns (session_id, results).

    `max_retries=1` so the outage costs one attempt rather than a backoff
    schedule — this test is about what happens *after* the transport gives up.
    """
    client = HttpIngestClient(
        BASE_URL,
        API_KEY,
        max_retries=1,
        transport=store.transport(),
        spool_dir=str(spool),
    )
    agent_id = uuid4()
    results: list[str] = []

    async with rootsign.session(agent_id=agent_id, client=client, objective="kill-network") as ctx:
        # Defined inside the session so the explicit form can bind the real
        # context; a module-level decorated tool would have nothing to bind to.
        @rootsign.trace(ingest_client=client, session_context=ctx)
        async def charge_card(amount: int) -> str:
            return f"charged {amount}"

        for i in range(ONLINE_CALLS):
            results.append(await charge_card(i))

        store.sever()  # the network dies mid-session

        for i in range(ONLINE_CALLS, ONLINE_CALLS + OFFLINE_CALLS):
            results.append(await charge_card(i))

        session_id = str(ctx.session_id)

    await client.close()
    return session_id, results


class TestKillNetwork:
    async def test_the_agent_never_notices_the_outage(self, store, spool):
        """ADR-002, under the one condition that actually tests it.

        A logging layer that raises when the network dies is worse than no
        logging layer: it turns an observability outage into an application
        outage. Every call must return its real result, including the ones
        whose records never left the machine.
        """
        _, results = await _run_session_with_an_outage(store, spool)

        assert results == [f"charged {i}" for i in range(ONLINE_CALLS + OFFLINE_CALLS)]

    async def test_the_records_split_between_the_wire_and_the_spool(self, store, spool):
        session_id, _ = await _run_session_with_an_outage(store, spool)

        uploaded = store.actions_for(session_id)
        assert [p["sequence_number"] for p in uploaded] == [1, 2, 3]

        spooled = _spooled_actions(spool, session_id)
        assert [r["sequence_number"] for r in spooled] == [4, 5, 6]
        # One chain, not two. The mechanism is seal-then-route: the transport
        # seals every action against its own `ChainState` *before* choosing the
        # wire or the spool, and the JSONL writer **adopts** a payload that
        # already carries a seal instead of minting a new one. Break the
        # adoption and record 4 starts a second chain from scratch.
        assert spooled[0]["prev_action_hash"] == uploaded[-1]["self_hash"]

    async def test_a_mid_session_spool_verifies_incomplete_not_valid(self, store, spool):
        """A partial spool file reports INCOMPLETE — and that is the right answer.

        The sprint plan expected VALID here, which holds only when a session
        spools from its first record. After a *mid-session* outage the file
        contains sequences 4-6; 1-3 went over the wire and are not in it. The
        verifier is reading one file, not the union of the file and a server it
        cannot reach, so calling that VALID would mean "this file is the whole
        session" — a claim nobody can make offline, and the exact claim an
        auditor must not be handed.

        INCOMPLETE is the honest verdict, and the CLI's own remedy line for it
        already names this case: "check for a spooled session that was never
        synced". The evidence is intact (nothing is TAMPERED) and the gap is
        provable from the chain itself.
        """
        session_id, _ = await _run_session_with_an_outage(store, spool)
        path = spool / "sessions" / f"{session_id}.jsonl"

        result = await asyncio.to_thread(runner.invoke, user_app, ["verify", "--local", str(path)])

        assert result.exit_code == 2, result.output
        assert "INCOMPLETE" in result.output
        assert "missing at sequence 1-3" in result.output
        # The records that ARE here verify cleanly — nothing was altered.
        assert verify_session_local(str(path)).missing_ranges == [(1, 3)]
        # And it tells the operator what to do about it (ADR-013 D4).
        assert SYNC_BREADCRUMB in result.output

    async def test_a_session_that_was_offline_from_the_start_verifies_valid(self, store, spool):
        """The other half: when the endpoint is dead before SESSION_OPEN, the
        file holds the whole session and VALID is available offline — the
        air-gapped case the JSONL backend was built for."""
        store.sever()
        client = HttpIngestClient(
            BASE_URL, API_KEY, max_retries=1, transport=store.transport(), spool_dir=str(spool)
        )
        async with rootsign.session(
            agent_id=uuid4(), client=client, objective="offline from the start"
        ) as ctx:

            @rootsign.trace(ingest_client=client, session_context=ctx)
            async def charge_card(amount: int) -> str:
                return f"charged {amount}"

            for i in range(3):
                await charge_card(i)
            session_id = str(ctx.session_id)
        await client.close()

        path = spool / "sessions" / f"{session_id}.jsonl"
        result = await asyncio.to_thread(runner.invoke, user_app, ["verify", "--local", str(path)])

        assert result.exit_code == 0, result.output
        assert "VALID" in result.output
        assert store.accepted == [], "something reached a severed endpoint"

    async def test_sync_after_the_network_returns_leaves_one_chain_in_the_store(
        self, store, spool, monkeypatch
    ):
        """The whole promise, asserted at the far end.

        The store ends up holding six actions it received in two batches days
        apart, and its copy verifies VALID under the auditor's own verifier.
        Any re-minting anywhere — spool writer, replay, or a second chain
        registry — and the halves would not link.
        """
        session_id, _ = await _run_session_with_an_outage(store, spool)
        store.restore()
        monkeypatch.setenv("ROOTSIGN_API_KEY", API_KEY)
        _bind_transport(monkeypatch, store.transport())

        result = await asyncio.to_thread(
            runner.invoke, admin_app, ["sync", "--spool-dir", str(spool)]
        )

        assert result.exit_code == 0, result.output
        assert [p["sequence_number"] for p in store.actions_for(session_id)] == [1, 2, 3, 4, 5, 6]

        dump = store.dump_chain(session_id, spool / "store-copy.jsonl")
        verified = verify_session_local(str(dump))
        assert verified.verdict is Verdict.VALID, verified.summary
        assert verified.record_count == 6

    async def test_the_session_close_that_was_spooled_also_lands(self, store, spool, monkeypatch):
        """A session whose CLOSE never reached the store would read as still
        running forever. It spools like anything else, and syncs like anything
        else."""
        session_id, _ = await _run_session_with_an_outage(store, spool)
        store.restore()
        monkeypatch.setenv("ROOTSIGN_API_KEY", API_KEY)
        _bind_transport(monkeypatch, store.transport())

        await asyncio.to_thread(runner.invoke, admin_app, ["sync", "--spool-dir", str(spool)])

        arrived = [e["event_type"] for e in store.accepted if str(e["session_id"]) == session_id]
        assert arrived.count(EventType.SESSION_OPEN.value) == 1
        assert arrived.count(EventType.SESSION_CLOSE.value) == 1
        assert arrived.count(EventType.ACTION_RECORD.value) == 6

    async def test_the_spool_file_is_retired_and_a_second_sync_is_a_no_op(
        self, store, spool, monkeypatch
    ):
        session_id, _ = await _run_session_with_an_outage(store, spool)
        store.restore()
        monkeypatch.setenv("ROOTSIGN_API_KEY", API_KEY)
        _bind_transport(monkeypatch, store.transport())

        await asyncio.to_thread(runner.invoke, admin_app, ["sync", "--spool-dir", str(spool)])
        accepted_after_first = len(store.accepted)

        second = await asyncio.to_thread(
            runner.invoke, admin_app, ["sync", "--spool-dir", str(spool)]
        )

        assert second.exit_code == 0, second.output
        assert "Nothing to sync" in second.output
        assert not (spool / "sessions" / f"{session_id}.jsonl").exists()
        assert (spool / "synced" / f"{session_id}.jsonl").exists()
        assert len(store.accepted) == accepted_after_first

    async def test_a_tampered_spool_file_is_caught_before_it_is_ever_uploaded(self, store, spool):
        """The offline window is exactly when nobody is watching the file.

        Someone with disk access could rewrite a spooled record while the
        network is down. `verify --local` is what makes that detectable
        without a server, and the seal is what makes it detectable at all —
        the record's own `self_hash` no longer matches its fields.
        """
        session_id, _ = await _run_session_with_an_outage(store, spool)
        path = spool / "sessions" / f"{session_id}.jsonl"
        path.write_text(path.read_text().replace("charge_card", "refund_card", 1))

        result = await asyncio.to_thread(runner.invoke, user_app, ["verify", "--local", str(path)])

        assert result.exit_code == 1, result.output
        assert "TAMPERED" in result.output


def _spooled_actions(spool: Path, session_id: str) -> list[dict]:
    import json

    path = spool / "sessions" / f"{session_id}.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in records if r.get("event_type") == EventType.ACTION_RECORD.value]


def _bind_transport(monkeypatch, transport) -> None:
    """Point the CLI's own `HttpIngestClient` at the restored network."""
    real = HttpIngestClient

    def factory(**kwargs):
        return real(transport=transport, **kwargs)

    monkeypatch.setattr("rootsign.sdk.client.HttpIngestClient", factory)
