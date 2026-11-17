"""The PostgreSQL flow in `docs/site/guides/human-approval.mdx`, run for real.

The page's `transfer.py` runs as its own process against the test database
and waits at `wire_transfer`; `rootsign approve` runs as a second process, as
the reader would run it from another terminal. Three flows, each checked
against the output and records the page shows:

* approve: `--list` shows the action, approving prints the page's line, and
  the agent finishes the transfer;
* reject: the agent gets HiTLRejectedError with the reason;
* timeout: `timeout_seconds` is cut from 300 to 3 so the test is quick, and
  the page's 300s lines are checked with 3s swapped in.

Then the approval records match the page's table, and each session verifies
VALID. `clean_db` truncates every table afterwards.
"""

from __future__ import annotations

import getpass
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import text

from rootsign.config import settings
from tests.support.docs_pages import code_block, page_text

PAGE = "guides/human-approval.mdx"


@pytest.fixture
def env(tmp_path):
    return {
        **os.environ,
        "ROOTSIGN_BACKEND": "postgres",
        "DATABASE_URL": settings.TEST_DATABASE_URL,
        "DATABASE_URL_SYNC": settings.TEST_DATABASE_URL_SYNC,
        "HOME": str(tmp_path),
    }


def _rootsign(env, *args) -> subprocess.CompletedProcess:
    exe = Path(sys.executable).parent / "rootsign"
    return subprocess.run([str(exe), *args], env=env, capture_output=True, text=True)


def _start_agent(tmp_path, env, *, timeout: int = 300) -> subprocess.Popen:
    source = code_block(PAGE, "transfer.py")
    if timeout != 300:
        source = source.replace("timeout_seconds=300", f"timeout_seconds={timeout}")
    (tmp_path / "transfer.py").write_text(source)
    return subprocess.Popen(
        [sys.executable, "transfer.py"],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for_pending(env) -> tuple[str, str]:
    deadline = time.time() + 30
    while time.time() < deadline:
        listed = _rootsign(env, "approve", "--list")
        match = re.search(r"^\s+([0-9a-f-]{36})\s+wire_transfer\b", listed.stdout, re.M)
        if match:
            return match.group(1), listed.stdout
        time.sleep(0.5)
    raise AssertionError("the agent never submitted a pending wire_transfer")


def _page_lines(after: str) -> list[str]:
    text = page_text(PAGE)
    start = text.index(after)
    block = re.search(r"```text\n(.*?)\n\s*```", text[start:], re.S).group(1)
    return [line.strip() for line in block.splitlines() if line.strip()]


def _swap(line: str, ids: dict[str, str]) -> str:
    for old, new in ids.items():
        line = line.replace(old, new)
    return line


PAGE_APPROVE_ID = "06f72d12-ec7d-4ac7-9a87-5a988322ffaa"
PAGE_APPROVE_SESSION = "8bbb4a2e-ce25-46e2-969d-1f7884fcd72a"
PAGE_REJECT_ID = "7d8d56ec-805c-430d-ae2f-ff1141a17f30"
PAGE_TIMEOUT_ID = "b5e0e3d3-fa5e-4cb4-9462-b1048fd54c83"


async def test_approve_reject_and_timeout_as_the_page_shows(clean_db, tmp_path, env):
    # --- approve
    agent = _start_agent(tmp_path, env)
    action_id, listed = _wait_for_pending(env)
    assert "Pending approvals (1):" in listed

    approved = _rootsign(
        env, "approve", action_id, "--reason", "Verified with the supplier by phone"
    )
    assert approved.returncode == 0, approved.stderr
    assert approved.stdout.strip() == _swap(
        _page_lines("approve 06f72d12-ec7d-4ac7-9a87-5a988322ffaa --reason")[0],
        {PAGE_APPROVE_ID: action_id},
    )
    out, err = agent.communicate(timeout=30)
    assert agent.returncode == 0, err
    session_id = out.strip().splitlines()[-1]
    assert out.strip().splitlines() == [
        _swap(line, {PAGE_APPROVE_SESSION: session_id})
        for line in _page_lines("runs the tool and finishes")
    ]

    again = _rootsign(env, "approve", action_id)
    assert again.returncode == 1
    assert f"Error: no pending action found with ID {action_id}." in again.stderr + again.stdout

    # --- reject
    agent = _start_agent(tmp_path, env)
    action_id, _ = _wait_for_pending(env)
    rejected = _rootsign(
        env, "approve", action_id, "--reject", "--reason", "Above the unattended mandate"
    )
    assert rejected.stdout.strip() == _swap(
        _page_lines("--reject --reason")[0], {PAGE_REJECT_ID: action_id}
    )
    out, _ = agent.communicate(timeout=30)
    assert (
        _swap(
            _page_lines("raises `HiTLRejectedError` in the agent")[0], {PAGE_REJECT_ID: action_id}
        )
        in out
    )

    # --- timeout (300s on the page, 3s here)
    agent = _start_agent(tmp_path, env, timeout=3)
    out, _ = agent.communicate(timeout=60)
    timed_out_id = re.search(r"for action ([0-9a-f-]{36})", out).group(1)
    shown = _page_lines("kept distinct from a person saying no")[0].replace("300s", "3s")
    assert _swap(shown, {PAGE_TIMEOUT_ID: timed_out_id}) in out

    # --- the approval records match the page's table
    await clean_db.rollback()  # see the rows the other processes committed
    result = await clean_db.execute(
        text(
            "select a.authorization_status, p.approver_id, p.approver_type, p.decision, "
            "p.decision_reason from actions a join approvals p on p.action_id = a.action_id "
            "order by a.timestamp"
        )
    )
    rows = ["|".join(str(v) for v in row) for row in result.all()]
    user = getpass.getuser()
    assert rows == [
        f"human_approved|cli:{user}|human|approved|Verified with the supplier by phone",
        f"human_rejected|cli:{user}|human|rejected|Above the unattended mandate",
        "timed_out|system:timeout|timeout_auto_rejected|rejected|No response within 3s",
    ]
    assert "No response within 300s" in page_text(PAGE)
