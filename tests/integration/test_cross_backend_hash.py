"""T2.8 — cross-backend hash contract (ADR-011, the sprint's cardinal test).

Drives the *same* scripted session through both backends and proves they share
one hash formula:

  * The JSONL client's stored `self_hash` equals `compute_action_self_hash`
    recomputed from the record's canonical fields — i.e. the client assembles
    the canonical input exactly as the frozen spec expects (no second formula).
  * A session driven through JsonlIngestClient verifies VALID via
    `verify_session_local`, AND the same session driven through the Postgres
    store verifies VALID via `action_crud.verify_chain`.
  * Tampering a JSONL field flips `verify` to TAMPERED at the right sequence.

Note on "byte-identical": each backend mints its own `action_id` (`uuid4()`),
so the two chains' hashes are not literally equal action-for-action — the
guarantee is that both feed the *same* canonical fields to the *same* frozen
formula, which both verifiers then reproduce. `seeded_agent` (committed) for
the Postgres side (Flag 3).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

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
            for ln in open(client._session_path(str(sid)))
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

    async def test_jsonl_tamper_flips_to_tampered(self, tmp_path):
        client = JsonlIngestClient(data_dir=tmp_path)
        sid = uuid4()
        for e in _scripted_envelopes(uuid4(), sid):
            await client.handle(e)
        path = str(client._session_path(str(sid)))
        lines = open(path).read().splitlines()
        for i, ln in enumerate(lines):
            rec = json.loads(ln)
            if rec.get("event_type") == "ACTION_RECORD" and rec["sequence_number"] == 2:
                rec["input_hash"] = "f" * 64  # break the canonical input at seq 2
                lines[i] = json.dumps(rec)
                break
        open(path, "w").write("\n".join(lines) + "\n")
        result = verify_session_local(path)
        assert result.valid is False
        assert result.first_invalid_sequence == 2
