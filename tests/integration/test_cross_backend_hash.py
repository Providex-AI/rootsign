"""Cross-backend hash contract (ADR-011 T2.8, extended to cloud by ADR-013 T2.3).

Drives the *same* scripted session through **all three** backends — jsonl,
postgres, cloud — and proves they share one hash formula:

  * The JSONL client's stored `self_hash` equals `compute_action_self_hash`
    recomputed from the record's canonical fields — i.e. the client assembles
    the canonical input exactly as the frozen spec expects (no second formula).
  * A session driven through JsonlIngestClient verifies VALID via
    `verify_session_local`, AND the same session driven through the Postgres
    store verifies VALID via `action_crud.verify_chain`.
  * The exact byte string each backend feeds into SHA-256 is captured and
    asserted byte-identical — parity at the canonical *input* level, not just
    at the digest.
  * Tampering a hashed field flips `verify` to TAMPERED at the right sequence.
  * Tampering a field that sits OUTSIDE `self_hash` but is re-bound to it
    (`input_redacted` -> `input_hash`) is caught on BOTH backends, with the
    same verdict and the same sequence number.

Note on "byte-identical": every backend mints its own `action_id`, so for an
unpinned run the chains' hashes are not literally equal action-for-action.
`test_canonical_hash_input_is_byte_identical` pins identity so that any
*remaining* difference is a genuine canonicalization divergence — field set,
ordering, or value normalization. That turns the note from an assumption into
an assertion.

**Shape of this harness (T2.3).** Backends are entries in `BACKEND_BUILDERS`
and identity is pinned once, at `rootsign.chain_state.uuid4` — the single
minting point. Before that extraction the harness patched `uuid4` in two
modules, so each new backend meant another patch and a harness that quietly
proved less as it grew. Adding a fourth backend is now one dict entry.

`seeded_agent` (committed) for the Postgres side (Flag 3).
"""

from __future__ import annotations

import copy
import json
import sys
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from rootsign.crud import action as action_crud
from rootsign.hashing import compute_action_self_hash
from rootsign.sdk.chain import verify_session_local
from rootsign.sdk.client import HttpIngestClient, LocalIngestClient
from rootsign.sdk.hashing import compute_payload_hash
from rootsign.sdk.jsonl_client import JsonlIngestClient
from tests.conftest import make_envelope


def _scripted_envelopes(agent_id, session_id):
    """One session: SESSION_OPEN → 3 actions (+ decision + approval) → CLOSE.
    Identical envelope list fed to both backends."""
    envs = [make_envelope("SESSION_OPEN", agent_id, session_id, {"objective": "cross-backend"})]
    for i, tool in enumerate(("send_email", "query_db", "write_file")):
        ir = {"tool": tool, "i": i}
        envs.append(
            make_envelope(
                "ACTION_RECORD",
                agent_id,
                session_id,
                {
                    "tool_name": tool,
                    "input_hash": compute_payload_hash(ir),
                    "output_hash": "b" * 64,
                    "input_redacted": ir,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "authorization_status": "auto_authorized",
                },
            )
        )
    envs.append(make_envelope("SESSION_CLOSE", agent_id, session_id, {"status": "completed"}))
    return envs


class _CapturePreimages:
    """Record every byte string `compute_action_self_hash` hands to SHA-256.

    Intercepts `hashlib` *inside* rootsign.hashing rather than reconstructing
    the canonical form here. Re-deriving it in a test is precisely the drift
    this file exists to catch — a test-local copy would agree with itself while
    the store quietly diverged (see the "never re-implement" rule in ADR-001).
    """

    def __init__(self) -> None:
        self.preimages: list[bytes] = []

    def __enter__(self) -> "_CapturePreimages":
        # Reached via sys.modules, matching how `pinned_identity` gets at its
        # patch target, so the file keeps a single import style.
        self._module = sys.modules["rootsign.hashing"]
        self._saved = self._module.hashlib
        real_sha256 = self._saved.sha256
        capture = self

        class _Shim:
            @staticmethod
            def sha256(data: bytes = b""):
                capture.preimages.append(data)
                return real_sha256(data)

        self._module.hashlib = _Shim
        return self

    def __exit__(self, *exc) -> bool:
        self._module.hashlib = self._saved
        return False

    def as_dicts(self) -> list[dict]:
        return [json.loads(b.decode("utf-8")) for b in self.preimages]


