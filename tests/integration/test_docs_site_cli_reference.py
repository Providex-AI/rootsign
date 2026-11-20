"""The CLI reference pages' PostgreSQL rows stay true.

The DB-free half of these checks is tests/unit/test_docs_site_cli_reference.py.
This half runs the commands as a reader would, in subprocesses pointed at the
test database: an unknown session ID, a never-initialised database, `init`, and
`status`.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from typer.testing import CliRunner

from rootsign.config import settings
from rootsign.sdk.cli import app as user_app
from tests.support.docs_pages import output_after, page_text

USER = "reference/cli-rootsign.mdx"
ADMIN = "reference/cli-rootsign-admin.mdx"
BIN = Path(sys.executable).parent


@pytest.fixture
def env(tmp_path):
    return {
        **os.environ,
        "HOME": str(tmp_path),
        "ROOTSIGN_BACKEND": "jsonl",  # the commands below must ignore it
        "DATABASE_URL": settings.TEST_DATABASE_URL,
        "DATABASE_URL_SYNC": settings.TEST_DATABASE_URL_SYNC,
    }


def _run(env, tool: str, *args: str, cwd: Path) -> subprocess.CompletedProcess:
    # cwd keeps the repo's .env out of pydantic-settings' reach.
    return subprocess.run(
        [str(BIN / tool), *args], env=env, cwd=cwd, capture_output=True, text=True
    )


def _row(page: str, heading: str, startswith: str) -> re.Pattern[str]:
    text = page_text(page)
    section = text[text.index(f"\n## {heading}\n") :]
    for _, message in re.findall(r"^\| (``?)(Error: .+?)\1 \|", section, re.MULTILINE):
        if message.startswith(startswith):
            parts = re.split(r"(<[^>]+>|\.\.\.)", message)
            return re.compile(
                "".join(
                    ".+?" if p.startswith("<") else ".*" if p == "..." else re.escape(p)
                    for p in parts
                )
            )
    raise AssertionError(f"{page}: no {startswith!r} row under {heading}")


class TestRootsign:
    def test_verify_an_unknown_session_exits_3(self, env, tmp_path, clean_db):
        session_id = str(uuid4())
        result = _run(env, "rootsign", "verify", session_id, cwd=tmp_path)

        assert result.returncode == 3
        pattern = _row(USER, "rootsign verify", "Error: no session <session_id>")
        assert pattern.fullmatch(result.stderr.strip()), result.stderr

    def test_export_an_unknown_session_exits_1(self, env, tmp_path, clean_db):
        result = _run(env, "rootsign", "export", str(uuid4()), cwd=tmp_path)

        assert result.returncode == 1
        pattern = _row(USER, "rootsign export", "Error: Session <session_id> not found")
        assert pattern.fullmatch(result.stderr.strip()), result.stderr

    def test_approve_an_unknown_action_exits_1(self, env, tmp_path, clean_db):
        result = _run(env, "rootsign", "approve", str(uuid4()), cwd=tmp_path)

        assert result.returncode == 1
        pattern = _row(USER, "rootsign approve", "Error: no pending action")
        assert pattern.fullmatch(result.stderr.strip()), result.stderr

    def test_approve_list_with_nothing_waiting(self, env, tmp_path, clean_db):
        result = _run(env, "rootsign", "approve", "--list", cwd=tmp_path)

        assert result.returncode == 0
        assert f"it prints `{result.stdout.strip()}`" in page_text(USER)

    def test_an_uninitialised_database(self, monkeypatch):
        # In-process: the "never initialised" state is the test database seen
        # through an empty search_path, which only connect_args can set.
        engine = create_async_engine(
            settings.TEST_DATABASE_URL,
            poolclass=NullPool,
            connect_args={"server_settings": {"search_path": "rootsign_never_initialised"}},
        )
        monkeypatch.setattr("rootsign.sdk.cli.AsyncSessionLocal", async_sessionmaker(bind=engine))

        result = CliRunner().invoke(user_app, ["verify", str(uuid4())])

        assert result.exit_code == 3
        pattern = _row(USER, "rootsign verify", "Error: the database at <url> has no")
        assert pattern.fullmatch(result.output.strip()), result.output


class TestRootsignAdmin:
    def test_init_is_safe_to_rerun(self, env, tmp_path):
        expected = output_after(ADMIN, "rootsign-admin init\n```")
        for _ in range(2):
            result = _run(env, "rootsign-admin", "init", cwd=tmp_path)
            assert result.returncode == 0, result.stderr
            lines = result.stdout.splitlines()
            assert lines[0] == expected[0]
            assert re.fullmatch(r"Done in \d+\.\d\ds", lines[1])

    def test_status_lists_the_tables_the_page_shows(self, env, tmp_path):
        result = _run(env, "rootsign-admin", "status", cwd=tmp_path)

        assert result.returncode == 0, result.stderr
        expected = output_after(ADMIN, "Prints the number of rows in each RootSign table")
        printed = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        assert printed[:2] == expected[:2]
        assert [r.split()[0] for r in printed[2:]] == [r.split()[0] for r in expected[2:]]
        assert all(re.fullmatch(r"\w+\s+\d+", r) for r in printed[2:])

    def test_status_on_an_unreachable_database_exits_1(self, env, tmp_path):
        env = {**env, "DATABASE_URL_SYNC": "postgresql+psycopg2://rootsign:pw@localhost:1/x"}
        result = _run(env, "rootsign-admin", "status", cwd=tmp_path)

        assert result.returncode == 1
        assert result.stderr.startswith("Error: cannot use the database at ")
        assert "DATABASE_URL_SYNC" in result.stderr
