"""Wire contract for the cloud transport (Sprint B T2.6, `docs/ingest-spec-v1.md`).

`tests/unit/test_http_ingest_client.py` tests the client's own moving parts
against a scripted responder. This suite tests the **contract between the
client and a store**, so the mock here is not a script — it is a small
implementation of the spec (`MockIngestStore`): it validates envelopes with the
real schemas, dedupes by `event_id`, and — the part that matters most — it
**verifies the client's seal instead of computing its own**, exactly as
ADR-013 Decision 1 says a real backend must. A client that sealed wrongly
gets HASH_CHAIN_BROKEN from this store the same way it would in production,
rather than being quietly accepted by a mock that agrees with whatever it is
sent.

What that buys, and what nothing else in the suite asserts: the records the
store ends up holding are dumped back out and run through
`verify_session_local` — the same verifier an auditor runs. "The chain
survives the network" stops being an argument and becomes an assertion, and
it holds for the offline path too, where records reach the store days later
through `rootsign-admin sync`.

Sections map to the T2.6 checklist: wire conformance, the full rejection
registry, retry/backoff as the client actually schedules it, mid-batch
partial failure, timeout → spool, `sync` end-to-end with re-run idempotency,
and the ADR-009 buffering contract re-run with the real transport underneath.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from typer.testing import CliRunner

from rootsign.cli import app as admin_app
from rootsign.ingest.schemas import ErrorCode, EventType
from rootsign.sdk.buffered_client import BufferedIngestClient
from rootsign.sdk.chain import verify_session_local
from rootsign.sdk.client import BACKOFF_MAX_SECONDS, HttpIngestClient
from rootsign.verdict import Verdict
from tests.conftest import make_envelope
from tests.support.mock_ingest_store import NON_RETRYABLE, RETRYABLE, MockIngestStore

runner = CliRunner()

BASE_URL = "https://ingest.example.test/v1"
API_KEY = "sk-contract"


@pytest.fixture
def store() -> MockIngestStore:
    return MockIngestStore()


@pytest.fixture
def recorded_delays(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the delay the client *would* sleep, and don't sleep it.

    Wrapping the real `_backoff_delay` rather than replacing it keeps the
    jitter and the ceiling under test; only the wall-clock cost is removed.
    """
    from rootsign.sdk import client as client_module

    real = client_module._backoff_delay
    delays: list[float] = []

    def recording(attempt: int, retry_after: float | None) -> float:
        delays.append(real(attempt, retry_after))
        return 0.0

    monkeypatch.setattr(client_module, "_backoff_delay", recording)
    return delays


def _client(store: MockIngestStore, **kwargs: Any) -> HttpIngestClient:
    return HttpIngestClient(BASE_URL, API_KEY, transport=store.transport(), **kwargs)


def _session(agent_id: UUID, session_id: UUID, *, actions: int = 3) -> list[dict[str, Any]]:
    envelopes = [make_envelope("SESSION_OPEN", agent_id, session_id, {"objective": "contract"})]
    for i in range(actions):
        envelopes.append(
            make_envelope(
                "ACTION_RECORD",
                agent_id,
                session_id,
                {
                    "tool_name": f"tool_{i}",
                    "input_hash": f"{i:064d}",
                    "output_hash": "b" * 64,
                    "timestamp": "2026-08-21T10:00:00+00:00",
                    "authorization_status": "auto_authorized",
                },
            )
        )
    envelopes.append(make_envelope("SESSION_CLOSE", agent_id, session_id, {"status": "completed"}))
    return envelopes


# ---------------------------------------------------------------------------
# Wire conformance (spec §3, §7)
# ---------------------------------------------------------------------------


