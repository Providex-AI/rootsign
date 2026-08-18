"""rootsign — user-facing CLI.

Subcommands:

* `rootsign version` — print the installed package version.
* `rootsign verify <session_id>` — verify the tamper-evident hash chain for
  a session stored in the configured Postgres. Exit 0 = VALID, exit 1 =
  TAMPERED or error. See Sprint Plan §3.3 and ADR-001/ADR-005.
* `rootsign verify --local <path.jsonl>` — verify a session stored offline
  in a JSONL file. No DB required.
* `rootsign approve <id>` — approve or reject a pending HiTL action.
  `--list` lists all pending approvals; `--reject` flips the decision;
  `--reason` attaches a free-form note. Sprint 4 / ADR-007.

For schema/storage operations (init/status/reset), use the operator CLI:
    rootsign-admin --help
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

import typer

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

app = typer.Typer(
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


@app.command()
def version() -> None:
    """Print the installed RootSign SDK version."""
    from importlib.metadata import PackageNotFoundError, version as pkg_version

    try:
        typer.echo(f"rootsign {pkg_version('rootsign')}")
    except PackageNotFoundError:
        typer.echo("rootsign (development install — version unknown)")


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

        rootsign verify --local ~/.rootsign/sessions/my_session.jsonl
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
            typer.echo(f"Error: {session_id!r} is not a valid UUID.", err=True)
            raise typer.Exit(code=1) from None
        _verify_remote(parsed, verbose)
        return
    typer.echo("Error: provide a session_id or --local path", err=True)
    raise typer.Exit(code=1)


def _verify_local(path: Path, verbose: bool) -> None:
    from rootsign.sdk.chain import verify_session_local

    result = verify_session_local(str(path))
    _print_result(result, verbose=verbose)
    raise typer.Exit(code=0 if result.valid else 1)


def _verify_remote(session_id: UUID, verbose: bool) -> None:
    # The session factory is resolved lazily via `_session_factory()`; tests
    # monkeypatch the module-level `AsyncSessionLocal` seam to point at a test
    # engine. See tests/integration/test_verify_cli.py.
    from rootsign.sdk.chain import verify_session

    # Resolve the factory up front: a missing `postgres` extra should print the
    # install hint and exit, not surface from inside asyncio.run().
    factory = _session_factory()

    async def _run():
        async with factory() as db:
            return await verify_session(session_id, db)

    result = asyncio.run(_run())
    _print_result(result, verbose=verbose)
    raise typer.Exit(code=0 if result.valid else 1)


def _print_result(result, *, verbose: bool) -> None:
    if result.valid:
        typer.echo(
            typer.style(
                f"VALID ✓  —  {result.record_count} records, chain intact",
                fg=typer.colors.GREEN,
                bold=True,
            )
        )
        typer.echo(f"  Session:  {result.session_id}")
        if verbose and result.record_count > 0:
            # Verbose detail is only meaningful for local JSONL verification
            # right now — the DB path returns aggregate verdict only. Skip
            # the per-record breakdown gracefully when unavailable.
            typer.echo("  (per-record detail not yet implemented for DB path)")
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
    typer.echo(f"  Session:  {result.session_id}")
    typer.echo(
        typer.style(
            "WARNING: This session log may have been tampered with.",
            fg=typer.colors.YELLOW,
        )
    )


@app.command()
def approve(
    action_id: Optional[str] = typer.Argument(
        None,
        help="Action ID of the pending HiTL action (UUID). Omit when using --list.",
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
    """Approve or reject a pending HiTL action.

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

    actions = asyncio.run(_run())
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
    from rootsign.errors import ActionAlreadyResolvedError, ActionNotFoundError
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
            except ActionNotFoundError as exc:
                typer.echo(f"Error: {exc}", err=True)
                raise typer.Exit(code=1) from exc

    asyncio.run(_run())
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
