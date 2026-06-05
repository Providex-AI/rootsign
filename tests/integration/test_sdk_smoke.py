"""End-to-end SDK smoke test (Sprint 1).

Proves the plumbing works without any framework dependency:
  - SessionContext + LocalIngestClient + @rootsign.trace
  - Decorate a plain async function, call it once
  - Verify ONE Action row materialises in the DB with the expected fields

LangGraph contract tests land in Sprint 2 — this test stays as the
framework-agnostic baseline.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from rootsign.crud import action as action_crud
from rootsign.sdk.context import SessionContext
from rootsign.sdk.decorator import trace
from rootsign.sdk.hashing import compute_payload_hash

pytestmark = pytest.mark.integration


class TestSdkSmoke:
    async def test_decorated_function_creates_action_record(
        self, ingest_client, registered_agent, make_envelope_fixture
    ):
        session_id = uuid4()
        ctx = SessionContext(
            agent_id=registered_agent.agent_id,
            session_id=session_id,
        )

        # Open the session first — IngestHandler refuses ACTION_RECORDs for
        # unopened sessions.
        open_resp = await ingest_client.handle(
            make_envelope_fixture(
                "SESSION_OPEN",
                registered_agent.agent_id,
                session_id,
                {"objective": "sdk smoke"},
            )
        )
        assert open_resp.status == "accepted"

        @trace(ingest_client=ingest_client, session_context=ctx, tool_name="echo_tool")
        async def echo_tool(x: int) -> str:
            return f"result_{x}"

        result = await echo_tool(42)
        assert result == "result_42"

        # The decorator should have produced exactly one Action row.
        chain = await action_crud.get_session_chain(
            ingest_client._handler.db, session_id=session_id  # noqa: SLF001
        )
        assert len(chain) == 1
        action = chain[0]
        assert action.tool_name == "echo_tool"
        assert action.sequence_number == 1
        assert action.authorization_status == "auto_authorized"

        # Re-derive the expected input hash from outside the decorator and
        # confirm it matches what the store recorded. This is the proof that
        # the SDK and the store agree on canonical serialisation.
        expected_input = {"args": [42], "kwargs": {}}
        assert action.input_hash == compute_payload_hash(expected_input)

    async def test_decorated_function_propagates_exception_but_still_records(
        self, ingest_client, registered_agent, make_envelope_fixture
    ):
        """Failure isolation: a tool that raises must (a) reach the caller
        and (b) still produce an Action row so the audit trail shows the
        failed attempt."""
        session_id = uuid4()
        ctx = SessionContext(
            agent_id=registered_agent.agent_id,
            session_id=session_id,
        )

        await ingest_client.handle(
            make_envelope_fixture(
                "SESSION_OPEN",
                registered_agent.agent_id,
                session_id,
                {"objective": "failure-path smoke"},
            )
        )

        @trace(ingest_client=ingest_client, session_context=ctx, tool_name="exploding_tool")
        async def exploding_tool() -> None:
            raise RuntimeError("tool blew up")

        with pytest.raises(RuntimeError, match="tool blew up"):
            await exploding_tool()

        chain = await action_crud.get_session_chain(
            ingest_client._handler.db, session_id=session_id  # noqa: SLF001
        )
        assert len(chain) == 1
        assert chain[0].tool_name == "exploding_tool"
        # output_hash is None because the function raised before producing
        # a return value.
        assert chain[0].output_hash is None
