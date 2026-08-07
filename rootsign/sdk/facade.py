"""`rootsign.init()` + ambient session resolution — ADR-012.

Three calls are the whole documented quickstart::

    import rootsign

    rootsign.init(agent="invoice-agent", risk_tier="high")   # once, at startup

    async with rootsign.session(objective="process invoice batch"):
        tools = rootsign.wrap_tools([send_invoice])          # no ctx, no client

`init()` is **synchronous and does no I/O** (ADR-012 Decision 2) so it is safe
at module scope in a script *and* inside an already-running event loop
(notebooks, FastAPI startup). It validates its arguments, resolves
`SDKSettings`, and stores an `_InitConfig` singleton. The agent get-or-create
and the client construction happen lazily on first `rootsign.session()` entry,
which is already async.

Ambient context lives in a `ContextVar` (Decision 3) — the only primitive that
propagates across `asyncio` task boundaries, so parallel LangGraph branches
inherit the right session while two concurrent sessions in one process stay
isolated. Resolution order is always: explicit kwargs → ContextVar →
`RootSignNotInitializedError`. **Explicit arguments always win.**

Nothing here holds a session: `_init_config` is the only singleton, and session
state lives exclusively in the ContextVar (Decision 5).
"""

from __future__ import annotations

import asyncio
import contextvars
import getpass
import logging
import os
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rootsign.errors import RootSignNotInitializedError
from rootsign.schemas.agent import AgentEnvironment, AgentFramework, AgentRiskTier

if TYPE_CHECKING:
    from rootsign.sdk.client import IngestClient
    from rootsign.sdk.config import SDKSettings
    from rootsign.sdk.context import SessionContext

logger = logging.getLogger("rootsign.sdk.facade")


# ---------------------------------------------------------------------------
# init() config singleton
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _InitConfig:
    """Validated, I/O-free result of `rootsign.init(...)`.

    Frozen so a stored config can't drift out from under a session that's
    already resolved against it.
    """

    agent: str
    owner: str
    environment: str
    risk_tier: str
    framework: str
    description: str | None
    model_version: str | None
    permitted_tools: tuple[str, ...]
    regulatory_categories: tuple[str, ...]
    settings: SDKSettings

    @property
    def backend(self) -> str:
        return self.settings.BACKEND


_init_config: _InitConfig | None = None


def _default_agent_name() -> str:
    """Entrypoint script name, per ADR-012 Decision 2's zero-arg defaults.

    Falls back to `"rootsign-agent"` for a REPL / notebook / `-c` invocation
    where `sys.argv[0]` is empty or not a real path. The name must satisfy
    `AgentCreate`'s min_length=2.
    """
    argv0 = sys.argv[0] if sys.argv else ""
    stem = os.path.splitext(os.path.basename(argv0))[0]
    if len(stem) < 2 or stem.startswith("-"):
        return "rootsign-agent"
    return stem[:200]


def _default_owner() -> str:
    """Local OS user — `agents.owner` is NOT NULL and has no ADR-given default."""
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 — no passwd entry / no LOGNAME in a container
        return "unknown"
    return user if len(user) >= 2 else "unknown"


def init(
    *,
    agent: str | None = None,
    owner: str | None = None,
    environment: str | AgentEnvironment | None = None,
    risk_tier: str | AgentRiskTier | None = None,
    framework: str | AgentFramework | None = None,
    description: str | None = None,
    model_version: str | None = None,
    permitted_tools: list[str] | None = None,
    regulatory_categories: list[str] | None = None,
) -> None:
    """Configure RootSign once, at startup. Synchronous; performs no I/O.

    Every argument is optional — `rootsign.init()` with no arguments resolves
    to the entrypoint script name, `development`, risk tier `medium`, and
    whatever `ROOTSIGN_BACKEND` says (default `jsonl`, ADR-011). Zero-config
    must reach a verified chain.

    Args:
        agent: Logical agent name. Identity is `(agent, environment)` — the
            same name may exist independently per environment.
        owner: Team or person accountable for the agent. Defaults to the local
            OS user.
        environment: `development` (default) / `staging` / `production`.
        risk_tier: `low` / `medium` (default) / `high` / `critical`.
        framework: `langgraph` / `crewai` / `autogen` / `custom` / `unknown`
            (default — the facade doesn't guess what will be wrapped).
        description, model_version, permitted_tools, regulatory_categories:
            Stored on the Agent record at creation time.

    Idempotent: calling `init()` a second time with identical arguments is a
    no-op. Calling it with *different* arguments raises `ValueError` — two
    different agent identities in one process is the explicit API's job
    (build `SessionContext` / clients directly), not a mutable global's.

    Raises:
        ValueError: on an invalid enum value, or on a conflicting re-init.
    """
    global _init_config

    # Read settings fresh so monkeypatched env vars take effect without a
    # module reload — same contract as get_ingest_client().
    from rootsign.sdk.config import SDKSettings

    try:
        env = AgentEnvironment(environment or AgentEnvironment.DEVELOPMENT).value
        tier = AgentRiskTier(risk_tier or AgentRiskTier.MEDIUM).value
        fw = AgentFramework(framework or AgentFramework.UNKNOWN).value
    except ValueError as exc:
        raise ValueError(f"rootsign.init(): {exc}") from exc

    config = _InitConfig(
        agent=agent or _default_agent_name(),
        owner=owner or _default_owner(),
        environment=env,
        risk_tier=tier,
        framework=fw,
        description=description,
        model_version=model_version,
        permitted_tools=tuple(permitted_tools or ()),
        regulatory_categories=tuple(regulatory_categories or ()),
        settings=SDKSettings(),
    )

    if _init_config is not None and not _same_config(_init_config, config):
        raise ValueError(
            "rootsign.init() was already called with different arguments "
            f"(agent={_init_config.agent!r}/{_init_config.environment!r}, backend="
            f"{_init_config.backend!r} → agent={config.agent!r}/{config.environment!r}, "
            f"backend={config.backend!r}). init() configures one agent identity per "
            "process; for multiple identities use the explicit API "
            "(SessionContext + get_ingest_client)."
        )
    _init_config = config