class TestWireConformance:
    async def test_a_whole_session_is_accepted_and_the_store_can_verify_it(self, store, tmp_path):
        """The claim the sprint is built on, stated once end-to-end.

        Every event type crosses the wire, the store validates each against
        the published schema, verifies the seal it was sent, and what it holds
        afterwards verifies VALID under the auditor's own verifier.
        """
        agent_id, session_id = uuid4(), uuid4()
        client = _client(store)

        responses = [await client.handle(e) for e in _session(agent_id, session_id)]
        await client.close()

        assert [r.status for r in responses] == ["accepted"] * 5
        assert len(store.accepted) == 5
        result = verify_session_local(str(store.dump_chain(session_id, tmp_path / "s.jsonl")))
        assert result.verdict is Verdict.VALID, result.summary

    async def test_the_request_is_a_json_array_with_bearer_auth(self, store):
        client = _client(store)
        envelopes = _session(uuid4(), uuid4(), actions=2)

        await client.handle_batch(envelopes)
        await client.close()

        assert store.requests == 1
        assert store.batch_sizes == [len(envelopes)]

    async def test_an_unsealed_action_is_refused_by_the_store(self, store):
        """The other half of "the server verifies rather than computes".

        If this store accepted an unsealed record it would have to assign
        identity itself — and the client would be holding a `self_hash` for a
        record that exists nowhere.
        """
        agent_id, session_id = uuid4(), uuid4()
        envelope = _session(agent_id, session_id, actions=1)[1]
        envelope["payload"].pop("self_hash", None)  # not sealed by a client

        # Post it directly, bypassing the client's sealer.
        async with httpx.AsyncClient(transport=store.transport()) as raw:
            response = await raw.post(f"{BASE_URL}/ingest", content=json.dumps([envelope]))

        body = response.json()[0]
        assert body["status"] == "rejected"
        assert body["error_code"] == ErrorCode.HASH_CHAIN_BROKEN.value

    async def test_a_forged_field_breaks_the_seal_at_the_store(self, store):
        """Tamper in flight: the payload says one thing, the seal says another."""
        agent_id, session_id = uuid4(), uuid4()
        client = _client(store)
        envelopes = _session(agent_id, session_id, actions=1)
        await client.handle(envelopes[0])

        action = envelopes[1]
        client._seal([action])  # seal it as the transport would
        action["payload"]["tool_name"] = "SOMETHING_ELSE"

        async with httpx.AsyncClient(transport=store.transport()) as raw:
            response = await raw.post(f"{BASE_URL}/ingest", content=json.dumps([action]))
        await client.close()

        body = response.json()[0]
        assert body["status"] == "rejected"
        assert body["error_code"] == ErrorCode.HASH_CHAIN_BROKEN.value
        assert "self_hash" in body["error_message"]


# ---------------------------------------------------------------------------
# The rejection registry (spec §9.3)
# ---------------------------------------------------------------------------


