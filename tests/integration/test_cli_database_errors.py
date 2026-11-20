"""The CLIs when the database cannot be used.

An unreachable server, a database that does not exist, or a schema nobody ran
`rootsign-admin init` on used to escape as a long traceback. On
`rootsign verify` it also exited 1, which is TAMPERED's code, so a cron job read
"the database is down" as "the records were altered". `verify` now exits 3
(nothing was verified) and every DB-backed command prints one line naming the
database, with the password hidden.

Every case runs against a real server state rather than a mocked driver: a
closed port, a database name that does not exist, and the real test database
seen through an empty `search_path`, which is what a never-initialised
database looks like to a query.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from typer.testing import CliRunner

from rootsign.cli import app as admin_app
from rootsign.config import settings
from rootsign.sdk.cli import EXIT_CANNOT_VERIFY, app

runner = CliRunner()

#: Port 1 (tcpmux) is never a PostgreSQL server; the connection is refused at once.
_CLOSED_PORT = "postgresql+asyncpg://rootsign:s3cret-pw@localhost:1/rootsign_nowhere"


def _factory(url: str, **connect_args):
    engine = create_async_engine(url, poolclass=NullPool, connect_args=connect_args)
    return async_sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def unreachable(monkeypatch):
    monkeypatch.setattr("rootsign.sdk.cli.AsyncSessionLocal", _factory(_CLOSED_PORT))


@pytest.fixture
def missing_database(monkeypatch):
    url = make_url(settings.TEST_DATABASE_URL).set(database="rootsign_no_such_database")
    monkeypatch.setattr(
        "rootsign.sdk.cli.AsyncSessionLocal", _factory(url.render_as_string(hide_password=False))
    )


@pytest.fixture
def uninitialised(monkeypatch):
    # A schema that does not exist on the search path: every RootSign table is
    # "relation does not exist", exactly as before `rootsign-admin init`.
    factory = _factory(
        settings.TEST_DATABASE_URL,
        server_settings={"search_path": "rootsign_never_initialised"},
    )
    monkeypatch.setattr("rootsign.sdk.cli.AsyncSessionLocal", factory)


def _one_clean_line(result) -> str:
    # CliRunner swallows an unhandled exception into `result.exception`; a
    # clean exit leaves a SystemExit there instead.
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    lines = result.output.strip().splitlines()
    assert len(lines) == 1, result.output
    assert lines[0].startswith("Error: ")
    return lines[0]


class TestVerify:
    def test_unreachable_database_exits_3_not_tampered(self, unreachable):
        result = runner.invoke(app, ["verify", str(uuid4())])

        assert result.exit_code == EXIT_CANNOT_VERIFY
        line = _one_clean_line(result)
        assert "cannot use the database at postgresql+asyncpg://rootsign:***@localhost:1/" in line
        assert "Connection refused" in line or "Connect call failed" in line
        assert "s3cret-pw" not in result.output
        assert "TAMPERED" not in result.output

    def test_missing_database_exits_3(self, missing_database):
        result = runner.invoke(app, ["verify", str(uuid4())])

        assert result.exit_code == EXIT_CANNOT_VERIFY
        assert 'database "rootsign_no_such_database" does not exist' in _one_clean_line(result)

    def test_uninitialised_database_points_at_init(self, uninitialised):
        result = runner.invoke(app, ["verify", str(uuid4())])

        assert result.exit_code == EXIT_CANNOT_VERIFY
        line = _one_clean_line(result)
        assert "has no RootSign tables" in line
        assert "rootsign-admin init" in line


class TestExportAndApprove:
    """Exit 1 is already these commands' error code; the fix is the output."""

    @pytest.mark.parametrize(
        "args",
        [
            pytest.param(["export", str(uuid4())], id="export"),
            pytest.param(["approve", "--list"], id="approve-list"),
            pytest.param(["approve", str(uuid4())], id="approve-id"),
        ],
    )
    def test_unreachable_database_is_one_line(self, unreachable, args):
        result = runner.invoke(app, args)

        assert result.exit_code == 1
        assert "cannot use the database at" in _one_clean_line(result)

    def test_uninitialised_database_on_approve_points_at_init(self, uninitialised):
        result = runner.invoke(app, ["approve", "--list"])

        assert result.exit_code == 1
        assert "rootsign-admin init" in _one_clean_line(result)

    def test_a_missing_session_still_reads_as_not_found(self, clean_db, monkeypatch):
        # The new handler must not swallow the errors the commands already
        # report: an unknown session on the real test database.
        monkeypatch.setattr(
            "rootsign.sdk.cli.AsyncSessionLocal", _factory(settings.TEST_DATABASE_URL)
        )
        session_id = uuid4()

        result = runner.invoke(app, ["export", str(session_id)])

        assert result.exit_code == 1
        assert f"Session {session_id} not found" in _one_clean_line(result)


class TestAdmin:
    @pytest.fixture
    def closed_port_sync(self, monkeypatch):
        monkeypatch.setattr(
            "rootsign.cli.settings.DATABASE_URL_SYNC",
            "postgresql+psycopg2://rootsign:s3cret-pw@localhost:1/rootsign_nowhere",
        )

    def test_status_unreachable_is_one_line(self, closed_port_sync):
        result = runner.invoke(admin_app, ["status"])

        assert result.exit_code == 1
        line = _one_clean_line(result)
        assert "postgresql+psycopg2://rootsign:***@localhost:1/rootsign_nowhere" in line
        assert "DATABASE_URL_SYNC" in line
        assert "s3cret-pw" not in result.output

    def test_init_unreachable_is_one_error_line(self, closed_port_sync):
        result = runner.invoke(admin_app, ["init"])

        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit), repr(result.exception)
        errors = [line for line in result.output.splitlines() if line.startswith("Error: ")]
        assert len(errors) == 1, result.output
        assert "cannot use the database at" in errors[0]
        assert "s3cret-pw" not in result.output
