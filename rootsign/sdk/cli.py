"""rootsign — user-facing CLI (Phase 1 scaffold).

Subcommands land progressively across Sprint 1 → Sprint 3. The decorator and
WAL-replay tooling are the first to ship; `rootsign verify` arrives in Sprint 3
alongside the full hash-chain CLI surface.

For schema/storage operations (init/status/reset), use the operator CLI:
    rootsign-admin --help
"""

from __future__ import annotations

import typer

app = typer.Typer(
    name="rootsign",
    help="RootSign — tamper-evident provenance logging for AI agents (powered by Providex AI).",
    add_completion=False,
    no_args_is_help=True,
)


@app.command()
def version() -> None:
    """Print the installed RootSign SDK version."""
    from importlib.metadata import PackageNotFoundError, version as pkg_version

    try:
        typer.echo(f"rootsign {pkg_version('rootsign')}")
    except PackageNotFoundError:
        typer.echo("rootsign (development install — version unknown)")


@app.command()
def verify(session_id: str = typer.Argument(..., help="Session UUID to verify.")) -> None:
    """Verify the hash chain of a recorded session. (Stub — lands in Sprint 3.)"""
    typer.echo(
        f"`rootsign verify {session_id}` is not implemented yet — "
        "the full hash-chain CLI ships in Sprint 3 alongside the WAL drain. "
        "Use `rootsign.crud.action.verify_chain(...)` from Python for now."
    )
    raise typer.Exit(code=2)


if __name__ == "__main__":
    app()