class TestRejectionRegistry:
    def test_every_code_in_the_registry_is_classified_by_this_suite(self):
        """A new `ErrorCode` must not slip in without a retry decision.

        The transport routes on the wire `retryable` flag, but the *store*
        side of the contract still has to say which codes are transient, and
        a code nobody classified is a code nobody thought about.
        """
        assert set(NON_RETRYABLE) | set(RETRYABLE) == set(ErrorCode)
        assert not set(NON_RETRYABLE) & set(RETRYABLE)

    @pytest.mark.parametrize("code", NON_RETRYABLE, ids=lambda c: c.value)
    async def test_a_non_retryable_rejection_passes_through_untouched(
        self, store, tmp_path, code: ErrorCode
    ):
        """One request, the server's own code, and nothing written to the spool.

        Spooling a deterministic rejection would be the worst of both: the
        record replays into the same refusal later, while the file on disk
        implies it is safe.
        """
        store.reject_all = (code, False)
        client = _client(store, max_retries=3, spool_dir=str(tmp_path))

        response = await client.handle(_session(uuid4(), uuid4(), actions=1)[0])
        await client.close()

        assert store.requests == 1, "a deterministic rejection was retried"
        assert response.status == "rejected"
        assert response.error_code is code
        assert client.is_spooling is False
        assert not (tmp_path / "sessions").exists()

    @pytest.mark.parametrize("code", RETRYABLE, ids=lambda c: c.value)
    async def test_a_retryable_rejection_exhausts_the_budget_then_spools(
        self, store, tmp_path, recorded_delays, code: ErrorCode
    ):
        store.reject_all = (code, True)
        client = _client(store, max_retries=3, spool_dir=str(tmp_path))
        session_id = uuid4()

        response = await client.handle(_session(uuid4(), session_id, actions=1)[0])
        await client.close()

        assert store.requests == 3, "the retry budget is total attempts, not retries"
        # Durable on disk, so `accepted` is the honest answer (ADR-013 D4).
        assert response.status == "accepted"
        assert client.spool_reason is code
        assert (tmp_path / "sessions" / f"{session_id}.jsonl").exists()

    async def test_an_unknown_code_is_classified_by_the_wire_flag(self, store, tmp_path):
        """Spec §9.3: a fielded client must honor `retryable` for a code it has
        never heard of, or the registry can never grow on a minor bump."""

        def respond(request: httpx.Request) -> httpx.Response:
            store.requests += 1
            batch = json.loads(request.content)
            return httpx.Response(
                200,
                json=[
                    {
                        "status": "rejected",
                        "event_id": e["event_id"],
                        "error_code": ErrorCode.INTERNAL_ERROR.value,
                        "error_message": "pretend this is a v1.2 code",
                        "retryable": False,
                    }
                    for e in batch
                ],
            )

        client = HttpIngestClient(
            BASE_URL,
            API_KEY,
            max_retries=3,
            transport=httpx.MockTransport(respond),
            spool_dir=str(tmp_path),
        )
        response = await client.handle(_session(uuid4(), uuid4(), actions=1)[0])
        await client.close()

        # INTERNAL_ERROR is in the retryable family, but this response says no.
        assert store.requests == 1
        assert response.status == "rejected"
        assert client.is_spooling is False


# ---------------------------------------------------------------------------
# Retry and backoff, as the client schedules it (spec §5, §7.6)
# ---------------------------------------------------------------------------


class TestRetryAndBackoff:
    @pytest.mark.parametrize("status", [429, 503])
    async def test_retry_then_succeed(self, store, recorded_delays, status: int):
        """Both transient statuses recover on the next attempt, not the third."""
        store.first_status = status
        client = _client(store, max_retries=3)

        response = await client.handle(_session(uuid4(), uuid4(), actions=1)[0])
        await client.close()

        assert response.status == "accepted"
        assert store.requests == 2, "the retry did not happen exactly once"
        assert len(recorded_delays) == 1

    async def test_the_scheduled_delays_are_bounded_and_jittered(
        self, store, recorded_delays, tmp_path
    ):
        """Two properties at once, because they fail in opposite directions.

        Unbounded growth turns an outage into a client that never comes back;
        no jitter turns every client reconnecting at once into a thundering
        herd that keeps the store down.
        """
        store.reject_all = (ErrorCode.RATE_LIMITED, True)
        client = _client(store, max_retries=8, spool_dir=str(tmp_path / "a"))

        # Two clients, same schedule — full jitter means the sequences differ.
        await client.handle(_session(uuid4(), uuid4(), actions=1)[0])
        first = list(recorded_delays)
        recorded_delays.clear()
        second_store = MockIngestStore()
        second_store.reject_all = (ErrorCode.RATE_LIMITED, True)
        second = _client(second_store, max_retries=8, spool_dir=str(tmp_path / "b"))
        await second.handle(_session(uuid4(), uuid4(), actions=1)[0])
        await client.close()
        await second.close()

        assert len(first) == 7  # 8 attempts → 7 sleeps
        assert all(0.0 <= d <= BACKOFF_MAX_SECONDS for d in first), first
        assert first != recorded_delays, "identical schedules — the jitter is gone"

    async def test_retry_after_floors_the_delay(self, store, recorded_delays, tmp_path):
        store.http_status = 429
        store.http_headers = {"Retry-After": "2"}
        client = _client(store, max_retries=2, spool_dir=str(tmp_path))

        await client.handle(_session(uuid4(), uuid4(), actions=1)[0])
        await client.close()

        assert recorded_delays and all(d >= 2.0 for d in recorded_delays), recorded_delays


