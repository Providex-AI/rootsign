"""The "Use RootSign without init()" guide stays true.

`guides/without-init.mdx`: its three scripts run as a reader would, in
subprocesses pointed at the test database. register.py prints two IDs and
saves them, and a second run raises exactly the error shown. agents.py records
one session per agent with no init(), and each verifies as shown. An unknown
agent ID fails as shown. own_transaction.py stores nothing until it commits,
and a rollback leaves the "no session" error the page quotes. A held call on
that client raises HiTLUnsupportedBackendError, and the client table names
real classes.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

import rootsign
from rootsign.config import settings
from rootsign.errors import HiTLUnsupportedBackendError
from rootsign.sdk.client import LocalIngestClient
from rootsign.sdk.context import SessionContext
from rootsign.sdk.decorator import trace
from tests.support.docs_pages import code_block, output_after, page_text

pytestmark = pytest.mark.integration

PAGE = "guides/without-init.mdx"
BIN = Path(sys.executable).parent
UUID = r"[0-9a-f-]{36}"


@pytest.fixture
def env(tmp_path, clean_db):
    # `clean_db` truncates on teardown: the subprocesses commit their rows.
    return {
        **{k: v for k, v in os.environ.items() if not k.startswith("ROOTSIGN_")},
        "HOME": str(tmp_path),
        "ROOTSIGN_BACKEND": "postgres",
        "DATABASE_URL": settings.TEST_DATABASE_URL,
        "DATABASE_URL_SYNC": settings.TEST_DATABASE_URL_SYNC,
    }


def _run(env, cwd: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), env=env, cwd=cwd, capture_output=True, text=True)


def _script(cwd: Path, title: str, source: str | None = None) -> str:
    (cwd / title).write_text(source or code_block(PAGE, title))
    return title


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def test_the_scripts_do_not_call_init():
    for title in ("register.py", "agents.py", "own_transaction.py"):
        assert "rootsign.init(" not in code_block(PAGE, title), title


def test_register_once_then_record_per_agent(env, tmp_path):
    first = _run(env, tmp_path, sys.executable, _script(tmp_path, "register.py"))
    assert first.returncode == 0, first.stderr
    ids = json.loads((tmp_path / "agents.json").read_text())
    assert json.loads(first.stdout) == ids
    assert list(ids) == ["billing-agent", "refund-agent"]
    shown_ids = json.loads("\n".join(output_after(PAGE, "python register.py")))
    assert list(shown_ids) == list(ids)

    again = _run(env, tmp_path, sys.executable, "register.py")
    assert again.returncode == 1
    (shown,) = output_after(PAGE, "It stops with an error that gives the ID you already have:")
    expected = shown.replace(shown_ids["billing-agent"], ids["billing-agent"])
    assert again.stderr.strip().splitlines()[-1] == expected

    recorded = _run(env, tmp_path, sys.executable, _script(tmp_path, "agents.py"))
    assert recorded.returncode == 0, recorded.stderr
    printed = _lines(recorded.stdout)
    shown_out = output_after(PAGE, "python agents.py")
    assert [line.split()[0] for line in printed] == [line.split()[0] for line in shown_out]
    sessions = [line.split()[1] for line in printed]
    assert all(re.fullmatch(UUID, s) for s in sessions)

    shown_session = shown_out[0].split()[1]
    for session_id in sessions:
        verify = _run(env, tmp_path, str(BIN / "rootsign"), "verify", session_id)
        assert verify.returncode == 0, verify.stdout + verify.stderr
        expected = [
            line.replace(shown_session, session_id)
            for line in output_after(PAGE, f"rootsign verify {shown_session}")
        ]
        assert _lines(verify.stdout) == expected


def test_an_unknown_agent_id_fails_as_shown(env, tmp_path):
    unknown = str(uuid4())
    (tmp_path / "agents.json").write_text(
        json.dumps({"billing-agent": unknown, "refund-agent": unknown})
    )

    result = _run(env, tmp_path, sys.executable, _script(tmp_path, "agents.py"))

    assert result.returncode == 1
    assert result.stdout == ""
    (shown,) = output_after(PAGE, "is refused, and the session does not open:")
    assert result.stderr.strip().splitlines()[-1] == shown.replace("<agent_id>", unknown)


def test_your_own_transaction_commits_or_rolls_back(env, tmp_path):
    assert _run(env, tmp_path, sys.executable, _script(tmp_path, "register.py")).returncode == 0
    source = code_block(PAGE, "own_transaction.py")

    committed = _run(env, tmp_path, sys.executable, _script(tmp_path, "own_transaction.py"))
    assert committed.returncode == 0, committed.stderr
    verify = _run(env, tmp_path, str(BIN / "rootsign"), "verify", committed.stdout.strip())
    assert verify.returncode == 0
    assert verify.stdout.startswith("VALID")

    rollback = source.replace(
        "await db.commit()  # nothing is stored until you commit", "await db.rollback()"
    )
    assert rollback != source
    rolled = _run(env, tmp_path, sys.executable, _script(tmp_path, "rollback.py", rollback))
    assert rolled.returncode == 0, rolled.stderr
    session_id = rolled.stdout.strip()
    verify = _run(env, tmp_path, str(BIN / "rootsign"), "verify", session_id)
    assert verify.returncode == 3
    assert f"Error: no session {session_id} in the database" in verify.stdout + verify.stderr
    assert "prints `Error: no session <session_id> in the database` and exits `3`" in page_text(
        PAGE
    )


async def test_a_held_call_on_your_own_transaction_is_refused(clean_db, seeded_agent):
    client = LocalIngestClient(db=clean_db)

    @trace(
        ingest_client=client,
        session_context=SessionContext(agent_id=seeded_agent.agent_id),
        require_approval=True,
    )
    async def wire_transfer(amount: float) -> str:
        return "sent"

    with pytest.raises(HiTLUnsupportedBackendError, match="ManagedLocalIngestClient"):
        await wire_transfer(amount=1.0)


def test_the_client_table_names_real_classes():
    names = re.findall(r"^\| `(\w+)\(", page_text(PAGE), re.M)
    assert names == [
        "ManagedLocalIngestClient",
        "LocalIngestClient",
        "JsonlIngestClient",
        "HttpIngestClient",
    ]
    for name in names:
        assert isinstance(getattr(rootsign, name), type), name
