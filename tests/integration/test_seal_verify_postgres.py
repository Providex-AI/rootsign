"""`verify_session` checks the seal PostgreSQL stored at close (ADR-016).

The store's own seal is checked against its rows through the same reader
that wrote it. When the store kept a differing client seal beside its own
(Decision 4), verify reports the disagreement: INCOMPLETE when the client
counted more actions than the store holds, TAMPERED otherwise.
"""

from __future__ import annotations

from uuid import uuid4

from rootsign.ingest import IdempotencyStore, IngestHandler
from rootsign.models.action import Action
from rootsign.sdk.chain import verify_session
from rootsign.seal import SealOutcome
from rootsign.verdict import Verdict
from tests.conftest import make_envelope
from tests.integration.test_seal_postgres import _ok, _session_with_side_records


async def _sealed(clean_db, agent_id, **kwargs):
    handler = IngestHandler(db=clean_db, idempotency=IdempotencyStore())
    session_id = await _session_with_side_records(handler, agent_id, **kwargs)
    await clean_db.commit()
    return session_id


async def test_a_sealed_session_verifies_sealed(clean_db, seeded_agent):
    result = await verify_session(await _sealed(clean_db, seeded_agent.agent_id), clean_db)
    assert result.verdict is Verdict.VALID
    assert result.sealed and result.seal.detail == "sealed at record 2"


async def test_a_row_edited_after_close_is_tampered(clean_db, seeded_agent):
    session_id = await _sealed(clean_db, seeded_agent.agent_id)
    await clean_db.execute(
        Action.__table__.update()
        .where(Action.session_id == session_id, Action.tool_name == "wire_transfer")
        .values(authorization_status="human_rejected")
    )
    await clean_db.commit()

    result = await verify_session(session_id, clean_db)
    assert result.verdict is Verdict.TAMPERED
    assert result.seal.outcome is SealOutcome.SIDE_EDITED
    assert result.summary.startswith("TAMPERED — the session's seal does not match its records")


async def test_a_client_that_counted_more_reads_incomplete(clean_db, seeded_agent):
    """Records the client sealed and the store never got: lost on the way."""
    theirs = {"version": 1, "final_sequence": 5, "seal_hash": "f" * 64}
    close = {"status": "completed", "metadata": {"total_actions": 2}, "seal": theirs}
    session_id = await _sealed(clean_db, seeded_agent.agent_id, close=close)

    result = await verify_session(session_id, clean_db)
    assert result.verdict is Verdict.INCOMPLETE
    assert result.error == "the client recorded 5 action(s); the store holds 2"
    assert result.missing_ranges == [(3, 5)]


async def test_a_client_seal_that_differs_otherwise_reads_tampered(clean_db, seeded_agent):
    theirs = {"version": 1, "final_sequence": 2, "seal_hash": "f" * 64}
    close = {"status": "completed", "metadata": {"total_actions": 2}, "seal": theirs}
    session_id = await _sealed(clean_db, seeded_agent.agent_id, close=close)

    result = await verify_session(session_id, clean_db)
    assert result.verdict is Verdict.TAMPERED
    assert result.seal.outcome is SealOutcome.CLIENT_DIFFERS


async def test_an_open_session_is_unsealed_and_require_seal_says_so(clean_db, seeded_agent):
    handler = IngestHandler(db=clean_db, idempotency=IdempotencyStore())
    session_id = uuid4()
    await _ok(
        handler,
        make_envelope("SESSION_OPEN", seeded_agent.agent_id, session_id, {"objective": "x"}),
    )
    await clean_db.commit()

    relaxed = await verify_session(session_id, clean_db)
    assert relaxed.verdict is Verdict.VALID and relaxed.seal.outcome is SealOutcome.UNSEALED

    strict = await verify_session(session_id, clean_db, require_seal=True)
    assert strict.verdict is Verdict.INCOMPLETE
    assert strict.error == "not sealed: its end cannot be verified"
