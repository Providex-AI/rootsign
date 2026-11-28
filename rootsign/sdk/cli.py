"""rootsign — user-facing CLI.

Subcommands:

* `rootsign version` — print the installed package version.
* `rootsign verify <session_id>` — verify the tamper-evident hash chain for
  a session stored in the configured Postgres. Exit 0 = VALID, 1 = TAMPERED
  (a record was altered), 2 = INCOMPLETE (a record is missing), 3 = nothing
  could be verified (unknown session, missing/unreadable/empty file, bad
  arguments). The three-verdict vocabulary is ADR-013 Decision 4b; see also
  ADR-001/ADR-005.
* `rootsign verify --local <path.jsonl>` — verify a session stored offline
  in a JSONL file. No DB required.
* `rootsign export <session_id>` — write a self-contained evidence bundle for
  a session (ADR-014), printing the manifest hash that anchors it. Postgres
  only in v0.3.0; `--local <path.jsonl>` exports a session file or a spool file
  with no database at all, and `--check <dir>` re-hashes a bundle that arrived.
* `rootsign approve <id>` — approve or reject a pending HiTL action.
  `--list` lists all pending approvals; `--reject` flips the decision;
  `--reason` attaches a free-form note. Sprint 4 / ADR-007.

For schema/storage operations (init/status/reset), use the operator CLI:
    rootsign-admin --help
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from collections.abc import Callable, Coroutine
from typing import Any, NoReturn, Optional
from uuid import UUID

import typer
from typer.core import TyperGroup

# A test seam, deliberately NOT an import. Tests monkeypatch
# `rootsign.sdk.cli.AsyncSessionLocal` to bind the CLI to the test engine
# (tests/integration/test_verify_cli.py, test_approve_cli.py,
# test_show_hn_quickstart.py), so the name has to exist at module scope.
#
# Importing `rootsign.database` here to provide it would drag SQLAlchemy in at
# module-import time, and since ADR-011 the DB stack lives in the optional
# `postgres` extra. On a no-extras install that made EVERY `rootsign ...`
# invocation die with ModuleNotFoundError before Typer could dispatch —
# including `rootsign version` and `rootsign verify --local`, neither of which
# touches a database. That violates T1.2 / exit criterion 1.
#
# `_session_factory()` resolves the real factory on demand instead.
AsyncSessionLocal: Any = None

#: `verify`'s exit code when there was nothing it could verify: an unknown
#: session ID, a session file that is missing, unreadable or empty, or bad
#: arguments. Kept apart from the verdict codes (0/1/2) so a CI job never reads
#: "you pointed me at the wrong thing" as VALID or as TAMPERED. Before this, an
#: unknown session ID reported VALID with 0 records and exit 0.
EXIT_CANNOT_VERIFY = 3


def _is_usage_error(exc: BaseException) -> bool:
    """True for a command-line usage error (mistyped option, unknown command).

    Typer up to 0.25 raises Click's `UsageError`; later Typer releases vendor
    Click as a private module and raise their own copy. Both descend from a
    class named `UsageError`, so match on that instead of importing either.
    """
    return any(cls.__name__ == "UsageError" for cls in type(exc).__mro__)


class _RootSignGroup(TyperGroup):
    """Command-line usage errors exit 3 instead of Click's default 2.

    Click exits 2 on a mistyped option or command, which is `verify`'s
    INCOMPLETE code: `rootsign verify --lcal x.jsonl` in a cron job would read
    as "records missing". The error carries its exit code and Typer exits
    with it, so setting it here is enough. Errors in the top-level options
    (`make_context`) and in subcommands (`invoke`) are both covered.
    """

    def make_context(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return super().make_context(*args, **kwargs)
        except Exception as exc:
            if _is_usage_error(exc):
                exc.exit_code = EXIT_CANNOT_VERIFY
            raise

    def invoke(self, ctx: Any) -> Any:
        try:
            return super().invoke(ctx)
        except Exception as exc:
            if _is_usage_error(exc):
                exc.exit_code = EXIT_CANNOT_VERIFY
            raise


app = typer.Typer(
    cls=_RootSignGroup,
    name="rootsign",
    help="RootSign — tamper-evident provenance logging for AI agents (powered by Providex AI).",
    add_completion=False,
    no_args_is_help=True,
)


def _session_factory() -> Any:
    """Return the AsyncSession factory, importing the DB stack on demand.

    Mirrors `LocalIngestClient._factory` (rootsign/sdk/client.py): a missing
    `postgres` extra becomes the actionable install hint rather than a bare
    ModuleNotFoundError from deep in the import graph. Only the DB-backed
    subcommands call this, so DB-free ones stay usable without the extra.
    """
    if AsyncSessionLocal is not None:
        # Monkeypatched by tests, or already resolved.
        return AsyncSessionLocal
    try:
        from rootsign.database import AsyncSessionLocal as factory
    except ModuleNotFoundError as exc:
        from rootsign.errors import RootSignPostgresExtraRequired

        # Reuse the canonical error so the CLI hint can never drift from the SDK's.
        typer.echo(
            f"Error: {RootSignPostgresExtraRequired(f'missing module: {exc.name}')}",
            err=True,
        )
        raise typer.Exit(code=1) from exc
    return factory


def _run_against_database(
    work: Callable[[], Coroutine[Any, Any, Any]], factory: Any, *, exit_code: int
) -> Any:
    """`asyncio.run(work())`, turning a database it cannot use into one line.

    An unreachable server, a missing database, a refused login or a schema that
    was never initialised used to escape as a long traceback, and on `verify`
    as exit 1, which is TAMPERED's code: a cron job read "the database is
    down" as "the records were altered". Any other exception is a bug and
    still raises.
    """
    from rootsign._cli_db import database_problem

    try:
        return asyncio.run(work())
    except Exception as exc:
        message = database_problem(exc, _database_url(factory))
        if message is None:
            raise
        typer.echo(f"Error: {message}", err=True)
        raise typer.Exit(code=exit_code) from None


def _database_url(factory: Any) -> Any:
    """The URL the factory connects to, or the configured one if it can't say."""
    bind = getattr(factory, "kw", {}).get("bind")
    url = getattr(bind, "url", None)
    if url is not None:
        return url
    from rootsign.config import settings

    return settings.DATABASE_URL