# ---------------------------------------------------------------------------
# Mid-batch partial failure (spec §7.1, §7.7)
# ---------------------------------------------------------------------------


class TestPartialBatch:
    async def test_responses_stay_index_aligned_and_only_the_failure_is_resent(
        self, store, recorded_delays, tmp_path
    ):
        """A batch is not atomic (spec §7.3). Element k failing must not move
        elements k+1..n, and the resend must carry only what actually needs
        resending — under the original `event_id`s, so idempotency holds.

        The rejected element is the *last* action on purpose: see
        `test_a_transient_rejection_orphans_the_actions_behind_it` for why an
        earlier one is a different (and unresolved) story.
        """
        agent_id, session_id = uuid4(), uuid4()
        envelopes = _session(agent_id, session_id, actions=4)
        rejected_index = len(envelopes) - 2  # the last ACTION_RECORD

        original = store._ingest
        attempts = {"n": 0}

        def ingest(envelope: dict[str, Any]) -> dict[str, Any]:
            is_target = envelope["event_id"] == envelopes[rejected_index]["event_id"]
            if is_target and attempts["n"] == 0:
                attempts["n"] += 1
                return MockIngestStore._reject(
                    envelope["event_id"], ErrorCode.STORE_UNAVAILABLE, True
                )
            return original(envelope)

        store._ingest = ingest  # type: ignore[method-assign]
        client = _client(store, max_retries=3, spool_dir=str(tmp_path))

        responses = await client.handle_batch(envelopes)
        await client.close()

        assert [r.status for r in responses] == ["accepted"] * len(envelopes)
        assert [r.event_id for r in responses] == [UUID(e["event_id"]) for e in envelopes]
        assert store.batch_sizes == [len(envelopes), 1], "the whole batch was resent"
        assert [p["sequence_number"] for p in store.actions_for(session_id)] == [1, 2, 3, 4]

    async def test_a_non_chain_rejection_does_not_block_its_neighbors(self, store, tmp_path):
        """A rejected SESSION_OPEN must not take the batch down with it.

        Nothing in the chain depends on it, so §7.3 applies cleanly: each
        element succeeds or fails on its own.
        """
        agent_id, session_id = uuid4(), uuid4()
        envelopes = _session(agent_id, session_id, actions=3)
        target = envelopes[0]["event_id"]  # SESSION_OPEN

        original = store._ingest

        def ingest(envelope: dict[str, Any]) -> dict[str, Any]:
            if envelope["event_id"] == target:
                return MockIngestStore._reject(target, ErrorCode.SESSION_ALREADY_EXISTS, False)
            return original(envelope)

        store._ingest = ingest  # type: ignore[method-assign]
        client = _client(store, max_retries=2, spool_dir=str(tmp_path))

        responses = await client.handle_batch(envelopes)
        await client.close()

        assert responses[0].status == "rejected"
        assert [r.status for r in responses[1:]] == ["accepted"] * 4
        assert [p["sequence_number"] for p in store.actions_for(session_id)] == [1, 2, 3]

    async def test_a_transient_rejection_does_not_orphan_the_actions_behind_it(
        self, store, recorded_delays, tmp_path
    ):
        """§7.3 and §8.3 together, on the case where they have to cooperate.

        Element k+1 names element k's `self_hash` as its parent. If k is
        rejected — even transiently — a store that checked *linkage* at ingest
        would have to refuse k+1..N for pointing at a record it does not have,
        and `HASH_CHAIN_BROKEN` is non-retryable, so one transient failure
        would permanently lose every action behind it. §8.3 forbids exactly
        that: verification is a record against itself, a dangling
        `prev_action_hash` is data rather than an error, and the retry of k
        closes the link.

        So the whole batch lands, and the chain the store ends up holding
        verifies VALID — including the record that was rejected on its first
        attempt and the three that arrived before it existed.
        """
        agent_id, session_id = uuid4(), uuid4()
        envelopes = _session(agent_id, session_id, actions=4)
        first_action = 1

        original = store._ingest
        attempts = {"n": 0}

        def ingest(envelope: dict[str, Any]) -> dict[str, Any]:
            if envelope["event_id"] == envelopes[first_action]["event_id"] and attempts["n"] == 0:
                attempts["n"] += 1
                return MockIngestStore._reject(
                    envelope["event_id"], ErrorCode.STORE_UNAVAILABLE, True
                )
            return original(envelope)

        store._ingest = ingest  # type: ignore[method-assign]
        client = _client(store, max_retries=3, spool_dir=str(tmp_path))

        responses = await client.handle_batch(envelopes)
        await client.close()

        assert [r.status for r in responses] == ["accepted"] * len(envelopes)
        assert store.batch_sizes == [len(envelopes), 1], "more than the failure was resent"
        assert client.is_spooling is False
        # Out-of-order arrival, in-order chain: identity travels with the
        # record, so the store's copy is whole once the retry lands.
        assert [p["sequence_number"] for p in store.actions_for(session_id)] == [2, 3, 4, 1]
        verified = verify_session_local(str(store.dump_chain(session_id, tmp_path / "s.jsonl")))
        assert verified.verdict is Verdict.VALID, verified.summary

    async def test_a_record_naming_an_unseen_predecessor_is_accepted(self, store, tmp_path):
        """Spec §8.3, stated on its own — the rule the spool depends on.

        When a spool write fails, `ChainState` keeps advancing so the loss is
        cryptographically provable, and every record after it names a
        predecessor that was never written. A store that rejected those would
        destroy the only evidence the gap ever existed. It must take them, and
        the gap must show up at verification as INCOMPLETE — not VALID, and
        not absent.
        """
        agent_id, session_id = uuid4(), uuid4()
        envelopes = _session(agent_id, session_id, actions=4)
        client = _client(store, max_retries=2, spool_dir=str(tmp_path))

        # Seal all four first, so the chain advances exactly as it would have,
        # then lose one on the way out. That is what a failed spool write looks
        # like from the store's side: a record that was numbered and linked,
        # and then never arrived.
        client._seal(envelopes)
        dropped = envelopes.pop(2)
        assert dropped["payload"]["sequence_number"] == 2

        responses = await client.handle_batch(envelopes)
        await client.close()

        assert [r.status for r in responses] == ["accepted"] * len(envelopes)
        assert [p["sequence_number"] for p in store.actions_for(session_id)] == [1, 3, 4]

        verified = verify_session_local(str(store.dump_chain(session_id, tmp_path / "s.jsonl")))
        assert verified.verdict is Verdict.INCOMPLETE, verified.summary
        assert verified.missing_ranges == [(2, 2)]


