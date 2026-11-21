"""One-line errors for CLI commands whose database cannot be used.

Shared by `rootsign` (verify, export, approve) and `rootsign-admin` (init,
status). An unreachable server, a database name that does not exist, a refused
login, or a schema nobody ran `rootsign-admin init` on used to escape as a long
traceback; on `rootsign verify` it also exited 1, TAMPERED's code. Each command
now prints the line built here and exits with its own error code.

Nothing here imports the DB stack at module level: the CLIs import this on a
bare install too, and only call it after a database attempt has already been
made (so the `postgres` extra is present by then).
"""

from __future__ import annotations

import os
import re
import socket
from importlib import import_module
from typing import Any

#: PostgreSQL's SQLSTATE for "relation does not exist": the tables were never
#: created, so the fix is `rootsign-admin init`, not the network.
_UNDEFINED_TABLE = "42P01"

#: The driver errors that mean "this database cannot be used", by module.
#: `sqlalchemy.exc.DBAPIError` wraps either driver's errors. SQLAlchemy's
#: other errors (a bad query, a misused session) are bugs and keep raising.
_DRIVER_ERRORS = (
    ("sqlalchemy.exc", "DBAPIError"),
    ("asyncpg", "PostgresError"),
    ("psycopg2", "Error"),
)


def database_problem(exc: BaseException, url: Any, *, setting: str = "DATABASE_URL") -> str | None:
    """Describe why the database at `url` could not be used, or None if `exc` is not that.

    `url` is a string or a SQLAlchemy URL; the password is hidden in the
    message. `setting` names the environment variable that points at it.
    """
    if not is_database_error(exc):
        return None
    where = _masked(url)
    if _sqlstate(exc) == _UNDEFINED_TABLE:
        return f"the database at {where} has no RootSign tables. Run `rootsign-admin init` first."
    return (
        f"cannot use the database at {where}: {_reason(exc)}. "
        f"Check that it is running and that {setting} points at it."
    )


def is_database_error(exc: BaseException) -> bool:
    """True when `exc` means the storage could not be used: an OS error, or an
    asyncpg / psycopg2 error or SQLAlchemy's wrapper for one. Also used by
    `rootsign.session()` to tell a storage failure from a setup mistake."""
    # OSError covers a refused connection, an unknown host and a timeout:
    # asyncpg raises them unwrapped while connecting.
    if isinstance(exc, OSError):
        return True
    kinds = []
    for module, name in _DRIVER_ERRORS:
        try:
            kinds.append(getattr(import_module(module), name))
        except ImportError:
            continue
    return bool(kinds) and isinstance(exc, tuple(kinds))


def _masked(url: Any) -> str:
    try:
        from sqlalchemy.engine import make_url

        return make_url(url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001 - an unparseable URL must not hide the real error
        return "the configured database"


def _sqlstate(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", None) or exc
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


def _reason(exc: BaseException) -> str:
    """The driver's own words, on one line, without a Python class name."""
    orig = getattr(exc, "orig", None) or exc
    if isinstance(orig, socket.gaierror) and orig.strerror:
        return orig.strerror
    if isinstance(orig, OSError):
        if orig.errno:
            return os.strerror(orig.errno)
        # asyncio's "Multiple exceptions: [Errno 61] Connect call failed ..."
        # when every address of the host refused.
        found = re.search(r"\[Errno (\d+)\]", str(orig))
        if found:
            return os.strerror(int(found.group(1)))
    text = str(orig).strip()
    first = text.splitlines()[0] if text else type(orig).__name__
    # SQLAlchemy's asyncpg adapter prefixes "<class 'asyncpg...Error'>: ".
    return re.sub(r"^<class '[^']+'>: ", "", first)
