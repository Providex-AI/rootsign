"""Payload→hash binding verification (pre-Phase-2 audit #4).

`self_hash` deliberately excludes `input_redacted`/`output_redacted`
(ADR-001), so the hash chain proves the *hashes* are intact but not that the
stored human-readable payloads still match them. Without a binding check,
DB write access could rewrite a redacted payload without tripping TAMPERED.

verify_chain (DB) and verify_session_local (JSONL) now re-derive
`compute_payload_hash(input_redacted)` and compare it to the `input_hash`
the chain protects. These tests prove:

  * a genuine chain with real redacted payloads verifies VALID — i.e. the
    JSONB round-trip does NOT cause a false payload_hash mismatch (the whole
    risk of this check);
  * rewriting a stored redacted payload flips the verdict to TAMPERED with a
    distinct payload_hash error, even though self_hash still verifies.

Real PostgreSQL only for the DB path.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select, update

from rootsign.crud import action as action_crud
from rootsign.ingest import IdempotencyStore, IngestHandler
from rootsign.models.action import Action
from rootsign.sdk.chain import verify_session_local
from rootsign.sdk.hashing import compute_payload_hash

# seeded_agent / clean_db shared from tests/conftest.py.


def _envelope(*, event_type: str, agent_id: UUID, session_id: UUID, payload: dict) -> dict:
    return {
        "schema_version": "1.0",
        "sdk_version": "0.1.0",
        "event_type": event_type,
        "event_id": str(uuid4()),
        "emitted_at": datetime.now(timezone.utc).isoformat(),
        "agent_id": str(agent_id),
        "session_id": str(session_id),
        "payload": payload,
    }


async def _seed_action_with_real_payloads(*, clean_db, agent_id: UUID) -> tuple[UUID, UUID]:
    """Open a session and emit one ACTION_RECORD whose input_hash/output_hash
    are the genuine compute_payload_hash of the stored redacted payloads —
    exactly what the SDK decorator produces. Returns (session_id, action_id)."""
    handler = IngestHandler(db=clean_db, idempotency=IdempotencyStore())
    session_id = uuid4()
    await handler.handle(
        _envelope(
            event_type="SESSION_OPEN",
            agent_id=agent_id,
            session_id=session_id,
            payload={"objective": "payload-binding test"},
        )
    )
    input_redacted = {"args": ["[REDACTED]", 42], "kwargs": {"note": "hello"}}
    output_redacted = {"result": "ok", "count": 3}
    response = await handler.handle(
        _envelope(
            event_type="ACTION_RECORD",
            agent_id=agent_id,
            session_id=session_id,
            payload={
                "tool_name": "send_invoice",
                "input_hash": compute_payload_hash(input_redacted),
                "output_hash": compute_payload_hash(output_redacted),
                "input_redacted": input_redacted,
                "output_redacted": output_redacted,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "authorization_status": "auto_authorized",
            },
        )
    )
    await clean_db.commit()
    assert response.entity_id is not None
    return session_id, response.entity_id


class TestVerifyChainBinding:
    async def test_genuine_payloads_verify_valid(self, clean_db, seeded_agent):
        """A real chain with matching redacted payloads verifies VALID —
        proving the JSONB round-trip does not break the binding check."""
        session_id, _ = await _seed_action_with_real_payloads(
            clean_db=clean_db, agent_id=seeded_agent.agent_id
        )
        result = await action_crud.verify_chain(clean_db, session_id=session_id)
        assert result["valid"] is True, result

    async def test_tampered_input_redacted_is_detected(self, clean_db, seeded_agent):
        """Rewriting input_redacted in the DB flips the verdict to TAMPERED
        with a payload_hash error, even though self_hash still verifies."""
        session_id, action_id = await _seed_action_with_real_payloads(
            clean_db=clean_db, agent_id=seeded_agent.agent_id
        )
        # Rewrite the human-readable evidence without touching input_hash.
        await clean_db.execute(
            update(Action)
            .where(Action.action_id == action_id)
            .values(input_redacted={"args": ["totally different"], "kwargs": {}})
        )
        await clean_db.commit()
        clean_db.expire_all()  # force verify_chain to reload the tampered row

        result = await action_crud.verify_chain(clean_db, session_id=session_id)
        assert result["valid"] is False
        assert "payload_hash mismatch" in result["error"]
        assert "input_redacted" in result["error"]

    async def test_tampered_output_redacted_is_detected(self, clean_db, seeded_agent):
        session_id, action_id = await _seed_action_with_real_payloads(
            clean_db=clean_db, agent_id=seeded_agent.agent_id
        )
        await clean_db.execute(
            update(Action)
            .where(Action.action_id == action_id)
            .values(output_redacted={"result": "SILENTLY CHANGED"})
        )
        await clean_db.commit()
        clean_db.expire_all()

        result = await action_crud.verify_chain(clean_db, session_id=session_id)
        assert result["valid"] is False
        assert "payload_hash mismatch" in result["error"]
        assert "output_redacted" in result["error"]

    async def test_selfhash_still_intact_after_payload_tamper(self, clean_db, seeded_agent):
        """The point of the fix: self_hash alone would NOT catch this — prove
        the tampered row's self_hash still matches its canonical fields."""
        from rootsign.hashing import compute_action_self_hash

        session_id, action_id = await _seed_action_with_real_payloads(
            clean_db=clean_db, agent_id=seeded_agent.agent_id
        )
        await clean_db.execute(
            update(Action)
            .where(Action.action_id == action_id)
            .values(input_redacted={"args": ["evidence rewritten"], "kwargs": {}})
        )
        await clean_db.commit()
        clean_db.expire_all()

        action = (
            await clean_db.execute(select(Action).where(Action.action_id == action_id))
        ).scalar_one()
        recomputed_self = compute_action_self_hash(
            {
                "action_id": action.action_id,
                "session_id": action.session_id,
                "tool_name": action.tool_name,
                "input_hash": action.input_hash,
                "output_hash": action.output_hash,
                "prev_action_hash": action.prev_action_hash,
                "timestamp": action.timestamp,
                "sequence_number": action.sequence_number,
            }
        )
        # self_hash is untouched by the payload rewrite — the binding check is
        # the only thing standing between a rewritten payload and a VALID verdict.
        assert recomputed_self == action.self_hash


