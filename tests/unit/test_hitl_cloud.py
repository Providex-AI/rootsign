"""`require_approval=True` on the cloud transport fails clearly, before anything runs.

Approvals on the cloud transport will come from the hosted backend, which is
not live yet. The held call used to fall through to the PostgreSQL poll loop:
it sent the pending action, then polled a local database at `DATABASE_URL`
that a cloud user never configured, ending in a raw `OSError` (nothing
listening) or `ActionNotFoundError` (a database without the action). The tool
never ran, which was right, but nothing said why. Now it raises
`HiTLUnsupportedBackendError`, like the JSONL backend without a terminal,
before sending anything or touching a database.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from uuid import uuid4

import httpx
import pytest

from rootsign.errors import HiTLUnsupportedBackendError
from rootsign.sdk.buffered_client import BufferedIngestClient
from rootsign.sdk.client import HttpIngestClient
from rootsign.sdk.context import SessionContext
from rootsign.sdk.decorator import trace


def _cloud_client(requests: list) -> HttpIngestClient:
    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503)

    return HttpIngestClient(
        "https://ingest.example.test/v1", "test-key", transport=httpx.MockTransport(respond)
    )


@pytest.mark.parametrize("buffered", [False, True])
async def test_a_held_call_on_cloud_raises_before_anything_runs(tmp_path, buffered):
    requests: list = []
    client = _cloud_client(requests)
    if buffered:
        client = BufferedIngestClient(client, flush_interval_seconds=999)
    ran = []

    @trace(
        ingest_client=client,
        session_context=SessionContext(agent_id=uuid4()),
        require_approval=True,
        timeout_seconds=1,
    )
    async def wire_transfer(amount: float) -> str:
        ran.append(amount)
        return "sent"

    with pytest.raises(HiTLUnsupportedBackendError, match="cloud transport") as raised:
        await wire_transfer(amount=10.0)

    assert ran == []
    assert requests == []
    assert raised.value.tool_name == "wire_transfer"
    assert "ROOTSIGN_BACKEND=postgres" in str(raised.value)


def test_the_jsonl_message_is_unchanged():
    assert str(HiTLUnsupportedBackendError("wire_transfer")) == (
        "HiTL not available for tool 'wire_transfer' on this backend. "
        + HiTLUnsupportedBackendError.FIX_HINT
    )


AGENT = textwrap.dedent(
    """
    import asyncio
    from uuid import UUID

    import rootsign

    rootsign.init(agent="payments-agent")


    @rootsign.trace(require_approval=True, timeout_seconds=2)
    async def wire_transfer(account: str, amount: float) -> str:
        return "transferred"


    async def main() -> None:
        async with rootsign.session(agent_id=UUID("6f1c2d4e-8a9b-4c3d-9e2f-1a2b3c4d5e6f")):
            try:
                await wire_transfer(account="GB29", amount=10.0)
            except Exception as error:
                print(type(error).__name__)


    asyncio.run(main())
    """
)


def test_a_cloud_user_without_a_local_database_gets_the_clear_error(tmp_path):
    (tmp_path / "agent.py").write_text(AGENT)
    env = {
        **os.environ,
        "ROOTSIGN_BACKEND": "cloud",
        "ROOTSIGN_API_KEY": "test-key",
        "ROOTSIGN_CLOUD_URL": "http://127.0.0.1:1/v1",
        "ROOTSIGN_HTTP_MAX_RETRIES": "1",
        "ROOTSIGN_DATA_DIR": str(tmp_path / "data"),
        # What a cloud user has: no database. main polled this and raised OSError.
        "DATABASE_URL": "postgresql+asyncpg://rootsign:rootsign@127.0.0.1:1/none",
        "DATABASE_URL_SYNC": "postgresql+psycopg2://rootsign:rootsign@127.0.0.1:1/none",
    }
    result = subprocess.run(
        [sys.executable, "agent.py"], cwd=tmp_path, env=env, capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "HiTLUnsupportedBackendError"
