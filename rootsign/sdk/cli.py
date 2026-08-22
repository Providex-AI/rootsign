"""rootsign — user-facing CLI.

Subcommands:

* `rootsign version` — print the installed package version.
* `rootsign verify <session_id>` — verify the tamper-evident hash chain for
  a session stored in the configured Postgres. Exit 0 = VALID, 1 = TAMPERED
  (a record was altered), 2 = INCOMPLETE (a record is missing); usage errors
  also exit 1. The three-verdict vocabulary is ADR-013 Decision 4b; see also
  Sprint Plan §3.3 and ADR-001/ADR-005.
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
    from rootsign.verdict import exit_code

    result = verify_session_local(str(path))
    _print_result(result, verbose=verbose)
    _spool_breadcrumb(path)
    raise typer.Exit(code=exit_code(result.verdict))


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

    async def _run():
        async with factory() as db:
            return await verify_session(session_id, db)

    from rootsign.verdict import exit_code

    result = asyncio.run(_run())
    _print_result(result, verbose=verbose)
    raise typer.Exit(code=exit_code(result.verdict))


def _print_result(result, *, verbose: bool) -> None:
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
    """Export one session as a self-contained evidence bundle (ADR-014).

    The bundle is a directory of JSON documents plus human-readable reports,
    with a SHA-256 for every file in `manifest.json`. The manifest's own hash is
    printed here — note it somewhere outside the bundle, because it is the only
    value that proves a bundle you receive later is the one that was generated.

    `export <session_id>` reads **Postgres only** in this release. Cloud-backed
    export needs a server read API that does not exist yet, so a cloud-mode user
    exports from the spool with `--local`.

    Examples:

        rootsign export 550e8400-e29b-41d4-a716-446655440001
        rootsign export --local ~/.rootsign/sessions/my_session.jsonl
        rootsign export --local ~/.rootsign/spool/sessions/my_session.jsonl --redact-previews
        rootsign export --check ./evidence-550e8400-e29b-41d4-a716-446655440001
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
        bundle = asyncio.run(_run())
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