@app.command()
def version() -> None:
    """Print the installed RootSign SDK version."""
    from importlib.metadata import PackageNotFoundError, version as pkg_version

    try:
        typer.echo(f"rootsign {pkg_version('rootsign')}")
    except PackageNotFoundError:
        typer.echo("rootsign (development install — version unknown)")


def _cannot_verify(message: str) -> NoReturn:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=EXIT_CANNOT_VERIFY)


@app.command()
def verify(
    session_id: Optional[str] = typer.Argument(None, help="Session ID to verify (UUID)."),
    local: Optional[Path] = typer.Option(
        None,
        "--local",
        "-l",
        help="Path to a local JSONL session file (no DB required).",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Show per-record hashes for debugging.",
    ),
) -> None:
    """Verify the tamper-evident hash chain for a session.

    Examples:

        rootsign verify 550e8400-e29b-41d4-a716-446655440001

        rootsign verify --local ~/.rootsign/sessions/<session_id>.jsonl

    Exit 0 VALID, 1 TAMPERED, 2 INCOMPLETE, 3 nothing was verified.
    """
    if local is not None:
        _verify_local(local, verbose)
        return
    if session_id is not None:
        # audit #11a: parse the UUID here so a bad value gets the same clean
        # one-line error as `approve`, not a raw ValueError traceback.
        try:
            parsed = UUID(session_id)
        except ValueError:
            _cannot_verify(f"{session_id!r} is not a valid UUID.")
        _verify_remote(parsed, verbose)
        return
    _cannot_verify("provide a session_id or --local path")