class _PinnedIdentity:
    """Hands out the same `action_id`s, in order, to whichever backend asks.

    `action_id` is assigned identity, not a property of the logical action, so
    an unpinned run differs in that field and — through the chain — in
    `prev_action_hash` too. Pinning holds identity constant so a byte
    comparison isolates canonicalization.

    One patch target: `rootsign.chain_state.uuid4`, the single minting point
    (T2.3). `reset()` rewinds the sequence before each backend runs.
    """

    def __init__(self, ids: list[UUID]) -> None:
        self.ids = ids
        self._iter = iter(ids)

    def reset(self) -> None:
        self._iter = iter(self.ids)

    def __call__(self) -> UUID:
        return next(self._iter)


@pytest.fixture
def pinned_identity(monkeypatch) -> _PinnedIdentity:
    pinned = _PinnedIdentity([uuid4() for _ in range(8)])
    # Reached via sys.modules to match `_CapturePreimages`' patch style.
    monkeypatch.setattr(sys.modules["rootsign.chain_state"], "uuid4", pinned)
    return pinned


# ---------------------------------------------------------------------------
# Backend builders — one entry per backend, nothing else to re-plumb (T2.3)
# ---------------------------------------------------------------------------


def _accept_all(request: httpx.Request) -> httpx.Response:
    """A server that accepts every envelope in the batch, index-aligned."""
    batch = json.loads(request.content)
    return httpx.Response(
        200,
        json=[
            {"status": "accepted", "event_id": env["event_id"], "entity_id": str(uuid4())}
            for env in batch
        ],
    )


async def _drive_jsonl(envelopes: list[dict], *, tmp_path, db) -> Any:
    client = JsonlIngestClient(data_dir=tmp_path / "jsonl")
    for env in envelopes:
        await client.handle(env)
    return client


async def _drive_postgres(envelopes: list[dict], *, tmp_path, db) -> Any:
    client = LocalIngestClient(db=db)
    for env in envelopes:
        await client.handle(env)
    await db.commit()
    return client


async def _drive_cloud(envelopes: list[dict], *, tmp_path, db) -> Any:
    client = HttpIngestClient(
        "https://ingest.example.test/v1",
        "sk-parity",
        transport=httpx.MockTransport(_accept_all),
    )
    for env in envelopes:
        await client.handle(env)
    await client.close()
    return client


BACKEND_BUILDERS = {
    "jsonl": _drive_jsonl,
    "postgres": _drive_postgres,
    "cloud": _drive_cloud,
}


async def _canonical_preimages(
    backend: str, envelopes: list[dict], *, tmp_path, db, pinned: _PinnedIdentity
) -> list[bytes]:
    """Run one backend over a private copy of the envelopes; return its pre-images.

    The copy matters: the cloud transport seals payloads **in place** (ADR-013
    Decision 1), and a sealed payload handed to the Postgres store is rejected
    by design. Each backend must see the envelopes as the SDK first emitted them.
    """
    pinned.reset()
    with _CapturePreimages() as capture:
        await BACKEND_BUILDERS[backend](copy.deepcopy(envelopes), tmp_path=tmp_path, db=db)
    return capture.preimages


