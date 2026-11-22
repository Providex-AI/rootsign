"""The "Run on PostgreSQL" guide stays true.

`guides/postgres.mdx`: the `start-db` and `init` output it shows is the output
the CLI reference pins to real runs; the quickstart's `agent.py` runs
unchanged on the PostgreSQL backend (subprocesses pointed at the test
database), its session verifies by ID exactly as shown, a second run reuses the
agent, several processes write at once, `status` lists the tables shown, and a
database without RootSign's tables fails the session as the page says.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from rootsign.config import settings
from tests.support.docs_pages import code_block, output_after, page_text

pytestmark = pytest.mark.integration

PAGE = "guides/postgres.mdx"
ADMIN = "reference/cli-rootsign-admin.mdx"
BIN = Path(sys.executable).parent


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


def _run(env, *argv: str, cwd: Path) -> subprocess.CompletedProcess:
    # cwd keeps the repo's .env out of pydantic-settings' reach.
    return subprocess.run(list(argv), env=env, cwd=cwd, capture_output=True, text=True)


def _agent(env, tmp_path) -> str:
    (tmp_path / "agent.py").write_text(code_block("quickstart.mdx", "agent.py"))
    result = _run(env, sys.executable, "agent.py", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_the_admin_output_is_the_cli_references():
    for command in ("rootsign-admin start-db", "rootsign-admin init"):
        guide = output_after(PAGE, f"{command}\n    ```")
        reference = output_after(ADMIN, f"{command}\n```")
        assert guide == reference, command


def test_the_quickstart_runs_unchanged_and_verifies_by_id(env, tmp_path):
    session_id = _agent(env, tmp_path)
    assert re.fullmatch(r"[0-9a-f-]{36}", session_id)

    result = _run(env, str(BIN / "rootsign"), "verify", session_id, cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    (shown_id,) = re.findall(r"rootsign verify ([0-9a-f-]{36})", page_text(PAGE))
    expected = [
        line.replace(shown_id, session_id) for line in output_after(PAGE, "rootsign verify 4af")
    ]
    printed = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    assert printed == expected


async def test_a_second_run_reuses_the_agent(env, tmp_path, clean_db):
    _agent(env, tmp_path)
    _agent(env, tmp_path)

    rows = (
        await clean_db.execute(text("SELECT count(*) FROM agents WHERE name = 'invoice-agent'"))
    ).scalar_one()
    assert rows == 1


def test_several_processes_write_at_once(env, tmp_path):
    (tmp_path / "agent.py").write_text(code_block("quickstart.mdx", "agent.py"))
    procs = [
        subprocess.Popen(
            [sys.executable, "agent.py"], env=env, cwd=tmp_path, stdout=subprocess.PIPE, text=True
        )
        for _ in range(3)
    ]
    session_ids = [p.communicate()[0].strip() for p in procs]
    assert all(p.returncode == 0 for p in procs)

    for session_id in session_ids:
        result = _run(env, str(BIN / "rootsign"), "verify", session_id, cwd=tmp_path)
        assert result.returncode == 0, result.stdout


def test_status_lists_the_tables_shown(env, tmp_path):
    _agent(env, tmp_path)

    result = _run(env, str(BIN / "rootsign-admin"), "status", cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    shown = output_after(PAGE, "rootsign-admin status\n    ```")
    printed = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    assert printed[:2] == shown[:2]
    assert [r.split()[0] for r in printed[2:]] == [r.split()[0] for r in shown[2:]]
    counts = {r.split()[0]: int(r.split()[1]) for r in printed[2:]}
    assert counts["agents"] == 1 and counts["sessions"] == 1 and counts["actions"] == 2


def test_init_connects_with_database_url_sync(env, tmp_path):
    broken = {**env, "DATABASE_URL_SYNC": "postgresql+psycopg2://rootsign:pw@localhost:1/x"}

    result = _run(broken, str(BIN / "rootsign-admin"), "init", cwd=tmp_path)

    assert result.returncode == 1
    assert "DATABASE_URL_SYNC points at it" in result.stderr
    assert "| `DATABASE_URL_SYNC` | `rootsign-admin init` and `status` |" in page_text(PAGE)


def test_the_timescaledb_message():
    """The server text is what PostgreSQL 16 without TimescaleDB answers to
    `CREATE EXTENSION IF NOT EXISTS timescaledb`, captured from a plain
    `postgres:16-alpine` container. CI has no such server, so the line `init`
    prints is rebuilt from it with the same formatter `init` uses."""
    import psycopg2

    from rootsign._cli_db import database_problem

    url = "postgresql+psycopg2://audituser:secret@db.example.test:5432/auditdb"
    server = psycopg2.errors.FeatureNotSupported('extension "timescaledb" is not available')

    message = database_problem(server, url, setting="DATABASE_URL_SYNC")

    (shown,) = output_after(PAGE, "even though the hint mentions the server:")
    placeholders = {"audituser": "<user>", "db.example.test": "<host>", "auditdb": "<database>"}
    for value, placeholder in placeholders.items():
        message = message.replace(value, placeholder)
    assert f"Error: {message}" == shown
    assert (
        "CREATE EXTENSION IF NOT EXISTS timescaledb"
        in (
            Path(__file__).parents[2] / "rootsign/_migrations/versions/0001_initial_schema.py"
        ).read_text()
    )


def test_a_database_without_the_tables_fails_the_session(env, tmp_path):
    # The server's maintenance database: reachable, and without RootSign's tables.
    url = make_url(settings.TEST_DATABASE_URL).set(database="postgres")
    bare = {**env, "DATABASE_URL": url.render_as_string(hide_password=False)}
    (tmp_path / "agent.py").write_text(code_block("quickstart.mdx", "agent.py"))

    result = _run(bare, sys.executable, "agent.py", cwd=tmp_path)

    assert result.returncode == 1
    assert result.stdout == ""  # the session body did not run
    (shown,) = output_after(PAGE, "The message names the missing table:")
    parts = re.split(r"\s*\.\.\.\s*", shown)
    pattern = re.compile(".*".join(re.escape(part) for part in parts), re.DOTALL)
    error = result.stderr[result.stderr.index("rootsign.errors.RecordPersistenceError") :]
    assert pattern.fullmatch(error.strip()), error
