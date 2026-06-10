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

import logging
from typing import TYPE_CHECKING, Any

from rootsign.sdk._async_bridge import _run_sync

if TYPE_CHECKING:
    from rootsign.sdk.client import IngestClient
    from rootsign.sdk.context import SessionContext
    from rootsign.sdk.redaction import RedactionConfig

logger = logging.getLogger("rootsign.sdk.langgraph")


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
            async def _runner() -> Any:
                # The wrapped callable bound here delegates to the *original*
                # invoke so we don't recurse into our own traced_invoke. We
                # adapt to _emit_action_record's (args, kwargs) signature by
                # passing the input dict as the sole positional arg.
                def _call(*_a: Any, **_kw: Any) -> Any:
                    return original_invoke(input, config, **kwargs)

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
            async def _call(*_a: Any, **_kw: Any) -> Any:
                return await original_ainvoke(input, config, **kwargs)

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