class TestCrossBackendHash:
    async def test_jsonl_self_hash_uses_frozen_formula(self, tmp_path):
        # The JSONL client's stored self_hash must equal the frozen canonical
        # formula recomputed from the record's own fields — proving identical
        # field assembly to the store (which the DB verify path also relies on).
        client = JsonlIngestClient(data_dir=tmp_path)
        sid = uuid4()
        await client.handle(make_envelope("SESSION_OPEN", uuid4(), sid, {}))
        await client.handle(
            make_envelope(
                "ACTION_RECORD",
                uuid4(),
                sid,
                {
                    "tool_name": "t",
                    "input_hash": "a" * 64,
                    "output_hash": None,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "authorization_status": "auto_authorized",
                },
            )
        )
        rec = next(
            json.loads(ln)
            for ln in client._session_path(str(sid)).read_text().splitlines()
            if json.loads(ln).get("event_type") == "ACTION_RECORD"
        )
        assert rec["self_hash"] == compute_action_self_hash(rec)

    async def test_same_session_verifies_valid_on_both_backends(
        self, tmp_path, clean_db, seeded_agent
    ):
        session_id = uuid4()
        envelopes = _scripted_envelopes(seeded_agent.agent_id, session_id)

        # JSONL backend.
        jc = JsonlIngestClient(data_dir=tmp_path)
        for e in envelopes:
            await jc.handle(e)
        jsonl_result = verify_session_local(str(jc._session_path(str(session_id))))

        # Postgres backend — same scripted envelopes.
        pc = LocalIngestClient(db=clean_db)
        for e in envelopes:
            await pc.handle(e)
        await clean_db.commit()
        pg_result = await action_crud.verify_chain(clean_db, session_id=session_id)

        assert jsonl_result.valid is True
        assert pg_result["valid"] is True
        # Both saw the same 3 actions with dense sequences.
        assert jsonl_result.record_count == 3
        assert pg_result["record_count"] == 3
        pg_chain = await action_crud.get_session_chain(clean_db, session_id=session_id)
        assert [a.sequence_number for a in pg_chain] == [1, 2, 3]

    @pytest.mark.parametrize(
        ("field", "forged"),
        [("input_hash", "f" * 64), ("tool_name", "TAMPERED_TOOL")],
        ids=["input_hash", "tool_name"],
    )
    async def test_jsonl_tamper_flips_to_tampered(self, tmp_path, field, forged):
        """Any canonical field, not just the hashes: both are inside self_hash,
        so either one must surface at the sequence it was altered."""
        client = JsonlIngestClient(data_dir=tmp_path)
        sid = uuid4()
        for e in _scripted_envelopes(uuid4(), sid):
            await client.handle(e)
        path = client._session_path(str(sid))
        lines = path.read_text().splitlines()
        for i, ln in enumerate(lines):
            rec = json.loads(ln)
            if rec.get("event_type") == "ACTION_RECORD" and rec["sequence_number"] == 2:
                rec[field] = forged  # break the canonical input at seq 2
                lines[i] = json.dumps(rec)
                break
        path.write_text("\n".join(lines) + "\n")
        result = verify_session_local(str(path))
        assert result.valid is False
        assert result.first_invalid_sequence == 2

    async def test_canonical_hash_input_is_byte_identical(
        self, tmp_path, clean_db, seeded_agent, pinned_identity
    ):
        """Every backend must feed SHA-256 the same bytes (T2.8, extended by T2.3).

        Comparing digests alone would mostly re-prove that SHA-256 is
        deterministic. Comparing the pre-images proves the backends agree on
        *which* fields are canonical, in what order, and how each value is
        normalized — the things that actually drift. Cloud joins as a third
        entry in `BACKEND_BUILDERS`, not as a third patch target.
        """
        session_id = uuid4()
        envelopes = _scripted_envelopes(seeded_agent.agent_id, session_id)

        preimages = {
            backend: await _canonical_preimages(
                backend, envelopes, tmp_path=tmp_path, db=clean_db, pinned=pinned_identity
            )
            for backend in BACKEND_BUILDERS
        }

        reference_name, reference = next(iter(preimages.items()))
        assert len(reference) == 3, (
            f"expected 3 action pre-images from {reference_name}, got {len(reference)}"
        )

        for name, captured in preimages.items():
            assert len(captured) == len(reference), (
                f"{name} produced {len(captured)} pre-images, {reference_name} produced "
                f"{len(reference)} — a backend is hashing a different number of records"
            )
            # Field sets first — a divergence there is more legible than a byte diff.
            assert [sorted(json.loads(b)) for b in captured] == [
                sorted(json.loads(b)) for b in reference
            ], f"canonical field sets diverge: {name} vs {reference_name}"
            for i, (got, want) in enumerate(zip(captured, reference), start=1):
                assert got == want, (
                    f"canonical SHA-256 input differs at action {i}:\n"
                    f"  {name}: {got.decode()}\n"
                    f"  {reference_name}: {want.decode()}"
                )

    async def test_all_backends_are_covered_by_the_parity_harness(self):
        """A backend that isn't a builder entry is a backend nobody checks.

        The tripwire for the T2.3 shape: adding a client-side backend means
        adding an entry here, not re-plumbing patch targets elsewhere.
        """
        assert set(BACKEND_BUILDERS) == {"jsonl", "postgres", "cloud"}

    async def test_cloud_seals_the_record_before_it_leaves_the_process(
        self, tmp_path, clean_db, seeded_agent, pinned_identity
    ):
        """ADR-013 Decision 1: the four chain fields ride in the payload (spec §8.2).

        And the seal is the *same* seal the JSONL backend would have computed —
        pinned identity makes that comparable, so a divergence here is the
        cloud path having grown its own sealer.
        """
        session_id = uuid4()
        envelopes = _scripted_envelopes(seeded_agent.agent_id, session_id)

        sent: list[dict] = []

        def capture(request: httpx.Request) -> httpx.Response:
            sent.extend(json.loads(request.content))
            return _accept_all(request)

        pinned_identity.reset()
        cloud = HttpIngestClient(
            "https://ingest.example.test/v1",
            "sk-seal",
            transport=httpx.MockTransport(capture),
        )
        for env in copy.deepcopy(envelopes):
            await cloud.handle(env)
        await cloud.close()

        actions = [e for e in sent if e["event_type"] == "ACTION_RECORD"]
        assert len(actions) == 3
        assert [a["payload"]["sequence_number"] for a in actions] == [1, 2, 3]
        assert actions[0]["payload"]["prev_action_hash"] is None
        for prev, nxt in zip(actions, actions[1:]):
            assert nxt["payload"]["prev_action_hash"] == prev["payload"]["self_hash"], (
                "cloud chain does not link"
            )

        # The seal matches what the JSONL backend computes for the same session.
        pinned_identity.reset()
        jsonl = JsonlIngestClient(data_dir=tmp_path / "seal-parity")
        for env in copy.deepcopy(envelopes):
            await jsonl.handle(env)
        local = [
            json.loads(ln)
            for ln in jsonl._session_path(str(session_id)).read_text().splitlines()
            if json.loads(ln).get("event_type") == "ACTION_RECORD"
        ]
        assert [a["payload"]["self_hash"] for a in actions] == [r["self_hash"] for r in local]
        assert [a["payload"]["action_id"] for a in actions] == [r["action_id"] for r in local]

    async def test_a_sealed_record_keeps_its_identity_through_the_jsonl_writer(
        self, tmp_path, clean_db, seeded_agent
    ):
        """The spool handoff (T2.4's precondition): adoption, not re-minting.

        A cloud-sealed envelope written to a session file must land with the
        identity it was sealed under. Re-minting would produce a locally
        consistent chain that never happened — and the record the client
        believes it sent would exist nowhere.
        """
        session_id = uuid4()
        envelopes = _scripted_envelopes(seeded_agent.agent_id, session_id)

        sent: list[dict] = []

        def capture(request: httpx.Request) -> httpx.Response:
            sent.extend(json.loads(request.content))
            return _accept_all(request)

        cloud = HttpIngestClient(
            "https://ingest.example.test/v1",
            "sk-spool",
            transport=httpx.MockTransport(capture),
        )
        for env in copy.deepcopy(envelopes):
            await cloud.handle(env)
        await cloud.close()

        # Replay the *sealed* envelopes through the file writer, as the spool will.
        spool = JsonlIngestClient(data_dir=tmp_path / "spool")
        for env in sent:
            await spool.handle(env)

        written = [
            json.loads(ln)
            for ln in spool._session_path(str(session_id)).read_text().splitlines()
            if json.loads(ln).get("event_type") == "ACTION_RECORD"
        ]
        sealed = [e["payload"] for e in sent if e["event_type"] == "ACTION_RECORD"]
        assert [w["self_hash"] for w in written] == [p["self_hash"] for p in sealed]
        assert [w["action_id"] for w in written] == [p["action_id"] for p in sealed]
        assert [w["sequence_number"] for w in written] == [1, 2, 3]

        # And the adopted chain verifies offline — the whole point of spooling.
        result = verify_session_local(str(spool._session_path(str(session_id))))
        assert result.valid is True, result.error

    async def test_postgres_rejects_a_client_sealed_record(self, clean_db, seeded_agent):
        """The store assigns identity under a row lock; it cannot honor a seal.

        Accepting one and recomputing would fork the chain silently — the
        client would hold a self_hash the store never stored. Spec §8.2 makes
        sealed records cloud-only; this is the loud half of that rule.
        """
        session_id = uuid4()
        envelopes = _scripted_envelopes(seeded_agent.agent_id, session_id)

        cloud = HttpIngestClient(
            "https://ingest.example.test/v1",
            "sk-reject",
            transport=httpx.MockTransport(_accept_all),
        )
        sealed_envelopes = copy.deepcopy(envelopes)
        for env in sealed_envelopes:
            await cloud.handle(env)
        await cloud.close()

        pg = LocalIngestClient(db=clean_db)
        responses = [await pg.handle(env) for env in sealed_envelopes]
        await clean_db.commit()

        action_responses = [
            r for r, e in zip(responses, sealed_envelopes) if e["event_type"] == "ACTION_RECORD"
        ]
        assert all(r.status == "rejected" for r in action_responses)
        assert all(r.error_code.value == "VALIDATION_ERROR" for r in action_responses)
        assert all("cloud-mode only" in (r.error_message or "") for r in action_responses)

    async def test_payload_binding_tamper_caught_on_both_backends(
        self, tmp_path, clean_db, seeded_agent
    ):
        """`input_redacted` is excluded from `self_hash` (ADR-001) but re-bound
        to `input_hash`, which is canonical. Rewriting a redacted payload must
        therefore be caught — identically — on both backends.

        Worth asserting across both: the binding is implemented twice, in
        `crud.action._payload_binding_error` and in `sdk.chain`, so the two can
        drift. A store where the DB path catches a rewritten payload and the
        offline path does not would be a silent hole in the audit story.
        """
        from sqlalchemy import select, update

        from rootsign.models.action import Action

        session_id = uuid4()
        envelopes = _scripted_envelopes(seeded_agent.agent_id, session_id)

        jsonl = JsonlIngestClient(data_dir=tmp_path)
        pg = LocalIngestClient(db=clean_db)
        for env in envelopes:
            await jsonl.handle(env)
            await pg.handle(env)
        await clean_db.commit()

        forged = {"tool": "ATTACKER_REWROTE_THIS", "i": 99}

        path = jsonl._session_path(str(session_id))
        lines = path.read_text().splitlines()
        for i, ln in enumerate(lines):
            rec = json.loads(ln)
            if rec.get("event_type") == "ACTION_RECORD" and rec.get("sequence_number") == 2:
                rec["input_redacted"] = forged
                lines[i] = json.dumps(rec)
                break
        path.write_text("\n".join(lines) + "\n")

        row = (
            await clean_db.execute(
                select(Action).where(
                    Action.session_id == session_id, Action.sequence_number == 2
                )
            )
        ).scalar_one()
        # Hypertable-safe two-column form (action_id, timestamp).
        await clean_db.execute(
            update(Action)
            .where(Action.action_id == row.action_id, Action.timestamp == row.timestamp)
            .values(input_redacted=forged)
        )
        await clean_db.commit()

        jsonl_result = verify_session_local(str(path))
        pg_result = await action_crud.verify_chain(clean_db, session_id=session_id)

        assert jsonl_result.valid is False, "offline verifier missed the payload tamper"
        assert pg_result["valid"] is False, "Postgres verifier missed the payload tamper"
        assert jsonl_result.first_invalid_sequence == 2
        assert "payload_hash mismatch" in (jsonl_result.error or "")
        assert "payload_hash mismatch" in (pg_result.get("error") or "")
        assert "sequence_number=2" in (pg_result.get("error") or ""), pg_result.get("error")
