"""`rootsign.session()` fails closed, as a RootSignError, when it cannot open.

A session that cannot be opened (the agent cannot be registered, or its
SESSION_OPEN cannot be written) used to raise whatever the storage layer
raised: `PermissionError` from a JSONL data folder that is not writable,
`OSError` from a PostgreSQL server that is down. `except RootSignError` did
not catch it, and the Errors page did not list it.

Failing open is not an option here: a record lost mid-session leaves a gap
that `verify` reports as INCOMPLETE, but a session that never opened leaves no
file and no evidence that anything was missed. So the body still does not run;
what changes is that the error is a `RecordPersistenceError`, chained from the
original, saying the session was not opened.

Each case runs as its own process so `rootsign.init()` and the backend
settings start clean, the way they do for a user.
"""

from __future__ import annotations

import os
import textwrap

import pytest

from tests.support.docs_pages import run_script

needs_permissions = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root writes to read-only folders, so the failure cannot be simulated",
)

FACADE = textwrap.dedent(
    """
    import asyncio
    import rootsign

    rootsign.init(agent="open-check")
    ran = []


    async def main() -> None:
        try:
            async with rootsign.session(objective="x"):
                ran.append(True)
        except rootsign.RootSignError as error:
            cause = error.__cause__
            print(type(error).__name__, "|", type(cause).__name__ if cause else None)
            print(error)
        print("body ran:", bool(ran))


    asyncio.run(main())
    """
)


@needs_permissions
def test_an_unwritable_data_folder(tmp_path):
    data = tmp_path / "data"
    data.mkdir(mode=0o500)
    run = run_script(FACADE, tmp_path, extra_env={"ROOTSIGN_DATA_DIR": str(data)})

    first, message, body = run.stdout.splitlines()
    assert first == "RecordPersistenceError | PermissionError"
    assert message.startswith("session could not be opened (PermissionError:")
    assert message.endswith("Nothing was recorded, and the session body did not run.")
    assert body == "body ran: False"


@needs_permissions
def test_an_unwritable_sessions_folder(tmp_path):
    data = tmp_path / "data"
    (data / "sessions").mkdir(parents=True)
    (data / "sessions").chmod(0o500)
    run = run_script(FACADE, tmp_path, extra_env={"ROOTSIGN_DATA_DIR": str(data)})

    first, _, body = run.stdout.splitlines()
    assert first == "RecordPersistenceError | PermissionError"
    assert body == "body ran: False"


@needs_permissions
def test_the_explicit_form(tmp_path):
    """agent_id= and client= given: only the SESSION_OPEN write can fail."""
    source = textwrap.dedent(
        f"""
        import asyncio
        from uuid import uuid4

        import rootsign
        from rootsign import JsonlIngestClient


        async def main() -> None:
            client = JsonlIngestClient(data_dir={str(tmp_path / "data")!r})
            try:
                async with rootsign.session(agent_id=uuid4(), client=client):
                    print("body ran")
            except rootsign.RootSignError as error:
                print(type(error).__name__, "|", type(error.__cause__).__name__)


        asyncio.run(main())
        """
    )
    (tmp_path / "data" / "sessions").mkdir(parents=True)
    (tmp_path / "data" / "sessions").chmod(0o500)

    run = run_script(source, tmp_path)

    assert run.stdout.splitlines() == ["RecordPersistenceError | PermissionError"]


def test_postgres_with_the_database_down(tmp_path):
    run = run_script(
        FACADE,
        tmp_path,
        extra_env={
            "ROOTSIGN_BACKEND": "postgres",
            # Port 1 is never a PostgreSQL server: refused at once.
            "DATABASE_URL": "postgresql+asyncpg://rootsign:rootsign@localhost:1/rootsign_dev",
        },
    )

    first, message, body = run.stdout.splitlines()
    assert first == "RecordPersistenceError | OSError"
    assert message.startswith("session could not be opened (OSError:")
    assert body == "body ran: False"


def test_setup_mistakes_keep_their_own_errors(tmp_path):
    """Not a storage failure: no init() and no explicit agent/client."""
    source = textwrap.dedent(
        """
        import asyncio
        import rootsign


        async def main() -> None:
            try:
                async with rootsign.session():
                    pass
            except rootsign.RootSignError as error:
                print(type(error).__name__)


        asyncio.run(main())
        """
    )
    assert run_script(source, tmp_path).stdout.strip() == "RootSignNotInitializedError"