def _verify_local(path: Path, verbose: bool) -> None:
    from rootsign.sdk.chain import verify_session_local
    from rootsign.verdict import exit_code

    path = path.expanduser()
    if not path.is_file():
        _cannot_verify(f"no session file at {path}")
    try:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeDecodeError) as exc:
        _cannot_verify(f"cannot read {path}: {exc}")
    if not lines:
        # An empty file holds no evidence either way. The library reports it
        # as TAMPERED (it cannot tell truncation from a blank file), which is
        # the right answer for a verifier, but on the CLI it would send a
        # cron job chasing tampering that a mistyped path explains.
        _cannot_verify(f"{path} is empty; there are no records to verify")

    result = verify_session_local(str(path))
    _print_result(result)
    if verbose:
        _print_records(_local_records(lines))
    _spool_breadcrumb(path)
    raise typer.Exit(code=exit_code(result.verdict))


def _local_records(lines: list[str]) -> list[dict[str, Any]]:
    """The ACTION_RECORD lines of a session file, in sequence order, for --verbose.

    Same selection as `verify_session_local`: a line without `event_type` is a
    legacy store-export record and counts as an action. Lines that do not parse
    are skipped here; the verdict above already reports them.
    """
    import json

    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(record, dict)
            and record.get("event_type", "ACTION_RECORD") == "ACTION_RECORD"
        ):
            records.append(record)
    return sorted(records, key=lambda r: r.get("sequence_number") or 0)


def _print_records(records: list[dict[str, Any]]) -> None:
    """--verbose: each action's sequence, tool, and the two hashes that link it."""
    if not records:
        return
    typer.echo("\n  Records:")
    for r in records:
        typer.echo(f"    #{r.get('sequence_number')}  {r.get('tool_name')}")
        typer.echo(f"        self_hash         {r.get('self_hash')}")
        typer.echo(
            f"        prev_action_hash  {r.get('prev_action_hash') or '(none: first record)'}"
        )


def _spool_breadcrumb(path: Path) -> None:
    """Point at `rootsign-admin sync` when the file being verified is spooled.

    The person who verifies a spool file is usually the developer whose laptop
    went offline, and `sync` lives on the *operator* CLI (ADR-013 Decision 4) —
    a command they have no reason to have read the help for. Verifying the file
    is the moment they are looking straight at the evidence that something is
    waiting to be uploaded, so the pointer belongs here.

    Never fatal: a breadcrumb that could break `verify` would be a bad trade,
    and this reads settings, which a misconfigured environment can refuse.
    """
    try:
        from rootsign.sdk.spool import SYNC_BREADCRUMB, is_spool_path

        if not is_spool_path(path):
            return
    except Exception:  # noqa: BLE001 - a hint is never worth failing verify over
        return

    typer.echo(
        "\nThis is a spooled session — recorded locally because the cloud endpoint was "
        f"unreachable.\nUpload it (and everything else waiting) with:\n\n    "
        f"{SYNC_BREADCRUMB}\n"
    )


def _verify_remote(session_id: UUID, verbose: bool) -> None:
    # The session factory is resolved lazily via `_session_factory()`; tests
    # monkeypatch the module-level `AsyncSessionLocal` seam to point at a test
    # engine. See tests/integration/test_verify_cli.py.
    from rootsign.sdk.chain import verify_session

    # Resolve the factory up front: a missing `postgres` extra should print the
    # install hint and exit, not surface from inside asyncio.run().
    factory = _session_factory()

    from rootsign.crud import action as action_crud
    from rootsign.crud import session as session_crud

    async def _run():
        async with factory() as db:
            # A session ID the database has never seen verifies as an empty,
            # VALID chain at the library level. Catch it here, before a typo
            # in a cron job reads as a clean audit.
            if await session_crud.get(db, session_id) is None:
                return None, []
            result = await verify_session(session_id, db)
            chain = (
                await action_crud.get_session_chain(db, session_id=session_id) if verbose else []
            )
            return result, chain

    from rootsign.verdict import exit_code

    result, chain = _run_against_database(_run, factory, exit_code=EXIT_CANNOT_VERIFY)
    if result is None:
        _cannot_verify(f"no session {session_id} in the database")
    _print_result(result)
    if verbose:
        _print_records(
            [
                {
                    "sequence_number": a.sequence_number,
                    "tool_name": a.tool_name,
                    "self_hash": a.self_hash,
                    "prev_action_hash": a.prev_action_hash,
                }
                for a in chain
            ]
        )
    raise typer.Exit(code=exit_code(result.verdict))


