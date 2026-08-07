"""CrewAI tool interceptor — see ADR-005.

`CrewAITracer.wrap_tool` mutates `._run` on a CrewAI tool in place so the
wrapped object remains a `crewai.tools.BaseTool` (or `@tool`-decorated
`Tool`) and CrewAI's own metadata (`name`, `description`, `args_schema`)
survives untouched.

Duck typing — not `isinstance` — is used so this module never imports
`crewai` at module load. The SDK installs cleanly without the `crewai`
extra.

Sync-path correctness: `_run` is synchronous and CrewAI agents are
routinely embedded in async pipelines. The tracer therefore uses the same
`_run_sync` bridge that ADR-004 introduced for LangGraph, now extracted to
`rootsign.sdk._async_bridge`.

Failure isolation (ADR-002): the tool's own success / exception is the
only thing that bubbles to the caller. Ingest errors are logged at
WARNING and swallowed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from rootsign.sdk._async_bridge import _run_sync

if TYPE_CHECKING:
    from rootsign.sdk.client import IngestClient
    from rootsign.sdk.context import SessionContext
    from rootsign.sdk.redaction import RedactionConfig

logger = logging.getLogger("rootsign.sdk.crewai")


def _is_crewai_tool(obj: Any) -> bool:
    """Duck-type check for CrewAI tools.

    True iff *obj* has `.name: str` and `._run: callable` and is not
    already instrumented. Never imports `crewai` — safe when the extra
    is not installed.

    Note: LangChain `StructuredTool` instances also satisfy this shape.
    The `@rootsign.trace` decorator handles disambiguation by checking
    `_is_langchain_tool` first — see ADR-005.
    """
    return (
        hasattr(obj, "name")
        and isinstance(obj.name, str)
        and hasattr(obj, "_run")
        and callable(obj._run)
        and not getattr(obj, "_rootsign_instrumented", False)
    )


class CrewAITracer:
    """Wrap CrewAI tool callables so each invocation emits ACTION_RECORD."""

    @staticmethod
    def wrap_tool(
        tool: Any,
        ctx: SessionContext | None = None,
        client: IngestClient | None = None,
        redaction_config: RedactionConfig | None = None,
    ) -> Any:
        """Wrap *tool* in place. Returns the same tool instance.

        Preserves `.name`, `.description`, and `.args_schema` by mutating
        the existing object (`object.__setattr__` to bypass Pydantic v2
        validation, which blocks direct `tool._run = ...` assignment on
        `BaseTool` subclasses).

        Sets `_rootsign_instrumented = True` and `_rootsign_context = ctx`
        so subsequent passes through `wrap_tools` skip re-wrapping.

        `ctx`/`client` may be omitted (ADR-012): they are then resolved from
        the ambient `rootsign.session()` at each **invocation** rather than
        here, since tools are typically built at import time. Explicit
        arguments always win; if neither is available at call time,
        `RootSignNotInitializedError` is raised.
        """
        # Imported here so the SDK is loadable without crewai installed.
        from rootsign.sdk.decorator import _emit_action_record
        from rootsign.sdk.facade import _resolve_ctx_client

        original_run = tool._run
        tool_name = tool.name

        async def _emit(
            captured_args: tuple,
            captured_kwargs: dict,
            call_ctx: SessionContext,
            call_client: IngestClient,
        ) -> Any:
            def _call(*_a: Any, **_kw: Any) -> Any:
                return original_run(*captured_args, **captured_kwargs)

            return await _emit_action_record(
                func=_call,
                args=captured_args,
                kwargs=captured_kwargs,
                tool_name=tool_name,
                client=call_client,
                ctx=call_ctx,
                redaction_config=redaction_config,
            )

        def traced_run(*args: Any, **kwargs: Any) -> Any:
            # Resolve in the caller's frame — `_run_sync` may hop threads, and
            # the ambient session is guaranteed visible here.
            call_ctx, call_client = _resolve_ctx_client(
                ctx, client, surface="wrap_crewai_tools"
            )
            return _run_sync(_emit(args, kwargs, call_ctx, call_client))

        async def traced_arun(*args: Any, **kwargs: Any) -> Any:
            """Awaitable shadow of `_run`.

            Public surface used by integration tests and any caller that
            already owns the event loop bound to the `LocalIngestClient`
            AsyncSession. Production CrewAI agents call sync `_run`; this
            coroutine exists so async tests can drive the same emission
            logic without crossing loop boundaries via `_run_sync`. Not
            part of the CrewAI BaseTool contract — it lives only on the
            instrumented instance.
            """
            call_ctx, call_client = _resolve_ctx_client(
                ctx, client, surface="wrap_crewai_tools"
            )
            return await _emit(args, kwargs, call_ctx, call_client)

        # CrewAI BaseTool is a Pydantic v2 model — direct attribute
        # assignment is rejected. `object.__setattr__` bypasses the
        # validation hook so we can mount our traced methods and marker
        # attributes on the existing instance.
        object.__setattr__(tool, "_run", traced_run)
        object.__setattr__(tool, "_rootsign_arun", traced_arun)
        object.__setattr__(tool, "_rootsign_instrumented", True)
        object.__setattr__(tool, "_rootsign_context", ctx)
        return tool

    @staticmethod
    def wrap_tools(
        tools: list[Any],
        ctx: SessionContext | None = None,
        client: IngestClient | None = None,
        redaction_config: RedactionConfig | None = None,
    ) -> list[Any]:
        """Wrap a list of CrewAI tools. Drop-in for `Agent(tools=[...])`.

        Already-wrapped tools (those with `_rootsign_instrumented` truthy)
        are returned unchanged — same double-wrap guard as
        `LangGraphTracer.wrap_tools`.

        `ctx`/`client` may be omitted — see `wrap_tool` for the ADR-012
        implicit-resolution contract.
        """
        return [
            t
            if getattr(t, "_rootsign_instrumented", False)
            else CrewAITracer.wrap_tool(
                t, ctx=ctx, client=client, redaction_config=redaction_config
            )
            for t in tools
        ]