def _same_config(a: _InitConfig, b: _InitConfig) -> bool:
    """Compare two configs ignoring the `SDKSettings` object's identity."""
    fields = (
        "agent",
        "owner",
        "environment",
        "risk_tier",
        "framework",
        "description",
        "model_version",
        "permitted_tools",
        "regulatory_categories",
    )
    if any(getattr(a, f) != getattr(b, f) for f in fields):
        return False
    return a.settings.model_dump() == b.settings.model_dump()


def get_init_config() -> _InitConfig | None:
    """The stored `init()` config, or None if `init()` was never called."""
    return _init_config


def _reset_init_config() -> None:
    """Test-only: forget the singleton so the next `init()` starts clean."""
    global _init_config
    _init_config = None


# ---------------------------------------------------------------------------
# Ambient session (ContextVar)
# ---------------------------------------------------------------------------

# Set by `rootsign.session()` on entry, reset on exit (always, in a finally —
# a leak here shows up as cross-test pollution under pytest-asyncio's per-test
# event loops).
_current_session: contextvars.ContextVar[tuple[SessionContext, IngestClient] | None] = (
    contextvars.ContextVar("rootsign_current_session", default=None)
)


def _set_current_session(
    ctx: SessionContext, client: IngestClient
) -> contextvars.Token[tuple[SessionContext, IngestClient] | None]:
    return _current_session.set((ctx, client))


def _reset_current_session(
    token: contextvars.Token[tuple[SessionContext, IngestClient] | None],
) -> None:
    _current_session.reset(token)


def current_session() -> tuple[SessionContext, IngestClient] | None:
    """The ambient `(ctx, client)` pair, or None outside a `rootsign.session()`."""
    return _current_session.get()


def _resolve_ctx_client(
    ctx: SessionContext | None,
    client: IngestClient | None,
    *,
    surface: str | None = None,
) -> tuple[SessionContext, IngestClient]:
    """Resolve a (ctx, client) pair — explicit → ContextVar → raise (ADR-012).

    Resolution is per-argument: an explicit `ctx=` with an implicit client is
    valid (and is what a test that builds its own SessionContext inside a
    facade session gets). Explicit arguments always win.

    Args:
        surface: Name of the calling surface (e.g. `"wrap_tools"`) — used only
            to prefix the error message.

    Raises:
        RootSignNotInitializedError: when either half is still unresolved.
    """
    if ctx is not None and client is not None:
        return ctx, client

    ambient = _current_session.get()
    if ambient is not None:
        ambient_ctx, ambient_client = ambient
        ctx = ctx if ctx is not None else ambient_ctx
        client = client if client is not None else ambient_client

    if ctx is None or client is None:
        raise RootSignNotInitializedError(surface)
    return ctx, client


# ---------------------------------------------------------------------------
# Lazy backend resolution for rootsign.session()
# ---------------------------------------------------------------------------


async def _resolve_agent_id(config: _InitConfig) -> Any:
    """Get-or-create the configured agent and return its UUID.

    This is where `init()`'s deferred I/O finally happens (ADR-012 Decision 2),
    which is why it's async. Backend-dispatched inside `get_or_register_agent`.
    """
    from rootsign.sdk.registration import get_or_register_agent

    return await get_or_register_agent(
        name=config.agent,
        environment=config.environment,
        owner=config.owner,
        risk_tier=config.risk_tier,
        framework=config.framework,
        description=config.description,
        model_version=config.model_version,
        permitted_tools=list(config.permitted_tools),
        regulatory_categories=list(config.regulatory_categories),
        settings=config.settings,
    )


def _build_client(config: _InitConfig) -> IngestClient:
    """Build the backend's IngestClient for a facade-managed session."""
    if config.backend == "postgres":
        # The facade has no caller-supplied AsyncSession to plumb, and
        # LocalIngestClient never commits — its callers do. So the facade owns
        # a per-record session/transaction (see ManagedLocalIngestClient).
        from rootsign.sdk.client import ManagedLocalIngestClient

        client: IngestClient = ManagedLocalIngestClient()
        if config.settings.BUFFERED:
            from rootsign.sdk.buffered_client import BufferedIngestClient

            client = BufferedIngestClient(
                client,
                flush_interval_seconds=config.settings.BUFFER_INTERVAL,
                max_buffer_size=config.settings.BUFFER_MAX_SIZE,
            )
    else:
        from rootsign.sdk.client import get_ingest_client

        client = get_ingest_client()

    return client


async def _maybe_close(client: IngestClient) -> None:
    """Best-effort `close()` on a client that owns resources (ADR-002 isolation).

    Duck-typed so no IngestClient ABC change is needed. Only the facade calls
    this — a caller-supplied client's lifetime belongs to the caller.
    """
    close = getattr(client, "close", None)
    if not callable(close):
        return
    try:
        result = close()
        if asyncio.iscoroutine(result):
            await result
    except Exception as exc:  # noqa: BLE001
        logger.warning("rootsign client close failed: %s", exc)