def _print_result(result) -> None:
    from rootsign.verdict import Verdict

    if result.verdict is Verdict.INCOMPLETE:
        # Distinct from TAMPERED on purpose: "records are missing" and "records
        # were altered" call for different responses, and conflating them
        # either cries wolf or misses a theft. Yellow, not red — what is here
        # is intact.
        typer.echo(
            typer.style(
                f"INCOMPLETE ⚠  —  {result.record_count} records present and intact, "
                f"but the chain is missing records",
                fg=typer.colors.YELLOW,
                bold=True,
            )
        )
        if result.error:
            typer.echo(f"  Detail:   {result.error}")
        typer.echo(f"  Session:  {result.session_id}")
        typer.echo(
            typer.style(
                "The records present verify cleanly; the gap is proven by the chain itself. "
                "Check for a spooled session that was never synced "
                "(`rootsign-admin sync`) or a deleted row.",
                fg=typer.colors.YELLOW,
            )
        )
        return

    if result.valid:
        typer.echo(
            typer.style(
                f"VALID ✓  —  {result.record_count} records, chain intact",
                fg=typer.colors.GREEN,
                bold=True,
            )
        )
        typer.echo(f"  Session:  {result.session_id}")
        return

    typer.echo(
        typer.style(
            f"TAMPERED ✗  —  chain broken at record #{result.first_invalid_sequence}",
            fg=typer.colors.RED,
            bold=True,
        )
    )
    if result.error:
        typer.echo(f"  Detail:   {result.error}")
    if getattr(result, "missing_ranges", None):
        # Worst verdict wins, but the gaps are still the operator's problem.
        from rootsign.verdict import describe_missing

        typer.echo(f"  Missing:  sequence {describe_missing(result.missing_ranges)}")
    typer.echo(f"  Session:  {result.session_id}")
    typer.echo(
        typer.style(
            "WARNING: This session log may have been tampered with.",
            fg=typer.colors.YELLOW,
        )
    )