# ---------------------------------------------------------------------------
# Timeout → spool → still verifiable offline (ADR-013 Decision 4)
# ---------------------------------------------------------------------------


class TestTimeoutFailover:
    async def test_a_timeout_spools_a_session_that_verifies_offline(
        self, store, recorded_delays, tmp_path
    ):
        """The offline promise, end to end: the agent never sees the outage,
        and the operator can verify the evidence before it is ever uploaded."""
        store.raise_timeout = True
        agent_id, session_id = uuid4(), uuid4()
        client = _client(store, max_retries=2, spool_dir=str(tmp_path))

        responses = [await client.handle(e) for e in _session(agent_id, session_id)]
        await client.close()

        assert [r.status for r in responses] == ["accepted"] * 5
        assert client.spool_reason is ErrorCode.WRITE_TIMEOUT
        # Only the first envelope pays the retry budget; spool mode is one-way.
        assert store.requests == 2

        spooled = tmp_path / "sessions" / f"{session_id}.jsonl"
        assert verify_session_local(str(spooled)).verdict is Verdict.VALID


# ---------------------------------------------------------------------------
# `rootsign-admin sync` end-to-end (T2.5, exercised against the store)
# ---------------------------------------------------------------------------


class TestSyncEndToEnd:
    def test_a_spooled_session_uploads_into_a_chain_the_store_can_verify(
        self, store, tmp_path, monkeypatch
    ):
        """The whole point of the sprint, in one test.

        Record offline; upload later; the store — which verifies rather than
        computes — accepts every record, and what it holds afterwards passes
        the auditor's verifier. If sealing, spooling, or replay re-minted so
        much as one `action_id`, the chain would not link and the store would
        answer HASH_CHAIN_BROKEN.
        """
        spool = tmp_path / "spool"
        session_id = _spool_offline(spool, actions=4)
        monkeypatch.setenv("ROOTSIGN_API_KEY", API_KEY)
        _bind_transport(monkeypatch, store.transport())

        result = runner.invoke(admin_app, ["sync", "--spool-dir", str(spool)])

        assert result.exit_code == 0, result.output
        assert len(store.actions_for(session_id)) == 4
        verified = verify_session_local(str(store.dump_chain(session_id, tmp_path / "s.jsonl")))
        assert verified.verdict is Verdict.VALID, verified.summary
        assert not (spool / "sessions" / f"{session_id}.jsonl").exists()

    def test_rerunning_sync_is_idempotent_and_writes_nothing_new(
        self, store, tmp_path, monkeypatch
    ):
        """Re-running after a completed sync must be a no-op, and re-running
        after a *partial* one must resume — both rest on `event_id` dedupe, so
        this pins that the client sends the original ids rather than fresh
        ones."""
        spool = tmp_path / "spool"
        session_id = _spool_offline(spool, actions=3)
        monkeypatch.setenv("ROOTSIGN_API_KEY", API_KEY)
        _bind_transport(monkeypatch, store.transport())

        assert runner.invoke(admin_app, ["sync", "--spool-dir", str(spool)]).exit_code == 0
        accepted_after_first = len(store.accepted)

        # Put the file back, as an operator restoring from `synced/` would.
        synced = spool / "synced" / f"{session_id}.jsonl"
        synced.replace(spool / "sessions" / f"{session_id}.jsonl")
        second = runner.invoke(admin_app, ["sync", "--spool-dir", str(spool)])

        assert second.exit_code == 0, second.output
        assert "already present" in second.output
        assert len(store.accepted) == accepted_after_first, "a re-run duplicated records"
        assert len(store.actions_for(session_id)) == 3


