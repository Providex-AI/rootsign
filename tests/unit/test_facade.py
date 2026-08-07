"""Unit tests for `rootsign.init()` + ambient session resolution (ADR-012, W3).

DB-free: everything here runs on the JSONL backend (ADR-011) against `tmp_path`,
so these also stand as the no-Docker onboarding proof.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

import pytest

import rootsign
from rootsign.errors import RootSignNotInitializedError
from rootsign.sdk import facade


@pytest.fixture(autouse=True)
def _jsonl_facade_env(monkeypatch, tmp_path):
    """Point the facade at a jsonl backend in tmp_path, and reset global state.

    The repo's dev `.env` pins `ROOTSIGN_BACKEND=local` and conftest pins
    `postgres` in the environment, so the backend has to be set explicitly here
    (env vars beat `.env`). Resetting `_init_config` on both sides of the test
    keeps the singleton from leaking between tests.
    """
    monkeypatch.setenv("ROOTSIGN_BACKEND", "jsonl")
    monkeypatch.setenv("ROOTSIGN_DATA_DIR", str(tmp_path))
    facade._reset_init_config()
    yield
    facade._reset_init_config()


def _session_file(tmp_path, session_id):
    return tmp_path / "sessions" / f"{session_id}.jsonl"


# --------------------------------------------------------------------------
# T3.1 / T3.4 — init() validation, idempotency, zero-arg defaults
# --------------------------------------------------------------------------


def test_init_stores_validated_config():
    rootsign.init(agent="invoice-agent", risk_tier="high", environment="production")
    config = facade.get_init_config()
    assert config is not None
    assert (config.agent, config.environment, config.risk_tier) == (
        "invoice-agent",
        "production",
        "high",
    )
    assert config.backend == "jsonl"


def test_init_zero_arg_defaults_to_script_name_development_medium():
    """T3.4 — zero-config must be enough to reach a verified chain."""
    rootsign.init()
    config = facade.get_init_config()
    assert config is not None
    assert config.environment == "development"
    assert config.risk_tier == "medium"
    assert config.framework == "unknown"
    assert config.backend == "jsonl"
    # sys.argv[0] under pytest is the pytest entrypoint; the fallback covers
    # a REPL / `-c` invocation where argv[0] isn't a usable name.
    assert len(config.agent) >= 2


def test_init_is_idempotent_for_identical_args():
    rootsign.init(agent="a", risk_tier="low")
    first = facade.get_init_config()
    rootsign.init(agent="a", risk_tier="low")
    assert facade.get_init_config() == first


def test_init_with_different_args_raises():
    rootsign.init(agent="a")
    with pytest.raises(ValueError, match="already called with different arguments"):
        rootsign.init(agent="b")


def test_init_rejects_invalid_enum_value():
    with pytest.raises(ValueError, match="rootsign.init"):
        rootsign.init(agent="a", risk_tier="catastrophic")


def test_init_does_no_io(tmp_path):
    """ADR-012 Decision 2 — no file writes, so nothing lands in DATA_DIR."""
    rootsign.init(agent="lazy-agent")
    assert not (tmp_path / "agents.jsonl").exists()
    assert not (tmp_path / "sessions").exists()


def test_init_works_inside_a_running_event_loop():
    """init() must be sync + loop-safe (notebooks, FastAPI startup)."""

    async def main():
        rootsign.init(agent="in-loop")
        return facade.get_init_config()

    assert asyncio.run(main()).agent == "in-loop"


def test_agent_name_falls_back_when_argv0_is_not_a_usable_name(monkeypatch):
    """A REPL / `python -c` invocation has no script name to borrow."""
    monkeypatch.setattr("sys.argv", ["-c"])
    assert facade._default_agent_name() == "rootsign-agent"
    monkeypatch.setattr("sys.argv", [])
    assert facade._default_agent_name() == "rootsign-agent"


def test_owner_falls_back_when_the_os_user_is_unavailable(monkeypatch):
    """No passwd entry / no LOGNAME (a slim container) must not break init()."""
    monkeypatch.setattr("getpass.getuser", lambda: (_ for _ in ()).throw(OSError()))
    assert facade._default_owner() == "unknown"
    monkeypatch.setattr("getpass.getuser", lambda: "x")  # too short for min_length=2
    assert facade._default_owner() == "unknown"


def test_unsupported_backend_is_rejected_at_first_session(monkeypatch):
    """`cloud` has no agent registry until Phase 2 — fail with a clear message."""
    monkeypatch.setenv("ROOTSIGN_BACKEND", "cloud")
    rootsign.init(agent="cloud-agent")

    async def main():
        async with rootsign.session():
            pass

    with pytest.raises(ValueError, match="does not support ROOTSIGN_BACKEND"):
        asyncio.run(main())


@pytest.mark.asyncio
async def test_maybe_close_swallows_and_logs_a_failing_close(caplog):
    class Boom:
        async def close(self):
            raise RuntimeError("nope")

    with caplog.at_level(logging.WARNING, logger="rootsign.sdk.facade"):
        await facade._maybe_close(Boom())  # ADR-002: never raises out
    assert "client close failed" in caplog.text


@pytest.mark.asyncio
async def test_buffered_setting_wraps_the_facade_client(monkeypatch):
    """ADR-012 Decision 5 keeps buffering opt-in — via ROOTSIGN_BUFFERED, not init()."""
    monkeypatch.setenv("ROOTSIGN_BUFFERED", "true")
    monkeypatch.setenv("ROOTSIGN_BACKEND", "postgres")
    rootsign.init(agent="buffered-agent")
    config = facade.get_init_config()
    assert config is not None

    client = facade._build_client(config)
    assert isinstance(client, rootsign.BufferedIngestClient)
    assert isinstance(client._inner, rootsign.ManagedLocalIngestClient)


# --------------------------------------------------------------------------
# T3.2 — resolution order: explicit → ContextVar → raise
# --------------------------------------------------------------------------


def test_resolve_raises_outside_a_session():
    with pytest.raises(RootSignNotInitializedError) as exc:
        facade._resolve_ctx_client(None, None, surface="wrap_tools")
    # The message must carry the two-line fix — this is a first-run error.
    assert "rootsign.init(" in str(exc.value)
    assert "rootsign.session(" in str(exc.value)


@pytest.mark.asyncio
async def test_explicit_args_win_over_ambient_context():
    rootsign.init(agent="ambient-agent")
    explicit_ctx = rootsign.SessionContext(agent_id=UUID(int=7))
    explicit_client = rootsign.JsonlIngestClient(data_dir="/dev/null-not-used")

    async with rootsign.session(objective="o") as ambient_ctx:
        ctx, client = facade._resolve_ctx_client(explicit_ctx, explicit_client)
        assert ctx is explicit_ctx and client is explicit_client
        # Resolution is per-argument: an explicit ctx still picks up the
        # ambient client.
        ctx, client = facade._resolve_ctx_client(explicit_ctx, None)
        assert ctx is explicit_ctx and client is not explicit_client
        # And the all-implicit form yields the session's own pair.
        ctx, _ = facade._resolve_ctx_client(None, None)
        assert ctx is ambient_ctx


@pytest.mark.asyncio
async def test_context_var_is_reset_after_the_session_exits():
    rootsign.init(agent="leak-check")
    async with rootsign.session():
        assert facade.current_session() is not None
    assert facade.current_session() is None


@pytest.mark.asyncio
async def test_context_var_is_reset_even_when_the_body_raises():
    rootsign.init(agent="leak-check-raise")
    with pytest.raises(RuntimeError):
        async with rootsign.session():
            raise RuntimeError("boom")
    assert facade.current_session() is None


@pytest.mark.asyncio
async def test_session_without_init_raises_not_initialized():
    with pytest.raises(RootSignNotInitializedError):
        async with rootsign.session():
            pass


# --------------------------------------------------------------------------
# End-to-end: the documented 6-line quickstart, verified
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_facade_quickstart_produces_a_verified_chain(tmp_path):
    rootsign.init(agent="quickstart-agent")

    @rootsign.trace()
    async def add(a: int, b: int) -> int:
        return a + b

    async with rootsign.session(objective="add some numbers") as ctx:
        assert await add(2, 3) == 5
        assert await add(4, 5) == 9

    result = rootsign.verify_session_local(str(_session_file(tmp_path, ctx.session_id)))
    assert result.valid is True
    assert result.record_count == 2


@pytest.mark.asyncio
async def test_trace_resolves_ambient_client_per_call_not_at_decoration(tmp_path):
    """The decorator normally runs at import time — before any session exists."""
    rootsign.init(agent="deferred-agent")

    @rootsign.trace()
    async def echo(value: str) -> str:
        return value

    # Decorated, but no session yet.
    with pytest.raises(RootSignNotInitializedError):
        await echo("nope")

    async with rootsign.session() as ctx:
        assert await echo("yes") == "yes"

    assert rootsign.verify_session_local(
        str(_session_file(tmp_path, ctx.session_id))
    ).valid is True


@pytest.mark.asyncio
async def test_agent_is_get_or_created_once_across_sessions(tmp_path):
    rootsign.init(agent="stable-agent", environment="staging")

    async with rootsign.session() as first:
        pass
    async with rootsign.session() as second:
        pass

    assert first.agent_id == second.agent_id
    # One line in the registry — re-running a script never re-registers.
    lines = (tmp_path / "agents.jsonl").read_text().splitlines()
    assert len([ln for ln in lines if ln.strip()]) == 1


@pytest.mark.asyncio
async def test_attribute_drift_warns_and_keeps_stored_values(tmp_path, caplog):
    rootsign.init(agent="drift-agent", risk_tier="low", owner="alice")
    async with rootsign.session() as first:
        pass

    facade._reset_init_config()
    rootsign.init(agent="drift-agent", risk_tier="critical", owner="bob")
    with caplog.at_level(logging.WARNING, logger="rootsign.sdk.registration"):
        async with rootsign.session() as second:
            pass

    assert first.agent_id == second.agent_id  # same identity, not re-registered
    assert "already registered with different attributes" in caplog.text
    assert "risk_tier" in caplog.text
    stored = (tmp_path / "agents.jsonl").read_text()
    assert '"risk_tier":"low"' in stored  # stored values win
    assert "critical" not in stored


@pytest.mark.asyncio
async def test_hitl_on_jsonl_raises_on_first_invocation_when_headless(monkeypatch):
    """Founder decision 2 — the check fires at first call, before the body runs.

    The facade resolves the backend lazily, so wrap time can't know which
    backend it will get; `_emit_hitl_action` raises instead.
    """
    from rootsign.errors import HiTLUnsupportedBackendError

    # Headless: no TTY, so the JSONL backend's inline prompt isn't available.
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    rootsign.init(agent="hitl-agent")

    ran = False

    @rootsign.trace(require_approval=True)
    async def dangerous() -> str:
        nonlocal ran
        ran = True
        return "did it"

    async with rootsign.session():
        with pytest.raises(HiTLUnsupportedBackendError):
            await dangerous()

    assert ran is False


# --------------------------------------------------------------------------
# T3.5 — concurrency
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_concurrent_sessions_stay_isolated(tmp_path):
    """Distinct session files, distinct chains — the ContextVar is per-task."""
    rootsign.init(agent="concurrent-agent")

    @rootsign.trace()
    async def work(n: int) -> int:
        await asyncio.sleep(0)  # force interleaving
        return n

    async def one_session(count: int) -> UUID:
        async with rootsign.session(objective=f"n={count}") as ctx:
            for i in range(count):
                assert await work(i) == i
            return ctx.session_id

    first, second = await asyncio.gather(one_session(2), one_session(3))

    assert first != second
    first_result = rootsign.verify_session_local(str(_session_file(tmp_path, first)))
    second_result = rootsign.verify_session_local(str(_session_file(tmp_path, second)))
    assert (first_result.valid, first_result.record_count) == (True, 2)
    assert (second_result.valid, second_result.record_count) == (True, 3)


@pytest.mark.asyncio
async def test_parallel_tool_calls_in_one_session_keep_monotonic_sequences(tmp_path):
    rootsign.init(agent="parallel-agent")

    @rootsign.trace()
    async def branch(n: int) -> int:
        await asyncio.sleep(0)
        return n

    async with rootsign.session() as ctx:
        results = await asyncio.gather(*(branch(i) for i in range(8)))

    assert results == list(range(8))
    assert ctx.current_sequence == 8
    result = rootsign.verify_session_local(str(_session_file(tmp_path, ctx.session_id)))
    assert result.valid is True
    assert result.record_count == 8
