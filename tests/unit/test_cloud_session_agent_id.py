"""On the cloud backend, `rootsign.session()` takes the agent's ID.

RootSign cannot register an agent with the hosted backend: the backend issues
agent IDs. `init()` + `session()` on cloud used to fail with
"rootsign.init() does not support ROOTSIGN_BACKEND='cloud'. Use 'jsonl'
(default) or 'postgres'.", which read as "cloud does not work with init()",
while `init()` + `session(agent_id=...)` worked all along. The error now names
that form.

Each case runs as its own Python process against a closed port, so records
fail over to the spool and nothing leaves the machine.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

AGENT = textwrap.dedent(
    """
    import asyncio
    from uuid import UUID

    import rootsign

    rootsign.init(agent="cloud-agent")


    @rootsign.trace()
    async def lookup(q: str) -> str:
        return q.upper()


    async def main() -> None:
        async with rootsign.session({session_args}) as ctx:
            print(await lookup(q="x"))
        print(ctx.session_id)


    asyncio.run(main())
    """
)


def _run(tmp_path, session_args: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "agent.py").write_text(AGENT.format(session_args=session_args))
    env = {
        **os.environ,
        "ROOTSIGN_BACKEND": "cloud",
        "ROOTSIGN_API_KEY": "test-key",
        "ROOTSIGN_CLOUD_URL": "http://127.0.0.1:1/v1",
        "ROOTSIGN_HTTP_MAX_RETRIES": "1",
        "ROOTSIGN_DATA_DIR": str(tmp_path / "data"),
    }
    return subprocess.run(
        [sys.executable, "agent.py"], cwd=tmp_path, env=env, capture_output=True, text=True
    )


def test_a_session_without_an_agent_id_names_the_form_that_works(tmp_path):
    result = _run(tmp_path, "")

    assert result.returncode == 1
    last = result.stderr.strip().splitlines()[-1]
    assert last.startswith("ValueError: ")
    assert "rootsign.session(agent_id=...)" in last
    assert "hosted backend issues agent IDs" in last
    assert not (tmp_path / "data").exists()  # nothing was recorded


def test_a_session_with_an_agent_id_records_on_cloud(tmp_path):
    result = _run(tmp_path, 'agent_id=UUID("6f1c2d4e-8a9b-4c3d-9e2f-1a2b3c4d5e6f")')

    assert result.returncode == 0, result.stderr
    printed, session_id = result.stdout.split()
    assert printed == "X"
    assert (tmp_path / "data" / "spool" / "sessions" / f"{session_id}.jsonl").exists()