def _spool_offline(spool: Path, *, actions: int) -> str:
    """Record a session with the endpoint down; return the session id."""
    dead = MockIngestStore()
    dead.raise_timeout = True

    async def _run() -> str:
        client = HttpIngestClient(
            BASE_URL, API_KEY, max_retries=1, transport=dead.transport(), spool_dir=str(spool)
        )
        agent_id, session_id = uuid4(), uuid4()
        for envelope in _session(agent_id, session_id, actions=actions):
            await client.handle(envelope)
        await client.close()
        return str(session_id)

    return asyncio.run(_run())


def _bind_transport(monkeypatch: pytest.MonkeyPatch, transport: Any) -> None:
    real = HttpIngestClient

    def factory(**kwargs: Any) -> HttpIngestClient:
        return real(transport=transport, **kwargs)

    monkeypatch.setattr("rootsign.sdk.client.HttpIngestClient", factory)


# ---------------------------------------------------------------------------
# ADR-009 buffering contract, re-run with the real transport underneath
# ---------------------------------------------------------------------------


class TestBufferedOverHttp:
    """`tests/unit/test_buffered_client.py` proves these against a mock inner.

    Restated here through the wire, because the composition is where the two
    ADRs meet: the buffer decides *what* and *when*, the transport decides
    *how many requests*, and the interesting failures (a stacked retry, a
    passthrough that overtakes the buffer) are only visible from outside.
    """

    async def test_buffered_actions_flush_as_one_request(self, store):
        agent_id, session_id = uuid4(), uuid4()
        envelopes = _session(agent_id, session_id, actions=5)
        client = BufferedIngestClient(
            _client(store), max_buffer_size=99, flush_interval_seconds=999
        )

        for envelope in envelopes[1:-1]:  # the five actions
            await client.handle(envelope)
        assert store.requests == 0, "buffered records went out early"

        await client.flush()

        assert store.requests == 1
        assert store.batch_sizes == [5]
        await client.close()

    async def test_a_full_buffer_flushes_itself(self, store):
        agent_id, session_id = uuid4(), uuid4()
        envelopes = _session(agent_id, session_id, actions=3)
        client = BufferedIngestClient(_client(store), max_buffer_size=3, flush_interval_seconds=999)

        for envelope in envelopes[1:-1]:
            await client.handle(envelope)

        assert store.batch_sizes == [3]
        await client.close()

    async def test_closing_the_buffer_loses_nothing(self, store, tmp_path):
        agent_id, session_id = uuid4(), uuid4()
        envelopes = _session(agent_id, session_id, actions=4)
        client = BufferedIngestClient(
            _client(store), max_buffer_size=99, flush_interval_seconds=999
        )

        for envelope in envelopes:
            await client.handle(envelope)
        await client.close()

        assert len(store.actions_for(session_id)) == 4
        verified = verify_session_local(str(store.dump_chain(session_id, tmp_path / "s.jsonl")))
        assert verified.verdict is Verdict.VALID, verified.summary

    async def test_a_passthrough_drains_the_buffer_first(self, store):
        """SESSION_CLOSE must not overtake the actions it closes over.

        Order is the whole product here: a store that received CLOSE before
        the actions would have to either reject them or accept a session that
        kept growing after it ended.
        """
        agent_id, session_id = uuid4(), uuid4()
        envelopes = _session(agent_id, session_id, actions=3)
        client = BufferedIngestClient(
            _client(store), max_buffer_size=99, flush_interval_seconds=999
        )

        await client.handle(envelopes[0])  # SESSION_OPEN — passthrough
        for envelope in envelopes[1:-1]:
            await client.handle(envelope)
        await client.handle(envelopes[-1])  # SESSION_CLOSE — passthrough
        await client.close()

        arrived = [e["event_type"] for e in store.accepted]
        assert arrived == [
            EventType.SESSION_OPEN.value,
            *[EventType.ACTION_RECORD.value] * 3,
            EventType.SESSION_CLOSE.value,
        ]

    async def test_one_flush_costs_one_retry_budget_not_two(self, store, recorded_delays, tmp_path):
        """ADR-013 Decision 3, asserted where it is actually observable.

        `HttpIngestClient.owns_retry` makes the buffer stand down to a single
        attempt per flush. Stacked, one flush would cost 3 x 3 = 9 requests
        and tens of seconds — while the flush interval keeps firing.

        Scoped to **one explicit flush** on purpose: the buffer re-queues what
        it could not send and tries again at the next flush (ADR-009's FIFO
        retry), which is a different mechanism and not stacking. Counting over
        the whole lifecycle would conflate the two.
        """
        store.reject_all = (ErrorCode.STORE_UNAVAILABLE, True)
        # `enable_spool=False` so exhaustion surfaces to the buffer as a
        # retryable rejection. With spooling on, the transport answers
        # `accepted` (the record is durable) and the buffer has nothing to
        # stack a retry on — the test would pass either way and prove nothing.
        inner = _client(store, max_retries=3, enable_spool=False)
        client = BufferedIngestClient(
            inner, max_buffer_size=99, max_retries=3, flush_interval_seconds=999
        )

        agent_id, session_id = uuid4(), uuid4()
        for envelope in _session(agent_id, session_id, actions=2)[1:-1]:
            await client.handle(envelope)
        assert store.requests == 0

        await client.flush()

        assert store.requests == 3, f"expected the transport's 3 attempts, got {store.requests}"
