"""`rootsign._cli_db.database_problem`: which errors it describes and how.

The integration tests (tests/integration/test_cli_database_errors.py) drive the
CLIs against real refused, missing and uninitialised databases. These cover the
message shapes those servers don't produce on demand.
"""

from __future__ import annotations

import errno
import os
import socket

from rootsign._cli_db import database_problem

URL = "postgresql+asyncpg://rootsign:s3cret@db.internal:5432/rootsign"


def test_a_refused_connection_names_the_masked_url_and_the_setting():
    message = database_problem(ConnectionRefusedError(errno.ECONNREFUSED, "refused"), URL)

    assert message == (
        "cannot use the database at postgresql+asyncpg://rootsign:***@db.internal:5432/rootsign: "
        "Connection refused. Check that it is running and that DATABASE_URL points at it."
    )


def test_asyncio_multiple_exceptions_reads_as_the_first_errno():
    exc = OSError(
        "Multiple exceptions: [Errno 61] Connect call failed ('::1', 1, 0, 0), "
        "[Errno 61] Connect call failed ('127.0.0.1', 1)"
    )

    assert f": {os.strerror(61)}." in database_problem(exc, URL)


def test_an_unknown_host_uses_the_resolver_message():
    exc = socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided, or not known")

    assert ": nodename nor servname provided, or not known." in database_problem(exc, URL)


def test_the_setting_name_is_configurable():
    message = database_problem(OSError(errno.ECONNREFUSED, "x"), URL, setting="DATABASE_URL_SYNC")

    assert message.endswith("Check that it is running and that DATABASE_URL_SYNC points at it.")


def test_an_unparseable_url_still_describes_the_error():
    message = database_problem(OSError(errno.ECONNREFUSED, "x"), "not a url")

    assert message.startswith("cannot use the database at the configured database:")


def test_other_exceptions_are_not_database_problems():
    assert database_problem(ValueError("bug"), URL) is None
    assert database_problem(LookupError("Session not found"), URL) is None
    assert database_problem(RuntimeError("typer.Exit is a RuntimeError"), URL) is None
