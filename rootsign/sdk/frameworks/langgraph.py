"""LangGraph tool interceptor — see ADR-004.

`LangGraphTracer.wrap_tool` mutates `.invoke` / `.ainvoke` on a LangChain
`BaseTool` in place so the wrapped object remains a `BaseTool` and all
LangChain metadata (`name`, `description`, `args_schema`) survives untouched.

The tracer never raises into the caller on ingest failure (ADR-002): the
tool's own success / exception is the only thing that bubbles out. Ingest
problems are logged at WARNING and replayed by the Sprint 3 WAL drain.

The sync `invoke` path is the subtle one — see ADR-004 § "Sync-path
correctness — running event loop". `_run_sync` detects whether the current
thread already has a running loop and either calls `asyncio.run` directly
or routes through a fresh worker thread.
"""

from __future__ import annotations

import contextvars
import logging
from typing import TYPE_CHECKING, Any

from rootsign.sdk._async_bridge import _run_sync

if TYPE_CHECKING:
    from rootsign.sdk.client import IngestClient
    from rootsign.sdk.context import SessionContext
    from rootsign.sdk.redaction import RedactionConfig

logger = logging.getLogger("rootsign.sdk.langgraph")

# Re-entrancy guard (ADR-004 § "Sync-path correctness"). When `traced_ainvoke`
# awaits the original `ainvoke` on a tool whose underlying function is SYNC,
# LangChain runs that sync function via `run_in_executor(None, self.invoke, …)`
# — and `self.invoke` is our replaced `traced_invoke`. That re-entry lands in a
# ThreadPoolExecutor thread with no running loop, so `_run_sync` spins up a
# fresh event loop and opens a second AsyncSession bound to it, while the
# caller's connection is bound to the outer loop → asyncpg "another operation
# in progress" / "attached to a different loop" (the cold-run flake, issue #3),
# plus a redundant ACTION_RECORD. LangChain's executor uses `copy_context()`,
# so this ContextVar — set around the original call in the outer traced method —
# propagates into that thread, where the re-entrant call sees it and passes
# straight through to the untraced tool. The outer async emit is authoritative.
_emitting: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "rootsign_langgraph_emitting", default=False
)


class LangGraphTracer:
    """Wrap LangChain BaseTool callables so each invocation emits ACTION_RECORD."""

    @staticmethod
    def wrap_tool(
        tool: Any,
        *,
        ctx: SessionContext,
        client: IngestClient,
        redaction_config: RedactionConfig | None = None,
    ) -> Any:
        """Wrap *tool* in place. Returns the same `BaseTool` instance.

        Preserves `.name`, `.description`, and `.args_schema` by mutating the
        existing object rather than constructing a new one. Sets
        `_rootsign_instrumented = True` and `_rootsign_context = ctx` on the
        tool so subsequent passes through `wrap_tools` skip re-wrapping.
        """
        # Import inside the function so the SDK installs cleanly without the
        # langgraph extra. ADR-004 keeps third-party deps lazy.
        from rootsign.sdk.decorator import _emit_action_record

        original_invoke = tool.invoke
        original_ainvoke = tool.ainvoke
        tool_name = tool.name

        def traced_invoke(input: Any, config: Any = None, **kwargs: Any) -> Any:
            if _emitting.get():
                # Re-entrant sync fallback from within traced_ainvoke — the
                # outer async emit owns this call. Run the tool untraced.
                return original_invoke(input, config, **kwargs)

            async def _runner() -> Any:
                # The wrapped callable bound here delegates to the *original*
                # invoke so we don't recurse into our own traced_invoke. We
                # adapt to _emit_action_record's (args, kwargs) signature by
                # passing the input dict as the sole positional arg.
                def _call(*_a: Any, **_kw: Any) -> Any:
                    token = _emitting.set(True)
                    try:
                        return original_invoke(input, config, **kwargs)
                    finally:
                        _emitting.reset(token)

                return await _emit_action_record(
                    func=_call,
                    args=(input,),
                    kwargs={},
                    tool_name=tool_name,
                    client=client,
                    ctx=ctx,
                    redaction_config=redaction_config,
                )

            return _run_sync(_runner())

        async def traced_ainvoke(input: Any, config: Any = None, **kwargs: Any) -> Any:
            if _emitting.get():
                return await original_ainvoke(input, config, **kwargs)

            async def _call(*_a: Any, **_kw: Any) -> Any:
                # Set the guard around the original call so LangChain's
                # sync-in-executor fallback (which copy_context()s this frame
                # into a worker thread) sees it and skips re-emitting.
                token = _emitting.set(True)
                try:
                    return await original_ainvoke(input, config, **kwargs)
                finally:
                    _emitting.reset(token)

            return await _emit_action_record(
                func=_call,
                args=(input,),
                kwargs={},
                tool_name=tool_name,
                client=client,
                ctx=ctx,
                redaction_config=redaction_config,
            )

        # langchain_core's BaseTool is a Pydantic v2 model — direct attribute
        # assignment is blocked by `__setattr__`. `object.__setattr__` bypasses
        # the validation hook so we can mount our traced methods + marker
        # attributes on the existing instance. This preserves all LangChain
        # metadata (name / description / args_schema) by construction.
        object.__setattr__(tool, "invoke", traced_invoke)
        object.__setattr__(tool, "ainvoke", traced_ainvoke)
        object.__setattr__(tool, "_rootsign_instrumented", True)
        object.__setattr__(tool, "_rootsign_context", ctx)
        return tool

    @staticmethod
    def wrap_tools(
        tools: list[Any],
        *,
        ctx: SessionContext,
        client: IngestClient,
        redaction_config: RedactionConfig | None = None,
    ) -> list[Any]:
        """Wrap a list of tools. Drop-in for `ToolNode([...])`.

        Already-wrapped tools (those with `_rootsign_instrumented` truthy) are
        returned unchanged so a `@rootsign.trace`-decorated tool subsequently
        passed through `wrap_tools` is not double-instrumented.
        """
        return [
            t
            if getattr(t, "_rootsign_instrumented", False)
            else LangGraphTracer.wrap_tool(
                t, ctx=ctx, client=client, redaction_config=redaction_config
            )
            for t in tools
        ]