@app.command()
def export(
    session_id: Optional[str] = typer.Argument(
        None, help="Session ID to export (UUID). Reads the configured Postgres."
    ),
    local: Optional[Path] = typer.Option(
        None,
        "--local",
        "-l",
        help="Export from a local JSONL session file (or a spool file). No DB required.",
    ),
    check: Optional[Path] = typer.Option(
        None,
        "--check",
        help="Verify a received bundle directory instead of exporting.",
    ),
    out: Path = typer.Option(
        Path("."), "--out", "-o", help="Directory to write evidence-<session_id>/ into."
    ),
    fmt: str = typer.Option(
        "all",
        "--format",
        "-f",
        help="Which renderings to include: all (default), json, md, html.",
    ),
    redact_previews: bool = typer.Option(
        False,
        "--redact-previews",
        help="Withhold stored payload content (previews, approval context, decision summaries).",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing bundle directory."),
) -> None:
    """Export one session as a self-contained evidence bundle.

    The bundle is a directory of JSON documents plus human-readable reports,
    with a SHA-256 for every file in manifest.json. The manifest's own hash is
    printed at the end. Record it somewhere outside the bundle: it is the only
    value that proves a bundle you receive later is the one that was generated.

    A session ID is read from PostgreSQL. For a JSONL session file or a spool
    file, use --local. --check re-hashes a bundle you received.

    Examples:

        rootsign export 550e8400-e29b-41d4-a716-446655440001

        rootsign export --local ~/.rootsign/sessions/<session_id>.jsonl

        rootsign export --check ./evidence-550e8400-e29b-41d4-a716-446655440001
    \f
    ADR-014. Cloud-backed export needs a server read API that does not exist
    yet, so a cloud-mode user exports from the spool with `--local`.
    """
    if check is not None:
        _check_bundle(check)
        return
    if fmt not in _EXPORT_FORMATS:
        typer.echo(
            f"Error: --format must be one of {', '.join(_EXPORT_FORMATS)}, got {fmt!r}.",
            err=True,
        )
        raise typer.Exit(code=1)
    if local is not None:
        _export_local(local, out, fmt=fmt, redact_previews=redact_previews, force=force)
        return
    if session_id is not None:
        try:
            parsed = UUID(session_id)
        except ValueError:
            typer.echo(f"Error: {session_id!r} is not a valid UUID.", err=True)
            raise typer.Exit(code=1) from None
        _export_remote(parsed, out, fmt=fmt, redact_previews=redact_previews, force=force)
        return
    typer.echo(
        "Error: provide a session_id, --local <path>, or --check <dir>.",
        err=True,
    )
    raise typer.Exit(code=1)


#: `all` writes the JSON documents plus both reports. The narrower values drop
#: the renderings, never the machine truth — a bundle without `verification.json`
#: would be a report, not evidence.
_EXPORT_FORMATS = ("all", "json", "md", "html")


def _export_local(path: Path, out: Path, *, fmt: str, redact_previews: bool, force: bool) -> None:
    from rootsign.sdk.export import export_local

    try:
        bundle = export_local(path, redact_previews=redact_previews)
    except FileNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    _finish_export(bundle, out, fmt=fmt, force=force)


def _export_remote(
    session_id: UUID, out: Path, *, fmt: str, redact_previews: bool, force: bool
) -> None:
    from rootsign.errors import RootSignPostgresExtraRequired
    from rootsign.sdk.export import export_session

    # Resolve the factory first so a missing `postgres` extra prints the install
    # hint here rather than surfacing from inside asyncio.run() (same shape as
    # `verify`).
    factory = _session_factory()

    async def _run():
        async with factory() as db:
            return await export_session(session_id, db, redact_previews=redact_previews)

    try:
        bundle = _run_against_database(_run, factory, exit_code=1)
    except RootSignPostgresExtraRequired as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except LookupError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    _finish_export(bundle, out, fmt=fmt, force=force)


def _finish_export(bundle: Any, out: Path, *, fmt: str, force: bool) -> None:
    """Render, write, and print the anchor."""
    from rootsign.sdk.export import BundleExists, write_bundle
    from rootsign.sdk.report import HTML_FILE, MARKDOWN_FILE, attach_reports

    if fmt != "json":
        attach_reports(bundle)
        if fmt == "md":
            bundle.rendered.pop(HTML_FILE, None)
        elif fmt == "html":
            bundle.rendered.pop(MARKDOWN_FILE, None)

    try:
        directory = write_bundle(bundle, out, overwrite=force)
    except BundleExists as exc:
        typer.echo(f"Error: {exc}  (use --force to overwrite)", err=True)
        raise typer.Exit(code=1) from None
    except OSError as exc:
        typer.echo(f"Error: could not write the bundle ({exc})", err=True)
        raise typer.Exit(code=1) from None

    _print_verdict_banner(bundle.verification)
    typer.echo(f"  Bundle:   {directory}")
    for name in sorted(bundle.files()):
        typer.echo(f"            {name}")
    typer.echo("            manifest.json")
    typer.echo("")
    # The anchor. Everything else in the bundle can be recomputed from the
    # bundle; this is the one value that has to leave with the human.
    typer.echo(typer.style(f"  manifest.json SHA-256:  {bundle.manifest_hash}", bold=True))
    # Two lines, not one: at 130 characters this wrapped mid-word in an
    # 80-100 column terminal, which is where it is actually read.
    typer.echo(
        "  Record that hash outside the bundle — a ticket, an email, a chain-of-custody log."
    )
    typer.echo("  It is what proves a bundle you receive later is the one that was generated.")


def _print_verdict_banner(verification: dict) -> None:
    """The verdict leads the output, as it leads the report."""
    from rootsign.verdict import Verdict

    verdict = verification.get("verdict")
    colors = {
        Verdict.VALID.value: typer.colors.GREEN,
        Verdict.TAMPERED.value: typer.colors.RED,
        Verdict.INCOMPLETE.value: typer.colors.YELLOW,
    }
    # `summary` already leads with the verdict (`VALID — 3 records, ...`), so
    # printing both would stutter.
    typer.echo(
        typer.style(
            verification.get("summary") or str(verdict),
            fg=colors.get(str(verdict), typer.colors.WHITE),
            bold=True,
        )
    )


def _check_bundle(directory: Path) -> None:
    """`rootsign export --check DIR` — re-hash a received bundle.

    Exit 0 when the bundle is internally consistent, 1 otherwise. The manifest
    hash is printed either way: per-file agreement proves only that nobody
    edited a file *without* updating the manifest, and the out-of-band hash is
    the only check an attacker cannot satisfy from inside the bundle.
    """
    from rootsign.sdk.export import check_bundle

    result = check_bundle(directory)

    # `summary` already leads with INTACT / ALTERED / UNREADABLE, so the marker
    # is all that is added here.
    typer.echo(
        typer.style(
            f"{'✓' if result.intact else '✗'}  {result.summary}",
            fg=typer.colors.GREEN if result.intact else typer.colors.RED,
            bold=True,
        )
    )
    for name in result.altered:
        typer.echo(f"  altered:     {name}")
    for name in result.missing:
        typer.echo(f"  missing:     {name}")
    for name in result.unexpected:
        typer.echo(f"  unexpected:  {name}  (not listed in manifest.json)")

    if result.manifest_hash:
        typer.echo("")
        typer.echo(typer.style(f"  manifest.json SHA-256:  {result.manifest_hash}", bold=True))
        typer.echo("  Compare this against the hash recorded when the bundle was exported.")
        typer.echo("  The file checks above cannot detect an edit that also updated the manifest.")
    raise typer.Exit(code=0 if result.intact else 1)


@app.command()
def approve(
    action_id: Optional[str] = typer.Argument(
        None,
        help="ID of the action waiting for approval (UUID). Omit when using --list.",
    ),
    reject: bool = typer.Option(
        False,
        "--reject",
        "-r",
        help="Reject this action instead of approving.",
    ),
    reason: Optional[str] = typer.Option(
        None,
        "--reason",
        help="Optional free-form reason recorded with the decision.",
    ),
    list_pending: bool = typer.Option(
        False,
        "--list",
        "-l",
        help="List the 20 most-recent pending approvals across all sessions.",
    ),
) -> None:
    """Approve or reject a tool call waiting for human approval (PostgreSQL).

    Examples:

        rootsign approve --list
        rootsign approve 550e8400-e29b-41d4-a716-446655440001
        rootsign approve 550e8400-... --reject --reason "Looks risky"
    """
    if list_pending:
        _list_pending_approvals()
        return
    if action_id is None:
        typer.echo(
            "Error: provide an action_id, or use --list to see pending approvals.",
            err=True,
        )
        raise typer.Exit(code=1)
    try:
        parsed = UUID(action_id)
    except ValueError:
        typer.echo(f"Error: {action_id!r} is not a valid UUID.", err=True)
        raise typer.Exit(code=1) from None
    decision = "rejected" if reject else "approved"
    _submit_approval(action_id=parsed, decision=decision, reason=reason)


def _list_pending_approvals() -> None:
    """Print the 20 most-recent pending Actions awaiting human decision.

    Scoped across all sessions for the v0.1.0 single-tenant deployment.
    Phase 2 will add `--session` and `--owner` filters once multi-tenant
    auth ships.
    """
    # Before any DB import — otherwise a no-extras install tracebacks on
    # `from sqlalchemy import select` instead of getting the install hint.
    factory = _session_factory()

    from sqlalchemy import select

    from rootsign.models.action import Action

    async def _run() -> list[Action]:
        async with factory() as db:
            result = await db.execute(
                select(Action)
                .where(Action.authorization_status == "pending")
                .order_by(Action.timestamp.desc())
                .limit(20)
            )
            return list(result.scalars().all())

    actions = _run_against_database(_run, factory, exit_code=1)
    if not actions:
        typer.echo("No pending approvals.")
        return
    typer.echo(f"Pending approvals ({len(actions)}):")
    for action in actions:
        typer.echo(
            f"  {action.action_id}  {action.tool_name:<30}  "
            f"session={action.session_id}  submitted={action.timestamp.isoformat()}"
        )


def _submit_approval(*, action_id: UUID, decision: str, reason: str | None) -> None:
    """Write the APPROVAL_RECORD + flip Action.authorization_status atomically.

    The lookup is intentionally two-step: SELECT first by (action_id,
    authorization_status='pending') so we can echo a clear "not pending"
    message rather than the CRUD's generic ActionAlreadyResolvedError.
    The hypertable-safe (action_id, timestamp) write lives inside
    `create_with_chain_link`.
    """
    import getpass

    # Before any DB import — see _list_pending_approvals.
    factory = _session_factory()

    from sqlalchemy import select

    from rootsign.crud.approval import approval as approval_crud
    from rootsign.errors import ActionAlreadyResolvedError, ActionNotFoundError, SessionClosedError
    from rootsign.models.action import Action

    # audit #7a: attribute the approval to the actual OS operator instead of a
    # hardcoded "cli:operator", so the audit trail records who approved.
    # Keep the "cli:" prefix so the approver namespace stays legible.
    try:
        approver_id = f"cli:{getpass.getuser()}"
    except Exception:  # noqa: BLE001 — getuser() can raise if no username resolvable
        approver_id = "cli:operator"

    async def _run() -> None:
        async with factory() as db:
            result = await db.execute(
                select(Action).where(
                    Action.action_id == action_id,
                    Action.authorization_status == "pending",
                )
            )
            action = result.scalar_one_or_none()
            if action is None:
                typer.echo(
                    f"Error: no pending action found with ID {action_id}.",
                    err=True,
                )
                raise typer.Exit(code=1)
            try:
                await approval_crud.create_with_chain_link(
                    db,
                    action_id=action.action_id,
                    action_timestamp=action.timestamp,
                    approver_id=approver_id,
                    approver_type="human",
                    context_presented=action.input_redacted or {},
                    decision=decision,
                    decision_reason=reason,
                )
                await db.commit()
            except ActionAlreadyResolvedError as exc:
                # The HiTL poll loop's timeout-write raced us — the action
                # transitioned to 'timed_out' between our SELECT and the
                # CRUD's UPDATE. Surface this clearly so the operator
                # knows their decision didn't land.
                typer.echo(
                    f"Error: action {action_id} was already resolved "
                    f"between selection and write ({exc}).",
                    err=True,
                )
                raise typer.Exit(code=1) from exc
            except (ActionNotFoundError, SessionClosedError) as exc:
                typer.echo(f"Error: {exc}", err=True)
                raise typer.Exit(code=1) from exc

    _run_against_database(_run, factory, exit_code=1)
    colour = typer.colors.GREEN if decision == "approved" else typer.colors.RED
    symbol = "✓" if decision == "approved" else "✗"
    typer.echo(
        typer.style(
            f"{symbol}  Action {action_id} {decision}.",
            fg=colour,
            bold=True,
        )
    )


if __name__ == "__main__":
    app()