class TestVerifyLocalBinding:
    def _record(self, *, input_redacted, output_redacted=None):
        from rootsign.hashing import compute_action_self_hash

        rec = {
            "action_id": str(uuid4()),
            "session_id": str(uuid4()),
            "tool_name": "send_invoice",
            "input_hash": compute_payload_hash(input_redacted),
            "output_hash": compute_payload_hash(output_redacted),
            "prev_action_hash": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sequence_number": 1,
            "input_redacted": input_redacted,
            "output_redacted": output_redacted,
        }
        rec["self_hash"] = compute_action_self_hash(rec)
        return rec

    def test_genuine_local_export_verifies_valid(self, tmp_path):
        rec = self._record(input_redacted={"args": [1, 2], "kwargs": {}})
        p = tmp_path / "chain.jsonl"
        p.write_text(json.dumps(rec) + "\n")
        result = verify_session_local(str(p))
        assert result.valid is True, result.error

    def test_tampered_local_input_redacted_detected(self, tmp_path):
        rec = self._record(input_redacted={"args": [1, 2], "kwargs": {}})
        rec["input_redacted"] = {"args": ["tampered"], "kwargs": {}}  # hash unchanged
        p = tmp_path / "chain.jsonl"
        p.write_text(json.dumps(rec) + "\n")
        result = verify_session_local(str(p))
        assert result.valid is False
        assert "payload_hash mismatch" in (result.error or "")
        assert "input_redacted" in (result.error or "")
