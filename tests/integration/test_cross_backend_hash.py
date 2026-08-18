"""T2.8 — cross-backend hash contract (ADR-011, the sprint's cardinal test).

Drives the *same* scripted session through both backends and proves they share
one hash formula:

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

Note on "byte-identical": each backend mints its own `action_id` (`uuid4()`),
so for an unpinned run the two chains' hashes are not literally equal
action-for-action. `test_canonical_hash_input_is_byte_identical` pins the
assigned ids so that identity assignment is held constant and any *remaining*
difference would be a genuine canonicalization divergence — field set,
ordering, or value normalization. That turns this note from an assumption into
an assertion. `seeded_agent` (committed) for the Postgres side (Flag 3).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from rootsign.crud import action as action_crud
from rootsign.hashing import compute_action_self_hash
from rootsign.sdk.chain import verify_session_local
from rootsign.sdk.client import LocalIngestClient
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
        # Reached via sys.modules, matching how _pin_action_ids gets at its
        # patch targets, so the file keeps a single import style.
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


def _pin_action_ids(monkeypatch, ids: list[UUID]) -> None:
    """Make both stores assign the same `action_id`s, in order.

    `action_id` is store-assigned identity (`uuid4()` in jsonl_client and in
    crud.action), not a property of the logical action, so an unpinned run
    differs in that field and — via the chain — in `prev_action_hash` too.
    Pinning holds identity constant so the comparison isolates canonicalization.
    """
    seq = iter(ids)

    def _next() -> UUID:
        return next(seq)

    # `rootsign.crud.action` resolves to the CRUDAction *instance* (crud/__init__
    # rebinds the name), so reach the modules through sys.modules.
    monkeypatch.setattr(sys.modules["rootsign.sdk.jsonl_client"], "uuid4", _next)
    monkeypatch.setattr(sys.modules["rootsign.crud.action"], "uuid4", _next)


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
        self, tmp_path, clean_db, seeded_agent, monkeypatch
    ):
        """T2.8 at the input level: both backends must feed SHA-256 the same bytes.

        Comparing digests alone would mostly re-prove that SHA-256 is
        deterministic. Comparing the pre-images proves the two stores agree on
        *which* fields are canonical, in what order, and how each value is
        normalized — the things that actually drift.
        """
        session_id = uuid4()
        envelopes = _scripted_envelopes(seeded_agent.agent_id, session_id)
        pinned = [uuid4() for _ in range(3)]

        _pin_action_ids(monkeypatch, list(pinned))
        jsonl = JsonlIngestClient(data_dir=tmp_path)
        with _CapturePreimages() as jsonl_cap:
            for env in envelopes:
                await jsonl.handle(env)

        _pin_action_ids(monkeypatch, list(pinned))
        pg = LocalIngestClient(db=clean_db)
        with _CapturePreimages() as pg_cap:
            for env in envelopes:
                await pg.handle(env)
        await clean_db.commit()

        assert len(jsonl_cap.preimages) == 3, (
            f"expected 3 action pre-images from JSONL, got {len(jsonl_cap.preimages)}"
        )
        assert len(jsonl_cap.preimages) == len(pg_cap.preimages)

        # Field sets first — a divergence here is more legible than a byte diff.
        assert [sorted(d) for d in jsonl_cap.as_dicts()] == [
            sorted(d) for d in pg_cap.as_dicts()
        ], "canonical field sets diverge between backends"

        for i, (j, p) in enumerate(zip(jsonl_cap.preimages, pg_cap.preimages), start=1):
            assert j == p, (
                f"canonical SHA-256 input differs at action {i}:\n"
                f"  jsonl: {j.decode()}\n"
                f"  pg   : {p.decode()}"
            )

        # And the chains both verify, so identical inputs really did produce
        # a valid chain on each side rather than matching garbage.
        jsonl_result = verify_session_local(str(jsonl._session_path(str(session_id))))
        pg_result = await action_crud.verify_chain(clean_db, session_id=session_id)
        assert jsonl_result.valid is True
        assert pg_result["valid"] is True

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
